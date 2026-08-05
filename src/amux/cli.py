from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from collections import Counter

from amux import context_service, core, events, monitor, runtime, sandbox, store, utils, worktree
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


HOST = runtime.HOST
DOCKER_SANDBOX = runtime.DOCKER_SANDBOX
RUNTIMES = (HOST, DOCKER_SANDBOX)

_SANDBOX_ONLY = ("cpus", "memory", "share_skills", "context_port")


def _service_probe(port: int):
    """How `sandbox.preflight` asks whether the context service is usable.

    Health alone is not the question: a service listening on a different port
    than the one this grid would hand its sandboxes is no use to them, and
    saying so here is much cheaper than debugging it from inside a microVM.
    """

    def probe() -> tuple[bool, str]:
        result = context_service.status(context_service.ServiceConfig(port=port))
        if result.running and result.port != port:
            return False, (
                f"the running service is on port {result.port}, but this grid would"
                f" tell its sandboxes to dial {port}"
            )
        return result.healthy, result.message

    return probe


def _resolve_runtime(args) -> runtime.Runtime | None:
    """The runtime a spawn should use, or None for the default host one.

    Sandbox-only flags are refused rather than ignored under `--runtime host`:
    a cap that silently does nothing is worse than a rejected command.
    """
    chosen = getattr(args, "runtime", HOST)
    given = [
        name for name in _SANDBOX_ONLY if getattr(args, name, None) not in (None, False)
    ]
    if chosen == HOST:
        if given:
            flags = ", ".join("--" + name.replace("_", "-") for name in sorted(given))
            raise ValueError(
                f"{flags} only applies to --runtime {DOCKER_SANDBOX}"
            )
        return None
    defaults = sandbox.Resources()
    resources = sandbox.Resources(
        cpus=defaults.cpus if args.cpus is None else args.cpus,
        memory=defaults.memory if args.memory is None else args.memory,
        share_skills=bool(args.share_skills),
    )
    # Before tmux, git, or the database is touched.
    resources.validate()
    config = runtime.SandboxConfig(resources=resources, port=args.context_port)
    return runtime.SandboxRuntime(
        config, service_healthy=_service_probe(config.resolved_port)
    )


def _resolve_grid(args) -> tuple[int, int, list[str]]:
    agents = core.parse_agent_specs(args.agent or [], args.rows, args.cols)
    nrows, ncols = core.resolve_grid_shape(len(agents), args.rows, args.cols)
    return nrows, ncols, agents


def _composition(agents: list[str]) -> str:
    return " + ".join(f"{n} {agent}" for agent, n in Counter(agents).items())


def _cmd_spw(server, args) -> int:
    # Argument validation first, before any tmux or git lookup: a rejected flag
    # should read as a rejected flag, not as a missing workspace.
    chosen = _resolve_runtime(args)
    nrows, ncols, agents = _resolve_grid(args)
    space = core.spawn_agent_space(
        server,
        session_path=args.path,
        session_name=args.workspace,
        init_grid_nrows=nrows,
        init_grid_ncols=ncols,
        init_grid_agents=agents,
        init_task_name=args.task,
        runtime=chosen,
    )
    print(
        f"spawned {ALIAS['session']} '{space.project_name}' "
        f"({nrows}x{ncols}: {_composition(agents)}) @ {space.cwd}"
    )
    socket = args.socket_name or core.DEFAULT_SOCKET
    print(f"attach: tmux -L {socket} attach -t {space.project_name}")
    return 0


