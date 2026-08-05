"""Stopping a sandbox-backed task, and resuming it later.

`kg`/`kw` without `--clean` must stop sandboxes without destroying anything:
the microVM keeps its disk and whatever provider session the agent signed into,
and its capability stays valid. A later spawn of the same agent must then come
back to *that* sandbox rather than build a second one, which is only possible
because the sandbox name is derived rather than random.
"""

from __future__ import annotations

import pytest

import random

import fake_tmux
from amux import core, runtime, sandbox, store
from test_host_grid_snapshot import tmux_calls  # noqa: F401 - fixture
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


# --- reattach has to be reachable, not merely correct ---
#
# `_taken_names` reads live panes only. Once `kg` removes the pane, the prior
# agent name is neither taken nor preferred, so a respawn drew a NEW name, built
# a NEW VM and orphaned the stopped one -- while `_prior_row` and `sandbox.find`
# both resolved perfectly for the sandbox nothing ever asked about. Naming is
# what closes the loop.


def test_a_stopped_sandbox_name_is_offered_back_to_a_respawn(git_repo, fake_sbx):
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    fake_sbx.respond("stop")
    create_one(git_repo, fake_sbx)
    runtime.stop_task("ws", "t0")

    offered = make_runtime().resumable_names(
        workspace="ws", task="t0", cwd=str(git_repo)
    )
    assert offered == {"claude": ["alpha"]}


def test_a_respawned_grid_lands_on_its_prior_names(git_repo, fake_sbx, tmux_calls):
    """The end-to-end gap. Build a grid, then build it again on a fresh window
    with a different random draw: the second grid must adopt the first's names
    rather than orphaning its sandboxes."""
    ready(fake_sbx, [])

    def build_grid(seed):
        window = fake_tmux.new_window()
        random.seed(seed)
        return core._build_grid(  # noqa: SLF001
            window, 1, 2, ["claude", "claude"], str(git_repo),
            workspace="ws", task="t0", runtime=_naming_runtime(),
        )

    first = sorted(p.name for p in build_grid(4321).agent_panes)
    assert len(set(first)) == 2

    # A different seed, so coinciding by chance is not the explanation.
    second = sorted(p.name for p in build_grid(999).agent_panes)
    assert second == first


def _naming_runtime():
    """Registers rows exactly as the real runtime does, but runs no sbx -- so
    this is about NAME SELECTION and nothing else."""

    class Naming(runtime.SandboxRuntime):
        def prepare(self, panes, *, workspace, task, cwd, socket=""):
            repo = str(cwd)
            for spec in panes:
                store.register_worktree(
                    pane=spec.pane, workspace=workspace or "", task=task or "",
                    agent=spec.agent, name=spec.name, path="",
                    branch=f"amux/{workspace}/{task}/{spec.name}", base_ref="abc",
                    repo=repo, runtime="docker-sandbox", runtime_status="stopped",
                    sandbox_name=sandbox.sandbox_name(
                        workspace or "", task or "", spec.name, repo
                    ),
                    sandbox_id=f"sbx_{spec.name}", socket_name="amux-root",
                )
            return [runtime.Launch(pane=s.pane, cwd="", keys=()) for s in panes]

    return Naming(
        runtime.SandboxConfig(port=47317), service_healthy=lambda: (True, "ok")
    )


def test_a_live_pane_keeps_its_name_and_the_respawn_takes_the_other(
    git_repo, fake_sbx
):
    """Two panes must never share a name: notes, events and worktrees are
    addressed by it. A name still worn by a live pane is skipped."""
    names = names_for(git_repo, "alpha", "beta")
    ready(fake_sbx, names)
    rt = make_runtime()
    rt.prepare(
        specs(("%1", "claude", "alpha"), ("%2", "claude", "beta")),
        workspace="ws", task="t0", cwd=str(git_repo), socket="amux-root",
    )
    offered = rt.resumable_names(workspace="ws", task="t0", cwd=str(git_repo))
    assert offered["claude"] == ["alpha", "beta"]

    taken = {"alpha"}  # alpha's pane is still alive
    assert core._next_name("claude", dict(offered), taken) == "beta"  # noqa: SLF001


def test_a_different_agent_kind_is_never_offered_a_prior_name(git_repo, fake_sbx):
    """A sandbox holds the agent it was built for, so handing a codex pane a
    stopped claude sandbox's name would reattach it to the wrong tool."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    create_one(git_repo, fake_sbx, agent="claude")

    offered = make_runtime().resumable_names(
        workspace="ws", task="t0", cwd=str(git_repo)
    )
    assert "codex" not in offered

    random.seed(7)
    fresh = core._next_name("codex", dict(offered), set())  # noqa: SLF001
    assert fresh != "alpha"


def test_another_repository_does_not_adopt_these_sandboxes(
    git_repo, git_factory, fake_sbx
):
    """workspace and task are reusable labels; two checkouts must not adopt each
    other's VMs, which is the same reason a sandbox name carries a repo digest."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    create_one(git_repo, fake_sbx)
    other = git_factory("other")

    assert make_runtime().resumable_names(
        workspace="ws", task="t0", cwd=str(other)
    ) == {}


def test_a_removed_sandbox_is_not_offered_back(git_repo, fake_sbx):
    """Only a VM that may still exist. A cleaned row has nothing to resume."""
    names = names_for(git_repo, "alpha")
    ready(fake_sbx, names)
    create_one(git_repo, fake_sbx)
    (row,) = store.worktrees_for("ws", "t0")
    store.set_worktree_runtime(row["id"], runtime_status="removed")

    assert make_runtime().resumable_names(
        workspace="ws", task="t0", cwd=str(git_repo)
    ) == {}


def test_the_host_runtime_offers_nothing(git_repo):
    """Reusing a host name would fail `worktree add`, not resume anything."""
    assert runtime.HostRuntime().resumable_names(
        workspace="ws", task="t0", cwd=str(git_repo)
    ) == {}
