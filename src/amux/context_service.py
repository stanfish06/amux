"""Host-side context service: the only path from a sandbox to amux context.

A sandboxed agent has no host tmux socket, no amux state directory, and no
`context.db`. It gets structured context by asking this service, which runs on
the host, owns the SQLite store, and answers a deliberately small versioned
JSON interface over loopback. Docker routes `host.docker.internal:<port>` here
only when the user has allowed `localhost:<port>` in sandbox policy; amux
checks that rule elsewhere and never widens it.

Everything here fails closed. The bind address is not configurable — a service
that can be reached from off-host is a different trust boundary, not a
configuration choice. Every request that is not `GET /healthz` must present a
capability token; routing happens *after* authentication so an unauthenticated
caller cannot map the interface. Bodies are bounded before they are read,
errors carry a stable code, and logs are redacted on the way out.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from amux import store
from amux.shared import DEFAULT_SOCKET, STATE_DIR

SERVICE_NAME = "amux-context"
API_VERSION = "v1"

# Not a setting. See the module docstring.
LOOPBACK = "127.0.0.1"

DEFAULT_PORT = 47317
LOG_NAME = "context-service.log"
LOGGER_NAME = "amux.context"

ENV_PORT = "AMUX_CONTEXT_PORT"
ENV_DB = "AMUX_CONTEXT_DB"
ENV_SOCKET = "AMUX_CONTEXT_SOCKET"

# `secrets.token_urlsafe(32)` is 43 characters; the cap only exists so an
# unauthenticated caller cannot make us hash arbitrary amounts of input.
MAX_TOKEN_CHARS = 512


class ConfigError(ValueError):
    """Bad service configuration — actionable, printed, never a traceback."""


class ServiceStartupError(RuntimeError):
    """The listener could not be opened. Never a reason to open a weaker one."""


# --- error envelope ---

# code -> HTTP status. The code is the stable half of the contract: clients
# branch on it, so statuses may gain nuance but a code never changes meaning.
ERROR_STATUS: dict[str, int] = {
    "invalid_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "length_required": 411,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "internal_error": 500,
    "service_unavailable": 503,
}

_CODE_BY_STATUS = {status: code for code, status in ERROR_STATUS.items()}


def _code_for_status(status: int) -> str:
    """A code for a status http.server raised on its own (414, 431, 501, 505)."""
    return _CODE_BY_STATUS.get(
        status, "invalid_request" if status < 500 else "internal_error"
    )


class ServiceError(Exception):
    """A refusal the client is allowed to see. Messages say what to fix and
    never quote request material back, which would echo secrets into logs."""

    def __init__(self, code: str, message: str, *, close: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = ERROR_STATUS.get(code, 500)
        # Set when we refuse a body we did not drain: the connection is out of
        # step with the client and cannot be reused for a second request.
        self.close = close

    def envelope(self) -> dict[str, Any]:
        return {
            "error": {"code": self.code, "message": self.message, "status": self.status}
        }


# --- redaction ---

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b(authorization)(\s*[:=]\s*)\S+"), r"\1\2<redacted>"),
    (
        re.compile(
            r"(?i)\b(tokens?|secret|password|api[_-]?key)"
            r"(\"?\s*[:=]\s*\"?)([^\s\"',&}]+)"
        ),
        r"\1\2<redacted>",
    ),
)


def redact(text: str) -> str:
    """Strip credential-shaped material from a string bound for a log.

    Belt and braces: no code path here logs a body or an Authorization header,
    so this exists to keep the next one honest too.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def configure_logging(
    config: ServiceConfig,
    *,
    stream: Any | None = None,
    level: int = logging.INFO,
) -> logging.Logger:
    """Point the service log at its file (and optionally a stream) through the
    redacting formatter. `propagate` stays off: a root handler would re-emit
    the same records unredacted."""
    log = get_logger()
    log.setLevel(level)
    log.propagate = False
    for handler in list(log.handlers):
        if getattr(handler, "_amux_context", False):
            log.removeHandler(handler)
            handler.close()
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(config.log_file)]
    if stream is not None:
        handlers.append(logging.StreamHandler(stream))
    for handler in handlers:
        handler.setFormatter(
            RedactingFormatter("%(asctime)s %(levelname)s %(message)s")
        )
        handler._amux_context = True  # type: ignore[attr-defined]
        log.addHandler(handler)
    return log


