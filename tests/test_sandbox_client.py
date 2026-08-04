"""Behaviour of the in-sandbox `amux` shim (`amux.sandbox_client`).

Every test drives `sandbox_client.main` exactly as the shim is invoked inside a
microVM: argv plus a mode-0600 config file naming the endpoint and capability
token. The service is a stdlib fake, so these tests depend on nothing from the
host store, the real context service, or Docker.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from amux import sandbox_client as sc
from test_sandbox_client_fake_service import TOKEN, FakeContextService

# --- canned service payloads -------------------------------------------------

SELF = {
    "pane": "%7",
    "name": "brave-hawk",
    "agent": "claude",
    "label": "r0c1",
    "task": "fix",
    "workspace": "myproj",
    "state": "busy",
    "cwd": "/work/repo",
    "branch": "amux/myproj/fix/brave-hawk",
    "repo": "/work/repo",
    "runtime": "docker-sandbox",
    "runtime_status": "running",
    "sandbox_name": "amux-myproj-fix-brave-hawk-ab12cd",
    "sandbox_id": "sbx_0001",
    "last_event": None,
}

TEAMMATE = {
    "pane": "%9",
    "name": "golden-owl",
    "agent": "codex",
    "label": "r0c0",
    "state": "idle",
    "cwd": "/work/repo",
    "branch": "amux/myproj/fix/golden-owl",
    "last_event": {"kind": "stop", "ts": 1_000_000.0, "detail": "done"},
}

NOTE = {
    "id": 4,
    "ts": 1_000_000.0,
    "worktree_id": 2,
    "repo": "/work/repo",
    "workspace": "myproj",
    "task": "fix",
    "pane": "%7",
    "agent": "claude",
    "scope": "task",
    "kind": "decision",
    "text": "use sqlite, not jsonl",
}

CONTEXT = {
    "self": SELF,
    "team": [{"task": "fix", "agents": [SELF, TEAMMATE]}],
    "notes": [NOTE],
}

PANE_STATES = {
    "panes": [
        {
            "pane": "%7",
            "kind": "amux",
            "workspace": "myproj",
            "task": "fix",
            "agent": "claude",
            "name": "brave-hawk",
            "label": "r0c1",
            "state": "busy",
            "last_event": None,
        },
        {
            "pane": "%9",
            "kind": "amux",
            "workspace": "myproj",
            "task": "fix",
            "agent": "codex",
            "name": "golden-owl",
            "label": "r0c0",
            "state": "idle",
            "last_event": {"kind": "stop", "ts": 1_000_000.0, "detail": "done"},
        },
    ]
}


# --- harness -----------------------------------------------------------------


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point the shim at a config file whose endpoint a test fills in."""

    path = tmp_path / "context.json"

    def write(endpoint: str, token: str = TOKEN, **extra) -> str:
        path.write_text(json.dumps({"endpoint": endpoint, "token": token, **extra}))
        path.chmod(0o600)
        monkeypatch.setenv(sc.CONFIG_ENV, str(path))
        return str(path)

    write.path = path  # type: ignore[attr-defined]
    monkeypatch.setenv(sc.CONFIG_ENV, str(path))
    return write


@pytest.fixture
def run(config):
    """`run(routes, "ctx", "--json")` -> (rc, stdout, stderr, service)."""

    def _run(routes, *argv, **kwargs):
        with FakeContextService(routes) as service:
            config(service.endpoint, **kwargs)
            rc = sc.main(list(argv))
            return rc, service

    return _run


def out(capsys) -> str:
    return capsys.readouterr().out


# --- ctx ---------------------------------------------------------------------


def test_ctx_json_is_the_services_context_document(run, capsys):
    rc, service = run({("GET", "/v1/context"): CONTEXT}, "ctx", "--json")
    assert rc == 0
    assert json.loads(out(capsys)) == CONTEXT
    assert service.only("GET", "/v1/context").authorization == f"Bearer {TOKEN}"


