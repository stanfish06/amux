from libtmux import Window, Session
from amux.shared import ALIAS


def window_to_string(window: Window):
    panes_info = {}
    for p in window.panes:
        if p.id:
            panes_info[p.pane_id] = {
                "id": p.id,
                "index": p.index if p.index else -1,
                "title": p.title if p.title else "",
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
