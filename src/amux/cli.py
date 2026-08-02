from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from amux import core, events, monitor, utils
from amux.shared import ALIAS, scrub_pyinstaller_env


def _get_session(server, workspace: str):
    session = server.sessions.get(session_name=workspace, default=None)
    if session is None:
        raise ValueError(f"{ALIAS['session']} '{workspace}' not found")
    return session


def _get_window(session, task: str):
    window = session.windows.get(window_name=task, default=None)
    if window is None:
        raise ValueError(
            f"{ALIAS['window']} '{task}' not found in {ALIAS['session']} '{session.name}'"
        )
    return window


def _cmd_lsw(server, args) -> int:
    spaces = core.load_agent_spaces(server)
    if not spaces:
        print(f"no {ALIAS['session']}s")
        return 0
    for space in spaces:
        lines = utils.session_to_string(space.session)
        if lines:
            print("\n".join(lines))
    return 0


def _cmd_lsg(server, args) -> int:
    session = _get_session(server, args.workspace)
    for window in session.windows:
        lines = utils.window_to_string(window)
        if lines:
            print("\n".join(lines))
    return 0


def _resolve_grid(args) -> tuple[int, int, list[str]]:
    agents = core.parse_agent_specs(args.agent or [], args.rows, args.cols)
    nrows, ncols = core.resolve_grid_shape(len(agents), args.rows, args.cols)
    return nrows, ncols, agents


def _composition(agents: list[str]) -> str:
    return " + ".join(f"{n} {agent}" for agent, n in Counter(agents).items())


def _cmd_spw(server, args) -> int:
    nrows, ncols, agents = _resolve_grid(args)
    space = core.spawn_agent_space(
        server,
        session_path=args.path,
        session_name=args.workspace,
        init_grid_nrows=nrows,
        init_grid_ncols=ncols,
        init_grid_agents=agents,
        init_task_name=args.task,
    )
    print(
        f"spawned {ALIAS['session']} '{space.project_name}' "
        f"({nrows}x{ncols}: {_composition(agents)}) @ {space.cwd}"
    )
    socket = args.socket_name or core.DEFAULT_SOCKET
    print(f"attach: tmux -L {socket} attach -t {space.project_name}")
    return 0


def _cmd_spg(server, args) -> int:
    session = _get_session(server, args.workspace)
    nrows, ncols, agents = _resolve_grid(args)
    grid = core.spawn_agent_grid(
        session,
        window_name=args.task,
        nrows=nrows,
        ncols=ncols,
        agents=agents,
        cwd=args.path,
    )
    print(
        f"spawned {ALIAS['window']} '{grid.task_name}' in '{args.workspace}' "
        f"({nrows}x{ncols}: {_composition(agents)})"
    )
    return 0


def _cmd_kw(server, args) -> int:
    session = _get_session(server, args.workspace)
    core.load_agent_space(session).terminate()
    print(f"killed {ALIAS['session']} '{args.workspace}'")
    return 0


def _cmd_kg(server, args) -> int:
    session = _get_session(server, args.workspace)
    window = _get_window(session, args.task)
    core.load_agent_grid(window).terminate()
    print(f"killed {ALIAS['window']} '{args.task}' in '{args.workspace}'")
    return 0


def _cmd_ctx(server, args) -> int:
    pane = args.pane or events.self_pane_id()
    if pane is None:
        raise ValueError(
            f"not inside an amux {ALIAS['pane']} pane; pass --pane to inspect one"
        )
    ctx = core.build_context(server, pane)
    if args.json:
        print(json.dumps(ctx))
    else:
        print("\n".join(utils.context_to_string(ctx)))
    return 0


def _add_grid_args(parser: argparse.ArgumentParser):
    parser.add_argument(
        "-r", "--rows", type=int, default=None, help="grid rows (default: derived)"
    )
    parser.add_argument(
        "-c", "--cols", type=int, default=None, help="grid columns (default: derived)"
    )
    parser.add_argument(
        "-a",
        "--agent",
        action="append",
        default=None,
        metavar="AGENT[:COUNT]",
        help=f"agent spec, repeatable: {'/'.join(core.AGENT_COMMANDS)} or a raw "
        "command, with an optional pane count (e.g. -a claude:3 -a codex)",
    )


