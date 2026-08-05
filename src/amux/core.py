from __future__ import annotations

import random
from dataclasses import dataclass

from libtmux import Pane, Server, Session, Window
from libtmux.constants import PaneDirection

from amux import events, sandbox_hooks, store, worktree
from amux.runtime import (
    AGENT_COMMANDS,
    HOST,
    GridCreationError,
    HostRuntime,
    PaneSpec,
    Runtime,
)
from amux.shared import ALIAS, DEFAULT_SOCKET

AGENT_OPTION = "@amux_agent"
LABEL_OPTION = "@amux_label"
NAME_OPTION = "@amux_name"
MARK_OPTION = "@amux_pane"

ADJECTIVES = [
    "amber",
    "azure",
    "bold",
    "brave",
    "calm",
    "clever",
    "coral",
    "crimson",
    "dusty",
    "fuzzy",
    "gentle",
    "golden",
    "happy",
    "ivory",
    "jade",
    "jolly",
    "lucky",
    "mellow",
    "misty",
    "noble",
    "olive",
    "pearl",
    "proud",
    "purple",
    "quick",
    "quiet",
    "rapid",
    "ruby",
    "rusty",
    "scarlet",
    "shiny",
    "silent",
    "silver",
    "sunny",
    "swift",
    "teal",
    "velvet",
    "violet",
    "witty",
    "zesty",
]
NOUNS = [
    "badger",
    "bear",
    "comet",
    "crane",
    "deer",
    "eagle",
    "ember",
    "falcon",
    "fox",
    "gecko",
    "hawk",
    "heron",
    "ibis",
    "koala",
    "lemur",
    "lynx",
    "mango",
    "maple",
    "meadow",
    "mole",
    "newt",
    "otter",
    "owl",
    "panda",
    "pebble",
    "pepper",
    "potato",
    "puma",
    "quail",
    "raven",
    "river",
    "seal",
    "storm",
    "tiger",
    "toad",
    "walnut",
    "whale",
    "wolf",
    "yak",
    "zebra",
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


def get_server(socket_name: str | None = None) -> Server:
    return Server(socket_name=socket_name or DEFAULT_SOCKET)


def _parse_agent_spec(spec: str) -> tuple[str, int | None]:
    """Split `<agent>[:count]` on the last `:` only when the suffix is all
    digits; raw commands containing colons pass through whole."""
    agent, sep, suffix = spec.rpartition(":")
    if not sep or not suffix.isdigit():
        if not spec:
            raise ValueError("empty agent spec")
        return spec, None
    if not agent:
        raise ValueError(f"malformed agent spec '{spec}'")
    count = int(suffix)
    if count < 1:
        raise ValueError(f"agent count must be >= 1, got '{spec}'")
    return agent, count


def parse_agent_specs(
    specs: list[str], nrows: int | None, ncols: int | None
) -> list[str]:
    """Expand `<agent>[:count]` specs into a per-pane agent list, row-major.

    With a known shape (both dims given), a single countless spec absorbs the
    remainder; with an unknown or partial shape, countless means 1.
    """
    parsed = [_parse_agent_spec(s) for s in specs or ["claude"]]
    countless = [i for i, (_, count) in enumerate(parsed) if count is None]
    if nrows is not None and ncols is not None:
        if len(countless) > 1:
            raise ValueError(
                "at most one agent spec may omit its count when the grid shape is given"
            )
        if countless:
            i = countless[0]
            remainder = nrows * ncols - sum(c for _, c in parsed if c is not None)
            if remainder < 1:
                raise ValueError(
                    f"no panes left for '{parsed[i][0]}' in a {nrows}x{ncols} grid"
                )
            parsed[i] = (parsed[i][0], remainder)
    # Any spec still countless here (unknown/partial shape) means 1.
    return [agent for agent, count in parsed for _ in range(count or 1)]


def resolve_grid_shape(n: int, nrows: int | None, ncols: int | None) -> tuple[int, int]:
    """Fit `n` agents into a grid, deriving whatever `-r`/`-c` left out.

    With neither given, pick the factor pair closest to square, rows <= cols.
    """
    if nrows is not None and ncols is not None:
        if nrows * ncols != n:
            raise ValueError(f"{n} agents do not fit a {nrows}x{ncols} grid")
        return nrows, ncols
    if nrows is not None:
        if n % nrows:
            raise ValueError(f"{n} agents do not divide into {nrows} rows")
        return nrows, n // nrows
    if ncols is not None:
        if n % ncols:
            raise ValueError(f"{n} agents do not divide into {ncols} columns")
        return n // ncols, ncols
    rows = max(d for d in range(1, int(n**0.5) + 1) if n % d == 0)
    return rows, n // rows


@dataclass
class AgentSpace:
    session: Session
    agent_grids: list[AgentGrid]
    cwd: str
    project_name: str

    def terminate(self):
        self.session.cmd("kill-session")

    def _print_identity(self):
        print(
            f"{ALIAS['session']} {self.project_name} ({self.session.id}) @ {self.cwd}"
        )


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
    state: str = "starting"

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


def _socket_name(obj) -> str:
    """Socket of the server behind any libtmux object."""
    return getattr(obj.server, "socket_name", None) or DEFAULT_SOCKET


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


def _rollback(runtime: Runtime) -> list[str]:
    """Unwind a runtime's acquired resources, reporting failures rather than
    raising them. A rollback runs while an error is already propagating, so it
    must never be the thing that surfaces."""
    try:
        return list(runtime.rollback())
    except Exception as exc:  # noqa: BLE001
        return [f"runtime rollback: {exc}"]


def _discard(what: str, teardown) -> str | None:
    """Best-effort tmux teardown. Returns a problem description, or None."""
    try:
        teardown()
    except Exception as exc:  # noqa: BLE001
        return f"{what}: {exc}"
    return None


def _split_evenly(
    pane: Pane, n: int, direction: PaneDirection, cwd: str | None
) -> list[Pane]:
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
    agents: list[str],
    cwd: str | None,
    workspace: str | None = None,
    task: str | None = None,
    runtime: Runtime | None = None,
) -> AgentGrid:
    if len(agents) != nrows * ncols:
        raise ValueError(f"{len(agents)} agents do not fit a {nrows}x{ncols} grid")
    runtime = runtime or HostRuntime()
    taken = _taken_names(window.session)
    rows = _split_evenly(window.panes[0], nrows, PaneDirection.Below, cwd)
    agent_panes = []
    panes_info: list[tuple[Pane, str, str]] = []
    for i, row_pane in enumerate(rows):
        cols = _split_evenly(row_pane, ncols, PaneDirection.Right, cwd)
        for j, pane in enumerate(cols):
            agent = agents[i * ncols + j]
            label = f"r{i}c{j}"
            name = random_name(taken)
            taken.add(name)
            # Keep the name tag stable: block apps/prompts from re-titling the pane.
            pane.cmd("set-option", "-p", "allow-set-title", "off")
            pane.cmd("select-pane", "-T", f"{name}[{agent}]")
            pane.cmd("set-option", "-p", AGENT_OPTION, agent)
            pane.cmd("set-option", "-p", LABEL_OPTION, label)
            pane.cmd("set-option", "-p", NAME_OPTION, name)
            pane.cmd("set-option", "-p", MARK_OPTION, "1")
            pane.set_hook(
                "pane-exited", "run-shell 'amux event emit exit --pane #{hook_pane}'"
            )
            panes_info.append((pane, agent, name))

    # The runtime decides where each pane works and what it runs; everything
    # tmux-facing stays here so pane metadata and events are runtime-agnostic.
    socket = _socket_name(window)
    try:
        launches = {
            launch.pane: launch
            for launch in runtime.prepare(
                [PaneSpec(p.id or "", agent, name) for p, agent, name in panes_info],
                workspace=workspace,
                task=task,
                cwd=cwd,
                socket=socket,
            )
        }
    except Exception as exc:
        # A grid is all-or-nothing: half a grid leaves sandboxes nothing will
        # ever attach to and rows a later integrate would try to merge.
        raise GridCreationError(exc, _rollback(runtime)) from exc

    for pane, agent, name in panes_info:
        launch = launches[pane.id or ""]
        pane_cwd = launch.cwd or pane.pane_current_path or ""
        for keys in launch.keys:
            pane.send_keys(keys)
        events.emit("spawn", pane=pane.id, agent=agent, socket=socket)
        agent_panes.append(
            AgentPane(
                pane=pane,
                cwd=pane_cwd,
                agent_name=agent,
                label=label_for(pane),
                name=name,
            )
        )
    return AgentGrid(
        window=window,
        agent_panes=agent_panes,
        cwd=cwd or "",
        task_name=window.name or "",
    )


