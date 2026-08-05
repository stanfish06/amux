import time

from libtmux import Pane, Session, Window

from amux import sandbox_client
from amux.core import load_agent_pane
from amux.shared import ALIAS


def _pane_state(pane: Pane) -> str:
    return load_agent_pane(pane).state


def _age(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def _addr(agent: dict) -> str:
    return f"@{agent['label']} {agent['pane']}" if agent["name"] else agent["pane"]


#: Appended to a state that its agent cannot fully report. ASCII, not an emoji:
#: the monitor lays panels out in fixed columns and an emoji's width is not
#: reliably one cell, so it misaligns the box borders.
DEGRADED_MARK = "*"


def state_to_string(agent: dict) -> str:
    """An agent's state, marked when the agent cannot report all of them.

    No seventh state: the vocabulary stays starting/busy/idle/needs-input/
    stopped/dead and the marker rides alongside, so nothing that branches on
    state has to learn a new value. But a degraded agent's state is never
    rendered bare -- showing `idle` for an agent that physically cannot send
    `busy` is a claim amux is not entitled to make.
    """
    state = agent.get("state") or "-"
    return f"{state}{DEGRADED_MARK}" if agent.get("state_degraded") else state


def context_to_string(ctx: dict) -> list[str]:
    """Concise agent-facing view of `core.build_context` output."""
    me = ctx["self"]
    branch = f"  branch:{me.get('branch')}" if me.get("branch") else ""
    lines = [
        f"you: {me['name']}  {me['agent']} @{me['label']} {me['pane']}  "
        f"{ALIAS['window']}:{me['task']}  {ALIAS['session']}:{me['workspace']}  "
        f"{state_to_string(me)}{branch}  {me['cwd']}",
    ]
    # Immediately after the identity line, and only for a non-host runtime, so
    # host output stays byte-identical. Shape shared with the sandbox client.
    runtime_line = sandbox_client.runtime_to_string(me)
    if runtime_line:
        lines.append(runtime_line)
    if me.get("state_degraded"):
        lines.append(
            f"  note: {DEGRADED_MARK} marks a state this agent cannot fully "
            f"report (no {', '.join(me['missing_kinds'])})"
        )
    lines.append(f"team @ {me['workspace']}")
    rows = [a for group in ctx["team"] for a in group["agents"]]
    wn = max(len(a["name"] or "-") for a in rows)
    wa = max(len(a["agent"]) for a in rows)
    wd = max(len(_addr(a)) for a in rows)
    ws = max(len(state_to_string(a)) for a in rows)
    for i, group in enumerate(ctx["team"]):
        own = f" (your {ALIAS['window']})" if i == 0 else ""
        lines.append(f"  {group['task']}{own}")
        for a in group["agents"]:
            row = (
                f"    {(a['name'] or '-'):<{wn}}  {a['agent']:<{wa}}  "
                f"{_addr(a):<{wd}}  {state_to_string(a):<{ws}}"
            )
            if a["pane"] == me["pane"]:
                row += " (you)"
            else:
                last = a["last_event"]
                if last:
                    row += f"  {_age(last['ts'])}"
                    if last["detail"]:
                        row += f"  \"{last['detail']}\""
                if a.get("branch"):
                    row += f"  {a['branch']}"
                if a.get("last_commit"):
                    row += f"  \"{a['last_commit']}\""
                if a["cwd"] and a["cwd"] != me["cwd"]:
                    row += f"  {a['cwd']}"
            lines.append(row.rstrip())
    notes = ctx.get("notes") or []
    if notes:
        lines.append(f"notes @ {me['workspace']}/{me['task']} (visible):")
        for n in notes:
            lines.append(
                f"  [{n['kind']}:{n['scope']}] {_age(n['ts'])} "
                f"({n['agent'] or n['pane']})  {n['text']}"
            )
    return lines


def window_to_string(window: Window):
    panes_info = {}
    for p in window.panes:
        if p.id:
            panes_info[p.pane_id] = {
                "id": p.id,
                "index": p.index if p.index else -1,
                "title": p.title if p.title else "",
                "state": _pane_state(p),
            }
    if len(panes_info) > 0:
        panes_info = dict(
            sorted(panes_info.items(), key=lambda pane_info: pane_info[1]["index"])
        )
    else:
        return
    window_info = [
        f"{ALIAS['session']} id: {window.session.id}",
        f"{ALIAS['session']} name: {window.session.name}",
        f"{ALIAS['window']} id: {window.id}",
        f"{ALIAS['window']} index: {window.index}",
        f"{ALIAS['window']} name: {window.name}",
        f"#{ALIAS['pane']}s: {len(panes_info)}",
    ]
    max_window_info_width = max([len(v) for v in window_info])
    key_width = [len(k) for k in list(panes_info.values())[0]]
    value_width = [
        [len(str(v)) for v in pane_info.values()] for pane_info in panes_info.values()
    ]
    max_col_width = [
        max(max([v[i] for v in value_width]) + 2, key_width[i])
        for i in range(len(value_width[0]))
    ]
    max_col_width[-1] += max(
        max_window_info_width - sum(max_col_width) - len(max_col_width) + 1, 0
    )
    top = "┌" + "─".join(["─" * w for w in max_col_width]) + "┐"
    cap = [
        "│" + str(v).ljust(sum(max_col_width) + len(max_col_width) - 1) + "│"
        for v in window_info
    ]
    sep1 = "├" + "┬".join(["─" * w for w in max_col_width]) + "┤"
    headers = (
        "│"
        + "│".join(
            [
                str(k).ljust(max_col_width[i])
                for i, k in enumerate(list(panes_info.values())[0].keys())
            ]
        )
        + "│"
    )
    sep2 = "├" + "┼".join(["─" * w for w in max_col_width]) + "┤"
    rows = [
        "│"
        + "│".join(
            [str(v).ljust(max_col_width[i]) for i, v in enumerate(pane_info.values())]
        )
        + "│"
        for pane_info in panes_info.values()
    ]
    bottom = "└" + "┴".join(["─" * w for w in max_col_width]) + "┘"
    return [top, *cap, sep1, headers, sep2, *rows, bottom]


def session_to_string(session: Session):
    n_tables = 0
    window_strings = []
    for w in session.windows:
        window_string = window_to_string(w)
        if window_string and len(window_string) > 0:
            window_strings.extend(window_string)
            n_tables += 1
    if n_tables > 0:
        max_col_width = max([len(s) for s in window_strings])
        server_name = (
            session.server.socket_name if session.server.socket_name else "default"
        )
        session_info = [
            f"server name: {server_name}",
            f"{ALIAS['session']} name: {session.name}",
            f"{ALIAS['session']} id: {session.id}",
            f"{ALIAS['session']} name: {session.name}",
            f"#{ALIAS['window']}s: {n_tables}",
        ]
        max_session_info_width = max([len(v) for v in session_info])
        max_col_width = max(max_col_width, max_session_info_width)
        top = "┌" + "─" * max_col_width + "┐"
        cap = ["│" + str(v).ljust(max_col_width) + "│" for v in session_info]
        sep = "├" + "─" * max_col_width + "┤"
        window_strings = ["│" + s.ljust(max_col_width) + "│" for s in window_strings]
        bottom = "└" + "─" * max_col_width + "┘"
        return [top, *cap, sep, *window_strings, bottom]
    else:
        return
