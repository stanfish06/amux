## Why

Amux currently runs autonomous coding agents directly on the host with approval and sandbox checks bypassed, so an agent can affect host files, processes, credentials, and the host Docker daemon beyond its assigned repository. Docker Sandboxes offers a stronger microVM boundary, but adopting it safely requires an execution backend that preserves amux coordination and a context path that does not mount or replicate the host SQLite database into untrusted sandboxes.

## What Changes

- Add an opt-in Docker Sandbox execution runtime for Claude and Codex agents while retaining the existing host runtime as the default.
- Create one named, resource-capped sandbox per sandboxed agent and map its lifecycle to amux spawn, stop, integrate, and clean operations.
- Use Docker Sandbox clone mode for Git-capable agent isolation and integrate committed sandbox branches through the host task integration branch.
- Add a host-only context service that remains the sole owner of `context.db` and exposes a minimal authenticated API for identity, roster, notes, state events, and bounded waits.
- Add a thin sandbox context client so existing `amux ctx`, `notes`, `note`, and `event` workflows can operate without access to the host tmux socket or state directory.
- Add explicit preflight checks, conservative network and credential handling, deterministic cleanup, and focused prototype tests and measurements.
- Do not mount the amux state directory or SQLite files into sandboxes, change the default runtime, or make Docker Sandboxes a mandatory dependency.

## Capabilities

### New Capabilities

- `sandbox-agent-runtime`: Opt-in creation, attachment, Git integration, resource controls, status reporting, and cleanup for Docker Sandbox-backed agents.
- `sandbox-context-bridge`: Authenticated, scope-aware context access between sandboxed agents and the host-owned amux context store without exposing the database or tmux control socket.

### Modified Capabilities

None. This repository has no existing OpenSpec capability specifications; current host-agent behavior remains the default contract.

## Impact

- Affected Python modules include CLI argument handling, agent launch orchestration, runtime lifecycle, Git integration, context storage access, events, and monitoring.
- The context store requires a schema migration for runtime and sandbox identity metadata while retaining SQLite as the authoritative same-host store.
- Docker's `sbx` CLI and account authentication are optional runtime prerequisites detected at execution time, not Python package dependencies.
- Sandboxed execution introduces microVM CPU, memory, disk, startup, network-policy, and cleanup considerations that must be surfaced in CLI output and prototype measurements.
- User-level Claude and Codex hooks are not inherited by sandboxes, so the prototype must install or generate sandbox-local context hooks/client configuration.
