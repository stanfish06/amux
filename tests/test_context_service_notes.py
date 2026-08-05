"""Task 2.3: context and notes, answered by the native functions.

The requirement is equivalence, not similarity: a sandboxed caller must get
what an equivalently scoped host agent gets, because both go through
`core.build_context` and `store.visible_notes`. So the tests here mostly
compare the service's answer against the same call made directly.
"""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from amux import context_service as cs
from amux import core, events, store


# --- a notes_tmux server without notes_tmux ---


@dataclass
class FakePane:
    id: str
    facts: events.PaneFacts
    server: object = None


@dataclass
class FakeWindow:
    id: str
    name: str
    panes: list[FakePane] = field(default_factory=list)


@dataclass
class FakeSession:
    name: str
    windows: list[FakeWindow] = field(default_factory=list)


@dataclass
class FakeServer:
    """Enough libtmux surface for `core.build_context`. Pane facts come from
    the table below rather than from a notes_tmux subprocess."""

    socket_name: str = "amux-root"
    sessions: list[FakeSession] = field(default_factory=list)


@pytest.fixture
def notes_tmux(monkeypatch):
    """A one-workspace, one-task server holding two agent panes."""
    facts: dict[str, events.PaneFacts] = {}

    def make_pane(pane: str, name: str, agent: str, created: float) -> FakePane:
        facts[pane] = events.PaneFacts(
            alive=True,
            kind="amux",
            created=created,
            state_option="idle",
            name=name,
            label=f"{name}:{pane}",
            command=agent,
            agent=agent,
            cwd="/sandbox",
            workspace="proj",
            task="fix",
        )
        return FakePane(id=pane, facts=facts[pane])

    window = FakeWindow(
        id="@1",
        name="fix",
        panes=[
            make_pane("%1", "swift-crane", "claude", 1000.0),
            make_pane("%2", "happy-deer", "codex", 1000.0),
        ],
    )
    server = FakeServer(sessions=[FakeSession(name="proj", windows=[window])])
    for session in server.sessions:
        for w in session.windows:
            for p in w.panes:
                p.server = server

    monkeypatch.setattr(
        events, "pane_facts", lambda pane, socket=None: facts.get(pane, events.PaneFacts(alive=False))
    )
    return server


# --- service ---


