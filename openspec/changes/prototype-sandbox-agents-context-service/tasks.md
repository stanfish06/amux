## 1. Test Foundations and Durable Identity

- [x] 1.1 Create `tests/` pytest scaffolding with isolated `XDG_STATE_HOME`, temporary Git repositories, fake tmux facts, and an executable fake-`sbx` fixture; verify `pytest -q` runs without Docker or provider credentials.
- [x] 1.2 Write failing migration tests for schema version 2 to version 3, then add backward-compatible runtime, runtime-status, sandbox-name, sandbox-id, and socket-name fields to `worktrees`; verify old rows read as host runtime and existing explicit inserts still work.
- [x] 1.3 Write failing token-lifecycle tests, then add `context_tokens` storage operations for minting metadata, hashed lookup, expiry, permission binding, and revocation; verify plaintext tokens never enter SQLite or captured logs.
- [x] 1.4 Add store-level concurrency and visibility regression tests covering simultaneous native and service notes/events, WAL busy handling, repository filtering, and agent-private notes; verify existing native semantics remain unchanged.

## 2. Host Context Service

- [x] 2.1 Implement the loopback-only context-service skeleton, configuration, health endpoint, bounded JSON parsing, stable error envelope, and redacted logging in `src/amux/context_service.py`; verify oversized, malformed, wrong-content-type, and unauthenticated requests fail closed.
- [x] 2.2 Implement capability authentication that derives the caller's execution identity and permissions from the token record instead of request fields; verify identity spoofing, cross-repository access, cross-workspace access, and revoked tokens are rejected.
- [ ] 2.3 Implement `GET /v1/context`, `GET /v1/notes`, and `POST /v1/notes` by reusing existing context and note-visibility functions; verify sandbox and native callers receive equivalent scoped results and monotonic note cursors.
- [ ] 2.4 Implement `POST /v1/events`, `GET /v1/events/state`, and bounded `GET /v1/events/wait`; verify committed sandbox events update the correct tmux pane option, wake waiters, resume after event cursors, and remain attributable after pane-ID reuse.
- [ ] 2.5 Add foreground serve plus idempotent start/status/stop lifecycle support with PID, configurable port, stale-process detection, and redacted logs under the amux state directory; verify port conflicts and incompatible schema versions return actionable errors without opening a fallback listener.

## 3. Sandbox Context Client and Bootstrap

- [x] 3.1 Build the self-contained standard-library sandbox client in `src/amux/sandbox_client.py` with compatible `ctx`, `notes`, `note`, and `event emit|state|wait` parsing and output; verify supported commands against a fake service and host-control commands fail locally.
- [x] 3.2 Implement secure client-config staging and sandbox copy with a mode-`0600` endpoint/token file, cleanup of the host staging file, and no secret-bearing subprocess arguments; verify failure paths remove plaintext material and redact command diagnostics.
- [ ] 3.3 Inspect the current Claude and Codex Docker Sandbox template hook locations, capture representative fixtures, and implement agent-specific bootstrap merging that invokes the context client without copying host user configuration; verify generated hook configs preserve unrelated template settings.
- [x] 3.4 Add an end-to-end fake-service test in which two simulated sandbox clients exchange task notes and state transitions while agent-private notes remain isolated; verify no test sandbox mount includes the amux state directory or tmux socket.

## 4. Runtime Adapter and Sandbox Creation

- [ ] 4.1 Introduce grid-scoped runtime configuration and a host runtime adapter in `src/amux/runtime.py`, then refactor `core.py` to consume prepared launches; verify existing host spawn, raw-command, worktree, event, and pane-metadata behavior is unchanged.
- [ ] 4.2 Split task integration-worktree creation from host per-agent worktree creation in `worktree.py`; verify both paths roll back branches, worktrees, and registry states atomically when setup fails.
- [ ] 4.3 Implement the isolated `sbx` subprocess adapter, deterministic sandbox naming, JSON inspection, supported-version reporting, and resource validation in `src/amux/sandbox.py`; verify exact commands and parsing through fake-`sbx` fixtures.
- [ ] 4.4 Implement non-mutating sandbox preflight for executable availability, supported agent kind, primary Git checkout, service health, Docker authentication/diagnostics, localhost policy reachability, and resource values; verify failures occur before tmux, Git, database, or sandbox mutation and include exact remediation.
- [ ] 4.5 Implement clone-mode sandbox creation, assigned-branch bootstrap, context-client installation, token delivery, registry activation, and `sbx run --name` pane attachment; verify a mixed Claude/Codex grid creates one capped, no-shared-skills sandbox per pane.
- [ ] 4.6 Implement reverse-order transactional rollback for partially created grids, including sandboxes, remotes, tokens, registry rows, integration worktrees, panes, and sessions; verify cleanup errors are aggregated without replacing the originating failure.

## 5. Git Integration, Lifecycle, and Visibility

- [ ] 5.1 Extend `amux integrate` to fetch each sandbox branch through its Docker-created host remote and merge the fetched commit into the task integration worktree; verify success, no-delta, missing-commit, stopped-sandbox, and merge-conflict cases with disposable repositories.
- [ ] 5.2 Add sandbox stop and reattach behavior to `kg`, `kw`, and subsequent runtime launch paths while preserving VM state and active credentials; verify stopped sandboxes remain inspectable and reattach to the same recorded sandbox ID.
- [ ] 5.3 Add safe sandbox cleanup that checks for dirty files, preserves committed branch tips locally, removes clean sandboxes/remotes, revokes tokens, and requires an explicit force flag for data loss; verify a failed or refused removal never marks the registry row removed.
- [ ] 5.4 Include runtime, runtime status, sandbox name, and sandbox ID in context/list JSON and monitor models while preserving host-agent output compatibility; verify mixed host and sandbox workspaces resolve starting, busy, idle, needs-input, stopped, merged, and dead states correctly.
- [ ] 5.5 Add `--runtime docker-sandbox`, CPU, memory, shared-skills opt-in, and forced-clean CLI options plus a sandbox doctor/preflight command; verify help text identifies the backend as optional and no command silently installs `sbx`, signs in, or changes Docker policy.

## 6. Documentation and Prototype Verification

- [ ] 6.1 Update `README.md` and `skills/amux/SKILL.md` with the runtime boundary, prerequisites, context-service trust model, clone/commit/integrate workflow, lifecycle commands, troubleshooting, and the prohibition on mounting the state database.
- [ ] 6.2 Add a disposable-repository smoke-test guide that records `sbx` version, spawn-to-prompt latency, four-agent CPU and memory use, disk growth, blocked-network diagnostics, context exchange, committed branch integration, reattachment, and fixed-point cleanup.
- [ ] 6.3 Run the complete automated suite, package build, CLI help probes, `git diff --check`, OpenSpec validation, and a second clean test run; fix failures and record the exact verification commands and outcomes.
- [ ] 6.4 On a supported authenticated Docker Sandbox host, execute the documented Claude/Codex smoke test in a disposable repository and record measurements and compatibility findings; if an external prerequisite is unavailable, leave the prototype code complete and report the exact unverified live step without weakening automated coverage.
