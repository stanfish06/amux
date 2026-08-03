from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from amux import core, events, monitor, store, utils, worktree
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
    if args.clean:
        for window in session.windows:
            worktree.remove_task(args.workspace, window.name or "")
    core.load_agent_space(session).terminate()
    print(f"killed {ALIAS['session']} '{args.workspace}'")
    return 0


def _cmd_kg(server, args) -> int:
    session = _get_session(server, args.workspace)
    window = _get_window(session, args.task)
    if args.clean:
        worktree.remove_task(args.workspace, args.task)
    core.load_agent_grid(window).terminate()
    print(f"killed {ALIAS['window']} '{args.task}' in '{args.workspace}'")
    return 0


def _cmd_note(server, args) -> int:
    pane = args.pane or events.self_pane_id()
    if pane is None:
        raise ValueError(
            f"not inside an amux {ALIAS['pane']} pane; pass --pane to attribute the note"
        )
    workspace, task = events.resolve_scope(pane)
    if not workspace:
        raise ValueError(
            f"could not resolve workspace/task for {ALIAS['pane']} {pane}; "
            f"pass --pane or run inside the pane"
        )
    agent = ""
    row = store.worktree_for_pane(pane)
    if row:
        agent = row["agent"]
    else:
        for s in server.sessions:
            for w in s.windows:
                for p in w.panes:
                    if p.id == pane:
                        agent = core.load_agent_pane(p).agent_name
    note_id = store.add_note(
        workspace=workspace,
        task=task,
        pane=pane,
        agent=agent,
        worktree_id=row["id"] if row else None,
        repo=row["repo"] if row else None,
        text=" ".join(args.text),
        scope=args.scope,
        kind=args.kind,
    )
    origin = f" [{row['name']}]" if row and row["name"] else ""
    print(
        f"note #{note_id} @ {workspace}/{task}{origin} "
        f"(scope={args.scope}, kind={args.kind})"
    )
    return 0


def _cmd_notes(server, args) -> int:
    if args.workspace or args.repo:
        # agent-scoped notes are private to their pane on every route, not just
        # the pane one below; without a pane filter here --workspace would hand
        # out every teammate's private notes.
        pane = None
        if args.scope == "agent":
            pane = args.pane or events.self_pane_id()
            if pane is None:
                raise ValueError(
                    f"--scope agent is private to one {ALIAS['pane']}; "
                    f"run it inside a pane or pass --pane"
                )
        notes = store.query_notes(
            workspace=args.workspace,
            task=args.task,
            scope=args.scope,
            kind=args.kind,
            pane=pane,
            repo=args.repo,
            limit=args.n,
        )
    else:
        pane = args.pane or events.self_pane_id()
        if pane is None:
            raise ValueError(
                f"not inside an amux {ALIAS['pane']} pane; pass --workspace or --pane"
            )
        workspace, task = events.resolve_scope(pane)
        if not workspace:
            raise ValueError(f"could not resolve workspace for {ALIAS['pane']} {pane}")
        row = store.worktree_for_pane(pane)
        repo = row["repo"] if row else None
        if args.scope:
            notes = store.query_notes(
                workspace=workspace,
                task=args.task or task,
                scope=args.scope,
                kind=args.kind,
                # see above: narrow to this pane, never widen.
                pane=pane if args.scope == "agent" else None,
                repo=repo,
                limit=args.n,
            )
        else:
            notes = store.visible_notes(
                workspace=workspace,
                task=args.task or task,
                pane=pane,
                kind=args.kind,
                repo=repo,
                limit=args.n,
            )
    if args.json:
        for n in notes:
            print(json.dumps(n, separators=(",", ":"), default=str))
    else:
        for n in notes:
            scope = n["scope"]
            kind = n["kind"]
            print(
                f"{n['id']:>3}  {scope:<9} {kind:<9} "
                f"{n['agent'] or n['pane']:<12}  {n['text']}"
            )
    return 0


def _cmd_integrate(server, args) -> int:
    names = None if args.all else (args.agent or None)
    results = worktree.integrate(args.workspace, args.task, names=names)
    rc = 0
    for r in results:
        if r.ok:
            print(
                f"merged {r.name} ({r.branch}) — {r.commits} commit(s), {r.shortstat or 'no changes'}"
            )
        else:
            print(f"CONFLICT {r.name} ({r.branch}): {r.error}", file=sys.stderr)
            rc = 1
    return rc


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
    p_kw.add_argument(
        "--clean",
        action="store_true",
        help="also remove the workspace's git worktrees (branches are kept)",
    )
    p_kw.set_defaults(func=_cmd_kw)

    p_kg = sub.add_parser("kg", help="kill an agent grid")
    p_kg.add_argument("workspace")
    p_kg.add_argument("task")
    p_kg.add_argument(
        "--clean",
        action="store_true",
        help="also remove the task's git worktrees (branches are kept)",
    )
    p_kg.set_defaults(func=_cmd_kg)

    p_note = sub.add_parser(
        "note", help="publish a scoped note (decision/finding/blocker/note)"
    )
    p_note.add_argument("text", nargs="+", help="note text")
    p_note.add_argument(
        "--scope",
        default="task",
        choices=store.NOTE_SCOPES,
        help="visibility scope (default: task)",
    )
    p_note.add_argument(
        "--kind",
        default="note",
        choices=store.NOTE_KINDS,
        help="note kind (default: note)",
    )
    p_note.add_argument("--pane", default=None, help="pane id (default: $TMUX_PANE)")
    p_note.set_defaults(func=_cmd_note)

    p_notes = sub.add_parser("notes", help="list scoped notes")
    p_notes.add_argument("--workspace", default=None, help="workspace filter")
    p_notes.add_argument("--task", default=None, help="task filter")
    p_notes.add_argument("--repo", default=None, help="project repo path filter")
    p_notes.add_argument("--scope", default=None, choices=store.NOTE_SCOPES)
    p_notes.add_argument("--kind", default=None, choices=store.NOTE_KINDS)
    p_notes.add_argument("-n", type=int, default=20, help="max notes")
    p_notes.add_argument("--pane", default=None, help="pane id (default: $TMUX_PANE)")
    p_notes.add_argument("--json", action="store_true", help="JSONL output")
    p_notes.set_defaults(func=_cmd_notes)

    p_integrate = sub.add_parser(
        "integrate",
        help="merge agent worktree branches into the task integration branch",
    )
    p_integrate.add_argument("workspace")
    p_integrate.add_argument("task")
    p_integrate.add_argument(
        "--agent",
        action="append",
        default=None,
        help="agent name to merge (repeatable; default: every active worktree)",
    )
    p_integrate.add_argument(
        "--all",
        action="store_true",
        help="merge every active worktree of the task (default)",
    )
    p_integrate.set_defaults(func=_cmd_integrate)

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
    p_tail.add_argument("--workspace", default=None, help="only this workspace")
    p_tail.add_argument("--task", default=None, help="only this task")
    p_tail.set_defaults(func=events.cmd_tail)

    p_wait = ev_sub.add_parser("wait", help="block until a pane reaches a state")
    p_wait.add_argument("pane", help="pane id, e.g. %%42")
    p_wait.add_argument("--timeout", type=float, default=300.0)
    p_wait.set_defaults(func=events.cmd_wait)

    args = parser.parse_args(argv)
    server = core.get_server(args.socket_name)
    try:
        return args.func(server, args)
    except (ValueError, worktree.WorktreeError) as exc:
        print(f"amux: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