def label_for(pane: Pane) -> str:
    return _pane_option(pane, LABEL_OPTION) or pane.id or ""


def spawn_agent_space(
    server: Server,
    session_path: str,
    session_name: str,
    init_grid_nrows: int = 1,
    init_grid_ncols: int = 1,
    init_grid_agents: list[str] | None = None,
    init_task_name: str = "task0",
    runtime: Runtime | None = None,
) -> AgentSpace:
    if server.has_session(session_name):
        raise ValueError(f"{ALIAS['session']} '{session_name}' already exists")
    agents = init_grid_agents or ["claude"] * (init_grid_nrows * init_grid_ncols)
    # Before the session exists: a runtime that cannot run this grid must leave
    # no tmux session, sandbox, git reference or registry row behind.
    runtime = runtime or HostRuntime()
    runtime.preflight(
        agents, workspace=session_name, task=init_task_name, cwd=session_path
    )
    # Detached sessions get a virtual size; make it big enough to split evenly.
    width = max(200, 80 * init_grid_ncols)
    height = max(50, 24 * init_grid_nrows)
    server.cmd(
        "new-session",
        "-d",
        "-s",
        session_name,
        "-c",
        session_path,
        "-x",
        str(width),
        "-y",
        str(height),
    )
    session = server.sessions.get(session_name=session_name)
    assert session is not None
    window = session.windows[0]
    window.rename_window(init_task_name)
    try:
        grid = _build_grid(
            window,
            init_grid_nrows,
            init_grid_ncols,
            agents,
            session_path,
            workspace=session_name,
            task=init_task_name,
            runtime=runtime,
        )
    except BaseException as exc:
        # This call created the session, so this call takes it away again: a
        # failed spawn must not leave a workspace holding dead panes.
        problem = _discard("kill session", lambda: session.cmd("kill-session"))
        if problem:
            if isinstance(exc, GridCreationError):
                exc.add_cleanup_failure(problem)
            else:
                print(f"amux: {problem}")
        raise
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
    agents: list[str] | None = None,
    cwd: str | None = None,
    runtime: Runtime | None = None,
) -> AgentGrid:
    agents = agents or ["claude"] * (nrows * ncols)
    # Before the window exists, for the same reason as `spawn_agent_space`.
    runtime = runtime or HostRuntime()
    runtime.preflight(
        agents, workspace=session.name or "", task=window_name, cwd=cwd
    )
    window = session.new_window(
        window_name=window_name, start_directory=cwd, attach=False
    )
    try:
        return _build_grid(
            window,
            nrows,
            ncols,
            agents,
            cwd,
            workspace=session.name or "",
            task=window_name,
            runtime=runtime,
        )
    except BaseException as exc:
        # Only the window: the workspace pre-existed this call and other tasks
        # are still running in it.
        problem = _discard("kill task window", lambda: window.cmd("kill-window"))
        if problem:
            if isinstance(exc, GridCreationError):
                exc.add_cleanup_failure(problem)
            else:
                print(f"amux: {problem}")
        raise