# --- configuration ---


@dataclass(frozen=True)
class ServiceConfig:
    """Ports, paths, and every bound the interface promises.

    The limits are part of the contract, not tuning knobs: clients are told a
    request may be refused for exceeding them, so they live in one place and
    are reported the same way each time.
    """

    port: int = DEFAULT_PORT
    db_path: Path | None = None  # None -> store.DB_PATH
    socket: str = DEFAULT_SOCKET  # tmux server whose pane options we update
    state_dir: Path = STATE_DIR
    log_path: Path | None = None  # None -> state_dir/LOG_NAME
    max_body_bytes: int = 64 * 1024
    max_text_chars: int = 4000
    max_detail_chars: int = 2000
    max_results: int = 200
    default_results: int = 10
    max_wait_s: float = 60.0
    request_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        # 0 binds an ephemeral port. Real runs want a stable one the sandbox
        # policy names, but tests need to bind without picking a fight.
        if not 0 <= self.port <= 65535:
            raise ConfigError(f"port must be between 0 and 65535, got {self.port}")
        for name in (
            "max_body_bytes",
            "max_text_chars",
            "max_detail_chars",
            "max_results",
            "default_results",
        ):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be positive, got {getattr(self, name)}")
        if self.default_results > self.max_results:
            raise ConfigError(
                f"default_results ({self.default_results}) exceeds max_results"
                f" ({self.max_results})"
            )
        if self.max_wait_s <= 0:
            raise ConfigError(f"max_wait_s must be positive, got {self.max_wait_s}")

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, **overrides: Any
    ) -> ServiceConfig:
        env = os.environ if env is None else env
        fields: dict[str, Any] = {}
        raw_port = (env.get(ENV_PORT) or "").strip()
        if raw_port:
            try:
                fields["port"] = int(raw_port)
            except ValueError:
                raise ConfigError(
                    f"{ENV_PORT} must be an integer port, got '{raw_port}'"
                ) from None
        if env.get(ENV_DB):
            fields["db_path"] = Path(env[ENV_DB]).expanduser()
        if env.get(ENV_SOCKET):
            fields["socket"] = env[ENV_SOCKET]
        return cls(**{**fields, **overrides})

    @property
    def address(self) -> tuple[str, int]:
        return (LOOPBACK, self.port)

    @property
    def database(self) -> Path:
        return self.db_path or store.DB_PATH

    @property
    def log_file(self) -> Path:
        """Redacted service log, under the amux state directory by default."""
        return self.log_path or self.state_dir / LOG_NAME


# --- identity ---


@dataclass(frozen=True)
class Identity:
    """Who the *host* says a caller is.

    Derived from the token record and the execution row it names — never from
    the request. A body field called `agent` or `workspace` is data, not
    identity, and the handlers must not read it as one.
    """

    worktree_id: int
    pane: str = ""
    workspace: str = ""
    task: str = ""
    repo: str = ""
    agent: str = ""
    name: str = ""
    runtime: str = "docker-sandbox"
    socket: str = DEFAULT_SOCKET
    permissions: frozenset[str] = frozenset()


# `authenticator(service, token) -> Identity`, raising ServiceError to refuse.
Authenticator = Callable[["ContextService", str], Identity]


def reject_all_tokens(service: ContextService, token: str) -> Identity:
    """The default authenticator: nothing is a valid token.

    Until the capability store is wired in, a service with no authenticator is
    a service with no callers — which is the correct failure direction.
    """
    raise ServiceError("unauthorized", "invalid or expired capability token")


# --- schema compatibility ---