def _cmd_spg(server, args) -> int:
    chosen = _resolve_runtime(args)  # see `_cmd_spw`
    session = _get_session(server, args.workspace)
    nrows, ncols, agents = _resolve_grid(args)
    grid = core.spawn_agent_grid(
        session,
        window_name=args.task,
        nrows=nrows,
        ncols=ncols,
        agents=agents,
        cwd=args.path,
        runtime=chosen,
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
    ctx = events.pane_context(pane)
    if not ctx.workspace:
        raise ValueError(
            f"could not resolve workspace/task for {ALIAS['pane']} {pane}; "
            f"pass --pane or run inside the pane"
        )
    row = ctx.worktree
    agent = ""
    if row:
        agent = row["agent"]
    else:
        for s in server.sessions:
            for w in s.windows:
                for p in w.panes:
                    if p.id == pane:
                        agent = core.load_agent_pane(p).agent_name
    note_id = store.add_note(
        workspace=ctx.workspace,
        task=ctx.task,
        pane=pane,
        agent=agent,
        worktree_id=row["id"] if row else None,
        repo=row["repo"] if row else "",
        text=" ".join(args.text),
        scope=args.scope,
        kind=args.kind,
    )
    origin = f" [{row['name']}]" if row and row["name"] else ""
    print(
        f"note #{note_id} @ {ctx.workspace}/{ctx.task}{origin} "
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
        ctx = events.pane_context(pane)
        workspace, task = ctx.workspace, ctx.task
        if not workspace:
            raise ValueError(f"could not resolve workspace for {ALIAS['pane']} {pane}")
        repo = ctx.worktree["repo"] if ctx.worktree else None
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


def _cmd_doctor(server, args) -> int:
    """Report whether a runtime's prerequisites hold. Never fixes anything.

    Deliberately read-only: it does not install `sbx`, sign anyone in, or widen
    Docker's network policy. Each failure prints the exact command to run
    instead, so the decision stays the user's.
    """
    if args.runtime == HOST:
        print(f"runtime {HOST}: no external prerequisites (tmux and git only)")
        return 0

    defaults = sandbox.Resources()
    resources = sandbox.Resources(
        cpus=defaults.cpus if args.cpus is None else args.cpus,
        memory=defaults.memory if args.memory is None else args.memory,
        share_skills=bool(args.share_skills),
    )
    port = (
        args.context_port
        if args.context_port is not None
        else context_service.DEFAULT_PORT
    )
    config = runtime.SandboxConfig(resources=resources, port=port)
    git_failure = ""
    try:
        repo = worktree.repo_root(args.path) or ""
    except OSError as exc:
        # git itself missing. A doctor reports; it does not abort on the first
        # thing it cannot do, or the user learns one prerequisite per run.
        repo, git_failure = "", f"cannot run git: {exc.strerror or exc}"
    report = sandbox.preflight(
        agents=core.parse_agent_specs(args.agent or [], None, None),
        repo=repo,
        resources=resources,
        endpoint=config.policy_target,
        service_healthy=_service_probe(config.resolved_port),
    )
    print(f"runtime {DOCKER_SANDBOX} (optional backend) for {args.path}:")
    if git_failure:
        print(f"  [FAIL] git: {git_failure}")
        print("         fix: install git and put it on PATH")
    print(report.report())
    if report.ok and not git_failure:
        print("\nall checks pass")
        return 0
    print(
        f"\n{len(report.failures) + bool(git_failure)} check(s) failed."
        f" amux changes nothing on its own: run the fixes above yourself."
    )
    return 1


def _cmd_context_service(server, args) -> int:
    overrides = {}
    if args.port is not None:
        overrides["port"] = args.port
    if args.db is not None:
        overrides["db_path"] = Path(args.db).expanduser()
    config = context_service.ServiceConfig.from_env(**overrides)
    code, message = context_service.run_action(
        args.action, config, force=getattr(args, "force", False)
    )
    if message:
        print(message, file=sys.stderr if code else sys.stdout)
    return code


def _add_sandbox_args(parser: argparse.ArgumentParser, runtime_default: str = HOST):
    """The `docker-sandbox` runtime's options.

    Every one of them is inert under the default runtime and says so, because
    the backend is opt-in: amux runs agents on the host unless asked otherwise.
    """
    defaults = sandbox.Resources()
    parser.add_argument(
        "--runtime",
        default=runtime_default,
        choices=RUNTIMES,
        help=f"execution backend (default: {runtime_default}; {DOCKER_SANDBOX} is "
        "optional and needs Docker Sandboxes installed and signed in)",
    )
    parser.add_argument(
        "--cpus",
        type=int,
        default=None,
        help=f"CPU cap per sandbox (default: {defaults.cpus}; {DOCKER_SANDBOX} only)",
    )
    parser.add_argument(
        "--memory",
        default=None,
        help=f"memory cap per sandbox, e.g. 4g (default: {defaults.memory}; "
        f"{DOCKER_SANDBOX} only)",
    )
    parser.add_argument(
        "--share-skills",
        action="store_true",
        help="let sandboxes share Docker's skills store, which is read-write "
        f"and shared between them (default: off; {DOCKER_SANDBOX} only)",
    )
    parser.add_argument(
        "--context-port",
        type=int,
        default=None,
        help="loopback port of the host context service (default: "
        f"{context_service.DEFAULT_PORT}; {DOCKER_SANDBOX} only)",
    )


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
    _add_sandbox_args(p_spw)
    p_spw.set_defaults(func=_cmd_spw)

    p_spg = sub.add_parser("spg", help="spawn a new agent grid in a workspace")
    p_spg.add_argument("workspace")
    p_spg.add_argument("task")
    p_spg.add_argument(
        "-p", "--path", default=None, help="task directory (default: workspace dir)"
    )
    _add_grid_args(p_spg)
    _add_sandbox_args(p_spg)
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

    p_state = ev_sub.add_parser(
        "state", help=f"resolved state of every {ALIAS['pane']} on the server"
    )
    p_state.add_argument("--json", action="store_true", help="machine-readable output")
    p_state.set_defaults(func=events.cmd_state)

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

    p_doctor = sub.add_parser(
        "doctor",
        help="check a runtime's prerequisites (read-only; fixes nothing)",
    )
    p_doctor.add_argument(
        "-p", "--path", default=os.getcwd(), help="project directory to check"
    )
    p_doctor.add_argument(
        "-a",
        "--agent",
        action="append",
        default=None,
        metavar="AGENT[:COUNT]",
        help="agent spec to check, repeatable (default: claude)",
    )
    # Unlike spw/spg, doctor exists *to* inspect the optional backend, so
    # checking it is the useful default.
    _add_sandbox_args(p_doctor, runtime_default=DOCKER_SANDBOX)
    p_doctor.set_defaults(func=_cmd_doctor)

    p_svc = sub.add_parser(
        "context-service",
        help="the host-only context service sandboxed agents read through",
    )
    p_svc.add_argument(
        "action",
        choices=context_service.ACTIONS,
        help="serve in the foreground, or start/status/stop the background one",
    )
    p_svc.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"loopback port (default: {context_service.DEFAULT_PORT}, or "
        f"${context_service.ENV_PORT})",
    )
    p_svc.add_argument(
        "--db", default=None, help="context store path (default: the amux state one)"
    )
    p_svc.add_argument(
        "--force",
        action="store_true",
        help="stop: send SIGKILL instead of SIGTERM",
    )
    p_svc.set_defaults(func=_cmd_context_service)

    args = parser.parse_args(argv)
    server = core.get_server(args.socket_name)
    try:
        return args.func(server, args)
    except (
        ValueError,
        worktree.WorktreeError,
        sandbox.SandboxError,
        context_service.ServiceLifecycleError,
    ) as exc:
        print(f"amux: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
