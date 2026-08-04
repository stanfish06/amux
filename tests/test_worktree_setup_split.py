"""Splitting task-integration setup from per-agent setup.

`setup_task` used to do both jobs in one function with one rollback block.
Sandboxed agents need the first half (they branch off the same task integration
line) but not the second (they get clones, not host worktrees), so the halves
are now `setup_task_integration` and `setup_host_agents`.

The thing that must not regress is atomicity: a failure at any point leaves no
worktree on disk, no worktree registered as active, and no half-built task. The
one thing deliberately *not* rolled back is branches — see
`remove_task_integration`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amux import store, worktree
from test_host_grid_snapshot import (  # noqa: F401 - fixtures
    git,
    isolated_state,
    repo,
)


def branches(repo: Path) -> set[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return set(out.splitlines())


def worktree_paths(repo: Path) -> set[str]:
    """Paths git itself still considers worktrees of this repo."""
    out = git(repo, "worktree", "list", "--porcelain")
    return {
        line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")
    }


def task_root() -> Path:
    return Path(worktree.task_worktree_root("ws", "t0"))


def statuses() -> list[str]:
    return [row["status"] for row in store.worktrees_for("ws", "t0")]


def test_integration_setup_is_self_contained(repo, isolated_state):
    integration = worktree.setup_task_integration(str(repo), "ws", "t0")

    assert integration.branch == "amux/ws/t0/integration"
    assert integration.base_ref == git(repo, "rev-parse", "HEAD")
    assert Path(integration.path).is_dir()
    assert integration.branch in branches(repo)
    # It registers nothing: the registry row belongs to an agent, not a task.
    assert store.worktrees_for("ws", "t0") == []


def test_integration_setup_is_retryable_after_removal(repo, isolated_state):
    first = worktree.setup_task_integration(str(repo), "ws", "t0")
    worktree.remove_task_integration(first)

    assert not Path(first.path).exists()
    # The branch survives on purpose, so the retry must reuse it rather than
    # fail trying to create it again.
    assert first.branch in branches(repo)
    second = worktree.setup_task_integration(str(repo), "ws", "t0")
    assert Path(second.path).is_dir()
    assert second.branch == first.branch


def test_integration_setup_refuses_a_repo_without_commits(tmp_path, isolated_state):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    with pytest.raises(worktree.WorktreeError, match="no commits"):
        worktree.setup_task_integration(str(empty), "ws", "t0")


def test_agent_setup_rolls_back_its_own_worktrees_and_rows(repo, isolated_state):
    """A duplicate agent name fails the second `worktree add`."""
    integration = worktree.setup_task_integration(str(repo), "ws", "t0")

    with pytest.raises(worktree.WorktreeError):
        worktree.setup_host_agents(
            integration,
            [("%1", "claude", "alpha"), ("%2", "codex", "alpha")],
        )

    # No agent worktree survives, and no row is left active for `integrate` to
    # merge a branch whose worktree is gone.
    assert not (task_root() / "alpha").exists()
    assert statuses() == ["removed"]
    assert worktree_paths(repo) == {str(repo), integration.path}
    # The caller owns the integration worktree, so this half must not touch it.
    assert Path(integration.path).is_dir()


def test_agent_setup_rolls_back_when_the_registry_fails(
    repo, isolated_state, monkeypatch
):
    """The worktree is on disk before the row exists; rollback must undo both."""
    integration = worktree.setup_task_integration(str(repo), "ws", "t0")
    real = store.register_worktree
    calls = {"n": 0}

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("registry unavailable")
        return real(**kwargs)

    monkeypatch.setattr(store, "register_worktree", flaky)

    with pytest.raises(RuntimeError, match="registry unavailable"):
        worktree.setup_host_agents(
            integration,
            [("%1", "claude", "alpha"), ("%2", "codex", "beta")],
        )

    assert not (task_root() / "alpha").exists()
    assert not (task_root() / "beta").exists()
    assert statuses() == ["removed"]
    assert worktree_paths(repo) == {str(repo), integration.path}


def test_setup_task_rolls_back_both_halves(repo, isolated_state):
    with pytest.raises(worktree.WorktreeError):
        worktree.setup_task(
            str(repo), "ws", "t0", [("%1", "claude", "alpha"), ("%2", "codex", "alpha")]
        )

    # Nothing left: not the agent worktrees, not the integration worktree.
    assert worktree_paths(repo) == {str(repo)}
    assert statuses() == ["removed"]
    assert not (task_root() / worktree.INTEGRATION_DIR).exists()


def test_setup_task_leaves_branches_behind_and_can_be_retried(repo, isolated_state):
    """Documented non-change: rollback keeps branches, so a retry is clean."""
    with pytest.raises(worktree.WorktreeError):
        worktree.setup_task(
            str(repo), "ws", "t0", [("%1", "claude", "alpha"), ("%2", "codex", "alpha")]
        )
    assert "amux/ws/t0/integration" in branches(repo)
    assert "amux/ws/t0/alpha" in branches(repo)

    # The retry has to cope with those leftovers. A distinct name set succeeds;
    # this is the real-world case, since `_build_grid` re-rolls agent names.
    paths = worktree.setup_task(
        str(repo), "ws", "t0", [("%1", "claude", "gamma"), ("%2", "codex", "delta")]
    )
    assert set(paths) == {"%1", "%2"}
    assert all(Path(p).is_dir() for p in paths.values())
    assert sorted(statuses()) == ["active", "active", "removed"]


def test_setup_task_matches_the_split_halves(repo, isolated_state):
    """The composition is exactly the two halves, so host callers see no change."""
    paths = worktree.setup_task(
        str(repo), "ws", "t0", [("%1", "claude", "alpha"), ("%2", "codex", "beta")]
    )
    rows = {row["name"]: row for row in store.worktrees_for("ws", "t0")}
    base = git(repo, "rev-parse", "HEAD")

    assert paths == {"%1": str(task_root() / "alpha"), "%2": str(task_root() / "beta")}
    assert (task_root() / worktree.INTEGRATION_DIR).is_dir()
    for name in ("alpha", "beta"):
        assert rows[name]["branch"] == f"amux/ws/t0/{name}"
        assert rows[name]["base_ref"] == base
        assert rows[name]["status"] == "active"
        # Agent branches start at the integration branch, not at HEAD directly.
        assert git(repo, "rev-parse", f"amux/ws/t0/{name}") == git(
            repo, "rev-parse", "amux/ws/t0/integration"
        )
