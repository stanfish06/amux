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

Identity is the other half. A caller presents a token and nothing else: the
workspace, task, repository, pane, agent and permissions all come from the
execution row that token is bound to, so a request cannot name who it is. The
handlers reuse the native `store`, `core` and `events` functions, which is what
makes a sandboxed agent's answers the same as an equivalently scoped host
agent's rather than merely similar.

At the bottom: `serve_foreground` runs the service, and `start`/`status`/`stop`
manage one per state directory through a run file recording its pid and bound
port. Every one of them refuses rather than degrades — there is no
unauthenticated listener and no mounted database to fall back to.
"""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from amux import core, events, shared, store
from amux.shared import DEFAULT_SOCKET

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
    state_dir: Path | None = None  # None -> shared.STATE_DIR, resolved late
    log_path: Path | None = None  # None -> state_home/LOG_NAME
    max_body_bytes: int = 64 * 1024
    max_text_chars: int = 4000
    max_detail_chars: int = 2000
    max_results: int = 200
    default_results: int = 10
    max_wait_s: float = 60.0
    # How often a long poll re-queries SQLite. In-process writes wake it
    # immediately; this is what makes a *native* host write visible too.
    poll_interval_s: float = 0.25
    # How often the accept loop checks whether it has been asked to stop, which
    # is therefore also how long `shutdown()` can block. A daemon should not
    # wake hundreds of times a second to poll a flag it will read once, so this
    # stays coarse; a test that starts and stops a server per case sets it low.
    # Not the same knob as `poll_interval_s` — conflating them would change how
    # long a caller's wait can miss a native write.
    shutdown_poll_s: float = 0.5
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
        if self.shutdown_poll_s <= 0:
            raise ConfigError(
                f"shutdown_poll_s must be positive, got {self.shutdown_poll_s}"
            )
        if not 0 < self.poll_interval_s <= self.max_wait_s:
            raise ConfigError(
                f"poll_interval_s must be positive and no larger than max_wait_s,"
                f" got {self.poll_interval_s}"
            )

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
    def state_home(self) -> Path:
        """The amux state directory, resolved on use rather than captured.

        A dataclass default is evaluated once, at class-definition time, so a
        field defaulting to `shared.STATE_DIR` would keep pointing at the real
        one even after a test redirected it — and this is where 2.5 puts the
        PID file and the log.
        """
        return self.state_dir or shared.STATE_DIR

    @property
    def log_file(self) -> Path:
        """Redacted service log, under the amux state directory by default."""
        return self.log_path or self.state_home / LOG_NAME


# --- identity and capabilities ---

# What a capability may do. A token carries a subset; a route names the one it
# needs. Read is not implied by write: a hook-only capability that may post
# events has no business reading the roster.
PERM_CONTEXT_READ = "context:read"
PERM_NOTES_WRITE = "notes:write"
PERM_EVENTS_WRITE = "events:write"

# What amux mints for a sandboxed agent. Every agent token carries all three,
# so this is *not* least privilege in practice, and documentation must not
# claim it is. Two things are true instead: host control — spawn, kill, clean,
# integrate, monitor — is not expressible in this vocabulary at all, so no
# token can be escalated into it; and `requires=` on each route is the seam
# through which a narrower capability can be minted later without changing any
# handler.
AGENT_PERMISSIONS = (PERM_CONTEXT_READ, PERM_NOTES_WRITE, PERM_EVENTS_WRITE)


@dataclass(frozen=True)
class Identity:
    """Who the *host* says a caller is.

    Every field is read from the capability's execution row, never from the
    request. A body field called `agent` or `workspace` is data, not identity,
    and no handler may read it as one. These fields are also the whole of the
    caller's visibility: workspace, task, pane, and repo are exactly what the
    native note rules filter on.
    """

    worktree_id: int
    token_id: int = 0
    pane: str = ""
    workspace: str = ""
    task: str = ""
    repo: str = ""
    agent: str = ""
    name: str = ""
    branch: str = ""
    runtime: str = "docker-sandbox"
    runtime_status: str = ""
    sandbox_name: str = ""
    sandbox_id: str = ""
    status: str = "active"
    socket: str = DEFAULT_SOCKET
    permissions: frozenset[str] = frozenset()

    @property
    def scope(self) -> str:
        """`workspace/task`, for a refusal an agent has to act on."""
        return f"{self.workspace}/{self.task}"


def identity_from_record(
    record: Mapping[str, Any], default_socket: str = DEFAULT_SOCKET
) -> Identity:
    """Build an `Identity` from `store.context_token_record`'s row."""
    return Identity(
        worktree_id=int(record["worktree_id"]),
        token_id=int(record["id"]),
        pane=record["pane"] or "",
        workspace=record["workspace"] or "",
        task=record["task"] or "",
        repo=record["repo"] or "",
        agent=record["agent"] or "",
        name=record["name"] or "",
        branch=record["branch"] or "",
        runtime=record["runtime"] or "host",
        runtime_status=record["runtime_status"] or "",
        sandbox_name=record["sandbox_name"] or "",
        sandbox_id=record["sandbox_id"] or "",
        status=record["status"] or "active",
        # A row that names its tmux server wins. An empty value means exactly
        # one thing — a row migrated from schema 2, before the column existed —
        # since register_worktree now always records it. That reading is
        # unambiguous only while amux runs one tmux server per host, which holds
        # for the prototype.
        socket=record["socket_name"] or default_socket,
        permissions=frozenset(record["permissions"]),
    )


