# Reliable Agent Messaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable `amux send` delivery that waits for an idle target and confirms a fresh idle-to-busy transition for host and Docker-sandbox agents.

**Architecture:** A new `amux.messages` module owns one deadline-driven dispatcher, target-scoped advisory locks, attribution, and status results. Native CLI calls enter it directly; the sandbox client calls a capability-protected context-service route that invokes the same dispatcher. SQLite records every attempt, while cursor-aware event waiting prevents stale state from being mistaken for delivery.

**Tech Stack:** Python 3.12, SQLite WAL, libtmux/tmux, stdlib `fcntl`, threaded stdlib HTTP service, pytest, uv, PyInstaller.

---

## File structure

- Create `src/amux/messages.py`: identities, validation, envelope rendering, file locks, dispatcher, output rendering.
- Modify `src/amux/store.py`: schema version 5 and durable message operations.
- Modify `src/amux/events.py`: event cursor and fresh-state wait.
- Modify `src/amux/core.py`: expose the existing safe composer submission primitive.
- Modify `src/amux/cli.py`: native `send` and `messages` commands.
- Modify `src/amux/context_service.py`: message permission and authenticated HTTP routes.
- Modify `src/amux/sandbox_client.py`: matching sandbox commands.
- Create `tests/test_message_store.py`, `tests/test_message_events.py`, `tests/test_messages.py`, `tests/test_message_cli.py`, `tests/test_context_service_messages.py`, and `tests/test_sandbox_client_messages.py`.
- Modify `tests/test_store_migration.py` and existing permission/bootstrap tests where exact schema or capability sets change.
- Modify `README.md` and `skills/amux/SKILL.md`: public contract, sandbox boundary, and removal of the raw send-keys workaround.

### Task 1: Durable message schema and store operations

**Files:**
- Modify: `src/amux/store.py:17-218,255-450`
- Create: `tests/test_message_store.py`
- Modify: `tests/test_store_migration.py:1-280`

- [ ] **Step 1: Write failing store and migration tests**

Create `tests/test_message_store.py` with concrete rows and terminal-transition assertions:

```python
from pathlib import Path

from amux import store


def create(db_path: Path, *, deadline: float = 200.0) -> int:
    return store.create_message(
        created_ts=100.0,
        deadline_ts=deadline,
        sender_worktree_id=1,
        target_worktree_id=2,
        repo="/repo",
        workspace="ws",
        sender_task="plan",
        sender_pane="%1",
        sender_agent="codex",
        sender_name="red-fox",
        target_task="review",
        target_pane="%2",
        target_agent="claude",
        target_name="blue-owl",
        target_created_ts=90.0,
        body="review this",
        db_path=db_path,
    )


def test_message_moves_from_pending_to_delivered(db_path: Path) -> None:
    message_id = create(db_path)
    store.set_message_envelope(message_id, "[amux message #1] review this", db_path)
    store.mark_message_submitted(message_id, 120.0, db_path)
    assert store.finish_message(message_id, "delivered", now=125.0, db_path=db_path)
    row = store.message_by_id(message_id, db_path)
    assert row is not None
    assert row["status"] == "delivered"
    assert row["submitted_ts"] == 120.0
    assert row["delivered_ts"] == 125.0
    assert row["envelope"].endswith("review this")


def test_terminal_message_cannot_be_finished_twice(db_path: Path) -> None:
    message_id = create(db_path)
    assert store.finish_message(message_id, "undelivered", "timeout", "no busy event", 201.0, db_path)
    assert not store.finish_message(message_id, "delivered", now=202.0, db_path=db_path)
    assert store.message_by_id(message_id, db_path)["status"] == "undelivered"


def test_expiry_marks_only_overdue_pending_rows(db_path: Path) -> None:
    expired = create(db_path, deadline=110.0)
    live = create(db_path, deadline=210.0)
    assert store.expire_messages(120.0, db_path) == 1
    assert store.message_by_id(expired, db_path)["reason_code"] == "deadline_expired"
    assert store.message_by_id(live, db_path)["status"] == "pending"


def test_visible_messages_are_only_sent_or_received_by_the_pane(db_path: Path) -> None:
    mine = create(db_path)
    rows = store.visible_messages("ws", "/repo", "%1", db_path=db_path)
    assert [row["id"] for row in rows] == [mine]
    assert store.visible_messages("ws", "/repo", "%9", db_path=db_path) == []
```