def test_ctx_human_output_keeps_the_native_shape(run, capsys):
    rc, _ = run({("GET", "/v1/context"): CONTEXT}, "ctx")
    assert rc == 0
    lines = out(capsys).splitlines()
    # the identity line the native renderer produces, field for field
    assert lines[0].startswith("you: brave-hawk  claude @r0c1 %7  task:fix  workspace:myproj  busy")
    assert "branch:amux/myproj/fix/brave-hawk" in lines[0]
    # one additive line: an agent that cannot tell it is sandboxed will reach for
    # host commands. Every native line keeps its shape around it.
    assert lines[1] == "runtime: docker-sandbox running amux-myproj-fix-brave-hawk-ab12cd"
    assert lines[2] == "team @ myproj"
    assert any("brave-hawk" in ln and ln.endswith("(you)") for ln in lines)
    assert any("golden-owl" in ln and "idle" in ln for ln in lines)
    assert any(ln.startswith("notes @ myproj/fix") for ln in lines)
    assert any("[decision:task]" in ln and "use sqlite, not jsonl" in ln for ln in lines)


def test_ctx_reports_the_sandbox_runtime_it_is_running_in(run, capsys):
    run({("GET", "/v1/context"): CONTEXT}, "ctx")
    assert "docker-sandbox" in out(capsys)


# The runtime line's exact shape is shared with the host renderer (task 5.4), so
# it is pinned here component by component rather than only end to end.
@pytest.mark.parametrize(
    "fields,expected",
    [
        (
            {"runtime": "docker-sandbox", "runtime_status": "running", "sandbox_name": "box-a1b2"},
            "runtime: docker-sandbox running box-a1b2",
        ),
        (
            {"runtime": "docker-sandbox", "runtime_status": "running", "sandbox_name": ""},
            "runtime: docker-sandbox running",
        ),
        (
            {"runtime": "docker-sandbox", "runtime_status": "", "sandbox_name": ""},
            "runtime: docker-sandbox",
        ),
        (
            # an empty component takes its preceding space with it
            {"runtime": "docker-sandbox", "runtime_status": "", "sandbox_name": "box-a1b2"},
            "runtime: docker-sandbox box-a1b2",
        ),
        ({"runtime": "host", "runtime_status": "running", "sandbox_name": ""}, ""),
        ({}, ""),
    ],
)
def test_the_runtime_line_shape_is_exactly_the_agreed_one(fields, expected):
    assert sc.runtime_to_string(fields) == expected


def test_a_host_agents_ctx_output_is_byte_identical_to_the_native_render(run, capsys):
    """The runtime line is why host output must be checked, not assumed: it is
    additive only while `runtime != host`."""
    host_self = {k: v for k, v in SELF.items() if not k.startswith(("runtime", "sandbox"))}
    document = {
        "self": {**host_self, "runtime": "host"},
        "team": [{"task": "fix", "agents": [{**host_self, "runtime": "host"}, TEAMMATE]}],
        "notes": [NOTE],
    }
    run({("GET", "/v1/context"): document}, "ctx")
    assert not any(ln.startswith("runtime:") for ln in out(capsys).splitlines())


def test_ctx_refuses_to_inspect_another_pane(run, capsys):
    rc, service = run({("GET", "/v1/context"): CONTEXT}, "ctx", "--pane", "%9")
    assert rc == 2
    assert service.requests == []
    err = capsys.readouterr().err
    assert "--pane" in err and "host" in err


# --- notes -------------------------------------------------------------------


def _notes_route(record):
    return {"notes": [NOTE], "cursor": NOTE["id"]}


def test_notes_human_output_matches_the_native_columns(run, capsys):
    rc, _ = run({("GET", "/v1/notes"): _notes_route}, "notes")
    assert rc == 0
    assert out(capsys).rstrip("\n") == (
        f"{NOTE['id']:>3}  {'task':<9} {'decision':<9} {'claude':<12}  use sqlite, not jsonl"
    )