def main(argv: list[str] | None = None) -> int:
    scrub_pyinstaller_env()
    parser = argparse.ArgumentParser(prog="amux", description=__doc__)
    parser.add_argument(
        "-L",
        "--socket-name",
        default=None,
        help=f"tmux socket name (default: {core.DEFAULT_SOCKET}, a dedicated amux server)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_lsw = sub.add_parser("lsw", help="list workspaces")
    p_lsw.set_defaults(func=_cmd_lsw)

    p_lsg = sub.add_parser("lsg", help="list agent grids in a workspace")
    p_lsg.add_argument("workspace")
    p_lsg.set_defaults(func=_cmd_lsg)

    p_spw = sub.add_parser("spw", help="spawn a new workspace")
    p_spw.add_argument("workspace")
    p_spw.add_argument("-p", "--path", default=os.getcwd(), help="project directory")
    p_spw.add_argument("-t", "--task", default="task0", help="initial task name")
    _add_grid_args(p_spw)
    p_spw.set_defaults(func=_cmd_spw)

    p_spg = sub.add_parser("spg", help="spawn a new agent grid in a workspace")
    p_spg.add_argument("workspace")
    p_spg.add_argument("task")
    p_spg.add_argument(
        "-p", "--path", default=None, help="task directory (default: workspace dir)"
    )
    _add_grid_args(p_spg)
    p_spg.set_defaults(func=_cmd_spg)

    p_kw = sub.add_parser("kw", help="kill a workspace")
    p_kw.add_argument("workspace")
    p_kw.set_defaults(func=_cmd_kw)

    p_kg = sub.add_parser("kg", help="kill an agent grid")
    p_kg.add_argument("workspace")
    p_kg.add_argument("task")
    p_kg.set_defaults(func=_cmd_kg)

    p_ctx = sub.add_parser(
        "ctx",
        help=f"show an {ALIAS['pane']}'s identity and its {ALIAS['session']} team",
    )
    p_ctx.add_argument("--json", action="store_true", help="machine-readable output")
    p_ctx.add_argument("--pane", default=None, help="pane id (default: $TMUX_PANE)")
    p_ctx.set_defaults(func=_cmd_ctx)

    p_mon = sub.add_parser(
        "monitor",
        help=f"live read-only dashboard of every {ALIAS['session']} and {ALIAS['pane']}",
    )
    p_mon.add_argument(
        "-W",
        "--width",
        type=int,
        default=monitor.DEFAULT_WIDTH,
        help=f"total dashboard width in columns (default: {monitor.DEFAULT_WIDTH})",
    )
    p_mon.add_argument(
        "-T",
        "--tree-width",
        type=int,
        default=monitor.DEFAULT_TREE_WIDTH,
        help=f"{ALIAS['session']} tree width in columns (default: {monitor.DEFAULT_TREE_WIDTH})",
    )
    p_mon.add_argument(
        "-i",
        "--interval",
        type=int,
        default=monitor.DEFAULT_INTERVAL_MS,
        help=f"poll interval in ms (default: {monitor.DEFAULT_INTERVAL_MS})",
    )
    p_mon.set_defaults(func=monitor.cmd_monitor)

    p_ev = sub.add_parser("event", help="agent state events")
    ev_sub = p_ev.add_subparsers(dest="event_command", required=True)

    p_emit = ev_sub.add_parser(
        "emit", help="append an event (called by agent/tmux hooks)"
    )
    p_emit.add_argument("kind", choices=sorted(events.STATE_BY_KIND))
    p_emit.add_argument("--pane", default=None, help="pane id (default: $TMUX_PANE)")
    p_emit.add_argument("--agent", default="", help="agent kind, e.g. claude")
    p_emit.add_argument(
        "--detail",
        default=None,
        help="free-form note (default: extracted from hook JSON on stdin)",
    )
    p_emit.set_defaults(func=events.cmd_emit)

    p_tail = ev_sub.add_parser("tail", help="print recent events as JSONL")
    p_tail.add_argument("-n", type=int, default=20, help="number of events")
    p_tail.add_argument("--pane", default=None, help="only this pane id")
    p_tail.set_defaults(func=events.cmd_tail)

    p_wait = ev_sub.add_parser("wait", help="block until a pane reaches a state")
    p_wait.add_argument("pane", help="pane id, e.g. %%42")
    p_wait.add_argument("--timeout", type=float, default=300.0)
    p_wait.set_defaults(func=events.cmd_wait)

    args = parser.parse_args(argv)
    server = core.get_server(args.socket_name)
    try:
        return args.func(server, args)
    except ValueError as exc:
        print(f"amux: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
