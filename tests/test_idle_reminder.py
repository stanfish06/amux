"""Claude Code's idle reminder must classify as idle, not needs-input.

The Notification hook fires both for prompts that need a human and for the
~60s "Claude is waiting for your input" reminder. The reminder means the
agent is parked at an empty composer -- exactly the state `amux send`
delivers to -- so classifying it needs-input stranded idle agents out of
reach of messages until the delivery deadline.
"""

from __future__ import annotations

from amux import events, store

REMINDER = "Claude is waiting for your input"


def test_an_idle_reminder_notification_is_idle() -> None:
    assert events.state_for("notify", REMINDER) == "idle"
    event = events.Event(ts=1.0, kind="notify", pane="%7", detail=REMINDER)
    assert event.state == "idle"


def test_a_genuine_notification_still_needs_input() -> None:
    assert events.state_for("notify", "") == "needs-input"
    assert (
        events.state_for("notify", "Claude needs your permission to use Bash")
        == "needs-input"
    )


def test_a_stale_needs_input_option_defers_to_an_idle_reminder_event() -> None:
    """A pane option published before this classification existed keeps saying
    needs-input; the event it was derived from is the source of truth."""
    latest = events.Event(ts=200.0, kind="notify", pane="%7", detail=REMINDER)
    state = events.resolve_state(alive=True, option="needs-input", latest=latest)
    assert state == "idle"


def test_a_needs_input_option_survives_a_genuine_notification() -> None:
    latest = events.Event(
        ts=200.0, kind="notify", pane="%7", detail="may I edit main.py?"
    )
    state = events.resolve_state(alive=True, option="needs-input", latest=latest)
    assert state == "needs-input"


def test_wait_for_fresh_state_reads_a_reminder_as_idle(db_path, monkeypatch) -> None:
    store.add_event(110.0, "%7", "notify", detail=REMINDER, db_path=db_path)
    monkeypatch.setattr(
        events,
        "pane_facts",
        lambda pane, socket=None: events.PaneFacts(alive=True, created=100.0),
    )

    assert (
        events.wait_for_fresh_state(
            "%7",
            after=0,
            for_states=("idle",),
            timeout=1.0,
            socket="amux-test",
            expected_created=100.0,
            db_path=db_path,
        )
        == "idle"
    )
