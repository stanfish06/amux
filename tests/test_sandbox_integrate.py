"""Integrating a sandboxed agent's committed work.

A sandboxed agent commits inside its own clone. That clone reaches the host
only through the `sandbox-<name>` remote Docker publishes, so `integrate` has
to fetch before it can merge -- and only committed work can cross, which is the
intended boundary rather than a limitation.

The remote is a real second git repository here, not a fake. Docker's remote is
an ordinary git remote from the host's point of view, so exercising the actual
fetch-then-merge is both possible offline and much stronger than asserting on
recorded commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amux import store, worktree
from test_host_grid_snapshot import git

SANDBOX = "amux-ws-t0-alpha-deadbeef"
BRANCH = "amux/ws/t0/alpha"


@pytest.fixture
def task(git_repo):
    """A task integration worktree, ready to merge into."""
    return worktree.setup_task_integration(str(git_repo), "ws", "t0")


@pytest.fixture
def clone(git_repo, tmp_path):
    """A stand-in for the sandbox's private clone, wired up as Docker would.

    `sbx create --clone` gives the host a `sandbox-<name>` remote pointing at
    the VM's repository; from the host that is just a git remote.
    """
    path = tmp_path / "sandbox-clone"
    git(git_repo.parent, "clone", "-q", str(git_repo), str(path))
    git(git_repo, "remote", "add", worktree.sandbox_remote(SANDBOX), str(path))
    return path


def register(git_repo, *, name="alpha", sandbox_name=SANDBOX, runtime="docker-sandbox"):
    return store.register_worktree(
        pane="%1",
        workspace="ws",
        task="t0",
        agent="claude",
        name=name,
        path="",
        branch=worktree.agent_branch("ws", "t0", name),
        base_ref=worktree.head_ref(str(git_repo)),
        repo=str(git_repo),
        runtime=runtime,
        runtime_status="running",
        sandbox_name=sandbox_name,
        sandbox_id="sbx_1",
        socket_name="amux-root",
    )


def commit_in(clone, branch, filename, text, message):
    git(clone, "checkout", "-q", "-b", branch)
    (clone / filename).write_text(text)
    git(clone, "add", filename)
    git(clone, "commit", "-qm", message)


def notes():
    return store.query_notes(workspace="ws", task="t0", limit=50)


# --- the success path ---


def test_a_committed_sandbox_branch_integrates(git_repo, task, clone):
    commit_in(clone, BRANCH, "from_sandbox.txt", "work\n", "sandbox work")
    wt_id = register(git_repo)

    (result,) = worktree.integrate("ws", "t0")

    assert result.ok
    assert result.commits == 1
    assert "1 file changed" in result.shortstat
    # The file really landed in the integration worktree.
    assert (Path(task.path) / "from_sandbox.txt").read_text() == "work\n"
    assert store.worktrees_for("ws", "t0")[0]["status"] == "merged"
    assert any("merged alpha" in n["text"] for n in notes())
    assert wt_id


def test_the_fetched_tip_is_kept_in_a_durable_local_ref(git_repo, task, clone):
    """Cleanup depends on this: the ref is what survives `sbx rm`."""
    commit_in(clone, BRANCH, "f.txt", "x\n", "sandbox work")
    register(git_repo)
    worktree.integrate("ws", "t0")

    ref = worktree.sandbox_tracking_ref(SANDBOX, BRANCH)
    assert git(git_repo, "rev-parse", "--verify", ref)
    # Namespaced away from refs/heads so it cannot collide with a host agent's
    # identically named branch.
    assert ref.startswith("refs/amux/sandboxes/")
    assert BRANCH not in git(
        git_repo, "for-each-ref", "--format=%(refname:short)", "refs/heads"
    ).splitlines()


def test_a_merge_commit_is_made_even_for_a_single_commit(git_repo, task, clone):
    """`--no-ff` semantics are shared with host agents, not re-invented."""
    commit_in(clone, BRANCH, "f.txt", "x\n", "sandbox work")
    register(git_repo)
    before = git(task.path, "rev-parse", "HEAD")
    worktree.integrate("ws", "t0")
    after = git(task.path, "rev-parse", "HEAD")

    assert before != after
    assert len(git(task.path, "rev-list", "--parents", "-1", "HEAD").split()) == 3


# --- nothing to integrate ---


def test_a_branch_with_no_delta_reports_no_changes(git_repo, task, clone):
    """The branch exists and is reachable but has nothing beyond the base."""
    git(clone, "checkout", "-q", "-b", BRANCH)
    git(clone, "push", "-q", "origin", BRANCH)
    register(git_repo)

    (result,) = worktree.integrate("ws", "t0")

    assert result.ok
    assert result.commits == 0
    assert result.shortstat == ""
    assert any("no changes" in n["text"] for n in notes())


def test_uncommitted_sandbox_files_are_not_integrated(git_repo, task, clone):
    """Only committed work crosses the boundary; this is the boundary working."""
    commit_in(clone, BRANCH, "committed.txt", "in\n", "committed work")
    (clone / "uncommitted.txt").write_text("out\n")
    register(git_repo)

    (result,) = worktree.integrate("ws", "t0")

    assert result.ok
    assert (Path(task.path) / "committed.txt").exists()
    assert not (Path(task.path) / "uncommitted.txt").exists()


# --- failure paths ---


def test_a_sandbox_that_never_committed_the_branch_is_reported(git_repo, task, clone):
    """The remote is reachable, but the branch is not there."""
    register(git_repo)

    (result,) = worktree.integrate("ws", "t0")

    assert not result.ok
    assert result.error
    assert store.worktrees_for("ws", "t0")[0]["status"] == "active"
    assert any("cannot reach alpha" in n["text"] for n in notes())


def test_a_stopped_or_removed_sandbox_is_reported_not_guessed(git_repo, task):
    """No remote at all: the sandbox is gone or was never created."""
    register(git_repo)

    (result,) = worktree.integrate("ws", "t0")

    assert not result.ok
    assert result.error
    # Still active, so the user can restart the sandbox and retry.
    assert store.worktrees_for("ws", "t0")[0]["status"] == "active"
    blockers = [n for n in notes() if n["kind"] == "blocker"]
    assert blockers and "cannot reach" in blockers[0]["text"]


def test_a_row_without_a_recorded_sandbox_name_is_refused(git_repo, task, clone):
    register(git_repo, sandbox_name="")

    (result,) = worktree.integrate("ws", "t0")

    assert not result.ok
    assert "no sandbox name recorded" in result.error


def test_a_conflicting_sandbox_branch_aborts_and_blocks(git_repo, task, clone):
    commit_in(clone, BRANCH, "shared.txt", "from sandbox\n", "sandbox edit")
    # The integration branch touches the same file first.
    worktree_path = task.path
    (Path(worktree_path) / "shared.txt").write_text("from integration\n")
    git(worktree_path, "add", "shared.txt")
    git(worktree_path, "commit", "-qm", "integration edit")
    register(git_repo)

    (result,) = worktree.integrate("ws", "t0")

    assert not result.ok
    assert result.error
    # Aborted cleanly: nothing left half-merged in the working tree.
    status = git(worktree_path, "status", "--porcelain")
    assert status == ""
    # The row stays active and the sandbox's commits are untouched.
    assert store.worktrees_for("ws", "t0")[0]["status"] == "active"
    blockers = [n for n in notes() if n["kind"] == "blocker"]
    assert any("conflict merging alpha" in n["text"] for n in blockers)


# --- mixed grids ---


def test_host_and_sandbox_agents_integrate_in_one_pass(git_repo, task, clone):
    """A task can hold both; each is merged from where its branch actually is."""
    commit_in(clone, BRANCH, "sandbox.txt", "s\n", "sandbox work")
    register(git_repo)

    # A host agent alongside it, with a real worktree.
    worktree.setup_host_agents(task, [("%2", "codex", "beta")])
    host_path = f"{worktree.task_worktree_root('ws', 't0')}/beta"
    (Path(host_path) / "host.txt").write_text("h\n")
    git(host_path, "add", "host.txt")
    git(host_path, "commit", "-qm", "host work")

    results = {r.name: r for r in worktree.integrate("ws", "t0")}

    assert set(results) == {"alpha", "beta"}
    assert all(r.ok for r in results.values())
    assert (Path(task.path) / "sandbox.txt").exists()
    assert (Path(task.path) / "host.txt").exists()


def test_one_unreachable_sandbox_does_not_stop_the_others(git_repo, task, clone):
    commit_in(clone, BRANCH, "sandbox.txt", "s\n", "sandbox work")
    register(git_repo)
    # A second sandbox row whose remote does not exist.
    register(git_repo, name="gamma", sandbox_name="amux-ws-t0-gamma-cafebabe")

    results = {r.name: r for r in worktree.integrate("ws", "t0")}

    assert results["alpha"].ok
    assert not results["gamma"].ok
    rows = {r["name"]: r["status"] for r in store.worktrees_for("ws", "t0")}
    assert rows["alpha"] == "merged"
    assert rows["gamma"] == "active"
