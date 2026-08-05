"""Transactional grid creation.

A grid is all-or-nothing. Half a grid leaves sandboxes nothing will ever attach
to, capabilities nobody will revoke, and registry rows a later `integrate`
would try to merge branches from.

The two properties asserted here:

1. **Reverse order.** Resources are released newest-first, so a release never
   depends on something already torn down.
2. **The original error survives.** Rollback runs while a failure is already
   propagating. Cleanup problems are aggregated *alongside* the cause, never in
   place of it -- otherwise the user reads "could not remove sandbox" and never
   learns why the grid failed at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fake_tmux
from amux import core, runtime, sandbox, store, worktree
from test_host_grid_snapshot import tmux_calls  # noqa: F401 - fixture
from test_sandbox_runtime import (  # noqa: F401 - `minted` is a fixture
    make_runtime,
    minted,
    names_for,
    ready,
    specs,
)


def statuses():
    return [(r["name"], r["status"], r["runtime_status"])
            for r in store.worktrees_for("ws", "t0")]


def fail_second_create(fake_sbx, names):
    """Script a fake where the first sandbox is created and the second is not."""
    ready(fake_sbx, names[:1])
    # First match wins, so this specific failure must be registered ahead of the
    # general `create` success `ready` already added -- register by re-writing.
    fake_sbx._responses.insert(  # noqa: SLF001 - ordering is the point
        0,
        {
            "argv": ["create", "--clone", "--name", names[1]],
            "stdout": "",
            "stderr": "ERROR: insufficient memory for sandbox\n",
            "returncode": 1,
        },
    )
    fake_sbx.script.write_text(__import__("json").dumps(fake_sbx._responses))


# --- reverse-order unwind ---


def test_a_failed_second_sandbox_unwinds_the_first(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha", "beta")
    fail_second_create(fake_sbx, names)
    rt = make_runtime()

    with pytest.raises(sandbox.SandboxError, match="insufficient memory"):
        rt.prepare(
            specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
            workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
        )

    problems = rt.rollback()
    assert problems == []
    # The one sandbox that was created is gone again.
    assert fake_sbx.called_with("rm", "-f", names[0])
    # Its row is marked, not deleted: the registry is append-only.
    assert statuses() == [("alpha", "removed", "failed")]


def test_rollback_releases_newest_first(git_repo, fake_sbx, monkeypatch):
    names = names_for(git_repo, "alpha", "beta", "gamma")
    ready(fake_sbx, names)
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "claude", "alpha"), ("%2", "codex", "beta"),
              ("%3", "claude", "gamma")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    order: list[str] = []
    monkeypatch.setattr(sandbox, "remove", lambda name, force=False: order.append(name))

    rt.rollback()
    assert order == [names[2], names[1], names[0]]


def test_rollback_revokes_every_capability(git_repo, fake_sbx, minted):
    """A leaked token must not outlive the sandbox it was minted for."""
    names = names_for(git_repo, "alpha", "beta")
    ready(fake_sbx, names)
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    # Both capabilities authenticate before the rollback...
    assert len(minted) == 2
    for record in minted:
        assert store.context_token_record(record["plaintext"]) is not None

    rt.rollback()

    # ...and neither does afterwards.
    for record in minted:
        assert store.context_token_record(record["plaintext"]) is None


def test_rollback_removes_the_shared_integration_worktree(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    int_path = Path(worktree.task_worktree_root("ws", "t0")) / worktree.INTEGRATION_DIR
    assert int_path.is_dir()

    rt.rollback()
    assert not int_path.exists()
    # The branch survives, as everywhere else in this codebase.
    branches = worktree._git(  # noqa: SLF001
        str(git_repo), "for-each-ref", "--format=%(refname:short)", "refs/heads"
    ).stdout
    assert "amux/ws/t0/integration" in branches


def test_rollback_drops_the_host_side_sandbox_remote(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    # Stand in for what `sbx create --clone` publishes on the host.
    remote = worktree.sandbox_remote(names[0])
    worktree._git(str(git_repo), "remote", "add", remote, "/dev/null")  # noqa: SLF001

    rt.rollback()
    remotes = worktree._git(str(git_repo), "remote").stdout  # noqa: SLF001
    assert remote not in remotes


def test_rollback_is_idempotent(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    assert rt.rollback() == []
    # A second pass has nothing left to do and must not invent failures.
    assert rt.rollback() == []


# --- error aggregation ---


def test_cleanup_failures_never_replace_the_original_error(git_repo, fake_sbx,
                                                           monkeypatch):
    names = names_for(git_repo, "alpha", "beta")
    fail_second_create(fake_sbx, names)
    rt = make_runtime()

    def stubborn(name, force=False):
        raise sandbox.SandboxError(f"sandbox {name} is busy")

    monkeypatch.setattr(sandbox, "remove", stubborn)

    with pytest.raises(sandbox.SandboxError) as caught:
        rt.prepare(
            specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
            workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
        )
    problems = rt.rollback()

    # The originating failure is still the one that surfaced...
    assert "insufficient memory" in str(caught.value)
    # ...and the cleanup failure is reported alongside rather than instead.
    assert any("is busy" in p for p in problems)
    # The row is still marked despite the sandbox refusing to go.
    assert statuses() == [("alpha", "removed", "failed")]


def test_grid_creation_error_carries_both_cause_and_cleanup():
    cause = ValueError("original failure")
    err = runtime.GridCreationError(cause, ["sandbox sb1: still running"])

    assert err.cause is cause
    assert "original failure" in str(err)
    assert "still running" in str(err)


def test_grid_creation_error_reads_cleanly_when_cleanup_succeeded():
    err = runtime.GridCreationError(ValueError("boom"))
    assert str(err) == "boom"
    assert "cleanup" not in str(err)


def test_a_rollback_that_itself_raises_is_reported_not_propagated():
    class Exploding:
        kind = "exploding"

        def rollback(self):
            raise RuntimeError("rollback exploded")

    problems = core._rollback(Exploding())  # noqa: SLF001
    assert problems == ["runtime rollback: rollback exploded"]


# --- pane and session teardown ---


def test_a_failed_grid_leaves_no_task_window(git_repo, fake_sbx):
    # `spawn_agent_grid` rolls its own agent names, so fail every create rather
    # than naming one.
    # Registered before `ready`'s success: the fake matches first-registered-wins.
    fake_sbx.respond(
        "create", stderr="ERROR: insufficient memory for sandbox\n", returncode=1
    )
    ready(fake_sbx, [])
    window = fake_tmux.new_window()
    session = window.session
    created: list[fake_tmux.FakeWindow] = []

    def new_window(window_name, start_directory, attach):
        made = fake_tmux.FakeWindow(session, name=window_name)
        created.append(made)
        return made

    session.new_window = new_window  # type: ignore[attr-defined]

    with pytest.raises(runtime.GridCreationError) as caught:
        core.spawn_agent_grid(
            session,  # type: ignore[arg-type]
            window_name="t0",
            nrows=1,
            ncols=2,
            agents=["claude", "codex"],
            cwd=str(git_repo),
            runtime=make_runtime(),
        )
    assert "insufficient memory" in str(caught.value)
    # The window this call created was killed; the pre-existing one was not.
    assert created  # a window really was made, so killing it means something
    kills = [e for e in session.server.log if e[0] == "window-cmd"
             and "kill-window" in e]
    assert kills


def test_a_failed_workspace_spawn_leaves_no_session(git_repo, fake_sbx):
    """`spw` created the session, so `spw` takes it away again."""
    # Registered before `ready`'s success: the fake matches first-registered-wins.
    fake_sbx.respond(
        "create", stderr="ERROR: insufficient memory for sandbox\n", returncode=1
    )
    ready(fake_sbx, [])
    window = fake_tmux.new_window()
    session = window.session
    killed: list[str] = []
    session.cmd = lambda *args: killed.append(args[0])  # type: ignore[attr-defined]

    with pytest.raises(runtime.GridCreationError):
        core._build_grid(  # noqa: SLF001
            window, 1, 2, ["claude", "codex"], str(git_repo),
            workspace="ws", task="t0", runtime=make_runtime(),
        )
    # _build_grid itself does not own the session; the spawn entry point does.
    assert killed == []


def test_host_grids_are_unaffected_by_the_unwind_path(git_repo, tmux_calls):
    """The host runtime has nothing to roll back, and must not acquire a
    rollback it does not need."""
    assert runtime.HostRuntime().rollback() == []
    window = fake_tmux.new_window()
    grid = core._build_grid(  # noqa: SLF001
        window, 1, 1, ["claude"], str(git_repo), workspace="ws", task="t0"
    )
    assert len(grid.agent_panes) == 1


# --- pathless rows must never touch host git ---


def test_a_sandbox_row_never_reports_the_hosts_commit(git_repo, fake_sbx, tmux_calls,
                                                      monkeypatch):
    """`git -C ""` is a no-op that reports the calling process's checkout, so an
    unguarded host-path git call attributes the host's HEAD to a sandboxed agent
    that never made that commit."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    make_runtime().prepare(
        specs(("%1", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    (row,) = store.worktrees_for("ws", "t0")
    assert row["path"] == ""  # the precondition that makes this reachable

    # Run from a checkout with a distinctive HEAD subject: if the guard is
    # missing, that subject is what leaks onto the roster.
    monkeypatch.chdir(git_repo)
    worktree._git(str(git_repo), "commit", "--allow-empty",  # noqa: SLF001
                  "-m", "HOST ONLY do not attribute this")

    window = fake_tmux.new_window()
    pane = window.panes[0]
    pane.cmd("set-option", "-p", "@amux_pane", "1")
    monkeypatch.setattr(
        store, "worktree_for_pane", lambda *a, **k: dict(row)
    )
    entry = core._roster_entry(pane)  # noqa: SLF001

    assert "HOST ONLY" not in str(entry.get("last_commit", ""))
    # Absent rather than wrong: a sandbox commit arrives via its remote.
    assert "last_commit" not in entry
    assert entry["worktree"] == ""


def test_a_host_row_still_reports_its_commit(git_repo, tmux_calls, monkeypatch):
    """The guard must not cost host agents their commit subject."""
    integration = worktree.setup_task_integration(str(git_repo), "ws", "t0")
    worktree.setup_host_agents(integration, [("%1", "claude", "alpha")])
    (row,) = store.worktrees_for("ws", "t0")

    window = fake_tmux.new_window()
    monkeypatch.setattr(store, "worktree_for_pane", lambda *a, **k: dict(row))
    entry = core._roster_entry(window.panes[0])  # noqa: SLF001

    assert entry["last_commit"] == "initial commit"