def test_notes_json_is_one_object_per_line(run, capsys):
    rc, _ = run({("GET", "/v1/notes"): _notes_route}, "notes", "--json")
    assert rc == 0
    assert [json.loads(ln) for ln in out(capsys).splitlines()] == [NOTE]


def test_notes_filters_travel_as_query_parameters(run):
    _, service = run(
        {("GET", "/v1/notes"): _notes_route},
        "notes",
        "--task",
        "review",
        "--scope",
        "workspace",
        "--kind",
        "blocker",
        "-n",
        "5",
    )
    request = service.only("GET", "/v1/notes")
    assert request.q("task") == "review"
    assert request.q("scope") == "workspace"
    assert request.q("kind") == "blocker"
    assert request.q("limit") == "5"


def test_notes_never_sends_a_cursor_so_it_keeps_the_native_newest_first_order(run):
    """The service returns newest-first without `after` and ascending-from-cursor
    with it. `notes` renders like native amux, so it must not pass one."""
    _, service = run({("GET", "/v1/notes"): _notes_route}, "notes")
    assert service.only("GET", "/v1/notes").q("after") is None


def test_ctx_treats_an_empty_last_commit_as_none(run, capsys):
    """The service blanks `last_commit` for sandbox rows, because computing it
    host-side would return the host repository's subject instead."""
    teammate = {**TEAMMATE, "last_commit": ""}
    document = {
        "self": SELF,
        "team": [{"task": "fix", "agents": [SELF, teammate]}],
        "notes": [],
    }
    rc, _ = run({("GET", "/v1/context"): document}, "ctx")
    assert rc == 0
    owl = next(ln for ln in out(capsys).splitlines() if "golden-owl" in ln)
    assert '""' not in owl and "  \n" not in owl


def test_notes_rejects_an_unknown_scope_before_calling_the_service(run, capsys):
    rc, service = run({("GET", "/v1/notes"): _notes_route}, "notes", "--scope", "global")
    assert rc == 2
    assert service.requests == []
    err = capsys.readouterr().err
    assert "global" in err and "task" in err  # names what was wrong and what is valid


@pytest.mark.parametrize("flag,value", [("--workspace", "other"), ("--repo", "/elsewhere"), ("--pane", "%9")])
def test_notes_refuses_flags_that_would_widen_or_reassign_scope(run, capsys, flag, value):
    rc, service = run({("GET", "/v1/notes"): _notes_route}, "notes", flag, value)
    assert rc == 2
    assert service.requests == []
    assert flag in capsys.readouterr().err


# --- note --------------------------------------------------------------------


def test_note_posts_text_scope_and_kind_and_prints_the_receipt(run, capsys):
    created = {**NOTE, "name": "brave-hawk"}
    rc, service = run(
        {("POST", "/v1/notes"): (201, {"note": created})},
        "note",
        "use",
        "sqlite,",
        "not",
        "jsonl",
        "--kind",
        "decision",
    )
    assert rc == 0
    assert service.body_of("POST", "/v1/notes") == {
        "text": "use sqlite, not jsonl",
        "scope": "task",
        "kind": "decision",
    }
    assert out(capsys).strip() == (
        "note #4 @ myproj/fix [brave-hawk] (scope=task, kind=decision)"
    )


def test_note_never_claims_an_identity_the_token_does_not_grant(run):
    created = {**NOTE, "name": "brave-hawk"}
    _, service = run({("POST", "/v1/notes"): (201, {"note": created})}, "note", "hi")
    body = service.body_of("POST", "/v1/notes")
    for field in ("pane", "agent", "workspace", "task", "repo", "worktree_id", "name"):
        assert field not in body


def test_note_rejects_empty_text_locally(run, capsys):
    rc, service = run({("POST", "/v1/notes"): (201, {"note": NOTE})}, "note", "   ")
    assert rc == 2
    assert service.requests == []


def test_note_refuses_to_attribute_itself_to_another_pane(run, capsys):
    rc, service = run(
        {("POST", "/v1/notes"): (201, {"note": NOTE})}, "note", "hi", "--pane", "%9"
    )
    assert rc == 2
    assert service.requests == []