def load_agent_pane(pane: Pane, facts: events.PaneFacts | None = None) -> AgentPane:
    """One tmux query per pane, not one per option; `facts` carries them all."""
    facts = facts or events.pane_facts(pane.id or "", _socket_name(pane))
    state, _ = events.pane_status(pane.id or "", facts=facts)
    return AgentPane(
        pane=pane,
        cwd=facts.cwd,
        agent_name=facts.agent or facts.command,
        label=facts.label or pane.id or "",
        name=facts.name,
        state=(state or "idle") if facts.kind == "amux" else "-",
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


def _roster_entry(pane: Pane) -> dict:
    facts = events.pane_facts(pane.id or "", _socket_name(pane))
    _, last = events.pane_status(pane.id or "", facts=facts)
    ap = load_agent_pane(pane, facts=facts)
    wt = store.worktree_for_pane(pane.id or "", since=facts.boundary)
    entry = {
        "name": ap.name,
        "agent": ap.agent_name,
        "label": ap.label,
        "pane": pane.id or "",
        "state": ap.state,
        "cwd": ap.cwd,
        "last_event": (
            {"kind": last.kind, "ts": last.ts, "detail": last.detail} if last else None
        ),
    }
    if wt:
        entry["branch"] = wt["branch"]
        entry["worktree"] = wt["path"]
        entry["repo"] = wt["repo"]
        # Only for a row that actually has a host worktree. A sandbox row
        # carries path='', and `git -C ''` is a documented no-op that reports
        # the *calling process's* checkout -- so an unguarded call here
        # attributes the host's HEAD subject to a sandboxed agent that never
        # made that commit. A sandbox's real commit arrives through its
        # `sandbox-<name>` remote, not from a host path.
        if wt["path"]:
            entry["last_commit"] = worktree.latest_commit_subject(wt["path"])
        entry.update(runtime_fields(wt))
    return entry


def runtime_fields(row) -> dict:
    """Runtime identity for a roster entry, or {} for a host agent.

    Empty for `host` on purpose: adding the keys unconditionally would change
    every existing consumer's output for agents whose runtime has not changed.
    """
    runtime = (row["runtime"] if "runtime" in row.keys() else "") or HOST
    if runtime == HOST:
        return {}
    missing = missing_state_kinds(row)
    return {
        "runtime": runtime,
        "runtime_status": row["runtime_status"] or "",
        "sandbox_name": row["sandbox_name"] or "",
        "sandbox_id": row["sandbox_id"] or "",
        # A degraded agent cannot report every state, so its resolved state is
        # not authoritative and must never be presented as if it were.
        "state_degraded": bool(missing),
        "missing_kinds": list(missing),
    }


def missing_state_kinds(row) -> tuple[str, ...]:
    """State kinds this execution's agent cannot report.

    Derived rather than stored: it is a pure function of the agent and which
    hook mechanism its image supports, so every reader computes the same answer
    from one recorded fact instead of three of them storing a list.
    """
    mechanism = (
        row["hook_mechanism"] if "hook_mechanism" in row.keys() else ""
    ) or ""
    if not mechanism:
        # Nothing recorded: a host row, or an execution from before the
        # mechanism was tracked. Claiming degradation we have not observed
        # would be as wrong as hiding it.
        return ()
    return sandbox_hooks.missing_kinds(
        row["agent"], hooks_supported=mechanism == "hooks"
    )


def build_context(server: Server, pane_id: str) -> dict:
    home = None
    for session in server.sessions:
        for window in session.windows:
            if any(p.id == pane_id for p in window.panes):
                home = (session, window)
    if home is None:
        raise ValueError(
            f"no {ALIAS['pane']} {pane_id} on this server; "
            f"run inside an amux {ALIAS['pane']} or pass --pane"
        )
    session, window = home
    self_entry = None
    team = []
    for w in [window, *[x for x in session.windows if x.id != window.id]]:
        agents = []
        for p in w.panes:
            entry = _roster_entry(p)
            agents.append(entry)
            if p.id == pane_id:
                self_entry = {
                    **entry,
                    "task": w.name or "",
                    "workspace": session.name or "",
                }
        team.append({"task": w.name or "", "agents": agents})
    assert self_entry is not None
    notes = store.visible_notes(
        workspace=self_entry["workspace"],
        task=self_entry["task"],
        pane=pane_id,
        # This is the path that briefs an agent, so it is the one that most
        # needs the repo filter: workspace/task are reusable tmux labels.
        repo=self_entry.get("repo"),
    )
    return {"self": self_entry, "team": team, "notes": notes}


# future feat, spawn and space where humans work and colab
def spawn_human_space():
    pass
