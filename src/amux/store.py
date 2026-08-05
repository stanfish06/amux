from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from hmac import compare_digest
from pathlib import Path
from typing import Any, Literal

from amux.shared import STATE_DIR

DB_PATH = STATE_DIR / "context.db"

SCHEMA_VERSION = 3

NoteScope = Literal["agent", "task", "workspace"]
NoteKind = Literal["note", "decision", "finding", "blocker"]
WorktreeStatus = Literal["active", "merged", "removed"]
Runtime = Literal["host", "docker-sandbox"]

NOTE_SCOPES = ("agent", "task", "workspace")
NOTE_KINDS = ("note", "decision", "finding", "blocker")
RUNTIMES = ("host", "docker-sandbox")

_RUNTIME_COLUMNS = (
    ("runtime", "TEXT NOT NULL DEFAULT 'host'"),
    ("runtime_status", "TEXT NOT NULL DEFAULT ''"),
    ("sandbox_name", "TEXT NOT NULL DEFAULT ''"),
    ("sandbox_id", "TEXT NOT NULL DEFAULT ''"),
    ("socket_name", "TEXT NOT NULL DEFAULT ''"),
)

_WORKTREES_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  id INTEGER PRIMARY KEY,
  pane TEXT NOT NULL,
  workspace TEXT NOT NULL,
  task TEXT NOT NULL,
  agent TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL,
  branch TEXT NOT NULL,
  base_ref TEXT NOT NULL DEFAULT '',
  repo TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_ts REAL NOT NULL,
  runtime TEXT NOT NULL DEFAULT 'host',
  runtime_status TEXT NOT NULL DEFAULT '',
  sandbox_name TEXT NOT NULL DEFAULT '',
  sandbox_id TEXT NOT NULL DEFAULT '',
  socket_name TEXT NOT NULL DEFAULT ''
);
"""

_SCHEMA = (
    _WORKTREES_DDL.format(table="worktrees")
    + """
CREATE INDEX IF NOT EXISTS idx_worktrees_task ON worktrees(workspace, task);
CREATE INDEX IF NOT EXISTS idx_worktrees_pane ON worktrees(pane, status, created_ts);
CREATE INDEX IF NOT EXISTS idx_worktrees_repo ON worktrees(repo, created_ts);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  worktree_id INTEGER REFERENCES worktrees(id),
  repo TEXT NOT NULL DEFAULT '',
  workspace TEXT NOT NULL DEFAULT '',
  task TEXT NOT NULL DEFAULT '',
  pane TEXT NOT NULL,
  agent TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_scope ON events(workspace, task, ts);