def _libtmux_server(socket: str) -> Any:
    import libtmux

    return libtmux.Server(socket_name=socket)


# `authenticator(service, token) -> Identity`, raising ServiceError to refuse.
Authenticator = Callable[["ContextService", str], Identity]

_UNAUTHORIZED = "invalid or expired capability token"


def store_authenticator(service: ContextService, token: str) -> Identity:
    """Resolve a presented token through the host store.

    `store.context_token_record` answers None for unknown, expired, and revoked
    alike and compares the hash in constant time, so this cannot be used to
    narrow a guess.
    """
    record = store.context_token_record(token, db_path=service.db_path)
    if record is None:
        raise ServiceError("unauthorized", _UNAUTHORIZED)
    if record["status"] == "removed":
        # Removal revokes tokens; reaching here means that failed, so refuse
        # anyway rather than serve a retired execution.
        raise ServiceError(
            "unauthorized", "this execution has been removed from the registry"
        )
    return identity_from_record(record, default_socket=service.config.socket)


def reject_all_tokens(service: ContextService, token: str) -> Identity:
    """An authenticator for a service that should have no callers at all."""
    raise ServiceError("unauthorized", _UNAUTHORIZED)


# --- scope ---


def _quote(value: str, limit: int = 64) -> str:
    """A caller-supplied value, made safe to put in a message.

    Refusals name what was asked for — "pane %99 is not in proj/fix" is
    actionable where "forbidden" is not — but the value came from the wire, so
    it is truncated and stripped of anything that could forge a log line.
    """
    cleaned = "".join(c for c in value[:limit] if c.isprintable() and c not in "\"'")
    return cleaned + ("…" if len(value) > limit else "")


def deny(what: str, value: str, identity: Identity) -> ServiceError:
    return ServiceError(
        "forbidden", f"{what} {_quote(value)} is not in {identity.scope}"
    )


def require_scope(
    identity: Identity,
    *,
    workspace: str | None = None,
    repo: str | None = None,
) -> None:
    """Refuse a request that names a workspace or repository other than the
    caller's. Task is deliberately absent: a sibling task in the same workspace
    is readable to exactly the extent the native note rules allow, so handlers
    pass the task through and let visibility decide."""
    if workspace is not None and workspace != identity.workspace:
        raise deny("workspace", workspace, identity)
    if repo is not None and repo != identity.repo:
        raise deny("repository", repo, identity)


def require_permission(identity: Identity, permission: str) -> None:
    if permission not in identity.permissions:
        raise ServiceError(
            "forbidden", f"this capability does not grant '{permission}'"
        )


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
    """The store's schema version, as `store` reports it after opening.

    The store and the service judge a version differently, on purpose. The
    store is a library and stays permissive: opening migrates a database this
    build is ahead of, and one written by a *newer* amux is left alone, because
    the migration is additive and an old binary can still read it. The service
    is a trust boundary and fails closed: it refuses to serve a version it does
    not expect rather than guess that the difference is additive.
    """
    try:
        version = store.schema_version(db_path)
    except sqlite3.Error:
        return SchemaInfo(version=None, expected=store.SCHEMA_VERSION)
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

    @property
    def caller(self) -> Identity:
        """The authenticated caller. Handlers use this and never `body`, for
        anything that decides attribution or scope."""
        if self.identity is None:
            raise ServiceError("unauthorized", _UNAUTHORIZED)
        return self.identity


Handler = Callable[["ContextService", Request], "tuple[int, dict[str, Any]]"]


@dataclass(frozen=True)
class Route:
    handler: Handler
    public: bool = False
    requires: str = ""  # capability this operation needs


_ROUTES: dict[tuple[str, str], Route] = {}


