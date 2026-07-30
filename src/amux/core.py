from __future__ import annotations

from dataclasses import dataclass

from libtmux import Pane, Server, Window

AGENT_COMMANDS = {
    "claude": "claude --dangerously-skip-permissions",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox",
}


@dataclass
class AgentSpace:
    agent_grids: list[AgentGrid]
    cwd: str
    project_name: str

    def terminate(self):
        pass

    def _print_identity(self):
        pass


@dataclass
class AgentGrid:
    window: Window
    agent_panes: list[AgentPane]
    cwd: str
    task_name: str

    def terminate(self):
        pass

    def _print_identity(self):
        pass


@dataclass
class AgentPane:
    pane: Pane
    cwd: str
    agent_name: str
    label: str

    @property
    def is_agent(self) -> bool:
        return self.agent_name in AGENT_COMMANDS

    @property
    def target_pane(self) -> str:
        assert self.pane.id
        return self.pane.id

    def terminate(self):
        pass

    def _print_identity(self):
        pass


def spawn_human_space():
    pass


def spawn_agent_space(
    server: Server,
    session_path: str,
    session_name: str,
    init_grid_nrows: int = 1,
    init_grid_ncols: int = 1,
    init_grid_agent: str = "claude",
):
    pass


def spawn_agent_grid(
    window_name: str,
    nrows: int,
    ncols: int,
    agent: str | None = "claude",
):
    pass
