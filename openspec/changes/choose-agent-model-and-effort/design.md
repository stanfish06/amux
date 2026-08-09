## Context

Agent launch flows through one narrow seam. `core.parse_agent_specs` expands `-a` specs into a flat `list[str]` of agent names, `core._build_grid` zips that list against panes and builds a `runtime.PaneSpec(pane, agent, name)` for each, and the active runtime turns each `PaneSpec` into a `runtime.Launch` carrying the keys to send.

There are exactly two places a launch command is composed:

- `HostRuntime.prepare` — `AGENT_COMMANDS.get(spec.agent, spec.agent)`, sent to the pane as `send-keys` text after an optional `cd` into the agent's worktree.
- `sandbox.attach_argv(name, agent)` — `sbx run --name <name>`, plus `<agent> -- <extra>` when that agent needs extra arguments, which today means `codex` and its `--dangerously-bypass-hook-trust` flag. `DockerSandboxRuntime.prepare` renders it via `attach_command` into a single key string.

Both CLIs already accept what is needed, verified against the installed versions (`claude` 2.1.223, `codex-cli` 0.146.0):

| | model | effort |
|---|---|---|
| `claude` | `--model <alias-or-id>` | `--effort <low\|medium\|high\|xhigh\|max>` |
| `codex` | `-m <model>` | `-c model_reasoning_effort=<level>` |

The spellings differ, and codex routes effort through its generic config-override flag rather than a dedicated one, so a single shared flag name is not available. The effort vocabularies also differ between the two tools.

The agent string is load-bearing beyond launch: `core.AgentPane.is_agent` tests membership in `AGENT_COMMANDS`, `sandbox.create_argv` tests membership in `SUPPORTED_AGENTS`, `sandbox_bootstrap.install_hooks`/`install_skill` branch on it, the `@amux_agent` pane option feeds `events.pane_facts`, and the store's `agent` column feeds notes, events, and the context service. Anything that changes the meaning of that string ripples into all of them.

## Goals / Non-Goals

**Goals:**

- One spec can say which model and effort its agent runs: `AGENT[@MODEL][/EFFORT][:COUNT]`.
- A heterogeneous grid works — `-a claude@opus/high:2 -a codex@gpt-5.6-sol/xhigh` — because each spec carries its own values.
- Host and `docker-sandbox` runtimes behave identically with respect to model and effort.
- A new model or effort level from either vendor works with no amux change.
- Existing specs launch byte-identical commands to today.
- An agent can read its own model and effort back out of `amux ctx` under either runtime.

**Non-Goals:**

- Grid-wide `--model` / `--effort` flags. Rejected during proposal: the model-name spaces of claude and codex do not overlap, so one global value cannot serve a mixed grid, and per-spec already expresses everything a global would.
- Validating model names or effort levels against a list amux maintains.
- Persistent defaults (config file, env var, per-repo settings).
- Changing model or effort on a running pane.
- Extending model/effort to raw command specs — a raw command already spells out its own flags.

## Decisions

### Grammar: `AGENT[@MODEL][/EFFORT][:COUNT]`, parsed only for known agents

`@` for model, `/` for effort, `:` for count as today. Parse order is count first (preserving the existing "last `:` only when the suffix is all digits" rule), then — and only if the remaining head token is a key in `AGENT_COMMANDS` — split `@` and `/`.

Gating on known agents is what keeps raw commands safe. `-a /usr/bin/bash` and `-a 'ssh user@host'` have heads that are not `claude` or `codex`, so they are never split, and the existing "anything unknown is a raw command" contract holds unchanged.

*Alternatives considered.* A delimiter that cannot appear in a model id (`+`, `%`, `^`) would remove the `openai/gpt-5` collision described under Risks, but reads worse and was not the grammar chosen. Repeatable long flags (`-a claude --model opus`) cannot bind a value to one spec among several. Bracket syntax (`claude[opus,high]`) requires shell quoting every time.

### A structured request type replaces `list[str]`

