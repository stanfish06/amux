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
from test_host_grid_snapshot import git
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


def wire_remote(git_repo, tmp_path, agent_name, *, commits=True):
    """A real `sandbox-<name>` remote, as `sbx create --clone` publishes.

    The clone is a second git repository, because that is what the host sees:
    `ls-remote` and `fetch` against it exercise the real code path. Without one,
    a test models a sandbox whose remote is unreachable -- which is now a
    refusal, correctly, so faking its absence would test the wrong thing.
    """
    name = sandbox.sandbox_name("ws", "t0", agent_name, str(git_repo))
    branch = worktree.agent_branch("ws", "t0", agent_name)
    clone = tmp_path / f"clone-{agent_name}"
    git(git_repo.parent, "clone", "-q", str(git_repo), str(clone))
    if commits:
        git(clone, "checkout", "-q", "-b", branch)
        (clone / f"{agent_name}.txt").write_text("work\n")
        git(clone, "add", f"{agent_name}.txt")
        git(clone, "commit", "-qm", f"{agent_name} work")
    git(git_repo, "remote", "add", worktree.sandbox_remote(name), str(clone))
    return clone


def build(git_repo, fake_sbx, *names, tmp_path=None, commits=True):
    if tmp_path is not None:
        for name in names:
            wire_remote(git_repo, tmp_path, name, commits=commits)
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


def test_a_clean_sandbox_is_removed_and_its_row_marked(git_repo, fake_sbx, tmp_path, minted):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

    removed = runtime.clean_task("ws", "t0")

    assert removed == names
    assert fake_sbx.called_with("rm", names[0])
    assert rows()["alpha"]["status"] == "removed"
    assert rows()["alpha"]["runtime_status"] == "removed"
    # The capability dies with the sandbox.
    assert store.context_token_record(minted[0]["plaintext"]) is None


def test_the_committed_tip_is_preserved_before_the_sandbox_is_removed(
    git_repo, fake_sbx, tmp_path, monkeypatch
):
    """Order matters absolutely: after `sbx rm` the commits are unreachable."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

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


def test_the_host_remote_is_dropped_with_the_sandbox(git_repo, fake_sbx, tmp_path):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)
    remote = worktree.sandbox_remote(names[0])
    assert remote in worktree._git(str(git_repo), "remote").stdout  # noqa: SLF001

    runtime.clean_task("ws", "t0")

    assert remote not in worktree._git(str(git_repo), "remote").stdout  # noqa: SLF001


# --- the refusal ---


def test_a_dirty_sandbox_is_refused_by_default(git_repo, fake_sbx, tmp_path):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {names[0]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

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


def test_a_refused_removal_changes_nothing(git_repo, fake_sbx, tmp_path, minted):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {names[0]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

    with pytest.raises(sandbox.SandboxError):
        runtime.clean_task("ws", "t0")

    assert not fake_sbx.called_with("rm")
    assert rows()["alpha"]["status"] == "active"
    assert rows()["alpha"]["runtime_status"] == "running"
    # The capability still works: nothing was cleaned up.
    assert store.context_token_record(minted[0]["plaintext"]) is not None


def test_one_dirty_sandbox_spares_the_others_too(git_repo, fake_sbx, tmp_path):
    """All-or-nothing: a partial clean would be the worst of both."""
    names = names_for(git_repo, "alpha", "beta")
    with_status(fake_sbx, names, {names[1]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", "beta", tmp_path=tmp_path)

    with pytest.raises(sandbox.SandboxError):
        runtime.clean_task("ws", "t0")

    assert not fake_sbx.called_with("rm")
    assert {r["status"] for r in store.worktrees_for("ws", "t0")} == {"active"}


def test_every_dirty_sandbox_is_listed_at_once(git_repo, fake_sbx, tmp_path):
    """Not one per re-run: the user should see the whole problem."""
    names = names_for(git_repo, "alpha", "beta")
    with_status(fake_sbx, names, {names[0]: DIRTY, names[1]: " M other.py\n"})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", "beta", tmp_path=tmp_path)

    with pytest.raises(sandbox.SandboxError) as caught:
        runtime.clean_task("ws", "t0")

    assert names[0] in str(caught.value) and names[1] in str(caught.value)


def test_an_unreadable_working_tree_counts_as_dirty(git_repo, fake_sbx, tmp_path):
    """Treating an unanswerable question as 'clean' would delete work on
    exactly the sandboxes that are already misbehaving."""
    names = names_for(git_repo, "alpha")
    fake_sbx.respond(
        "exec", names[0], "git", "status", "--porcelain",
        stderr="ERROR: sandbox is not running\n", returncode=1,
    )
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

    with pytest.raises(sandbox.SandboxError, match="could not read the working tree"):
        runtime.clean_task("ws", "t0")
    assert not fake_sbx.called_with("rm")


# --- the override ---


def test_force_removes_a_dirty_sandbox(git_repo, fake_sbx, tmp_path):
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {names[0]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

    removed = runtime.clean_task("ws", "t0", force=True)

    assert removed == names
    assert fake_sbx.called_with("rm", "-f", names[0])
    assert rows()["alpha"]["status"] == "removed"


def test_force_still_preserves_the_committed_tip(git_repo, fake_sbx, tmp_path, monkeypatch):
    """`--force` gives up the *uncommitted* work only."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {names[0]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)
    fetched: list[str] = []
    real = worktree.fetch_sandbox_branch

    def spy(repo, name, branch):
        fetched.append(name)
        return real(repo, name, branch)

    monkeypatch.setattr(worktree, "fetch_sandbox_branch", spy)
    runtime.clean_task("ws", "t0", force=True)

    assert fetched == names


