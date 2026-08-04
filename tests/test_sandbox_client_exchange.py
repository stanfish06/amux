"""Two sandbox clients coordinating through one host store (task 3.4).

Unlike the unit tests, the fake service here is *stateful* and backed by the
real `store`, so note visibility, repository filtering and event attribution are
decided by the same functions native amux uses — not by canned JSON. That is the
point: it proves two shims can actually coordinate, and that agent-private notes
stay private, without depending on the HTTP service implementation.

`_StoreBackedService` is a stand-in for `context_service.py`, not a second
implementation of it: it maps the pinned wire contract onto store calls in the
fewest lines that make the exchange real. When the service lands, these tests
should be re-pointed at it (task 6.3).
"""

from __future__ import annotations

import ast
import json
import threading
import time

import pytest

from amux import events, sandbox_bootstrap as sb
from amux import sandbox_client as sc
from amux import store
from test_sandbox_client_fake_service import FakeContextService, Recorded

WORKSPACE = "myproj"
TASK = "fix"
REPO = "/work/repo"


# --- a stateful service over the real store ----------------------------------


class _StoreBackedService(FakeContextService):
    """The pinned wire contract, served from the authoritative host store."""

    def __init__(self):
        super().__init__(routes={})
        self.tmux_state: dict[str, str] = {}  # what the host pane option becomes
        self._woke = threading.Event()

    # identity comes from the token, never from the request
    def _identity(self, record: Recorded) -> dict | None:
        header = record.authorization
        token = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
        return store.context_token_record(token)

    def respond(self, record: Recorded):
        me = self._identity(record)
        if me is None:
            return 401, {"error": {"code": "unauthorized", "message": "no capability"}}
        handler = {
            ("GET", "/v1/context"): self._context,
            ("GET", "/v1/notes"): self._notes,
            ("POST", "/v1/notes"): self._add_note,
            ("POST", "/v1/events"): self._add_event,
            ("GET", "/v1/events/state"): self._state,
            ("GET", "/v1/events/wait"): self._wait,
        }.get((record.method, record.path))
        if handler is None:
            return 404, {"error": {"code": "not_found", "message": record.path}}
        result = handler(record, me)
        return result if isinstance(result, tuple) else (200, result)

    # -- roster and state, resolved without tmux ---------------------------

    def _entry(self, row: dict) -> dict:
        latest = store.latest_event(row["pane"])
        event = events.Event.from_row(latest) if latest else None
        state = events.resolve_state(alive=None, latest=event) or "idle"
        return {
            "pane": row["pane"],
            "name": row["name"],
            "agent": row["agent"],
            "label": row["name"][:4],
            "state": state,
            "cwd": row["path"] or f"/sandbox/{row['sandbox_name']}",
            "branch": row["branch"],
            "runtime": row["runtime"],
            "runtime_status": row["runtime_status"],
            "sandbox_name": row["sandbox_name"],
            "last_event": (
                {"kind": event.kind, "ts": event.ts, "detail": event.detail}
                if event
                else None
            ),
        }

    def _roster(self, me: dict) -> list[dict]:
        rows = store.worktrees_for(me["workspace"], me["task"])
        return [self._entry(r) for r in rows if r["repo"] == me["repo"]]

    def _context(self, record: Recorded, me: dict):
        mine = self._entry(dict(me, path=me["path"]))
        return {
            "self": {**mine, "workspace": me["workspace"], "task": me["task"]},
            "team": [{"task": me["task"], "agents": self._roster(me)}],
            "notes": store.visible_notes(
                workspace=me["workspace"],
                task=me["task"],
                pane=me["pane"],
                repo=me["repo"],
            ),
        }

    def _notes(self, record: Recorded, me: dict):
        limit = int(record.q("limit") or 10)
        scope = record.q("scope")
        notes = store.visible_notes(
            workspace=me["workspace"],
            task=record.q("task") or me["task"],
            pane=me["pane"],
            kind=record.q("kind"),
            repo=me["repo"],
            limit=limit,
        )
        if scope:  # narrowing only: visible_notes has already excluded the rest
            notes = [n for n in notes if n["scope"] == scope]
        return {"notes": notes, "cursor": notes[0]["id"] if notes else None}

    def _add_note(self, record: Recorded, me: dict):
        body = json.loads(record.body or b"{}")
        note_id = store.add_note(
            workspace=me["workspace"],
            task=me["task"],
            pane=me["pane"],
            agent=me["agent"],
            worktree_id=me["worktree_id"],
            repo=me["repo"],
            text=body["text"],
            scope=body.get("scope", "task"),
            kind=body.get("kind", "note"),
        )
        rows = store.query_notes(workspace=me["workspace"], limit=200)
        row = next(n for n in rows if n["id"] == note_id)
        return 201, {"note": {**row, "name": me["name"]}}

    def _add_event(self, record: Recorded, me: dict):
        body = json.loads(record.body or b"{}")
        kind = body["kind"]
        event_id = store.add_event(
            ts=time.time(),
            pane=me["pane"],
            kind=kind,
            workspace=me["workspace"],
            task=me["task"],
            agent=me["agent"],
            detail=body.get("detail", ""),
            worktree_id=me["worktree_id"],
            repo=me["repo"],
        )
        # what the host does after committing: update the pane option, wake waiters
        self.tmux_state[me["pane"]] = events.STATE_BY_KIND[kind]
        self._woke.set()
        return {"event": {"kind": kind, "pane": me["pane"]}, "cursor": event_id}

    def _state(self, record: Recorded, me: dict):
        return {
            "panes": [
                {
                    **entry,
                    "kind": "amux",
                    "workspace": me["workspace"],
                    "task": me["task"],
                }
                for entry in self._roster(me)
            ]
        }

    def _wait(self, record: Recorded, me: dict):
        pane = record.q("pane") or me["pane"]
        wanted = {s for s in str(record.q("states") or "").split(",") if s}
        deadline = time.monotonic() + min(float(record.q("timeout") or 1.0), 5.0)
        while True:
            latest = store.latest_event(pane)
            event = events.Event.from_row(latest) if latest else None
            state = events.resolve_state(alive=None, latest=event)
            cursor = latest["id"] if latest else None
            if state in wanted:
                return {"pane": pane, "state": state, "cursor": cursor}
            if time.monotonic() >= deadline:
                # the cap expired: null state plus a usable cursor, so the
                # client resumes instead of restarting
                return {"pane": pane, "state": None, "cursor": cursor}
            self._woke.wait(0.02)
            self._woke.clear()


