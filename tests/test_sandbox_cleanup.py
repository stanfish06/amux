"""Cleaning up sandboxes without losing work.

`--clean` is the only operation here that destroys anything, so the ordering is
the safety property: check the working tree, refuse if dirty, preserve the
committed tip on the host, remove, revoke, and only then mark the row removed.
A refused or failed removal must leave the row exactly as it was -- a row marked
removed while its sandbox still holds work is both unreachable and invisible.
"""

from __future__ import annotations

import pytest

from amux import runtime, sandbox, store, worktree
from test_sandbox_runtime import (  # noqa: F401 - `minted` is a fixture
    make_runtime,
    minted,
    names_for,
    ready,
    specs,
)

DIRTY = " M src/thing.py\n?? notes.txt\n"


def rows():
    return {r["name"]: r for r in store.worktrees_for("ws", "t0")}


def build(git_repo, fake_sbx, *names):
    rt = make_runtime()
    rt.prepare(
        specs(*[(f"%{i}", "claude", n) for i, n in enumerate(names, start=1)]),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    return rt


def with_status(fake_sbx, names, status_by_name):
    """Answer `git status --porcelain` per sandbox, before `ready`'s catch-all."""
    for name in names:
        fake_sbx.respond(
            "exec", name, "git", "status", "--porcelain",
            stdout=status_by_name.get(name, ""),
        )


# --- the clean case ---


def test_a_clean_sandbox_is_removed_and_its_row_marked(git_repo, fake_sbx, minted):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")

    removed = runtime.clean_task("ws", "t0")

    assert removed == names
    assert fake_sbx.called_with("rm", names[0])
    assert rows()["alpha"]["status"] == "removed"
    assert rows()["alpha"]["runtime_status"] == "removed"
    # The capability dies with the sandbox.
    assert store.context_token_record(minted[0]["plaintext"]) is None


def test_the_committed_tip_is_preserved_before_the_sandbox_is_removed(
    git_repo, fake_sbx, monkeypatch
):
    """Order matters absolutely: after `sbx rm` the commits are unreachable."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")

    order: list[str] = []
    real_fetch = worktree.fetch_sandbox_branch
    real_remove = sandbox.remove

    def fetch(repo, name, branch):
        order.append("fetch")
        return real_fetch(repo, name, branch)

    def remove(name, force=False):
        order.append("remove")
        return real_remove(name, force=force)

    monkeypatch.setattr(worktree, "fetch_sandbox_branch", fetch)
    monkeypatch.setattr(sandbox, "remove", remove)
    runtime.clean_task("ws", "t0")

    assert order == ["fetch", "remove"]


def test_the_host_remote_is_dropped_with_the_sandbox(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")
    remote = worktree.sandbox_remote(names[0])
    worktree._git(str(git_repo), "remote", "add", remote, "/dev/null")  # noqa: SLF001

    runtime.clean_task("ws", "t0")

    assert remote not in worktree._git(str(git_repo), "remote").stdout  # noqa: SLF001


# --- the refusal ---


def test_a_dirty_sandbox_is_refused_by_default(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {names[0]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")

    with pytest.raises(sandbox.SandboxError) as caught:
        runtime.clean_task("ws", "t0")

    message = str(caught.value)
    assert "refusing to remove" in message
    # It names the sandbox and the files that must be resolved.
    assert names[0] in message
    assert "src/thing.py" in message and "notes.txt" in message
    # And it says how to proceed, plus what is safe.
    assert "--force" in message
    assert "Committed branch tips are preserved" in message


def test_a_refused_removal_changes_nothing(git_repo, fake_sbx, minted):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {names[0]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")

    with pytest.raises(sandbox.SandboxError):
        runtime.clean_task("ws", "t0")

    assert not fake_sbx.called_with("rm")
    assert rows()["alpha"]["status"] == "active"
    assert rows()["alpha"]["runtime_status"] == "running"
    # The capability still works: nothing was cleaned up.
    assert store.context_token_record(minted[0]["plaintext"]) is not None


def test_one_dirty_sandbox_spares_the_others_too(git_repo, fake_sbx):
    """All-or-nothing: a partial clean would be the worst of both."""
    names = names_for(git_repo, "alpha", "beta")
    with_status(fake_sbx, names, {names[1]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", "beta")

    with pytest.raises(sandbox.SandboxError):
        runtime.clean_task("ws", "t0")

    assert not fake_sbx.called_with("rm")
    assert {r["status"] for r in store.worktrees_for("ws", "t0")} == {"active"}


def test_every_dirty_sandbox_is_listed_at_once(git_repo, fake_sbx):
    """Not one per re-run: the user should see the whole problem."""
    names = names_for(git_repo, "alpha", "beta")
    with_status(fake_sbx, names, {names[0]: DIRTY, names[1]: " M other.py\n"})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", "beta")

    with pytest.raises(sandbox.SandboxError) as caught:
        runtime.clean_task("ws", "t0")

    assert names[0] in str(caught.value) and names[1] in str(caught.value)


def test_an_unreadable_working_tree_counts_as_dirty(git_repo, fake_sbx):
    """Treating an unanswerable question as 'clean' would delete work on
    exactly the sandboxes that are already misbehaving."""
    names = names_for(git_repo, "alpha")
    fake_sbx.respond(
        "exec", names[0], "git", "status", "--porcelain",
        stderr="ERROR: sandbox is not running\n", returncode=1,
    )
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")

    with pytest.raises(sandbox.SandboxError, match="could not read the working tree"):
        runtime.clean_task("ws", "t0")
    assert not fake_sbx.called_with("rm")


# --- the override ---


def test_force_removes_a_dirty_sandbox(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {names[0]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")

    removed = runtime.clean_task("ws", "t0", force=True)

    assert removed == names
    assert fake_sbx.called_with("rm", "-f", names[0])
    assert rows()["alpha"]["status"] == "removed"


def test_force_still_preserves_the_committed_tip(git_repo, fake_sbx, monkeypatch):
    """`--force` gives up the *uncommitted* work only."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {names[0]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")
    fetched: list[str] = []
    real = worktree.fetch_sandbox_branch

    def spy(repo, name, branch):
        fetched.append(name)
        return real(repo, name, branch)

    monkeypatch.setattr(worktree, "fetch_sandbox_branch", spy)
    runtime.clean_task("ws", "t0", force=True)

    assert fetched == names


