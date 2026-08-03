from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from amux.shared import DEFAULT_SOCKET

TUI_ENTRY = Path("tui") / "dist" / "index.js"

DEFAULT_WIDTH = 120
DEFAULT_TREE_WIDTH = 44
DEFAULT_INTERVAL_MS = 1500


def _find_tui() -> Path:
    override = os.environ.get("AMUX_TUI")
    if override:
        entry = Path(override).expanduser()
        if not entry.is_file():
            raise ValueError(f"$AMUX_TUI does not point at a file: {entry}")
        return entry

    for start in (Path(sys.executable), Path(__file__)):
        for parent in start.resolve().parents:
            entry = parent / TUI_ENTRY
            if entry.is_file():
                return entry

    raise ValueError(
        f"monitor UI not built: no {TUI_ENTRY} found near {Path(__file__).resolve()}. "
        "Run `npm install && npm run build` in tui/, or point $AMUX_TUI at its index.js"
    )


def cmd_monitor(server, args) -> int:
    if args.tree_width >= args.width:
        raise ValueError(
            f"--tree-width ({args.tree_width}) must be less than --width ({args.width})"
        )

    entry = _find_tui()
    node = shutil.which("node")
    if node is None:
        raise ValueError("node not found on PATH; the monitor UI needs Node.js")

    # The TUI reads events by shelling back into amux (the store is sqlite, and
    # only this side runs its migration). Hand it our own path so the frozen
    # binary works even when amux is not on the child's PATH.
    amux_bin = sys.executable if getattr(sys, "frozen", False) else shutil.which("amux")
    if amux_bin:
        os.environ["AMUX_BIN"] = amux_bin

    os.execv(
        node,
        [
            node,
            str(entry),
            "-L", args.socket_name or DEFAULT_SOCKET,
            "-i", str(args.interval),
            "-W", str(args.width),
            "-T", str(args.tree_width),
        ],
    )