# --- failures during removal ---


def test_a_sandbox_that_refuses_to_go_is_raised_not_printed(
    git_repo, fake_sbx, tmp_path
):
    """A print let the caller carry on and kill the tmux session, after which
    the abandoned VMs could not be addressed by any amux command."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    fake_sbx.respond("rm", names[0], stderr="ERROR: sandbox is busy\n", returncode=1)
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

    with pytest.raises(sandbox.SandboxError) as caught:
        runtime.clean_task("ws", "t0")

    message = str(caught.value)
    assert "still on this host" in message
    assert names[0] in message and "sandbox is busy" in message
    # It says what to do, including that the manual escape discards work.
    assert "sbx rm -f" in message
    # Never marked removed: the sandbox is still there and still owns its work.
    assert rows()["alpha"]["status"] == "active"


def test_a_sandbox_with_nothing_committed_is_still_removable(
    git_repo, fake_sbx, tmp_path, capsys
):
    """An agent that never committed has nothing to lose, so this is a fact to
    report rather than a reason to refuse. The remote is reachable and simply
    has no such branch -- which is exactly what distinguishes it from the
    refusals below, and what the old code could not tell apart."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path, commits=False)

    removed = runtime.clean_task("ws", "t0")

    assert removed == names
    assert "nothing committed on" in capsys.readouterr().out
    assert rows()["alpha"]["status"] == "removed"


# --- scope ---


def test_host_agents_are_left_to_the_worktree_path(git_repo, fake_sbx, tmp_path):
    integration = worktree.setup_task_integration(str(git_repo), "ws", "t0")
    worktree.setup_host_agents(integration, [("%1", "claude", "hosty")])

    assert runtime.clean_task("ws", "t0") == []
    assert not fake_sbx.calls
    assert rows()["hosty"]["status"] == "active"


