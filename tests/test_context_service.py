"""Task 2.1: the loopback skeleton refuses everything it should.

These tests speak raw `http.client` rather than a convenience wrapper because
half the contract is about malformed requests — a missing Content-Length, a
lying one, a chunked body — which a well-behaved client will not produce.

Every test points the service at a database under `tmp_path`. Nothing here may
touch the real `$XDG_STATE_HOME/amux/context.db`.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import sqlite3
import time
from dataclasses import dataclass

import pytest

from amux import context_service as cs
from amux import store


# --- fixtures ---


@dataclass
class Client:
    """Requests against a running service, with the escape hatches the bounded
    parsing tests need."""

    handle: cs.ServiceHandle
    records: list[logging.LogRecord]

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        token: str | None = None,
    ) -> tuple[int, dict, dict[str, str]]:
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection(cs.LOOPBACK, self.handle.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            raw = response.read()
            payload = json.loads(raw) if raw else {}
            return response.status, payload, dict(response.getheaders())
        finally:
            conn.close()

    def raw(self, lines: list[str], payload: bytes = b"") -> tuple[int, dict]:
        """Send a hand-built request. `http.client` refuses to send some of the
        shapes we must reject, so this writes the bytes itself."""
        sock = socket.create_connection((cs.LOOPBACK, self.handle.port), timeout=5)
        try:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            status = int(head.split(b" ")[1])
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            while len(rest) < length:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                rest += chunk
            return status, json.loads(rest) if rest else {}
        finally:
            sock.close()

    @property
    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]

    def wait_for_log(self, needle: str, timeout: float = 5.0) -> str:
        """Block until a log record containing `needle` exists, then return all.

        The access line is written after the response is flushed, so a client
        holding its answer can be ahead of the log. Waiting for the record keeps
        this deterministic: an intermittently green leak test is worse than a
        red one, because it teaches everyone to re-run until it passes.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            joined = "\n".join(self.messages)
            if needle in joined:
                return joined
            time.sleep(0.01)
        raise AssertionError(f"no log record containing {needle!r} within {timeout}s")



class _Capture(logging.Handler):
    """Collects records so the redaction tests can read what would be written."""

    def __init__(self, sink: list[logging.LogRecord]) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(record)


def _config(tmp_path, **overrides) -> cs.ServiceConfig:
    # port 0 is ephemeral: tests must not fight over the stable port.
    settings: dict = {
        "port": 0,
        "db_path": tmp_path / "context.db",
        "state_dir": tmp_path / "state",
        # A test starts and stops a server per case, and shutdown() blocks until
        # the accept loop next checks its flag. A daemon's 0.5s would be most of
        # this file's runtime.
        "shutdown_poll_s": 0.01,
    }
    return cs.ServiceConfig(**{**settings, **overrides})


def _serve(config, authenticator=None):
    records: list[logging.LogRecord] = []
    handle = cs.start_service(config, authenticator=authenticator)
    capture = _Capture(records)
    log = cs.get_logger()
    log.addHandler(capture)
    log.setLevel(logging.DEBUG)
    try:
        yield Client(handle=handle, records=records)
    finally:
        log.removeHandler(capture)
        handle.stop()


@pytest.fixture
def client(tmp_path):
    """A service no token can reach — the shipped default."""
    yield from _serve(_config(tmp_path))


VALID_TOKEN = "test-token"
IDENTITY = cs.Identity(worktree_id=7, pane="%1", workspace="ws", task="t0", repo="/r")


def _accept_test_token(service, token):
    if token != VALID_TOKEN:
        raise cs.ServiceError("unauthorized", "invalid or expired capability token")
    return IDENTITY


@pytest.fixture
def authed(tmp_path):
    """A service that accepts one token, so routing and bounds can be reached.

    Stands in for task 2.2's store-backed authenticator; the seam it uses is
    the one 2.2 fills.
    """
    yield from _serve(_config(tmp_path), authenticator=_accept_test_token)


