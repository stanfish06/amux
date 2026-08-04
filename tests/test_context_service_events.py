"""Task 2.4: events, pane state, and the bounded wait.

The two things that are easy to get wrong and are pinned hardest here: an
event must be attributed to the capability's execution row rather than to
whatever worktree the pane fronts now — events_tmux recycles `%N` — and the pane
option must be set on the socket the row names, not on the service's default.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from amux import context_service as cs
from amux import events, store


# --- a events_tmux server that only records ---


@dataclass
class FakeTmux:
    """Stands in for `events._tmux` / `_tmux_out`, so pane options and wait-for
    signals can be inspected without a events_tmux server."""

    calls: list[tuple[str, ...]] = field(default_factory=list)
    alive: dict[str, bool] = field(default_factory=dict)
    facts: dict[str, events.PaneFacts] = field(default_factory=dict)
    listing: list[str] = field(default_factory=list)

    def events_tmux(self, socket: str, *args: str) -> None:
        self.calls.append((socket, *args))

    def out(self, socket: str, *args: str) -> str | None:
        if args and args[0] == "list-panes":
            return "\n".join(self.listing)
        return None

    def pane_facts(self, pane: str, socket: str | None = None) -> events.PaneFacts:
        self.calls.append(("facts", socket or "", pane))
        if pane in self.facts:
            return self.facts[pane]
        if self.alive.get(pane, False):
            return events.PaneFacts(alive=True, kind="amux", created=1000.0)
        return events.PaneFacts(alive=False)

    def options(self) -> list[tuple[str, str, str]]:
        """(socket, pane, state) for each set-option call."""
        return [
            (c[0], c[c.index("-t") + 1], c[-1])
            for c in self.calls
            if len(c) > 2 and c[1] == "set-option"
        ]

    def signals(self) -> list[tuple[str, str]]:
        """(socket, channel) for each wait-for -S call."""
        return [(c[0], c[-1]) for c in self.calls if len(c) > 2 and c[1] == "wait-for"]


@pytest.fixture
def events_tmux(monkeypatch):
    fake = FakeTmux(alive={"%1": True, "%2": True})
    monkeypatch.setattr(events, "_tmux", fake.events_tmux)
    monkeypatch.setattr(events, "_tmux_out", fake.out)
    monkeypatch.setattr(events, "pane_facts", fake.pane_facts)
    return fake


# --- service ---


@dataclass
class Probe:
    handle: cs.ServiceHandle
    db: Path
    events_tmux: FakeTmux
    tokens: dict[str, str] = field(default_factory=dict)
    worktrees: dict[str, int] = field(default_factory=dict)

    def get(self, path: str, token: str, timeout: float = 10) -> tuple[int, dict]:
        return self._request("GET", path, token, timeout=timeout)

    def post(self, path: str, payload: dict, token: str) -> tuple[int, dict]:
        return self._request(
            "POST",
            path,
            token,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

    def _request(self, method, path, token, body=None, headers=None, timeout=10):
        sent = dict(headers or {})
        sent["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection(cs.LOOPBACK, self.handle.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
        finally:
            conn.close()

    def agent(self, pane: str, name: str, **overrides) -> str:
        permissions = overrides.pop("permissions", cs.AGENT_PERMISSIONS)
        row: dict = {
            "pane": pane,
            "workspace": "proj",
            "task": "fix",
            "agent": "claude",
            "name": name,
            "path": "",
            "branch": f"amux/proj/fix/{name}",
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


@pytest.fixture
def events_probe(tmp_path, db_path, events_tmux):
    config = cs.ServiceConfig(
        port=0,
        db_path=db_path,
        state_dir=tmp_path / "state",
        max_wait_s=2.0,
        poll_interval_s=0.05,
        shutdown_poll_s=0.01,  # a server per test case; see the note in _config
    )
    handle = cs.start_service(config)
    try:
        yield Probe(handle=handle, db=db_path, events_tmux=events_tmux)
    finally:
        handle.stop()


@pytest.fixture
def crane(events_probe):
    return events_probe.agent("%1", "swift-crane")


# --- POST /v1/events ---


def test_an_event_is_recorded_and_attributed_to_the_caller(events_probe, crane):
    status, payload = events_probe.post("/v1/events", {"kind": "busy", "detail": "Edit"}, crane)
    assert status == 200
    event = payload["event"]
    assert event["kind"] == "busy"
    assert event["state"] == "busy"
    assert event["detail"] == "Edit"
    assert event["pane"] == "%1"
    assert event["agent"] == "claude"
    assert event["workspace"] == "proj"
    assert event["task"] == "fix"
    assert event["repo"] == "/repos/proj"
    assert event["worktree_id"] == events_probe.worktrees["swift-crane"]
    assert payload["cursor"] == event["id"]

    stored = store.iter_events(pane="%1", db_path=events_probe.db)
    assert len(stored) == 1
    assert {k: stored[0][k] for k in stored[0]} == {
        k: v for k, v in event.items() if k != "state"
    }


def test_the_pane_option_is_set_on_the_socket_the_row_names(events_probe):
    token = events_probe.agent("%1", "swift-crane", socket_name="amux-other")
    events_probe.post("/v1/events", {"kind": "notify", "detail": "may I?"}, token)
    assert events_probe.events_tmux.options() == [("amux-other", "%1", "needs-input")]
    assert events_probe.events_tmux.signals() == [("amux-other", "amux-state-1")]


@pytest.mark.parametrize(
    ("kind", "state"),
    [
        ("spawn", "starting"),
        ("busy", "busy"),
        ("stop", "idle"),
        ("notify", "needs-input"),
        ("exit", "dead"),
    ],
)
def test_every_event_kind_maps_to_the_native_state(events_probe, crane, kind, state):
    _, payload = events_probe.post("/v1/events", {"kind": kind}, crane)
    assert payload["event"]["state"] == state
    assert events_probe.events_tmux.options()[-1] == ("amux-root", "%1", state)
    assert events.STATE_BY_KIND[kind] == state


def test_a_needs_input_event_resolves_the_pane_to_needs_input(events_probe, crane):
    """The scenario from the spec: a hook posts a notification, and the host
    pane resolves to `needs-input`."""
    events_probe.post("/v1/events", {"kind": "notify", "detail": "which branch?"}, crane)
    assert events_probe.events_tmux.options()[-1][2] == "needs-input"
    state = events.resolve_state(
        alive=True,
        option="needs-input",
        latest=events.Event.from_row(store.latest_event("%1", db_path=events_probe.db)),
    )
    assert state == "needs-input"


def test_an_event_stays_attributed_after_the_pane_id_is_recycled(events_probe):
    """events_tmux hands `%N` back out after a restart. `events.emit` asks which
    worktree a pane fronts *now*, which is right for a hook inside that pane and
    wrong for a capability: the token names one execution, for good."""
    old_token = events_probe.agent("%1", "old-agent")
    old_id = events_probe.worktrees["old-agent"]
    # A newer registration takes over the same pane id.
    new_id = store.register_worktree(
        pane="%1",
        workspace="proj",
        task="fix",
        path="",
        branch="amux/proj/fix/new-agent",
        name="new-agent",
        repo="/repos/proj",
        db_path=events_probe.db,
    )
    assert new_id != old_id
    assert store.worktree_for_pane("%1", db_path=events_probe.db)["id"] == new_id

    _, payload = events_probe.post("/v1/events", {"kind": "busy"}, old_token)
    assert payload["event"]["worktree_id"] == old_id
    assert [e["worktree_id"] for e in store.iter_events(pane="%1", db_path=events_probe.db)] == [
        old_id
    ]


def test_an_event_for_a_vanished_pane_is_still_recorded(events_probe):
    """A killed task with a live sandbox: the event is durable, and the response
    says plainly that nothing was signalled."""
    token = events_probe.agent("%99", "ghost")
    _, payload = events_probe.post("/v1/events", {"kind": "stop"}, token)
    assert payload["pane_updated"] is False
    assert events_probe.events_tmux.options() == []
    assert events_probe.events_tmux.signals() == []
    assert len(store.iter_events(pane="%99", db_path=events_probe.db)) == 1


def test_a_body_cannot_attribute_an_event_to_another_pane(events_probe, crane):
    other = events_probe.agent("%2", "happy-deer")
    _, payload = events_probe.post(
        "/v1/events",
        {
            "kind": "exit",
            "pane": "%2",
            "agent": "codex",
            "workspace": "elsewhere",
            "worktree_id": events_probe.worktrees["happy-deer"],
        },
        crane,
    )
    assert payload["event"]["pane"] == "%1"
    assert payload["event"]["worktree_id"] == events_probe.worktrees["swift-crane"]
    assert events_probe.events_tmux.options()[-1][1] == "%1"

    # The stored row, not just the receipt.
    stored = store.iter_events(db_path=events_probe.db)
    assert [(e["pane"], e["worktree_id"], e["agent"]) for e in stored] == [
        ("%1", events_probe.worktrees["swift-crane"], "claude")
    ]


@pytest.mark.parametrize("kind", ["", "restart", "BUSY", "busy ", None, 7])
def test_an_unknown_event_kind_is_refused(events_probe, crane, kind):
    status, payload = events_probe.post("/v1/events", {"kind": kind}, crane)
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"
    assert store.iter_events(db_path=events_probe.db) == []
    assert events_probe.events_tmux.options() == []


def test_an_oversized_detail_is_refused_with_both_numbers(events_probe, crane):
    limit = events_probe.handle.service.config.max_detail_chars
    status, payload = events_probe.post(
        "/v1/events", {"kind": "busy", "detail": "x" * (limit + 3)}, crane
    )
    assert status == 400
    assert str(limit) in payload["error"]["message"]
    assert str(limit + 3) in payload["error"]["message"]
    assert store.iter_events(db_path=events_probe.db) == []


def test_an_empty_detail_is_fine(events_probe, crane):
    _, payload = events_probe.post("/v1/events", {"kind": "stop"}, crane)
    assert payload["event"]["detail"] == ""


def test_posting_an_event_needs_the_write_capability(events_probe):
    token = events_probe.agent("%1", "reader", permissions=(cs.PERM_CONTEXT_READ,))
    status, payload = events_probe.post("/v1/events", {"kind": "busy"}, token)
    assert status == 403
    assert cs.PERM_EVENTS_WRITE in payload["error"]["message"]
    assert store.iter_events(db_path=events_probe.db) == []


# --- GET /v1/events/state ---


def _pane_line(pane: str, name: str, workspace: str, task: str, state: str) -> str:
    """One row of `events._PANE_FORMAT`."""
    return "\x1f".join(
        [
            pane,
            "1",
            "1000.0",
            state,
            name,
            f"{name}:{pane}",
            "claude",
            "claude",
            "/sandbox",
            workspace,
            task,
            "amux",
        ]
    )


def test_state_reports_the_panes_in_the_callers_workspace(events_probe, crane):
    events_probe.events_tmux.listing = [
        _pane_line("%1", "swift-crane", "proj", "fix", "busy"),
        _pane_line("%2", "happy-deer", "proj", "fix", "idle"),
    ]
    status, payload = events_probe.get("/v1/events/state", crane)
    assert status == 200
    assert [p["pane"] for p in payload["panes"]] == ["%1", "%2"]
    assert [p["state"] for p in payload["panes"]] == ["busy", "idle"]
    # Shaped exactly like the native call.
    assert set(payload["panes"][0]) == {
        "pane",
        "kind",
        "workspace",
        "task",
        "agent",
        "name",
        "label",
        "state",
        "last_event",
    }
    assert payload["panes"] == [
        p for p in events.pane_states("amux-root") if p["workspace"] == "proj"
    ]


def test_state_hides_another_workspace(events_probe, crane):
    events_probe.events_tmux.listing = [
        _pane_line("%1", "swift-crane", "proj", "fix", "busy"),
        _pane_line("%7", "someone-else", "other-project", "task0", "idle"),
    ]
    _, payload = events_probe.get("/v1/events/state", crane)
    assert [p["pane"] for p in payload["panes"]] == ["%1"]
    assert "other-project" not in json.dumps(payload)


def test_state_needs_the_read_capability(events_probe):
    token = events_probe.agent("%1", "writer", permissions=(cs.PERM_EVENTS_WRITE,))
    status, payload = events_probe.get("/v1/events/state", token)
    assert status == 403
    assert cs.PERM_CONTEXT_READ in payload["error"]["message"]


# --- GET /v1/events/wait ---


def test_wait_returns_at_once_when_the_pane_is_already_there(events_probe, crane):
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="idle"
    )
    started = time.monotonic()
    status, payload = events_probe.get("/v1/events/wait?pane=%251&timeout=5", crane)
    assert status == 200
    assert payload["state"] == "idle"
    assert payload["pane"] == "%1"
    assert time.monotonic() - started < 1.0


def test_wait_is_released_by_a_sandbox_event(events_probe, crane):
    """The spec's scenario: a hook posts a notification and a waiter is let go."""
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="busy"
    )
    answer: dict = {}

    def waiter() -> None:
        answer["result"] = events_probe.get(
            "/v1/events/wait?pane=%251&timeout=5&states=needs-input", crane, timeout=20
        )

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.3)
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="needs-input"
    )
    events_probe.post("/v1/events", {"kind": "notify", "detail": "which branch?"}, crane)
    thread.join(20)
    assert not thread.is_alive()
    status, payload = answer["result"]
    assert status == 200
    assert payload["state"] == "needs-input"
    assert [e["kind"] for e in payload["events"]] == ["notify"]
    assert payload["cursor"] == payload["events"][-1]["id"]


