from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, replace

from libtmux import Pane, Server, Session, Window
from libtmux.constants import PaneDirection

from amux import events, roles, sandbox_hooks, store, worktree
from amux.runtime import (
    AGENT_COMMANDS,
    HOST,
    GridCreationError,
    HostRuntime,
    PaneSpec,
    Runtime,
)
from amux.shared import ALIAS, DEFAULT_SOCKET, AgentRequest, report

AGENT_OPTION = "@amux_agent"
LABEL_OPTION = "@amux_label"
NAME_OPTION = "@amux_name"
MARK_OPTION = "@amux_pane"
MODEL_OPTION = "@amux_model"
EFFORT_OPTION = "@amux_effort"
ROLE_OPTION = "@amux_role"

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


RAW_COMMAND_HINT = (
    "a model id containing '/' or ending in ':<digits>' must be launched as a "
    "raw command instead, e.g. -a 'claude --model openai/gpt-5'"
)


def _tune(agent: str, head: str, spec: str) -> AgentRequest:
    rest = head[len(agent) :]
    model = effort = ""
    if rest.startswith("@"):
        model, slash, tail = rest[1:].partition("/")
        if not model:
            raise ValueError(f"empty model in agent spec '{spec}'")
        if slash:
            effort = tail
    elif rest.startswith("/"):
        effort = rest[1:]
        slash = "/"
    else:
        slash = ""
    if slash and not effort:
        raise ValueError(f"empty effort in agent spec '{spec}'")
    return AgentRequest(agent=agent, model=model, effort=effort)


def _parse_agent_spec(spec: str) -> tuple[AgentRequest, int | None]:
    role, eq, rest = spec.partition("=")
    if not (eq and roles.ROLE_NAME.fullmatch(role)):
        return _parse_plain_spec(spec)
    if not rest:
        raise ValueError(f"role '{role}' needs an agent, e.g. -a {role}=claude")
    request, count = _parse_plain_spec(rest)
    if request.agent not in roles.ROLE_AGENTS:
        raise ValueError(
            f"role '{role}' runs on {'/'.join(roles.ROLE_AGENTS)}, not the raw "
            f"command '{rest}'; if '{role}=' is an environment assignment, "
            f"write -a 'env {spec}'"
        )
    return replace(request, role=role), count


def _parse_plain_spec(spec: str) -> tuple[AgentRequest, int | None]:
    head, sep, suffix = spec.rpartition(":")
    count = None
    if not sep or not suffix.isdigit():
        head = spec
    elif not head:
        raise ValueError(f"malformed agent spec '{spec}'")
    else:
        count = int(suffix)
        if count < 1:
            raise ValueError(
                f"agent count must be >= 1, got '{spec}'; {RAW_COMMAND_HINT}"
            )
    if not head:
        raise ValueError("empty agent spec")
    agent = head.split("@", 1)[0].split("/", 1)[0]
    if agent not in AGENT_COMMANDS:
        return AgentRequest(agent=head), count
    return _tune(agent, head, spec), count


def parse_agent_specs(
    specs: list[str], nrows: int | None, ncols: int | None
) -> list[AgentRequest]:
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
                    f"no panes left for '{parsed[i][0].agent}' in a "
                    f"{nrows}x{ncols} grid"
                )
            parsed[i] = (parsed[i][0], remainder)
    return [request for request, count in parsed for _ in range(count or 1)]


def resolve_grid_shape(n: int, nrows: int | None, ncols: int | None) -> tuple[int, int]:
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
class AgentRole:
    name: str
    persona: str
    tasks: list[str]

    def assemble_system_prompt(self):
        pass


@dataclass
class AgentPane:
    pane: Pane
    cwd: str
    harness_name: str
    label: str
    name: str = ""
    role: AgentRole | None = None
    state: str = "starting"

    @property
    def is_agent(self) -> bool:
        return self.harness_name in AGENT_COMMANDS

    @property
    def target_pane(self) -> str:
        assert self.pane.id
        return self.pane.id

    def terminate(self):
        self.pane.cmd("kill-pane")

    def _print_identity(self):
        print(
            f"{ALIAS['pane']} {self.name} ({self.harness_name} {self.label}) "
            f"{self.pane.id} @ {self.cwd}"
        )


