"""A stdlib fake of the host context service, for sandbox-client tests.

This is deliberately independent of `amux.context_service`: the sandbox client
is the *other* side of the HTTP contract and must be testable on its own. The
canned responses here are the wire contract that `sandbox_client` codes against
(documented in that module's docstring); when the real service lands, the same
tests should pass against it unchanged.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "test-token-not-a-real-secret"


class Recorded:
    """One request the fake saw, as the client sent it."""

    def __init__(self, method: str, path: str, query: dict, headers, body):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    @property
    def authorization(self) -> str:
        return self.headers.get("Authorization", "")

    def q(self, key: str, default=None):
        values = self.query.get(key)
        return values[0] if values else default


class FakeContextService:
    """Threaded loopback HTTP service returning canned JSON.

    `routes` maps `("GET", "/v1/context")` to either a literal response dict or
    a callable taking the `Recorded` request and returning
    `(status, payload)` / `payload`.
    """

    def __init__(self, routes: dict | None = None, token: str = TOKEN):
        self.routes: dict = dict(routes or {})
        self.token = token
        self.requests: list[Recorded] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> FakeContextService:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                pass  # keep pytest output clean

            def _handle(self, method: str) -> None:
                url = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                record = Recorded(
                    method, url.path, parse_qs(url.query), self.headers, body
                )
                fake.requests.append(record)
                status, payload = fake.respond(record)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # shutdown() only returns once serve_forever notices, so the default
        # 0.5s poll would cost every test half a second of teardown.
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        assert self._thread is not None
        self._thread.join(timeout=5)

    @property
    def endpoint(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # -- routing -----------------------------------------------------------

    def respond(self, record: Recorded) -> tuple[int, Any]:
        if record.path != "/healthz" and record.authorization != f"Bearer {self.token}":
            return 401, {
                "error": {"code": "unauthenticated", "message": "invalid capability"}
            }
        route = self.routes.get((record.method, record.path))
        if route is None:
            return 404, {
                "error": {"code": "unknown_operation", "message": record.path}
            }
        result = route(record) if callable(route) else route
        if isinstance(result, tuple):
            status, payload = result
            return status, payload
        return 200, result

    def only(self, method: str, path: str) -> Recorded:
        """The single request made to one route (asserts there was exactly one)."""
        hits = [r for r in self.requests if r.method == method and r.path == path]
        assert len(hits) == 1, f"expected 1 {method} {path}, saw {len(hits)}"
        return hits[0]

    def body_of(self, method: str, path: str) -> dict:
        return json.loads(self.only(method, path).body)
