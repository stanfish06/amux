from __future__ import annotations

import argparse
import sys

from amux import core


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="amux", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_lsw = sub.add_parser("lsw", help="list workspaces")
    p_lsg = sub.add_parser("lsg", help="list agent grids in a workspace")
    p_spawn_workspace = sub.add_parser("spw", help="spawn a new workspace")
    p_spawn_grid = sub.add_parser("spg", help="spawn a new agent grid in a workspace")
    p_kill = sub.add_parser("kw", help="kill a workspace")
    p_kill = sub.add_parser("kg", help="kill an agent grid")


if __name__ == "__main__":
    sys.exit(main())