# --- event emit --------------------------------------------------------------


def test_event_emit_posts_kind_and_detail(run):
    _, service = run(
        {("POST", "/v1/events"): {"event": {"kind": "busy"}, "cursor": 11}},
        "event",
        "emit",
        "busy",
        "--detail",
        "Bash",
    )
    assert service.body_of("POST", "/v1/events") == {"kind": "busy", "detail": "Bash"}


def test_event_emit_takes_its_detail_from_hook_json_on_stdin(run, monkeypatch):
    payload = json.dumps({"message": "needs your approval"})
    monkeypatch.setattr(sys, "stdin", _Stdin(payload))
    _, service = run(
        {("POST", "/v1/events"): {"event": {"kind": "notify"}, "cursor": 12}},
        "event",
        "emit",
        "notify",
    )
    body = service.body_of("POST", "/v1/events")
    assert body["detail"] == "needs your approval"


def test_event_emit_is_silent_and_succeeds_when_the_service_is_unreachable(
    config, capsys
):
    config("http://127.0.0.1:9")  # discard port: nothing listens
    assert sc.main(["event", "emit", "stop"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_event_emit_never_fails_a_hook_on_a_service_error(run, capsys):
    rc, _ = run(
        {("POST", "/v1/events"): (500, {"error": {"code": "boom", "message": "x"}})},
        "event",
        "emit",
        "busy",
    )
    assert rc == 0
    assert capsys.readouterr() == ("", "")


def test_event_emit_rejects_a_kind_that_is_not_a_state(run, capsys):
    rc, service = run({("POST", "/v1/events"): {}}, "event", "emit", "explode")
    assert rc == 2
    assert service.requests == []


def test_event_emit_ignores_host_hook_identity_flags(run):
    """A copied host hook may still pass --pane/--agent; identity comes from the
    token, and refusing would break the hook it is meant to serve."""
    _, service = run(
        {("POST", "/v1/events"): {"event": {"kind": "busy"}, "cursor": 13}},
        "event",
        "emit",
        "busy",
        "--pane",
        "%9",
        "--agent",
        "codex",
    )
    body = service.body_of("POST", "/v1/events")
    assert body == {"kind": "busy", "detail": ""}


# --- event state -------------------------------------------------------------


def test_event_state_human_output_matches_native_columns(run, capsys):
    rc, _ = run({("GET", "/v1/events/state"): PANE_STATES}, "event", "state")
    assert rc == 0
    assert out(capsys).splitlines() == [
        "%7\tbusy\tmyproj/fix\tbrave-hawk",
        "%9\tidle\tmyproj/fix\tgolden-owl",
    ]


def test_event_state_json_is_the_pane_list(run, capsys):
    rc, _ = run({("GET", "/v1/events/state"): PANE_STATES}, "event", "state", "--json")
    assert rc == 0
    assert json.loads(out(capsys)) == PANE_STATES["panes"]


def test_event_state_renders_a_missing_state_as_a_dash(run, capsys):
    payload = {"panes": [{**PANE_STATES["panes"][0], "state": None}]}
    run({("GET", "/v1/events/state"): payload}, "event", "state")
    assert out(capsys).splitlines()[0].split("\t")[1] == "-"


# --- event wait --------------------------------------------------------------


def test_event_wait_prints_the_state_it_reached(run, capsys):
    rc, service = run(
        {("GET", "/v1/events/wait"): {"pane": "%9", "state": "idle", "cursor": 20}},
        "event",
        "wait",
        "%9",
    )
    assert rc == 0
    assert out(capsys).strip() == "idle"
    assert service.only("GET", "/v1/events/wait").q("pane") == "%9"


def test_event_wait_exits_2_on_a_dead_pane(run, capsys):
    rc, _ = run(
        {("GET", "/v1/events/wait"): {"pane": "%9", "state": "dead", "cursor": 21}},
        "event",
        "wait",
        "%9",
    )
    assert rc == 2
    assert out(capsys).strip() == "dead"


def test_event_wait_times_out_with_the_native_message_and_exit_1(run, capsys):
    rc, _ = run(
        {("GET", "/v1/events/wait"): {"pane": "%9", "state": None, "cursor": 22}},
        "event",
        "wait",
        "%9",
        "--timeout",
        "0.2",
    )
    assert rc == 1
    assert out(capsys).strip() == "timeout after 0.2s"


def test_event_wait_repolls_from_its_cursor_until_the_state_arrives(run, capsys):
    """The service caps a single long poll, so the client must resume after its
    last acknowledged event id rather than restarting the wait."""
    seen: list[str | None] = []

    def route(record):
        seen.append(record.q("after"))
        if len(seen) < 3:
            return {"pane": "%9", "state": None, "cursor": 30 + len(seen)}
        return {"pane": "%9", "state": "needs-input", "cursor": 33}

    rc, _ = run(
        {("GET", "/v1/events/wait"): route}, "event", "wait", "%9", "--timeout", "30"
    )
    assert rc == 0
    assert out(capsys).strip() == "needs-input"
    assert seen == [None, "31", "32"]


def test_event_wait_bounds_each_poll_by_its_remaining_time(run):
    _, service = run(
        {("GET", "/v1/events/wait"): {"pane": "%9", "state": "idle", "cursor": 40}},
        "event",
        "wait",
        "%9",
        "--timeout",
        "5",
    )
    assert float(service.only("GET", "/v1/events/wait").q("timeout")) <= 5.0


# --- host-only commands ------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["spw", "myproj"],
        ["spg", "myproj", "task1"],
        ["kg", "myproj", "task1"],
        ["kw", "myproj"],
        ["integrate", "myproj", "task1"],
        ["monitor"],
        ["lsw"],
        ["lsg", "myproj"],
        ["event", "tail"],
    ],
)
def test_host_control_commands_fail_locally_without_touching_the_service(
    run, capsys, argv
):
    rc, service = run({}, *argv)
    assert rc == 2
    assert service.requests == []
    err = capsys.readouterr().err
    assert argv[0] in err
    assert "host" in err.lower()
    assert out(capsys) == ""


