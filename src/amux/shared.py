import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

ALIAS = {"session": "workspace", "window": "task", "pane": "agent"}
DEFAULT_SOCKET = "amux-root"

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


def render_command(base: str, args: Sequence[str] = ()) -> str:
    """`base` with `args` appended, each quoted as exactly one shell argument.

    THE render-and-quote seam. Both places amux composes an agent's launch --
    `runtime.HostRuntime.prepare`'s send-keys text and `sandbox.attach_command`
    -- flatten through here, so anything that appends arguments composes with
    whatever else already did rather than replacing it. Add arguments by
    extending the list handed in, never by concatenating onto the string.
    """
    return " ".join([base, *(shlex.quote(arg) for arg in args)]) if args else base


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
