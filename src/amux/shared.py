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
    """One `-a` spec after parsing: which agent, and how it should be tuned.

    Lives here rather than in `runtime` because `sandbox` needs it too and
    `runtime` imports `sandbox`.
    """

    agent: str
    model: str = ""
    effort: str = ""


# The two CLIs spell these differently and codex has no dedicated effort flag,
# so there is no shared spelling to collapse this into. Adding a third agent is
# a row here.
AGENT_TUNING: dict[str, dict[str, tuple[str, str]]] = {
    "claude": {"model": ("--model", "{}"), "effort": ("--effort", "{}")},
    "codex": {"model": ("-m", "{}"), "effort": ("-c", "model_reasoning_effort={}")},
}


def render_tuning(request: AgentRequest) -> tuple[str, ...]:
    """Model and effort as argv for `request.agent`. Empty fields add nothing."""
    table = AGENT_TUNING.get(request.agent, {})
    args: list[str] = []
    for field in ("model", "effort"):
        value = getattr(request, field)
        flag = table.get(field)
        if value and flag:
            args += [flag[0], flag[1].format(value)]
    return tuple(args)


def append_args(command: str, args: Sequence[str]) -> str:
    """Append argv to a shell command line, quoted.

    The single place anything gets appended to a host launch command. Values
    reach here unvalidated and the result is typed into a pane by `send-keys`,
    so `;` or `$(...)` in a model name must arrive as text, not as shell syntax.
    """
    if not (command and args):
        return command
    return f"{command} {shlex.join(args)}"

STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "amux"
)


def scrub_pyinstaller_env() -> None:
    """Drop PyInstaller onefile bootstrap vars before anything can spawn tmux.

    The frozen amux runs with _PYI_* set by its bootloader. If those reach a
    long-lived child (the tmux server, started on first spw), every frozen
    amux later spawned from that environment believes it is already unpacked
    and dies looking for a stale /tmp/_MEI* dir. LD_LIBRARY_PATH gets the
    same treatment: restore the pre-bootloader value (LD_LIBRARY_PATH_ORIG)
    and drop /tmp/_MEI* entries, which die with this process.
    """
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