# --- failures during removal ---


def test_a_sandbox_that_refuses_to_go_keeps_its_row(git_repo, fake_sbx, capsys):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    fake_sbx.respond("rm", names[0], stderr="ERROR: sandbox is busy\n", returncode=1)
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")

    removed = runtime.clean_task("ws", "t0")

    assert removed == []
    assert "could not remove sandbox" in capsys.readouterr().out
    # Never marked removed: the sandbox is still there and still owns its work.
    assert rows()["alpha"]["status"] == "active"


def test_a_sandbox_with_nothing_committed_is_still_removable(
    git_repo, fake_sbx, capsys
):
    """No branch to preserve is a fact to report, not a reason to refuse."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")

    removed = runtime.clean_task("ws", "t0")

    assert removed == names
    assert "no committed branch to preserve" in capsys.readouterr().out
    assert rows()["alpha"]["status"] == "removed"


# --- scope ---


def test_host_agents_are_left_to_the_worktree_path(git_repo, fake_sbx):
    integration = worktree.setup_task_integration(str(git_repo), "ws", "t0")
    worktree.setup_host_agents(integration, [("%1", "claude", "hosty")])

    assert runtime.clean_task("ws", "t0") == []
    assert not fake_sbx.calls
    assert rows()["hosty"]["status"] == "active"


def test_remove_task_skips_sandbox_rows(git_repo, fake_sbx):
    """`git worktree remove ''` is not something to rely on; sandbox rows are
    `clean_task`'s job because they need the dirty check first."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha")

    assert worktree.remove_task("ws", "t0") == []
    # Untouched, so a later clean_task can still refuse or preserve properly.
    assert rows()["alpha"]["status"] == "active"