# --- loopback only ---


def test_service_binds_loopback_only(client):
    assert client.handle.server.server_address[0] == cs.LOOPBACK
    assert cs.ServiceConfig(port=0).address == (cs.LOOPBACK, 0)
    # There is no bind-host setting to get wrong.
    assert "host" not in cs.ServiceConfig.__dataclass_fields__


def test_start_service_reports_the_bound_port(client):
    assert client.handle.port > 0
    assert client.handle.service.config.port == client.handle.port
    assert client.handle.base_url == f"http://{cs.LOOPBACK}:{client.handle.port}"


def test_busy_port_fails_loudly_without_a_second_listener(tmp_path):
    first = cs.start_service(_config(tmp_path))
    try:
        taken = _config(tmp_path, port=first.port)
        with pytest.raises(cs.ServiceStartupError) as caught:
            cs.build_server(cs.ContextService(taken))
        assert str(first.port) in str(caught.value)
        assert "port" in str(caught.value)
    finally:
        first.stop()


# --- health ---


def test_healthz_needs_no_token_and_reports_schema(client):
    status, payload, _ = client.request("GET", "/healthz")
    assert status == 200
    assert payload["ok"] is True
    assert payload["service"] == cs.SERVICE_NAME
    assert payload["api"] == "v1"
    assert payload["status"] == "ok"
    assert payload["schema_version"] == store.SCHEMA_VERSION
    assert payload["expected_schema_version"] == store.SCHEMA_VERSION
    assert payload["compatible"] is True


def test_healthz_leaks_no_paths_or_identity(client):
    _, payload, _ = client.request("GET", "/healthz")
    blob = json.dumps(payload)
    assert str(client.handle.service.db_path) not in blob
    assert "/" not in blob.replace('"api":"v1"', "")
    assert set(payload) == {
        "ok",
        "service",
        "api",
        "status",
        "schema_version",
        "expected_schema_version",
        "compatible",
    }


def test_healthz_ignores_a_bogus_token(client):
    status, payload, _ = client.request("GET", "/healthz", token="nonsense")
    assert status == 200
    assert payload["status"] == "ok"


def test_healthz_is_degraded_on_an_unsupported_schema(tmp_path):
    db = tmp_path / "context.db"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 5}")
    conn.close()
    for probe in _serve(_config(tmp_path)):
        status, payload, _ = probe.request("GET", "/healthz")
        assert status == 503
        assert payload["ok"] is False
        assert payload["status"] == "degraded"
        assert payload["compatible"] is False
        assert payload["schema_version"] == store.SCHEMA_VERSION + 5
        break


def test_healthz_is_unavailable_when_the_store_cannot_be_opened(tmp_path):
    unopenable = tmp_path / "context.db"
    unopenable.write_text("this is not a database")
    for probe in _serve(_config(tmp_path)):
        status, payload, _ = probe.request("GET", "/healthz")
        assert status == 503
        assert payload["ok"] is False
        assert payload["status"] == "unavailable"
        assert payload["schema_version"] is None
        break


def test_healthz_rejects_other_methods(client):
    status, payload, _ = client.request("POST", "/healthz", body=b"{}", headers={
        "Content-Type": "application/json"
    })
    assert status == 405
    assert payload["error"]["code"] == "method_not_allowed"
    assert "GET" in payload["error"]["message"]


# --- authentication fails closed ---


@pytest.mark.parametrize("path", ["/v1/context", "/v1/notes", "/v1/events", "/v1/nope"])
def test_v1_without_a_token_is_unauthorized(client, path):
    status, payload, _ = client.request("GET", path)
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert "Authorization" in payload["error"]["message"]


