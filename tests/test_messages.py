from __future__ import annotations

import time
from types import SimpleNamespace

import fake_tmux
import pytest

from amux import core, events, messages, store


@pytest.fixture
def delivery(monkeypatch, tmp_path):
    window = fake_tmux.new_window()
    target_pane = window.new_pane()
    sender = messages.Actor(
        "%1", 100.0, "ws", "plan", "/repo", "codex", "red-fox", "r0c0", None
    )
    target = messages.Actor(
        "%2", 100.0, "ws", "review", "/repo", "claude", "blue-owl", "r0c1", None
    )
    actors = {"%1": sender, "%2": target}
    timeline: list[str] = []
    monkeypatch.setattr(
        messages,
        "actor_for",
        lambda server, pane, db_path=None: actors[pane],
    )
    monkeypatch.setattr(messages, "_pane_by_id", lambda server, pane: target_pane)
    monkeypatch.setattr(events, "current_state", lambda pane, socket=None: "idle")
    monkeypatch.setattr(events, "event_cursor", lambda *args, **kwargs: 17)
    monkeypatch.setattr(
        events, "wait_for_fresh_state", lambda *args, **kwargs: "busy"
    )
    monkeypatch.setattr(
        core,
        "submit_to_interface",
        lambda *args, **kwargs: timeline.append("submit") or "",
    )
    return SimpleNamespace(
        server=window.server,
        target=target_pane,
        sender_actor=sender,
        target_actor=target,
        actors=actors,
        timeline=timeline,
        state_dir=tmp_path,
    )


def send(delivery, text: str = "review") -> messages.DeliveryResult:
    return messages.send(
        delivery.server,
        "%1",
        "%2",
        text,
        timeout=5.0,
        state_dir=delivery.state_dir,
    )


def test_busy_target_is_waited_for_before_any_text_is_sent(
    delivery, monkeypatch
) -> None:
    states = iter(["busy", "idle"])
    monkeypatch.setattr(
        events, "current_state", lambda *args, **kwargs: next(states)
    )
    monkeypatch.setattr(
        events,
        "wait",
        lambda *args, **kwargs: delivery.timeline.append("idle") or "idle",
    )

    result = send(delivery)

    assert result.status == "delivered"
    assert delivery.timeline == ["idle", "submit"]


def test_fresh_busy_event_marks_the_message_delivered(delivery) -> None:
    result = send(delivery)

    assert result.status == "delivered"
    assert result.message_id > 0
    assert store.message_by_id(result.message_id)["status"] == "delivered"


def test_no_fresh_busy_event_is_durably_undelivered(
    delivery, monkeypatch
) -> None:
    monkeypatch.setattr(
        events, "wait_for_fresh_state", lambda *args, **kwargs: None
    )

    result = send(delivery)

    assert result.status == "undelivered"
    assert result.reason_code == "busy_timeout"
    assert store.message_by_id(result.message_id)["status"] == "undelivered"


def test_envelope_is_attributed_and_correlated(delivery) -> None:
    result = send(delivery, "review this")

    row = store.message_by_id(result.message_id)
    assert row["envelope"] == (
        f"[amux red-fox @r0c0 %1 message #{result.message_id}] review this"
    )


def test_self_send_is_rejected_before_a_row_is_created(delivery) -> None:
    with pytest.raises(messages.DeliveryFailure, match="itself"):
        messages.send(
            delivery.server,
            "%1",
            "%1",
            "review",
            state_dir=delivery.state_dir,
        )
    assert store.visible_messages("ws", "/repo", "%1") == []


@pytest.mark.parametrize(
    "replacement",
    [
        messages.Actor(
            "%2",
            100.0,
            "other",
            "review",
            "/repo",
            "claude",
            "blue-owl",
            "r0c1",
            None,
        ),
        messages.Actor(
            "%2",
            100.0,
            "ws",
            "review",
            "/other",
            "claude",
            "blue-owl",
            "r0c1",
            None,
        ),
    ],
)
def test_cross_scope_target_is_rejected(delivery, replacement) -> None:
    delivery.actors["%2"] = replacement

    with pytest.raises(messages.DeliveryFailure, match="workspace and repository"):
        send(delivery)