CREATE INDEX IF NOT EXISTS idx_events_pane ON events(pane, ts);
CREATE INDEX IF NOT EXISTS idx_events_worktree ON events(worktree_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_repo ON events(repo, ts);

CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  worktree_id INTEGER REFERENCES worktrees(id),
  repo TEXT NOT NULL DEFAULT '',
  workspace TEXT NOT NULL,
  task TEXT NOT NULL,
  pane TEXT NOT NULL,
  agent TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'note',
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_scope ON notes(workspace, task, scope, ts);
CREATE INDEX IF NOT EXISTS idx_notes_worktree ON notes(worktree_id, ts);
CREATE INDEX IF NOT EXISTS idx_notes_repo ON notes(repo, ts);

CREATE TABLE IF NOT EXISTS context_tokens (
  id INTEGER PRIMARY KEY,
  worktree_id INTEGER NOT NULL REFERENCES worktrees(id),
  token_hash TEXT NOT NULL UNIQUE,
  permissions TEXT NOT NULL DEFAULT '',
  created_ts REAL NOT NULL,
  expires_ts REAL,
  revoked_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_hash ON context_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_tokens_worktree ON context_tokens(worktree_id);
"""
)


_PANE_WORKTREE_SQL = (
    "SELECT {cols} FROM worktrees WHERE pane = ?{since}"
    " ORDER BY (status = 'active') DESC, created_ts DESC, id DESC LIMIT 1"
)


def _pane_worktree(
    conn: sqlite3.Connection, pane: str, cols: str, since: float | None
) -> sqlite3.Row | None:
    """The worktree row a pane fronts. `since` drops rows registered before the
    pane existed, which a recycled `%N` would otherwise inherit."""
    sql = _PANE_WORKTREE_SQL.format(
        cols=cols, since="" if since is None else " AND created_ts >= ?"
    )
    params = (pane,) if since is None else (pane, since)
    return conn.execute(sql, params).fetchone()


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Run _SCHEMA statement by statement. Not executescript(): that commits any
    pending transaction first, which would break _migrate's atomicity."""
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        is not None
    )


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Column names in declared order — order matters when copying a table."""
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing db up to SCHEMA_VERSION. Runs with foreign keys off and
    inside one transaction, so a crash mid-migration leaves the old db intact."""
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _table_exists(conn, "worktrees") and "id" not in _columns(conn, "worktrees"):
            cols = ", ".join(_columns(conn, "worktrees"))
            conn.execute(_WORKTREES_DDL.format(table="worktrees_v2"))
            conn.execute(
                f"INSERT INTO worktrees_v2 (id, {cols}) SELECT rowid, {cols} FROM worktrees"
            )
            conn.execute("DROP TABLE worktrees")
            conn.execute("ALTER TABLE worktrees_v2 RENAME TO worktrees")

        for table in ("notes", "events"):
            if not _table_exists(conn, table):
                continue
            cols = _columns(conn, table)
            if "worktree_id" not in cols:
                conn.execute(
                    f"ALTER TABLE {table}"
                    " ADD COLUMN worktree_id INTEGER REFERENCES worktrees(id)"
                )
            if "repo" not in cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN repo TEXT NOT NULL DEFAULT ''"
                )
            match = (
                "SELECT w.{col} FROM worktrees w"
                f" WHERE w.pane = {table}.pane AND w.workspace = {table}.workspace"
                f" AND w.task = {table}.task"
                " ORDER BY w.created_ts DESC, w.id DESC LIMIT 1"
            )
            conn.execute(
                f"UPDATE {table} SET"
                f"  worktree_id = ({match.format(col='id')}),"
                f"  repo = COALESCE(({match.format(col='repo')}), '')"
                "  WHERE worktree_id IS NULL"
            )

        # Schema 3. CREATE TABLE IF NOT EXISTS in _apply_schema cannot widen a
        # table that already exists, so existing worktrees need explicit ALTERs.
        # SQLite makes DDL transactional, so a failure below still rolls these
        # back and leaves a usable version 2 file.
        if _table_exists(conn, "worktrees"):
            existing = _columns(conn, "worktrees")
            for column, decl in _RUNTIME_COLUMNS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE worktrees ADD COLUMN {column} {decl}")

        _apply_schema(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=3.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    _migrate(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open a connection and *close* it.

    `with sqlite3.connect(...) as conn` commits; it does not close. Every call
    here used that form, so each one leaked an open connection until the
    garbage collector happened to finalise it — unbounded in the context
    service, which is long-lived and calls the store per request.

    The intermittent failure it caused is worth recording: closing a WAL
    connection checkpoints the log back into the main database file. A test
    that corrupted `context.db` and asserted the store was unopenable would
    pass, then a leaked connection from an earlier call would be finalised at
    an arbitrary moment, rewrite a valid header over the garbage, and the next
    process would open it happily.
    """
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _rows(
    conn: sqlite3.Connection, sql: str, params: tuple = ()
) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _attribution(
    conn: sqlite3.Connection,
    pane: str,
    worktree_id: int | None,
    repo: str | None,
    since: float | None = None,
) -> tuple[int | None, str]:
    """(worktree_id, repo) to store on a note or event. Callers holding the
    worktree row pass both; hook callers pass neither and get whatever worktree
    the pane fronts now, bounded by `since`."""
    if worktree_id is None and repo is None:
        row = _pane_worktree(conn, pane, "id, repo", since)
        return (row["id"], row["repo"]) if row else (None, "")
    return worktree_id, repo or ""


def schema_version(db_path: Path | None = None) -> int:
    """The store's schema version, after opening it.

    Opening migrates, so a store this build is merely *ahead* of reports the
    current version rather than an old one — the caller learns "compatible",
    not "stale". A version above `SCHEMA_VERSION` means a newer amux has been
    here and is left for the caller to judge: `_migrate` will not touch it.
    """
    with _session(db_path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def add_event(
    ts: float,
    pane: str,
    kind: str,
    workspace: str = "",
    task: str = "",
    agent: str = "",
    detail: str = "",
    worktree_id: int | None = None,
    repo: str | None = None,
    worktree_since: float | None = None,
    db_path: Path | None = None,
) -> int:
    """Record a state change. `worktree_id`/`repo` default to whatever worktree
    the pane fronts now, bounded by `worktree_since`."""
    with _session(db_path) as conn:
        worktree_id, repo = _attribution(conn, pane, worktree_id, repo, worktree_since)
        cur = conn.execute(
            "INSERT INTO events"
            " (ts, worktree_id, repo, workspace, task, pane, agent, kind, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, worktree_id, repo, workspace, task, pane, agent, kind, detail),
        )
        return cur.lastrowid or 0


def iter_events(
    pane: str | None = None,
    workspace: str | None = None,
    task: str | None = None,
    worktree_id: int | None = None,
    repo: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM events", ()
    clauses = []
    if pane is not None:
        clauses.append("pane = ?")
        params += (pane,)
    if workspace is not None:
        clauses.append("workspace = ?")
        params += (workspace,)
    if task is not None:
        clauses.append("task = ?")
        params += (task,)
    if worktree_id is not None:
        clauses.append("worktree_id = ?")
        params += (worktree_id,)
    if repo is not None:
        clauses.append("repo = ?")
        params += (repo,)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts, id"
    with _session(db_path) as conn:
        return _rows(conn, sql, params)


def latest_event(pane: str, db_path: Path | None = None) -> dict[str, Any] | None:
    """The pane's newest event, whichever incarnation wrote it. Callers bound it
    with `events.in_incarnation`."""
    with _session(db_path) as conn:
        rows = _rows(
            conn,
            "SELECT * FROM events WHERE pane = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (pane,),
        )
    return rows[0] if rows else None


def events_for_panes(
    panes: Sequence[str], since: float | None = None, db_path: Path | None = None
) -> list[dict[str, Any]]:
    """Events for several panes in one query, oldest first."""
    if not panes:
        return []
    sql = f"SELECT * FROM events WHERE pane IN ({', '.join('?' * len(panes))})"
    params: tuple = tuple(panes)
    if since is not None:
        sql += " AND ts >= ?"
        params += (since,)
    with _session(db_path) as conn:
        return _rows(conn, sql + " ORDER BY ts, id", params)


def _page(sql: str, params: tuple, after: int | None, limit: int) -> tuple[str, tuple]:
    """Append ordering and a limit, flipping direction for a cursor walk.

    Without `after` these queries answer "the latest N", so they sort newest
    first. A client resuming from a cursor wants the opposite — the *oldest*
    rows it has not seen yet — and combining a cursor with a newest-first limit
    silently drops the middle of a burst: the caller gets the newest N past the
    cursor and advances past everything older it never saw.

    Ordering by id rather than ts is deliberate. ts comes from the writer's
    clock, so two writers can interleave timestamps, but id is assigned by
    SQLite and is what makes the cursor monotonic.
    """
    if after is not None:
        # query_notes with no filters has no WHERE to hang the cursor off.
        joiner = "AND" if " WHERE " in sql else "WHERE"
        return (
            f"{sql} {joiner} id > ? ORDER BY id ASC LIMIT ?",
            (*params, after, limit),
        )
    return f"{sql} ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit)


# --- notes ---


def add_note(
    workspace: str,
    task: str,
    pane: str,
    text: str,
    scope: NoteScope = "task",
    kind: NoteKind = "note",
    agent: str = "",
    worktree_id: int | None = None,
    repo: str | None = None,
    ts: float | None = None,
    db_path: Path | None = None,
) -> int:
    """Publish a note. `worktree_id`/`repo` default to whatever worktree the pane
    fronts right now; pass them explicitly when the caller already resolved the
    row (an empty `repo` is how a caller says "there is none")."""
    if scope not in NOTE_SCOPES:
        raise ValueError(f"scope must be one of {NOTE_SCOPES}, got '{scope}'")
    if kind not in NOTE_KINDS:
        raise ValueError(f"kind must be one of {NOTE_KINDS}, got '{kind}'")
    if not text.strip():
        raise ValueError("note text is empty")
    with _session(db_path) as conn:
        worktree_id, repo = _attribution(conn, pane, worktree_id, repo)
        cur = conn.execute(
            "INSERT INTO notes"
            " (ts, worktree_id, repo, workspace, task, pane, agent, scope, kind, text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts or time.time(),
                worktree_id,
                repo,
                workspace,
                task,
                pane,
                agent,
                scope,
                kind,
                text,
            ),
        )
        return cur.lastrowid or 0


def visible_notes(
    workspace: str,
    task: str,
    pane: str,
    limit: int = 10,
    kind: str | None = None,
    repo: str | None = None,
    after: int | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Notes an agent may see: workspace notes, its task's notes, its own.

    `after` turns this into a cursor walk — see `_page` for why that flips the
    ordering.
    """
    sql = (
        "SELECT * FROM notes WHERE workspace = ? AND ("
        "  scope = 'workspace'"
        "  OR (scope = 'task' AND task = ?)"
        "  OR (scope = 'agent' AND task = ? AND pane = ?)"
        ")"
    )
    params: tuple = (workspace, task, task, pane)
    if kind is not None:
        sql += " AND kind = ?"
        params += (kind,)
    if repo:
        sql += " AND (repo = ? OR repo = '')"
        params += (repo,)
    sql, params = _page(sql, params, after, limit)
    with _session(db_path) as conn:
        return _rows(conn, sql, params)


def query_notes(
    workspace: str | None = None,
    task: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    pane: str | None = None,
    worktree_id: int | None = None,
    repo: str | None = None,
    limit: int = 20,
    after: int | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM notes", ()
    clauses = []
    if workspace is not None:
        clauses.append("workspace = ?")
        params += (workspace,)
    if task is not None:
        clauses.append("task = ?")
        params += (task,)
    if scope is not None:
        clauses.append("scope = ?")
        params += (scope,)
    if kind is not None:
        clauses.append("kind = ?")
        params += (kind,)
    if pane is not None:
        clauses.append("pane = ?")
        params += (pane,)
    if worktree_id is not None:
        clauses.append("worktree_id = ?")
        params += (worktree_id,)
    if repo:
        # Same rule as visible_notes, so a pane sees the same note set whether
        # or not --scope routed it here.
        clauses.append("(repo = ? OR repo = '')")
        params += (repo,)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql, params = _page(sql, params, after, limit)
    with _session(db_path) as conn:
        return _rows(conn, sql, params)


def register_worktree(
    pane: str,
    workspace: str,
    task: str,
    path: str,
    branch: str,
    agent: str = "",
    name: str = "",
    base_ref: str = "",
    repo: str = "",
    created_ts: float | None = None,
    runtime: Runtime = "host",
    runtime_status: str = "",
    sandbox_name: str = "",
    sandbox_id: str = "",
    socket_name: str = "",
    db_path: Path | None = None,
) -> int:
    """Append an execution record and return its id. Append-only on purpose: the
    old INSERT OR REPLACE keyed on pane erased the previous worktree, and every
    note and event that pointed at it, whenever tmux reissued the pane id.

    A `docker-sandbox` row describes a microVM rather than a directory, so it
    carries an empty `path`; callers must consult `runtime` before treating
    `path` as somewhere on disk.
    """
    if runtime not in RUNTIMES:
        raise ValueError(f"runtime must be one of {RUNTIMES}, got '{runtime}'")
    with _session(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO worktrees"
            " (pane, workspace, task, agent, name, path, branch, base_ref, repo,"
            "  status, created_ts, runtime, runtime_status, sandbox_name,"
            "  sandbox_id, socket_name)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
            (
                pane,
                workspace,
                task,
                agent,
                name,
                path,
                branch,
                base_ref,
                repo,
                created_ts or time.time(),
                runtime,
                runtime_status,
                sandbox_name,
                sandbox_id,
                socket_name,
            ),
        )
        return cur.lastrowid or 0


def worktree_for_pane(
    pane: str, since: float | None = None, db_path: Path | None = None
) -> dict[str, Any] | None:
    """The worktree a pane currently fronts. A pane id can head several rows once
    tmux recycles it, so active wins, then most recently created; `since` rules
    out rows the pane never owned."""
    with _session(db_path) as conn:
        row = _pane_worktree(conn, pane, "*", since)
    return dict(row) if row else None


def worktrees_for_panes(
    panes: Sequence[str], since: float | None = None, db_path: Path | None = None
) -> dict[str, dict[str, Any]]:
    """The worktree each pane fronts, in one query. Panes with none are absent.

    The bulk counterpart to `worktree_for_pane`, for the monitor's per-refresh
    view: resolving these one pane at a time would put a query per pane on
    every refresh. Selection matches the single-pane version exactly — active
    wins, then most recently created — so the two cannot disagree about which
    row a pane fronts.
    """
    if not panes:
        return {}
    placeholders = ", ".join("?" * len(panes))
    sql = f"SELECT * FROM worktrees WHERE pane IN ({placeholders})"
    params: tuple = tuple(panes)
    if since is not None:
        sql += " AND created_ts >= ?"
        params += (since,)
    # Ordered so the row each pane should front is the last one written into
    # the dict, mirroring `_pane_worktree`'s ORDER BY.
    sql += " ORDER BY (status = 'active') ASC, created_ts ASC, id ASC"
    with _session(db_path) as conn:
        return {row["pane"]: row for row in _rows(conn, sql, params)}


def worktree_by_id(
    worktree_id: int, db_path: Path | None = None
) -> dict[str, Any] | None:
    with _session(db_path) as conn:
        rows = _rows(conn, "SELECT * FROM worktrees WHERE id = ?", (worktree_id,))
    return rows[0] if rows else None


def worktrees_for(
    workspace: str,
    task: str | None = None,
    repo: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM worktrees WHERE workspace = ?"
    params: tuple = (workspace,)
    if task is not None:
        sql += " AND task = ?"
        params += (task,)
    if repo is not None:
        sql += " AND repo = ?"
        params += (repo,)
    sql += " ORDER BY created_ts"
    with _session(db_path) as conn:
        return _rows(conn, sql, params)


def set_worktree_status(
    worktree_id: int, status: WorktreeStatus, db_path: Path | None = None
) -> None:
    with _session(db_path) as conn:
        conn.execute(
            "UPDATE worktrees SET status = ? WHERE id = ?", (status, worktree_id)
        )


# A sandbox holds a plaintext token; the host keeps only its SHA-256. Every
# fact the context service attributes to a caller — workspace, task, repo,
# pane, agent, visibility — is read from the execution row this token is bound
# to, never from the request. That is what stops a sandbox claiming to be
# another agent.
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_context_token(
    worktree_id: int,
    permissions: Sequence[str] = (),
    ttl: float | None = None,
    now: float | None = None,
    db_path: Path | None = None,
) -> tuple[str, int]:
    """Create a capability for one execution and return `(plaintext, id)`."""
    for permission in permissions:
        if not permission.strip() or "," in permission:
            raise ValueError(
                f"permission must be non-empty and comma-free, got '{permission}'"
            )
    now = time.time() if now is None else now
    token = secrets.token_urlsafe(32)
    with _session(db_path) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM worktrees WHERE id = ?", (worktree_id,)
            ).fetchone()
            is None
        ):
            raise ValueError(f"no worktree with id {worktree_id}")
        cur = conn.execute(
            "INSERT INTO context_tokens"
            " (worktree_id, token_hash, permissions, created_ts, expires_ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                worktree_id,
                _hash_token(token),
                ",".join(permissions),
                now,
                None if ttl is None else now + ttl,
            ),
        )
        return token, cur.lastrowid or 0


def context_token_record(
    token: str, now: float | None = None, db_path: Path | None = None
) -> dict[str, Any] | None:
    """Resolve a plaintext token to its execution identity, or None."""
    if not token:
        return None
    now = time.time() if now is None else now
    digest = _hash_token(token)
    with _session(db_path) as conn:
        rows = _rows(
            conn,
            "SELECT t.id, t.worktree_id, t.token_hash, t.permissions,"
            "       t.created_ts, t.expires_ts, t.revoked_ts,"
            "       w.pane, w.workspace, w.task, w.agent, w.name, w.path,"
            "       w.branch, w.base_ref, w.repo, w.status, w.created_ts AS"
            "       worktree_created_ts, w.runtime, w.runtime_status,"
            "       w.sandbox_name, w.sandbox_id, w.socket_name"
            " FROM context_tokens t JOIN worktrees w ON w.id = t.worktree_id"
            " WHERE t.token_hash = ?",
            (digest,),
        )
    if not rows:
        return None
    record = rows[0]
    if not compare_digest(record["token_hash"], digest):
        return None
    if record["revoked_ts"] is not None:
        return None
    if record["expires_ts"] is not None and now >= record["expires_ts"]:
        return None
    record["permissions"] = tuple(p for p in record["permissions"].split(",") if p)
    del record["token_hash"]
    return record


def revoke_context_token(
    token_id: int, now: float | None = None, db_path: Path | None = None
) -> None:
    """Retire one capability. The row stays so an audit can see it existed."""
    with _session(db_path) as conn:
        conn.execute(
            "UPDATE context_tokens SET revoked_ts = ?"
            " WHERE id = ? AND revoked_ts IS NULL",
            (time.time() if now is None else now, token_id),
        )


def revoke_context_tokens_for_worktree(
    worktree_id: int, now: float | None = None, db_path: Path | None = None
) -> int:
    """Retire every capability an execution holds and return how many. Sandbox
    removal calls this: a removed sandbox must leave nothing that still
    authenticates."""
    with _session(db_path) as conn:
        cur = conn.execute(
            "UPDATE context_tokens SET revoked_ts = ?"
            " WHERE worktree_id = ? AND revoked_ts IS NULL",
            (time.time() if now is None else now, worktree_id),
        )
        return cur.rowcount


def set_worktree_runtime(
    worktree_id: int,
    runtime_status: str | None = None,
    sandbox_name: str | None = None,
    sandbox_id: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Update the VM-side lifecycle fields, leaving unnamed ones alone."""
    updates = {
        "runtime_status": runtime_status,
        "sandbox_name": sandbox_name,
        "sandbox_id": sandbox_id,
    }
    given = {k: v for k, v in updates.items() if v is not None}
    if not given:
        return
    assignments = ", ".join(f"{k} = ?" for k in given)
    with _session(db_path) as conn:
        conn.execute(
            f"UPDATE worktrees SET {assignments} WHERE id = ?",
            (*given.values(), worktree_id),
        )