def test_wait_sees_a_native_write_too(events_probe, crane):
    """Nothing signals this service when a host hook writes, so the poll has to
    notice on its own."""
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="busy"
    )
    answer: dict = {}

    def waiter() -> None:
        answer["result"] = events_probe.get(
            "/v1/events/wait?pane=%251&timeout=5&states=idle", crane, timeout=20
        )

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.3)
    # A native writer: straight into the store and the pane option, with no
    # request to the service at all.
    store.add_event(
        ts=time.time(),
        pane="%1",
        kind="stop",
        workspace="proj",
        task="fix",
        worktree_id=events_probe.worktrees["swift-crane"],
        repo="/repos/proj",
        db_path=events_probe.db,
    )
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="idle"
    )
    thread.join(20)
    status, payload = answer["result"]
    assert status == 200
    assert payload["state"] == "idle"


def test_wait_expires_with_a_usable_cursor(events_probe, crane):
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="busy"
    )
    _, first = events_probe.post("/v1/events", {"kind": "busy"}, crane)
    started = time.monotonic()
    status, payload = events_probe.get(
        "/v1/events/wait?pane=%251&timeout=0.4&states=idle", crane
    )
    elapsed = time.monotonic() - started
    assert status == 200
    assert payload["state"] is None
    assert payload["cursor"] == first["cursor"]
    assert 0.3 < elapsed < 3.0