def _socket_name(obj) -> str:
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


def _next_name(agent: str, resumable: dict[str, list[str]], taken: set[str]) -> str:
    while resumable.get(agent):
        candidate = resumable[agent].pop(0)
        if candidate not in taken:
            return candidate
    return random_name(taken)


def _rollback(runtime: Runtime) -> list[str]:
    try:
        return list(runtime.rollback())
    except Exception as exc:  # noqa: BLE001
        return [f"runtime rollback: {exc}"]


def _discard(what: str, teardown) -> str | None:
    try:
        teardown()
    except Exception as exc:  # noqa: BLE001
        return f"{what}: {exc}"
    return None


_CARET = re.compile(r"^\s*[>›❯]\s*(?:\S.*)?$")

_CHOOSER = re.compile(r"^\W*[1-9]\.\s+\S", re.MULTILINE)
_FORM = re.compile(r"\b\d to \w+\s*·\s*\d to \w+")
_FORM_LINES = 30

BOOTSTRAP_READY_TIMEOUT_S = 45.0
BOOTSTRAP_POLL_S = 0.5
BOOTSTRAP_SUBMIT_PAUSE_S = 0.4
_PROBE_CHARS = 40


def interface_ready(capture: str) -> bool:
    lines = capture.splitlines()
    last = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    carets = [i for i, line in enumerate(lines) if _CARET.match(line)]
    if not any(i < last for i in carets):
        return False
    return not (chooser_in_view(capture) or form_in_view(capture))


def form_in_view(capture: str) -> str:
    for line in capture.splitlines()[-_FORM_LINES:]:
        match = _FORM.search(line)
        if match:
            return match.group(0)
    return ""


def chooser_in_view(capture: str) -> bool:
    lines = capture.splitlines()
    carets = [i for i, line in enumerate(lines) if _CARET.match(line)]
    if not carets:
        return False
    return bool(_CHOOSER.search("\n".join(lines[carets[-1] :])))


def _not_ready_reason(capture: str, timeout: float) -> str:
    form = form_in_view(capture)
    if form:
        return (
            f"after {timeout:g}s a prompt meant for a human was still on its "
            f"screen ('{form}'); nothing was typed, so answer it and resend"
        )
    if chooser_in_view(capture):
        return (
            f"after {timeout:g}s it was still waiting on a prompt of its own, "
            "most likely asking whether to trust this directory -- every agent "
            "gets a fresh worktree, so answer it and the agent runs normally"
        )
    return f"its interface was not ready within {timeout:g}s"


def _probe(text: str) -> str:
    return " ".join(text.split())[-_PROBE_CHARS:]


def _held_in_the_composer(capture: str, text: str) -> bool:
    lines = capture.splitlines()
    carets = [i for i, line in enumerate(lines) if _CARET.match(line)]
    if not carets:
        return False
    return _probe(text) in " ".join(" ".join(lines[carets[-1] :]).split())


def send_bootstrap(
    pane: Pane,
    text: str,
    *,
    timeout: float | None = None,
    poll: float | None = None,
    pause: float | None = None,
    clock=time.monotonic,
    sleep=time.sleep,
) -> str:
    timeout = BOOTSTRAP_READY_TIMEOUT_S if timeout is None else timeout
    poll = BOOTSTRAP_POLL_S if poll is None else poll
    pause = BOOTSTRAP_SUBMIT_PAUSE_S if pause is None else pause
    try:
        return submit_to_interface(
            pane,
            text,
            timeout=timeout,
            poll=poll,
            pause=pause,
            clock=clock,
            sleep=sleep,
        )
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001 - see the docstring
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def submit_to_interface(
    pane: Pane,
    text: str,
    *,
    timeout: float,
    poll: float,
    pause: float,
    clock,
    sleep,
    on_submit=None,
) -> str:
    deadline = clock() + timeout
    while True:
        capture = _capture(pane)
        if interface_ready(capture):
            break
        remaining = deadline - clock()
        if remaining <= 0:
            return _not_ready_reason(capture, timeout)
        sleep(min(poll, remaining))

    if on_submit is not None:
        on_submit()
    pane.send_keys(text, enter=False, suppress_history=False, literal=True)
    sleep(pause)
    pane.enter()
    sleep(pause)

    if not _held_in_the_composer(_capture(pane), text):
        return ""
    pane.enter()
    sleep(pause)
    if _held_in_the_composer(_capture(pane), text):
        return "its interface did not accept the message; it is on the input line"
    return ""


