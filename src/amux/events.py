from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Literal

from amux import store
from amux.shared import DEFAULT_SOCKET, STATE_DIR  # noqa: F401  (re-export)

STATE_OPTION = "@amux_state"

EventKind = Literal["spawn", "busy", "stop", "notify", "exit"]
AgentState = Literal["starting", "busy", "idle", "needs-input", "dead"]

STATE_BY_KIND: dict[EventKind, AgentState] = {
    "spawn": "starting",
    "busy": "busy",
    "stop": "idle",
    "notify": "needs-input",
    "exit": "dead",
}


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
    return socket_path if socket_path and socket_path.split("/")[-1].startswith(DEFAULT_SOCKET) else None


def _tmux(socket_path: str, *args: str) -> None:
    subprocess.run(
        ["tmux", "-S", socket_path, *args],
        check=False,
        capture_output=True,
    )


def self_pane_id() -> str | None:
    """Pane id of the calling process when inside an amux pane, else None."""
    if _amux_socket() is None:
        return None
    return os.environ.get("TMUX_PANE") or None


def _wait_channel(pane: str) -> str:
    return f"amux-state-{pane.lstrip('%')}"


def resolve_scope(pane: str, socket_path: str | None = None) -> tuple[str, str]:
    """Map a pane to its (workspace, task). Live tmux first (works for panes
    with no worktree row), then the worktree registry (works for dead panes),
    else ("", "")."""
    socket_path = socket_path or _amux_socket()
    if socket_path:
        out = subprocess.run(
            [
                "tmux",
                "-S",
                socket_path,
                "display-message",
                "-p",
                "-t",
                pane,
                "#{session_name}|||#{window_name}",
            ],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and "|||" in out.stdout:
            session, _, window = out.stdout.strip().partition("|||")
            if session:
                return session, window
    row = store.worktree_for_pane(pane)
    if row:
        return row["workspace"], row["task"]
    return "", ""


def emit(
    kind: EventKind,
    pane: str | None = None,
    agent: str = "",
    detail: str = "",
) -> Event | None:
    socket_path = _amux_socket()
    if socket_path is None:
        return None
    pane = pane or os.environ.get("TMUX_PANE", "")
    if not pane:
        return None

    workspace, task = resolve_scope(pane, socket_path)
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
    )
    _tmux(socket_path, "set-option", "-p", "-t", pane, STATE_OPTION, event.state)
    _tmux(socket_path, "wait-for", "-S", _wait_channel(pane))
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


def current_state(pane: str, socket_path: str | None = None) -> AgentState | None:
    socket_path = socket_path or _amux_socket()
    if socket_path:
        out = subprocess.run(
            [
                "tmux",
                "-S",
                socket_path,
                "show-options",
                "-pqv",
                "-t",
                pane,
                STATE_OPTION,
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if out:
            return out  # type: ignore[return-value]
    row = store.latest_event(pane)
    return Event.from_row(row).state if row else None


def wait(
    pane: str,
    for_states: tuple[AgentState, ...] = ("idle", "needs-input", "dead"),
    timeout: float = 300.0,
    socket_path: str | None = None,
) -> AgentState | None:
    socket_path = socket_path or _amux_socket()
    if socket_path is None:
        return None
    deadline = time.monotonic() + timeout
    while True:
        state = current_state(pane, socket_path)
        if state in for_states or state == "dead":
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            subprocess.run(
                ["tmux", "-S", socket_path, "wait-for", _wait_channel(pane)],
                timeout=remaining,
                capture_output=True,
            )
        except subprocess.TimeoutExpired:
            pass


def _hook_payload() -> dict:
    """JSON that Claude Code pipes to hook commands on stdin (empty if run
    interactively or the payload is malformed)."""
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
        or payload.get("message")  # Notification: what the agent is asking
        or payload.get("tool_name")  # PreToolUse: which tool went busy
        or payload.get("reason")  # SessionEnd: why it exited
        or ""
    )
    try:
        emit(args.kind, pane=args.pane, agent=args.agent, detail=detail)
    except Exception:
        pass  # a hook must never look like an agent failure
    return 0


def cmd_tail(server, args) -> int:
    for event in tail(n=args.n, pane=args.pane, workspace=args.workspace, task=args.task):
        print(event.to_line())
    return 0


def cmd_wait(server, args) -> int:
    state = wait(args.pane, timeout=args.timeout)
    if state is None:
        print(f"timeout after {args.timeout}s", flush=True)
        return 1
    print(state)
    return 0 if state != "dead" else 2