def route(
    method: str, path: str, *, public: bool = False, requires: str = ""
) -> Callable[[Handler], Handler]:
    def register(handler: Handler) -> Handler:
        _ROUTES[(method, path)] = Route(
            handler=handler, public=public, requires=requires
        )
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
        server_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config or ServiceConfig()
        self.authenticator: Authenticator = authenticator or store_authenticator
        self.server_factory = server_factory or _libtmux_server
        self.log = get_logger()
        self._servers: dict[str, Any] = {}
        self._servers_lock = threading.Lock()
        self._changed = threading.Condition()

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
            if matched.requires:
                require_permission(request.caller, matched.requires)
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

    def wake(self) -> None:
        """Release every waiter: something this service committed has changed."""
        with self._changed:
            self._changed.notify_all()

    def sleep(self, timeout: float) -> None:
        """Wait to be woken, or for `timeout` — whichever comes first."""
        with self._changed:
            self._changed.wait(timeout)

    def tmux_server(self, socket: str) -> Any:
        """The libtmux server a caller's pane lives on, cached per socket.

        A seam, so the read paths can be exercised without a tmux server. In
        production this is the same object the native commands build.
        """
        with self._servers_lock:
            server = self._servers.get(socket)
            if server is None:
                server = self.server_factory(socket)
                self._servers[socket] = server
            return server

    def build_context(self, identity: Identity) -> dict[str, Any]:
        """`core.build_context` for a sandboxed caller.

        Same function the native `amux ctx` calls, so a sandbox and an
        equivalently scoped host agent see the same roster and the same notes.
        Only the runtime fields are added on top, and only to `self`.
        """
        try:
            context = core.build_context(self.tmux_server(identity.socket), identity.pane)
        except ValueError as exc:
            # The pane the execution was attached to is gone — a killed task,
            # usually, with the sandbox still running. Say so instead of
            # returning a roster that quietly claims to be complete.
            raise ServiceError(
                "service_unavailable",
                f"the host pane for {identity.name or identity.agent} is no longer on"
                f" the tmux server, so its roster cannot be resolved",
            ) from exc
        context["self"] = {
            **context["self"],
            "runtime": identity.runtime,
            "runtime_status": identity.runtime_status,
            "sandbox_name": identity.sandbox_name,
            "sandbox_id": identity.sandbox_id,
        }
        for entry in [context["self"], *(a for t in context["team"] for a in t["agents"])]:
            # A sandbox row has no host worktree, and `git -C ""` silently runs
            # wherever the service happens to live — which would report the
            # host's own checkout as the agent's last commit. Task 5.4 makes
            # core runtime-aware; this boundary must not pass it on regardless.
            if entry.get("worktree") == "" and entry.get("last_commit"):
                entry["last_commit"] = ""
        return context


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


# --- bounded input ---


def _one(request: Request, name: str) -> str | None:
    """A single query value, or None. A repeated parameter is a refusal rather
    than a guess about which one the caller meant."""
    values = request.query.get(name)
    if not values:
        return None
    if len(values) > 1:
        raise ServiceError(
            "invalid_request", f"parameter '{name}' was given more than once"
        )
    return values[0]