@pytest.mark.parametrize(
    "header",
    [
        "",
        "   ",
        "Basic abc123",
        "Bearer",
        "Bearer    ",
        "token abc123",
        "Bearer " + "x" * (cs.MAX_TOKEN_CHARS + 1),
    ],
)
def test_malformed_authorization_is_unauthorized(authed, header):
    status, payload, _ = authed.request(
        "GET", "/v1/context", headers={"Authorization": header}
    )
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_wrong_token_is_unauthorized(authed):
    status, payload, _ = authed.request("GET", "/v1/context", token="not-the-token")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_default_service_accepts_no_token_at_all(client):
    status, payload, _ = client.request("GET", "/v1/context", token=VALID_TOKEN)
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_unauthenticated_callers_cannot_map_the_interface(client, authed):
    """An unknown path answers the same 401 as a real one until a caller proves
    who it is; only then does it become a 404."""
    anonymous, payload, _ = client.request("GET", "/v1/nope")
    assert (anonymous, payload["error"]["code"]) == (401, "unauthorized")
    known, _, _ = client.request("GET", "/v1/context")
    assert known == anonymous

    status, payload, _ = authed.request("GET", "/v1/nope", token=VALID_TOKEN)
    assert status == 404
    assert payload["error"]["code"] == "not_found"


def test_authenticated_request_reaches_routing(authed):
    """The seam works end to end: identity is derived, then the route decides."""
    seen = {}

    def spy(service, request):
        seen["identity"] = request.identity
        return 200, {"ok": True}

    cs._ROUTES[("GET", "/v1/spy")] = cs.Route(handler=spy)
    try:
        status, payload, _ = authed.request("GET", "/v1/spy", token=VALID_TOKEN)
    finally:
        del cs._ROUTES[("GET", "/v1/spy")]
    assert (status, payload) == (200, {"ok": True})
    assert seen["identity"] == IDENTITY