@dataclass
class Probe:
    handle: cs.ServiceHandle
    db: Path
    server: FakeServer

    def get(self, path: str, token: str) -> tuple[int, dict]:
        return self._request("GET", path, token)

    def post(self, path: str, payload: dict, token: str) -> tuple[int, dict]:
        return self._request(
            "POST",
            path,
            token,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    def _request(self, method, path, token, body=None, headers=None):
        sent = dict(headers or {})
        sent["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection(cs.LOOPBACK, self.handle.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
        finally:
            conn.close()

    def agent(self, pane: str, name: str, agent: str = "claude", **overrides) -> str:
        permissions = overrides.pop("permissions", cs.AGENT_PERMISSIONS)
        row: dict = {
            "pane": pane,
            "workspace": "proj",
            "task": "fix",
            "agent": agent,
            "name": name,
            "path": "",
            "branch": f"amux/proj/fix/{name}",
            "base_ref": "main",
            "repo": "/repos/proj",
            "runtime": "docker-sandbox",
            "runtime_status": "running",
            "sandbox_name": f"amux-proj-fix-{name}",
            "sandbox_id": f"sbx-{name}",
            "socket_name": "amux-root",
        }
        row.update(overrides)
        worktree_id = store.register_worktree(db_path=self.db, **row)
        token, _ = store.mint_context_token(
            worktree_id, permissions=permissions, db_path=self.db
        )
        self.tokens[name] = token
        self.worktrees[name] = worktree_id
        return token

    tokens: dict[str, str] = field(default_factory=dict)
    worktrees: dict[str, int] = field(default_factory=dict)


@pytest.fixture
def notes_probe(tmp_path, db_path, notes_tmux):
    config = cs.ServiceConfig(
        port=0,
        db_path=db_path,
        state_dir=tmp_path / "state",
        shutdown_poll_s=0.01,  # a server per test case; see the note in _config
    )
    handle = cs.start_service(config, server_factory=lambda socket: notes_tmux)
    try:
        yield Probe(handle=handle, db=db_path, server=notes_tmux)
    finally:
        handle.stop()


@pytest.fixture
def caller(notes_probe):
    """The caller: %1, `swift-crane`."""
    return notes_probe.agent("%1", "swift-crane")


@pytest.fixture
def teammate(notes_probe):
    """A teammate in the same task: %2, `happy-deer`."""
    return notes_probe.agent("%2", "happy-deer", agent="codex")


# --- GET /v1/context ---


def _without_last_commit(entry: dict) -> dict:
    return {k: v for k, v in entry.items() if k != "last_commit"}


def test_context_matches_the_native_call(notes_probe, caller, teammate):
    """Same function, so same answer — bar `last_commit`, which the service
    blanks for a pathless row (see the next test)."""
    status, payload = notes_probe.get("/v1/context", caller)
    assert status == 200
    native = core.build_context(notes_probe.server, "%1")
    assert [
        {"task": t["task"], "agents": [_without_last_commit(a) for a in t["agents"]]}
        for t in payload["team"]
    ] == [
        {"task": t["task"], "agents": [_without_last_commit(a) for a in t["agents"]]}
        for t in native["team"]
    ]
    assert payload["notes"] == native["notes"]
    # `self` is the native entry plus the runtime fields, and nothing else.
    assert _without_last_commit(
        {k: v for k, v in payload["self"].items() if k in native["self"]}
    ) == _without_last_commit(
        {k: v for k, v in native["self"].items() if k in payload["self"]}
    )
    assert set(payload) == {"self", "team", "notes"}


def test_context_self_carries_the_runtime_identity(notes_probe, caller):
    _, payload = notes_probe.get("/v1/context", caller)
    assert payload["self"]["runtime"] == "docker-sandbox"
    assert payload["self"]["runtime_status"] == "running"
    assert payload["self"]["sandbox_name"] == "amux-proj-fix-swift-crane"
    assert payload["self"]["sandbox_id"] == "sbx-swift-crane"
    assert payload["self"]["workspace"] == "proj"
    assert payload["self"]["task"] == "fix"
    assert payload["self"]["name"] == "swift-crane"


def test_context_includes_the_whole_task_roster(notes_probe, caller, teammate):
    _, payload = notes_probe.get("/v1/context", caller)
    tasks = {t["task"] for t in payload["team"]}
    assert tasks == {"fix"}
    names = {a["name"] for t in payload["team"] for a in t["agents"]}
    assert names == {"swift-crane", "happy-deer"}


def test_context_reports_no_last_commit_for_a_pathless_row(notes_probe, caller):
    """A sandbox row has no host worktree, and `git -C ""` runs wherever the
    service happens to live — so an unguarded native call would report the
    *host* checkout's commit subject for a sandbox pane.

    The core-side gap this anticipated is now closed: `core._roster_entry` only
    asks for a commit subject when the row actually has a host path, so it
    omits the field entirely rather than filling it with someone else's commit.
    The service's own guard is kept as defence in depth — both layers are
    asserted here so neither can regress silently.
    """
    native = core.build_context(notes_probe.server, "%1")
    assert native["self"]["worktree"] == ""
    # Absent, not merely empty: core no longer runs host git for a pathless row.
    assert "last_commit" not in native["self"]

    _, payload = notes_probe.get("/v1/context", caller)
    entries = [payload["self"], *(a for t in payload["team"] for a in t["agents"])]
    registered = [e for e in entries if "worktree" in e]
    assert registered, "the caller at least must have a registry row"
    for entry in registered:
        assert entry["worktree"] == ""
        # Absent or blank both satisfy the contract "no commit is exposed":
        # core omits the field, and the service blanks it if it ever reappears.
        assert entry.get("last_commit", "") == ""
        # The absence above is only meaningful if the entry has real content,
        # so pin something positive alongside it.
        assert entry["name"] and entry["pane"]

    # The host checkout's HEAD subject must appear nowhere in a sandbox
    # agent's context, whichever layer would have introduced it.
    assert "initial commit" not in json.dumps(payload)


def test_context_says_so_when_the_host_pane_is_gone(notes_probe):
    token = notes_probe.agent("%99", "ghost-agent")
    status, payload = notes_probe.get("/v1/context", token)
    assert status == 503
    assert payload["error"]["code"] == "service_unavailable"
    assert "ghost-agent" in payload["error"]["message"]


def test_context_needs_the_read_capability(notes_probe):
    notes_probe.agent("%1", "swift-crane", permissions=())
    worktree_id = store.register_worktree(
        pane="%1",
        workspace="proj",
        task="fix",
        path="",
        branch="b",
        db_path=notes_probe.db,
    )
    token, _ = store.mint_context_token(
        worktree_id, permissions=(cs.PERM_NOTES_WRITE,), db_path=notes_probe.db
    )
    status, payload = notes_probe.get("/v1/context", token)
    assert status == 403
    assert cs.PERM_CONTEXT_READ in payload["error"]["message"]


# --- GET /v1/notes ---


def _post_note(notes_probe, token, text, scope="task", kind="note"):
    status, payload = notes_probe.post(
        "/v1/notes", {"text": text, "scope": scope, "kind": kind}, token
    )
    assert status == 200, payload
    return payload["note"]


def test_notes_match_the_native_visible_notes(notes_probe, caller, teammate):
    _post_note(notes_probe, caller, "mine, task scoped")
    _post_note(notes_probe, teammate, "theirs, task scoped")
    _post_note(notes_probe, caller, "mine, private", scope="agent")
    _post_note(notes_probe, teammate, "theirs, private", scope="agent")
    _post_note(notes_probe, teammate, "everyone", scope="workspace")

    _, payload = notes_probe.get("/v1/notes", caller)
    native = store.visible_notes(
        workspace="proj", task="fix", pane="%1", repo="/repos/proj", db_path=notes_probe.db
    )
    assert payload["notes"] == native
    texts = [n["text"] for n in payload["notes"]]
    assert "theirs, private" not in texts
    assert "mine, private" in texts
    assert "everyone" in texts


def test_an_agent_scoped_note_stays_private_on_the_scoped_route(notes_probe, caller, teammate):
    _post_note(notes_probe, teammate, "theirs, private", scope="agent")
    _post_note(notes_probe, caller, "mine, private", scope="agent")
    _, payload = notes_probe.get("/v1/notes?scope=agent", caller)
    assert [n["text"] for n in payload["notes"]] == ["mine, private"]


def test_notes_are_filtered_by_repository(notes_probe, caller):
    """Workspace and task are reusable notes_tmux labels; the repo is what makes a
    note belong to this checkout."""
    other = notes_probe.agent("%2", "other-repo-agent", repo="/repos/elsewhere")
    _post_note(notes_probe, other, "from another repository")
    _post_note(notes_probe, caller, "from this repository")
    _, payload = notes_probe.get("/v1/notes", caller)
    texts = [n["text"] for n in payload["notes"]]
    assert texts == ["from this repository"]


def test_notes_honours_kind_and_limit(notes_probe, caller):
    _post_note(notes_probe, caller, "a finding", kind="finding")
    for i in range(5):
        _post_note(notes_probe, caller, f"note {i}")
    _, payload = notes_probe.get("/v1/notes?kind=finding", caller)
    assert [n["text"] for n in payload["notes"]] == ["a finding"]
    _, payload = notes_probe.get("/v1/notes?limit=2", caller)
    assert len(payload["notes"]) == 2


def test_notes_defaults_to_the_configured_page_size(notes_probe, caller):
    for i in range(15):
        _post_note(notes_probe, caller, f"note {i}")
    _, payload = notes_probe.get("/v1/notes", caller)
    assert len(payload["notes"]) == notes_probe.handle.service.config.default_results


def test_a_sibling_task_is_readable_but_its_private_notes_are_not(notes_probe, caller):
    """Native `amux notes --task other` is allowed; widening the task must not
    widen what is private."""
    sibling = notes_probe.agent("%2", "sibling", task="other")
    _post_note(notes_probe, sibling, "sibling task note")
    _post_note(notes_probe, sibling, "sibling private", scope="agent")
    _, payload = notes_probe.get("/v1/notes?task=other", caller)
    texts = [n["text"] for n in payload["notes"]]
    assert "sibling task note" in texts
    assert "sibling private" not in texts


def test_a_foreign_workspace_is_refused(notes_probe, caller):
    status, payload = notes_probe.get("/v1/notes?workspace=elsewhere", caller)
    assert status == 403
    assert payload["error"]["code"] == "forbidden"
    assert "elsewhere" in payload["error"]["message"]
    assert "proj/fix" in payload["error"]["message"]
    # The caller's own workspace is fine.
    assert notes_probe.get("/v1/notes?workspace=proj", caller)[0] == 200


def test_a_foreign_repository_is_refused(notes_probe, caller):
    status, payload = notes_probe.get("/v1/notes?repo=/repos/elsewhere", caller)
    assert status == 403
    assert notes_probe.get("/v1/notes?repo=/repos/proj", caller)[0] == 200


# --- cursors ---


def test_the_cursor_is_the_highest_note_seen(notes_probe, caller):
    first = _post_note(notes_probe, caller, "one")
    second = _post_note(notes_probe, caller, "two")
    _, payload = notes_probe.get("/v1/notes", caller)
    assert payload["cursor"] == max(first["id"], second["id"])


def test_a_cursor_returns_only_later_notes_in_order(notes_probe, caller):
    first = _post_note(notes_probe, caller, "one")
    _, payload = notes_probe.get(f"/v1/notes?after={first['id']}", caller)
    assert payload["notes"] == []
    assert payload["cursor"] == first["id"]  # never rewinds

    second = _post_note(notes_probe, caller, "two")
    third = _post_note(notes_probe, caller, "three")
    _, payload = notes_probe.get(f"/v1/notes?after={first['id']}", caller)
    assert [n["id"] for n in payload["notes"]] == [second["id"], third["id"]]
    assert payload["cursor"] == third["id"]


def test_a_cursor_walk_sees_every_note_exactly_once(notes_probe, caller):
    """A burst larger than one page must be caught up, not skipped."""
    posted = [_post_note(notes_probe, caller, f"note {i}")["id"] for i in range(25)]
    seen: list[int] = []
    cursor = 0
    for _ in range(10):
        _, payload = notes_probe.get(f"/v1/notes?after={cursor}&limit=5", caller)
        ids = [n["id"] for n in payload["notes"]]
        if not ids:
            break
        seen.extend(ids)
        cursor = payload["cursor"]
    assert seen == posted
    assert len(set(seen)) == len(seen)


def test_the_cursor_is_null_when_there_is_nothing_at_all(notes_probe, caller):
    _, payload = notes_probe.get("/v1/notes", caller)
    assert payload["notes"] == []
    assert payload["cursor"] is None


# --- POST /v1/notes ---


def test_a_posted_note_is_attributed_to_the_caller(notes_probe, caller):
    note = _post_note(notes_probe, caller, "hello from the sandbox")
    assert note["workspace"] == "proj"
    assert note["task"] == "fix"
    assert note["pane"] == "%1"
    assert note["agent"] == "claude"
    assert note["worktree_id"] == notes_probe.worktrees["swift-crane"]
    assert note["repo"] == "/repos/proj"
    assert note["name"] == "swift-crane"
    assert note["scope"] == "task"
    assert note["kind"] == "note"


def test_the_returned_row_is_the_row_the_store_holds(notes_probe, caller):
    """The response is built from what was inserted, so this pins it against a
    read-back: a new notes column has to fail here rather than drift."""
    note = _post_note(notes_probe, caller, "read me back")
    stored = [
        n
        for n in store.query_notes(
            workspace="proj", task="fix", limit=10, db_path=notes_probe.db
        )
        if n["id"] == note["id"]
    ][0]
    assert set(note) == set(stored) | {"name"}
    assert {k: v for k, v in note.items() if k != "name"} == stored


def test_a_body_cannot_attribute_a_note_to_anyone_else(notes_probe, caller, teammate):
    status, payload = notes_probe.post(
        "/v1/notes",
        {
            "text": "not from me",
            "pane": "%2",
            "agent": "codex",
            "workspace": "elsewhere",
            "task": "other",
            "worktree_id": notes_probe.worktrees["happy-deer"],
            "name": "happy-deer",
        },
        caller,
    )
    assert status == 200
    note = payload["note"]
    assert note["pane"] == "%1"
    assert note["agent"] == "claude"
    assert note["workspace"] == "proj"
    assert note["worktree_id"] == notes_probe.worktrees["swift-crane"]
    assert note["name"] == "swift-crane"

    # And what was actually stored, which is the part that matters: a receipt
    # assembled from the identity would report the caller's own pane whatever
    # the insert did.
    stored = [
        n
        for n in store.query_notes(workspace="proj", limit=10, db_path=notes_probe.db)
        if n["id"] == note["id"]
    ]
    assert len(stored) == 1
    assert stored[0]["pane"] == "%1"
    assert stored[0]["agent"] == "claude"
    assert stored[0]["worktree_id"] == notes_probe.worktrees["swift-crane"]
    assert stored[0]["workspace"] == "proj"
    assert stored[0]["task"] == "fix"
    assert store.query_notes(pane="%2", limit=10, db_path=notes_probe.db) == []


def test_every_scope_and_kind_round_trips(notes_probe, caller):
    for scope in store.NOTE_SCOPES:
        for kind in store.NOTE_KINDS:
            note = _post_note(notes_probe, caller, f"{scope}/{kind}", scope=scope, kind=kind)
            assert (note["scope"], note["kind"]) == (scope, kind)


def test_a_posted_note_is_visible_to_a_teammate_and_to_the_native_path(
    notes_probe, caller, teammate
):
    note = _post_note(notes_probe, caller, "shared with the task")
    _, theirs = notes_probe.get("/v1/notes", teammate)
    assert note["id"] in [n["id"] for n in theirs["notes"]]
    native = store.visible_notes(
        workspace="proj", task="fix", pane="%2", repo="/repos/proj", db_path=notes_probe.db
    )
    assert note["id"] in [n["id"] for n in native]


def test_an_agent_scoped_note_is_invisible_to_a_teammate(notes_probe, caller, teammate):
    """The shared note is the control: without it, an empty list would satisfy
    the absence check for any reason at all, including a failed request."""
    private = _post_note(notes_probe, caller, "just for me", scope="agent")
    shared = _post_note(notes_probe, caller, "for the task")
    _, theirs = notes_probe.get("/v1/notes", teammate)
    visible = [n["id"] for n in theirs["notes"]]
    assert shared["id"] in visible
    assert private["id"] not in visible


def test_posting_needs_the_write_capability(notes_probe):
    worktree_id = store.register_worktree(
        pane="%1", workspace="proj", task="fix", path="", branch="b", db_path=notes_probe.db
    )
    token, _ = store.mint_context_token(
        worktree_id, permissions=(cs.PERM_CONTEXT_READ,), db_path=notes_probe.db
    )
    status, payload = notes_probe.post("/v1/notes", {"text": "nope"}, token)
    assert status == 403
    assert cs.PERM_NOTES_WRITE in payload["error"]["message"]


# --- bounds on notes ---


def test_an_oversized_note_is_refused_with_the_limit_and_the_length(notes_probe, caller):
    limit = notes_probe.handle.service.config.max_text_chars
    status, payload = notes_probe.post("/v1/notes", {"text": "x" * (limit + 7)}, caller)
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"
    assert str(limit) in payload["error"]["message"]
    assert str(limit + 7) in payload["error"]["message"]
    # Nothing was committed.
    assert store.query_notes(workspace="proj", limit=10, db_path=notes_probe.db) == []


def test_a_note_at_the_limit_is_accepted(notes_probe, caller):
    limit = notes_probe.handle.service.config.max_text_chars
    note = _post_note(notes_probe, caller, "x" * limit)
    assert len(note["text"]) == limit


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_an_empty_note_is_refused(notes_probe, caller, text):
    status, payload = notes_probe.post("/v1/notes", {"text": text}, caller)
    assert status == 400
    assert "text" in payload["error"]["message"]


def test_a_missing_text_field_is_refused(notes_probe, caller):
    status, payload = notes_probe.post("/v1/notes", {"scope": "task"}, caller)
    assert status == 400
    assert "text" in payload["error"]["message"]


@pytest.mark.parametrize("text", [42, None, ["a"], {"a": 1}])
def test_a_non_string_note_is_refused(notes_probe, caller, text):
    status, payload = notes_probe.post("/v1/notes", {"text": text}, caller)
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [("scope", "everything"), ("kind", "rumour"), ("scope", "'; DROP TABLE notes--")],
)
def test_an_unknown_scope_or_kind_is_refused(notes_probe, caller, field_name, value):
    status, payload = notes_probe.post(
        "/v1/notes", {"text": "hello", field_name: value}, caller
    )
    assert status == 400
    assert field_name in payload["error"]["message"]
    assert store.query_notes(workspace="proj", limit=10, db_path=notes_probe.db) == []


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=-1",
        "limit=99999",
        "limit=lots",
        "after=-5",
        "after=soon",
        "scope=everything",
        "kind=rumour",
        "limit=5&limit=6",
    ],
)
def test_out_of_bounds_query_parameters_are_refused(notes_probe, caller, query):
    status, payload = notes_probe.get(f"/v1/notes?{query}", caller)
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"