def _int_param(
    request: Request, name: str, default: int, minimum: int, maximum: int
) -> int:
    raw = _one(request, name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ServiceError(
            "invalid_request", f"parameter '{name}' must be an integer"
        ) from None
    if not minimum <= value <= maximum:
        raise ServiceError(
            "invalid_request",
            f"parameter '{name}' must be between {minimum} and {maximum},"
            f" got {value}",
        )
    return value


def _cursor_param(request: Request, name: str) -> int | None:
    """A cursor, where absent and zero are different things: 0 means "from the
    beginning of my walk", which is not the same as "just give me a page"."""
    raw = _one(request, name)
    if raw is None or raw == "":
        return None
    return _int_param(request, name, 0, 0, 2**63 - 1)


def _float_param(
    request: Request, name: str, default: float, minimum: float, maximum: float
) -> float:
    raw = _one(request, name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ServiceError(
            "invalid_request", f"parameter '{name}' must be a number"
        ) from None
    if not minimum <= value <= maximum:
        raise ServiceError(
            "invalid_request",
            f"parameter '{name}' must be between {minimum:g} and {maximum:g},"
            f" got {value:g}",
        )
    return value


def _choice(request: Request, name: str, allowed: Sequence[str]) -> str | None:
    raw = _one(request, name)
    if raw is None or raw == "":
        return None
    if raw not in allowed:
        raise ServiceError(
            "invalid_request",
            f"parameter '{name}' must be one of {', '.join(allowed)}",
        )
    return raw


def _text_field(
    body: Mapping[str, Any], name: str, limit: int, *, required: bool = True
) -> str:
    """A bounded string from a JSON body, with the limit and the actual length
    in the refusal — the agent sees only this text, and truncating its note
    silently would be worse than refusing it."""
    value = body.get(name, "")
    if not isinstance(value, str):
        raise ServiceError("invalid_request", f"field '{name}' must be a string")
    if required and not value.strip():
        raise ServiceError("invalid_request", f"field '{name}' is required")
    if len(value) > limit:
        raise ServiceError(
            "invalid_request",
            f"field '{name}' is {len(value)} characters, over the {limit}"
            f" character limit",
        )
    return value


def _choice_field(
    body: Mapping[str, Any], name: str, allowed: Sequence[str], default: str
) -> str:
    value = body.get(name) or default
    if value not in allowed:
        raise ServiceError(
            "invalid_request",
            f"field '{name}' must be one of {', '.join(allowed)}",
        )
    return str(value)


def _cursor(rows: Sequence[Mapping[str, Any]], after: int | None) -> int | None:
    """The highest id the caller has now seen. Monotonic: an empty page keeps
    the cursor the caller came in with, so it never rewinds and never skips."""
    ids = [int(r["id"]) for r in rows]
    if not ids:
        return after
    return max(ids + ([after] if after is not None else []))


# --- context and notes ---


@route("GET", "/v1/context", requires=PERM_CONTEXT_READ)
def _context(service: ContextService, request: Request) -> tuple[int, dict[str, Any]]:
    return 200, service.build_context(request.caller)


@route("GET", "/v1/notes", requires=PERM_CONTEXT_READ)
def _notes(service: ContextService, request: Request) -> tuple[int, dict[str, Any]]:
    """Visible notes, by exactly the native rules.

    `task` may name a sibling task in the caller's workspace, as `amux notes
    --task` does; agent-scoped notes stay pinned to the caller's own pane on
    every route, so widening the task cannot widen what is private.
    """
    caller = request.caller
    require_scope(
        caller, workspace=_one(request, "workspace"), repo=_one(request, "repo")
    )
    limit = _int_param(
        request,
        "limit",
        service.config.default_results,
        1,
        service.config.max_results,
    )
    after = _cursor_param(request, "after")
    scope = _choice(request, "scope", store.NOTE_SCOPES)
    kind = _choice(request, "kind", store.NOTE_KINDS)
    task = _one(request, "task") or caller.task

    # `after` is a store-level cursor: `id > after` ascending, so a walk is
    # caught up page by page and cannot leave a hole behind the cursor. Without
    # one the query keeps its native newest-first shape.
    if scope is None:
        notes = store.visible_notes(
            workspace=caller.workspace,
            task=task,
            pane=caller.pane,
            kind=kind,
            repo=caller.repo,
            limit=limit,
            after=after,
            db_path=service.db_path,
        )
    else:
        notes = store.query_notes(
            workspace=caller.workspace,
            task=task,
            scope=scope,
            kind=kind,
            # Narrow to this pane, never widen: an agent-scoped query from
            # anyone else must not return this agent's private notes.
            pane=caller.pane if scope == "agent" else None,
            repo=caller.repo,
            limit=limit,
            after=after,
            db_path=service.db_path,
        )
    return 200, {"notes": notes, "cursor": _cursor(notes, after)}


@route("POST", "/v1/notes", requires=PERM_NOTES_WRITE)
def _add_note(service: ContextService, request: Request) -> tuple[int, dict[str, Any]]:
    """Publish a note as the caller.

    Scope here is a visibility level, not a target: workspace, task, pane and
    agent all come from the capability, so there is no scope outside the
    caller's own for a note to land in.
    """
    caller = request.caller
    text = _text_field(request.body, "text", service.config.max_text_chars)
    scope = _choice_field(request.body, "scope", store.NOTE_SCOPES, "task")
    kind = _choice_field(request.body, "kind", store.NOTE_KINDS, "note")
    ts = time.time()
    note_id = store.add_note(
        workspace=caller.workspace,
        task=caller.task,
        pane=caller.pane,
        text=text,
        scope=scope,
        kind=kind,
        agent=caller.agent,
        worktree_id=caller.worktree_id,
        repo=caller.repo,
        ts=ts,
        db_path=service.db_path,
    )
    # Read the row back rather than describing what we think we wrote. A
    # response assembled from the identity would report the caller's own pane
    # even if the insert had somehow filed the note under another one, which
    # makes the receipt evidence of nothing.
    row = _note_by_id(service, caller.worktree_id, note_id) or {
        "id": note_id,
        "ts": ts,
        "worktree_id": caller.worktree_id,
        "repo": caller.repo,
        "workspace": caller.workspace,
        "task": caller.task,
        "pane": caller.pane,
        "agent": caller.agent,
        "scope": scope,
        "kind": kind,
        "text": text,
    }
    # `name` is not a notes column — it is the execution's stable name, which
    # the client prints in its receipt line.
    return 200, {"note": {**row, "name": caller.name}}


def _note_by_id(
    service: ContextService, worktree_id: int, note_id: int
) -> dict[str, Any] | None:
    """The one note with this id, through the public cursor: ids ascend, so the
    first row after `note_id - 1` is it."""
    rows = store.query_notes(
        worktree_id=worktree_id,
        after=note_id - 1,
        limit=1,
        db_path=service.db_path,
    )
    if rows and int(rows[0]["id"]) == note_id:
        return rows[0]
    service.log.warning("note %d could not be read back after insert", note_id)
    return None


# --- events ---

EVENT_KINDS: tuple[str, ...] = tuple(events.STATE_BY_KIND)
AGENT_STATES: tuple[str, ...] = tuple(dict.fromkeys(events.STATE_BY_KIND.values()))

# What `events.wait` blocks for when a caller names nothing.
DEFAULT_WAIT_STATES: tuple[str, ...] = ("idle", "needs-input", "dead")


def _states_param(request: Request) -> tuple[str, ...]:
    raw = _one(request, "states")
    if raw is None or raw == "":
        return DEFAULT_WAIT_STATES
    wanted = tuple(s.strip() for s in raw.split(",") if s.strip())
    if not wanted or len(wanted) > len(AGENT_STATES):
        raise ServiceError(
            "invalid_request",
            f"parameter 'states' must be 1 to {len(AGENT_STATES)} comma-separated"
            f" values from {', '.join(AGENT_STATES)}",
        )
    unknown = [s for s in wanted if s not in AGENT_STATES]
    if unknown:
        raise ServiceError(
            "invalid_request",
            f"parameter 'states' must be from {', '.join(AGENT_STATES)},"
            f" got {_quote(unknown[0])}",
        )
    return wanted


def _pane_param(service: ContextService, request: Request) -> str:
    """The pane a wait is about: the caller's own unless it names another in
    its scope. A pane outside the scope is refused rather than watched."""
    caller = request.caller
    pane = _one(request, "pane")
    if pane is None or pane == "" or pane == caller.pane:
        return caller.pane
    if len(pane) > 32:
        raise ServiceError("invalid_request", "parameter 'pane' is too long")
    in_scope = {
        row["pane"]
        for row in store.worktrees_for(
            caller.workspace,
            task=caller.task,
            repo=caller.repo,
            db_path=service.db_path,
        )
    }
    if pane not in in_scope:
        raise deny("pane", pane, caller)
    return pane


@route("POST", "/v1/events", requires=PERM_EVENTS_WRITE)
def _add_event(service: ContextService, request: Request) -> tuple[int, dict[str, Any]]:
    """Record a state change for the caller and update its host pane.

    Attribution is the capability's execution row, passed explicitly rather
    than resolved from the pane. `events.emit` asks the store which worktree a
    pane fronts *now*, which is right for a hook running inside that pane and
    wrong here: tmux recycles `%N`, and a token outlives the coincidence.
    """
    caller = request.caller
    kind = _choice_field(request.body, "kind", EVENT_KINDS, "")
    detail = _text_field(
        request.body, "detail", service.config.max_detail_chars, required=False
    )
    ts = time.time()
    event_id = store.add_event(
        ts=ts,
        pane=caller.pane,
        kind=kind,
        workspace=caller.workspace,
        task=caller.task,
        agent=caller.agent,
        detail=detail,
        worktree_id=caller.worktree_id,
        repo=caller.repo,
        db_path=service.db_path,
    )
    state = events.STATE_BY_KIND[kind]  # type: ignore[index]
    # Only after the event is durable: a pane option that claims a state the
    # store cannot corroborate is worse than a late one.
    alive = events.pane_facts(caller.pane, caller.socket).alive
    if alive:
        events.publish_state(caller.pane, state, caller.socket)  # type: ignore[arg-type]
    service.wake()
    return 200, {
        "event": {
            "id": event_id,
            "ts": ts,
            "worktree_id": caller.worktree_id,
            "repo": caller.repo,
            "workspace": caller.workspace,
            "task": caller.task,
            "pane": caller.pane,
            "agent": caller.agent,
            "kind": kind,
            "detail": detail,
            "state": state,
        },
        "cursor": event_id,
        # False when the host pane is gone — a stopped grid with a live
        # sandbox. The event is still recorded; nothing was signalled.
        "pane_updated": bool(alive),
    }


@route("GET", "/v1/events/state", requires=PERM_CONTEXT_READ)
def _event_state(service: ContextService, request: Request) -> tuple[int, dict[str, Any]]:
    """Resolved state for the panes in the caller's workspace.

    `events.pane_states` answers for the whole tmux server, which is more than
    this caller may see. The workspace is the boundary, which is exactly what
    `amux ctx` already discloses to the same agent.
    """
    caller = request.caller
    panes = [
        entry
        for entry in events.pane_states(caller.socket)
        if entry.get("workspace") == caller.workspace
    ]
    return 200, {"panes": panes[: service.config.max_results]}


@route("GET", "/v1/events/wait", requires=PERM_CONTEXT_READ)
def _event_wait(service: ContextService, request: Request) -> tuple[int, dict[str, Any]]:
    """Block until a pane reaches one of `states`, or until the cap expires.

    Two wake-ups, on purpose: an in-process signal from `POST /v1/events`, and
    a re-query every `poll_interval_s` so a *native* host write — a hook in a
    host pane, a `kg` — is seen too. Expiring is not a failure: the answer is
    `state: null` plus a cursor the caller resumes from, so capping the poll
    costs a round trip and never a lost transition.
    """
    caller = request.caller
    pane = _pane_param(service, request)
    wanted = _states_param(request)
    after = _cursor_param(request, "after")
    # The caller's timeout is a request, not a promise: the cap wins, and
    # expiring early is a resumable answer rather than an error.
    timeout = min(
        _float_param(request, "timeout", service.config.max_wait_s, 0.0, 86400.0),
        service.config.max_wait_s,
    )

    deadline = time.monotonic() + timeout
    while True:
        state, _ = events.pane_status(pane, caller.socket)
        fresh = _events_after(service, pane, after)
        cursor = _cursor(fresh, after)
        if state is not None and state in wanted:
            return 200, {
                "pane": pane,
                "state": state,
                "cursor": cursor,
                "events": fresh,
            }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 200, {"pane": pane, "state": None, "cursor": cursor, "events": fresh}
        service.sleep(min(remaining, service.config.poll_interval_s))


def _events_after(
    service: ContextService, pane: str, after: int | None
) -> list[dict[str, Any]]:
    """A pane's events past a cursor, oldest first and bounded."""
    rows = store.iter_events(pane=pane, db_path=service.db_path)
    if after is not None:
        rows = [r for r in rows if int(r["id"]) > after]
    rows.sort(key=lambda r: int(r["id"]))
    return rows[-service.config.max_results :]


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
        request: Request | None = None

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
            # Who, by durable id — never the token, and never the body.
            caller = request.identity if request else None
            self.service.log.info(
                "%s %s -> %s %s %s %.1fms",
                method,
                redact(path),
                status,
                code,
                f"wt{caller.worktree_id}/cap{caller.token_id}" if caller else "-",
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
    server_factory: Callable[[str], Any] | None = None,
    log_stream: Any | None = None,
) -> ServiceHandle:
    """Start the service on a background thread and return a handle to it."""
    service = ContextService(
        config, authenticator=authenticator, server_factory=server_factory
    )
    configure_logging(service.config, stream=log_stream)
    server = build_server(service)
    service.config = replace(service.config, port=int(server.server_address[1]))
    thread = threading.Thread(
        target=server.serve_forever,
        args=(service.config.shutdown_poll_s,),
        name=SERVICE_NAME,
        daemon=True,
    )
    thread.start()
    if config is not None and config.db_path not in (None, store.DB_PATH):
        # The native read paths — core.build_context, events.pane_status — take
        # no db_path and always use the store's own. Pointing the service
        # somewhere else makes those two disagree, which is a debugging aid at
        # best and a wrong answer at worst.
        service.log.warning(
            "the configured database is not the store's own; the native context"
            " and state paths will still read %s",
            store.DB_PATH,
        )
    service.log.info(
        "%s listening on %s:%d (schema %s)",
        SERVICE_NAME,
        LOOPBACK,
        service.config.port,
        service.schema_info().version,
    )
    return ServiceHandle(service=service, server=server, thread=thread)


# --- lifecycle ---
#
# One run file per state directory records the pid and the *bound* port, so
# preflight can find a service it did not start. Every failure here refuses:
# there is no unauthenticated listener and no mounted database to fall back to,
# and `start` will not quietly stand up a second service beside a confusing one.

RUNFILE_NAME = "context-service.json"

ServiceState = Literal["running", "stopped", "stale", "unresponsive"]


class ServiceLifecycleError(RuntimeError):
    """A start, stop, or status operation that could not be completed safely."""


class ServiceStartupError(ServiceLifecycleError):
    """The listener could not be opened. Never a reason to open a weaker one."""


@dataclass(frozen=True)
class RunFile:
    pid: int
    port: int
    started_ts: float
    schema_version: int | None = None

    def to_json(self) -> str:
        return json.dumps(vars(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> RunFile:
        data = json.loads(raw)
        return cls(
            pid=int(data["pid"]),
            port=int(data["port"]),
            started_ts=float(data["started_ts"]),
            schema_version=data.get("schema_version"),
        )


def runfile_path(config: ServiceConfig) -> Path:
    return config.state_home / RUNFILE_NAME


def read_runfile(config: ServiceConfig) -> RunFile | None:
    """The recorded service, or None when there is none to believe.

    A corrupt run file reads as no run file: it is a hint, and the process
    check behind it is the actual evidence.
    """
    path = runfile_path(config)
    try:
        return RunFile.from_json(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        get_logger().warning("ignoring an unreadable run file at %s", path)
        return None


def write_runfile(config: ServiceConfig, run: RunFile) -> Path:
    """Claim the run file atomically, so a reader never sees half of one."""
    path = runfile_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{run.pid}.tmp")
    tmp.write_text(run.to_json())
    tmp.replace(path)
    return path


def remove_runfile(config: ServiceConfig, only_pid: int | None = None) -> None:
    """Release the run file, optionally only if it is still ours — a service
    that outlived its own file must not delete its successor's."""
    run = read_runfile(config)
    if run is None or (only_pid is not None and run.pid != only_pid):
        return
    runfile_path(config).unlink(missing_ok=True)


def _is_zombie(pid: int) -> bool:
    """Whether a pid is a process that has exited but not yet been reaped.

    `kill(pid, 0)` cannot tell: the kernel keeps the slot until the parent reads
    the exit status, and answers as if it were alive until then. We are often
    not that parent — a service started by an amux process that is still running
    is *its* child — so ask the process table. Only reached when the pid already
    looks alive, so the cost is one `ps` on a path that is either rare (status)
    or already waiting on a process (stop).
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False  # no evidence of death is not evidence of death
    return out.stdout.strip().upper().startswith("Z")


def _pid_alive(pid: int) -> bool:
    """Whether a pid is a process that could still be serving."""
    if pid <= 0:
        return False
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False  # our own child, and it has exited
    except OSError:
        pass  # not our child, or we have none
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # someone else's process, but it is there
    return not _is_zombie(pid)


def probe_health(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """`GET /healthz` against a loopback port, or None if nothing answered.

    A degraded service still answers, so the payload — not the status — is what
    says whether it is usable.
    """
    conn = http.client.HTTPConnection(LOOPBACK, port, timeout=timeout)
    try:
        conn.request("GET", "/healthz")
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        conn.close()


@dataclass(frozen=True)
class ServiceStatus:
    state: ServiceState
    message: str
    pid: int | None = None
    port: int | None = None
    health: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def healthy(self) -> bool:
        return bool(self.health and self.health.get("ok"))


def status(config: ServiceConfig | None = None) -> ServiceStatus:
    """What the state directory says about the service, checked against reality."""
    config = config or ServiceConfig()
    run = read_runfile(config)
    if run is None:
        return ServiceStatus("stopped", f"{SERVICE_NAME} is not running")
    if not _pid_alive(run.pid):
        return ServiceStatus(
            "stale",
            f"a run file names pid {run.pid}, which is gone; starting the"
            f" service will clear it",
            pid=run.pid,
            port=run.port,
        )
    health = probe_health(run.port)
    if health is None:
        return ServiceStatus(
            "unresponsive",
            f"pid {run.pid} is alive but nothing answers on {LOOPBACK}:{run.port};"
            f" stop it before starting another",
            pid=run.pid,
            port=run.port,
        )
    detail = "" if health.get("ok") else f" (schema {health.get('status', 'degraded')})"
    return ServiceStatus(
        "running",
        f"{SERVICE_NAME} is running on {LOOPBACK}:{run.port}, pid {run.pid}{detail}",
        pid=run.pid,
        port=run.port,
        health=health,
    )


def _require_compatible_schema(config: ServiceConfig) -> SchemaInfo:
    """Refuse before binding anything. A service that cannot read the store it
    is the sole owner of has nothing safe to serve."""
    info = schema_info(config.database)
    if info.version is None:
        raise ServiceStartupError(
            f"cannot open the context store at {config.database}."
            f" Check the path and permissions; the service will not serve"
            f" without it"
        )
    if not info.compatible:
        raise ServiceStartupError(
            f"the context store is schema version {info.version} but this amux"
            f" expects {info.expected}."
            f" {'Upgrade amux' if info.version > info.expected else 'Run any native amux command to migrate it'}"
            f" — the service will not serve a schema it does not know"
        )
    return info


def launch_argv(config: ServiceConfig) -> list[str]:
    """How to re-exec amux as a detached foreground service.

    A frozen build has no `-m`: `sys.executable` *is* the amux binary, so it is
    invoked through its own subcommand. From a source checkout the module form
    keeps the launcher working even where the `amux` script is not on PATH.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "context-service", "serve", "--port", str(config.port)]
    module = __spec__.name if __spec__ else "amux.context_service"
    return [sys.executable, "-m", module, "serve", "--port", str(config.port)]


def _default_launcher(config: ServiceConfig) -> subprocess.Popen:
    return subprocess.Popen(
        launch_argv(config),
        start_new_session=True,  # survives the shell that asked for it
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,  # the service logs to its own file
    )


def start(
    config: ServiceConfig | None = None,
    *,
    launcher: Callable[[ServiceConfig], Any] | None = None,
    timeout: float = 15.0,
) -> ServiceStatus:
    """Ensure a healthy service is running, and say what happened.

    Idempotent, because sandbox preflight calls it on every spawn: an already
    healthy service is left exactly as it is.
    """
    config = config or ServiceConfig()
    current = status(config)
    if current.running:
        return current
    if current.state == "unresponsive":
        raise ServiceLifecycleError(current.message)
    if current.state == "stale":
        get_logger().info("clearing a stale run file for pid %s", current.pid)
        remove_runfile(config)

    _require_compatible_schema(config)
    (launcher or _default_launcher)(config)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        latest = status(config)
        if latest.running:
            return latest
        time.sleep(0.05)
    raise ServiceStartupError(
        f"{SERVICE_NAME} did not become healthy within {timeout:g}s;"
        f" see {config.log_file}"
    )


def stop(
    config: ServiceConfig | None = None,
    *,
    timeout: float = 10.0,
    force: bool = False,
) -> ServiceStatus:
    """Signal the recorded service and wait for it to go.

    Idempotent. The run file is removed only once the process is actually gone,
    so a refusal never leaves a record claiming nothing is running.
    """
    config = config or ServiceConfig()
    current = status(config)
    if current.state == "stopped":
        return current
    if current.state == "stale":
        remove_runfile(config)
        return ServiceStatus("stopped", f"cleared a stale run file for pid {current.pid}")

    pid = current.pid or 0
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        remove_runfile(config)
        return ServiceStatus("stopped", f"pid {pid} was already gone")
    except PermissionError as exc:
        raise ServiceLifecycleError(
            f"not allowed to signal pid {pid}; it belongs to another user"
        ) from exc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            remove_runfile(config)
            return ServiceStatus("stopped", f"{SERVICE_NAME} (pid {pid}) stopped")
        time.sleep(0.05)
    raise ServiceLifecycleError(
        f"pid {pid} did not stop within {timeout:g}s; retry with force to"
        f" send SIGKILL"
    )


def serve_foreground(
    config: ServiceConfig | None = None,
    *,
    stream: Any | None = None,
    ready: Callable[[ServiceHandle], None] | None = None,
) -> int:
    """Run the service in this process until it is signalled.

    The order matters: schema, then bind, then claim the run file. Each step
    refuses outright, so a run file only ever names a process that really is
    listening on the port it records.
    """
    config = config or ServiceConfig()
    log = configure_logging(config, stream=stream or sys.stderr)
    try:
        info = _require_compatible_schema(config)
        service = ContextService(config)
        server = build_server(service)
    except ServiceLifecycleError as exc:
        log.error("%s", exc)
        return 1

    port = int(server.server_address[1])
    service.config = replace(service.config, port=port)
    handle = ServiceHandle(service=service, server=server)
    write_runfile(
        config,
        RunFile(
            pid=os.getpid(),
            port=port,
            started_ts=time.time(),
            schema_version=info.version,
        ),
    )

    def shut_down(signum: int, _frame: Any) -> None:
        log.info("stopping on signal %d", signum)
        # From a signal handler, so it must not block: shutdown() waits for the
        # serve loop, which is this thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, shut_down)
        except ValueError:
            pass  # not the main thread; the caller owns shutdown

    log.info(
        "%s serving on %s:%d, pid %d, schema %s",
        SERVICE_NAME,
        LOOPBACK,
        port,
        os.getpid(),
        info.version,
    )
    try:
        if ready is not None:
            ready(handle)
        server.serve_forever(poll_interval=config.shutdown_poll_s)
    finally:
        server.server_close()
        remove_runfile(config, only_pid=os.getpid())
        log.info("%s stopped", SERVICE_NAME)
    return 0


ACTIONS = ("serve", "start", "status", "stop")


def run_action(
    action: str, config: ServiceConfig, *, force: bool = False
) -> tuple[int, str]:
    """Perform one lifecycle action and return `(exit code, message)`.

    Shared by `python -m amux.context_service` and `amux context-service`, so
    the two cannot drift into reporting the same state differently.
    """
    if action == "serve":
        return serve_foreground(config), ""
    try:
        result = (
            start(config)
            if action == "start"
            else status(config)
            if action == "status"
            else stop(config, force=force)
        )
    except ServiceLifecycleError as exc:
        return 1, str(exc)
    return (0 if result.state in ("running", "stopped") else 1), result.message


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m amux.context_service <serve|start|status|stop>`.

    A debugging and launcher entry point; `amux context-service` is the
    user-facing one, and both go through `run_action`.
    """
    parser = argparse.ArgumentParser(prog="amux-context-service")
    parser.add_argument("action", choices=ACTIONS, help="what to do")
    parser.add_argument("--port", type=int, default=None, help="loopback port")
    parser.add_argument("--db", default=None, help="context store path")
    parser.add_argument(
        "--force", action="store_true", help="stop: send SIGKILL instead of SIGTERM"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    overrides: dict[str, Any] = {}
    if args.port is not None:
        overrides["port"] = args.port
    if args.db is not None:
        overrides["db_path"] = Path(args.db).expanduser()
    try:
        config = ServiceConfig.from_env(**overrides)
        code, message = run_action(args.action, config, force=args.force)
    except ConfigError as exc:
        print(f"amux context-service: {exc}", file=sys.stderr)
        return 1
    if message:
        print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
