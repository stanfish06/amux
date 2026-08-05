"""Stopping a sandbox-backed task, and resuming it later.

`kg`/`kw` without `--clean` must stop sandboxes without destroying anything:
the microVM keeps its disk and whatever provider session the agent signed into,
and its capability stays valid. A later spawn of the same agent must then come
back to *that* sandbox rather than build a second one, which is only possible
because the sandbox name is derived rather than random.
"""

from __future__ import annotations

import pytest

from amux import runtime, sandbox, store
from test_sandbox_runtime import (  # noqa: F401 - `minted` is a fixture
    make_runtime,
    minted,
    names_for,
    ready,
    specs,
)


def rows():
    return {r["name"]: r for r in store.worktrees_for("ws", "t0")}


def active_rows():
    return [r for r in store.worktrees_for("ws", "t0") if r["status"] == "active"]


def create_one(git_repo, fake_sbx, name="alpha", agent="claude"):
    rt = make_runtime()
    rt.prepare(
        specs(("%1", agent, name)),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    return rt


# --- stopping ---


def test_stop_task_stops_each_sandbox_without_removing_it(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    fake_sbx.respond("stop")
    create_one(git_repo, fake_sbx)

    stopped = runtime.stop_task("ws", "t0")

    assert stopped == names
    assert fake_sbx.called_with("stop", names[0])
    # Stopped, never removed: the VM and its contents survive.
    assert not fake_sbx.called_with("rm")


def test_a_stopped_sandbox_keeps_its_identity_and_row(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    fake_sbx.respond("stop")
    create_one(git_repo, fake_sbx)
    before = rows()["alpha"]

    runtime.stop_task("ws", "t0")
    after = rows()["alpha"]

    assert after["runtime_status"] == "stopped"
    # Everything needed to find it again is retained.
    assert after["sandbox_name"] == before["sandbox_name"]
    assert after["sandbox_id"] == before["sandbox_id"]
    assert after["branch"] == before["branch"]
    # The row stays active: the execution is paused, not finished.
    assert after["status"] == "active"


def test_stopping_does_not_revoke_the_capability(git_repo, fake_sbx, minted):
    """A stopped sandbox must resume as itself, which means its credential
    outlives the stop. Revocation belongs to cleanup, which is explicit."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    fake_sbx.respond("stop")
    create_one(git_repo, fake_sbx)
    assert store.context_token_record(minted[0]["plaintext"]) is not None

    runtime.stop_task("ws", "t0")

    assert store.context_token_record(minted[0]["plaintext"]) is not None


def test_a_stubborn_sandbox_does_not_block_the_rest(git_repo, fake_sbx, capsys):
    names = names_for(git_repo, "alpha", "beta")
    ready(fake_sbx, names)
    fake_sbx.respond("stop", names[0], stderr="ERROR: sandbox busy\n", returncode=1)
    fake_sbx.respond("stop")
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )

    stopped = runtime.stop_task("ws", "t0")

    assert stopped == [names[1]]
    assert "could not stop" in capsys.readouterr().out
    # The one that refused is not misreported as stopped.
    assert rows()["alpha"]["runtime_status"] == "running"
    assert rows()["beta"]["runtime_status"] == "stopped"


def test_stop_task_ignores_host_agents(git_repo, fake_sbx):
    """A host agent has no VM to stop, and must not be touched."""
    from amux import worktree

    integration = worktree.setup_task_integration(str(git_repo), "ws", "t0")
    worktree.setup_host_agents(integration, [("%1", "claude", "hosty")])

    assert runtime.stop_task("ws", "t0") == []
    assert not fake_sbx.calls
    assert rows()["hosty"]["status"] == "active"


# --- resuming ---


def test_a_later_spawn_reattaches_to_the_same_sandbox(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    fake_sbx.respond("stop")
    create_one(git_repo, fake_sbx)
    original_id = rows()["alpha"]["sandbox_id"]
    runtime.stop_task("ws", "t0")

    creates_before = len([c for c in fake_sbx.calls if c[0] == "create"])
    rt2 = make_runtime()
    (launch,) = rt2.prepare(
        specs(("%2", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )

    # No second VM was built...
    assert len([c for c in fake_sbx.calls if c[0] == "create"]) == creates_before
    # ...and the pane attaches to the sandbox that already exists.
    assert launch.keys == (f"sbx run --name {names[0]}",)
    resumed = [r for r in store.worktrees_for("ws", "t0") if r["status"] == "active"]
    assert len(resumed) == 1
    assert resumed[0]["pane"] == "%2"
    assert resumed[0]["sandbox_id"] == original_id


def test_resuming_checks_out_the_existing_branch_rather_than_creating_it(
    git_repo, fake_sbx
):
    """The branch already carries the agent's commits; `-b` would fail, and
    asking to create it is the wrong request in the first place."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    create_one(git_repo, fake_sbx)
    make_runtime().prepare(
        specs(("%2", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    checkouts = [c for c in fake_sbx.calls if "checkout" in c]
    assert checkouts[0][-3:] == ["checkout", "-b", "amux/ws/t0/alpha"]
    assert checkouts[1][-2:] == ["checkout", "amux/ws/t0/alpha"]


def test_resuming_supersedes_the_previous_row(git_repo, fake_sbx, minted):
    """Two active rows for one sandbox would make integrate merge the same
    branch twice and leave a dead pane's capability working."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    create_one(git_repo, fake_sbx)
    first_token = minted[0]["plaintext"]

    make_runtime().prepare(
        specs(("%2", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )

    assert len(active_rows()) == 1
    # The superseded row is marked, not deleted: notes and events point at it.
    assert len(store.worktrees_for("ws", "t0")) == 2
    # The dead pane's capability no longer authenticates; the new one does.
    assert store.context_token_record(first_token) is None
    assert store.context_token_record(minted[-1]["plaintext"]) is not None


def test_a_resumed_sandbox_is_never_destroyed_by_a_rollback(git_repo, fake_sbx):
    """A failed respawn must not turn into data loss for work that predates
    this command entirely."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    create_one(git_repo, fake_sbx)

    rt2 = make_runtime()
    rt2.prepare(
        specs(("%2", "claude", "alpha")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    rt2.rollback()

    assert not fake_sbx.called_with("rm", "-f", names[0])
    assert not fake_sbx.called_with("rm", names[0])


def test_a_sandbox_this_run_created_is_still_destroyed_by_a_rollback(
    git_repo, fake_sbx
):
    """The counterpart: the reattach guard must not disable normal cleanup."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    rt = create_one(git_repo, fake_sbx)
    rt.rollback()

    assert fake_sbx.called_with("rm", "-f", names[0])