def test_the_result_limit_cannot_be_raised_past_the_configured_maximum(notes_probe, caller):
    maximum = notes_probe.handle.service.config.max_results
    assert notes_probe.get(f"/v1/notes?limit={maximum}", caller)[0] == 200
    status, payload = notes_probe.get(f"/v1/notes?limit={maximum + 1}", caller)
    assert status == 400
    assert str(maximum) in payload["error"]["message"]


def test_sql_shaped_input_is_stored_as_text_not_executed(notes_probe, caller):
    hostile = "'); DROP TABLE notes; --"
    note = _post_note(notes_probe, caller, hostile)
    assert note["text"] == hostile
    _, payload = notes_probe.get("/v1/notes", caller)
    assert [n["text"] for n in payload["notes"]] == [hostile]
    # The table is still there and still holds the row.
    assert store.query_notes(workspace="proj", limit=10, db_path=notes_probe.db)


# --- the routes are the documented ones ---


def test_the_read_and_write_routes_carry_the_agreed_permissions():
    assert cs._ROUTES[("GET", "/v1/context")].requires == cs.PERM_CONTEXT_READ
    assert cs._ROUTES[("GET", "/v1/notes")].requires == cs.PERM_CONTEXT_READ
    assert cs._ROUTES[("POST", "/v1/notes")].requires == cs.PERM_NOTES_WRITE
    for (method, path), route in cs._ROUTES.items():
        assert route.public == (path == "/healthz"), f"{method} {path}"
