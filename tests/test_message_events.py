from __future__ import annotations

from amux import events, store


def test_cursor_uses_the_latest_event_in_the_current_incarnation(db_path) -> None:
    store.add_event(90.0, "%7", "busy", db_path=db_path)
    store.add_event(110.0, "%7", "stop", db_path=db_path)

    assert events.event_cursor("%7", 100.0, db_path=db_path) == 2


def test_wait_requires_a_state_event_newer_than_the_cursor(
    db_path, monkeypatch
) -> None:
    store.add_event(110.0, "%7", "busy", db_path=db_path)
    cursor = events.event_cursor("%7", 100.0, db_path=db_path)
    monkeypatch.setattr(
        events,
        "pane_facts",
        lambda pane, socket=None: events.PaneFacts(alive=True, created=100.0),
    )
    signals = []

    def signal(socket: str, pane: str, timeout: float) -> None:
        signals.append(pane)
        store.add_event(120.0, pane, "busy", db_path=db_path)

    assert (
        events.wait_for_fresh_state(
            "%7",
            after=cursor,
            for_states=("busy",),
            timeout=1.0,
            socket="amux-test",
            expected_created=100.0,
            db_path=db_path,
            block=signal,
        )
        == "busy"
    )
    assert signals == ["%7"]


def test_recycled_pane_is_reported_dead(db_path, monkeypatch) -> None:
    monkeypatch.setattr(
        events,
        "pane_facts",
        lambda pane, socket=None: events.PaneFacts(alive=True, created=200.0),
    )

    assert (
        events.wait_for_fresh_state(
            "%7",
            after=0,
            timeout=1.0,
            socket="amux-test",
            expected_created=100.0,
            db_path=db_path,
        )
        == "dead"
    )


def test_wait_times_out_without_a_fresh_matching_event(db_path, monkeypatch) -> None:
    now = [0.0]
    monkeypatch.setattr(
        events,
        "pane_facts",
        lambda pane, socket=None: events.PaneFacts(alive=True, created=100.0),
    )

    def block(socket: str, pane: str, timeout: float) -> None:
        now[0] += timeout

    assert (
        events.wait_for_fresh_state(
            "%7",
            after=0,
            timeout=2.0,
            socket="amux-test",
            expected_created=100.0,
            db_path=db_path,
            clock=lambda: now[0],
            block=block,
        )
        is None
    )


def test_wait_ignores_a_busy_event_before_submission(
    db_path, monkeypatch
) -> None:
    store.add_event(105.0, "%7", "busy", db_path=db_path)
    store.add_event(110.0, "%7", "busy", db_path=db_path)
    monkeypatch.setattr(
        events,
        "pane_facts",
        lambda pane, socket=None: events.PaneFacts(alive=True, created=100.0),
    )
    signals = []

    def signal(socket: str, pane: str, timeout: float) -> None:
        signals.append(pane)
        store.add_event(120.0, pane, "busy", db_path=db_path)

    assert (
        events.wait_for_fresh_state(
            "%7",
            after=0,
            timeout=1.0,
            socket="amux-test",
            expected_created=100.0,
            not_before=110.0,
            db_path=db_path,
            block=signal,
        )
        == "busy"
    )
    assert signals == ["%7"]


def test_wait_does_not_accept_an_event_observed_after_the_deadline(
    db_path, monkeypatch
) -> None:
    now = [0.0]
    monkeypatch.setattr(
        events,
        "pane_facts",
        lambda pane, socket=None: events.PaneFacts(alive=True, created=100.0),
    )

    def signal(socket: str, pane: str, timeout: float) -> None:
        now[0] += timeout + 1.0
        store.add_event(120.0, pane, "busy", db_path=db_path)

    assert (
        events.wait_for_fresh_state(
            "%7",
            after=0,
            timeout=1.0,
            socket="amux-test",
            expected_created=100.0,
            db_path=db_path,
            clock=lambda: now[0],
            block=signal,
        )
        is None
    )
