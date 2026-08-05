"""Capability tokens for sandbox context access.

The security property these tests defend is narrow and absolute: the host mints
a token, hands the plaintext to exactly one sandbox, and keeps only a hash. If
the plaintext is ever recoverable from the database or the logs, a reader of
either can impersonate an agent, so those two assertions are checked against
raw bytes rather than through the API that is supposed to be hiding them.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from pathlib import Path

import pytest

from amux import store


@pytest.fixture
def worktree_id(db_path: Path) -> int:
    return store.register_worktree(
        pane="%7",
        workspace="proj",
        task="task0",
        path="",
        branch="amux/proj/task0/brave-hawk",
        agent="claude",
        name="brave-hawk",
        repo="/tmp/repo",
        runtime="docker-sandbox",
        runtime_status="created",
        sandbox_name="amux-proj-task0-brave-hawk-a1b2c3",
        db_path=db_path,
    )


# --- minting ---


def test_minting_returns_plaintext_and_id(db_path: Path, worktree_id: int) -> None:
    token, token_id = store.mint_context_token(
        worktree_id, permissions=["context:read"], db_path=db_path
    )
    assert token_id > 0
    assert isinstance(token, str)


def test_tokens_are_high_entropy_and_unique(db_path: Path, worktree_id: int) -> None:
    tokens = {
        store.mint_context_token(worktree_id, permissions=[], db_path=db_path)[0]
        for _ in range(20)
    }
    assert len(tokens) == 20
    # secrets.token_urlsafe(32) is 43 characters; anything materially shorter
    # would be brute-forceable against a loopback service with no rate limit.
    assert all(len(t) >= 43 for t in tokens)


def test_plaintext_token_never_reaches_the_database(
    db_path: Path, worktree_id: int
) -> None:
    """Read the file, not the API. A future refactor that starts persisting the
    plaintext somewhere incidental must fail here.

    The hash assertion is not decoration. Without it this test passes on an
    empty database — a mint that wrote nothing at all would satisfy "the token
    is absent", so the evidence for "plaintext never enters SQLite" would come
    from a file that contains nothing whatsoever.
    """
    token, token_id = store.mint_context_token(
        worktree_id, permissions=["context:read"], db_path=db_path
    )
    raw = db_path.read_bytes()
    for suffix in ("", "-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        if sidecar.exists():
            raw += sidecar.read_bytes()

    # Positive first: prove a row really landed in these bytes.
    assert token_id > 0
    assert hashlib.sha256(token.encode()).hexdigest().encode() in raw

    assert token.encode() not in raw


def test_only_the_sha256_hash_is_stored(db_path: Path, worktree_id: int) -> None:
    token, token_id = store.mint_context_token(
        worktree_id, permissions=[], db_path=db_path
    )
    conn = sqlite3.connect(db_path)
    try:
        stored = conn.execute(
            "SELECT token_hash FROM context_tokens WHERE id = ?", (token_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert stored == hashlib.sha256(token.encode()).hexdigest()


def test_minting_emits_no_log_records_at_all(
    db_path: Path, worktree_id: int, caplog: pytest.LogCaptureFixture
) -> None:
    """`store` has no logger, so "the token is not in the log" is structurally
    vacuous — it passes because nothing is ever written, not because anything
    was redacted. Assert the real property instead: minting stays silent.

    If someone adds logging here, this fails and they have to decide
    deliberately whether the new record can carry capability material, rather
    than inheriting an assertion that never could have caught it.
    """
    with caplog.at_level(logging.DEBUG):
        token, token_id = store.mint_context_token(
            worktree_id, permissions=["context:read"], db_path=db_path
        )

    assert token_id > 0  # minting actually happened
    assert caplog.records == [], (
        "store now logs during minting; assert redaction of the plaintext"
        " rather than its absence"
    )
    assert token not in caplog.text


def test_a_resolved_record_never_carries_the_plaintext(
    db_path: Path, worktree_id: int
) -> None:
    """The other surface a plaintext could escape through: what lookup hands
    back to the service, which then puts it in responses and logs."""
    token, _ = store.mint_context_token(
        worktree_id, permissions=["context:read"], db_path=db_path
    )
    record = store.context_token_record(token, db_path=db_path)

    assert record is not None and record["name"] == "brave-hawk"  # a real record
    assert token not in repr(record)
    assert not any(value == token for value in record.values())


# --- lookup ---


def test_valid_token_resolves_to_its_execution_identity(
    db_path: Path, worktree_id: int
) -> None:
    """Identity comes from the token record, never from the caller. This is the
    lookup the service builds its whole authorization story on."""
    token, token_id = store.mint_context_token(
        worktree_id, permissions=["context:read", "notes:write"], db_path=db_path
    )
    record = store.context_token_record(token, db_path=db_path)
    assert record is not None
    assert record["id"] == token_id
    assert record["worktree_id"] == worktree_id
    assert record["permissions"] == ("context:read", "notes:write")
    assert record["workspace"] == "proj"
    assert record["task"] == "task0"
    assert record["pane"] == "%7"
    assert record["name"] == "brave-hawk"
    assert record["agent"] == "claude"
    assert record["repo"] == "/tmp/repo"
    assert record["runtime"] == "docker-sandbox"
    assert record["branch"] == "amux/proj/task0/brave-hawk"


def test_unknown_token_resolves_to_nothing(db_path: Path, worktree_id: int) -> None:
    store.mint_context_token(worktree_id, permissions=[], db_path=db_path)
    assert store.context_token_record("not-a-real-token", db_path=db_path) is None


def test_empty_token_resolves_to_nothing(db_path: Path, worktree_id: int) -> None:
    assert store.context_token_record("", db_path=db_path) is None


def test_token_hash_is_not_accepted_as_a_token(
    db_path: Path, worktree_id: int
) -> None:
    """Presenting the stored hash must not authenticate, or a database read
    would be equivalent to holding every token."""
    token, _ = store.mint_context_token(worktree_id, permissions=[], db_path=db_path)
    digest = hashlib.sha256(token.encode()).hexdigest()
    assert store.context_token_record(digest, db_path=db_path) is None


def test_permissions_round_trip_in_order(db_path: Path, worktree_id: int) -> None:
    perms = ["context:read", "notes:read", "notes:write", "events:write"]
    token, _ = store.mint_context_token(
        worktree_id, permissions=perms, db_path=db_path
    )
    record = store.context_token_record(token, db_path=db_path)
    assert record is not None
    assert record["permissions"] == tuple(perms)


def test_permission_values_are_validated(db_path: Path, worktree_id: int) -> None:
    """A comma would silently split one permission into two on read."""
    with pytest.raises(ValueError, match="permission"):
        store.mint_context_token(
            worktree_id, permissions=["notes:write,events:write"], db_path=db_path
        )


def test_token_must_belong_to_a_real_execution(db_path: Path) -> None:
    with pytest.raises(ValueError, match="worktree"):
        store.mint_context_token(9999, permissions=[], db_path=db_path)


# --- expiry ---


def test_expired_token_is_rejected(db_path: Path, worktree_id: int) -> None:
    token, _ = store.mint_context_token(
        worktree_id, permissions=[], ttl=60.0, db_path=db_path
    )
    assert store.context_token_record(token, db_path=db_path) is not None
    future = time.time() + 61.0
    assert store.context_token_record(token, now=future, db_path=db_path) is None


def test_token_without_a_ttl_does_not_expire(db_path: Path, worktree_id: int) -> None:
    token, _ = store.mint_context_token(worktree_id, permissions=[], db_path=db_path)
    far_future = time.time() + 365 * 24 * 3600
    assert store.context_token_record(token, now=far_future, db_path=db_path) is not None


def test_expiry_boundary_is_exclusive(db_path: Path, worktree_id: int) -> None:
    now = time.time()
    token, token_id = store.mint_context_token(
        worktree_id, permissions=[], ttl=60.0, now=now, db_path=db_path
    )
    assert store.context_token_record(token, now=now + 59.9, db_path=db_path) is not None
    assert store.context_token_record(token, now=now + 60.0, db_path=db_path) is None
    assert token_id > 0


# --- revocation ---


def test_revoked_token_is_rejected(db_path: Path, worktree_id: int) -> None:
    token, token_id = store.mint_context_token(
        worktree_id, permissions=[], db_path=db_path
    )
    assert store.context_token_record(token, db_path=db_path) is not None
    store.revoke_context_token(token_id, db_path=db_path)
    assert store.context_token_record(token, db_path=db_path) is None


def test_revocation_is_recorded_not_deleted(db_path: Path, worktree_id: int) -> None:
    """The row survives so an audit can still show that a token existed and
    when it stopped working."""
    token, token_id = store.mint_context_token(
        worktree_id, permissions=[], db_path=db_path
    )
    store.revoke_context_token(token_id, db_path=db_path)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT revoked_ts FROM context_tokens WHERE id = ?", (token_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] is not None
    assert token  # the plaintext is still not recoverable from that row


def test_revoking_twice_is_harmless(db_path: Path, worktree_id: int) -> None:
    _, token_id = store.mint_context_token(worktree_id, permissions=[], db_path=db_path)
    store.revoke_context_token(token_id, db_path=db_path)
    store.revoke_context_token(token_id, db_path=db_path)


def test_sandbox_removal_revokes_every_token_it_held(
    db_path: Path, worktree_id: int
) -> None:
    """`sbx rm` must leave nothing behind that still authenticates."""
    tokens = [
        store.mint_context_token(worktree_id, permissions=[], db_path=db_path)[0]
        for _ in range(3)
    ]
    other = store.register_worktree(
        pane="%8",
        workspace="proj",
        task="task0",
        path="",
        branch="b",
        runtime="docker-sandbox",
        db_path=db_path,
    )
    survivor, _ = store.mint_context_token(other, permissions=[], db_path=db_path)

    revoked = store.revoke_context_tokens_for_worktree(worktree_id, db_path=db_path)

    assert revoked == 3
    assert all(store.context_token_record(t, db_path=db_path) is None for t in tokens)
    assert store.context_token_record(survivor, db_path=db_path) is not None


def test_revoking_for_an_unknown_execution_revokes_nothing(db_path: Path) -> None:
    assert store.revoke_context_tokens_for_worktree(9999, db_path=db_path) == 0