def test_identity_comes_from_the_token_not_the_request(authed):
    """A body cannot rename its author. 2.2 enforces this against real token
    records; the rule is that handlers only ever see `request.identity`."""
    seen = {}

    def spy(service, request):
        seen["identity"] = request.identity
        seen["body"] = request.body
        return 200, {"ok": True}

    cs._ROUTES[("POST", "/v1/spy")] = cs.Route(handler=spy)
    try:
        authed.request(
            "POST",
            "/v1/spy",
            token=VALID_TOKEN,
            body=json.dumps(
                {"agent": "impostor", "workspace": "other", "worktree_id": 999}
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
    finally:
        del cs._ROUTES[("POST", "/v1/spy")]
    assert seen["identity"] == IDENTITY
    assert seen["identity"].worktree_id == 7
    assert seen["body"]["agent"] == "impostor"  # data, never attribution


# --- bounded JSON parsing ---


def _post(client, body: bytes, content_type: str | None = "application/json"):
    headers = {} if content_type is None else {"Content-Type": content_type}
    return client.request("POST", "/v1/echo", body=body, token=VALID_TOKEN, headers=headers)


@pytest.fixture
def echo(authed):
    """A POST route that reflects its parsed body, so the parsing bounds are
    observable without waiting for 2.3."""
    cs._ROUTES[("POST", "/v1/echo")] = cs.Route(
        handler=lambda service, request: (200, {"body": request.body})
    )
    try:
        yield authed
    finally:
        del cs._ROUTES[("POST", "/v1/echo")]


def test_valid_json_object_is_accepted(echo):
    status, payload, _ = _post(echo, b'{"text": "hello"}')
    assert (status, payload) == (200, {"body": {"text": "hello"}})


def test_empty_body_parses_as_an_empty_object(echo):
    status, payload, _ = _post(echo, b"")
    assert (status, payload) == (200, {"body": {}})


def test_oversized_body_is_refused_unread(tmp_path):
    for probe in _serve(_config(tmp_path, max_body_bytes=64), _accept_test_token):
        cs._ROUTES[("POST", "/v1/echo")] = cs.Route(
            handler=lambda service, request: (200, {"body": request.body})
        )
        try:
            status, payload, headers = probe.request(
                "POST",
                "/v1/echo",
                body=json.dumps({"text": "x" * 200}).encode(),
                token=VALID_TOKEN,
                headers={"Content-Type": "application/json"},
            )
            assert status == 413
            assert payload["error"]["code"] == "payload_too_large"
            assert "64" in payload["error"]["message"]
            # Refused unread, so the connection cannot be reused.
            assert headers.get("Connection") == "close"
            # ...and the service is still there for the next caller.
            assert probe.request("GET", "/healthz")[0] == 200
        finally:
            del cs._ROUTES[("POST", "/v1/echo")]
        break


def test_a_lying_content_length_cannot_smuggle_a_large_body(echo):
    """The limit is enforced on the declared length, before any read."""
    status, payload = echo.raw(
        [
            "POST /v1/echo HTTP/1.1",
            f"Host: {cs.LOOPBACK}",
            f"Authorization: Bearer {VALID_TOKEN}",
            "Content-Type: application/json",
            f"Content-Length: {echo.handle.service.config.max_body_bytes + 1}",
        ],
        payload=b'{"text": "short"}',
    )
    assert status == 413
    assert payload["error"]["code"] == "payload_too_large"


def test_missing_content_length_is_refused(echo):
    status, payload = echo.raw(
        [
            "POST /v1/echo HTTP/1.1",
            f"Host: {cs.LOOPBACK}",
            f"Authorization: Bearer {VALID_TOKEN}",
            "Content-Type: application/json",
        ]
    )
    assert status == 411
    assert payload["error"]["code"] == "length_required"


def test_chunked_body_is_refused(echo):
    status, payload = echo.raw(
        [
            "POST /v1/echo HTTP/1.1",
            f"Host: {cs.LOOPBACK}",
            f"Authorization: Bearer {VALID_TOKEN}",
            "Content-Type: application/json",
            "Transfer-Encoding: chunked",
        ],
        payload=b"5\r\n{\"a\"}\r\n0\r\n\r\n",
    )
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"


def test_non_integer_content_length_is_refused(echo):
    status, payload = echo.raw(
        [
            "POST /v1/echo HTTP/1.1",
            f"Host: {cs.LOOPBACK}",
            f"Authorization: Bearer {VALID_TOKEN}",
            "Content-Type: application/json",
            "Content-Length: seven",
        ]
    )
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "content_type",
    [None, "text/plain", "application/x-www-form-urlencoded", "application/jsonx"],
)
def test_wrong_content_type_is_refused(echo, content_type):
    status, payload, _ = _post(echo, b'{"text": "hello"}', content_type)
    assert status == 415
    assert payload["error"]["code"] == "unsupported_media_type"


def test_json_content_type_parameters_are_tolerated(echo):
    status, payload, _ = _post(echo, b'{"text": "hi"}', "application/json; charset=utf-8")
    assert (status, payload) == (200, {"body": {"text": "hi"}})


@pytest.mark.parametrize("body", [b"{", b"not json", b'{"a": }', b"\xff\xfe"])
def test_malformed_json_is_refused(echo, body):
    status, payload, _ = _post(echo, body)
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("body", [b"[]", b'"text"', b"12", b"null", b"true"])
def test_a_json_body_that_is_not_an_object_is_refused(echo, body):
    status, payload, _ = _post(echo, body)
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"
    assert "object" in payload["error"]["message"]


def test_malformed_json_is_not_echoed_back(echo):
    secret = "sk-live-should-never-appear"
    status, payload, _ = _post(echo, b'{"leak": "' + secret.encode() + b'"')
    assert status == 400
    assert secret not in json.dumps(payload)


def test_a_get_with_a_body_is_refused(authed):
    status, payload = authed.raw(
        [
            "GET /v1/context HTTP/1.1",
            f"Host: {cs.LOOPBACK}",
            f"Authorization: Bearer {VALID_TOKEN}",
            "Content-Type: application/json",
            "Content-Length: 2",
        ],
        payload=b"{}",
    )
    assert status == 400
    assert payload["error"]["code"] == "invalid_request"
    assert "body" in payload["error"]["message"]


# --- error envelope ---


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/v1/context", 401),
        ("GET", "/", 401),
        ("POST", "/healthz", 405),
        ("DELETE", "/healthz", 405),
        ("PUT", "/v1/notes", 405),
    ],
)
def test_every_refusal_uses_the_same_envelope(client, method, path, expected):
    status, payload, headers = client.request(method, path)
    assert status == expected
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "status"}
    assert payload["error"]["status"] == status
    assert isinstance(payload["error"]["message"], str) and payload["error"]["message"]
    assert headers["Content-Type"] == "application/json"


