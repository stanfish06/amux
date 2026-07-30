from __future__ import annotations

import random
from dataclasses import dataclass

from libtmux import Pane, Server, Session, Window
from libtmux.constants import PaneDirection

from amux.shared import ALIAS

AGENT_COMMANDS = {
    "claude": "claude --dangerously-skip-permissions",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox",
}

AGENT_OPTION = "@amux_agent"
LABEL_OPTION = "@amux_label"
NAME_OPTION = "@amux_name"

ADJECTIVES = [
    "amber", "azure", "bold", "brave", "calm", "clever", "coral", "crimson",
    "dusty", "fuzzy", "gentle", "golden", "happy", "ivory", "jade", "jolly",
    "lucky", "mellow", "misty", "noble", "olive", "pearl", "proud", "purple",
    "quick", "quiet", "rapid", "ruby", "rusty", "scarlet", "shiny", "silent",
    "silver", "sunny", "swift", "teal", "velvet", "violet", "witty", "zesty",
]
NOUNS = [
    "badger", "bear", "comet", "crane", "deer", "eagle", "ember", "falcon",
    "fox", "gecko", "hawk", "heron", "ibis", "koala", "lemur", "lynx",
    "mango", "maple", "meadow", "mole", "newt", "otter", "owl", "panda",
    "pebble", "pepper", "potato", "puma", "quail", "raven", "river", "seal",
    "storm", "tiger", "toad", "walnut", "whale", "wolf", "yak", "zebra",
]


def random_name(taken: set[str]) -> str:
    """Pick a memorable adjective-noun tag (e.g. brave-hawk) not in `taken`."""
    combos = [f"{a}-{n}" for a in ADJECTIVES for n in NOUNS]
    available = [c for c in combos if c not in taken]
    if available:
        return random.choice(available)
    base = random.choice(combos)
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"


# Dedicated tmux server: keeps agent workspaces out of regular tmux sessions.
DEFAULT_SOCKET = "amux-root"


def get_server(socket_name: str | None = None) -> Server:
    return Server(socket_name=socket_name or DEFAULT_SOCKET)


@dataclass
class AgentSpace:
    session: Session
    agent_grids: list[AgentGrid]
    cwd: str
    project_name: str

    def terminate(self):
        self.session.cmd("kill-session")

    def _print_identity(self):
        print(f"{ALIAS['session']} {self.project_name} ({self.session.id}) @ {self.cwd}")


@dataclass
class AgentGrid:
    window: Window
    agent_panes: list[AgentPane]
    cwd: str
    task_name: str

    def terminate(self):
        self.window.cmd("kill-window")

    def _print_identity(self):
        print(f"{ALIAS['window']} {self.task_name} ({self.window.id}) @ {self.cwd}")


@dataclass
class AgentPane:
    pane: Pane
    cwd: str
    agent_name: str
    label: str
    name: str = ""

    @property
    def is_agent(self) -> bool:
        return self.agent_name in AGENT_COMMANDS

    @property
    def target_pane(self) -> str:
        assert self.pane.id
        return self.pane.id

    def terminate(self):
        self.pane.cmd("kill-pane")

    def _print_identity(self):
        print(
            f"{ALIAS['pane']} {self.name} ({self.agent_name} {self.label}) "
            f"{self.pane.id} @ {self.cwd}"
        )


def _pane_option(pane: Pane, name: str) -> str | None:
    out = pane.cmd("show-options", "-pqv", name).stdout
    return out[0] if out else None


def _taken_names(session: Session) -> set[str]:
    names = set()
    for window in session.windows:
        for pane in window.panes:
            name = _pane_option(pane, NAME_OPTION)
            if name:
                names.add(name)
    return names


def _split_evenly(pane: Pane, n: int, direction: PaneDirection, cwd: str | None) -> list[Pane]:
    panes = [pane]
    for i in range(1, n):
        remaining = n - i
        pct = round(100 * remaining / (remaining + 1))
        panes.append(
            panes[-1].split(direction=direction, size=f"{pct}%", start_directory=cwd)
        )
    return panes