Extend `tests/test_store_migration.py` so frozen v2/v3 databases upgrade to version 5, assert `messages` exists, and assert repeated migration stays idempotent.

- [ ] **Step 2: Run the store tests and verify RED**

Run:

```bash
uv run pytest tests/test_message_store.py tests/test_store_migration.py -q
```

Expected: failures name missing `store.create_message` and current schema version 4.

- [ ] **Step 3: Implement schema version 5 and message operations**

In `src/amux/store.py`, add the exact status vocabulary and table to `_SCHEMA`:

```python
MessageStatus = Literal["pending", "delivered", "undelivered"]
MESSAGE_STATUSES = ("pending", "delivered", "undelivered")
SCHEMA_VERSION = 5
```

```sql
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  created_ts REAL NOT NULL,
  updated_ts REAL NOT NULL,
  deadline_ts REAL NOT NULL,
  submitted_ts REAL,
  delivered_ts REAL,
  sender_worktree_id INTEGER REFERENCES worktrees(id),
  target_worktree_id INTEGER REFERENCES worktrees(id),
  repo TEXT NOT NULL DEFAULT '',
  workspace TEXT NOT NULL,
  sender_task TEXT NOT NULL,
  sender_pane TEXT NOT NULL,
  sender_agent TEXT NOT NULL DEFAULT '',
  sender_name TEXT NOT NULL DEFAULT '',
  target_task TEXT NOT NULL,
  target_pane TEXT NOT NULL,
  target_agent TEXT NOT NULL DEFAULT '',
  target_name TEXT NOT NULL DEFAULT '',
  target_created_ts REAL NOT NULL,
  body TEXT NOT NULL,
  envelope TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  reason_code TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(workspace, repo, sender_pane, id);
CREATE INDEX IF NOT EXISTS idx_messages_target ON messages(workspace, repo, target_pane, id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status, deadline_ts, id);
```

Add `create_message`, `set_message_envelope`, `mark_message_submitted`, `finish_message`, `expire_messages`, `message_by_id`, and `visible_messages`. Terminal writes use `UPDATE messages SET status = ?, reason_code = ?, reason = ?, updated_ts = ?, delivered_ts = ? WHERE id = ? AND status = 'pending'`. `finish_message` sets `delivered_ts` only for delivered status and returns `cursor.rowcount == 1`. `visible_messages` calls `expire_messages(time.time())`, filters `(sender_pane = ? OR target_pane = ?)`, applies optional status, and orders by `id DESC LIMIT ?`.

- [ ] **Step 4: Run store tests and verify GREEN**

Run:

```bash
uv run pytest tests/test_message_store.py tests/test_store_migration.py -q
```

Expected: all tests pass and both fresh and frozen databases report schema version 5.

- [ ] **Step 5: Commit the store slice**

```bash
git add src/amux/store.py tests/test_message_store.py tests/test_store_migration.py
git commit -m "feat(messages): persist delivery attempts"
```

### Task 2: Share the safe interface-submission primitive

**Files:**
- Modify: `src/amux/core.py:316-423`
- Modify: `tests/test_bootstrap_send.py:154-320`

- [ ] **Step 1: Add a failing direct-submission test**

Add to `tests/test_bootstrap_send.py`:

```python
def test_shared_submission_returns_the_problem_without_fail_soft_wrapping():
    p = pane(capture("codex_0.146.0_ready"), capture("codex_0.146.0_text_on_the_input_line"))
    clock = Clock(pane=p)
    problem = core.submit_to_interface(
        p,
        STUCK,
        timeout=1.0,
        poll=0.1,
        pause=0.0,
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert "input line" in problem
```

- [ ] **Step 2: Run the test and verify RED**

```bash
uv run pytest tests/test_bootstrap_send.py::test_shared_submission_returns_the_problem_without_fail_soft_wrapping -q
```

Expected: `AttributeError: module 'amux.core' has no attribute 'submit_to_interface'`.

- [ ] **Step 3: Expose the existing implementation without changing behavior**