def test_the_boundary_message_names_the_supported_commands(run, capsys):
    run({}, "spw", "myproj")
    err = capsys.readouterr().err
    for supported in ("ctx", "notes", "note", "event"):
        assert supported in err


def test_host_only_commands_are_refused_even_without_a_config_file(monkeypatch, capsys):
    monkeypatch.setenv(sc.CONFIG_ENV, "/nonexistent/context.json")
    assert sc.main(["integrate", "myproj", "task1"]) == 2
    assert "host" in capsys.readouterr().err.lower()


def test_an_unknown_command_is_a_usage_error(run):
    with pytest.raises(SystemExit) as exit_info:
        run({}, "teleport")
    assert exit_info.value.code == 2


# --- configuration and failure modes ----------------------------------------


def test_config_is_read_from_the_env_pointed_file(tmp_path, monkeypatch):
    path = tmp_path / "elsewhere.json"
    path.write_text(json.dumps({"endpoint": "http://host.docker.internal:8765", "token": "t"}))
    path.chmod(0o600)
    monkeypatch.setenv(sc.CONFIG_ENV, str(path))
    config = sc.load_config()
    assert config.endpoint == "http://host.docker.internal:8765"
    assert config.token == "t"


def test_config_defaults_to_the_documented_sandbox_path(tmp_path, monkeypatch):
    monkeypatch.delenv(sc.CONFIG_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert sc.default_config_path() == tmp_path / "amux" / "context.json"


def test_a_missing_config_is_a_clear_failure_not_a_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(sc.CONFIG_ENV, str(tmp_path / "absent.json"))
    assert sc.main(["ctx"]) == 1
    err = capsys.readouterr().err
    assert "absent.json" in err
    assert "sandbox" in err.lower()


def test_a_config_readable_beyond_its_owner_is_refused(tmp_path, monkeypatch, capsys):
    secret = "zzz-capability-secret-zzz"
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"endpoint": "http://127.0.0.1:1", "token": secret}))
    path.chmod(0o644)
    monkeypatch.setenv(sc.CONFIG_ENV, str(path))
    assert sc.main(["ctx"]) == 1
    err = capsys.readouterr().err
    assert "0600" in err or "permission" in err.lower()
    assert secret not in err