def _build_grid(
    window: Window,
    nrows: int,
    ncols: int,
    agent: str,
    cwd: str | None,
) -> AgentGrid:
    command = AGENT_COMMANDS.get(agent, agent)
    taken = _taken_names(window.session)
    rows = _split_evenly(window.panes[0], nrows, PaneDirection.Below, cwd)
    agent_panes = []
    for i, row_pane in enumerate(rows):
        cols = _split_evenly(row_pane, ncols, PaneDirection.Right, cwd)
        for j, pane in enumerate(cols):
            label = f"r{i}c{j}"
            name = random_name(taken)
            taken.add(name)
            # Keep the name tag stable: block apps/prompts from re-titling the pane.
            pane.cmd("set-option", "-p", "allow-set-title", "off")
            pane.cmd("select-pane", "-T", f"{name}[{agent}]")
            pane.cmd("set-option", "-p", AGENT_OPTION, agent)
            pane.cmd("set-option", "-p", LABEL_OPTION, label)
            pane.cmd("set-option", "-p", NAME_OPTION, name)
            if command:
                pane.send_keys(command)
            agent_panes.append(
                AgentPane(
                    pane=pane,
                    cwd=cwd or pane.pane_current_path or "",
                    agent_name=agent,
                    label=label,
                    name=name,
                )
            )
    return AgentGrid(
        window=window,
        agent_panes=agent_panes,
        cwd=cwd or "",
        task_name=window.name or "",
    )


def spawn_agent_space(
    server: Server,
    session_path: str,
    session_name: str,
    init_grid_nrows: int = 1,
    init_grid_ncols: int = 1,
    init_grid_agent: str = "claude",
    init_task_name: str = "task0",
) -> AgentSpace:
    if server.has_session(session_name):
        raise ValueError(f"{ALIAS['session']} '{session_name}' already exists")
    # Detached sessions get a virtual size; make it big enough to split evenly.
    width = max(200, 80 * init_grid_ncols)
    height = max(50, 24 * init_grid_nrows)
    server.cmd(
        "new-session", "-d",
        "-s", session_name,
        "-c", session_path,
        "-x", str(width),
        "-y", str(height),
    )
    session = server.sessions.get(session_name=session_name)
    assert session is not None
    window = session.windows[0]
    window.rename_window(init_task_name)
    grid = _build_grid(window, init_grid_nrows, init_grid_ncols, init_grid_agent, session_path)
    return AgentSpace(
        session=session,
        agent_grids=[grid],
        cwd=session_path,
        project_name=session_name,
    )


def spawn_agent_grid(
    session: Session,
    window_name: str,
    nrows: int,
    ncols: int,
    agent: str = "claude",
    cwd: str | None = None,
) -> AgentGrid:
    window = session.new_window(window_name=window_name, start_directory=cwd, attach=False)
    return _build_grid(window, nrows, ncols, agent, cwd)


def load_agent_pane(pane: Pane) -> AgentPane:
    return AgentPane(
        pane=pane,
        cwd=pane.pane_current_path or "",
        agent_name=_pane_option(pane, AGENT_OPTION) or pane.pane_current_command or "",
        label=_pane_option(pane, LABEL_OPTION) or pane.id or "",
        name=_pane_option(pane, NAME_OPTION) or "",
    )


def load_agent_grid(window: Window) -> AgentGrid:
    panes = [load_agent_pane(p) for p in window.panes]
    return AgentGrid(
        window=window,
        agent_panes=panes,
        cwd=panes[0].cwd if panes else "",
        task_name=window.name or "",
    )


def load_agent_space(session: Session) -> AgentSpace:
    grids = [load_agent_grid(w) for w in session.windows]
    return AgentSpace(
        session=session,
        agent_grids=grids,
        cwd=grids[0].cwd if grids else "",
        project_name=session.name or "",
    )


def load_agent_spaces(server: Server) -> list[AgentSpace]:
    return [load_agent_space(s) for s in server.sessions]


def spawn_human_space():
    pass
