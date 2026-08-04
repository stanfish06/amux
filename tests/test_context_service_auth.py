"""Task 2.2: identity comes from the capability, never from the caller.

The rule under test is narrow and total: a request may present a token, and
everything else about who the caller is — workspace, task, repository, pane,
agent, name, runtime — is read from the execution row that token is bound to.
A body or query string that names any of those is data.
"""

from __future__ import annotations

import http.client
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from amux import context_service as cs
from amux import store


# --- fixtures ---


@dataclass
class Probe:
    """A running service plus the store it authenticates against."""

    handle: cs.ServiceHandle
    db: Path
    records: list[logging.LogRecord]

    def get(self, path: str, token: str | None = None) -> tuple[int, dict]:
        return self._request("GET", path, token=token)

    def post(
        self, path: str, payload: dict | None = None, token: str | None = None
    ) -> tuple[int, dict]:
        return self._request(
            "POST",
            path,
            token=token,
            body=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection(cs.LOOPBACK, self.handle.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=sent)
            response = conn.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
        finally:
            conn.close()

    def register(self, **overrides) -> int:
        """A sandbox execution row, as the runtime adapter will create it."""
        row: dict = {
            "pane": "%1",
            "workspace": "proj",
            "task": "fix",
            "agent": "claude",
            "name": "swift-crane",
            "path": "",  # sandbox rows carry no host path
            "branch": "amux/proj/fix/swift-crane",
            "base_ref": "main",
            "repo": "/repos/proj",
            "runtime": "docker-sandbox",
            "runtime_status": "running",
            "sandbox_name": "amux-proj-fix-swift-crane-a1b2",
            "sandbox_id": "sbx-0001",
            "socket_name": "amux-root",
        }
        row.update(overrides)
        return store.register_worktree(db_path=self.db, **row)

    def mint(
        self, worktree_id: int, permissions=cs.AGENT_PERMISSIONS, **kw
    ) -> tuple[str, int]:
        return store.mint_context_token(
            worktree_id, permissions=permissions, db_path=self.db, **kw
        )

    def agent(self, **overrides) -> tuple[str, int, int]:
        """`(token, token_id, worktree_id)` for one authenticated agent."""
        permissions = overrides.pop("permissions", cs.AGENT_PERMISSIONS)
        worktree_id = self.register(**overrides)
        token, token_id = self.mint(worktree_id, permissions=permissions)
        return token, token_id, worktree_id

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
    def __init__(self, sink: list[logging.LogRecord]) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(record)


@pytest.fixture
def token_probe(tmp_path, db_path):
    """A service on the real store authenticator — the shipped default."""
    config = cs.ServiceConfig(
        port=0,
        db_path=db_path,
        state_dir=tmp_path / "state",
        shutdown_poll_s=0.01,  # a server per test case; see the note in _config
    )
    handle = cs.start_service(config)
    records: list[logging.LogRecord] = []
    capture = _Capture(records)
    cs.get_logger().addHandler(capture)
    try:
        yield Probe(handle=handle, db=db_path, records=records)
    finally:
        cs.get_logger().removeHandler(capture)
        handle.stop()


@pytest.fixture
def spy():
    """A route that reports what the service decided about the caller."""

    def handler(service, request):
        return 200, {
            "identity": vars(request.caller) | {
                "permissions": sorted(request.caller.permissions)
            },
            "body": request.body,
            "query": request.query,
        }

    for method in ("GET", "POST"):
        cs._ROUTES[(method, "/v1/spy")] = cs.Route(handler=handler)
    try:
        yield "/v1/spy"
    finally:
        for method in ("GET", "POST"):
            del cs._ROUTES[(method, "/v1/spy")]


# --- identity is derived, not asserted ---


def test_a_minted_token_authenticates_and_carries_the_whole_row(token_probe, spy):
    token, token_id, worktree_id = token_probe.agent()
    status, payload = token_probe.get(spy, token=token)
    assert status == 200
    assert payload["identity"] == {
        "worktree_id": worktree_id,
        "token_id": token_id,
        "pane": "%1",
        "workspace": "proj",
        "task": "fix",
        "repo": "/repos/proj",
        "agent": "claude",
        "name": "swift-crane",
        "branch": "amux/proj/fix/swift-crane",
        "runtime": "docker-sandbox",
        "runtime_status": "running",
        "sandbox_name": "amux-proj-fix-swift-crane-a1b2",
        "sandbox_id": "sbx-0001",
        "status": "active",
        "socket": "amux-root",
        "permissions": sorted(cs.AGENT_PERMISSIONS),
    }


def test_identity_ignores_every_field_the_body_supplies(token_probe, spy):
    token, _, worktree_id = token_probe.agent()
    _, payload = token_probe.post(
        spy,
        {
            "worktree_id": 9999,
            "token_id": 9999,
            "pane": "%99",
            "workspace": "other-workspace",
            "task": "other-task",
            "repo": "/repos/elsewhere",
            "agent": "impostor",
            "name": "not-me",
            "permissions": ["host:control"],
            "identity": {"workspace": "other-workspace"},
        },
        token=token,
    )
    identity = payload["identity"]
    assert identity["worktree_id"] == worktree_id
    assert identity["workspace"] == "proj"
    assert identity["task"] == "fix"
    assert identity["pane"] == "%1"
    assert identity["agent"] == "claude"
    assert identity["name"] == "swift-crane"
    assert identity["repo"] == "/repos/proj"
    assert identity["permissions"] == sorted(cs.AGENT_PERMISSIONS)
    # The body still arrives — it is data, and a handler may read `text` from
    # it. It is only attribution that it cannot touch.
    assert payload["body"]["agent"] == "impostor"


def test_identity_ignores_every_field_the_query_supplies(token_probe, spy):
    token, _, worktree_id = token_probe.agent()
    _, payload = token_probe.get(
        f"{spy}?workspace=other&pane=%2599&agent=impostor&worktree_id=9999",
        token=token,
    )
    assert payload["identity"]["worktree_id"] == worktree_id
    assert payload["identity"]["workspace"] == "proj"
    assert payload["identity"]["pane"] == "%1"


def test_two_agents_get_two_identities(token_probe, spy):
    first_token, _, first_id = token_probe.agent(pane="%1", name="swift-crane")
    second_token, _, second_id = token_probe.agent(pane="%2", name="happy-deer")
    _, first = token_probe.get(spy, token=first_token)
    _, second = token_probe.get(spy, token=second_token)
    assert first["identity"]["worktree_id"] == first_id
    assert second["identity"]["worktree_id"] == second_id
    assert first["identity"]["name"] == "swift-crane"
    assert second["identity"]["name"] == "happy-deer"


def test_one_agents_token_cannot_become_another(token_probe, spy):
    """The obvious attack: hold a valid capability, ask to be someone else."""
    victim_id = token_probe.register(pane="%2", name="happy-deer", agent="codex")
    attacker_token, _, attacker_id = token_probe.agent(pane="%1", name="swift-crane")
    _, payload = token_probe.post(
        spy, {"worktree_id": victim_id, "name": "happy-deer"}, token=attacker_token
    )
    assert payload["identity"]["worktree_id"] == attacker_id
    assert payload["identity"]["name"] == "swift-crane"


def test_the_socket_comes_from_the_row_so_events_reach_the_right_server(token_probe, spy):
    token, _, _ = token_probe.agent(socket_name="amux-other")
    _, payload = token_probe.get(spy, token=token)
    assert payload["identity"]["socket"] == "amux-other"


def test_a_row_without_a_socket_falls_back_to_the_service_socket(token_probe, spy):
    token, _, _ = token_probe.agent(socket_name="")
    _, payload = token_probe.get(spy, token=token)
    assert payload["identity"]["socket"] == token_probe.handle.service.config.socket


def test_a_host_runtime_row_with_a_capability_still_authenticates(token_probe, spy):
    """Nothing here gates on the runtime: a capability is a capability."""
    token, _, _ = token_probe.agent(
        runtime="host", runtime_status="", sandbox_name="", sandbox_id=""
    )
    _, payload = token_probe.get(spy, token=token)
    assert payload["identity"]["runtime"] == "host"


# --- tokens that must not work ---


def test_an_unknown_token_is_rejected(token_probe, spy):
    token_probe.agent()  # a real capability exists, just not this one
    status, payload = token_probe.get(spy, token="not-a-real-token")
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_a_revoked_token_is_rejected(token_probe, spy):
    token, token_id, _ = token_probe.agent()
    assert token_probe.get(spy, token=token)[0] == 200
    store.revoke_context_token(token_id, db_path=token_probe.db)
    status, payload = token_probe.get(spy, token=token)
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_removing_a_sandbox_revokes_its_capabilities(token_probe, spy):
    token, _, worktree_id = token_probe.agent()
    other_token, _, _ = token_probe.agent(pane="%2")
    revoked = store.revoke_context_tokens_for_worktree(worktree_id, db_path=token_probe.db)
    assert revoked == 1
    assert token_probe.get(spy, token=token)[0] == 401
    # A teammate's capability is untouched.
    assert token_probe.get(spy, token=other_token)[0] == 200


def test_an_expired_token_is_rejected(token_probe, spy):
    worktree_id = token_probe.register()
    token, _ = token_probe.mint(worktree_id, now=time.time() - 100, ttl=1.0)
    status, payload = token_probe.get(spy, token=token)
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"


def test_a_token_whose_execution_was_removed_is_rejected(token_probe, spy):
    """Removal is meant to revoke; if that failed, the row still refuses."""
    token, _, worktree_id = token_probe.agent()
    store.set_worktree_status(worktree_id, "removed", db_path=token_probe.db)
    status, payload = token_probe.get(spy, token=token)
    assert status == 401
    assert payload["error"]["code"] == "unauthorized"
    assert "removed" in payload["error"]["message"]


def test_a_merged_execution_still_authenticates(token_probe, spy):
    """`status` tracks the merge lifecycle, not the VM: an integrated agent is
    still running and still needs to post notes and events."""
    token, _, worktree_id = token_probe.agent()
    store.set_worktree_status(worktree_id, "merged", db_path=token_probe.db)
    status, payload = token_probe.get(spy, token=token)
    assert status == 200
    assert payload["identity"]["status"] == "merged"


def test_unknown_expired_and_revoked_are_indistinguishable(token_probe, spy):
    """A caller must not be able to tell which of the three it hit — that is a
    guessing oracle."""
    worktree_id = token_probe.register()
    expired, _ = token_probe.mint(worktree_id, now=time.time() - 100, ttl=1.0)
    revoked, revoked_id = token_probe.mint(worktree_id)
    store.revoke_context_token(revoked_id, db_path=token_probe.db)
    answers = {
        json.dumps(token_probe.get(spy, token=t)) for t in ("unknown-token", expired, revoked)
    }
    assert len(answers) == 1


def test_a_token_from_another_database_does_not_authenticate(token_probe, spy, tmp_path):
    """Authentication reads the service's own store and no other."""
    elsewhere = tmp_path / "elsewhere.db"
    other_id = store.register_worktree(
        pane="%1",
        workspace="proj",
        task="fix",
        path="",
        branch="b",
        db_path=elsewhere,
    )
    foreign, _ = store.mint_context_token(
        other_id, permissions=cs.AGENT_PERMISSIONS, db_path=elsewhere
    )
    assert token_probe.get(spy, token=foreign)[0] == 401


# --- scope ---


def test_a_foreign_workspace_is_refused_and_the_message_names_the_scope():
    identity = cs.Identity(worktree_id=1, workspace="proj", task="fix")
    cs.require_scope(identity, workspace="proj")  # own workspace is fine
    with pytest.raises(cs.ServiceError) as caught:
        cs.require_scope(identity, workspace="other")
    assert caught.value.code == "forbidden"
    assert caught.value.status == 403
    assert "other" in caught.value.message
    assert "proj/fix" in caught.value.message


def test_a_foreign_repository_is_refused():
    identity = cs.Identity(
        worktree_id=1, workspace="proj", task="fix", repo="/repos/proj"
    )
    cs.require_scope(identity, repo="/repos/proj")
    with pytest.raises(cs.ServiceError) as caught:
        cs.require_scope(identity, repo="/repos/elsewhere")
    assert caught.value.code == "forbidden"
    assert "/repos/elsewhere" in caught.value.message


def test_a_sibling_task_is_not_a_scope_error():
    """Task is not part of the scope check on purpose: native `amux notes
    --task other` is allowed, and visibility — not authorization — is what
    keeps another task's private notes private."""
    identity = cs.Identity(worktree_id=1, workspace="proj", task="fix")
    cs.require_scope(identity, workspace="proj")  # no task argument exists
    assert "task" not in cs.require_scope.__code__.co_varnames


def test_a_refusal_quotes_the_request_safely():
    identity = cs.Identity(worktree_id=1, workspace="proj", task="fix")
    assert cs.deny("pane", "%99", identity).message == "pane %99 is not in proj/fix"
    assert cs.deny("pane", "%99", identity).code == "forbidden"

    # Whatever arrives on the wire cannot forge a log line or run long.
    hostile = "%9\n2026-01-01 INFO forged\x00" + "x" * 200
    message = cs.deny("pane", hostile, identity).message
    assert "\n" not in message and "\x00" not in message
    assert len(message) < 120
    assert message.endswith("is not in proj/fix")


def test_scope_failures_are_forbidden_not_not_found():
    """403 and not 404: the caller is authenticated, and pretending the scope
    does not exist would send it hunting for a service bug instead."""
    identity = cs.Identity(worktree_id=1, workspace="proj", task="fix")
    for kwargs in ({"workspace": "other"}, {"repo": "/elsewhere"}):
        with pytest.raises(cs.ServiceError) as caught:
            cs.require_scope(identity, **kwargs)
        assert caught.value.status == 403


# --- capabilities ---


def test_a_route_refuses_a_capability_that_lacks_its_permission(token_probe):
    cs._ROUTES[("POST", "/v1/needs-notes")] = cs.Route(
        handler=lambda service, request: (200, {"ok": True}),
        requires=cs.PERM_NOTES_WRITE,
    )
    try:
        allowed, _, _ = token_probe.agent(pane="%1", permissions=cs.AGENT_PERMISSIONS)
        read_only, _, _ = token_probe.agent(pane="%2", permissions=(cs.PERM_CONTEXT_READ,))
        assert token_probe.post("/v1/needs-notes", token=allowed)[0] == 200
        status, payload = token_probe.post("/v1/needs-notes", token=read_only)
        assert status == 403
        assert payload["error"]["code"] == "forbidden"
        assert cs.PERM_NOTES_WRITE in payload["error"]["message"]
    finally:
        del cs._ROUTES[("POST", "/v1/needs-notes")]


def test_a_capability_with_no_permissions_can_reach_nothing_that_needs_one(token_probe):
    cs._ROUTES[("GET", "/v1/needs-read")] = cs.Route(
        handler=lambda service, request: (200, {"ok": True}),
        requires=cs.PERM_CONTEXT_READ,
    )
    try:
        token, _, _ = token_probe.agent(permissions=())
        assert token_probe.get("/v1/needs-read", token=token)[0] == 403
    finally:
        del cs._ROUTES[("GET", "/v1/needs-read")]


def test_permissions_round_trip_through_the_store(token_probe, spy):
    token, _, _ = token_probe.agent(permissions=(cs.PERM_EVENTS_WRITE,))
    _, payload = token_probe.get(spy, token=token)
    assert payload["identity"]["permissions"] == [cs.PERM_EVENTS_WRITE]


def test_the_agent_permission_set_grants_context_only():
    """Nothing in the vocabulary can express host control, so no capability can
    hold it."""
    assert set(cs.AGENT_PERMISSIONS) == {
        cs.PERM_CONTEXT_READ,
        cs.PERM_NOTES_WRITE,
        cs.PERM_EVENTS_WRITE,
    }
    for permission in cs.AGENT_PERMISSIONS:
        assert permission.split(":")[0] in {"context", "notes", "events"}


def test_a_handler_cannot_forget_to_authenticate(token_probe):
    """`request.caller` is the only way to reach an identity, and it refuses
    when there is none."""
    request = cs.Request(method="GET", path="/v1/spy")
    with pytest.raises(cs.ServiceError) as caught:
        _ = request.caller
    assert caught.value.code == "unauthorized"


# --- the token stays out of everything durable ---


def test_the_plaintext_token_is_absent_from_the_database(token_probe, spy):
    token, _, _ = token_probe.agent()
    assert token_probe.get(spy, token=token)[0] == 200
    assert token.encode() not in token_probe.db.read_bytes()
    for suffix in ("-wal", "-shm"):
        sidecar = token_probe.db.with_name(token_probe.db.name + suffix)
        if sidecar.exists():
            assert token.encode() not in sidecar.read_bytes()


def test_the_plaintext_token_is_absent_from_the_logs(token_probe, spy):
    token, _, worktree_id = token_probe.agent()
    token_probe.get(spy, token=token)
    token_probe.get(spy, token="wrong-" + token)
    joined = token_probe.wait_for_log(f"wt{worktree_id}")
    assert token not in joined
    assert token_probe.handle.service.config.log_file.read_text().find(token) == -1