def test_a_malformed_config_is_a_clear_failure(tmp_path, monkeypatch, capsys):
    path = tmp_path / "context.json"
    path.write_text("{not json")
    path.chmod(0o600)
    monkeypatch.setenv(sc.CONFIG_ENV, str(path))
    assert sc.main(["ctx"]) == 1
    assert "context.json" in capsys.readouterr().err


def test_an_unreachable_service_is_a_transient_failure_with_no_fallback(config, capsys):
    config("http://127.0.0.1:9")
    assert sc.main(["ctx"]) == 1
    err = capsys.readouterr().err
    assert "context service" in err.lower()
    for forbidden in ("context.db", "sqlite", "fallback"):
        assert forbidden not in err.lower()


def test_a_service_error_envelope_is_reported_verbatim(run, capsys):
    rc, _ = run(
        {
            ("GET", "/v1/context"): (
                403,
                {"error": {"code": "scope_denied", "message": "not your workspace"}},
            )
        },
        "ctx",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "scope_denied" in err
    assert "not your workspace" in err


def test_a_revoked_token_reports_authentication_not_a_crash(run, capsys):
    rc, _ = run({("GET", "/v1/context"): CONTEXT}, "ctx", token="stale-token")
    assert rc == 1
    assert "unauthenticated" in capsys.readouterr().err


def test_no_diagnostic_ever_prints_the_capability_token(run, capsys):
    rc, _ = run(
        {("GET", "/v1/context"): (500, {"error": {"code": "e", "message": TOKEN}})},
        "ctx",
    )
    assert rc == 1
    captured = capsys.readouterr()
    # the service echoing the token back is still not ours to print
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out


def test_the_token_never_reaches_the_url_or_the_process_table(run):
    _, service = run({("GET", "/v1/context"): CONTEXT}, "ctx")
    request = service.only("GET", "/v1/context")
    assert TOKEN not in request.path
    assert not request.query
    assert TOKEN in request.authorization
    assert not any(TOKEN in arg for arg in sys.argv)


def test_a_non_json_service_response_is_a_clear_failure(config, capsys, tmp_path):
    class Raw(FakeContextService):
        def respond(self, record):
            return 200, "not-a-document"

    with Raw({("GET", "/v1/context"): {}}) as service:
        config(service.endpoint)
        assert sc.main(["ctx"]) == 1
    assert "context service" in capsys.readouterr().err.lower()


# --- shim self-containment ---------------------------------------------------


def test_the_client_imports_only_the_standard_library():
    source = (sc.__file__ or "")
    assert source.endswith("sandbox_client.py")
    text = open(source).read()
    assert "import amux" not in text
    assert "from amux" not in text


def test_the_module_runs_as_a_copied_single_file_shim(tmp_path, config, monkeypatch):
    """The shim is installed as a lone executable `amux`, outside any package."""
    import shutil
    import subprocess

    shim = tmp_path / "bin" / "amux"
    shim.parent.mkdir()
    shutil.copy(sc.__file__ or "", shim)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    with FakeContextService({("GET", "/v1/context"): CONTEXT}) as service:
        cfg = config(service.endpoint)
        result = subprocess.run(
            [sys.executable, str(shim), "ctx", "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, sc.CONFIG_ENV: cfg, "PYTHONPATH": ""},
        )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == CONTEXT


class _Stdin:
    """Just enough of a piped stdin for the hook-payload path."""

    def __init__(self, text: str):
        self._text = text

    def isatty(self) -> bool:
        return False

    def read(self) -> str:
        return self._text
