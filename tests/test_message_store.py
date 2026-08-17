from __future__ import annotations

from pathlib import Path

from amux import store


def create(db_path: Path, *, deadline: float = 200.0) -> int:
    return store.create_message(
        created_ts=100.0,
        deadline_ts=deadline,
        sender_worktree_id=None,
        target_worktree_id=None,
        repo="/repo",
        workspace="ws",
        sender_task="plan",
        sender_pane="%1",
        sender_agent="codex",
        sender_name="red-fox",
        target_task="review",
        target_pane="%2",
        target_agent="claude",
        target_name="blue-owl",
        target_created_ts=90.0,
        body="review this",
        db_path=db_path,
    )


def test_message_moves_from_pending_to_delivered(db_path: Path) -> None:
    message_id = create(db_path)
    store.set_message_envelope(
        message_id, "[amux red-fox %1 message #1] review this", db_path
    )
    store.mark_message_submitted(message_id, 120.0, db_path)

    assert store.finish_message(
        message_id, "delivered", now=125.0, db_path=db_path
    )

    row = store.message_by_id(message_id, db_path)
    assert row is not None
    assert row["status"] == "delivered"
    assert row["submitted_ts"] == 120.0
    assert row["delivered_ts"] == 125.0
    assert row["envelope"].endswith("review this")


def test_terminal_message_cannot_be_finished_twice(db_path: Path) -> None:
    message_id = create(db_path)
    assert store.finish_message(
        message_id,
        "undelivered",
        "busy_timeout",
        "no busy event",
        201.0,
        db_path,
    )
    assert not store.finish_message(
        message_id, "delivered", now=202.0, db_path=db_path
    )
    assert store.message_by_id(message_id, db_path)["status"] == "undelivered"


def test_expiry_marks_only_overdue_pending_rows(db_path: Path) -> None:
    expired = create(db_path, deadline=110.0)
    live = create(db_path, deadline=210.0)

    assert store.expire_messages(120.0, db_path) == 1

    assert store.message_by_id(expired, db_path)["reason_code"] == "deadline_expired"
    assert store.message_by_id(live, db_path)["status"] == "pending"


def test_visible_messages_are_only_sent_or_received_by_the_pane(
    db_path: Path,
) -> None:
    mine = create(db_path)

    assert [
        row["id"]
        for row in store.visible_messages(
            "ws", "/repo", "%1", now=150.0, db_path=db_path
        )
    ] == [mine]
    assert [
        row["id"]
        for row in store.visible_messages(
            "ws", "/repo", "%2", now=150.0, db_path=db_path
        )
    ] == [mine]
    assert store.visible_messages(
        "ws", "/repo", "%9", now=150.0, db_path=db_path
    ) == []


def test_visible_messages_filter_status_and_limit(db_path: Path) -> None:
    first = create(db_path)
    second = create(db_path)
    store.finish_message(first, "undelivered", "timeout", "late", 201.0, db_path)
    store.finish_message(second, "delivered", now=150.0, db_path=db_path)

    rows = store.visible_messages(
        "ws",
        "/repo",
        "%1",
        status="delivered",
        limit=1,
        now=150.0,
        db_path=db_path,
    )

    assert [row["id"] for row in rows] == [second]
