from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Literal

from amux.shared import DEFAULT_SOCKET

STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "amux"
)
EVENTS_FILE = STATE_DIR / "events.jsonl"

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

    @property
    def state(self) -> AgentState:
        return STATE_BY_KIND[self.kind]

    def to_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_line(cls, line: str) -> Event:
        return cls(**json.loads(line))


def _amux_socket() -> str | None:
    tmux = os.environ.get("TMUX", "")
    if not tmux:
        return None
    socket_path = tmux.split(",")[0]
    return socket_path if Path(socket_path).name.startswith(DEFAULT_SOCKET) else None


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

    event = Event(ts=time.time(), kind=kind, pane=pane, agent=agent, detail=detail)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("a") as f:
        f.write(event.to_line() + "\n")

    _tmux(socket_path, "set-option", "-p", "-t", pane, STATE_OPTION, event.state)
    _tmux(socket_path, "wait-for", "-S", _wait_channel(pane))
    return event


def iter_events() -> Iterator[Event]:
    if not EVENTS_FILE.exists():
        return
    with EVENTS_FILE.open() as f:
        for line in f:
            if line.strip():
                yield Event.from_line(line)


def tail(n: int = 20, pane: str | None = None) -> list[Event]:
    events = [e for e in iter_events() if pane is None or e.pane == pane]
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
    for event in reversed(list(iter_events())):
        if event.pane == pane:
            return event.state
    return None


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
    for event in tail(n=args.n, pane=args.pane):
        print(event.to_line())
    return 0


def cmd_wait(server, args) -> int:
    state = wait(args.pane, timeout=args.timeout)
    if state is None:
        print(f"timeout after {args.timeout}s", flush=True)
        return 1
    print(state)
    return 0 if state != "dead" else 2