Introduce a frozen dataclass — `AgentRequest(agent: str, model: str = "", effort: str = "")` — and change `parse_agent_specs` to return `list[AgentRequest]`. `PaneSpec` gains the same two fields. `Runtime.preflight` and `Runtime.prepare` take requests instead of bare strings; `DockerSandboxRuntime.preflight` passes `[r.agent for r in requests]` down to `sandbox.preflight`, which continues to deal in agent kinds.

Encoding the values back into a string (`"claude@opus/high"`) and re-parsing at the runtime boundary was rejected: every consumer of the agent string would need to know whether it held a kind or a spec, which is exactly the ambiguity that would leak into `is_agent`, `SUPPORTED_AGENTS`, and the store.

### One flag table, in `shared.py`

Both `runtime.py` and `sandbox.py` need to render flags, and `runtime` imports `sandbox`, so the table cannot live in `runtime` without a cycle. Put it in `shared.py` with a single function that renders a request into an argument tuple:

```
AGENT_TUNING = {
    "claude": {"model": ("--model", "{}"), "effort": ("--effort", "{}")},
    "codex":  {"model": ("-m", "{}"),      "effort": ("-c", "model_reasoning_effort={}")},
}
```

A table keeps the difference between the two CLIs declarative and puts adding a third agent in one place. Rendering returns a tuple of arguments, not a string, so the sandbox path can use it directly as argv and the host path can quote it.

### Quoting at the host seam

Host launches are shell text sent with `send-keys`, so rendered arguments are joined with `shlex.join` before being appended to the agent command. Values pass through unvalidated, which means they must be assumed hostile to the shell; without quoting a value containing `;` or `$(...)` would execute in the agent's pane. The sandbox path builds argv for `sbx` directly and only needs the same treatment where `attach_command` flattens argv to a string, which it already does.

### `docker-sandbox`: flags go on the attach command, not on create

`sbx create` fixes the sandbox's agent and repository; the model is a per-run choice, so the flags belong on `sbx run`. `attach_argv` grows an optional request parameter and composes: existing per-agent args (`AGENT_ATTACH_ARGS`) first, then rendered tuning flags. The `<agent> -- <args>` form must now be emitted whenever *either* source contributes arguments, where today it is emitted only when `AGENT_ATTACH_ARGS` has an entry — otherwise a sandboxed `claude@opus` would silently drop its flags, since claude has no entry.

### Discovery: pane options for host, store columns for sandbox

These are two different read paths and both need to work, so both get the data.

- Host `ctx` reads tmux pane options through `events.pane_facts`. Add `@amux_model` and `@amux_effort` next to the existing `@amux_agent`/`@amux_label`/`@amux_name`, extend `_PANE_FORMAT` and `_parse_pane`, and add the fields to `PaneFacts`. This is the only path that works for a host agent in a non-repo directory, which has no worktree row at all.
- Sandboxed `ctx` is served by the host context service from the worktree row, because the agent cannot reach tmux. Add `model` and `effort` columns to `worktrees` via the existing additive-column migration (`_RUNTIME_COLUMNS` pattern, `ALTER TABLE ... ADD COLUMN` guarded by `SCHEMA_VERSION`, bumping 3 → 4) and include them in the `self` payload.

`amux ctx` omits a value it does not have rather than printing a guessed default, matching the existing treatment of the `runtime:` line, which appears only when it is not `host`.

### The recorded agent kind stays bare

`@amux_agent`, the store's `agent` column, hook installation, and `SUPPORTED_AGENTS` all keep seeing `claude` or `codex`. Model and effort travel in their own fields. The pane title (`{name}[{agent}]`) also stays as-is — widening it to `{name}[claude@opus/high]` costs golden-snapshot churn and TUI column width for a string that `ctx` reports better.

## Risks / Trade-offs

