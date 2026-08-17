## 1. Spec parsing and the request type

- [x] 1.1 Add a frozen `AgentRequest(agent, model="", effort="")` dataclass in `shared.py`, so both `runtime.py` and `sandbox.py` can import it without a cycle.
- [x] 1.2 Add the `AGENT_TUNING` flag table and a render function in `shared.py` that turns a request into an argument tuple: claude `--model V` / `--effort V`, codex `-m V` / `-c model_reasoning_effort=V`; empty fields contribute nothing.
- [x] 1.3 Rewrite `core._parse_agent_spec` to parse `AGENT[@MODEL][/EFFORT][:COUNT]`: strip the count first with the existing "last `:` only when the suffix is all digits" rule, then split `@` and `/` only when the remaining head is a key in `AGENT_COMMANDS`.
- [x] 1.4 Reject structurally malformed specs with a clear message: empty model (`claude@`), empty effort (`claude@opus/` or `claude/`), and empty agent. Make the invalid-count error mention the raw-command escape hatch.
- [x] 1.5 Change `core.parse_agent_specs` to return `list[AgentRequest]`, keeping the countless-spec-absorbs-remainder and default-count-of-1 behaviour unchanged.
- [x] 1.6 Unit-test the parser: every subset of the grammar, raw commands containing `/` and `@`, raw command with a count, mixed-agent grids with `-r`/`-c`, and each malformed form.

## 2. Grid construction seam

- [x] 2.1 Add `model` and `effort` fields (defaulting to `""`) to `runtime.PaneSpec`.
- [x] 2.2 Change `Runtime.preflight` and `Runtime.prepare` in the protocol and both implementations to take `AgentRequest` values instead of `str`; have `DockerSandboxRuntime.preflight` pass `[r.agent for r in requests]` to `sandbox.preflight`.
- [x] 2.3 Update `core._build_grid`, `core.spawn_agent_space`, and `core.spawn_agent_grid` to carry requests through and construct `PaneSpec` with model and effort; keep their `["claude"] * n` defaults working as `AgentRequest("claude")`.
- [x] 2.4 Keep the recorded agent kind bare everywhere: `@amux_agent`, the pane title, `events.emit(agent=...)`, and the store's `agent` column continue to receive `claude`/`codex` only.

## 3. Host runtime

- [x] 3.1 In `HostRuntime.prepare`, append rendered tuning arguments to the resolved agent command, joined with `shlex.join` so unvalidated values cannot inject shell syntax.
- [x] 3.2 Leave raw command specs untouched — they carry no model or effort by construction, so nothing is appended.
- [x] 3.3 Test the composed launch keys for claude and codex, for each subset of model/effort, and assert a plain `claude` spec still produces today's exact command.
- [x] 3.4 Test that a model or effort value containing shell metacharacters arrives quoted.

## 4. Docker Sandbox runtime

- [x] 4.1 Extend `sandbox.attach_argv` (and `attach_command`) to accept the request and append rendered tuning arguments after the existing `AGENT_ATTACH_ARGS` entries.
- [x] 4.2 Emit the `<agent> -- <args>` form whenever either source contributes arguments, not only when `AGENT_ATTACH_ARGS` has an entry, so a sandboxed `claude@opus` does not silently drop its flags.
- [x] 4.3 Pass the request through `DockerSandboxRuntime.prepare` to the attach command; leave `sandbox.create_argv` on the bare agent kind.
- [x] 4.4 Test sandboxed claude (flags present, `--` form emitted) and sandboxed codex (hook-trust flag and tuning flags both present, in a stable order).

## 5. Discovery through `amux ctx`

- [x] 5.1 Set `@amux_model` and `@amux_effort` pane options during grid construction, alongside the existing identity options, only when a value is present.
- [x] 5.2 Add `model` and `effort` to `events.PaneFacts`, appending the two fields to `_PANE_FORMAT` and `_parse_pane`.
- [x] 5.3 Bump `store.SCHEMA_VERSION` to 4 and add `model` / `effort` columns to `worktrees` following the `_RUNTIME_COLUMNS` additive-migration pattern; include them in the worktree DDL and in `record_worktree`.
- [x] 5.4 Record model and effort on the worktree row when a pane's worktree is registered, under both runtimes.
- [x] 5.5 Report model and effort in host `amux ctx` (human output and `self` in `--json`), omitting them when absent rather than printing a default.
- [x] 5.6 Serve model and effort from the worktree row in the context service's `self` payload, so a sandboxed `amux ctx` and `ctx --json` report them too.
- [x] 5.7 Extend `test_store_migration.py` for the 3 → 4 upgrade, and test `ctx` output for both runtimes with and without values.

## 6. CLI surface and documentation

- [x] 6.1 Update the `-a/--agent` help text in `cli._add_grid_args` to the full `AGENT[@MODEL][/EFFORT][:COUNT]` grammar with a mixed-agent example.
- [x] 6.2 Update `README.md` with the new grammar, per-agent flag mapping, and the raw-command escape hatch for model ids containing `/` or ending in `:<digits>`.
- [x] 6.3 Update `skills/amux/SKILL.md` — the copy installed into every sandbox — with the grammar, an example, the pass-through behaviour (~~a bad value is rejected by the agent CLI, not amux~~ — **disconfirmed by 7.4: nothing rejects it; see design.md Risks**), and the escape hatch.
- [x] 6.4 Update `docs/sandbox-smoke-test.md` so its procedure exercises a spec carrying model and effort.

## 7. Verification

- [x] 7.1 Update the affected existing tests to the new signatures: `test_runtime_seam.py`, `test_cli_runtime.py`, `test_host_grid_snapshot.py`, `test_sandbox_adapter.py`, `test_sandbox_runtime.py`, and any golden files under `tests/golden`.
- [x] 7.2 Run the full test suite and confirm the no-tuning path is byte-identical to before via the host grid snapshot.
- [x] 7.3 Spawn a real mixed grid on the host (`-a claude@opus/high -a codex@gpt-5.6-sol/xhigh`), confirm from each pane that the agent came up on the requested model and effort, and that `amux ctx` reports them.
- [x] 7.4 Confirm a mistyped effort fails the way the design says it does — ~~the agent CLI rejects it and the pane dies at spawn~~ — and that the behaviour matches what the documentation now claims. **Ran, and the prediction was DISCONFIRMED: nothing dies.** claude warns and runs on its default, codex accepts the string silently, and a bad model on either agent starts normally and fails at the first API call. The real hazard is the inverse — the pane survives on a configuration `amux ctx` does not report. design.md's Risks entry carries the measurement; README, `skills/amux/SKILL.md` and `docs/sandbox-smoke-test.md` were corrected to match, which is what makes the second clause true.
