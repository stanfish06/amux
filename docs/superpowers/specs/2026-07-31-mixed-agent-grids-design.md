# Mixed-agent grids — design

2026-07-31. Spawn a grid containing a mixture of agents (e.g. claude + codex)
from the CLI, without waiting for the Phase 3 fleet file.

## Motivation

`spw`/`spg` currently take a single `-a/--agent` applied to every pane. There
is no way to spawn, say, 3 claude + 1 codex in one command. The syntax chosen
here is the shell shorthand; the Phase 3 `amux.yaml` remains the declarative
form and this expands cleanly into it.

## CLI surface

Applies to both `spw` and `spg` (shared `_add_grid_args`).

- `-a/--agent` becomes repeatable (`action="append"`). Each value is
  `<agent>[:count]`, where `<agent>` is a key of `AGENT_COMMANDS` or a raw
  command.
  - Count parsing: split on the **last** `:` only when the suffix is all
    digits. Raw commands containing colons pass through whole; a raw command
    that genuinely ends in `:<digits>` must be spelled by repeating the flag
    instead.
- Specs expand in order and fill the grid **row-major**:
  `-a claude:3 -a codex` on a 2x2 grid → `r0c0 r0c1 r1c0` claude, `r1c1` codex.
- No `-a` at all → `claude` (today's default), count rules below.

### Examples

```sh
# 3 claude + 1 codex, auto 2x2 grid
amux spg myws review -a claude:3 -a codex

# explicit shape; codex count inferred from the remainder
amux spg myws review -r 2 -c 2 -a claude:3 -a codex

# row of 4, explicit counts
amux spg myws review -r 1 -c 4 -a claude:2 -a codex:2

# unchanged: single agent replicated over an explicit shape
amux spw myws -r 2 -c 2 -a claude

# raw command as its own spec
amux spg myws t -a claude:2 -a "htop"
```

## Count and shape resolution

`-r`/`-c` defaults change from `1` to `None` so "omitted" is detectable.

**Count rules:**

- Explicit `:N` → exactly N panes. `N >= 1`; `:0`, negative, or empty prefix
  is an error.
- When the grid shape is **known** (both `-r` and `-c` given): at most
  **one** spec may omit its count; it absorbs the remainder (`r*c - sum(explicit counts)`), which must be
  `>= 1`. Two or more countless specs with a known shape is ambiguous → error.
  - The single-agent backward-compat case (`-r 2 -c 2 -a claude`) is this
    rule with remainder = whole grid.
- When the shape is **unknown** (neither `-r` nor `-c`): a countless spec
  means 1.

**Shape rules** (after counts resolve to total `n`):

- Both `-r` and `-c` given → `n` must equal `r*c`, else error naming both
  numbers.
- One given → the other is derived; `n` must divide evenly, else error.
- Neither given → auto-shape: the factor pair of `n` closest to square, rows
  <= cols (4→2x2, 6→2x3, 5→1x5). No ragged grids in v1; `_build_grid`
  geometry is untouched. (Ragged last row is a possible follow-up if prime
  counts prove annoying.)

Order of evaluation: shape-with-one-side-derived requires `n`, and the
remainder rule requires the shape — so resolution runs as: explicit counts
summed → if both `r`,`c` known, filler absorbs remainder → else countless
specs default to 1 → derive missing dimension(s). A countless spec combined
with only one of `-r`/`-c` therefore defaults to 1 (no remainder is inferable
before the other dimension exists).

## Core changes

Per the "one core, three faces" invariant, parsing lives in `core.py` so the
MCP face reuses it:

- `parse_agent_specs(specs: list[str], nrows, ncols) -> list[str]` — applies
  the count rules and returns the expanded per-pane agent list.
- `resolve_grid_shape(n: int, rows: int | None, cols: int | None) -> (int, int)`
  — applies the shape rules.
- `_build_grid`, `spawn_agent_grid`, `spawn_agent_space` take
  `agents: list[str]` (one entry per pane, row-major) instead of `agent: str`.
  Each pane gets its own command, `@amux_agent` option, and `name[agent]`
  title. `load_*` and the event layer already read agent kind per-pane; no
  changes there.

## Output

Spawn messages show the composition via `collections.Counter`, e.g.
`spawned grid 'review' in 'myws' (2x2: 3 claude + 1 codex)`.

## Errors

All raised as `ValueError` (existing CLI handler prints `amux: <msg>`, exit 1):

- count `:0` / negative / malformed spec
- more than one countless spec with a known shape
- remainder `< 1` for a filler spec
- total vs `r*c` mismatch (message includes both numbers)
- non-divisible single-dimension derivation

## Testing

- Pure-function unit tests (no tmux): spec parsing incl. raw commands with
  colons, count inference/remainder, shape resolution incl. auto-shape and
  divisibility errors, replicate rule.
- One integration test on the libtmux pytest fixture: spawn a mixed 2x2 grid,
  assert each pane's `@amux_agent` option and launch command match the
  row-major expansion.

## Non-goals

- Ragged grids (auto-shape always factors exactly).
- Per-pane prompts/roles — Phase 3 fleet file territory.
- More than one filler spec or percentage/ratio syntax.