def test_wait_never_exceeds_the_configured_cap(events_probe, crane):
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="busy"
    )
    started = time.monotonic()
    status, payload = events_probe.get(
        "/v1/events/wait?pane=%251&timeout=3600&states=idle", crane, timeout=30
    )
    elapsed = time.monotonic() - started
    assert status == 200
    assert payload["state"] is None
    assert elapsed < events_probe.handle.service.config.max_wait_s + 2


def test_wait_resumes_from_a_cursor_without_repeating(events_probe, crane):
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="idle"
    )
    _, first = events_probe.post("/v1/events", {"kind": "busy"}, crane)
    _, second = events_probe.post("/v1/events", {"kind": "stop"}, crane)

    _, payload = events_probe.get("/v1/events/wait?pane=%251&timeout=1", crane)
    assert [e["id"] for e in payload["events"]] == [
        first["event"]["id"],
        second["event"]["id"],
    ]

    _, resumed = events_probe.get(
        f"/v1/events/wait?pane=%251&timeout=1&after={first['cursor']}", crane
    )
    assert [e["id"] for e in resumed["events"]] == [second["event"]["id"]]
    assert resumed["cursor"] == second["cursor"]

    _, caught_up = events_probe.get(
        f"/v1/events/wait?pane=%251&timeout=1&after={second['cursor']}", crane
    )
    assert caught_up["events"] == []
    assert caught_up["cursor"] == second["cursor"]  # never rewinds