def test_codes_and_statuses_agree(client):
    for code, status in cs.ERROR_STATUS.items():
        assert cs.ServiceError(code, "x").status == status
        assert cs.ServiceError(code, "x").envelope()["error"] == {
            "code": code,
            "message": "x",
            "status": status,
        }


def test_a_bad_request_line_still_answers_json(client):
    status, payload = client.raw(["GARBAGE"])
    assert status >= 400
    assert payload["error"]["code"] in cs.ERROR_STATUS
    assert payload["error"]["status"] == status


def test_a_handler_crash_does_not_leak_internals(authed):
    def boom(service, request):
        raise RuntimeError("/private/path/secret-token-abc123 exploded")

    cs._ROUTES[("GET", "/v1/boom")] = cs.Route(handler=boom)
    try:
        status, payload, _ = authed.request("GET", "/v1/boom", token=VALID_TOKEN)
    finally:
        del cs._ROUTES[("GET", "/v1/boom")]
    assert status == 500
    assert payload["error"]["code"] == "internal_error"
    assert "secret-token" not in json.dumps(payload)
    assert "/private/path" not in json.dumps(payload)
    assert authed.request("GET", "/healthz")[0] == 200


# --- redacted logging ---


@pytest.mark.parametrize(
    ("raw", "leak"),
    [
        ("Authorization: Bearer sekrit-abc123", "sekrit-abc123"),
        ("authorization=sekrit-abc123", "sekrit-abc123"),
        ("GET /v1/notes?token=sekrit-abc123", "sekrit-abc123"),
        ('{"token": "sekrit-abc123"}', "sekrit-abc123"),
        ("token=sekrit-abc123&limit=5", "sekrit-abc123"),
        ("api_key: sekrit-abc123", "sekrit-abc123"),
        ("password=sekrit-abc123", "sekrit-abc123"),
        ("tokens: sekrit-abc123", "sekrit-abc123"),
    ],
)
def test_redact_removes_credential_shaped_material(raw, leak):
    scrubbed = cs.redact(raw)
    assert leak not in scrubbed
    assert "<redacted>" in scrubbed


def test_redact_keeps_ordinary_text():
    line = "GET /v1/notes -> 200 - 1.4ms"
    assert cs.redact(line) == line


def test_request_logs_never_carry_the_token(authed):
    authed.request(
        "POST",
        "/v1/nope",
        token="sekrit-abc123",
        body=b'{"text": "hello", "token": "sekrit-abc123"}',
        headers={"Content-Type": "application/json"},
    )
    joined = authed.wait_for_log("/v1/nope")
    assert "sekrit-abc123" not in joined
    assert "hello" not in joined  # bodies are never logged either


def test_the_log_file_is_written_through_the_redacting_formatter(tmp_path):
    config = _config(tmp_path)
    cs.configure_logging(config)
    try:
        cs.get_logger().info("Authorization: Bearer sekrit-abc123")
    finally:
        for handler in list(cs.get_logger().handlers):
            if getattr(handler, "_amux_context", False):
                cs.get_logger().removeHandler(handler)
                handler.close()
    written = config.log_file.read_text()
    assert "sekrit-abc123" not in written
    assert "<redacted>" in written