def test_remove_task_skips_sandbox_rows(git_repo, fake_sbx, tmp_path):
    """`git worktree remove ''` is not something to rely on; sandbox rows are
    `clean_task`'s job because they need the dirty check first."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

    assert worktree.remove_task("ws", "t0") == []
    # Untouched, so a later clean_task can still refuse or preserve properly.
    assert rows()["alpha"]["status"] == "active"


# --- the integrated task, which is the workflow the guide documents ---
#
# Every other cleanup test here uses an *active* row. That is exactly how this
# leaked: `integrate` sets status='merged', and stop/clean filtered on 'active',
# so the one case the documented workflow always reaches was the one case
# neither path ever touched. The rows below are merged on purpose.


def merge_rows():
    for row in store.worktrees_for("ws", "t0"):
        store.set_worktree_status(row["id"], "merged")


def test_an_integrated_task_still_cleans_its_sandboxes(git_repo, fake_sbx, tmp_path):
    """The leak: five microVMs survived `kw --clean --force` with no warning."""
    names = names_for(git_repo, "alpha", "beta")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", "beta", tmp_path=tmp_path)
    merge_rows()

    removed = runtime.clean_task("ws", "t0")

    assert sorted(removed) == sorted(names)
    for name in names:
        assert fake_sbx.called_with("rm", name)


def test_an_integrated_task_still_stops_its_sandboxes(git_repo, fake_sbx, tmp_path):
    """Same root cause: `kg` never stopped a merged task's VMs, which is why
    reattachment could not be measured at all."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    fake_sbx.respond("stop")
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)
    merge_rows()

    assert runtime.stop_task("ws", "t0") == names
    assert fake_sbx.called_with("stop", names[0])
    assert rows()["alpha"]["runtime_status"] == "stopped"


def test_cleaning_a_merged_row_does_not_rewrite_its_merge_history(git_repo, fake_sbx, tmp_path):
    """The two axes stay independent: the work really was merged, and only the
    VM went away. Overwriting `merged` with `removed` would lose that."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)
    merge_rows()

    runtime.clean_task("ws", "t0")

    assert rows()["alpha"]["status"] == "merged"
    assert rows()["alpha"]["runtime_status"] == "removed"


def test_an_active_row_is_still_marked_removed(git_repo, fake_sbx, tmp_path):
    """The counterpart: an execution that never merged is finished by cleanup."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

    runtime.clean_task("ws", "t0")

    assert rows()["alpha"]["status"] == "removed"
    assert rows()["alpha"]["runtime_status"] == "removed"


def test_a_dirty_merged_sandbox_is_still_refused(git_repo, fake_sbx, tmp_path):
    """Merging does not make uncommitted work in the VM disposable."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {names[0]: DIRTY})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)
    merge_rows()

    with pytest.raises(sandbox.SandboxError, match="refusing to remove"):
        runtime.clean_task("ws", "t0")
    assert not fake_sbx.called_with("rm")


def test_a_removed_row_is_not_acted_on_twice(git_repo, fake_sbx, tmp_path):
    """Cleanup is a fixed point: the second run has nothing left to do."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

    assert runtime.clean_task("ws", "t0") == names
    assert runtime.clean_task("ws", "t0") == []
    assert runtime.stop_task("ws", "t0") == []
    assert len([c for c in fake_sbx.calls if c[0] == "rm"]) == 1


# --- a stopped sandbox's committed work ---
#
# The `sandbox-<name>` remote is served from inside the VM, so a stopped
# sandbox's tip cannot be read at all. Cleanup used to swallow that as "no
# committed branch to preserve" -- identical handling to an agent that genuinely
# never committed -- and with --force it destroyed the commits. The only signal
# was a bare git error naming no sandbox.