def _capture(pane: Pane) -> str:
    captured = pane.capture_pane()
    return captured if isinstance(captured, str) else "\n".join(captured)


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
    agents: list[AgentRequest],
    cwd: str | None,
    workspace: str | None = None,
    task: str | None = None,
    runtime: Runtime | None = None,
) -> AgentGrid:
    if len(agents) != nrows * ncols:
        raise ValueError(f"{len(agents)} agents do not fit a {nrows}x{ncols} grid")
    runtime = runtime or HostRuntime()
    taken = _taken_names(window.session)
    resumable = runtime.resumable_names(workspace=workspace, task=task, cwd=cwd)
    rows = _split_evenly(window.panes[0], nrows, PaneDirection.Below, cwd)
    agent_panes = []
    panes_info: list[tuple[Pane, AgentRequest, str]] = []
    for i, row_pane in enumerate(rows):
        cols = _split_evenly(row_pane, ncols, PaneDirection.Right, cwd)
        for j, pane in enumerate(cols):
            request = agents[i * ncols + j]
            agent = request.agent
            label = f"r{i}c{j}"
            name = _next_name(agent, resumable, taken)
            taken.add(name)
            pane.cmd("set-option", "-p", "allow-set-title", "off")
            pane.cmd("select-pane", "-T", f"{name}[{request.role or agent}]")
            pane.cmd("set-option", "-p", AGENT_OPTION, agent)
            pane.cmd("set-option", "-p", LABEL_OPTION, label)
            pane.cmd("set-option", "-p", NAME_OPTION, name)
            pane.cmd("set-option", "-p", MARK_OPTION, "1")
            if request.model:
                pane.cmd("set-option", "-p", MODEL_OPTION, request.model)
            if request.effort:
                pane.cmd("set-option", "-p", EFFORT_OPTION, request.effort)
            if request.role:
                pane.cmd("set-option", "-p", ROLE_OPTION, request.role)
            pane.set_hook(
                "pane-exited", "run-shell 'amux event emit exit --pane #{hook_pane}'"
            )
            panes_info.append((pane, request, name))

    socket = _socket_name(window)
    try:
        launches = {
            launch.pane: launch
            for launch in runtime.prepare(
                [
                    PaneSpec(p.id or "", r.agent, name, r.model, r.effort, r.role)
                    for p, r, name in panes_info
                ],
                workspace=workspace,
                task=task,
                cwd=cwd,
                socket=socket,
            )
        }
    except Exception as exc:
        raise GridCreationError(exc, _rollback(runtime)) from exc

    for pane, request, name in panes_info:
        launch = launches[pane.id or ""]
        pane_cwd = launch.cwd or pane.pane_current_path or ""
        for keys in launch.keys:
            pane.send_keys(keys)
        events.emit("spawn", pane=pane.id, agent=request.agent, socket=socket)
        agent_panes.append(
            AgentPane(
                pane=pane,
                cwd=pane_cwd,
                harness_name=request.agent,
                label=label_for(pane),
                name=name,
            )
        )

    for pane, _agent, name in panes_info:
        bootstrap = launches[pane.id or ""].bootstrap
        if not bootstrap:
            continue
        problem = send_bootstrap(pane, bootstrap)
        if problem:
            report(
                f"amux: {name} was not given amux's skill pointer "
                f"({problem}); it is running and will need telling by hand"
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
    init_grid_agents: list[AgentRequest] | None = None,
    init_task_name: str = "task0",
    runtime: Runtime | None = None,
) -> AgentSpace:
    if server.has_session(session_name):
        raise ValueError(f"{ALIAS['session']} '{session_name}' already exists")
    agents = init_grid_agents or [AgentRequest("claude")] * (
        init_grid_nrows * init_grid_ncols
    )
    runtime = runtime or HostRuntime()
    agents = roles.resolve(agents, session_path, runtime.kind)
    runtime.preflight(
        agents, workspace=session_name, task=init_task_name, cwd=session_path
    )
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
    agents: list[AgentRequest] | None = None,
    cwd: str | None = None,
    runtime: Runtime | None = None,
) -> AgentGrid:
    agents = agents or [AgentRequest("claude")] * (nrows * ncols)
    runtime = runtime or HostRuntime()
    agents = roles.resolve(agents, cwd, runtime.kind)
    runtime.preflight(agents, workspace=session.name or "", task=window_name, cwd=cwd)
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
        problem = _discard("kill task window", lambda: window.cmd("kill-window"))
        if problem:
            if isinstance(exc, GridCreationError):
                exc.add_cleanup_failure(problem)
            else:
                print(f"amux: {problem}")
        raise


