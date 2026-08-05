"""The runtime seam itself.

`test_host_grid_snapshot` proves the host runtime still behaves exactly as it
did. These tests prove the other half: that the seam is real — a runtime other
than `HostRuntime` fully controls where a pane works and what it runs, while
pane identity, tmux metadata, exit hooks and spawn events stay identical.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import fake_tmux
from amux import core, runtime, store, worktree
from test_host_grid_snapshot import tmux_calls  # noqa: F401 - fixture


@pytest.fixture
def repo(git_repo):
    """Alias: these tests read better talking about "the repo"."""
    return git_repo


def specs(*panes: tuple[str, str, str]) -> list[runtime.PaneSpec]:
    return [runtime.PaneSpec(pane, agent, name) for pane, agent, name in panes]


def test_host_runtime_prepares_a_worktree_per_agent(repo):
    launches = runtime.HostRuntime().prepare(
        specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
        workspace="ws",
        task="t0",
        cwd=str(repo),
    )
    for launch, name, expected in zip(
        launches,
        ("alpha", "beta"),
        (runtime.AGENT_COMMANDS["claude"], runtime.AGENT_COMMANDS["codex"]),
        strict=True,
    ):
        assert launch.cwd.endswith(f"/worktrees/ws/t0/{name}")
        assert launch.keys == (f"cd {launch.cwd}", expected)
        assert Path(launch.cwd).is_dir()


def test_host_runtime_passes_raw_commands_through(tmp_path):
    """A non-repo target: no `cd`, and the raw command is launched verbatim."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (launch,) = runtime.HostRuntime().prepare(
        specs(("%1", "echo hi", "alpha")), workspace="ws", task="t0", cwd=str(plain)
    )
    assert launch.cwd == str(plain)
    assert launch.keys == ("echo hi",)


def test_host_runtime_sends_nothing_for_an_empty_agent(tmp_path):
    (launch,) = runtime.HostRuntime().prepare(
        specs(("%1", "", "alpha")), workspace="ws", task="t0", cwd=str(tmp_path)
    )
    assert launch.keys == ()


class RecordingRuntime:
    """A stand-in for the sandbox runtime: it prepares launches and nothing else."""

    kind = "recording"

    def __init__(self):
        self.seen: list[runtime.PaneSpec] = []
        self.scope: tuple = ()
        self.preflighted: list[list[str]] = []
        self.socket = ""

    def preflight(self, agents, *, workspace, task, cwd):
        self.preflighted.append(list(agents))

    def rollback(self):
        return []

    def prepare(self, panes, *, workspace, task, cwd, socket=""):
        self.seen = list(panes)
        self.scope = (workspace, task, cwd)
        self.socket = socket
        return [
            runtime.Launch(
                pane=spec.pane,
                cwd=f"/sandbox/{spec.name}",
                keys=(f"sbx run --name {spec.name}",),
            )
            for spec in panes
        ]


def test_a_custom_runtime_owns_launch_and_cwd(repo, tmux_calls):
    fake = RecordingRuntime()
    window = fake_tmux.new_window()
    random.seed(1234)
    grid = core._build_grid(  # noqa: SLF001
        window,
        1,
        2,
        ["claude", "codex"],
        str(repo),
        workspace="ws",
        task="t0",
        runtime=fake,
    )

    # The runtime receives full pane identity and the grid's scope...
    assert fake.scope == ("ws", "t0", str(repo))
    assert [(s.pane, s.agent) for s in fake.seen] == [("%1", "claude"), ("%2", "codex")]

    # ...and its launches are what actually reach the panes.
    sent = [entry for entry in window.server.log if entry[0] == "send_keys"]
    assert sent == [
        ("send_keys", "%1", f"sbx run --name {fake.seen[0].name}"),
        ("send_keys", "%2", f"sbx run --name {fake.seen[1].name}"),
    ]
    assert [p.cwd for p in grid.agent_panes] == [
        f"/sandbox/{fake.seen[0].name}",
        f"/sandbox/{fake.seen[1].name}",
    ]

    # No host worktrees were created: the runtime never asked for any.
    assert store.worktrees_for("ws", "t0") == []


def test_pane_metadata_and_events_do_not_depend_on_the_runtime(
    repo, tmux_calls
):
    """Same grid, two runtimes: everything except the launch keys is identical."""

    def run(rt) -> tuple[list, list]:
        window = fake_tmux.new_window()
        random.seed(1234)
        core._build_grid(  # noqa: SLF001
            window, 1, 2, ["claude", "codex"], str(repo),
            workspace="ws", task="t0", runtime=rt,
        )
        tmux = [e for e in window.server.log if e[0] != "send_keys"]
        return tmux, list(tmux_calls)

    host_tmux, host_events = run(runtime.HostRuntime())
    tmux_calls.clear()
    sandbox_tmux, sandbox_events = run(RecordingRuntime())

    assert host_tmux == sandbox_tmux
    assert host_events == sandbox_events


