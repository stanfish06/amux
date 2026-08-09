from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Literal, cast

from amux import store
from amux.shared import DEFAULT_SOCKET, STATE_DIR  # noqa: F401  (re-export)

STATE_OPTION = "@amux_state"

EventKind = Literal["spawn", "busy", "stop", "notify", "exit"]
AgentState = Literal["starting", "busy", "idle", "needs-input", "stopped", "dead"]
PaneKind = Literal["amux", "other"]

STATE_BY_KIND: dict[EventKind, AgentState] = {
    "spawn": "starting",
    "busy": "busy",
    "stop": "idle",
    "notify": "needs-input",
    "exit": "dead",
}

STARTUP_GRACE_S = 10.0


@dataclass
class Event:
    ts: float
    kind: EventKind
    pane: str
    agent: str = ""
    detail: str = ""
    workspace: str = ""
    task: str = ""

    @property
    def state(self) -> AgentState:
        return STATE_BY_KIND[self.kind]

    def to_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_line(cls, line: str) -> Event:
        return cls(**json.loads(line))

    @classmethod
    def from_row(cls, row: dict) -> Event:
        return cls(
            ts=row["ts"],
            kind=row["kind"],
            pane=row["pane"],
            agent=row["agent"],
            detail=row["detail"],
            workspace=row.get("workspace", ""),
            task=row.get("task", ""),
        )


def _amux_socket() -> str | None:
    tmux = os.environ.get("TMUX", "")
    if not tmux:
        return None
    socket_path = tmux.split(",")[0]
    return (
        socket_path
        if socket_path and socket_path.split("/")[-1].startswith(DEFAULT_SOCKET)
        else None
    )


def _socket_args(socket: str) -> list[str]:
    return ["-S", socket] if socket.startswith("/") else ["-L", socket]


def _tmux(socket: str, *args: str) -> None:
    subprocess.run(
        ["tmux", *_socket_args(socket), *args],
        check=False,
        capture_output=True,
    )


