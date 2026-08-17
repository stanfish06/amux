"""Runtime identity and degradation in amux's own output.

Two obligations pull against each other here and both are asserted:

- a sandbox-backed agent must show its runtime, lifecycle state and sandbox
  identity, and must never present a state it cannot actually report as
  authoritative;
- a host-backed agent's output must be byte-identical to before any of this
  existed.

The runtime line's shape is shared with the sandbox client's renderer, so the
parametrised cases below are the same ones happy-deer pinned on that side --
copied deliberately rather than paraphrased, because two renderers agreeing
today is worth nothing if nothing fails when one drifts.
"""

from __future__ import annotations

import pytest

from amux import core, sandbox_client, store, utils


def register(**overrides) -> int:
    row = {
        "pane": "%1",
        "workspace": "ws",
        "task": "t0",
        "agent": "claude",
        "name": "alpha",
        "path": "",
        "branch": "amux/ws/t0/alpha",
        "base_ref": "abc",
        "repo": "/repo",
        "runtime": "docker-sandbox",
        "runtime_status": "running",
        "sandbox_name": "amux-ws-t0-alpha-deadbeef",
        "sandbox_id": "sbx_1",
        "socket_name": "amux-root",
    }
    row.update(overrides)
    return store.register_worktree(**row)


def row_for(**overrides):
    register(**overrides)
    return store.worktrees_for("ws", "t0")[-1]


# --- the runtime line, shape shared with the sandbox client ---


@pytest.mark.parametrize(
    "fields,expected",
    [
        (
            {"runtime": "docker-sandbox", "runtime_status": "running",
             "sandbox_name": "box-a1b2"},
            "runtime: docker-sandbox running box-a1b2",
        ),
        (
            {"runtime": "docker-sandbox", "runtime_status": "running",
             "sandbox_name": ""},
            "runtime: docker-sandbox running",
        ),
        (
            {"runtime": "docker-sandbox", "runtime_status": "", "sandbox_name": ""},
            "runtime: docker-sandbox",
        ),
        (
            # an empty component takes its preceding space with it
            {"runtime": "docker-sandbox", "runtime_status": "",
             "sandbox_name": "box-a1b2"},
            "runtime: docker-sandbox box-a1b2",
        ),
        ({"runtime": "host", "runtime_status": "running", "sandbox_name": ""}, ""),
        ({}, ""),
    ],
)
def test_the_runtime_line_shape_is_exactly_the_agreed_one(fields, expected):
    assert sandbox_client.runtime_to_string(fields) == expected


def test_the_host_renderer_uses_that_same_function(monkeypatch):
    """Not a similar one. If these ever diverge, this fails rather than the two
    quietly drifting apart."""
    assert utils.context_to_string.__module__ == "amux.utils"
    calls = []
    monkeypatch.setattr(
        sandbox_client,
        "runtime_to_string",
        lambda me: calls.append(me) or "runtime: sentinel",
    )
    lines = utils.context_to_string(_ctx())
    assert calls, "the host renderer did not consult the shared function"
    assert lines[1] == "runtime: sentinel"


def _ctx(**self_overrides):
    me = {
        "name": "alpha", "agent": "claude", "label": "r0c0", "pane": "%1",
        "state": "idle", "cwd": "/w", "task": "t0", "workspace": "ws",
        "last_event": None,
    }
    me.update(self_overrides)
    return {"self": me, "team": [{"task": "t0", "agents": [me]}], "notes": []}


# --- placement ---


def test_the_runtime_line_follows_the_identity_line(git_repo):
    lines = utils.context_to_string(
        _ctx(runtime="docker-sandbox", runtime_status="running",
             sandbox_name="box-1", sandbox_id="sbx_1")
    )
    assert lines[0].startswith("you: ")
    assert lines[1] == "runtime: docker-sandbox running box-1"


def test_a_host_agent_gets_no_runtime_line():
    lines = utils.context_to_string(_ctx())
    assert not any(line.startswith("runtime:") for line in lines)
    assert lines[1].startswith("team @")


# --- degraded rendering ---


def test_a_degraded_state_is_marked_not_renamed():
    """No seventh state: the marker rides alongside the existing vocabulary."""
    marked = utils.state_to_string(
        {"state": "idle", "state_degraded": True, "missing_kinds": ["busy"]}
    )
    assert marked == "idle*"
    # The state itself is untouched, so anything branching on it still works.
    assert marked.rstrip("*") == "idle"


