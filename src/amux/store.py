"""Scoped context store: events, notes, and the worktree registry.

One SQLite file at $XDG_STATE_HOME/amux/context.db. WAL + busy_timeout so
concurrent agents can read/write without tripping over each other.

Identity: a worktree owns a surrogate `id` that nothing recycles, and notes and
events carry that id plus the `repo` they belong to. Pane ids are *not* stable —
tmux keeps its `%N` counter in the server process, so a restarted amux-root
hands `%67` back out to an unrelated agent. Anything keyed on a pane alone
silently merges two different worktrees' history.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from amux.shared import STATE_DIR

DB_PATH = STATE_DIR / "context.db"

SCHEMA_VERSION = 2

NoteScope = Literal["agent", "task", "workspace"]
NoteKind = Literal["note", "decision", "finding", "blocker"]
WorktreeStatus = Literal["active", "merged", "removed"]

NOTE_SCOPES = ("agent", "task", "workspace")
NOTE_KINDS = ("note", "decision", "finding", "blocker")

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
  created_ts REAL NOT NULL
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
"""
)


_PANE_WORKTREE_SQL = (
    "SELECT {cols} FROM worktrees WHERE pane = ?"
    " ORDER BY (status = 'active') DESC, created_ts DESC, id DESC LIMIT 1"
)


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


def _rows(
    conn: sqlite3.Connection, sql: str, params: tuple = ()
) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _attribution(
    conn: sqlite3.Connection, pane: str, worktree_id: int | None, repo: str | None
) -> tuple[int | None, str]:
    """(worktree_id, repo) to store on a note or event. Callers holding the
    worktree row pass both; hook callers such as events.emit pass neither and
    get whatever worktree the pane fronts right now."""
    if worktree_id is None and repo is None:
        row = conn.execute(
            _PANE_WORKTREE_SQL.format(cols="id, repo"), (pane,)
        ).fetchone()
        return (row["id"], row["repo"]) if row else (None, "")
    return worktree_id, repo or ""


# --- events ---


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
    db_path: Path | None = None,
) -> int:
    """Record a state change. `worktree_id`/`repo` default to whatever worktree
    the pane fronts right now, so hook callers need not know either."""
    with _connect(db_path) as conn:
        worktree_id, repo = _attribution(conn, pane, worktree_id, repo)
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
    with _connect(db_path) as conn:
        return _rows(conn, sql, params)


def latest_event(pane: str, db_path: Path | None = None) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        rows = _rows(
            conn,
            "SELECT * FROM events WHERE pane = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (pane,),
        )
    return rows[0] if rows else None


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
    fronts right now; pass them explicitly when the caller already has the row."""
    if scope not in NOTE_SCOPES:
        raise ValueError(f"scope must be one of {NOTE_SCOPES}, got '{scope}'")
    if kind not in NOTE_KINDS:
        raise ValueError(f"kind must be one of {NOTE_KINDS}, got '{kind}'")
    if not text.strip():
        raise ValueError("note text is empty")
    with _connect(db_path) as conn:
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
    repo: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Notes an agent may see: workspace notes, its task's notes, its own."""
    sql = (
        "SELECT * FROM notes WHERE workspace = ? AND ("
        "  scope = 'workspace'"
        "  OR (scope = 'task' AND task = ?)"
        "  OR (scope = 'agent' AND task = ? AND pane = ?)"
        ")"
    )
    params: tuple = (workspace, task, task, pane)
    if repo:
        sql += " AND (repo = ? OR repo = '')"
        params += (repo,)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params += (limit,)
    with _connect(db_path) as conn:
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
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params += (limit,)
    with _connect(db_path) as conn:
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
    db_path: Path | None = None,
) -> int:
    """Append a worktree and return its id. Append-only on purpose: the old
    INSERT OR REPLACE keyed on pane erased the previous worktree, and every note
    and event that pointed at it, whenever tmux reissued the pane id."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO worktrees"
            " (pane, workspace, task, agent, name, path, branch, base_ref, repo,"
            "  status, created_ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
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
            ),
        )
        return cur.lastrowid or 0


def worktree_for_pane(pane: str, db_path: Path | None = None) -> dict[str, Any] | None:
    """The worktree a pane currently fronts. A pane id can head several rows once
    tmux recycles it, so active wins, then most recently created."""
    with _connect(db_path) as conn:
        rows = _rows(conn, _PANE_WORKTREE_SQL.format(cols="*"), (pane,))
    return rows[0] if rows else None


def worktree_by_id(
    worktree_id: int, db_path: Path | None = None
) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
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
    with _connect(db_path) as conn:
        return _rows(conn, sql, params)


def set_worktree_status(
    worktree_id: int, status: WorktreeStatus, db_path: Path | None = None
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE worktrees SET status = ? WHERE id = ?", (status, worktree_id)
        )