# --- two sandbox agents ------------------------------------------------------


class Agent:
    """One simulated sandbox: a registered execution row, a real capability, and
    a config file staged exactly the way bootstrap stages it."""

    def __init__(self, tmp_path, name: str, pane: str, agent: str, endpoint: str):
        self.name = name
        self.pane = pane
        self.worktree_id = store.register_worktree(
            pane=pane,
            workspace=WORKSPACE,
            task=TASK,
            path="",  # a sandbox row describes a microVM, not a directory
            branch=f"amux/{WORKSPACE}/{TASK}/{name}",
            agent=agent,
            name=name,
            repo=REPO,
            runtime="docker-sandbox",
            runtime_status="running",
            sandbox_name=f"amux-{WORKSPACE}-{TASK}-{name}-ab12cd",
            sandbox_id=f"sbx_{name}",
        )
        self.token, self.token_id = store.mint_context_token(
            self.worktree_id, permissions=("context:read", "context:write")
        )
        directory = tmp_path / "vm" / name
        self.config = sb.stage_config_file(endpoint, self.token, directory=directory)

    def run(self, *argv: str) -> int:
        # Explicit, not via the environment: two agents run in one process here
        # and one of them runs on another thread.
        return sc.main(list(argv), config_path=str(self.config))


@pytest.fixture
def exchange(tmp_path, monkeypatch):
    """A live store-backed service plus two sandbox agents on it."""
    with _StoreBackedService() as service:
        monkeypatch.setenv(sc.CONFIG_ENV, "")
        hawk = Agent(tmp_path, "brave-hawk", "%7", "claude", service.endpoint)
        owl = Agent(tmp_path, "golden-owl", "%9", "codex", service.endpoint)
        yield service, hawk, owl


def lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


# --- notes travel between two sandboxes --------------------------------------


def test_a_task_note_from_one_sandbox_is_read_by_the_other(exchange, capsys):
    _, hawk, owl = exchange
    assert hawk.run("note", "migration", "applied,", "26", "tests", "green") == 0
    capsys.readouterr()

    assert owl.run("notes") == 0
    assert any("migration applied, 26 tests green" in ln for ln in lines(capsys))


def test_the_note_is_attributed_to_its_author_not_its_reader(exchange, capsys):
    _, hawk, owl = exchange
    hawk.run("note", "using sqlite", "--kind", "decision")
    capsys.readouterr()

    owl.run("notes", "--json")
    note = json.loads(lines(capsys)[0])
    assert (note["pane"], note["agent"], note["kind"]) == ("%7", "claude", "decision")
    assert note["worktree_id"] == hawk.worktree_id


def test_an_agent_scoped_note_stays_private_to_its_author(exchange, capsys):
    _, hawk, owl = exchange
    assert hawk.run("note", "my own scratch reasoning", "--scope", "agent") == 0
    capsys.readouterr()

    assert owl.run("notes", "--json") == 0
    assert lines(capsys) == []

    assert hawk.run("notes", "--json") == 0
    mine = [json.loads(ln) for ln in lines(capsys)]
    assert [n["text"] for n in mine] == ["my own scratch reasoning"]


def test_a_workspace_note_reaches_a_sandbox_in_another_task(exchange, tmp_path, capsys):
    service, hawk, _ = exchange
    other = Agent(tmp_path, "misty-panda", "%11", "claude", service.endpoint)
    store.set_worktree_runtime(other.worktree_id, runtime_status="running")
    hawk.run("note", "everyone: the port moved", "--scope", "workspace")
    capsys.readouterr()

    assert other.run("notes") == 0
    assert any("the port moved" in ln for ln in lines(capsys))


def test_a_task_note_does_not_reach_a_sandbox_in_a_different_repository(
    exchange, tmp_path, capsys
):
    """Repository filtering is what stops two workspaces that happen to share a
    task name from leaking into each other."""
    service, hawk, _ = exchange
    hawk.run("note", "repo-local finding", "--kind", "finding")
    capsys.readouterr()

    outsider = Agent(tmp_path, "lone-fox", "%13", "claude", service.endpoint)
    store.set_worktree_runtime(outsider.worktree_id)
    with store._connect() as conn:  # a row in the same workspace/task, other repo
        conn.execute(
            "UPDATE worktrees SET repo = ? WHERE id = ?",
            ("/elsewhere/repo", outsider.worktree_id),
        )
    assert outsider.run("notes", "--json") == 0
    assert lines(capsys) == []


def test_both_sandboxes_see_each_other_on_the_ctx_roster(exchange, capsys):
    _, hawk, _ = exchange
    assert hawk.run("ctx") == 0
    output = capsys.readouterr().out
    assert "you: brave-hawk" in output
    assert "golden-owl" in output
    assert "runtime: docker-sandbox running amux-myproj-fix-brave-hawk-ab12cd" in output


# --- state transitions cross the boundary ------------------------------------


def test_a_sandbox_event_updates_the_host_pane_state(exchange):
    service, hawk, _ = exchange
    assert hawk.run("event", "emit", "notify", "--detail", "needs approval") == 0
    assert service.tmux_state["%7"] == "needs-input"