def test_wait_events_are_in_identifier_order(events_probe, crane):
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="idle"
    )
    for kind in ("spawn", "busy", "stop", "busy", "stop"):
        events_probe.post("/v1/events", {"kind": kind}, crane)
    _, payload = events_probe.get("/v1/events/wait?pane=%251&timeout=1&after=0", crane)
    ids = [e["id"] for e in payload["events"]]
    assert ids == sorted(ids)


def test_wait_defaults_to_the_callers_own_pane(events_probe, crane):
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="idle"
    )
    _, payload = events_probe.get("/v1/events/wait?timeout=1", crane)
    assert payload["pane"] == "%1"
    assert payload["state"] == "idle"


def test_wait_allows_a_teammate_in_the_same_scope(events_probe, crane):
    events_probe.agent("%2", "happy-deer")
    events_probe.events_tmux.facts["%2"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="idle"
    )
    _, payload = events_probe.get("/v1/events/wait?pane=%252&timeout=1", crane)
    assert payload["pane"] == "%2"
    assert payload["state"] == "idle"


def test_wait_refuses_a_pane_outside_the_scope(events_probe, crane):
    """The message names the scope, because the agent sees only this text."""
    store.register_worktree(
        pane="%9",
        workspace="other-project",
        task="task0",
        path="",
        branch="b",
        repo="/repos/elsewhere",
        db_path=events_probe.db,
    )
    for pane in ("%9", "%404"):
        status, payload = events_probe.get(f"/v1/events/wait?pane=%25{pane[1:]}&timeout=1", crane)
        assert status == 403, pane
        assert payload["error"]["code"] == "forbidden"
        assert payload["error"]["message"] == f"pane {pane} is not in proj/fix"