@dataclass(frozen=True)
class SchemaInfo:
    version: int | None  # None: the store could not be opened
    expected: int

    @property
    def compatible(self) -> bool:
        return self.version == self.expected

    @property
    def status(self) -> str:
        if self.version is None:
            return "unavailable"
        return "ok" if self.compatible else "degraded"


def schema_info(db_path: Path) -> SchemaInfo:
    """The store's schema version, after letting `store` migrate it forward.

    Going through `store._connect` rather than reading the pragma directly
    matters: an unmigrated database is one native amux call away from being
    current, and reporting it as incompatible would be a false alarm.
    """
    try:
        conn = store._connect(db_path)
    except sqlite3.Error:
        return SchemaInfo(version=None, expected=store.SCHEMA_VERSION)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        return SchemaInfo(version=None, expected=store.SCHEMA_VERSION)
    finally:
        conn.close()
    return SchemaInfo(version=version, expected=store.SCHEMA_VERSION)


# --- requests and routing ---


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    authorization: str = ""
    identity: Identity | None = None


Handler = Callable[["ContextService", Request], "tuple[int, dict[str, Any]]"]


@dataclass(frozen=True)
class Route:
    handler: Handler
    public: bool = False


_ROUTES: dict[tuple[str, str], Route] = {}


def route(method: str, path: str, *, public: bool = False) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        _ROUTES[(method, path)] = Route(handler=handler, public=public)
        return handler

    return register


def _normalize_path(path: str) -> str:
    """Trailing-slash tolerance, and nothing else. Routing is exact-match, so
    an encoded or dot-segmented path simply does not resolve."""
    return path.rstrip("/") or "/"


def _token_from_authorization(raw: str) -> str:
    scheme, _, token = raw.strip().partition(" ")
    if not raw.strip():
        raise ServiceError(
            "unauthorized", "missing 'Authorization: Bearer <token>' header"
        )
    if scheme.lower() != "bearer" or not token.strip():
        raise ServiceError(
            "unauthorized", "expected 'Authorization: Bearer <token>'"
        )
    token = token.strip()
    if len(token) > MAX_TOKEN_CHARS:
        raise ServiceError("unauthorized", "capability token is too long")
    return token


# --- the service ---


