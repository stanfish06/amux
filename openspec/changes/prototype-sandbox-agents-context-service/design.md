## Context

Amux currently has one host-side control plane: a dedicated tmux server, a SQLite context store, and Git worktrees under `$XDG_STATE_HOME/amux`. `_build_grid` creates pane identity, sets tmux metadata, creates per-agent worktrees, launches host agent commands, and records events. Notes and events use the append-only worktree row as their durable identity because tmux pane IDs are recycled.

Docker Sandboxes changes the execution boundary. The agent runs inside a microVM that cannot see the host tmux socket, user-level agent configuration, or amux state. Docker clone mode is the appropriate Git boundary for an autonomous agent, but it cannot be launched from an existing secondary Git worktree. A sandbox therefore needs its own clone and branch, while amux must keep integration and coordination on the host.

The prototype targets supported local Docker Sandbox installations and the existing single-host amux topology. Docker's `sbx` CLI remains an optional external tool. SQLite remains authoritative; the design does not introduce PostgreSQL, Redis, database replication, or a shared database mount.

## Goals / Non-Goals

**Goals:**

- Prove that a Claude or Codex pane can attach to a resource-capped Docker Sandbox while amux retains workspace, task, agent, state, note, and integration semantics.
- Preserve Git-capable isolation by using one clone-mode sandbox and one named branch per agent.
- Give sandboxed agents scope-correct access to structured amux context without exposing the host database, state directory, or tmux socket.
- Keep host execution unchanged by default and make every sandbox prerequisite and policy change explicit.
- Make lifecycle, rollback, and cleanup safe enough to evaluate the backend with real work.
- Produce deterministic automated coverage with a fake `sbx` executable plus a documented live smoke test.

**Non-Goals:**

- Making Docker Sandboxes mandatory or changing the default runtime.
- Supporting arbitrary raw commands or every agent supported by `sbx` in the prototype.
- Sharing complete model prompt histories between agents.
- Allowing sandbox clients to spawn, kill, integrate, or otherwise control host resources.
- Migrating all native host commands behind a permanent daemon in this change.
- Solving remote or multi-host orchestration, high availability, or organization-wide Docker governance.
- Treating Docker's experimental shared skills store as trusted by default.

## Decisions

### 1. Add an execution-runtime seam and keep it grid-scoped

`spw` and `spg` gain `--runtime {host,docker-sandbox}` plus sandbox resource options. A grid has one runtime but may retain its existing Claude/Codex composition. `host` delegates to today's launch and worktree behavior. `docker-sandbox` delegates to a new runtime adapter responsible for preflight, creation, bootstrap, attachment, inspection, stop, cleanup, and branch fetch.

The implementation should introduce a small runtime interface rather than place `sbx` branches throughout `core.py`. The initial modules are expected to be:

- `src/amux/runtime.py`: runtime configuration, protocol, and host adapter boundary.
- `src/amux/sandbox.py`: `sbx` subprocess adapter, naming, preflight, lifecycle, and Git remote behavior.
- `src/amux/core.py`: pane/grid orchestration that consumes prepared runtime launches.
- `src/amux/cli.py`: runtime flags, resource validation, doctor output, and lifecycle dispatch.

The runtime is grid-scoped for a narrow first change. Per-pane runtime syntax was considered, but it complicates rollback, resource flags, output, and task cleanup before the backend has proven useful.

### 2. Use clone-mode sandboxes rather than mounting host worktrees

For each agent, amux creates a named sandbox from the repository's primary checkout using the equivalent of:

```text
sbx create --clone --name <sandbox> --cpus <n> --memory <size> --no-share-skills <agent> <repo>
```

Amux then uses `sbx exec` to create `amux/<workspace>/<task>/<agent-name>` from the task base, install the thin context client and sandbox-local hooks, and place the scoped client configuration. The host tmux pane attaches with `sbx run --name <sandbox>`.

Sandbox names are derived from a sanitized workspace, task, and stable agent name plus a short repository hash so independently named repositories cannot collide. Amux records the exact resulting `sbx` sandbox ID and does not reconstruct identity from a name alone.

Directly mounting an amux host worktree was rejected because the sandbox cannot resolve the worktree's external `.git` metadata and the agent loses normal Git operations. Mounting the main repository or `.git` metadata read-write would weaken the intended boundary. A per-agent mailbox can be useful as a minimal experiment, but it would duplicate context transport and does not solve Git.

