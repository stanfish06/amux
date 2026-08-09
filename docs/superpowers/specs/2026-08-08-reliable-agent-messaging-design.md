# Reliable Agent Messaging Design

## Goal

Add first-class agent-to-agent messaging to amux. A sender must never type into
a busy agent. A message is delivered only when the target was idle before
submission and emits a new `busy` event after submission; otherwise amux records
the message as undelivered and returns a failure to the sender.

The contract applies equally to host and Docker-sandbox agents.

## User interface

The host CLI and sandbox client expose the same commands:

```text
amux send <pane> <text...> [--timeout SECONDS]
amux messages [-n COUNT] [--status STATUS] [--json]
```

`<pane>` is the explicit tmux pane address shown by `amux ctx`, such as `%7`.
Using one unambiguous address avoids name and grid-label collisions across
tasks. The default timeout is 300 seconds and covers the complete transaction,
including waiting behind another sender, waiting for the target to become idle,
submitting the text, and waiting for the delivery transition.

Successful output names the durable message ID and target:

```text
message #42 delivered to rapid-whale (%7)
```

Failure output is sent to stderr, names the same ID and target, explains the
phase that failed, and exits nonzero:

```text
message #42 undelivered to rapid-whale (%7): no idle -> busy transition within 300s
```

`amux messages` shows messages where the caller is either sender or target. It
provides durable visibility after the original `send` process exits. Human host
invocations outside an agent pane must use `--pane` to select the caller whose
messages are being inspected; this hidden compatibility flag is not available
inside a sandbox because the capability fixes the caller's identity.

## Message envelope and attribution

Amux constructs the text typed into the target. Callers supply only the body.
The envelope is:

```text
[amux <sender-name> @<sender-label> <sender-pane> message #<id>] <body>
```

The sender identity is resolved from the live pane and registry rather than
trusted from command-line or HTTP fields. The message ID gives the receiver and
sender one durable correlation key. Message bodies are limited to 4,000
characters, matching the existing context-service text limit.

## Durable model

Schema version 5 adds a `messages` table. Each row stores:

- creation, update, deadline, submission, and delivery timestamps;
- nullable sender and target worktree IDs;
- repository and workspace scope;
- sender and target task, pane, agent kind, and stable agent name;
- the target pane creation timestamp, which binds delivery to one pane
  incarnation even if tmux later recycles the pane ID;
- the original body and rendered envelope;
- status: `pending`, `delivered`, or `undelivered`;
- a machine-readable reason code and human-readable reason.

Indexes support listing by sender, target, workspace, status, and descending ID.
Status updates are conditional: only a pending row can become delivered or
undelivered. This makes cleanup and competing error paths idempotent.

Every query or new send first expires pending rows whose deadline has passed.
They become undelivered with reason `deadline_expired`. This recovers records
left pending by `SIGKILL`, process crashes, or host restarts without adding a
background daemon.

## Target scope and authorization

The sender and target must both be live amux panes on the same amux tmux socket,
in the same workspace and repository. Messaging across tasks in that workspace
is allowed. Self-messaging is rejected because it cannot satisfy the sender's
own synchronous delivery wait safely.

Sandbox capability tokens gain `messages:write`; all currently minted agent
tokens receive it alongside the existing three permissions. The new
`POST /v1/messages` route derives the sender from the token, validates the
target against the host roster and registry, and runs the same host-side
dispatcher used by the native CLI. `GET /v1/messages` uses `context:read` and
returns only rows where the caller is sender or target within its bound
workspace and repository.

This expands the sandbox vocabulary only with messaging. It does not expose
the tmux socket, database, arbitrary keystrokes, spawning, killing, integration,
or any other host-control operation to the sandbox.

## Delivery state machine

One dispatcher owns the following state machine for both runtimes:

1. Resolve and validate the sender and target, including the target pane
   incarnation.
2. Persist a `pending` row and construct its attributed envelope.
3. Acquire a host-side advisory file lock keyed by tmux socket and target pane.
   The lock serializes separate host processes and sandbox-service threads so
   two amux senders cannot both observe the same idle composer and type over
   each other. Closing the lock file or terminating the process releases it, so
   a killed sender cannot strand the target behind a permanent lock.