def test_an_undegraded_state_is_rendered_bare():
    assert utils.state_to_string({"state": "busy", "state_degraded": False}) == "busy"
    assert utils.state_to_string({"state": "busy"}) == "busy"


def test_the_degraded_marker_is_ascii():
    """Emoji are not reliably one cell wide and misalign the monitor's borders."""
    assert utils.DEGRADED_MARK.isascii()
    assert len(utils.DEGRADED_MARK) == 1


def test_a_degraded_agent_explains_itself():
    lines = utils.context_to_string(
        _ctx(
            runtime="docker-sandbox", runtime_status="running",
            sandbox_name="box-1", state_degraded=True,
            missing_kinds=["busy", "notify"],
        )
    )
    assert lines[0].endswith("/w")
    assert "idle*" in lines[0]
    note = next(line for line in lines if "cannot fully" in line)
    assert "busy, notify" in note
    assert note.isascii()


def test_a_degraded_teammate_is_marked_in_the_roster():
    me = {
        "name": "alpha", "agent": "claude", "label": "r0c0", "pane": "%1",
        "state": "idle", "cwd": "/w", "task": "t0", "workspace": "ws",
        "last_event": None,
    }
    mate = {
        **me, "name": "beta", "pane": "%2", "state": "idle",
        "state_degraded": True, "missing_kinds": ["busy"],
    }
    lines = utils.context_to_string(
        {"self": me, "team": [{"task": "t0", "agents": [me, mate]}], "notes": []}
    )
    beta = next(line for line in lines if "beta" in line)
    assert "idle*" in beta


# --- the JSON contract ---


def test_a_sandbox_row_contributes_its_runtime_identity():
    fields = core.runtime_fields(row_for())
    assert fields["runtime"] == "docker-sandbox"
    assert fields["runtime_status"] == "running"
    assert fields["sandbox_name"] == "amux-ws-t0-alpha-deadbeef"
    assert fields["sandbox_id"] == "sbx_1"
    assert fields["state_degraded"] is False
    assert fields["missing_kinds"] == []


def test_a_host_row_contributes_nothing(git_repo):
    """Byte-identical host output is the whole compatibility requirement."""
    assert core.runtime_fields(row_for(runtime="host", path="/w")) == {}


def test_lifecycle_states_are_carried_through():
    for status in ("created", "running", "stopped", "failed", "removed"):
        assert core.runtime_fields(row_for(runtime_status=status))[
            "runtime_status"
        ] == status


# --- degradation is derived, not stored twice ---


def test_missing_kinds_are_derived_from_the_recorded_mechanism():
    """One recorded fact, so three readers cannot disagree about the list."""
    row = dict(row_for(agent="codex"))
    row["hook_mechanism"] = "notify"
    fields = core.runtime_fields(row)

    assert fields["state_degraded"] is True
    assert fields["missing_kinds"]
    # Exactly what the hook adapter says, not a second opinion.
    from amux import sandbox_hooks

    assert tuple(fields["missing_kinds"]) == sandbox_hooks.missing_kinds(
        "codex", hooks_supported=False
    )


def test_a_full_hook_surface_is_not_degraded():
    row = dict(row_for(agent="codex"))
    row["hook_mechanism"] = "hooks"
    fields = core.runtime_fields(row)
    assert fields["state_degraded"] is False
    assert fields["missing_kinds"] == []


def test_an_unrecorded_mechanism_claims_nothing():
    """Absent information is not evidence of degradation, and claiming it would
    be as wrong as hiding it."""
    fields = core.runtime_fields(row_for())
    assert fields["state_degraded"] is False


# --- the hazard, made impossible rather than remembered ---


def test_a_commit_subject_is_never_read_from_an_empty_path(git_repo, monkeypatch):
    """`git -C ""` is a no-op that reports the calling process's checkout, so an
    empty path must never reach git at all -- otherwise a sandbox row inherits
    whatever commit amux happens to be sitting on."""
    from amux import worktree

    monkeypatch.chdir(git_repo)
    ran: list[tuple] = []
    real = worktree.subprocess.run

    def spy(cmd, *args, **kwargs):
        ran.append(tuple(cmd))
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(worktree.subprocess, "run", spy)

    assert worktree.latest_commit_subject("") == ""
    # Not "returned the wrong answer" -- it never asked.
    assert ran == []
    # The positive control: a real path still reports its commit.
    assert worktree.latest_commit_subject(str(git_repo)) == "initial commit"