def _tmux_out(socket: str, *args: str) -> str | None:
    out = subprocess.run(
        ["tmux", *_socket_args(socket), *args],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip("\r\n") if out.returncode == 0 else None


def self_pane_id() -> str | None:
    if _amux_socket() is None:
        return None
    return os.environ.get("TMUX_PANE") or None


def _wait_channel(pane: str) -> str:
    return f"amux-state-{pane.lstrip('%')}"


def publish_state(pane: str, state: AgentState, socket: str) -> None:
    _tmux(socket, "set-option", "-p", "-t", pane, STATE_OPTION, state)
    _tmux(socket, "wait-for", "-S", _wait_channel(pane))


def _scope_from_registry(pane: str) -> tuple[str, str]:
    row = store.worktree_for_pane(pane)
    return (row["workspace"], row["task"]) if row else ("", "")


def resolve_scope(
    pane: str, socket: str | None = None, facts: PaneFacts | None = None
) -> tuple[str, str]:
    facts = facts or pane_facts(pane, socket)
    if facts.alive and facts.workspace:
        return facts.workspace, facts.task
    return _scope_from_registry(pane)


def emit(
    kind: EventKind,
    pane: str | None = None,
    agent: str = "",
    detail: str = "",
    socket: str | None = None,
) -> Event | None:
    socket = socket or _amux_socket()
    if socket is None:
        return None
    pane = pane or os.environ.get("TMUX_PANE", "")
    if not pane:
        return None

    facts = pane_facts(pane, socket)
    workspace, task = resolve_scope(pane, facts=facts)
    event = Event(
        ts=time.time(),
        kind=kind,
        pane=pane,
        agent=agent,
        detail=detail,
        workspace=workspace,
        task=task,
    )
    store.add_event(
        ts=event.ts,
        pane=pane,
        kind=kind,
        workspace=workspace,
        task=task,
        agent=agent,
        detail=detail,
        worktree_since=facts.boundary,
    )
    publish_state(pane, event.state, socket)
    return event


def iter_events(
    pane: str | None = None,
    workspace: str | None = None,
    task: str | None = None,
) -> list[Event]:
    return [
        Event.from_row(r)
        for r in store.iter_events(pane=pane, workspace=workspace, task=task)
    ]


def tail(
    n: int = 20,
    pane: str | None = None,
    workspace: str | None = None,
    task: str | None = None,
) -> list[Event]:
    events = iter_events(pane=pane, workspace=workspace, task=task)
    return events[-n:]


def in_incarnation(event: Event | None, boundary: float | None) -> Event | None:
    if event and boundary is not None and event.ts < boundary:
        return None
    return event


def resolve_state(
    *,
    alive: bool | None,
    option: str = "",
    latest: Event | None = None,
    now: float | None = None,
) -> AgentState | None:
    if alive is False:
        return "dead"
    state = cast("AgentState | None", option or (latest.state if latest else None))
    if state is None:
        return "idle" if alive else None
    if state == "starting":
        age = (now or time.time()) - latest.ts if latest else STARTUP_GRACE_S + 1
        if age > STARTUP_GRACE_S:
            return "idle"
    return state


def _as_ts(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


_SENTINEL = "amux"
_PANE_FIELDS = (
    "#{pane_id}",
    "#{@amux_pane}",
    "#{session_created}",
    f"#{{{STATE_OPTION}}}",
    "#{@amux_name}",
    "#{@amux_label}",
    "#{pane_current_command}",
    "#{@amux_agent}",
    "#{pane_current_path}",
    "#{session_name}",
    "#{window_name}",
    "#{@amux_model}",
    "#{@amux_effort}",
    _SENTINEL,
)
_FREE_TEXT = slice(7, 13)
_DELIM = "\x1f"
_PANE_FORMAT = _DELIM.join(_PANE_FIELDS)


@dataclass
class PaneFacts:
    alive: bool | None
    kind: PaneKind | None = None
    created: float | None = None
    state_option: str = ""
    name: str = ""
    label: str = ""
    command: str = ""
    agent: str = ""
    cwd: str = ""
    workspace: str = ""
    task: str = ""
    model: str = ""
    effort: str = ""

    def __post_init__(self) -> None:
        if self.alive and self.created is None:
            raise ValueError(
                "a live pane must carry a creation time: without one its events "
                "cannot be bounded and a recycled id inherits the last agent's"
            )

    @property
    def boundary(self) -> float | None:
        return self.created if self.alive else None


def _parse_pane(line: str) -> PaneFacts:
    fields = line.split(_DELIM)
    if not fields[0].startswith("%"):
        return PaneFacts(alive=False)
    if len(fields) < len(_PANE_FIELDS) or fields[-1] != _SENTINEL:
        return PaneFacts(alive=True, created=time.time())
    created = _as_ts(fields[2])
    facts = PaneFacts(
        alive=True,
        kind="amux" if fields[1] else "other",
        created=created if created is not None else time.time(),
        state_option=fields[3],
        name=fields[4],
        label=fields[5],
        command=fields[6],
    )
    if len(fields) == len(_PANE_FIELDS):
        (
            facts.agent,
            facts.cwd,
            facts.workspace,
            facts.task,
            facts.model,
            facts.effort,
        ) = fields[_FREE_TEXT]
    return facts


def pane_facts(pane: str, socket: str | None = None) -> PaneFacts:
    if not pane:
        return PaneFacts(alive=None)
    socket = socket or _amux_socket()
    out = (
        _tmux_out(socket, "display-message", "-p", "-t", pane, _PANE_FORMAT)
        if socket
        else None
    )
    if out is None:
        return PaneFacts(alive=None)
    facts = _parse_pane(out)
    return (
        facts
        if facts.alive and out.split(_DELIM)[0] == pane
        else PaneFacts(alive=False)
    )


def pane_status(
    pane: str, socket: str | None = None, facts: PaneFacts | None = None
) -> tuple[AgentState | None, Event | None]:
    facts = facts or pane_facts(pane, socket)
    row = store.latest_event(pane)
    latest = in_incarnation(Event.from_row(row) if row else None, facts.boundary)
    return (
        resolve_state(alive=facts.alive, option=facts.state_option, latest=latest),
        latest,
    )


def current_state(pane: str, socket: str | None = None) -> AgentState | None:
    return pane_status(pane, socket)[0]


def event_cursor(pane: str, boundary: float, db_path=None) -> int:
    return max(
        (
            int(row["id"])
            for row in store.iter_events(pane=pane, db_path=db_path)
            if row["ts"] >= boundary
        ),
        default=0,
    )


@dataclass
class PaneContext:
    workspace: str
    task: str
    worktree: dict | None


def pane_context(pane: str, socket: str | None = None) -> PaneContext:
    facts = pane_facts(pane, socket)
    workspace, task = resolve_scope(pane, facts=facts)
    return PaneContext(
        workspace=workspace,
        task=task,
        worktree=store.worktree_for_pane(pane, since=facts.boundary),
    )


def pane_states(socket: str | None = None) -> list[dict]:
    socket = socket or _amux_socket() or DEFAULT_SOCKET
    listing = _tmux_out(socket, "list-panes", "-a", "-F", _PANE_FORMAT)
    if not listing:
        return []

    facts_by_pane: dict[str, PaneFacts] = {}
    for line in listing.splitlines():
        facts = _parse_pane(line)
        if facts.alive:
            facts_by_pane[line.split(_DELIM)[0]] = facts

    floor = min(
        (f.boundary for f in facts_by_pane.values() if f.boundary is not None),
        default=None,
    )

    newest_by_pane: dict[str, Event] = {}
    for row in store.events_for_panes(list(facts_by_pane), since=floor):
        newest_by_pane[row["pane"]] = Event.from_row(row)

    rows = store.worktrees_for_panes(list(facts_by_pane), since=floor)

    out = []
    for pane, facts in facts_by_pane.items():
        latest = in_incarnation(newest_by_pane.get(pane), facts.boundary)
        out.append(
            {
                "pane": pane,
                "kind": facts.kind,
                "workspace": facts.workspace,
                "task": facts.task,
                "agent": facts.agent,
                "name": facts.name,
                "label": facts.label,
                "state": (
                    runtime_aware_state(
                        resolve_state(
                            alive=True, option=facts.state_option, latest=latest
                        ),
                        rows.get(pane),
                    )
                    if facts.kind == "amux"
                    else None
                ),
                "last_event": (
                    {"kind": latest.kind, "ts": latest.ts, "detail": latest.detail}
                    if latest
                    else None
                ),
                **runtime_identity(rows.get(pane)),
            }
        )
    return out


def runtime_identity(row) -> dict:
    if row is None:
        return {}
    from amux import core

    return core.runtime_fields(row)


def runtime_aware_state(state: AgentState | None, row) -> AgentState | None:
    if row is None or state is None:
        return state
    keys = row.keys() if hasattr(row, "keys") else row
    if "runtime" not in keys or "runtime_status" not in keys:
        return state
    if row["runtime"] != "host" and row["runtime_status"] == "stopped":
        return "stopped"
    return state


def wait(
    pane: str,
    for_states: tuple[AgentState, ...] = ("idle", "needs-input", "dead"),
    timeout: float = 300.0,
    socket: str | None = None,
) -> AgentState | None:
    socket = socket or _amux_socket()
    if socket is None:
        return None
    deadline = time.monotonic() + timeout
    while True:
        state = current_state(pane, socket)
        if state in for_states or state == "dead":
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        _wait_for_state_signal(socket, pane, remaining)


def _wait_for_state_signal(socket: str, pane: str, timeout: float) -> None:
    try:
        subprocess.run(
            ["tmux", *_socket_args(socket), "wait-for", _wait_channel(pane)],
            timeout=timeout,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        pass


def wait_for_fresh_state(
    pane: str,
    after: int,
    for_states: tuple[AgentState, ...] = ("busy",),
    timeout: float = 300.0,
    socket: str | None = None,
    expected_created: float | None = None,
    db_path=None,
    clock=time.monotonic,
    block=None,
) -> AgentState | None:
    socket = socket or _amux_socket()
    if socket is None:
        return None
    deadline = clock() + timeout
    wait_once = block or _wait_for_state_signal
    cursor = after
    while True:
        facts = pane_facts(pane, socket)
        if facts.alive is not True or (
            expected_created is not None and facts.created != expected_created
        ):
            return "dead"
        for row in store.iter_events(pane=pane, db_path=db_path):
            row_id = int(row["id"])
            if row_id <= cursor:
                continue
            cursor = row_id
            if expected_created is not None and row["ts"] < expected_created:
                continue
            state = STATE_BY_KIND[row["kind"]]
            if state in for_states or state == "dead":
                return state
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        wait_once(socket, pane, remaining)


def _hook_payload() -> dict:
    if sys.stdin.isatty():
        return {}
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return {}


def cmd_emit(server, args) -> int:
    payload = _hook_payload() if args.detail is None else {}
    detail = (
        args.detail
        or payload.get("message")
        or payload.get("tool_name")
        or payload.get("reason")
        or ""
    )
    try:
        emit(args.kind, pane=args.pane, agent=args.agent, detail=detail)
    except Exception:
        pass
    return 0


def cmd_state(server, args) -> int:
    states = pane_states(getattr(server, "socket_name", None) or DEFAULT_SOCKET)
    if args.json:
        print(json.dumps(states))
        return 0
    for entry in states:
        print(
            f"{entry['pane']}\t{entry['state'] or '-'}\t"
            f"{entry['workspace']}/{entry['task']}\t{entry['name']}"
        )
    return 0


def cmd_tail(server, args) -> int:
    for event in tail(
        n=args.n, pane=args.pane, workspace=args.workspace, task=args.task
    ):
        print(event.to_line())
    return 0


def cmd_wait(server, args) -> int:
    state = wait(args.pane, timeout=args.timeout)
    if state is None:
        print(f"timeout after {args.timeout}s", flush=True)
        return 1
    print(state)
    return 0 if state != "dead" else 2
