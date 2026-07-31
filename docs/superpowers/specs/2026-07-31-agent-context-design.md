# amux ctx — agent context awareness

**Date:** 2026-07-31
**Status:** approved (design), pending implementation plan

## Goal

An agent running inside an amux pane can ask "who am I, where am I, who is on
my team, and what are they doing" with one CLI call. This is the foundation
for agent-to-agent prompting (Phase 3): the roster prints each teammate's
address, so `amux send <name> "..."` drops in later without redesign.

Guiding constraint from the user: **keep it simple, information concise first.**

## Principle: tmux is already the registry

Panes carry `@amux_name`, `@amux_label`, `@amux_agent`; the event layer
maintains `@amux_state` and `events.jsonl`. Context is **derived live** from
the tmux server plus the last event per pane. No new state files, no daemon,
no registration step — nothing is cached, so nothing goes stale.

## Behavior

### Self-resolution

`amux ctx` reads `$TMUX` (socket path) and `$TMUX_PANE` (own pane id) from the
agent's environment, verifies the socket is the amux server, resolves its own
pane, then walks up: pane options → name/label/agent/state, window → task,
session → workspace, plus cwd.

- Outside an amux pane: print `amux: not inside an amux agent pane` to
  stderr, exit 1.
- `--pane %N` overrides self-detection (human debugging from outside).

### Roster

Scope: everyone in the same **workspace** (session), grouped by **task**
(window), own task first, self marked `(you)`.

Per row: name (stable address, unique per workspace), agent kind, label,
pane id, state, last-event age, last-event detail. The detail column is the
high-value part: for a `needs-input` teammate it shows *what* they are
blocked on (already captured from hook payloads in `events.py`).

```
you: jade-raven  claude @r0c1 %13  task:review  workspace:myproj  busy  ~/Git/myproj
team @ myproj
  review (your task)
    brave-hawk  claude @r0c0 %12  idle         2m   "done: tests pass"
    jade-raven  claude @r0c1 %13  busy (you)
  task0
    quiet-fox   claude @r0c0 %2   needs-input  10s  "May I edit foo.py?"
```

- Non-amux panes (hand-opened shells) appear with `-` for name and
  `pane_current_command` as agent kind — visible, clearly not teammates.
- cwd is shown on the `you:` line; roster rows omit it unless it differs
  from the workspace cwd (concise-first).
- State comes from `@amux_state`; a pane with no state option yet shows
  `starting` if it has amux options, `-` otherwise.

### JSON output

`amux ctx --json` emits the same data structurally:

```json
{
  "self": {"name": "jade-raven", "agent": "claude", "label": "r0c1",
           "pane": "%13", "task": "review", "workspace": "myproj",
           "state": "busy", "cwd": "/home/user/Git/myproj"},
  "team": [
    {"task": "review", "agents": [
      {"name": "brave-hawk", "agent": "claude", "label": "r0c0",
       "pane": "%12", "state": "idle", "cwd": "...",
       "last_event": {"kind": "stop", "ts": 1753900000.0, "detail": "done: tests pass"}}
    ]}
  ]
}
```

This schema later becomes the MCP `get_team` return value verbatim — one
core, three faces.

## Implementation shape

No new module; three small touches:

- **`core.py`**: `resolve_self(server) -> AgentPane` from `$TMUX_PANE`
  (existing `load_agent_pane` does the field work). `load_agent_pane` reads
  `@amux_state` into `AgentPane.state` instead of defaulting `"idle"`.
- **`events.py`**: reuse `tail(n=1, pane=...)` for the last-event column.
- **`cli.py` / `utils.py`**: `amux ctx [--json] [--pane %N]` subcommand;
  text formatting in `utils.py` alongside the existing `*_to_string`
  helpers.

Note: `AgentPane.__post_init__` currently sets a tmux hook on load; loading
panes for a roster must not re-register hooks — move hook registration to
spawn time (`_build_grid`) if it interferes.

## Testing

Pytest against the throwaway-server libtmux fixtures (`libtmux.pytest_plugin`,
Phase 1 item):

- self-resolution: correct name/label/task/workspace/state for a given pane id
- grouping: own task first, self marked, sibling tasks present
- non-amux pane rendered with `-` name
- outside-amux error path (bad/missing socket)
- `--json` schema: keys above present and typed

## Out of scope (noted for later)

- **Bootstrap**: how agents learn `amux ctx` exists. Deferred — user tells
  agents about the CLI manually for now.
- `--all` (whole-server roster across workspaces).
- The `send` envelope / agent-to-agent prompting itself (Phase 3).
