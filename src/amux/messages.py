from __future__ import annotations

import fcntl
import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from amux import core, events, store, worktree
from amux.shared import DEFAULT_SOCKET, STATE_DIR

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


class TargetLock:
    """Serialize senders for one target pane without requiring a daemon."""

    def __init__(
        self,
        state_dir: Path,
        socket: str,
        pane: str,
        *,
        deadline: float,
        clock=time.monotonic,
        sleep=time.sleep,
        poll: float = 0.05,
    ) -> None:
        digest = hashlib.sha256(f"{socket}\0{pane}".encode()).hexdigest()[:24]
        self.path = state_dir / "message-locks" / f"{digest}.lock"
        self.deadline = deadline
        self.clock = clock
        self.sleep = sleep
        self.poll = poll
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._fd = fd
                    return self
                except BlockingIOError:
                    remaining = self.deadline - self.clock()
                    if remaining <= 0:
                        raise DeliveryFailure(
                            "lock_timeout",
                            "another sender is already delivering to the target",
                        )
                    self.sleep(min(self.poll, remaining))
        except BaseException:
            os.close(fd)
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def actor_for(server, pane: str, db_path: Path | None = None) -> Actor:
    socket = getattr(server, "socket_name", None) or DEFAULT_SOCKET
    facts = events.pane_facts(pane, socket)
    if facts.alive is not True:
        raise DeliveryFailure("target_dead", f"pane {pane} is not alive")
    if facts.kind != "amux" or facts.created is None:
        raise DeliveryFailure("not_agent", f"pane {pane} is not an amux agent")
    row = store.worktree_for_pane(pane, since=facts.boundary, db_path=db_path)
    repo = row["repo"] if row else worktree.repo_root(facts.cwd) or ""
    return Actor(
        pane=pane,
        created=facts.created,
        workspace=facts.workspace,
        task=facts.task,
        repo=repo,
        agent=facts.agent,
        name=facts.name or pane,
        label=facts.label,
        worktree_id=int(row["id"]) if row else None,
    )


def _pane_by_id(server, pane: str):
    for session in server.sessions:
        for window in session.windows:
            for candidate in window.panes:
                if candidate.id == pane:
                    return candidate
    raise DeliveryFailure("target_dead", f"pane {pane} is not alive")


def _remaining(deadline: float, clock, code: str, reason: str) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise DeliveryFailure(code, reason)
    return remaining


def _wait_until_idle(
    target: Actor,
    socket: str,
    deadline: float,
    clock,
) -> None:
    while True:
        state = events.current_state(target.pane, socket)
        if state == "idle":
            return
        if state in ("dead", "stopped") or state is None:
            raise DeliveryFailure(
                "target_dead", "target stopped or disappeared before submission"
            )
        remaining = _remaining(
            deadline,
            clock,
            "idle_timeout",
            "target did not become idle before the delivery deadline",
        )
        state = events.wait(
            target.pane,
            for_states=("idle", "stopped", "dead"),
            timeout=remaining,
            socket=socket,
        )
        if state is None:
            raise DeliveryFailure(
                "idle_timeout",
                "target did not become idle before the delivery deadline",
            )
        if state == "idle":
            return
        raise DeliveryFailure(
            "target_dead", "target stopped or disappeared before submission"
        )