def test_build_grid_defaults_to_the_host_runtime(repo, tmux_calls):
    window = fake_tmux.new_window()
    random.seed(1234)
    grid = core._build_grid(  # noqa: SLF001
        window, 1, 1, ["claude"], str(repo), workspace="ws", task="t0"
    )
    (pane,) = grid.agent_panes
    assert pane.cwd.endswith(f"/worktrees/ws/t0/{pane.name}")


def test_spawn_agent_grid_forwards_the_runtime(repo, tmux_calls):
    """`spg`'s entry point must reach the seam, not just `_build_grid`."""
    fake = RecordingRuntime()
    window = fake_tmux.new_window()
    session = window.session

    def new_window(window_name, start_directory, attach):
        return fake_tmux.FakeWindow(session, name=window_name)

    session.new_window = new_window  # type: ignore[attr-defined]
    random.seed(1234)
    core.spawn_agent_grid(
        session,  # type: ignore[arg-type]
        window_name="t1",
        nrows=1,
        ncols=1,
        agents=["claude"],
        cwd=str(repo),
        runtime=fake,
    )
    assert fake.scope == ("ws", "t1", str(repo))


# --- spg without -p: the host consequence ---
#
# `HostRuntime._worktrees` returns {} for a falsy cwd, so `spg ws task` with no
# `-p` had ALWAYS skipped per-agent worktrees for host agents. tmux inheritance
# left the panes in a shared directory, so it looked like it worked while the
# feature -- one worktree per agent -- silently never happened. The cli now
# resolves the workspace directory; this covers the consequence in runtime.py,
# which is what actually has to produce the worktrees.


def test_a_resolved_cwd_produces_one_worktree_per_agent(repo, isolate_state):
    """What the cli resolution is for. Nothing covered this: the fix broke no
    existing test, which is precisely the problem."""
    launches = runtime.HostRuntime().prepare(
        specs(("%1", "claude", "alpha"), ("%2", "codex", "beta")),
        workspace="ws",
        task="t0",
        cwd=str(repo),
    )
    root = Path(worktree.task_worktree_root("ws", "t0"))

    for launch, name in zip(launches, ("alpha", "beta"), strict=True):
        assert launch.cwd == str(root / name)
        assert (root / name).is_dir()
        assert launch.keys[0] == f"cd {root / name}"
    # Distinct worktrees, which is the whole point of the feature.
    assert launches[0].cwd != launches[1].cwd
    rows = {r["name"]: r for r in store.worktrees_for("ws", "t0")}
    assert set(rows) == {"alpha", "beta"}
    assert rows["alpha"]["branch"] == "amux/ws/t0/alpha"


def test_an_unresolved_cwd_isolates_nothing(repo, isolate_state, capsys):
    """The bug's shape, pinned so it cannot come back silently: with cwd=None
    the panes share a directory and no worktree or row is created. The cli must
    never hand this down -- and if it does again, this says what happens."""
    launches = runtime.HostRuntime().prepare(
        specs(("%1", "claude", "alpha")), workspace="ws", task="t0", cwd=None
    )
    (launch,) = launches

    assert launch.cwd == ""
    # No `cd`, so the pane keeps whatever tmux gave it -- which is exactly why
    # this looked fine for so long.
    assert launch.keys == (runtime.AGENT_COMMANDS["claude"],)
    assert store.worktrees_for("ws", "t0") == []
    assert not Path(worktree.task_worktree_root("ws", "t0")).exists()
    # Still fail-soft, but no longer silent: an unresolved directory is a
    # caller that did not work out where the grid lives, unlike a non-repo
    # target which is a deliberate choice. They used to share this branch
    # without a word, which is how this went unnoticed.
    out = capsys.readouterr().out
    assert "no directory resolved for ws/t0" in out
    assert "-p" in out


def test_a_non_repo_target_stays_quiet(tmp_path, isolate_state, capsys):
    """The other side of that branch: sharing a directory because the target is
    not a repository is intended behaviour and must not nag."""
    plain = tmp_path / "plain"
    plain.mkdir()
    runtime.HostRuntime().prepare(
        specs(("%1", "claude", "alpha")), workspace="ws", task="t0", cwd=str(plain)
    )
    assert "no directory resolved" not in capsys.readouterr().out


def test_spawn_agent_grid_isolates_agents_when_given_a_path(repo, tmux_calls):
    """End to end through the seam's entry point, since that is what `spg`
    calls once the cli has resolved the directory."""
    window = fake_tmux.new_window()
    session = window.session
    session.new_window = lambda window_name, start_directory, attach: (
        fake_tmux.FakeWindow(session, name=window_name)
    )
    random.seed(1234)
    grid = core.spawn_agent_grid(
        session,  # type: ignore[arg-type]
        window_name="t0",
        nrows=1,
        ncols=2,
        agents=["claude", "codex"],
        cwd=str(repo),
    )
    cwds = [p.cwd for p in grid.agent_panes]
    assert len(set(cwds)) == 2, "each agent must get its own worktree"
    for pane in grid.agent_panes:
        assert pane.cwd.endswith(f"/worktrees/ws/t0/{pane.name}")
        assert Path(pane.cwd).is_dir()
