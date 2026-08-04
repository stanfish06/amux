"""Concurrency and visibility once a second writer exists.

Until now one process wrote to `context.db`. The context service adds a second,
writing on behalf of sandboxes while native host commands keep writing
directly. These tests pin the two properties that arrangement can break:
nothing is lost when both write at once, and the visibility rules a sandbox
caller goes through are exactly the ones a host caller goes through.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from amux import store


@pytest.fixture
def agents(db_path: Path) -> dict[str, int]:
    """Two host agents and one sandboxed agent in the same task."""
    return {
        "host_a": store.register_worktree(
            pane="%1",
            workspace="proj",
            task="task0",
            path="/tmp/wt-a",
            branch="amux/proj/task0/host-a",
            agent="claude",
            name="host-a",
            repo="/tmp/repo",
            db_path=db_path,
        ),
        "host_b": store.register_worktree(
            pane="%2",
            workspace="proj",
            task="task0",
            path="/tmp/wt-b",
            branch="amux/proj/task0/host-b",
            agent="claude",
            name="host-b",
            repo="/tmp/repo",
            db_path=db_path,
        ),
        "sandbox": store.register_worktree(
            pane="%3",
            workspace="proj",
            task="task0",
            path="",
            branch="amux/proj/task0/boxed",
            agent="codex",
            name="boxed",
            repo="/tmp/repo",
            runtime="docker-sandbox",
            runtime_status="running",
            sandbox_name="amux-proj-task0-boxed-a1b2",
            db_path=db_path,
        ),
    }


def _run_together(*fns) -> None:
    """Run callables on threads and re-raise the first failure."""
    errors: list[BaseException] = []

    def guarded(fn):
        def run() -> None:
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        return run

    threads = [threading.Thread(target=guarded(fn)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a writer thread hung"
    if errors:
        raise errors[0]


# --- concurrent writes ---


def test_native_and_service_notes_do_not_lose_writes(
    db_path: Path, agents: dict[str, int]
) -> None:
    """The host CLI and the context service are separate writers to one file."""

    def writer(pane: str, worktree_id: int, tag: str):
        def run() -> None:
            for i in range(25):
                store.add_note(
                    workspace="proj",
                    task="task0",
                    pane=pane,
                    text=f"{tag}-{i}",
                    scope="task",
                    worktree_id=worktree_id,
                    repo="/tmp/repo",
                    db_path=db_path,
                )

        return run

    _run_together(
        writer("%1", agents["host_a"], "native"),
        writer("%3", agents["sandbox"], "service"),
    )

    notes = store.query_notes(workspace="proj", limit=1000, db_path=db_path)
    texts = {n["text"] for n in notes}
    assert {f"native-{i}" for i in range(25)} <= texts
    assert {f"service-{i}" for i in range(25)} <= texts
    assert len(notes) == 50


def test_native_and_service_events_do_not_lose_writes(
    db_path: Path, agents: dict[str, int]
) -> None:
    def writer(pane: str, worktree_id: int, kind: str):
        def run() -> None:
            for _ in range(25):
                store.add_event(
                    ts=time.time(),
                    pane=pane,
                    kind=kind,
                    workspace="proj",
                    task="task0",
                    worktree_id=worktree_id,
                    repo="/tmp/repo",
                    db_path=db_path,
                )

        return run

    _run_together(
        writer("%1", agents["host_a"], "busy"),
        writer("%3", agents["sandbox"], "notify"),
    )

    events = store.iter_events(workspace="proj", db_path=db_path)
    assert len(events) == 50
    assert sum(e["kind"] == "busy" for e in events) == 25
    assert sum(e["kind"] == "notify" for e in events) == 25


def test_mixed_note_and_event_writers_interleave_safely(
    db_path: Path, agents: dict[str, int]
) -> None:
    def notes() -> None:
        for i in range(20):
            store.add_note(
                workspace="proj",
                task="task0",
                pane="%1",
                text=f"note-{i}",
                worktree_id=agents["host_a"],
                repo="/tmp/repo",
                db_path=db_path,
            )

    def events() -> None:
        for _ in range(20):
            store.add_event(
                ts=time.time(),
                pane="%3",
                kind="busy",
                workspace="proj",
                task="task0",
                worktree_id=agents["sandbox"],
                repo="/tmp/repo",
                db_path=db_path,
            )

    _run_together(notes, events)

    assert len(store.query_notes(workspace="proj", limit=1000, db_path=db_path)) == 20
    assert len(store.iter_events(workspace="proj", db_path=db_path)) == 20


def test_writer_waits_out_a_held_transaction(
    db_path: Path, agents: dict[str, int]
) -> None:
    """`busy_timeout` is what keeps a long service read from turning a native
    write into an error. Hold a write lock, then prove a second writer blocks
    and still succeeds rather than raising SQLITE_BUSY."""
    store.add_note(
        workspace="proj",
        task="task0",
        pane="%1",
        text="seed",
        worktree_id=agents["host_a"],
        db_path=db_path,
    )

    blocker = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    blocker.execute("PRAGMA busy_timeout=5000")
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute(
        "INSERT INTO notes (ts, worktree_id, repo, workspace, task, pane, agent,"
        " scope, kind, text) VALUES (1.0, NULL, '', 'proj', 'task0', '%9', '',"
        " 'task', 'note', 'held')"
    )

    released = threading.Event()

    def late_writer() -> None:
        store.add_note(
            workspace="proj",
            task="task0",
            pane="%3",
            text="after-lock",
            worktree_id=agents["sandbox"],
            db_path=db_path,
        )
        released.set()

    thread = threading.Thread(target=late_writer)
    thread.start()
    try:
        time.sleep(0.25)
        assert not released.is_set(), "writer should have been blocked by the lock"
        blocker.execute("COMMIT")
    finally:
        blocker.close()

    thread.join(timeout=10)
    assert released.is_set(), "writer never completed after the lock was released"
    texts = {n["text"] for n in store.query_notes(workspace="proj", limit=100, db_path=db_path)}
    assert {"seed", "held", "after-lock"} <= texts


def test_note_ids_are_monotonic_under_concurrency(
    db_path: Path, agents: dict[str, int]
) -> None:
    """The service hands note and event ids to clients as resume cursors, so
    they have to increase even when two writers race."""
    ids: list[int] = []
    lock = threading.Lock()

    def writer(pane: str, worktree_id: int):
        def run() -> None:
            for i in range(20):
                note_id = store.add_note(
                    workspace="proj",
                    task="task0",
                    pane=pane,
                    text=f"{pane}-{i}",
                    worktree_id=worktree_id,
                    db_path=db_path,
                )
                with lock:
                    ids.append(note_id)

        return run

    _run_together(writer("%1", agents["host_a"]), writer("%3", agents["sandbox"]))

    assert len(set(ids)) == 40
    assert sorted(ids) == list(range(min(ids), min(ids) + 40))


# --- visibility is unchanged ---


def test_agent_scoped_notes_stay_private_to_their_pane(
    db_path: Path, agents: dict[str, int]
) -> None:
    """The rule a sandbox caller must not be able to talk its way around."""
    store.add_note(
        workspace="proj",
        task="task0",
        pane="%3",
        text="sandbox private",
        scope="agent",
        worktree_id=agents["sandbox"],
        repo="/tmp/repo",
        db_path=db_path,
    )

    mine = store.visible_notes("proj", "task0", "%3", repo="/tmp/repo", db_path=db_path)
    assert [n["text"] for n in mine] == ["sandbox private"]

    for pane in ("%1", "%2"):
        theirs = store.visible_notes(
            "proj", "task0", pane, repo="/tmp/repo", db_path=db_path
        )
        assert theirs == []


def test_sandbox_and_host_agents_see_the_same_task_notes(
    db_path: Path, agents: dict[str, int]
) -> None:
    store.add_note(
        workspace="proj",
        task="task0",
        pane="%1",
        text="from a host agent",
        scope="task",
        worktree_id=agents["host_a"],
        repo="/tmp/repo",
        db_path=db_path,
    )
    store.add_note(
        workspace="proj",
        task="task0",
        pane="%3",
        text="from a sandboxed agent",
        scope="task",
        worktree_id=agents["sandbox"],
        repo="/tmp/repo",
        db_path=db_path,
    )

    seen = {
        pane: {
            n["text"]
            for n in store.visible_notes(
                "proj", "task0", pane, repo="/tmp/repo", db_path=db_path
            )
        }
        for pane in ("%1", "%3")
    }
    assert seen["%1"] == seen["%3"] == {"from a host agent", "from a sandboxed agent"}


def test_task_notes_do_not_cross_tasks(db_path: Path, agents: dict[str, int]) -> None:
    other = store.register_worktree(
        pane="%4",
        workspace="proj",
        task="task1",
        path="/tmp/wt-c",
        branch="amux/proj/task1/c",
        repo="/tmp/repo",
        db_path=db_path,
    )
    store.add_note(
        workspace="proj",
        task="task0",
        pane="%1",
        text="task0 only",
        scope="task",
        worktree_id=agents["host_a"],
        repo="/tmp/repo",
        db_path=db_path,
    )
    visible = store.visible_notes(
        "proj", "task1", "%4", repo="/tmp/repo", db_path=db_path
    )
    assert visible == []
    assert other > 0


def test_workspace_notes_reach_every_task(db_path: Path, agents: dict[str, int]) -> None:
    store.register_worktree(
        pane="%4",
        workspace="proj",
        task="task1",
        path="/tmp/wt-c",
        branch="b",
        repo="/tmp/repo",
        db_path=db_path,
    )
    store.add_note(
        workspace="proj",
        task="task0",
        pane="%3",
        text="everyone should see this",
        scope="workspace",
        worktree_id=agents["sandbox"],
        repo="/tmp/repo",
        db_path=db_path,
    )
    visible = store.visible_notes(
        "proj", "task1", "%4", repo="/tmp/repo", db_path=db_path
    )
    assert [n["text"] for n in visible] == ["everyone should see this"]


def test_repository_filtering_still_isolates_unrelated_repos(
    db_path: Path, agents: dict[str, int]
) -> None:
    """Same workspace name, different checkout. A sandbox must not read another
    repository's notes just because the workspace strings match."""
    elsewhere = store.register_worktree(
        pane="%9",
        workspace="proj",
        task="task0",
        path="",
        branch="b",
        repo="/tmp/other-repo",
        runtime="docker-sandbox",
        db_path=db_path,
    )
    store.add_note(
        workspace="proj",
        task="task0",
        pane="%9",
        text="other repo secret",
        scope="task",
        worktree_id=elsewhere,
        repo="/tmp/other-repo",
        db_path=db_path,
    )
    store.add_note(
        workspace="proj",
        task="task0",
        pane="%1",
        text="this repo",
        scope="task",
        worktree_id=agents["host_a"],
        repo="/tmp/repo",
        db_path=db_path,
    )

    here = store.visible_notes(
        "proj", "task0", "%1", repo="/tmp/repo", db_path=db_path
    )
    assert [n["text"] for n in here] == ["this repo"]


def test_unattributed_notes_remain_visible_to_every_repo(
    db_path: Path, agents: dict[str, int]
) -> None:
    """A note with no repo predates repo attribution or came from outside a
    checkout. The existing rule shows it everywhere; keep it that way."""
    store.add_note(
        workspace="proj",
        task="task0",
        pane="%1",
        text="repo-less",
        scope="task",
        worktree_id=agents["host_a"],
        repo="",
        db_path=db_path,
    )
    visible = store.visible_notes(
        "proj", "task0", "%1", repo="/tmp/repo", db_path=db_path
    )
    assert [n["text"] for n in visible] == ["repo-less"]


def test_sandbox_rows_are_returned_by_existing_queries(
    db_path: Path, agents: dict[str, int]
) -> None:
    """`worktrees_for` backs the roster. A sandbox agent has to appear on it
    alongside host agents or the team view silently loses members."""
    rows = store.worktrees_for("proj", task="task0", db_path=db_path)
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"host-a", "host-b", "boxed"}
    assert by_name["host-a"]["runtime"] == "host"
    assert by_name["boxed"]["runtime"] == "docker-sandbox"
    assert by_name["boxed"]["sandbox_name"] == "amux-proj-task0-boxed-a1b2"
