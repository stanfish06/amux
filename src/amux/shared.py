from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ALIAS = {"session": "workspace", "window": "task", "pane": "agent"}
DEFAULT_SOCKET = "amux-root"


@dataclass(frozen=True)
class AgentRequest:
    agent: str
    model: str = ""
    effort: str = ""
    role: str = ""


AGENT_TUNING: dict[str, dict[str, tuple[str, str]]] = {
    "claude": {"model": ("--model", "{}"), "effort": ("--effort", "{}")},
    "codex": {"model": ("-m", "{}"), "effort": ("-c", "model_reasoning_effort={}")},
}


def render_tuning(request: AgentRequest) -> tuple[str, ...]:
    table = AGENT_TUNING.get(request.agent, {})
    args: list[str] = []
    for key in ("model", "effort"):
        value = getattr(request, key)
        if not value:
            continue
        flag = table.get(key)
        if not flag:
            raise ValueError(
                f"agent '{request.agent}' has no {key} flag in AGENT_TUNING, "
                f"so '{value}' cannot be passed to it"
            )
        args += [flag[0], flag[1].format(value)]
    return tuple(args)


def render_command(base: str, args: Sequence[str] = ()) -> str:
    if not (base and args):
        return base
    return f"{base} {shlex.join(args)}"


STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "amux"
)

SKILL_POINTER = (
    "You are running inside an amux pane. amux is agent orchestration on top of "
    "tmux, and its own skill -- named `amux`, invocable by that name -- is the "
    "authoritative vocabulary for this pane: coordination, spawning work, "
    "messaging teammates, per-agent worktrees, and agent state. Read it before "
    "you do any of those, rather than guessing at commands that do not exist."
)

POINTER_FLAGS: dict[str, tuple[str, ...]] = {"claude": ("--append-system-prompt",)}


def skill_pointer_args(agent: str) -> tuple[str, ...]:
    flags = POINTER_FLAGS.get(agent)
    return (*flags, SKILL_POINTER) if flags else ()


def report(message: str) -> None:
    try:
        print(message)
    except Exception:  # noqa: BLE001 - there is nowhere left to report to
        pass


def skill_bootstrap_message(agent: str, skill_path: str) -> str:
    if agent in POINTER_FLAGS or not skill_path:
        return ""
    return (
        f"[amux] {SKILL_POINTER} amux has installed it for you at {skill_path}; "
        "read it now, before you answer anything else."
    )


def scrub_pyinstaller_env() -> None:
    for key in [k for k in os.environ if k.startswith("_PYI_")]:
        del os.environ[key]
    path = os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    if path is None and not getattr(sys, "frozen", False):
        path = os.environ.get("LD_LIBRARY_PATH")
    kept = [p for p in (path or "").split(":") if p and not p.startswith("/tmp/_MEI")]
    if kept:
        os.environ["LD_LIBRARY_PATH"] = ":".join(kept)
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)
