from __future__ import annotations

import json

import pytest

from amux import cli, core, events, messages, store


def actor(pane: str = "%1") -> messages.Actor:
    return messages.Actor(
        pane, 100.0, "ws", "plan", "/repo", "codex", "red-fox", "r0c0", 1
    )


def message_row(**overrides):
    row = {
        "id": 42,
        "status": "delivered",
        "sender_pane": "%1",
        "sender_name": "red-fox",
        "target_pane": "%2",
        "target_name": "blue-owl",
        "body": "review this",
        "reason": "",
    }
    row.update(overrides)
    return row


@pytest.fixture
def host(monkeypatch):
    server = object()
    monkeypatch.setattr(core, "get_server", lambda socket=None: server)
    monkeypatch.setattr(events, "self_pane_id", lambda: "%1")
    monkeypatch.setattr(
        messages,
        "actor_for",
        lambda current_server, pane, db_path=None: actor(pane),
    )
    return server


def test_send_prints_delivery_and_returns_zero(host, monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.setattr(
        messages,
        "send",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or messages.DeliveryResult(42, "delivered", "%2", "blue-owl"),
    )

    assert cli.main(["send", "%2", "review", "this"]) == 0

    assert capsys.readouterr().out.strip() == (
        "message #42 delivered to blue-owl (%2)"
    )
    assert calls[0][0] == (host, "%1", "%2", "review this")


def test_send_reports_undelivered_on_stderr(host, monkeypatch, capsys) -> None:
    result = messages.DeliveryResult(
        42,
        "undelivered",
        "%2",
        "blue-owl",
        "busy_timeout",
        "no fresh busy event",
        1,
    )
    monkeypatch.setattr(messages, "send", lambda *args, **kwargs: result)

    assert cli.main(["send", "%2", "review"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == (
        "message #42 undelivered to blue-owl (%2): no fresh busy event"
    )


def test_send_requires_sender_context(host, monkeypatch, capsys) -> None:
    monkeypatch.setattr(events, "self_pane_id", lambda: None)

    assert cli.main(["send", "%2", "review"]) == 1
    assert "not inside an amux agent pane" in capsys.readouterr().err


@pytest.mark.parametrize("timeout", ["0", "0.5", "3601", "nan"])
def test_send_rejects_timeout_outside_one_to_3600_seconds(timeout) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["send", "%2", "review", "--timeout", timeout])
    assert error.value.code == 2


def test_messages_passes_the_status_filter(host, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        store,
        "visible_messages",
        lambda *args, **kwargs: calls.append((args, kwargs)) or [],
    )

    assert cli.main(["messages", "--status", "undelivered", "-n", "7"]) == 0

    assert calls == [(('ws', '/repo', '%1'), {'status': 'undelivered', 'limit': 7})]


def test_messages_json_is_one_compact_row_per_line(
    host, monkeypatch, capsys
) -> None:
    rows = [message_row(), message_row(id=41, body="earlier")]
    monkeypatch.setattr(store, "visible_messages", lambda *args, **kwargs: rows)

    assert cli.main(["messages", "--json"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines == [json.dumps(row, separators=(",", ":")) for row in rows]


def test_messages_uses_only_the_selected_caller_pane(
    host, monkeypatch
) -> None:
    actors = []
    calls = []

    def resolve(server, pane, db_path=None):
        actors.append(pane)
        return actor(pane)

    monkeypatch.setattr(messages, "actor_for", resolve)
    monkeypatch.setattr(
        store,
        "visible_messages",
        lambda *args, **kwargs: calls.append((args, kwargs)) or [],
    )

    assert cli.main(["messages", "--pane", "%9"]) == 0

    assert actors == ["%9"]
    assert calls[0][0] == ("ws", "/repo", "%9")


def test_messages_human_output_names_direction_and_peer(
    host, monkeypatch, capsys
) -> None:
    rows = [
        message_row(),
        message_row(
            id=43,
            sender_pane="%3",
            sender_name="gold-moth",
            target_pane="%1",
            target_name="red-fox",
            status="undelivered",
            body="please check",
            reason="target died",
        ),
    ]
    monkeypatch.setattr(store, "visible_messages", lambda *args, **kwargs: rows)

    assert cli.main(["messages"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == " 42  delivered    to    blue-owl     review this"
    assert lines[1] == (
        " 43  undelivered  from  gold-moth    please check (target died)"
    )