class ContextService:
    """Policy: authentication, routing, and the store the handlers read.

    Holds no per-request state, so `ThreadingHTTPServer` can call `handle`
    from several threads at once.
    """

    def __init__(
        self,
        config: ServiceConfig | None = None,
        *,
        authenticator: Authenticator | None = None,
    ) -> None:
        self.config = config or ServiceConfig()
        self.authenticator: Authenticator = authenticator or reject_all_tokens
        self.log = get_logger()

    @property
    def db_path(self) -> Path:
        return self.config.database

    def schema_info(self) -> SchemaInfo:
        return schema_info(self.db_path)

    def handle(
        self, request: Request, read_body: Callable[[], dict[str, Any]] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """Authenticate, route, *then* read the body.

        That order is the point: an unauthenticated caller, or one asking for an
        operation that does not exist, is refused before we read a single byte
        it sent.
        """
        route = self.resolve(request)
        if read_body is not None:
            request.body = read_body()
        return route.handler(self, request)

    def resolve(self, request: Request) -> Route:
        methods = {m for (m, p) in _ROUTES if p == request.path}
        public = any(r.public for (_, p), r in _ROUTES.items() if p == request.path)
        if not public:
            # Before routing: a 404 handed to an unauthenticated caller would
            # tell it which operations exist.
            request.identity = self.authenticate(request)
        matched = _ROUTES.get((request.method, request.path))
        if matched is not None:
            return matched
        if methods:
            raise ServiceError(
                "method_not_allowed",
                f"{request.method} is not allowed on {request.path};"
                f" allowed: {', '.join(sorted(methods))}",
            )
        raise ServiceError(
            "not_found", f"unknown operation {request.method} {request.path}"
        )

    def authenticate(self, request: Request) -> Identity:
        return self.authenticator(self, _token_from_authorization(request.authorization))


@route("GET", "/healthz", public=True)
def _healthz(service: ContextService, request: Request) -> tuple[int, dict[str, Any]]:
    """Liveness plus schema compatibility, and nothing else: this is the one
    endpoint an unauthenticated caller can reach, so it names no path, port,
    workspace, or agent."""
    info = service.schema_info()
    payload = {
        # `ok` and `schema_version` are what the sandbox client reads; the rest
        # is for a human running `curl` or reading a preflight failure.
        "ok": info.compatible,
        "service": SERVICE_NAME,
        "api": API_VERSION,
        "status": info.status,
        "schema_version": info.version,
        "expected_schema_version": info.expected,
        "compatible": info.compatible,
    }
    return (200 if info.compatible else 503, payload)


# --- HTTP plumbing ---


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{SERVICE_NAME}/1"
    sys_version = ""

    @property
    def service(self) -> ContextService:
        return self.server.service  # type: ignore[attr-defined]

    # -- responses --

    def _write(
        self, status: int, payload: dict[str, Any], *, close: bool = False
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # No CORS headers, ever: a page in a host browser must not be able to
        # use this service, and `application/json` keeps it behind a preflight.
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _refuse(self, error: ServiceError) -> None:
        """Send a refusal, tolerating a client that has already hung up: a
        broken pipe while apologising is not a service fault."""
        try:
            self._write(error.status, error.envelope(), close=error.close)
        except OSError:
            self.close_connection = True

    def send_error(  # type: ignore[override]
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        """Keep http.server's own refusals (bad request line, 414, 501, 505) on
        the same envelope instead of its HTML page."""
        self.close_connection = True
        if self.request_version == "HTTP/0.9":
            # An unparseable request line leaves the version at 0.9, and
            # `send_response_only` then suppresses the status line and headers —
            # a bare JSON body no client can read. Answer as 1.1 instead.
            self.request_version = self.protocol_version
        error = ServiceError(_code_for_status(code), message or "request refused")
        payload = error.envelope()
        payload["error"]["status"] = code
        try:
            self._write(code, payload, close=True)
        except OSError:
            pass

    # -- request parsing --

    def _read_body(self) -> dict[str, Any]:
        """Parse a bounded JSON object, refusing before reading anything we are
        not willing to hold in memory."""
        if self.headers.get("Transfer-Encoding"):
            raise ServiceError(
                "invalid_request",
                "chunked or encoded request bodies are not supported;"
                " send a Content-Length",
                close=True,
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ServiceError(
                "length_required", "request body requires a Content-Length header"
            )
        try:
            length = int(raw_length)
        except ValueError:
            raise ServiceError(
                "invalid_request", "Content-Length is not an integer", close=True
            ) from None
        if length < 0:
            raise ServiceError(
                "invalid_request", "Content-Length is negative", close=True
            )
        limit = self.service.config.max_body_bytes
        if length > limit:
            # Refused unread, so the socket still holds the body: this
            # connection cannot be reused.
            raise ServiceError(
                "payload_too_large",
                f"request body of {length} bytes exceeds the {limit} byte limit",
                close=True,
            )
        media_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if media_type != "application/json":
            raise ServiceError(
                "unsupported_media_type",
                "Content-Type must be application/json",
                close=True,
            )
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ServiceError(
                "invalid_request", "request body ended early", close=True
            )
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError:
            raise ServiceError(
                "invalid_request", "request body is not valid UTF-8"
            ) from None
        except json.JSONDecodeError as exc:
            # Position only. Quoting the body back would put whatever the
            # caller sent into an error a log might keep.
            raise ServiceError(
                "invalid_request", f"request body is not valid JSON: {exc.msg}"
            ) from None
        if not isinstance(parsed, dict):
            raise ServiceError("invalid_request", "request body must be a JSON object")
        return parsed

    def _refuse_body(self, method: str) -> dict[str, Any]:
        length = (self.headers.get("Content-Length") or "").strip()
        if self.headers.get("Transfer-Encoding") or (length and length != "0"):
            raise ServiceError(
                "invalid_request",
                f"{method} does not accept a request body",
                close=True,
            )
        return {}

    # -- dispatch --

    def _serve(self, method: str) -> None:
        started = time.monotonic()
        path, status, code = "-", 500, "internal_error"
        drained = method != "POST"

        def read_body() -> dict[str, Any]:
            nonlocal drained
            body = self._read_body()
            drained = True
            return body

        try:
            target = urlsplit(self.path)
            path = _normalize_path(target.path)
            if method != "POST":
                self._refuse_body(method)
            request = Request(
                method=method,
                path=path,
                query=parse_qs(target.query),
                authorization=self.headers.get("Authorization", ""),
            )
            status, payload = self.service.handle(
                request, read_body=read_body if method == "POST" else None
            )
            code = "-"
            self._write(status, payload)
        except ServiceError as exc:
            status, code = exc.status, exc.code
            # A body we never read leaves the socket mid-request, whether we
            # refused it for its size or refused the caller before reading.
            exc.close = exc.close or not drained
            self._refuse(exc)
        except Exception:
            self.service.log.exception("unhandled error serving %s %s", method, path)
            error = ServiceError("internal_error", "internal service error", close=True)
            status, code = error.status, error.code
            self._refuse(error)
        finally:
            self.service.log.info(
                "%s %s -> %s %s %.1fms",
                method,
                redact(path),
                status,
                code,
                (time.monotonic() - started) * 1000,
            )

    def do_GET(self) -> None:
        self._serve("GET")

    def do_POST(self) -> None:
        self._serve("POST")

    def _unsupported_method(self) -> None:
        self.send_error(405, "method is not supported by this service")

    do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _unsupported_method

    # -- logging --

    def log_request(self, *args: Any, **kwargs: Any) -> None:
        pass  # _serve writes the access line, with a status and a duration.

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        self.service.log.debug(redact(format % args))

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        self.service.log.warning(redact(format % args))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True  # TIME_WAIT only; a live listener still collides

    def __init__(
        self, address: tuple[str, int], handler: type[_Handler], service: ContextService
    ) -> None:
        self.service = service
        self.timeout = service.config.request_timeout_s
        super().__init__(address, handler)

    def finish_request(self, request: Any, client_address: Any) -> None:
        # A client that opens a connection and stalls must not hold a thread.
        request.settimeout(self.service.config.request_timeout_s)
        super().finish_request(request, client_address)

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver's default prints a traceback to stderr, which for a
        # daemonised service means an unredacted line nobody reads.
        self.service.log.exception("connection from %s failed", client_address)


def build_server(service: ContextService) -> ThreadingHTTPServer:
    """Bind the loopback listener. A busy port is a hard failure: there is no
    second, weaker listener to fall back to."""
    try:
        return _Server(service.config.address, _Handler, service)
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            raise ServiceStartupError(
                f"cannot bind {LOOPBACK}:{service.config.port}: {exc.strerror}."
                f" Stop the process holding it or choose another port"
                f" (--port / {ENV_PORT})"
            ) from exc
        raise


@dataclass
class ServiceHandle:
    """A running service. `port` is the bound one, which matters when the
    configured port was 0."""

    service: ContextService
    server: ThreadingHTTPServer
    thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def stop(self, timeout: float = 5.0) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout)


def start_service(
    config: ServiceConfig | None = None,
    *,
    authenticator: Authenticator | None = None,
    log_stream: Any | None = None,
) -> ServiceHandle:
    """Start the service on a background thread and return a handle to it."""
    service = ContextService(config, authenticator=authenticator)
    configure_logging(service.config, stream=log_stream)
    server = build_server(service)
    service.config = replace(service.config, port=int(server.server_address[1]))
    thread = threading.Thread(
        target=server.serve_forever, name=SERVICE_NAME, daemon=True
    )
    thread.start()
    service.log.info(
        "%s listening on %s:%d (schema %s)",
        SERVICE_NAME,
        LOOPBACK,
        service.config.port,
        service.schema_info().version,
    )
    return ServiceHandle(service=service, server=server, thread=thread)