4. Wait until the target's resolved state is exactly `idle`. Amux never types
   while the target is `busy`, `needs-input`, `starting`, `stopped`, or dead.
5. Revalidate that the pane is alive, belongs to the original incarnation, and
   remains idle. Capture the latest target event ID as the delivery cursor.
6. Use the existing capture-based interface readiness and literal text/Enter
   submission behavior. Text and Enter are separate keystrokes with the
   existing pause; if the envelope remains in the composer, retry Enter, never
   the text.
7. Wait for a target event newer than the captured cursor. Mark delivered only
   for a new `busy` event from the same pane incarnation. Stale busy state from
   before submission cannot satisfy delivery.
8. Record the terminal status, release the target lock in `finally`, print the
   result, and return the corresponding exit code.

The one deadline is checked before every blocking operation and passed as the
remaining timeout. No phase can silently extend the caller's requested bound.
The maximum accepted timeout is 3,600 seconds.

The existing bootstrap sender becomes a thin wrapper around the shared
interface-submission helper. Bootstrap behavior and its fail-soft grid-creation
contract remain unchanged.

## Failure behavior

The dispatcher records `undelivered` for:

- timeout acquiring the per-target lock;
- timeout waiting for idle;
- target death, stop, disappearance, scope change, or pane-ID reuse;
- an interface that never becomes ready;
- text or Enter not being accepted by the composer;
- timeout without a fresh `busy` event after submission;
- interruption or any handled transport/database failure after row creation.

If persistence fails before a row exists, amux cannot honestly assign an ID or
status and reports the store failure directly. If the message was submitted but
the status update fails, the row remains pending until deadline expiration; it
is never guessed delivered.

`KeyboardInterrupt` records `sender_interrupted`, releases the target lock, and
returns exit 130. Hard termination is handled later by deadline expiration.

Delivery is intentionally evidence-limited. An agent that processes a prompt
without emitting a `busy` event is reported undelivered even if text reached its
interface. This is the conservative result required by the selected
idle-to-busy contract.

## Components

- `src/amux/messages.py` owns target validation, envelope rendering, locking,
  the deadline-driven state machine, advisory target locks, and result types.
- `src/amux/store.py` owns schema migration and conditional message writes,
  expiry, and scoped queries.
- `src/amux/events.py` exposes cursor-aware waiting for a fresh event without
  changing the behavior of `amux event wait`.
- `src/amux/core.py` exposes the existing interface-submission behavior for
  reuse while retaining `send_bootstrap` as the fail-soft wrapper.
- `src/amux/cli.py` adds native `send` and `messages` commands.
- `src/amux/context_service.py` adds permission checks and the host-side HTTP
  routes.
- `src/amux/sandbox_client.py` adds matching sandbox commands and request
  timeout handling.
- `README.md` and `skills/amux/SKILL.md` replace the raw `tmux send-keys`
  workaround with the first-class messaging contract and its limits.

## Verification strategy

Implementation follows test-driven development. Each behavior is first observed
failing for the expected missing-feature reason.

Focused tests cover:

- schema creation, migration from version 4, conditional terminal updates,
  scoped listing, and stale-pending expiry;
- automatic attribution and rejection of spoofed or cross-scope targets;
- waiting behind a busy target without sending keystrokes;
- per-target serialization of concurrent senders;
- fresh `idle -> busy` delivery and rejection of a stale busy event;
- target death, pane reincarnation, needs-input, submission failure, phase
  timeouts, interruption, and deadline accounting;
- preservation of every existing bootstrap readiness/submission fixture;
- native CLI output and exit codes;
- sandbox permission, route, error-envelope, timeout, and CLI parity tests.

After focused tests and the full Python suite pass, the new command is tested
live by using it to ask `rapid-whale` for a code review and simplification pass.
Their findings are applied where they reduce complexity without weakening the
delivery contract. The final checks are the focused suite, full suite, build,
and `git diff --check`.

## Non-goals

This change does not add automatic retries, a background delivery daemon,
broadcasts, message priorities, acknowledgements generated by model text,
cross-workspace messaging, encryption beyond the existing local capability and
filesystem boundaries, or a replacement transport for tmux. Those can be
considered after the synchronous delivery evidence is measured in real use.
