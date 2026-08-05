"""Schema version 2 to version 3 migration.

The version 2 DDL is frozen here on purpose. Importing it from `store` would
make these tests follow the schema as it changes and quietly stop testing the
upgrade at all — the one thing they exist to cover.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from amux import store

# --- a frozen version 2 database ---

_V2_SCHEMA = """
CREATE TABLE worktrees (
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
CREATE TABLE events (
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
CREATE TABLE notes (
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
"""

_V3_WORKTREE_COLUMNS = {
    "runtime",
    "runtime_status",
    "sandbox_name",
    "sandbox_id",
    "socket_name",
}


@pytest.fixture
def v2_db(db_path: Path) -> Path:
    """A populated version 2 database, exactly as an older amux left it."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executescript(_V2_SCHEMA)
    conn.execute(
        "INSERT INTO worktrees"
        " (id, pane, workspace, task, agent, name, path, branch, base_ref, repo,"
        "  status, created_ts)"
        " VALUES (1, '%7', 'proj', 'task0', 'claude', 'brave-hawk',"
        "         '/tmp/wt', 'amux/proj/task0/brave-hawk', 'main', '/tmp/repo',"
        "         'active', 100.0)",
    )
    conn.execute(
        "INSERT INTO notes (ts, worktree_id, repo, workspace, task, pane, agent,"
        " scope, kind, text)"
        " VALUES (101.0, 1, '/tmp/repo', 'proj', 'task0', '%7', 'claude',"
        "         'task', 'decision', 'use sqlite')",
    )
    conn.execute(
        "INSERT INTO events (ts, worktree_id, repo, workspace, task, pane, agent,"
        " kind, detail)"
        " VALUES (102.0, 1, '/tmp/repo', 'proj', 'task0', '%7', 'claude',"
        "         'busy', 'Bash')",
    )
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    return db_path


def _columns(db: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# --- the upgrade ---


def test_v2_database_upgrades_to_version_3(v2_db: Path) -> None:
    store.worktree_by_id(1, db_path=v2_db)
    assert _user_version(v2_db) == 3
    assert store.SCHEMA_VERSION == 3


def test_migration_adds_every_runtime_column(v2_db: Path) -> None:
    store.worktree_by_id(1, db_path=v2_db)
    assert _V3_WORKTREE_COLUMNS <= _columns(v2_db, "worktrees")


def test_pre_existing_row_reads_as_host_runtime(v2_db: Path) -> None:
    """The whole point of the additive migration: an agent recorded before
    sandboxes existed is a host agent, not an unknown one."""
    row = store.worktree_by_id(1, db_path=v2_db)
    assert row is not None
    assert row["runtime"] == "host"
    assert row["runtime_status"] == ""
    assert row["sandbox_name"] == ""
    assert row["sandbox_id"] == ""
    assert row["socket_name"] == ""


def test_migration_preserves_existing_data(v2_db: Path) -> None:
    row = store.worktree_by_id(1, db_path=v2_db)
    assert row is not None
    assert row["pane"] == "%7"
    assert row["name"] == "brave-hawk"
    assert row["branch"] == "amux/proj/task0/brave-hawk"
    assert row["created_ts"] == 100.0

    notes = store.query_notes(workspace="proj", db_path=v2_db)
    assert [n["text"] for n in notes] == ["use sqlite"]
    events = store.iter_events(workspace="proj", db_path=v2_db)
    assert [e["kind"] for e in events] == ["busy"]


def test_migration_is_idempotent(v2_db: Path) -> None:
    for _ in range(3):
        store.worktree_by_id(1, db_path=v2_db)
    assert _user_version(v2_db) == 3
    assert len(store.worktrees_for("proj", db_path=v2_db)) == 1


def test_migration_leaves_version_2_intact_when_it_fails(
    v2_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-applied schema is worse than an unapplied one, so the migration
    runs in one transaction. If it raises, the file must still be usable v2."""

    def boom(conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_apply_schema", boom)
    with pytest.raises(sqlite3.OperationalError):
        store.worktree_by_id(1, db_path=v2_db)

    assert _user_version(v2_db) == 2
    assert not _V3_WORKTREE_COLUMNS & _columns(v2_db, "worktrees")

    monkeypatch.undo()
    row = store.worktree_by_id(1, db_path=v2_db)
    assert row is not None and row["name"] == "brave-hawk"


# --- a fresh database ---


def test_fresh_database_is_created_at_version_3(db_path: Path) -> None:
    store.register_worktree(
        pane="%1",
        workspace="proj",
        task="task0",
        path="/tmp/wt",
        branch="amux/proj/task0/a",
        db_path=db_path,
    )
    assert _user_version(db_path) == 3
    assert _V3_WORKTREE_COLUMNS <= _columns(db_path, "worktrees")


def test_old_style_registration_still_defaults_to_host(db_path: Path) -> None:
    """Every existing caller passes no runtime argument. They must keep working
    unchanged and land on the host runtime."""
    wid = store.register_worktree(
        pane="%1",
        workspace="proj",
        task="task0",
        path="/tmp/wt",
        branch="amux/proj/task0/a",
        agent="claude",
        name="brave-hawk",
        base_ref="main",
        repo="/tmp/repo",
        db_path=db_path,
    )
    row = store.worktree_by_id(wid, db_path=db_path)
    assert row is not None
    assert row["runtime"] == "host"
    assert row["status"] == "active"
    assert row["sandbox_name"] == ""


def test_sandbox_registration_records_runtime_identity(db_path: Path) -> None:
    wid = store.register_worktree(
        pane="%2",
        workspace="proj",
        task="task0",
        path="",
        branch="amux/proj/task0/swift-crane",
        agent="claude",
        name="swift-crane",
        repo="/tmp/repo",
        runtime="docker-sandbox",
        runtime_status="created",
        sandbox_name="amux-proj-task0-swift-crane-a1b2c3",
        sandbox_id="sbx_0192",
        socket_name="amux-root",
        db_path=db_path,
    )
    row = store.worktree_by_id(wid, db_path=db_path)
    assert row is not None
    assert row["runtime"] == "docker-sandbox"
    assert row["runtime_status"] == "created"
    assert row["sandbox_name"] == "amux-proj-task0-swift-crane-a1b2c3"
    assert row["sandbox_id"] == "sbx_0192"
    assert row["socket_name"] == "amux-root"
    # A sandbox row has no host worktree; runtime-aware code must not treat
    # the empty path as a directory.
    assert row["path"] == ""


def test_runtime_must_be_known(db_path: Path) -> None:
    with pytest.raises(ValueError, match="runtime"):
        store.register_worktree(
            pane="%3",
            workspace="proj",
            task="task0",
            path="",
            branch="b",
            runtime="podman",  # type: ignore[arg-type]
            db_path=db_path,
        )


def test_runtime_status_updates_independently_of_status(db_path: Path) -> None:
    """`status` tracks amux's merge lifecycle, `runtime_status` tracks the VM.
    They move for different reasons and must not overwrite each other."""
    wid = store.register_worktree(
        pane="%2",
        workspace="proj",
        task="task0",
        path="",
        branch="b",
        runtime="docker-sandbox",
        runtime_status="created",
        db_path=db_path,
    )
    store.set_worktree_runtime(
        wid, runtime_status="running", sandbox_id="sbx_0192", db_path=db_path
    )
    row = store.worktree_by_id(wid, db_path=db_path)
    assert row is not None
    assert (row["runtime_status"], row["sandbox_id"]) == ("running", "sbx_0192")
    assert row["status"] == "active"

    store.set_worktree_status(wid, "merged", db_path=db_path)
    row = store.worktree_by_id(wid, db_path=db_path)
    assert row is not None
    assert row["status"] == "merged"
    assert row["runtime_status"] == "running"


def test_set_worktree_runtime_leaves_unnamed_fields_alone(db_path: Path) -> None:
    wid = store.register_worktree(
        pane="%2",
        workspace="proj",
        task="task0",
        path="",
        branch="b",
        runtime="docker-sandbox",
        runtime_status="created",
        sandbox_name="box",
        sandbox_id="sbx_1",
        db_path=db_path,
    )
    store.set_worktree_runtime(wid, runtime_status="stopped", db_path=db_path)
    row = store.worktree_by_id(wid, db_path=db_path)
    assert row is not None
    assert row["sandbox_name"] == "box"
    assert row["sandbox_id"] == "sbx_1"


# --- version reporting ---


def test_schema_version_reports_the_current_version(db_path: Path) -> None:
    assert store.schema_version(db_path=db_path) == store.SCHEMA_VERSION


def test_schema_version_migrates_an_old_store_before_reporting(v2_db: Path) -> None:
    """The context service gates on this. Reporting a stale version for a store
    that would migrate on first use would make a healthy host look
    incompatible and refuse to serve."""
    assert store.schema_version(db_path=v2_db) == store.SCHEMA_VERSION
    assert _user_version(v2_db) == store.SCHEMA_VERSION


def test_schema_version_reports_a_newer_store_verbatim(db_path: Path) -> None:
    """A store written by a newer amux is left alone by `_migrate`, so callers
    can see they are behind and decide for themselves."""
    store.register_worktree(
        pane="%1", workspace="proj", task="task0", path="", branch="b",
        db_path=db_path,
    )
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
    conn.close()

    assert store.schema_version(db_path=db_path) == store.SCHEMA_VERSION + 1
