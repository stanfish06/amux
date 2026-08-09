## Why

Every agent amux spawns runs on whatever model and reasoning effort its own CLI happens to default to, so a grid cannot mix a cheap reviewer with an expensive implementer, and the only way to raise effort for one pane is to launch it as a raw command and give up hook-driven state, sandbox support, and the agent-kind identity that `ctx`, `monitor`, and `integrate` rely on. Model and effort are the two knobs that decide what a grid costs and how well it reasons, and they are exactly the knobs amux does not expose.

## What Changes

- Extend the `-a/--agent` spec grammar from `AGENT[:COUNT]` to `AGENT[@MODEL][/EFFORT][:COUNT]`, so each spec in a grid may carry its own model and reasoning effort (e.g. `-a claude@opus/high:2 -a codex@gpt-5.6-sol/xhigh`).
- Apply the new grammar only to amux's known agents (`claude`, `codex`); raw command specs keep passing through verbatim, including ones containing `@` or `/`.
- Render model and effort into each agent CLI's own flags rather than assuming a shared spelling: `claude --model X --effort Y`, and `codex -m X -c model_reasoning_effort=Y`.
- Pass model and effort values through unvalidated. amux validates the *shape* of a spec (a delimiter with nothing after it is an error) but never the *values*, so a newly shipped model or effort level works without an amux release.
- Honour model and effort under both runtimes: the host runtime appends the flags to the launched command, and the `docker-sandbox` runtime appends them to the agent's `sbx run ... <agent> -- <args>` argument list alongside the existing hook-trust flag.
- Carry model and effort per pane through the grid-construction seam, keeping the recorded agent kind bare (`claude`, not `claude@opus/high`) so pane options, the context store's `agent` column, hook bootstrap, `SUPPORTED_AGENTS`, and state reporting are unaffected.
- Surface the resolved model and effort where an agent and its teammates can see them, so a pane's configuration is discoverable after spawn rather than only in the shell history of whoever spawned it.
- No change to defaults: a spec with no `@` and no `/` launches exactly the command it launches today.

## Capabilities

### New Capabilities

- `agent-model-selection`: Per-agent model and reasoning-effort selection expressed in the agent spec grammar, translated into each supported agent CLI's own flags, applied identically by the host and Docker Sandbox runtimes, and reported back through agent context.

### Modified Capabilities

None. This repository has no published OpenSpec capability specifications yet; the in-flight `sandbox-agent-runtime` capability is extended in spirit but its own change is not amended here, and the sandbox attach path is covered by the new capability above.

## Impact

- Affected Python modules: `cli.py` (spec help text), `core.py` (spec parsing, grid construction, pane identity), `runtime.py` (`PaneSpec`, `AGENT_COMMANDS`, both runtimes' `preflight`/`prepare` signatures), `sandbox.py` (`attach_argv`/`attach_command`), and whichever module hosts the shared per-agent flag table.
- `parse_agent_specs` stops returning `list[str]`. Every caller of the parser, of `Runtime.preflight`, and of `Runtime.prepare` moves to a structured per-agent request type; this is internal API only, with no user-visible break.
- Shell safety becomes load-bearing: host launches reach the pane as `send-keys` text and sandbox attach commands are rendered to a string, so pass-through values must be quoted rather than interpolated raw.
- No context-store schema migration is required if model and effort are surfaced from live pane state; persisting them would need one, and the design must choose deliberately.
- Documentation carrying the spec grammar must be updated together, or agents will keep reading the old grammar as authoritative: `README.md`, `skills/amux/SKILL.md` (also installed into every sandbox), and `docs/sandbox-smoke-test.md`.
- Accepted tradeoff from pass-through: a mistyped effort level is rejected by the agent CLI at startup, not by amux, so that pane dies at spawn instead of erroring before anything is created.
- Accepted grammar limitation: a model id containing `/` (e.g. `openai/gpt-5`) or ending in `:<digits>` (e.g. a Bedrock id like `...-v1:0`) collides with the effort and count delimiters and must be launched as a raw command spec instead.