def test_a_stopped_sandbox_is_woken_before_its_tip_is_read(
    git_repo, fake_sbx, tmp_path
):
    """`sbx exec` starts a stopped sandbox, which is what makes "preserve before
    removing" true rather than aspirational."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    ready(fake_sbx, names)
    fake_sbx.respond("stop")
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)
    runtime.stop_task("ws", "t0")
    assert rows()["alpha"]["runtime_status"] == "stopped"

    removed = runtime.clean_task("ws", "t0")

    assert removed == names
    # Woken, then its tip preserved, then removed -- in that order.
    order = [c for c in fake_sbx.calls if c[0] in ("exec", "rm")]
    assert ["exec", names[0], "true"] in [c for c in order]
    assert order[-1][:2] == ["rm", names[0]]
    ref = worktree.sandbox_tracking_ref(names[0], "amux/ws/t0/alpha")
    assert git(git_repo, "rev-parse", "--verify", ref)


def test_an_unreadable_tip_refuses_even_with_force(git_repo, fake_sbx, tmp_path):
    """`--force` authorises losing UNCOMMITTED work. It does not authorise
    destroying commits nobody has managed to copy out."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)
    # The VM is gone, so its remote answers nothing.
    git(git_repo, "remote", "set-url", worktree.sandbox_remote(names[0]), "/nonexistent")

    with pytest.raises(sandbox.SandboxError) as caught:
        runtime.clean_task("ws", "t0", force=True)

    message = str(caught.value)
    # Names the sandbox and says plainly that the tip is unsaved -- a bare git
    # error is what let this look like nothing.
    assert names[0] in message
    assert "NOT saved on the host" in message
    assert "amux/ws/t0/alpha" in message
    assert not fake_sbx.called_with("rm")
    assert rows()["alpha"]["status"] == "active"


def test_a_sandbox_that_cannot_be_started_is_not_removed(
    git_repo, fake_sbx, tmp_path
):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)
    fake_sbx._responses.insert(0, {  # noqa: SLF001 - ordering is the point
        "argv": ["exec", names[0], "true"],
        "stdout": "", "stderr": "ERROR: sandbox failed to start\n", "returncode": 1,
    })
    fake_sbx.script.write_text(__import__("json").dumps(fake_sbx._responses))

    with pytest.raises(sandbox.SandboxError, match="NOT saved on the host"):
        runtime.clean_task("ws", "t0", force=True)
    assert not fake_sbx.called_with("rm")


