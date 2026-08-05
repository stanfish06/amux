"""Runtime identity in the monitor's view.

`events.pane_states` is what the monitor refreshes from, and what
`GET /v1/events/state` serves. It is a different producer from
`core.build_context`, so a sandboxed agent being distinguishable in `ctx` says
nothing about whether it is distinguishable here -- which is exactly how this
gap survived: every runtime assertion lived on the ctx path.

These tests therefore go through `pane_states` specifically, never through
`build_context`.
"""

from __future__ import annotations

import pytest

from amux import events, store


@pytest.fixture
def panes(monkeypatch):
    """A two-pane amux server, answered from a table rather than tmux."""
    listing: list[str] = []
    facts: dict[str, events.PaneFacts] = {}

    def add(pane: str, name: str, agent: str = "claude", state_option: str = "idle"):
        facts[pane] = events.PaneFacts(
            alive=True, kind="amux", created=1000.0, state_option=state_option,
            name=name, label=f"r0c{len(facts)}", command=agent, agent=agent,
            cwd="/w", workspace="ws", task="t0",
        )
        listing.append(pane)

    def install():
        monkeypatch.setattr(
            events, "_tmux_out", lambda socket, *a: "\n".join(listing)
        )
        monkeypatch.setattr(
            events, "_parse_pane", lambda line: facts[line.split(events._DELIM)[0]]
        )

    add.install = install  # type: ignore[attr-defined]
    add.facts = facts  # type: ignore[attr-defined]
    return add


def register(pane="%1", name="alpha", **overrides):
    row = {
        "pane": pane, "workspace": "ws", "task": "t0", "agent": "claude",
        "name": name, "path": "", "branch": f"amux/ws/t0/{name}",
        "base_ref": "abc", "repo": "/repo", "runtime": "docker-sandbox",
        "runtime_status": "running", "sandbox_name": f"amux-ws-t0-{name}-dead",
        "sandbox_id": "sbx_1", "socket_name": "amux-root",
    }
    row.update(overrides)
    return store.register_worktree(**row)


def states(panes):
    panes.install()
    return {entry["pane"]: entry for entry in events.pane_states("amux-root")}


# --- the gap this closes ---


def test_a_sandbox_agent_is_distinguishable_in_the_monitor(panes):
    """The requirement names the monitor explicitly. Before this, a sandboxed
    agent and a host agent produced identical monitor rows."""
    panes("%1", "alpha")
    register()

    entry = states(panes)["%1"]

    assert entry["runtime"] == "docker-sandbox"
    assert entry["runtime_status"] == "running"
    assert entry["sandbox_name"] == "amux-ws-t0-alpha-dead"
    assert entry["sandbox_id"] == "sbx_1"


def test_a_host_agent_monitor_row_is_unchanged(panes):
    """Host rows carry none of the runtime keys, so existing consumers see
    exactly what they saw before."""
    panes("%1", "alpha")
    register(runtime="host", path="/w")

    entry = states(panes)["%1"]

    for key in ("runtime", "runtime_status", "sandbox_name", "sandbox_id"):
        assert key not in entry
    # The pre-existing shape is intact.
    assert set(entry) == {
        "pane", "kind", "workspace", "task", "agent", "name", "label",
        "state", "last_event",
    }


def test_a_pane_with_no_execution_row_is_unchanged(panes):
    panes("%1", "alpha")

    entry = states(panes)["%1"]
    assert "runtime" not in entry
    assert entry["state"]


def test_a_mixed_grid_distinguishes_its_agents(panes):
    """The scenario the requirement describes: both kinds, side by side."""
    panes("%1", "alpha")
    panes("%2", "beta", agent="codex")
    register(pane="%1", name="alpha")
    register(pane="%2", name="beta", runtime="host", path="/w")

    resolved = states(panes)

    assert resolved["%1"]["runtime"] == "docker-sandbox"
    assert "runtime" not in resolved["%2"]


# --- the stopped state ---


def test_stopped_is_part_of_the_state_vocabulary():
    """One of the six the spec enumerates, and it was simply missing."""
    assert "stopped" in events.AgentState.__args__
    assert set(events.AgentState.__args__) == {
        "starting", "busy", "idle", "needs-input", "stopped", "dead",
    }


def test_a_stopped_sandbox_reads_as_stopped_not_idle(panes):
    """Its pane still holds the state its last event implied, which would show
    a VM that is not running as indistinguishable from one that is."""
    panes("%1", "alpha", state_option="idle")
    register(runtime_status="stopped")

    assert states(panes)["%1"]["state"] == "stopped"


def test_a_running_sandbox_keeps_its_event_derived_state(panes):
    panes("%1", "alpha", state_option="busy")
    register(runtime_status="running")

    assert states(panes)["%1"]["state"] == "busy"


def test_a_host_agent_is_never_reported_stopped(panes):
    """`stopped` describes a VM. A host agent has none, so its state comes from
    its events exactly as before."""
    panes("%1", "alpha", state_option="idle")
    register(runtime="host", path="/w", runtime_status="stopped")

    assert states(panes)["%1"]["state"] == "idle"


@pytest.mark.parametrize("option", ["starting", "busy", "idle", "needs-input"])
def test_stopping_overrides_every_live_state(panes, option):
    """Whatever the agent last reported, the VM is not running now."""
    panes("%1", "alpha", state_option=option)
    register(runtime_status="stopped")

    assert states(panes)["%1"]["state"] == "stopped"


# --- degradation reaches the monitor too ---


def test_a_degraded_sandbox_is_marked_in_the_monitor(panes):
    panes("%1", "alpha", agent="codex")
    register(agent="codex")
    row = dict(store.worktrees_for("ws", "t0")[-1])
    row["hook_mechanism"] = "notify"

    fields = events.runtime_identity(row)
    assert fields["state_degraded"] is True
    assert fields["missing_kinds"]


def test_the_monitor_and_ctx_agree_about_runtime_identity(panes):
    """Two producers, one answer. They drifted before precisely because
    nothing compared them."""
    from amux import core

    panes("%1", "alpha")
    register()
    row = store.worktrees_for("ws", "t0")[-1]

    assert events.runtime_identity(row) == core.runtime_fields(row)