### 3. Split task integration setup from per-agent workspace setup

`worktree.setup_task` currently creates both the task integration worktree and every agent worktree. It will be split into:

- task integration preparation, shared by both runtimes;
- host-agent worktree preparation, used only by `host`;
- sandbox execution registration, used only by `docker-sandbox`.

Sandbox integration fetches the assigned branch through Docker's `sandbox-<name>` host remote into a namespaced remote-tracking ref, then merges that fetched commit into the existing task integration worktree with today's `--no-ff`, conflict-abort, blocker-note, and short-stat behavior. Integration never imports uncommitted files.

Before `sbx rm`, cleanup checks the sandbox working tree, refuses dirty removal without an explicit force flag, fetches the committed branch tip into a durable local ref, removes the sandbox, revokes its token, and marks the registry row removed. `kg` and `kw` without cleanup call `sbx stop` and preserve reattachment state.

### 4. Extend the existing registry additively for the prototype

The existing `worktrees` row remains the durable identity referenced by notes and events. Schema version 3 adds columns with backward-compatible defaults:

- `runtime` (`host` by default or `docker-sandbox`);
- `runtime_status` for created/running/stopped/failed lifecycle state;
- `sandbox_name` and `sandbox_id`;
- `socket_name` so the service can update the correct tmux server.

Sandbox rows use an empty host path and their assigned branch/repository values; runtime-aware code must not call host-path operations for them. A new `context_tokens` table stores a SHA-256 hash of a randomly generated high-entropy token, its worktree identity, permissions, expiry, creation time, and revocation time.

Renaming `worktrees` to a generic executions table was considered and rejected for the prototype because it would require migrating every foreign key and query before validating the runtime. The additive columns make an old binary tolerant of the migrated database and keep rollback practical. If sandbox execution becomes permanent, a later change can normalize the name and model.

### 5. Keep SQLite on the host and expose a bounded HTTP context service

A new `src/amux/context_service.py` provides a small standard-library HTTP service bound only to `127.0.0.1` on a configurable stable port. Docker routes `http://host.docker.internal:<port>` to this loopback service when the user explicitly allows `localhost:<port>` in sandbox policy. Amux checks that rule but does not modify global Docker policy.

The service reuses existing `store`, visibility, context-building, event-resolution, and tmux-update functions. It is the only sandbox-facing context path; native host commands continue their current local calls during the prototype. This avoids a second database and keeps the default runtime independent of a daemon. Moving every native call behind a Unix-socket daemon is a possible follow-up, not a prerequisite for evaluating sandbox execution.

The versioned interface is deliberately small:

| Operation | Purpose |
|---|---|
| `GET /healthz` | Non-sensitive liveness and schema compatibility |
| `GET /v1/context` | Caller identity, scoped roster, visible notes, and runtime metadata |
| `GET /v1/notes` | Cursor- and limit-bounded visible-note retrieval |
| `POST /v1/notes` | Create a validated note within the caller's allowed scope |
| `POST /v1/events` | Append a caller-attributed state event and signal the host pane |
| `GET /v1/events/state` | Return scope-visible resolved agent states |
| `GET /v1/events/wait` | Bounded long-poll after an event cursor |

`ThreadingHTTPServer` is sufficient for the prototype and avoids adding an application-server dependency. Every handler enforces JSON content types, body and field limits, result limits, a maximum long-poll duration, stable error codes, and redacted logs. Long-poll handlers periodically re-query SQLite as well as responding to in-process notifications so native host writes remain observable.

FastAPI was considered, but a new web stack is unnecessary for this bounded local prototype. Direct SQLite mounts were rejected because they expose unrelated context, allow forgery or corruption, couple clients to schema and WAL behavior, and place filesystem passthrough locking inside the correctness boundary.

### 6. Authenticate with per-agent capabilities and derive identity on the host

Amux creates a token with `secrets.token_urlsafe`, stores only its SHA-256 hash, and binds it to the sandbox execution row and an explicit permission set. Authentication comparisons use constant-time comparison. The host derives workspace, task, repository, pane, agent, name, runtime, and visibility from that row. Identity fields in request bodies are rejected rather than trusted.

The plaintext token and endpoint are copied through a mode-`0600` temporary configuration file into the sandbox and removed from the host staging path immediately after bootstrap. They are not printed or placed in subprocess arguments. The token is readable by the sandbox agent because the agent is the principal, but it grants only context operations for that identity and is revoked on sandbox removal.

