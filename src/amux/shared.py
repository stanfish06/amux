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
    """Model and effort as argv for `request.agent`. Empty fields add nothing.

    Raises when an agent is asked for tuning it has no flag spelling for. The
    parser gates on `runtime.AGENT_COMMANDS` and this gates on `AGENT_TUNING`,
    two tables in two modules; if a third agent is ever added to only one of
    them, the request would otherwise be dropped on the floor and the agent
    would launch on its default model with no error and no warning.
    """
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
    """`base` with `args` appended, each quoted as exactly one shell argument.

    THE render-and-quote seam. Both places amux composes an agent's launch --
    `runtime.HostRuntime.prepare`'s send-keys text and `sandbox.attach_command`
    -- flatten through here, so anything that appends arguments composes with
    whatever else already did rather than replacing it. Add arguments by
    extending the list handed in, never by concatenating onto the string.

    Values reach here unvalidated and the result is typed into a pane by
    `send-keys`, so `;` or `$(...)` in a model name must arrive as text, not as
    shell syntax. An empty base absorbs its arguments rather than producing a
    bare flag list: a pane with no command to run must be sent nothing at all.
    """
    if not (base and args):
        return base
    return f"{base} {shlex.join(args)}"


STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "amux"
)

#: What amux tells an agent about the pane it woke up in. Installing the skill
#: only makes it present -- both agents load skills lazily by description
#: matching, so a document nobody opens is the same as no document. One constant,
#: because a `claude` reads it from its system prompt and a `codex` reads it in a
#: bootstrap message, and those two must not be allowed to say different things.
SKILL_POINTER = (
    "You are running inside an amux pane. amux is agent orchestration on top of "
    "tmux, and its own skill -- named `amux`, invocable by that name -- is the "
    "authoritative vocabulary for this pane: coordination, spawning work, "
    "messaging teammates, per-agent worktrees, and agent state. Read it before "
    "you do any of those, rather than guessing at commands that do not exist."
)

#: `claude` takes the pointer as a system-prompt append. `codex` has no
#: system-prompt append of any kind (verified against codex-cli 0.146.0), so it
#: is activated by a bootstrap message instead and takes no launch argument.
POINTER_FLAGS: dict[str, tuple[str, ...]] = {"claude": ("--append-system-prompt",)}


def skill_pointer_args(agent: str) -> tuple[str, ...]:
    """The launch arguments that carry the pointer for `agent`, if it takes any.

    Empty for `codex`, and for every raw command spec -- amux does not know an
    arbitrary command's flags and must not speak for them.
    """
    flags = POINTER_FLAGS.get(agent)
    return (*flags, SKILL_POINTER) if flags else ()


def report(message: str) -> None:
    """Tell the operator something, and never let the telling cost them a grid.

    `amux spw ws | head -1` closes stdout mid-spawn, so the next `print` raises
    `BrokenPipeError` -- and both places amux degrades rather than fails (the
    skill install inside `HostRuntime.prepare`, the bootstrap send inside
    `core._build_grid`) sit under a caller that answers ANY exception by tearing
    the grid down. A warning must not be able to do that, so the reporting is
    degraded too, not only the thing it reports on.
    """
    try:
        print(message)
    except Exception:  # noqa: BLE001 - there is nowhere left to report to
        pass


def skill_bootstrap_message(agent: str, skill_path: str) -> str:
    """The pointer as a message, for an agent that cannot take it as a flag.

    Empty for any agent with a `POINTER_FLAGS` entry, so the two mechanisms can
    never both fire: `claude` already carries the pointer in its system prompt,
    and a message would cost it a turn to be told what it already knows.

    Empty too when there is no `skill_path`, which is what a failed install
    leaves behind. Sending an agent to read a document that is not there would
    spend its first turn on a dead end, and the install failure has already been
    reported on its own.

    The `[amux]` prefix is not decoration. This text arrives as keystrokes in the
    agent's own prompt, with no envelope and no sender field -- without it the
    agent cannot tell amux apart from its human operator or a teammate.
    """
    if agent in POINTER_FLAGS or not skill_path:
        return ""
    return (
        f"[amux] {SKILL_POINTER} amux has installed it for you at {skill_path}; "
        "read it now, before you answer anything else."
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
