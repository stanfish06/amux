import os
import sys
from pathlib import Path

ALIAS = {"session": "workspace", "window": "task", "pane": "agent"}
DEFAULT_SOCKET = "amux-root"

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