Rename `_send_bootstrap` to `submit_to_interface` with its current signature and body. Keep `send_bootstrap` as the fail-soft wrapper:

```python
def send_bootstrap(
    pane: Pane,
    text: str,
    *,
    timeout: float | None = None,
    poll: float | None = None,
    pause: float | None = None,
    clock=time.monotonic,
    sleep=time.sleep,
) -> str:
    timeout = BOOTSTRAP_READY_TIMEOUT_S if timeout is None else timeout
    poll = BOOTSTRAP_POLL_S if poll is None else poll
    pause = BOOTSTRAP_SUBMIT_PAUSE_S if pause is None else pause
    try:
        return submit_to_interface(
            pane,
            text,
            timeout=timeout,
            poll=poll,
            pause=pause,
            clock=clock,
            sleep=sleep,
        )
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
```

Preserve the current constants, readiness polling, literal send, separate Enter,
pause, composer probe, second-Enter retry, and exact error strings. Do not alter
the bootstrap call site in `_build_grid`.

- [ ] **Step 4: Run all bootstrap tests**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_bootstrap_send.py tests/test_bootstrap_wiring.py -q
```

Expected: all captured-interface and bootstrap wiring tests pass.

- [ ] **Step 5: Commit the extraction**

```bash
git add src/amux/core.py tests/test_bootstrap_send.py
git commit -m "refactor(core): share safe interface submission"
```

### Task 3: Cursor-aware event delivery evidence

**Files:**
- Modify: `src/amux/events.py:311-449`
- Create: `tests/test_message_events.py`

- [ ] **Step 1: Write failing cursor and fresh-state tests**

Create `tests/test_message_events.py`:

```python
from amux import events, store


def test_cursor_ignores_events_from_an_older_pane_incarnation(db_path):
    store.add_event(90.0, "%7", "busy", db_path=db_path)
    store.add_event(110.0, "%7", "stop", db_path=db_path)
    assert events.event_cursor("%7", 100.0, db_path=db_path) == 2


def test_wait_requires_a_busy_event_newer_than_the_cursor(db_path, monkeypatch):
    store.add_event(110.0, "%7", "busy", db_path=db_path)
    cursor = events.event_cursor("%7", 100.0, db_path=db_path)
    monkeypatch.setattr(
        events,
        "pane_facts",
        lambda pane, socket=None: events.PaneFacts(alive=True, created=100.0),
    )

    def signal(socket, pane, timeout):
        store.add_event(120.0, pane, "busy", db_path=db_path)

    assert events.wait_for_fresh_state(
        "%7",
        after=cursor,
        for_states=("busy",),
        timeout=1.0,
        socket="amux-test",
        expected_created=100.0,
        db_path=db_path,
        block=signal,
    ) == "busy"


def test_recycled_pane_is_reported_dead(db_path, monkeypatch):
    monkeypatch.setattr(
        events,
        "pane_facts",
        lambda pane, socket=None: events.PaneFacts(alive=True, created=200.0),
    )
    assert events.wait_for_fresh_state(
        "%7", after=0, timeout=1.0, expected_created=100.0, db_path=db_path
    ) == "dead"
```

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_message_events.py -q
```

Expected: missing `event_cursor` and `wait_for_fresh_state` failures.

- [ ] **Step 3: Implement cursor-aware waiting**

Add these public functions to `src/amux/events.py`:

```python
def event_cursor(pane: str, boundary: float, db_path=None) -> int:
    rows = store.iter_events(pane=pane, db_path=db_path)
    return max((int(row["id"]) for row in rows if row["ts"] >= boundary), default=0)


def wait_for_fresh_state(
    pane: str,
    after: int,
    for_states: tuple[AgentState, ...] = ("busy",),
    timeout: float = 300.0,
    socket: str | None = None,
    expected_created: float | None = None,
    db_path=None,
    clock=time.monotonic,
    block=None,
) -> AgentState | None:
    socket = socket or _amux_socket()
    if socket is None:
        return None
    deadline = clock() + timeout
    wait_once = block or _wait_for_state_signal
    while True:
        facts = pane_facts(pane, socket)
        if facts.alive is False or (
            expected_created is not None and facts.created != expected_created
        ):
            return "dead"
        for row in store.iter_events(pane=pane, db_path=db_path):
            if int(row["id"]) <= after:
                continue
            if expected_created is not None and row["ts"] < expected_created:
                continue
            state = STATE_BY_KIND[row["kind"]]
            if state in for_states or state == "dead":
                return state
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        wait_once(socket, pane, remaining)
```