def test_one_sandbox_reads_the_others_state_transition(exchange, capsys):
    _, hawk, owl = exchange
    hawk.run("event", "emit", "busy", "--detail", "Bash")
    capsys.readouterr()

    assert owl.run("event", "state") == 0
    rows = dict(ln.split("\t")[:2] for ln in lines(capsys))
    assert rows["%7"] == "busy"


def test_a_waiting_sandbox_is_released_by_the_others_event(exchange, capsys):
    _, hawk, owl = exchange
    released: list[int] = []

    def wait_for_hawk():
        released.append(owl.run("event", "wait", "%7", "--timeout", "10"))

    waiter = threading.Thread(target=wait_for_hawk)
    waiter.start()
    time.sleep(0.1)  # let the long poll actually be in flight
    hawk.run("event", "emit", "stop")
    waiter.join(timeout=15)

    assert not waiter.is_alive()
    assert released == [0]
    assert "idle" in capsys.readouterr().out


def test_waiting_on_a_dead_teammate_exits_2(exchange, capsys):
    _, hawk, owl = exchange
    hawk.run("event", "emit", "exit", "--detail", "SessionEnd")
    capsys.readouterr()

    assert owl.run("event", "wait", "%7", "--timeout", "5") == 2
    assert capsys.readouterr().out.strip() == "dead"


def test_an_event_is_attributed_to_the_token_not_to_a_body_field(exchange):
    """`event emit` sends no pane at all, so a compromised client cannot post as
    a teammate — the identity is the capability."""
    _, hawk, _ = exchange
    hawk.run("event", "emit", "busy", "--pane", "%9", "--agent", "codex")
    assert store.latest_event("%9") is None  # the named teammate is untouched
    mine = store.latest_event("%7")
    assert mine is not None
    assert (mine["agent"], mine["kind"]) == ("claude", "busy")


# --- a revoked capability stops working --------------------------------------


def test_removing_a_sandbox_revokes_its_capability(exchange, capsys):
    _, hawk, owl = exchange
    assert hawk.run("notes") == 0
    capsys.readouterr()

    assert store.revoke_context_tokens_for_worktree(hawk.worktree_id) == 1
    assert hawk.run("notes") == 1
    assert "unauthorized" in capsys.readouterr().err
    assert owl.run("notes") == 0  # the other capability is unaffected


# --- nothing host-side is reachable from a sandbox ---------------------------


def test_no_sandbox_config_names_the_state_directory_the_db_or_the_tmux_socket(
    exchange, isolate_state
):
    """The only thing a sandbox is given is an endpoint and a capability."""
    _, hawk, owl = exchange
    for agent in (hawk, owl):
        document = json.loads(agent.config.read_text())
        assert set(document) == {"endpoint", "token"}
        blob = agent.config.read_text()
        for forbidden in (str(isolate_state), "context.db", "/tmp/tmux", ".git"):
            assert forbidden not in blob


def test_a_sandbox_client_reaches_the_store_only_through_the_service():
    """`sandbox_client` imports no database, tmux or subprocess machinery — the
    boundary is a property of the file, not of how it is called. Checked over the
    import graph rather than the text, so prose about `context.db` and the store
    cannot pass or fail this."""
    tree = ast.parse(open(sc.__file__ or "").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "argparse",
        "dataclasses",
        "json",
        "os",
        "pathlib",
        "sys",
        "time",
        "typing",
        "urllib",
        "__future__",
    }, f"unexpected imports in the sandbox shim: {sorted(imported)}"
    assert imported.isdisjoint({"sqlite3", "subprocess", "socket", "libtmux", "amux"})


def test_host_control_commands_are_still_refused_with_a_real_capability(
    exchange, capsys
):
    _, hawk, _ = exchange
    assert hawk.run("integrate", WORKSPACE, TASK) == 2
    assert "host" in capsys.readouterr().err.lower()