def test_a_refused_removal_does_not_leave_a_stopped_vm_running(
    git_repo, fake_sbx, tmp_path
):
    """Inspecting a sandbox starts it, so a refusal would otherwise leave a VM
    running that the registry still calls stopped -- burning memory and
    disagreeing with its own record."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    fake_sbx.respond("rm", names[0], stderr="ERROR: sandbox is busy\n", returncode=1)
    ready(fake_sbx, names)
    fake_sbx.respond("stop")
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)
    runtime.stop_task("ws", "t0")
    stops_before = len([c for c in fake_sbx.calls if c[0] == "stop"])

    with pytest.raises(sandbox.SandboxError):
        runtime.clean_task("ws", "t0")

    # Stopped again after the refusal, so sbx and the registry still agree.
    assert len([c for c in fake_sbx.calls if c[0] == "stop"]) == stops_before + 1
    assert rows()["alpha"]["runtime_status"] == "stopped"


def test_a_running_sandbox_is_not_stopped_by_a_refusal(git_repo, fake_sbx, tmp_path):
    """Only restore what this call changed: a running sandbox stays running."""
    names = names_for(git_repo, "alpha")
    with_status(fake_sbx, names, {})
    fake_sbx.respond("rm", names[0], stderr="ERROR: sandbox is busy\n", returncode=1)
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", tmp_path=tmp_path)

    with pytest.raises(sandbox.SandboxError):
        runtime.clean_task("ws", "t0")

    assert not fake_sbx.called_with("stop")
    assert rows()["alpha"]["runtime_status"] == "running"


def test_the_sandboxes_that_can_be_cleaned_still_are(git_repo, fake_sbx, tmp_path):
    """A refusal for one must not strand the rest -- that would turn one stuck
    VM into a whole workspace of them."""
    names = names_for(git_repo, "alpha", "beta")
    with_status(fake_sbx, names, {})
    fake_sbx.respond("rm", names[0], stderr="ERROR: sandbox is busy\n", returncode=1)
    ready(fake_sbx, names)
    build(git_repo, fake_sbx, "alpha", "beta", tmp_path=tmp_path)

    with pytest.raises(sandbox.SandboxError) as caught:
        runtime.clean_task("ws", "t0")

    assert f"removed: {names[1]}" in str(caught.value)
    assert rows()["alpha"]["status"] == "active"
    assert rows()["beta"]["status"] == "removed"


# --- the session must outlive a refusal ---
#
# Non-force cleanup protected the work but abandoned the VMs: `sbx rm` refuses
# clone-mode sandboxes, the rows correctly stayed un-removed, and amux then
# printed "killed workspace", exited 0 AND killed the tmux session -- after which
# `kw` answered "workspace not found" and no amux command could address those
# microVMs again. Same shape as the leak: success reported, resources abandoned.


class FakeWin:
    def __init__(self, name):
        self.name = name


class FakeSess:
    def __init__(self, names):
        self.windows = [FakeWin(n) for n in names]
        self.killed = False


def test_kw_does_not_kill_the_session_when_a_sandbox_survives(monkeypatch):
    from amux import cli

    session = FakeSess(["t0"])
    monkeypatch.setattr(cli, "_get_session", lambda server, ws: session)
    monkeypatch.setattr(
        cli.runtime, "clean_task",
        lambda ws, task, force=False: (_ for _ in ()).throw(
            sandbox.SandboxError("box-1: could not be removed (busy)")
        ),
    )
    monkeypatch.setattr(cli.worktree, "remove_task", lambda ws, task: [])
    monkeypatch.setattr(
        cli.core, "load_agent_space",
        lambda s: (_ for _ in ()).throw(AssertionError("must not terminate")),
    )
    args = type("A", (), {"workspace": "ws", "clean": True, "force": False})()

    with pytest.raises(sandbox.SandboxError) as caught:
        cli._cmd_kw(None, args)  # noqa: SLF001

    message = str(caught.value)
    assert "was left in place" in message
    assert "box-1" in message


def test_kw_still_kills_the_session_when_cleanup_succeeds(monkeypatch):
    """The counterpart: the guard must not make `--clean` stop working."""
    from amux import cli

    session = FakeSess(["t0"])
    terminated: list[bool] = []
    monkeypatch.setattr(cli, "_get_session", lambda server, ws: session)
    monkeypatch.setattr(cli.runtime, "clean_task", lambda ws, task, force=False: [])
    monkeypatch.setattr(cli.worktree, "remove_task", lambda ws, task: [])
    monkeypatch.setattr(
        cli.core, "load_agent_space",
        lambda s: type("G", (), {"terminate": lambda self: terminated.append(True)})(),
    )
    args = type("A", (), {"workspace": "ws", "clean": True, "force": False})()

    assert cli._cmd_kw(None, args) == 0  # noqa: SLF001
    assert terminated == [True]


def test_kw_attempts_every_task_before_reporting(monkeypatch):
    """One stuck task must not stop the others being cleaned."""
    from amux import cli

    session = FakeSess(["t0", "t1", "t2"])
    seen: list[str] = []

    def clean(ws, task, force=False):
        seen.append(task)
        if task == "t1":
            raise sandbox.SandboxError(f"{task}-box: could not be removed")
        return []

    monkeypatch.setattr(cli, "_get_session", lambda server, ws: session)
    monkeypatch.setattr(cli.runtime, "clean_task", clean)
    monkeypatch.setattr(cli.worktree, "remove_task", lambda ws, task: [])
    monkeypatch.setattr(cli.core, "load_agent_space", lambda s: None)
    args = type("A", (), {"workspace": "ws", "clean": True, "force": False})()

    with pytest.raises(sandbox.SandboxError) as caught:
        cli._cmd_kw(None, args)  # noqa: SLF001

    assert seen == ["t0", "t1", "t2"]
    assert "t1-box" in str(caught.value)