def send(
    server,
    sender_pane: str,
    target_pane: str,
    body: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    state_dir: Path | None = None,
    db_path: Path | None = None,
    clock=time.monotonic,
    wall=time.time,
    sleep=time.sleep,
) -> DeliveryResult:
    if not 0 < timeout <= MAX_TIMEOUT_S:
        raise ValueError(f"timeout must be between 0 and {MAX_TIMEOUT_S:g} seconds")
    if not body.strip():
        raise ValueError("message body cannot be empty")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(f"message body cannot exceed {MAX_BODY_CHARS} characters")

    sender = actor_for(server, sender_pane, db_path)
    target = actor_for(server, target_pane, db_path)
    if sender.pane == target.pane:
        raise DeliveryFailure("self_send", "an agent cannot send a message to itself")
    if (sender.workspace, sender.repo) != (target.workspace, target.repo):
        raise DeliveryFailure(
            "scope_mismatch",
            "sender and target must share the same workspace and repository",
        )

    started = clock()
    deadline = started + timeout
    created = wall()
    store.expire_messages(created, db_path)
    message_id = store.create_message(
        created_ts=created,
        deadline_ts=created + timeout,
        sender_worktree_id=sender.worktree_id,
        target_worktree_id=target.worktree_id,
        repo=sender.repo,
        workspace=sender.workspace,
        sender_task=sender.task,
        sender_pane=sender.pane,
        sender_agent=sender.agent,
        sender_name=sender.name,
        target_task=target.task,
        target_pane=target.pane,
        target_agent=target.agent,
        target_name=target.name,
        target_created_ts=target.created,
        body=body,
        db_path=db_path,
    )
    envelope = (
        f"[amux {sender.name} @{sender.label} {sender.pane} message #{message_id}] "
        f"{body}"
    )
    store.set_message_envelope(message_id, envelope, db_path)

    def failed(code: str, reason: str, exit_code: int = 1) -> DeliveryResult:
        store.finish_message(
            message_id, "undelivered", code, reason, wall(), db_path
        )
        return DeliveryResult(
            message_id,
            "undelivered",
            target.pane,
            target.name,
            code,
            reason,
            exit_code,
        )

    try:
        socket = getattr(server, "socket_name", None) or DEFAULT_SOCKET
        with TargetLock(
            state_dir or STATE_DIR,
            socket,
            target.pane,
            deadline=deadline,
            clock=clock,
            sleep=sleep,
        ):
            _wait_until_idle(target, socket, deadline, clock)
            current_target = actor_for(server, target.pane, db_path)
            if current_target.created != target.created:
                raise DeliveryFailure(
                    "target_replaced", "target pane was replaced before submission"
                )
            if (current_target.workspace, current_target.repo) != (
                target.workspace,
                target.repo,
            ):
                raise DeliveryFailure(
                    "target_replaced", "target identity changed before submission"
                )
            _wait_until_idle(target, socket, deadline, clock)
            cursor = events.event_cursor(target.pane, target.created, db_path)
            pane = _pane_by_id(server, target.pane)
            problem = core.submit_to_interface(
                pane,
                envelope,
                timeout=_remaining(
                    deadline,
                    clock,
                    "submission_timeout",
                    "delivery deadline expired before submission",
                ),
                poll=0.05,
                pause=0.4,
                clock=clock,
                sleep=sleep,
            )
            if problem:
                raise DeliveryFailure("submission_failed", problem)
            store.mark_message_submitted(message_id, wall(), db_path)
            state = events.wait_for_fresh_state(
                target.pane,
                cursor,
                for_states=("busy",),
                timeout=_remaining(
                    deadline,
                    clock,
                    "busy_timeout",
                    "no fresh busy event before the delivery deadline",
                ),
                socket=socket,
                expected_created=target.created,
                db_path=db_path,
                clock=clock,
            )
            if state == "dead":
                raise DeliveryFailure(
                    "target_dead", "target stopped or disappeared after submission"
                )
            if state != "busy":
                raise DeliveryFailure(
                    "busy_timeout", "no fresh busy event before the delivery deadline"
                )
            store.finish_message(message_id, "delivered", now=wall(), db_path=db_path)
            return DeliveryResult(
                message_id, "delivered", target.pane, target.name
            )
    except KeyboardInterrupt:
        return failed(
            "sender_interrupted", "sender interrupted delivery before confirmation", 130
        )
    except DeliveryFailure as exc:
        return failed(exc.code, str(exc))
    except Exception as exc:  # noqa: BLE001 - every persisted attempt must terminate
        return failed("transport_error", str(exc))


def result_line(result: DeliveryResult) -> str:
    line = (
        f"message #{result.message_id} {result.status} to "
        f"{result.target_name} ({result.target_pane})"
    )
    return f"{line}: {result.reason}" if result.reason else line