Extract the subprocess call currently inside `wait` into `_wait_for_state_signal(socket, pane, timeout)`. Keep existing `wait` and `cmd_wait` output and exit behavior unchanged.

- [ ] **Step 4: Run event tests and the existing event suites**

```bash
uv run pytest tests/test_message_events.py tests/test_fixtures.py tests/test_context_service_events.py -q
```

Expected: all tests pass; native and sandbox event waits retain their current contract.

- [ ] **Step 5: Commit the event slice**

```bash
git add src/amux/events.py tests/test_message_events.py
git commit -m "feat(events): wait for fresh target state"
```

### Task 4: Host-side delivery dispatcher and advisory locking

**Files:**
- Create: `src/amux/messages.py`
- Create: `tests/test_messages.py`

- [ ] **Step 1: Write failing dispatcher tests**

Create `tests/test_messages.py` using this concrete harness and state seams:

```python
from types import SimpleNamespace
import time

import pytest

import fake_tmux
from amux import core, events, messages, store


@pytest.fixture
def delivery(monkeypatch, tmp_path):
    window = fake_tmux.new_window()
    target_pane = window.new_pane()
    sender = messages.Actor("%1", 100.0, "ws", "plan", "/repo", "codex", "red-fox", "r0c0", None)
    target = messages.Actor("%2", 100.0, "ws", "review", "/repo", "claude", "blue-owl", "r0c1", None)
    actors = {"%1": sender, "%2": target}
    timeline = []
    monkeypatch.setattr(messages, "actor_for", lambda server, pane, db_path=None: actors[pane])
    monkeypatch.setattr(messages, "_pane_by_id", lambda server, pane: target_pane)
    monkeypatch.setattr(events, "current_state", lambda pane, socket=None: "idle")
    monkeypatch.setattr(events, "event_cursor", lambda *args, **kwargs: 17)
    monkeypatch.setattr(events, "wait_for_fresh_state", lambda *args, **kwargs: "busy")
    monkeypatch.setattr(core, "submit_to_interface", lambda *args, **kwargs: timeline.append("submit") or "")
    return SimpleNamespace(server=window.server, target=target_pane, timeline=timeline, state_dir=tmp_path)


def send(delivery, text="review"):
    return messages.send(
        delivery.server, "%1", "%2", text, timeout=5.0, state_dir=delivery.state_dir
    )


def test_busy_target_is_waited_for_before_any_text_is_sent(delivery, monkeypatch):
    states = iter(["busy", "idle"])
    monkeypatch.setattr(events, "current_state", lambda *args, **kwargs: next(states))
    monkeypatch.setattr(
        events,
        "wait",
        lambda *args, **kwargs: delivery.timeline.append("idle") or "idle",
    )
    result = send(delivery)
    assert result.status == "delivered"
    assert delivery.timeline == ["idle", "submit"]


def test_fresh_busy_event_marks_the_message_delivered(delivery):
    result = send(delivery)
    assert result.status == "delivered"
    assert result.message_id > 0


def test_no_fresh_busy_event_is_durably_undelivered(delivery, monkeypatch):
    monkeypatch.setattr(events, "wait_for_fresh_state", lambda *args, **kwargs: None)
    result = send(delivery)
    assert result.status == "undelivered"
    assert result.reason_code == "busy_timeout"
    assert store.message_by_id(result.message_id)["status"] == "undelivered"


def test_envelope_is_attributed_and_correlated(delivery):
    result = send(delivery, "review this")
    row = store.message_by_id(result.message_id)
    assert row["envelope"] == f"[amux red-fox @r0c0 %1 message #{result.message_id}] review this"


def test_self_send_is_rejected(delivery):
    with pytest.raises(messages.DeliveryFailure, match="itself"):
        messages.send(delivery.server, "%1", "%1", "review", state_dir=delivery.state_dir)


def test_interface_failure_is_undelivered(delivery, monkeypatch):
    monkeypatch.setattr(core, "submit_to_interface", lambda *args, **kwargs: "input line")
    result = send(delivery)
    assert (result.status, result.reason_code) == ("undelivered", "submission_failed")


def test_interruption_is_recorded_with_exit_130(delivery, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(core, "submit_to_interface", interrupt)
    result = send(delivery)
    assert (result.status, result.reason_code, result.exit_code) == (
        "undelivered", "sender_interrupted", 130
    )


def test_body_limit_is_checked_before_persistence(delivery):
    with pytest.raises(ValueError, match="4000"):
        send(delivery, "x" * 4001)


def test_two_lock_handles_for_one_target_do_not_enter_together(tmp_path):
    now = time.monotonic()
    first = messages.TargetLock(tmp_path, "amux-root", "%2", deadline=now + 10.0)
    second = messages.TargetLock(tmp_path, "amux-root", "%2", deadline=now)
    with first:
        with pytest.raises(messages.DeliveryFailure, match="another sender"):
            with second:
                raise AssertionError("second lock entered")
```

