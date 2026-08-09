#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_ENV = "AMUX_CONTEXT_CONFIG"

ALIAS = {"session": "workspace", "window": "task", "pane": "agent"}
NOTE_SCOPES = ("agent", "task", "workspace")
NOTE_KINDS = ("note", "decision", "finding", "blocker")
EVENT_KINDS = ("busy", "exit", "notify", "spawn", "stop")
WAIT_STATES = ("idle", "needs-input", "dead")

DETAIL_LIMIT = 2000

MIN_REDACTABLE_TOKEN = 16

DEFAULT_TIMEOUT_S = 15.0
POLL_WINDOW_S = 25.0
POLL_SLACK_S = 5.0
POLL_FLOOR_S = 0.05

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_DEAD = 2

HOST_ONLY = {
    "spw": "spawns a workspace on the host tmux server",
    "spg": "spawns a task grid on the host tmux server",
    "kw": "kills a host workspace",
    "kg": "kills a host task grid",
    "integrate": "merges branches in the host's integration worktree",
    "monitor": "reads the host tmux server directly",
    "lsw": "lists sessions on the host tmux server",
    "lsg": "lists windows on the host tmux server",
}
HOST_ONLY_EVENT = {"tail": "streams raw host event rows"}

SUPPORTED = "ctx, notes, note, event emit|state|wait"


class UsageError(Exception):
    pass


class BoundaryError(UsageError):
    pass


class ClientError(Exception):
    pass


class ConfigError(ClientError):
    pass


@dataclass
class Config:
    endpoint: str
    token: str
    path: Path
    timeout: float = DEFAULT_TIMEOUT_S


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "amux" / "context.json"