def test_target_death_after_submission_is_undelivered(delivery, monkeypatch) -> None:
    monkeypatch.setattr(
        events, "wait_for_fresh_state", lambda *args, **kwargs: "dead"
    )

    result = send(delivery)

    assert (result.status, result.reason_code) == ("undelivered", "target_dead")


def test_recycled_target_is_rejected_before_submission(delivery, monkeypatch) -> None:
    replaced = messages.Actor(
        "%2",
        200.0,
        "ws",
        "review",
        "/repo",
        "claude",
        "blue-owl",
        "r0c1",
        None,
    )
    target_resolutions = iter([delivery.target_actor, replaced])

    def resolve(server, pane, db_path=None):
        return delivery.sender_actor if pane == "%1" else next(target_resolutions)

    monkeypatch.setattr(messages, "actor_for", resolve)

    result = send(delivery)

    assert (result.status, result.reason_code) == ("undelivered", "target_replaced")
    assert "submit" not in delivery.timeline


def test_interface_failure_is_undelivered(delivery, monkeypatch) -> None:
    monkeypatch.setattr(
        core, "submit_to_interface", lambda *args, **kwargs: "input line"
    )

    result = send(delivery)

    assert (result.status, result.reason_code) == (
        "undelivered",
        "submission_failed",
    )


def test_transport_exception_is_durably_undelivered(delivery, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise OSError("tmux socket vanished")

    monkeypatch.setattr(core, "submit_to_interface", fail)

    result = send(delivery)

    assert (result.status, result.reason_code) == (
        "undelivered",
        "transport_error",
    )
    assert store.message_by_id(result.message_id)["status"] == "undelivered"


def test_interruption_is_recorded_with_exit_130(delivery, monkeypatch) -> None:
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(core, "submit_to_interface", interrupt)

    result = send(delivery)

    assert (result.status, result.reason_code, result.exit_code) == (
        "undelivered",
        "sender_interrupted",
        130,
    )


def test_body_limit_is_checked_before_persistence(delivery) -> None:
    with pytest.raises(ValueError, match="4000"):
        send(delivery, "x" * 4001)
    assert store.visible_messages("ws", "/repo", "%1") == []


@pytest.mark.parametrize("timeout", [0.5, 3600.1, float("nan")])
def test_timeout_limit_is_checked_before_persistence(delivery, timeout) -> None:
    with pytest.raises(ValueError, match="between 1 and 3600"):
        messages.send(
            delivery.server,
            "%1",
            "%2",
            "review",
            timeout=timeout,
            state_dir=delivery.state_dir,
        )
    assert store.visible_messages("ws", "/repo", "%1") == []


def test_two_lock_handles_for_one_target_do_not_enter_together(tmp_path) -> None:
    now = time.monotonic()
    first = messages.TargetLock(tmp_path, "amux-root", "%2", deadline=now + 10.0)
    second = messages.TargetLock(tmp_path, "amux-root", "%2", deadline=now)

    with (
        first,
        pytest.raises(messages.DeliveryFailure, match="another sender"),
        second,
    ):
        raise AssertionError("second lock entered")


def test_target_lock_releases_after_exception(tmp_path) -> None:
    now = time.monotonic()
    with pytest.raises(RuntimeError, match="boom"), messages.TargetLock(
        tmp_path, "amux-root", "%2", deadline=now + 10.0
    ):
        raise RuntimeError("boom")

    with messages.TargetLock(
        tmp_path, "amux-root", "%2", deadline=time.monotonic() + 10.0
    ):
        pass


def test_result_line_tells_the_sender_delivery_status() -> None:
    delivered = messages.DeliveryResult(42, "delivered", "%2", "blue-owl")
    failed = messages.DeliveryResult(
        43,
        "undelivered",
        "%2",
        "blue-owl",
        "busy_timeout",
        "no fresh busy event",
        1,
    )

    assert messages.result_line(delivered) == "message #42 delivered to blue-owl (%2)"
    assert messages.result_line(failed) == (
        "message #43 undelivered to blue-owl (%2): no fresh busy event"
    )