The service does not expose raw SQL, filesystem paths, general tmux commands, shell execution, lifecycle actions, or arbitrary target scopes. Requests can affect only the caller's event record, allowed note scopes, and same-scope waits.

### 7. Install a self-contained sandbox client and sandbox-local hooks

`src/amux/sandbox_client.py` is a Python-standard-library client that can be copied as a single executable `amux` shim into the sandbox. It reads its endpoint and token from the protected configuration file and supports the context-only command subset:

- `amux ctx [--json]`;
- `amux notes ... [--json]`;
- `amux note ...`;
- `amux event emit|state|wait ...`.

Host-control commands fail locally with a clear boundary message. Output retains the established fields and human-readable shape so project instructions and the amux skill remain useful.

Bootstrap installs sandbox-local Claude or Codex hooks that call the shim for busy, stop, notification, and exit events. It merges only the required hook entries into the sandbox home and does not mount or copy the user's complete host configuration. Agent-image hook locations and formats must be inspected during implementation and covered with fixtures because they are a Docker/agent compatibility surface.

### 8. Treat sandbox creation as a rollback-capable transaction

Preflight runs before tmux or Git mutation. Creation then records every acquired resource—task integration worktree, registry row, token, sandbox, remote, and pane—in order. A failure unwinds successfully created resources in reverse order, marks durable rows failed or removed, revokes tokens, and reports cleanup failures alongside the originating error.

The context service has an explicit foreground command for debugging and an idempotent host-side start/status path used by sandbox preflight. Its PID, port, and redacted log live under the amux state directory. A stale PID or occupied port fails clearly; the system never falls back to an unauthenticated listener or a mounted database.

## Risks / Trade-offs

- **Docker Sandbox CLI and agent-image behavior are evolving** → Isolate every command and parsed response behind `sandbox.py`, pin tested behavior in fixtures, report the detected `sbx` version, and keep the backend opt-in.
- **One microVM and private Docker cache per agent consumes substantial CPU, memory, and disk** → Require explicit caps with conservative defaults, surface resource settings, and include four-agent measurements in the smoke test.
- **A stable localhost port requires a Docker policy exception and can collide** → Make the port configurable, preflight both the bind and policy, and print an exact remediation command without modifying policy.
- **Sandbox-local Claude/Codex hook formats may drift** → Keep bootstrap adapters agent-specific, test generated configuration, and fail with a visible degraded-integration error rather than silently claiming accurate state.
- **A compromised agent can read and exfiltrate its own context token** → Limit the token to one identity and context-only methods, bind the service to loopback, cap requests, revoke on removal, and never let the token authorize host control.
- **Removing a sandbox can destroy uncommitted work** → Refuse dirty cleanup by default, preserve committed tips locally first, and require a separately explicit force action.
- **Native writers and the context service concurrently use SQLite** → Retain short WAL transactions and busy timeouts, reuse current store functions, and add concurrency tests before considering a single-owner daemon.
- **Schema terminology remains worktree-centric for sandbox rows** → Keep the additive prototype narrow and record a follow-up decision point after the runtime is measured.

## Migration Plan

1. Add schema version 3 as an atomic additive migration and tests proving existing version 2 rows acquire `host` defaults without data loss.
2. Introduce the runtime seam with the host adapter and run the existing command flows to confirm no default behavior changes.
3. Add the context service, token store, and thin client with isolated database and fake-tmux tests.
4. Add the fake-`sbx` runtime, sandbox Git integration, transactional rollback, and lifecycle tests.
5. Add the explicit CLI flags, doctor/preflight output, documentation, and live smoke-test procedure.
6. Run the manual prototype on a disposable repository before using a production checkout.

Rollback consists of stopping sandbox-backed tasks, preserving their committed branch tips, removing their sandboxes, and returning to host runtime commands. The schema migration is additive: older amux code ignores the extra columns and token table, while the existing explicit inserts continue to receive defaults. No downgrade rewrite of `context.db` is required.

## Open Questions

- Which exact Claude and Codex hook files are present in the current Docker agent templates, and can both be merged without replacing template-owned settings?
- What conservative CPU and memory defaults produce acceptable four-agent behavior on the target Apple-silicon host?
- Does the current `sbx` JSON output expose every stable sandbox and remote identifier needed, or should amux persist additional raw inspect fields for diagnostics?

These questions are prototype measurements and compatibility checks; they do not change the selected database, trust boundary, runtime default, or Git architecture.