def test_the_log_lands_under_the_state_directory(tmp_path):
    config = _config(tmp_path)
    assert config.log_file == tmp_path / "state" / cs.LOG_NAME


def test_the_state_directory_is_resolved_late_not_captured(monkeypatch, tmp_path):
    """A dataclass default would freeze the real state directory at import time,
    and 2.5 writes a PID file and a log into it."""
    from amux import shared

    monkeypatch.setattr(shared, "STATE_DIR", tmp_path / "redirected")
    config = cs.ServiceConfig()
    assert config.state_home == tmp_path / "redirected"
    assert config.log_file == tmp_path / "redirected" / cs.LOG_NAME
    monkeypatch.setattr(shared, "STATE_DIR", tmp_path / "moved")
    assert cs.ServiceConfig().state_home == tmp_path / "moved"


def test_the_service_log_does_not_propagate_to_the_root_logger(tmp_path):
    cs.configure_logging(_config(tmp_path))
    assert cs.get_logger().propagate is False


# --- configuration ---


def test_defaults_are_the_documented_ones():
    config = cs.ServiceConfig()
    assert config.port == cs.DEFAULT_PORT
    assert config.address == (cs.LOOPBACK, cs.DEFAULT_PORT)
    assert config.database == store.DB_PATH
    assert config.socket == "amux-root"


def test_from_env_reads_port_db_and_socket(tmp_path):
    config = cs.ServiceConfig.from_env(
        {
            cs.ENV_PORT: "51000",
            cs.ENV_DB: str(tmp_path / "other.db"),
            cs.ENV_SOCKET: "amux-alt",
        }
    )
    assert config.port == 51000
    assert config.database == tmp_path / "other.db"
    assert config.socket == "amux-alt"


def test_from_env_falls_back_to_defaults():
    config = cs.ServiceConfig.from_env({})
    assert config.port == cs.DEFAULT_PORT
    assert config.database == store.DB_PATH


def test_from_env_overrides_win(tmp_path):
    config = cs.ServiceConfig.from_env({cs.ENV_PORT: "51000"}, port=51001)
    assert config.port == 51001


@pytest.mark.parametrize("raw", ["nope", "51000.5", "", " "])
def test_a_bad_port_in_the_environment_is_actionable(raw):
    if not raw.strip():
        assert cs.ServiceConfig.from_env({cs.ENV_PORT: raw}).port == cs.DEFAULT_PORT
        return
    with pytest.raises(cs.ConfigError) as caught:
        cs.ServiceConfig.from_env({cs.ENV_PORT: raw})
    assert cs.ENV_PORT in str(caught.value)


@pytest.mark.parametrize("port", [-1, 65536, 99999])
def test_an_out_of_range_port_is_refused(port):
    with pytest.raises(cs.ConfigError):
        cs.ServiceConfig(port=port)


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_body_bytes": 0},
        {"max_results": 0},
        {"default_results": 0},
        {"max_text_chars": -1},
        {"max_wait_s": 0},
        {"default_results": 500, "max_results": 100},
    ],
)
def test_incoherent_limits_are_refused(overrides):
    with pytest.raises(cs.ConfigError):
        cs.ServiceConfig(**overrides)


def test_limits_are_declared_in_one_place():
    config = cs.ServiceConfig()
    assert config.max_body_bytes == 64 * 1024
    assert config.max_wait_s <= 60.0
    assert config.default_results <= config.max_results


# --- the interface stays small ---


def test_only_healthz_is_public():
    public = {path for (_, path), r in cs._ROUTES.items() if r.public}
    assert public == {"/healthz"}


def test_no_route_exposes_sql_shell_or_lifecycle():
    forbidden = ("sql", "query", "exec", "shell", "tmux", "spawn", "kill", "clean")
    for _, path in cs._ROUTES:
        assert not any(word in path.lower() for word in forbidden)
