"""Model and effort in amux's own output, on both read paths.

A pane's tuning has to survive two entirely different journeys. A host agent
reads it back out of tmux pane options; a sandboxed agent cannot reach tmux at
all, so its copy travels through the worktree row, the capability record and
the context service. Both are asserted here, along with the third obligation
that pulls against them: a pane given neither value reports neither, rather
than a default amux invented on its behalf.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from amux import context_service as cs
from amux import core, events, sandbox_client, store, utils


# --- the tuning line, shape shared with the sandbox client ---


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"model": "opus", "effort": "high"}, "model: opus  effort: high"),
        ({"model": "opus"}, "model: opus"),
        ({"effort": "high"}, "effort: high"),
        ({"model": "", "effort": ""}, ""),
        ({}, ""),
    ],
)
def test_the_tuning_line_shape_is_exactly_the_agreed_one(fields, expected):
    assert sandbox_client.tuning_to_string(fields) == expected


def test_the_host_renderer_uses_that_same_function(monkeypatch):
    """Not a similar one. Two renderers agreeing today is worth nothing if
    nothing fails when one drifts."""
    calls = []
    monkeypatch.setattr(
        sandbox_client,
        "tuning_to_string",
        lambda me: calls.append(me) or "model: sentinel",
    )
    lines = utils.context_to_string(_ctx(model="opus"))
    assert calls, "the host renderer did not consult the shared function"
    assert "model: sentinel" in lines


def _ctx(**self_overrides):
    me = {
        "name": "alpha",
        "agent": "claude",
        "label": "r0c0",
        "pane": "%1",
        "state": "idle",
        "cwd": "/w",
        "task": "t0",
        "workspace": "ws",
        "last_event": None,
    }
    me.update(self_overrides)
    return {"self": me, "team": [{"task": "t0", "agents": [me]}], "notes": []}


def test_the_tuning_line_follows_the_identity_line():
    lines = utils.context_to_string(_ctx(model="opus", effort="high"))
    assert lines[0].startswith("you: ")
    assert lines[1] == "model: opus  effort: high"


def test_it_sits_below_the_runtime_line_when_both_are_present():
    lines = utils.context_to_string(
        _ctx(
            model="opus",
            effort="high",
            runtime="docker-sandbox",
            runtime_status="running",
            sandbox_name="box-1",
        )
    )
    assert lines[1] == "runtime: docker-sandbox running box-1"
    assert lines[2] == "model: opus  effort: high"


def test_an_untuned_agent_gets_no_tuning_line():
    """The whole no-invented-defaults rule, at the place a user would see it."""
    lines = utils.context_to_string(_ctx())
    assert not any(line.startswith(("model:", "effort:")) for line in lines)
    assert lines[1].startswith("team @")


def test_both_renderers_agree_line_for_line():
    ctx = _ctx(model="opus", effort="high")
    assert utils.context_to_string(ctx)[1] == sandbox_client.context_to_string(ctx)[1]


# --- host: tmux pane options ---


def test_the_pane_format_carries_model_and_effort_before_the_sentinel():
    """`_parse_pane` rejects a row whose last field is not the sentinel, so the
    new fields cannot simply be appended to the end of the format."""
    assert events._PANE_FIELDS[-1] == events._SENTINEL  # noqa: SLF001
    assert "#{@amux_model}" in events._PANE_FIELDS  # noqa: SLF001
    assert "#{@amux_effort}" in events._PANE_FIELDS  # noqa: SLF001


def _pane_line(**over) -> str:
    values = {
        "pane_id": "%1",
        "amux_pane": "1",
        "session_created": "1000",
        "state": "idle",
        "name": "alpha",
        "label": "r0c0",
        "command": "claude",
        "agent": "claude",
        "cwd": "/w",
        "session_name": "ws",
        "window_name": "t0",
        "model": "",
        "effort": "",
        "role": "",
    }
    values.update(over)
    return events._DELIM.join([*values.values(), events._SENTINEL])  # noqa: SLF001


def test_a_pane_row_parses_its_tuning():
    facts = events._parse_pane(_pane_line(model="opus", effort="high"))  # noqa: SLF001
    assert (facts.model, facts.effort) == ("opus", "high")
    # everything that was already parsed still lands in the same place
    assert (facts.agent, facts.cwd, facts.workspace, facts.task) == (
        "claude",
        "/w",
        "ws",
        "t0",
    )


def test_a_pane_row_parses_its_role():
    assert events._parse_pane(_pane_line(role="worker")).role == "worker"  # noqa: SLF001


def test_an_untuned_pane_row_parses_to_empty_strings():
    facts = events._parse_pane(_pane_line())  # noqa: SLF001
    assert (facts.model, facts.effort) == ("", "")


def test_a_value_containing_the_delimiter_degrades_instead_of_misassigning():
    """Tuning values are unvalidated free text. An over-split row must lose the
    free-text block wholesale rather than shift every field by one."""
    facts = events._parse_pane(_pane_line(model="a\x1fb"))  # noqa: SLF001
    assert facts.alive and facts.name == "alpha"
    assert (facts.agent, facts.cwd, facts.model, facts.effort) == ("", "", "", "")


# --- host: the roster entry both `self` and teammates are built from ---


@dataclass
class FakePane:
    id: str
    server: object = None


@dataclass
class FakeServer:
    socket_name: str = "amux-root"
    sessions: list = field(default_factory=list)


def _entry(monkeypatch, **facts_over) -> dict:
    facts = events.PaneFacts(
        alive=True,
        kind="amux",
        created=1000.0,
        state_option="idle",
        name="alpha",
        label="r0c0",
        command="claude",
        agent="claude",
        cwd="/w",
        workspace="ws",
        task="t0",
        **facts_over,
    )
    monkeypatch.setattr(events, "pane_facts", lambda pane, socket=None: facts)
    monkeypatch.setattr(events, "pane_status", lambda pane, facts=None: ("idle", None))
    return core._roster_entry(FakePane(id="%1", server=FakeServer()))  # noqa: SLF001


def test_a_roster_entry_carries_the_panes_tuning(monkeypatch):
    """`_roster_entry` builds `self` AND every teammate row, which is how the
    requirement's "and its teammates" half is satisfied without a second path."""
    entry = _entry(monkeypatch, model="opus", effort="high")
    assert entry["model"] == "opus"
    assert entry["effort"] == "high"