The file also contains `test_cross_scope_target_is_rejected`, parametrized with a changed workspace and changed repository; `test_dead_or_recycled_target_is_undelivered`, parametrized over `events.wait` and `events.wait_for_fresh_state` returning `dead`; `test_one_deadline_covers_idle_and_delivery`, using a monotonic clock whose sleep consumes the budget before submission; and `test_target_lock_releases_after_exception`, which raises inside one lock context and immediately acquires the same target lock again.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_messages.py -q
```

Expected: import failure for missing `amux.messages`.

- [ ] **Step 3: Implement the dispatcher**

Create `src/amux/messages.py` with these concrete public types:

```python
DEFAULT_TIMEOUT_S = 300.0
MAX_TIMEOUT_S = 3600.0
MAX_BODY_CHARS = 4000


@dataclass(frozen=True)
class Actor:
    pane: str
    created: float
    workspace: str
    task: str
    repo: str
    agent: str
    name: str
    label: str
    worktree_id: int | None


@dataclass(frozen=True)
class DeliveryResult:
    message_id: int
    status: str
    target_pane: str
    target_name: str
    reason_code: str = ""
    reason: str = ""
    exit_code: int = 0


class DeliveryFailure(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
```

Implement `TargetLock` with `os.open`, `fcntl.flock(fd, LOCK_EX | LOCK_NB)`, a short bounded poll, and unconditional unlock/close in `__exit__`. Hash `socket + NUL + pane` for the lock filename under `STATE_DIR / "message-locks"`; never interpolate a socket path into a filename.

Implement `actor_for(server, pane, db_path=None)` from `events.pane_facts`, the live pane metadata, and `store.worktree_for_pane(pane, since=facts.boundary)`. Reject missing, non-amux, or dead panes.

Implement `send(server, sender_pane, target_pane, body, timeout=DEFAULT_TIMEOUT_S, db_path=None, state_dir=None, clock=time.monotonic, wall=time.time, sleep=time.sleep)`. It must:

1. validate timeout/body and both actors;
2. reject self-send and mismatched workspace/repository;
3. expire stale rows and create a pending row;
4. render and persist `[amux {name} @{label} {pane} message #{id}] {body}`;
5. acquire `TargetLock` using the one deadline;
6. call `events.wait(target.pane, for_states=("idle", "stopped", "dead"), timeout=remaining, socket=socket)` until exactly idle;
7. revalidate the target boundary and capture `events.event_cursor`;
8. call `core.submit_to_interface` with the remaining timeout;
9. mark submitted, then call `events.wait_for_fresh_state(target.pane, after=cursor, for_states=("busy",), timeout=remaining, socket=socket, expected_created=target.created, db_path=db_path)`;
10. finish delivered only for `busy`; map every other terminal path to a specific undelivered reason;
11. catch `KeyboardInterrupt`, record `sender_interrupted`, and return exit code 130;
12. leave a pending row only when its own terminal store update fails.

Add `result_line(result)` for identical host and sandbox rendering.

- [ ] **Step 4: Run dispatcher and bootstrap regression tests**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_messages.py tests/test_message_events.py tests/test_bootstrap_send.py -q
```

Expected: all dispatcher paths and captured-interface regressions pass.

- [ ] **Step 5: Commit the dispatcher**

```bash
git add src/amux/messages.py tests/test_messages.py
git commit -m "feat(messages): deliver on idle to busy transition"
```

### Task 5: Native host CLI

**Files:**
- Modify: `src/amux/cli.py:10-21,228-360,479-664`
- Create: `tests/test_message_cli.py`

- [ ] **Step 1: Write failing native CLI tests**

Create `tests/test_message_cli.py` to drive `cli.main` with a fake server and a stubbed dispatcher:

```python
def test_send_prints_delivery_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(events, "self_pane_id", lambda: "%1")
    monkeypatch.setattr(core, "get_server", lambda socket=None: object())
    monkeypatch.setattr(
        messages,
        "send",
        lambda *a, **k: messages.DeliveryResult(42, "delivered", "%2", "blue-owl"),
    )
    assert cli.main(["send", "%2", "review", "this"]) == 0
    assert capsys.readouterr().out.strip() == "message #42 delivered to blue-owl (%2)"


def test_send_reports_undelivered_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(events, "self_pane_id", lambda: "%1")
    monkeypatch.setattr(core, "get_server", lambda socket=None: object())
    result = messages.DeliveryResult(42, "undelivered", "%2", "blue-owl", "busy_timeout", "no fresh busy event", 1)
    monkeypatch.setattr(messages, "send", lambda *a, **k: result)
    assert cli.main(["send", "%2", "review"]) == 1
    assert "undelivered" in capsys.readouterr().err
```

The same file contains `test_send_requires_sender_context`, `test_send_rejects_timeout_outside_one_to_3600_seconds`, `test_messages_passes_the_status_filter`, `test_messages_json_is_one_compact_row_per_line`, and `test_messages_uses_only_the_selected_caller_pane`. Each drives `cli.main` and asserts its exact exit code and stream.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_message_cli.py -q
```

Expected: argparse rejects unknown commands `send` and `messages`.

- [ ] **Step 3: Add native commands**

Import `messages` in `src/amux/cli.py`. Add `_cmd_send` and `_cmd_messages`. `_cmd_send` resolves the sender with `events.self_pane_id()`, invokes `messages.send`, prints delivered output to stdout and undelivered output to stderr, and returns `result.exit_code`. `_cmd_messages` expires and lists only the current pane's visible rows; JSON output is one compact object per line.

Register:

```python
p_send = sub.add_parser("send", help="send a message and confirm target processing")
p_send.add_argument("target", help="target pane id, e.g. %%42")
p_send.add_argument("text", nargs="+", help="message body")
p_send.add_argument("--timeout", type=float, default=messages.DEFAULT_TIMEOUT_S)
p_send.set_defaults(func=_cmd_send)

p_messages = sub.add_parser("messages", help="list sent and received messages")
p_messages.add_argument("-n", type=int, default=20, help="max messages")
p_messages.add_argument("--status", choices=store.MESSAGE_STATUSES, default=None)
p_messages.add_argument("--json", action="store_true", help="JSONL output")
p_messages.add_argument("--pane", default=None, help=argparse.SUPPRESS)
p_messages.set_defaults(func=_cmd_messages)
```

- [ ] **Step 4: Run native CLI tests**

```bash
uv run pytest tests/test_message_cli.py tests/test_cli_runtime.py -q
```

Expected: all new commands and existing runtime parsing pass.

- [ ] **Step 5: Commit native CLI support**

```bash
git add src/amux/cli.py tests/test_message_cli.py
git commit -m "feat(cli): add reliable send commands"
```

### Task 6: Authenticated context-service messaging

**Files:**
- Modify: `src/amux/context_service.py:136-223,536-692,743-888`
- Create: `tests/test_context_service_messages.py`
- Modify: `tests/test_context_service_auth.py`

- [ ] **Step 1: Write failing route and permission tests**

Create `tests/test_context_service_messages.py` using the existing `ContextService.handle` request harness with these route tests:

```python
def test_post_message_derives_sender_from_capability(service, request, monkeypatch):
    seen = {}
    def deliver(server, sender, target, body, **options):
        seen.update(sender=sender, target=target, body=body)
        return messages.DeliveryResult(42, "delivered", target, "blue-owl")
    monkeypatch.setattr(messages, "send", deliver)
    status, payload = service.handle(request("POST", "/v1/messages", body={
        "target": "%2", "text": "review", "timeout": 30.0
    }))
    assert status == 200
    assert seen == {"sender": "%1", "target": "%2", "body": "review"}
    assert payload["message"]["status"] == "delivered"


def test_message_send_requires_messages_write(service, request):
    request.identity = replace(request.identity, permissions=frozenset({"context:read"}))
    with pytest.raises(context_service.ServiceError, match="messages:write"):
        service.handle(request)
```

Add cases for target/text/timeout validation, undelivered returning HTTP 200 with durable status, same-workspace/repository enforcement by the dispatcher, GET visibility, result limits, and redacted error envelopes.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_context_service_messages.py tests/test_context_service_auth.py -q
```

Expected: unknown route and missing `PERM_MESSAGES_WRITE` failures.

- [ ] **Step 3: Implement permission and routes**

In `context_service.py` add:

```python
PERM_MESSAGES_WRITE = "messages:write"
AGENT_PERMISSIONS = (
    PERM_CONTEXT_READ,
    PERM_NOTES_WRITE,
    PERM_EVENTS_WRITE,
    PERM_MESSAGES_WRITE,
)
```

Add `_float_field(body, name, default, minimum, maximum)` with the same finite-number and range behavior as `_float_param`. Add `POST /v1/messages` requiring `messages:write`; derive `sender_pane` from `request.caller`, pass `service.tmux_server(caller.socket)`, `service.db_path`, and `service.config.state_home` to the shared dispatcher, and return its terminal record and summary. Add `GET /v1/messages` requiring `context:read`, filtering through `store.visible_messages` for the capability's workspace, repo, and pane.

- [ ] **Step 4: Run service tests**

```bash
uv run pytest tests/test_context_service_messages.py tests/test_context_service_auth.py tests/test_context_service.py -q
```

Expected: permission vocabulary, route validation, and existing service behavior pass.

- [ ] **Step 5: Commit service support**

```bash
git add src/amux/context_service.py tests/test_context_service_messages.py tests/test_context_service_auth.py
git commit -m "feat(context): bridge sandbox messages"
```

### Task 7: Docker-sandbox client parity

**Files:**
- Modify: `src/amux/sandbox_client.py:1-55,284-499`
- Create: `tests/test_sandbox_client_messages.py`
- Modify: `tests/test_sandbox_client.py`

- [ ] **Step 1: Write failing sandbox client tests**

Create `tests/test_sandbox_client_messages.py` with the existing fake HTTP service:

```python
def test_sandbox_send_posts_the_target_text_and_timeout(run, fake_service, capsys):
    fake_service.respond("POST", "/v1/messages", {
        "message": {"id": 42, "status": "delivered"},
        "summary": "message #42 delivered to blue-owl (%2)",
    })
    assert run("send", "%2", "review", "this", "--timeout", "30") == 0
    assert fake_service.last_json == {"target": "%2", "text": "review this", "timeout": 30.0}
    assert "delivered" in capsys.readouterr().out


def test_sandbox_send_returns_one_for_durable_undelivered(run, fake_service, capsys):
    fake_service.respond("POST", "/v1/messages", {
        "message": {"id": 42, "status": "undelivered"},
        "summary": "message #42 undelivered to blue-owl (%2): no fresh busy event",
    })
    assert run("send", "%2", "review") == 1
    assert "undelivered" in capsys.readouterr().err
```

The file also contains `test_send_extends_the_http_timeout_by_poll_slack`, `test_messages_prints_human_rows`, `test_messages_prints_jsonl`, `test_messages_forwards_status_and_limit`, and `test_boundary_text_lists_send_and_messages`, each asserting the fake service's recorded method, path, body/query, and the command's output stream.

- [ ] **Step 2: Run and verify RED**

```bash
uv run pytest tests/test_sandbox_client_messages.py -q
```

Expected: sandbox argparse rejects `send` and `messages`.

- [ ] **Step 3: Add matching sandbox commands**

Add `cmd_send` and `cmd_messages` to `sandbox_client.py`. `cmd_send` posts to `/v1/messages` with an HTTP timeout of `args.timeout + POLL_SLACK_S`, prints delivered summaries to stdout and undelivered summaries to stderr, and returns 0/1 from the response status. `cmd_messages` GETs `/v1/messages` with `status` and `limit`, then renders the same concise columns or JSONL. Update `SUPPORTED` and the parser with the same flags and defaults as the host CLI.

- [ ] **Step 4: Run all sandbox client tests**

```bash
uv run pytest tests/test_sandbox_client_messages.py tests/test_sandbox_client.py tests/test_sandbox_client_exchange.py -q
```

Expected: all sandbox client and live fake-service exchange tests pass.

- [ ] **Step 5: Commit sandbox parity**

```bash
git add src/amux/sandbox_client.py tests/test_sandbox_client_messages.py tests/test_sandbox_client.py
git commit -m "feat(sandbox): expose reliable messaging"
```

### Task 8: Documentation, live dogfood, rapid-whale review, and final verification

**Files:**
- Modify: `README.md:1-220`
- Modify: `skills/amux/SKILL.md:20-610`
- Modify: implementation files identified by rapid-whale only when simplification preserves tests and the approved delivery contract.

- [ ] **Step 1: Update public documentation**

Mark the README messaging goal complete, add `amux send` and `amux messages` usage, document the idle-before-send and fresh-busy guarantee, and explain conservative false-undelivered results when hooks emit no busy event.

In `skills/amux/SKILL.md`:

- add both commands to Quick reference;
- list them as supported in a sandbox;
- update the capability vocabulary from three to four permissions;
- replace the raw `tmux send-keys` procedure with `amux send`;
- retain notes-versus-messages guidance;
- explain timeout/undelivered output and `amux messages` recovery;
- remove common mistakes that claim no messaging command exists.

- [ ] **Step 2: Run documentation and focused tests before live messaging**

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 uv run pytest \
  tests/test_message_store.py \
  tests/test_message_events.py \
  tests/test_messages.py \
  tests/test_message_cli.py \
  tests/test_context_service_messages.py \
  tests/test_sandbox_client_messages.py \
  tests/test_bootstrap_send.py -q
```

Expected: `git diff --check` exits 0 and all focused tests pass.

- [ ] **Step 3: Commit the reviewable implementation**

```bash
git add README.md skills/amux/SKILL.md
git commit -m "docs: teach reliable agent messaging"
```

- [ ] **Step 4: Dogfood `amux send` to request rapid-whale's review**

Confirm rapid-whale is idle with `amux ctx`, then run the implementation from this worktree:

```bash
uv run amux send %1 "Please review and simplify crimson-falcon's reliable messaging implementation. Inspect branch amux/amux/message/crimson-falcon and reply to %0 with concrete findings; preserve idle-before-send and fresh idle-to-busy delivery evidence." --timeout 300
```

Expected: the command prints `delivered` only after rapid-whale moves idle to busy. If it prints undelivered, report that exact result and use `amux messages --status undelivered` rather than raw tmux send-keys.

- [ ] **Step 5: Apply and verify rapid-whale's simplifications**

For each finding, inspect the cited code, apply only reductions that preserve the approved spec, and run the narrowest affected test file immediately. Record rejected suggestions with the concrete invariant they would weaken. Commit accepted changes:

```bash
git add src tests README.md skills/amux/SKILL.md
git commit -m "refactor(messages): apply review simplifications"
```

If the review requires no changes, do not create an empty commit.

- [ ] **Step 6: Run complete fresh verification**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
git diff --check
make build
git status --short --branch
```

Expected: the full suite has zero failures, diff check and build exit 0, and the worktree is clean.

- [ ] **Step 7: Review the final diff against the approved spec**

```bash
git diff b2346d2..HEAD --stat
git log --oneline b2346d2..HEAD
```

Check each design requirement against code or a named test: durable terminal status, per-target serialization, never send before idle, fresh busy cursor, same-scope authorization, sandbox parity, sender-visible failure, stale pending expiry, and no raw send-keys guidance.