def test_wait_refuses_a_pane_in_another_repository(events_probe, crane):
    store.register_worktree(
        pane="%5",
        workspace="proj",
        task="fix",
        path="",
        branch="b",
        repo="/repos/elsewhere",
        db_path=events_probe.db,
    )
    status, payload = events_probe.get("/v1/events/wait?pane=%255&timeout=1", crane)
    assert status == 403
    assert "%5" in payload["error"]["message"]


@pytest.mark.parametrize(
    "query",
    [
        "states=sleeping",
        "states=idle,sleeping",
        "timeout=soon",
        "timeout=-1",
        "timeout=-0.5",
        "after=-1",
        "pane=" + "%25" * 40,
        "timeout=1&timeout=2",
    ],
)
def test_out_of_bounds_wait_parameters_are_refused(events_probe, crane, query):
    status, payload = events_probe.get(f"/v1/events/wait?{query}", crane)
    assert status in (400, 403)
    assert payload["error"]["code"] in ("invalid_request", "forbidden")


def test_an_empty_parameter_means_unspecified(events_probe, crane):
    """`?states=` is not a request for no states — every empty parameter here
    reads as absent, the same as `limit=` or `after=`."""
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="idle"
    )
    for query in ("states=", "limit=", "after=", "timeout=", "pane="):
        status, payload = events_probe.get(f"/v1/events/wait?timeout=1&{query}", crane)
        assert status == 200, query
        assert payload["pane"] == "%1"


def test_wait_accepts_every_state_name(events_probe, crane):
    events_probe.events_tmux.facts["%1"] = events.PaneFacts(
        alive=True, kind="amux", created=1000.0, state_option="idle"
    )
    _, payload = events_probe.get(
        f"/v1/events/wait?timeout=1&states={','.join(cs.AGENT_STATES)}", crane
    )
    assert payload["state"] == "idle"


def test_the_default_wait_states_match_the_native_ones(events_probe, crane):
    assert cs.DEFAULT_WAIT_STATES == ("idle", "needs-input", "dead")
    assert set(cs.DEFAULT_WAIT_STATES) <= set(cs.AGENT_STATES)


def test_waiting_needs_the_read_capability(events_probe):
    token = events_probe.agent("%1", "writer", permissions=(cs.PERM_EVENTS_WRITE,))
    status, payload = events_probe.get("/v1/events/wait?timeout=1", token)
    assert status == 403
    assert cs.PERM_CONTEXT_READ in payload["error"]["message"]


# --- the whole interface ---


def test_every_documented_operation_is_routed():
    assert set(cs._ROUTES) == {
        ("GET", "/healthz"),
        ("GET", "/v1/context"),
        ("GET", "/v1/notes"),
        ("POST", "/v1/notes"),
        ("POST", "/v1/events"),
        ("GET", "/v1/events/state"),
        ("GET", "/v1/events/wait"),
    }


def test_the_event_routes_carry_the_agreed_permissions():
    assert cs._ROUTES[("POST", "/v1/events")].requires == cs.PERM_EVENTS_WRITE
    assert cs._ROUTES[("GET", "/v1/events/state")].requires == cs.PERM_CONTEXT_READ
    assert cs._ROUTES[("GET", "/v1/events/wait")].requires == cs.PERM_CONTEXT_READ
