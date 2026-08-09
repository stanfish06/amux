from __future__ import annotations

import json

import pytest
from test_sandbox_client_fake_service import TOKEN, FakeContextService

from amux import sandbox_client as sc


@pytest.fixture
def run(tmp_path, monkeypatch):
    config = tmp_path / "context.json"
    monkeypatch.setenv(sc.CONFIG_ENV, str(config))

    def invoke(routes, *argv):
        with FakeContextService(routes) as service:
            config.write_text(
                json.dumps({"endpoint": service.endpoint, "token": TOKEN})
            )
            config.chmod(0o600)
            return sc.main(list(argv)), service

    return invoke


DELIVERED = {
    "message": {"id": 42, "status": "delivered"},
    "summary": "message #42 delivered to blue-owl (%2)",
}

UNDELIVERED = {
    "message": {"id": 43, "status": "undelivered"},
    "summary": (
        "message #43 undelivered to blue-owl (%2): no fresh busy event"
    ),
}

ROWS = [
    {
        "id": 42,
        "status": "delivered",
        "sender_pane": "%1",
        "sender_name": "red-fox",
        "target_pane": "%2",
        "target_name": "blue-owl",
        "body": "review this",
        "reason": "",
    },
    {
        "id": 41,
        "status": "undelivered",
        "sender_pane": "%3",
        "sender_name": "gold-moth",
        "target_pane": "%1",
        "target_name": "red-fox",
        "body": "please check",
        "reason": "target died",
    },
]


def test_sandbox_send_posts_target_text_and_timeout(run, capsys) -> None:
    rc, service = run(
        {("POST", "/v1/messages"): DELIVERED},
        "send",
        "%2",
        "review",
        "this",
        "--timeout",
        "30",
    )

    assert rc == 0
    assert service.body_of("POST", "/v1/messages") == {
        "target": "%2",
        "text": "review this",
        "timeout": 30.0,
    }
    assert capsys.readouterr().out.strip() == DELIVERED["summary"]


def test_sandbox_send_returns_one_for_durable_undelivered(run, capsys) -> None:
    rc, _ = run(
        {("POST", "/v1/messages"): UNDELIVERED},
        "send",
        "%2",
        "review",
    )

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == UNDELIVERED["summary"]


def test_send_extends_http_timeout_by_poll_slack(
    run, monkeypatch
) -> None:
    seen = []
    original = sc.ContextClient.post

    def post(client, path, body, timeout=None):
        seen.append(timeout)
        return original(client, path, body, timeout)

    monkeypatch.setattr(sc.ContextClient, "post", post)

    rc, _ = run(
        {("POST", "/v1/messages"): DELIVERED},
        "send",
        "%2",
        "review",
        "--timeout",
        "30",
    )

    assert rc == 0
    assert seen == [30.0 + sc.POLL_SLACK_S]


def test_messages_prints_human_rows(run, capsys) -> None:
    rc, _ = run(
        {
            ("GET", "/v1/messages"): {
                "messages": ROWS,
                "caller_pane": "%1",
            }
        },
        "messages",
    )

    assert rc == 0
    assert capsys.readouterr().out.splitlines() == [
        " 42  delivered    to    blue-owl     review this",
        " 41  undelivered  from  gold-moth    please check (target died)",
    ]


def test_messages_prints_jsonl(run, capsys) -> None:
    rc, _ = run(
        {
            ("GET", "/v1/messages"): {
                "messages": ROWS,
                "caller_pane": "%1",
            }
        },
        "messages",
        "--json",
    )

    assert rc == 0
    assert capsys.readouterr().out.splitlines() == [
        json.dumps(row, separators=(",", ":")) for row in ROWS
    ]


def test_messages_forwards_status_and_limit(run) -> None:
    rc, service = run(
        {
            ("GET", "/v1/messages"): {
                "messages": [],
                "caller_pane": "%1",
            }
        },
        "messages",
        "--status",
        "undelivered",
        "-n",
        "7",
    )

    assert rc == 0
    recorded = service.only("GET", "/v1/messages")
    assert recorded.q("status") == "undelivered"
    assert recorded.q("limit") == "7"


@pytest.mark.parametrize("timeout", ["0.5", "3601", "nan"])
def test_send_rejects_invalid_timeout_before_http(run, timeout) -> None:
    with pytest.raises(SystemExit) as error:
        run(
            {("POST", "/v1/messages"): DELIVERED},
            "send",
            "%2",
            "review",
            "--timeout",
            timeout,
        )
    assert error.value.code == 2


def test_boundary_text_lists_send_and_messages(run, capsys) -> None:
    rc, _ = run({}, "spw", "ws")

    assert rc == 2
    text = capsys.readouterr().err
    assert "send" in text
    assert "messages" in text