def load_config(path: str | os.PathLike | None = None) -> Config:
    target = Path(path or os.environ.get(CONFIG_ENV) or default_config_path())
    try:
        mode = target.stat().st_mode
        raw = target.read_text()
    except OSError as exc:
        raise ConfigError(
            f"no sandbox context configuration at {target} ({exc.strerror}); "
            f"this amux is the sandbox context client and needs one to reach the host"
        ) from exc
    if mode & 0o077:
        raise ConfigError(
            f"{target} is readable beyond its owner (mode {mode & 0o777:04o}); "
            f"it holds a capability token and must be mode 0600"
        )
    try:
        document = json.loads(raw)
        endpoint = str(document["endpoint"])
        token = str(document["token"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ConfigError(
            f"{target} is not a valid sandbox context configuration "
            f'(expected {{"endpoint": ..., "token": ...}}): {exc}'
        ) from exc
    timeout = document.get("timeout")
    return Config(
        endpoint=endpoint.rstrip("/"),
        token=token,
        path=target,
        timeout=float(timeout) if timeout else DEFAULT_TIMEOUT_S,
    )


class ContextClient:
    def __init__(self, config: Config):
        self.config = config
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def redact(self, text: str) -> str:
        token = self.config.token
        if len(token) < MIN_REDACTABLE_TOKEN:
            return text
        return text.replace(token, "***")

    def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict:
        return self._request("GET", path, params=params, timeout=timeout)

    def post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        return self._request("POST", path, body=body, timeout=timeout)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        url = self.config.endpoint + path
        if params:
            query = {k: v for k, v in params.items() if v is not None and v != ""}
            if query:
                url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.config.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(
                request, timeout=timeout or self.config.timeout
            ) as response:
                return self._document(response.read())
        except urllib.error.HTTPError as exc:
            raise self._service_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ClientError(
                f"cannot reach the amux context service at {self.config.endpoint}: "
                f"{reason}. It runs on the host; a sandbox has no other context path."
            ) from None

    def _document(self, raw: bytes) -> dict:
        try:
            document = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            raise ClientError(
                f"the amux context service returned a malformed response: {exc}"
            ) from None
        if not isinstance(document, dict):
            raise ClientError(
                "the amux context service returned a malformed response: "
                f"expected a JSON object, got {type(document).__name__}"
            )
        return document

    def _service_error(self, exc: urllib.error.HTTPError) -> ClientError:
        try:
            envelope = json.loads(exc.read() or b"{}")
            error = envelope["error"]
            detail = f"{error['code']}: {error['message']}"
        except (json.JSONDecodeError, KeyError, TypeError, OSError, ValueError):
            detail = f"HTTP {exc.code} {exc.reason}"
        return ClientError(f"the amux context service refused the request: {detail}")


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


def runtime_to_string(me: dict) -> str:
    runtime = me.get("runtime") or ""
    if not runtime or runtime == "host":
        return ""
    parts = [runtime, me.get("runtime_status") or "", me.get("sandbox_name") or ""]
    return "runtime: " + " ".join(p for p in parts if p)


def tuning_to_string(me: dict) -> str:
    parts = [f"{k}: {me.get(k)}" for k in ("model", "effort") if me.get(k)]
    return "  ".join(parts)


def context_to_string(ctx: dict) -> list[str]:
    me = ctx["self"]
    branch = f"  branch:{me.get('branch')}" if me.get("branch") else ""
    lines = [
        f"you: {me['name']}  {me['agent']} @{me['label']} {me['pane']}  "
        f"{ALIAS['window']}:{me['task']}  {ALIAS['session']}:{me['workspace']}  "
        f"{me['state']}{branch}  {me['cwd']}",
    ]
    runtime_line = runtime_to_string(me)
    if runtime_line:
        lines.append(runtime_line)
    tuning_line = tuning_to_string(me)
    if tuning_line:
        lines.append(tuning_line)
    lines.append(f"team @ {me['workspace']}")
    rows = [a for group in ctx["team"] for a in group["agents"]]
    wn = max(len(a["name"] or "-") for a in rows)
    wa = max(len(a["agent"]) for a in rows)
    wd = max(len(_addr(a)) for a in rows)
    ws = max(len(a["state"]) for a in rows)
    for i, group in enumerate(ctx["team"]):
        own = f" (your {ALIAS['window']})" if i == 0 else ""
        lines.append(f"  {group['task']}{own}")
        for a in group["agents"]:
            row = (
                f"    {(a['name'] or '-'):<{wn}}  {a['agent']:<{wa}}  "
                f"{_addr(a):<{wd}}  {a['state']:<{ws}}"
            )
            if a["pane"] == me["pane"]:
                row += " (you)"
            else:
                last = a.get("last_event")
                if last:
                    row += f"  {_age(last['ts'])}"
                    if last["detail"]:
                        row += f'  "{last["detail"]}"'
                if a.get("branch"):
                    row += f"  {a['branch']}"
                if a.get("last_commit"):
                    row += f'  "{a["last_commit"]}"'
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


def note_to_string(note: dict) -> str:
    return (
        f"{note['id']:>3}  {note['scope']:<9} {note['kind']:<9} "
        f"{note['agent'] or note['pane']:<12}  {note['text']}"
    )


def _one_of(value: str | None, allowed: tuple[str, ...], flag: str) -> str | None:
    if value is None or value in allowed:
        return value
    raise UsageError(f"{flag} must be one of {'/'.join(allowed)}, got '{value}'")


def _refuse_host_flags(args: argparse.Namespace, *flags: str) -> None:
    for flag in flags:
        if getattr(args, flag.lstrip("-").replace("-", "_"), None):
            raise BoundaryError(
                f"{flag} is host-only: inside a sandbox this amux is bound to one "
                f"agent identity and scope by its capability token. "
                f"Run 'amux ... {flag}' on the host to look at another."
            )


def cmd_ctx(client: ContextClient, args: argparse.Namespace) -> int:
    _refuse_host_flags(args, "--pane")
    ctx = client.get("/v1/context")
    if args.json:
        print(json.dumps(ctx))
    else:
        print("\n".join(context_to_string(ctx)))
    return EXIT_OK


def cmd_notes(client: ContextClient, args: argparse.Namespace) -> int:
    _refuse_host_flags(args, "--workspace", "--repo", "--pane")
    scope = _one_of(args.scope, NOTE_SCOPES, "--scope")
    kind = _one_of(args.kind, NOTE_KINDS, "--kind")
    document = client.get(
        "/v1/notes",
        {"task": args.task, "scope": scope, "kind": kind, "limit": args.n},
    )
    notes = document.get("notes") or []
    for note in notes:
        if args.json:
            print(json.dumps(note, separators=(",", ":"), default=str))
        else:
            print(note_to_string(note))
    return EXIT_OK


def cmd_note(client: ContextClient, args: argparse.Namespace) -> int:
    _refuse_host_flags(args, "--pane")
    scope = _one_of(args.scope, NOTE_SCOPES, "--scope") or "task"
    kind = _one_of(args.kind, NOTE_KINDS, "--kind") or "note"
    text = " ".join(args.text).strip()
    if not text:
        raise UsageError("note text is empty")
    document = client.post("/v1/notes", {"text": text, "scope": scope, "kind": kind})
    note = document.get("note") or {}
    origin = f" [{note['name']}]" if note.get("name") else ""
    print(
        f"note #{note.get('id')} @ {note.get('workspace')}/{note.get('task')}{origin} "
        f"(scope={note.get('scope', scope)}, kind={note.get('kind', kind)})"
    )
    return EXIT_OK


def _hook_payload() -> dict:
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def cmd_event_emit(client: ContextClient, args: argparse.Namespace) -> int:
    _one_of(args.kind, EVENT_KINDS, "kind")
    payload = _hook_payload() if args.detail is None else {}
    detail = (
        args.detail
        or payload.get("message")
        or payload.get("tool_name")
        or payload.get("reason")
        or payload.get("last-assistant-message")
        or ""
    )
    try:
        client.post(
            "/v1/events", {"kind": args.kind, "detail": str(detail)[:DETAIL_LIMIT]}
        )
    except Exception:
        pass
    return EXIT_OK


def cmd_event_state(client: ContextClient, args: argparse.Namespace) -> int:
    panes = client.get("/v1/events/state").get("panes") or []
    if args.json:
        print(json.dumps(panes))
        return EXIT_OK
    for entry in panes:
        print(
            f"{entry['pane']}\t{entry['state'] or '-'}\t"
            f"{entry['workspace']}/{entry['task']}\t{entry['name']}"
        )
    return EXIT_OK


def cmd_event_wait(client: ContextClient, args: argparse.Namespace) -> int:
    deadline = time.monotonic() + args.timeout
    cursor: Any = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f"timeout after {args.timeout}s", flush=True)
            return EXIT_FAIL
        window = min(remaining, POLL_WINDOW_S)
        document = client.get(
            "/v1/events/wait",
            {
                "pane": args.pane,
                "timeout": f"{window:.3f}",
                "after": cursor,
                "states": ",".join(WAIT_STATES),
            },
            timeout=window + POLL_SLACK_S,
        )
        state = document.get("state")
        if state:
            print(state)
            return EXIT_DEAD if state == "dead" else EXIT_OK
        if document.get("cursor") is not None:
            cursor = document["cursor"]
        time.sleep(min(POLL_FLOOR_S, max(0.0, deadline - time.monotonic())))


def _boundary_message(command: str, reason: str) -> str:
    return (
        f"'{command}' runs only on the amux host: it {reason}, and a sandbox has "
        f"no host tmux socket, state directory, or context database — by design.\n"
        f"  run it on the host:  amux {command} ...\n"
        f"  available here:      {SUPPORTED}"
    )


def _screen_host_only(argv: list[str]) -> str | None:
    if not argv:
        return None
    command = argv[0]
    if command in HOST_ONLY:
        return _boundary_message(command, HOST_ONLY[command])
    if command == "event" and len(argv) > 1 and argv[1] in HOST_ONLY_EVENT:
        return _boundary_message(f"event {argv[1]}", HOST_ONLY_EVENT[argv[1]])
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amux",
        description=(
            "amux context client for a Docker Sandbox agent. Supported: "
            f"{SUPPORTED}. Host control commands (spawn, kill, integrate, "
            "monitor, list) are unavailable inside a sandbox."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ctx = sub.add_parser("ctx", help="this agent's identity, team roster, and notes")
    p_ctx.add_argument("--json", action="store_true", help="machine-readable output")
    p_ctx.add_argument("--pane", default=None, help=argparse.SUPPRESS)
    p_ctx.set_defaults(func=cmd_ctx)

    p_notes = sub.add_parser("notes", help="list notes visible to this agent")
    p_notes.add_argument("--task", default=None, help="task filter")
    p_notes.add_argument(
        "--scope", default=None, help=f"one of {'/'.join(NOTE_SCOPES)}"
    )
    p_notes.add_argument("--kind", default=None, help=f"one of {'/'.join(NOTE_KINDS)}")
    p_notes.add_argument("-n", type=int, default=20, help="max notes")
    p_notes.add_argument("--json", action="store_true", help="JSONL output")
    for suppressed in ("--workspace", "--repo", "--pane"):
        p_notes.add_argument(suppressed, default=None, help=argparse.SUPPRESS)
    p_notes.set_defaults(func=cmd_notes)

    p_note = sub.add_parser("note", help="publish a scoped note")
    p_note.add_argument("text", nargs="+", help="note text")
    p_note.add_argument(
        "--scope",
        default="task",
        help=f"one of {'/'.join(NOTE_SCOPES)} (default: task)",
    )
    p_note.add_argument(
        "--kind", default="note", help=f"one of {'/'.join(NOTE_KINDS)} (default: note)"
    )
    p_note.add_argument("--pane", default=None, help=argparse.SUPPRESS)
    p_note.set_defaults(func=cmd_note)

    p_event = sub.add_parser("event", help="agent state events")
    ev = p_event.add_subparsers(dest="event_command", required=True)

    p_emit = ev.add_parser("emit", help="append an event (called by agent hooks)")
    p_emit.add_argument("kind", help=f"one of {'/'.join(EVENT_KINDS)}")
    p_emit.add_argument(
        "--detail",
        default=None,
        help="free-form note (default: from hook JSON on stdin)",
    )
    p_emit.add_argument("--pane", default=None, help=argparse.SUPPRESS)
    p_emit.add_argument("--agent", default="", help=argparse.SUPPRESS)
    p_emit.set_defaults(func=cmd_event_emit)

    p_state = ev.add_parser("state", help="resolved state of every visible agent")
    p_state.add_argument("--json", action="store_true", help="machine-readable output")
    p_state.set_defaults(func=cmd_event_state)

    p_wait = ev.add_parser("wait", help="block until a pane reaches a state")
    p_wait.add_argument("pane", help="pane id, e.g. %%42")
    p_wait.add_argument("--timeout", type=float, default=300.0)
    p_wait.set_defaults(func=cmd_event_wait)

    return parser


def main(argv: list[str] | None = None, *, config_path: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    boundary = _screen_host_only(argv)
    if boundary is not None:
        print(f"amux: {boundary}", file=sys.stderr)
        return EXIT_USAGE

    args = build_parser().parse_args(argv)
    emitting = args.func is cmd_event_emit
    client = None
    try:
        client = ContextClient(load_config(config_path))
        return args.func(client, args)
    except UsageError as exc:
        print(f"amux: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ClientError as exc:
        if emitting:
            return EXIT_OK
        message = client.redact(str(exc)) if client else str(exc)
        print(f"amux: {message}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