- **~~A mistyped effort kills the pane at spawn.~~ CORRECTED BY MEASUREMENT (task 7.4, crimson-newt).** This prediction was wrong in both mechanism and severity. Measured live against `claude` 2.1.223 and `codex-cli` 0.146.0, no bad value is fatal: `claude --effort hihg` prints `Warning: Unknown --effort value 'hihg' — ignoring it and using the default effort. Valid values: low, medium, high, xhigh, max.` and runs on its default; `codex -c model_reasoning_effort=hihg` accepts the string silently and displays it; a bad model on either agent starts normally and fails at the first API call. **The real risk is the inverse and quieter: the pane survives with a configuration that differs from the one `amux ctx` reports.** `ctx` reports what the pane was launched with and cannot know what the agent did with it, so after a typo the two disagree and only the agent's own UI is authoritative. → Mitigation: documented as such in README, `skills/amux/SKILL.md` and the smoke test, including that the agent's own startup box is the authority. A warning-only check against a known set would narrow this, and unlike the original framing it would now be about *reporting accuracy* rather than about saving the pane; still not required by this change, and it does not re-litigate pass-through.
- **A model id containing `/` is misparsed.** `-a claude@openai/gpt-5` reads as model `openai`, effort `gpt-5`. Likewise a Bedrock-style id ending `:0` is eaten by the count rule and reported as an invalid count. → Mitigation: document the raw-command escape hatch (`-a 'claude --model openai/gpt-5'`) in help text and the skill, and make the invalid-count error mention it. Accepted because the compact grammar was the chosen syntax and short aliases are the common case.
- **Raw-command specs lose the escape hatch's benefits.** A raw `claude ...` spec is not in `AGENT_COMMANDS`, so `is_agent` is false and it gets no sandbox support and no agent-kind identity. → Mitigation: out of scope, but note it where the escape hatch is documented so nobody discovers it by surprise.
- **Signature changes touch many call sites at once.** `parse_agent_specs`, `PaneSpec`, both runtimes' `preflight`/`prepare`, `attach_argv`, and their tests (`test_runtime_seam.py`, `test_sandbox_adapter.py`, `test_sandbox_runtime.py`, `test_cli_runtime.py`, `test_host_grid_snapshot.py`) move together. → Mitigation: keep `AgentRequest` construction from a plain agent name trivial (both tuning fields default to `""`) so existing tests can be updated mechanically rather than rewritten.
- **`_PANE_FORMAT` is positional.** Two new delimited fields will break any golden snapshot that pins the format string or its parsed output. → Mitigation: append the new fields at the end, and update `tests/golden` in the same commit.
- **Schema bump touches the sandbox context path.** Migration 3 → 4 is additive with defaults, so an older amux reading a newer DB still works, but a newer amux against an un-migrated DB must migrate before the context service answers a `ctx`. → Mitigation: the existing `_migrate` runs on connect; cover it in `test_store_migration.py` alongside the current cases.
- **Documentation drift is a correctness bug here.** `skills/amux/SKILL.md` is installed into every sandbox and is what agents treat as authoritative; if it keeps advertising `AGENT[:COUNT]`, agents will not use the feature and may report it as unsupported. → Mitigation: treat README, the skill, and `docs/sandbox-smoke-test.md` as part of the change, not follow-up.

## Migration Plan

1. Land the parser, `AgentRequest`, and the flag table with the host runtime; existing specs must produce identical commands (snapshot test).
2. Land the sandbox attach change.
3. Land the store migration and the `ctx` reporting for both paths.
4. Update README, `skills/amux/SKILL.md`, and `docs/sandbox-smoke-test.md`.

Rollback is a revert: no data is rewritten, the two new columns are additive with empty defaults, and a reverted amux ignores them.

## Open Questions

- Should `lsw` / `monitor` show model and effort, or is `ctx` enough? Deferred — the TUI has a column budget and `ctx` covers the "what am I running" question that motivated this.
- Should the invalid-count error message special-case a value that looks like a Bedrock-style model id, or just mention the escape hatch generically? Resolved as generic for now; revisit if it confuses anyone.
