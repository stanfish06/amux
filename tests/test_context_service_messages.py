from __future__ import annotations

from dataclasses import replace

import pytest

from amux import context_service as cs
from amux import messages, store


@pytest.fixture
def identity() -> cs.Identity:
    return cs.Identity(
        worktree_id=1,
        pane="%1",
        workspace="ws",
        task="plan",
        repo="/repo",
        agent="codex",
        name="red-fox",
        socket="amux-test",
        permissions=frozenset(
            {
                cs.PERM_CONTEXT_READ,
                cs.PERM_NOTES_WRITE,
                cs.PERM_EVENTS_WRITE,
                "messages:write",
            }
        ),
    )


@pytest.fixture
def service(tmp_path, db_path, identity):
    def authenticate(current_service, token):
        if token == "readonly":
            return replace(
                identity, permissions=frozenset({cs.PERM_CONTEXT_READ})
            )
        return identity

    return cs.ContextService(
        cs.ServiceConfig(
            db_path=db_path,
            state_dir=tmp_path / "state",
            max_results=50,
            default_results=10,
        ),
        authenticator=authenticate,
        server_factory=lambda socket: {"socket": socket},
    )


def request(
    method: str,
    path: str = "/v1/messages",
    *,
    body: dict | None = None,
    query: dict[str, list[str]] | None = None,
    token: str = "agent",
) -> cs.Request:
    return cs.Request(
        method=method,
        path=path,
        body=body or {},
        query=query or {},
        authorization=f"Bearer {token}",
    )


def test_post_message_derives_sender_from_capability(
    service, monkeypatch
) -> None:
    seen = {}

    def deliver(server, sender, target, body, **options):
        seen.update(
            server=server,
            sender=sender,
            target=target,
            body=body,
            options=options,
        )
        return messages.DeliveryResult(42, "delivered", target, "blue-owl")

    monkeypatch.setattr(messages, "send", deliver)

    status, payload = service.handle(
        request(
            "POST",
            body={"target": "%2", "text": "review", "timeout": 30.0},
        )
    )

    assert status == 200
    assert seen == {
        "server": {"socket": "amux-test"},
        "sender": "%1",
        "target": "%2",
        "body": "review",
        "options": {
            "timeout": 30.0,
            "db_path": service.db_path,
            "state_dir": service.config.state_home,
        },
    }
    assert payload["message"]["status"] == "delivered"
    assert payload["summary"] == "message #42 delivered to blue-owl (%2)"


def test_message_send_requires_messages_write(service) -> None:
    with pytest.raises(cs.ServiceError, match="messages:write") as error:
        service.handle(
            request(
                "POST",
                body={"target": "%2", "text": "review"},
                token="readonly",
            )
        )
    assert error.value.status == 403


@pytest.mark.parametrize(
    "body, message",
    [
        ({"text": "review"}, "target"),
        ({"target": 2, "text": "review"}, "target"),
        ({"target": "%2"}, "text"),
        ({"target": "%2", "text": 3}, "text"),
        ({"target": "%2", "text": "x" * 4001}, "4000"),
        ({"target": "%2", "text": "review", "timeout": 0.5}, "timeout"),
        ({"target": "%2", "text": "review", "timeout": 3601}, "timeout"),
        ({"target": "%2", "text": "review", "timeout": "30"}, "timeout"),
        ({"target": "%2", "text": "review", "timeout": float("nan")}, "timeout"),
    ],
)
def test_post_message_validates_fields(service, monkeypatch, body, message) -> None:
    monkeypatch.setattr(
        messages,
        "send",
        lambda *args, **kwargs: pytest.fail("invalid input reached dispatcher"),
    )

    with pytest.raises(cs.ServiceError, match=message) as error:
        service.handle(request("POST", body=body))
    assert error.value.status == 400


def test_undelivered_message_is_a_successful_http_exchange(
    service, monkeypatch
) -> None:
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

    status, payload = service.handle(
        request("POST", body={"target": "%2", "text": "review"})
    )

    assert status == 200
    assert payload["message"]["status"] == "undelivered"
    assert "undelivered" in payload["summary"]


def test_dispatcher_scope_failure_is_a_redacted_request_error(
    service, monkeypatch
) -> None:
    secret = "topsecret"

    def reject(*args, **kwargs):
        raise messages.DeliveryFailure(
            "scope_mismatch", f"Authorization: Bearer {secret} is out of scope"
        )

    monkeypatch.setattr(messages, "send", reject)

    with pytest.raises(cs.ServiceError) as error:
        service.handle(
            request("POST", body={"target": "%2", "text": "review"})
        )

    assert error.value.status == 400
    assert secret not in error.value.envelope()["error"]["message"]
    assert "<redacted>" in error.value.envelope()["error"]["message"]


def test_get_messages_is_capability_scoped(service, monkeypatch) -> None:
    rows = [{"id": 42, "status": "undelivered"}]
    calls = []
    monkeypatch.setattr(
        store,
        "visible_messages",
        lambda *args, **kwargs: calls.append((args, kwargs)) or rows,
    )

    status, payload = service.handle(
        request(
            "GET",
            query={"status": ["undelivered"], "limit": ["7"]},
        )
    )

    assert status == 200
    assert payload == {"messages": rows}
    assert calls == [
        (
            ("ws", "/repo", "%1"),
            {
                "status": "undelivered",
                "limit": 7,
                "db_path": service.db_path,
            },
        )
    ]


@pytest.mark.parametrize(
    "query, message",
    [
        ({"status": ["lost"]}, "status"),
        ({"limit": ["0"]}, "between 1 and 50"),
        ({"limit": ["51"]}, "between 1 and 50"),
    ],
)
def test_get_messages_validates_filters(service, query, message) -> None:
    with pytest.raises(cs.ServiceError, match=message) as error:
        service.handle(request("GET", query=query))
    assert error.value.status == 400


def test_get_messages_requires_context_read(service) -> None:
    identity = replace(
        service.authenticator(service, "agent"),
        permissions=frozenset({"messages:write"}),
    )
    service.authenticator = lambda current_service, token: identity

    with pytest.raises(cs.ServiceError, match="context:read"):
        service.handle(request("GET"))