def test_an_untuned_roster_entry_omits_the_keys_entirely(monkeypatch):
    entry = _entry(monkeypatch)
    assert "model" not in entry
    assert "effort" not in entry


# --- sandbox: the row, the capability record, the service payload ---


def _register(**over) -> int:
    row = {
        "pane": "%1",
        "workspace": "ws",
        "task": "t0",
        "agent": "claude",
        "name": "alpha",
        "path": "",
        "branch": "amux/ws/t0/alpha",
        "repo": "/repo",
        "runtime": "docker-sandbox",
        "runtime_status": "running",
        "sandbox_name": "amux-box",
        "sandbox_id": "sbx_1",
    }
    row.update(over)
    return store.register_worktree(**row)


def test_the_worktree_row_records_the_tuning():
    _register(model="opus", effort="high")
    row = store.worktrees_for("ws", "t0")[-1]
    assert (row["model"], row["effort"]) == ("opus", "high")


def test_the_capability_record_carries_it_to_the_service():
    """The token query names its columns one by one, so a new column reaches a
    sandboxed agent only if it is added there too."""
    wt = _register(model="opus", effort="high")
    token, _ = store.mint_context_token(wt, permissions=("context:read",))
    record = store.context_token_record(token)
    assert record is not None
    identity = cs.identity_from_record(record)
    assert (identity.model, identity.effort) == ("opus", "high")


def _service_self(monkeypatch, identity: cs.Identity) -> dict:
    monkeypatch.setattr(
        cs.core,
        "build_context",
        lambda server, pane: {
            "self": {"name": "alpha", "worktree": "", "last_commit": ""},
            "team": [{"task": "t0", "agents": []}],
            "notes": [],
        },
    )
    service = cs.ContextService(server_factory=lambda socket: object())
    return service.build_context(identity)["self"]


def test_a_sandboxed_agent_is_served_its_tuning_from_the_row(monkeypatch):
    """It cannot read tmux pane options, so this is its only source."""
    payload = _service_self(
        monkeypatch, cs.Identity(worktree_id=1, model="opus", effort="high")
    )
    assert payload["model"] == "opus"
    assert payload["effort"] == "high"


def test_an_untuned_sandboxed_agent_is_served_neither(monkeypatch):
    payload = _service_self(monkeypatch, cs.Identity(worktree_id=1))
    assert "model" not in payload
    assert "effort" not in payload