def load_agent_pane(pane: Pane, facts: events.PaneFacts | None = None) -> AgentPane:
    facts = facts or events.pane_facts(pane.id or "", _socket_name(pane))
    state, _ = events.pane_status(pane.id or "", facts=facts)
    return AgentPane(
        pane=pane,
        cwd=facts.cwd,
        harness_name=facts.agent or facts.command,
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
        "agent": ap.harness_name,
        "label": ap.label,
        "pane": pane.id or "",
        "state": ap.state,
        "cwd": ap.cwd,
        "last_event": (
            {"kind": last.kind, "ts": last.ts, "detail": last.detail} if last else None
        ),
    }
    if facts.model:
        entry["model"] = facts.model
    if facts.effort:
        entry["effort"] = facts.effort
    if facts.role:
        entry["role"] = facts.role
    if wt:
        entry["branch"] = wt["branch"]
        entry["worktree"] = wt["path"]
        entry["repo"] = wt["repo"]
        if wt["path"]:
            entry["last_commit"] = worktree.latest_commit_subject(wt["path"])
        entry.update(runtime_fields(wt))
    return entry


def runtime_fields(row) -> dict:
    runtime = (row["runtime"] if "runtime" in row.keys() else "") or HOST
    if runtime == HOST:
        return {}
    missing = missing_state_kinds(row)
    return {
        "runtime": runtime,
        "runtime_status": row["runtime_status"] or "",
        "sandbox_name": row["sandbox_name"] or "",
        "sandbox_id": row["sandbox_id"] or "",
        "state_degraded": bool(missing),
        "missing_kinds": list(missing),
    }


def missing_state_kinds(row) -> tuple[str, ...]:
    mechanism = (row["hook_mechanism"] if "hook_mechanism" in row.keys() else "") or ""
    if not mechanism:
        return ()
    return sandbox_hooks.missing_kinds(
        row["agent"], hooks_supported=mechanism == "hooks"
    )


def pane_for_role(
    server: Server, sender_pane: str, role: str, task: str | None = None
) -> str:
    facts = events.pane_facts_by_id(getattr(server, "socket_name", None))
    sender = facts.get(sender_pane)
    if sender is None or not sender.workspace:
        raise ValueError(f"cannot resolve the {ALIAS['session']} of {sender_pane}")
    task = task or sender.task
    matches = [
        pane
        for pane, f in facts.items()
        if pane != sender_pane
        and (f.workspace, f.task, f.role) == (sender.workspace, task, role)
    ]
    where = f"{sender.workspace}/{task}"
    if not matches:
        raise ValueError(f"no '{role}' {ALIAS['pane']} in {where}")
    if len(matches) > 1:
        raise ValueError(
            f"{len(matches)} '{role}' {ALIAS['pane']}s in {where} "
            f"({', '.join(matches)}); send to one by pane id"
        )
    return matches[0]


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
        repo=self_entry.get("repo"),
    )
    return {"self": self_entry, "team": team, "notes": notes}


def spawn_human_space():
    pass
