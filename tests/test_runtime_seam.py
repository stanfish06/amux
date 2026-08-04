"""The runtime seam itself.

`test_host_grid_snapshot` proves the host runtime still behaves exactly as it
did. These tests prove the other half: that the seam is real — a runtime other
than `HostRuntime` fully controls where a pane works and what it runs, while
pane identity, tmux metadata, exit hooks and spawn events stay identical.
"""

from __future__ import annotations

import random
from pathlib import Path

import fake_tmux
from amux import core, runtime, store
from test_host_grid_snapshot import (  # noqa: F401 - fixtures
    isolated_state,
    repo,
    tmux_calls,
)


def specs(*panes: tuple[str, str, str]) -> list[runtime.PaneSpec]:
    return [runtime.PaneSpec(pane, agent, name) for pane, agent, name in panes]


def test_host_runtime_prepares_a_worktree_per_agent(repo, isolated_state):
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


def test_host_runtime_passes_raw_commands_through(tmp_path, isolated_state):
    """A non-repo target: no `cd`, and the raw command is launched verbatim."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (launch,) = runtime.HostRuntime().prepare(
        specs(("%1", "echo hi", "alpha")), workspace="ws", task="t0", cwd=str(plain)
    )
    assert launch.cwd == str(plain)
    assert launch.keys == ("echo hi",)


def test_host_runtime_sends_nothing_for_an_empty_agent(tmp_path, isolated_state):
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

    def prepare(self, panes, *, workspace, task, cwd):
        self.seen = list(panes)
        self.scope = (workspace, task, cwd)
        return [
            runtime.Launch(
                pane=spec.pane,
                cwd=f"/sandbox/{spec.name}",
                keys=(f"sbx run --name {spec.name}",),
            )
            for spec in panes
        ]


def test_a_custom_runtime_owns_launch_and_cwd(repo, isolated_state, tmux_calls):
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
    repo, isolated_state, tmux_calls
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


def test_build_grid_defaults_to_the_host_runtime(repo, isolated_state, tmux_calls):
    window = fake_tmux.new_window()
    random.seed(1234)
    grid = core._build_grid(  # noqa: SLF001
        window, 1, 1, ["claude"], str(repo), workspace="ws", task="t0"
    )
    (pane,) = grid.agent_panes
    assert pane.cwd.endswith(f"/worktrees/ws/t0/{pane.name}")


def test_spawn_agent_grid_forwards_the_runtime(repo, isolated_state, tmux_calls):
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
