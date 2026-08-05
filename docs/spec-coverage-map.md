# Spec coverage map — prototype-sandbox-agents-context-service

Task 6.3. Every Requirement and Scenario in the change's two specifications,
mapped to its implementation, the test that proves it, and **the mutation that
would make that test fail**.

The third column is the point. A scenario with a passing test but no nameable
mutation is recorded as a finding, because that is the shape every coverage
defect found during this change actually had: a test whose green came from
something other than the behaviour it named.

## What this was built against

| | |
|---|---|
| Integration branch | `amux/amux/docker/integration` |
| Pinned SHA | **`f30c43b`** (R6 rows re-run; the rest of the map was first built against `efe8672`) |
| Mapped and mutated from | `amux/amux/docker/swift-crane` @ `a45fc7d` |
| Tree equivalence | `git diff --stat f30c43b a45fc7d -- src tests tui docs Makefile pyproject.toml` is **empty**. `tui/` is included this time and was not in the first pass, because the R6 show half lives there |
| Union suite | **800 passed** after the R6 restructure below (`f30c43b` itself is 799 passed / 1 failed, and that one failure is the predicted collision this re-run fixes) |
| Specs | `openspec/changes/prototype-sandbox-agents-context-service/specs/{sandbox-agent-runtime,sandbox-context-bridge}/spec.md` |
| Counted | 15 Requirements, 34 Scenarios (verified by `grep -c` on both files, not by trusting the scope) |

**Line numbers** are cited where verified by reading. Where a row cites
`file:symbol` instead, the exact line was not confirmed — that is deliberate, so
no number in this document is invented.

**Labels** for the mutation column:

- **(a) NO MUTATION EXISTS** — proven by running it: the behaviour was broken
  and the test stayed green. A finding.
- **(b) CANNOT NAME ONE** — the module is understood and no mutation could be
  constructed. A finding.
- **(c) UNSURE ABOUT THE MODULE** — the uncertainty is about someone else's
  module rather than about the test. Marked for the owner, not a finding.
- Unlabelled — a mutation is named and is expected to fail the test. Executed
  ones say so explicitly.

---

## 1. Findings — read these first

> **F1, F2 and F3 are FIXED as of `f30c43b`** — misty-panda `8786f6c` (Python)
> and `dc487a7` (TUI). The findings are kept below as written, because they are
> why 5.4 was un-ticked and because the fix is only legible against them. What
> landed, and how it was verified, is in §3 R6.

### F1. The monitor clause of "Runtime state is visible in existing amux views" has no implementation and no test

`sandbox-agent-runtime` Requirement 6 states: *"The monitor SHALL continue to
resolve starting, busy, idle, needs-input, stopped, and dead states from host
tmux facts plus context-service events"*, and its scenario requires that
*"`amux ctx --json`, list commands, **and the monitor** distinguish their
runtimes and show the sandbox name and lifecycle state only where applicable"*.

The `ctx` and list halves are implemented and tested. The monitor half is not:

- `core.runtime_fields` (`core.py:562`) is the only producer of `runtime`,
  `runtime_status` and `sandbox_name`, and its **only** caller is
  `_roster_entry` (`core.py:558`), which feeds `build_context` — the `ctx` and
  `/v1/context` path.
- `events.pane_states` (`events.py`, the monitor's feed) returns exactly
  `pane, kind, workspace, task, agent, name, label, state, last_event`. No
  runtime, no `runtime_status`, no `sandbox_name`.
- `monitor.py` (65 lines) contains no reference to `runtime` or `sandbox`.
- `tui/src` contains no reference to `runtime` or `sandbox`.
- No test relates `pane_states` to runtime identity. `pane_states` is referenced
  by exactly one test file, `test_context_service_events.py`, and only for
  `GET /v1/events/state`.

Consequence: a sandbox-backed agent is indistinguishable from a host agent in
the monitor, which is the view the requirement names explicitly.

Label: no implementation, therefore no test and no mutation. Owner:
misty-panda (`core.py`, `events.py`, `monitor.py`). The *intent* question is
theirs — it is possible runtime identity was judged out of monitor scope — but
the requirement names the monitor, so the deviation needs a ruling either way.

### F2. The `stopped` state named in that same requirement has no representation

`events.AgentState` (`events.py:17`) is
`Literal["starting", "busy", "idle", "needs-input", "dead"]` — five members, no
`stopped`. `STATE_BY_KIND` (`events.py:20`) maps five event kinds onto those
five states.

A stopped sandbox's stoppedness lives in `worktrees.runtime_status`
(`store.py:772` documents `created/running/stopped/failed`), which is a
different axis from the pane's `AgentState` — and by F1 it never reaches the
monitor. So no code path resolves a `stopped` state for the monitor to show.

Label: no implementation. Owner: misty-panda. Same ruling needed as F1; they
are two facets of one gap.

### F3. `GET /v1/events/state` inherits F1 through no fault of its own

My `_event_state` handler (`context_service.py:_event_state`) returns
`events.pane_states` output verbatim, by design and by happy-deer's pinned wire
contract. So a sandbox client asking for team state also cannot distinguish
runtimes. This is not a separate defect — it resolves when F1 does — but it is
worth recording that the gap reaches the sandbox-facing API too, and that fixing
`pane_states` fixes both callers at once.

---

## 2. Mutations executed

Named for all 34 rows; executed for flagged rows and for a sample of
security-bearing ones. Protocol: copy the file to scratch, mutate, run the named
tests, restore, assert `git diff` clean for that path. All runs in
`swift-crane@05b69b6` (byte-identical to `efe8672`); nothing was mutated in the
integration worktree.

| # | Mutation | Result |
|---|---|---|
| M1 | `sandbox.Resources.create_flags` stops appending `--no-share-skills` | **3 tests fail** — `test_resource_flags_disable_shared_skills_by_default`, `test_shared_skills_opt_in_is_the_only_difference`, `test_shared_skills_off_means_the_flag_is_passed_to_sbx`. Covered. |
| M2 | Both `store.revoke_context_tokens_for_worktree` call sites in `runtime.py` neutered | **0 tests fail.** Mis-targeted, not a finding — see M2′. |
| M2′ | *All* revocation neutered: both plural sites **and** `store.revoke_context_token` (`runtime.py:621`) | **3 tests fail**, one per call site — `test_rollback_revokes_every_capability` (621, rollback), `test_a_clean_sandbox_is_removed_and_its_row_marked` (288, cleanup), `test_resuming_supersedes_the_previous_row` (593, resume). Every revocation path is covered. |

### R6 re-run mutations (`f30c43b`)

| # | Mutation | Result |
|---|---|---|
| M-R6a | `core.runtime_fields` stops returning `{}` for `runtime == host`, so host rows gain runtime keys | **4 tests fail** — `test_a_host_agent_monitor_row_is_unchanged`, `test_a_mixed_grid_distinguishes_its_agents`, `test_a_host_row_contributes_nothing`, and `test_state_reports_the_panes_in_the_callers_workspace` |
| M-R6b | `runtime_aware_state` drops the `runtime != "host"` gate, so a host row with a stale `runtime_status` reports `stopped` | **3 tests fail**, incl. `test_a_host_agent_is_never_reported_stopped` |
| M-R6c | `events.runtime_identity` stops short-circuiting on a missing row, so a pane amux never registered gains runtime keys | **2 tests fail** — mine and `test_a_pane_with_no_execution_row_is_unchanged` |

The re-run also surfaced a **pre-existing latent flake in my own 2.4 tests**,
not caused by anything in `f30c43b`: `test_wait_is_released_by_a_sandbox_event`
failed once in a full run while passing 5/5 alone and 2/2 in-file. The fixture
caps `max_wait_s` at 2s to keep the cap test quick, so under full-suite load —
other files spawning real subprocesses — the server could expire the poll before
the main thread changed the state. Both threaded release tests now raise the cap
locally; the cap is incidental to what they assert. Three consecutive full runs
at 800 after the change.

M-R6a's first run was contaminated and is worth recording. I used `git stash`
to set the mutation aside, which stashed the *mutated* file along with my test
edit, so the pop reintroduced the mutant and the run had executed against
reverted tests. Re-run cleanly under the copy-mutate-restore protocol, the
result changed: my own guard did **not** fail, because `%2` had no execution
row at all, so `runtime_identity` returned `{}` at its `row is None` check and
never reached the code I mutated. My assertion was guarding the *no-row* case
while I described it as guarding host output. The test now covers three pane
shapes — sandbox row, host row, no row — and M-R6a/M-R6c each kill it for the
right reason. Two adjacent checks that look like one is the same class as
`events.py:623-624`.

M2 is worth keeping in the record. It looked like a vacuity finding and was
not: rollback revokes by token id, not by worktree, so the untouched call site
kept the test honestly green. A mutation that fails to fail is a claim about the
mutation before it is a claim about the test — checking which call site each test
covers is what separated the two, and reporting M2 as a finding would have cost
misty-panda real time.

---

## 3. `sandbox-agent-runtime` — 7 Requirements, 16 Scenarios

### R1. Sandbox execution is explicit and optional

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Existing command keeps host behavior | `runtime.HOST` (`runtime.py:28`), `HostRuntime.kind` (`runtime.py:125`), default in `core.spawn_agent_grid` (`core.py:314`), `cli._resolve_runtime` returns `None` for host (`cli.py:78`) | `test_host_grid_snapshot.py` — 5 golden tests pinning full tmux mutation order, pane metadata, hook wiring, worktree layout, registry rows and send-keys order, captured **before** the runtime refactor; plus `test_build_grid_defaults_to_the_host_runtime` | Make `_resolve_runtime` return a `SandboxRuntime` when `--runtime` is absent → every golden test fails on tmux call order and worktree layout |
| User selects sandbox execution | `SandboxRuntime` (`runtime.py:377`), `cli._resolve_runtime` (`cli.py:78-105`), and `cli._workspace_dir` resolving `spg`'s documented `-p` default from the registry before preflight sees it | `test_mixed_grid_creates_one_capped_sandbox_per_pane`, `test_the_sandbox_runtime_is_built_with_the_flags_as_given`; plus 5 tests added after note #78 finding 3: `test_spg_without_a_path_resolves_the_workspace_directory`, `test_spg_resolves_a_primary_checkout_not_an_agent_worktree`, `test_spg_with_an_explicit_path_still_wins`, `test_spg_without_a_path_or_a_registry_row_passes_none`, `test_spg_prefers_the_most_recent_repository_for_the_workspace` | Make `_resolve_runtime` ignore `--runtime docker-sandbox` and return `None` → both original tests fail (no `sbx create` recorded); revert `cwd` to `args.path` → 3 of the 5 `spg` tests fail (**executed, red-green verified at `68768c5`**) |
| Unsupported agent is rejected | `sandbox.SUPPORTED_AGENTS` (`sandbox.py:50`), rejection in `create_argv` (`sandbox.py:367`), preflight `agents` check | `test_preflight_refuses_an_unsupported_agent`, `test_unsupported_agent_is_named`, `test_create_argv_rejects_unsupported_agents`, `test_a_failed_grid_leaves_no_task_window` | Add `"shell"` to `SUPPORTED_AGENTS` → the three rejection tests fail; delete the preflight check but keep the `create_argv` one → the *before any pane* ordering tests fail |

### R2. Sandbox prerequisites are checked before mutation

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Preflight succeeds | `sandbox.preflight` (`sandbox.py:481`), `Preflight.ok`, `SandboxRuntime.preflight` | `test_preflight_passes_when_everything_is_in_place`, `test_raise_if_failed_is_silent_when_everything_passes` | Make one check return `ok=False` unconditionally → both fail |
| Preflight fails | `Preflight.raise_if_failed` (`sandbox.py:~465`), `Check.remediation` | `test_preflight_creates_nothing`, `test_missing_sbx_short_circuits_with_an_install_action`, `test_every_failure_carries_a_remediation`, `test_a_secondary_worktree_is_rejected_before_anything_external`, `test_uninitialized_policy_reports_init_and_never_runs_it`, `test_denied_port_reports_the_exact_allow_command` (+15 more in `test_sandbox_preflight.py`) | Make `raise_if_failed` return instead of raising → the ordering tests fail because creation proceeds; blank `Check.remediation` → `test_every_failure_carries_a_remediation` fails |

### R3. Each sandbox agent has an isolated and reproducible execution identity

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Sandboxed agent is created | `SandboxRuntime.prepare` (`runtime.py:416`), `sandbox.create_argv` (`sandbox.py:355`), `sandbox_name` (`sandbox.py:190`), `store.register_worktree(runtime=…)`, `sandbox_bootstrap.install` | `test_mixed_grid_creates_one_capped_sandbox_per_pane`, `test_each_agent_gets_its_own_branch_off_the_task_base`, `test_registry_rows_record_sandbox_identity`, `test_both_bootstrap_halves_run`, `test_each_agent_gets_its_own_capability` | Drop `--clone` from `create_argv` → adapter tests fail on exact argv; register with `runtime="host"` → `test_registry_rows_record_sandbox_identity` fails |
| Shared skills are not requested | `Resources.create_flags` (`sandbox.py:~240`), `share_skills` default `False` (`sandbox.py:218`) | `test_resource_flags_disable_shared_skills_by_default`, `test_shared_skills_opt_in_is_the_only_difference`, `test_shared_skills_off_means_the_flag_is_passed_to_sbx` | **Executed (M1): 3 tests fail.** Covered. |
| Partial grid creation fails | `_Acquired` (`runtime.py:203`), `SandboxRuntime.rollback`, `core._rollback` (`core.py:270`) | `test_sandbox_rollback.py` — 15 tests: `test_a_failed_second_sandbox_unwinds_the_first`, `test_rollback_releases_newest_first`, `test_rollback_revokes_every_capability`, `test_cleanup_failures_never_replace_the_original_error`, `test_a_rollback_that_itself_raises_is_reported_not_propagated`, `test_a_failed_grid_leaves_no_task_window` | Reverse the unwind order → `test_rollback_releases_newest_first` fails; swallow the original error and raise the cleanup one → `test_cleanup_failures_never_replace_the_original_error` fails. Revocation half **executed (M2′)** |

### R4. Sandboxed work is integrated through committed Git branches

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Sandbox branch integrates successfully | `worktree.integrate` sandbox path, `sandbox.git_remote` (`sandbox.py:203`), durable local ref | `test_a_committed_sandbox_branch_integrates`, `test_the_fetched_tip_is_kept_in_a_durable_local_ref`, `test_a_merge_commit_is_made_even_for_a_single_commit`, `test_host_and_sandbox_agents_integrate_in_one_pass` | Drop `--no-ff` → `test_a_merge_commit_is_made_even_for_a_single_commit` fails; skip the durable-ref write → its test fails |
| Sandbox branch conflicts | conflict abort + blocker note in `worktree.integrate` | `test_a_conflicting_sandbox_branch_aborts_and_blocks`, `test_one_unreachable_sandbox_does_not_stop_the_others` | Remove the `merge --abort` → the test fails on a left-behind conflicted index; skip the blocker note → fails on the missing note |
| Sandbox has no committed work | no-delta and never-committed branches in `worktree.integrate` | `test_a_branch_with_no_delta_reports_no_changes`, `test_uncommitted_sandbox_files_are_not_integrated`, `test_a_sandbox_that_never_committed_the_branch_is_reported`, `test_a_stopped_or_removed_sandbox_is_reported_not_guessed` | Report `ok=True` with a fabricated shortstat for an empty delta → the first two fail |

**Observed limitation, R4 (not a scenario failure).** `amux integrate <ws> <task>
--all` is one-shot per row: the first pass marks each agent record `merged`, and
`worktree.integrate` selects `status == "active"` (`worktree.py:289`), so a
second pass refuses with *"no active worktrees for task ..."*. clever-mole hit
this running the 6.3 integration on this change and finished the merge by hand
with `git`. R4's scenarios are all satisfied — the spec requires the record be
marked merged and says nothing about re-running — but the documented command
cannot be re-run after any further change lands.

**The adjacency, and its current state.** The same predicate was applied on two
different axes, and that is what made the original pair dangerous: with
`stop_task`/`clean_task` also filtering `status == "active"`, a merged row was
untouchable from *both* directions — integrate refused it and cleanup silently
skipped it — so its microVM was unreachable to amux entirely and only `sbx rm`
could recover it. That is the state clever-mole hit: `kw --clean --force`
reported success and left four VMs running.

**Half of it is now fixed**, verified at source rather than taken from the
report: `runtime.sandbox_rows` (`runtime.py:225-239`) selects on the *runtime*
axis, with the leak named in its own docstring — *"`status` answers 'was this
work merged'; `runtime_status` answers 'does a VM exist'"*. `worktree.py:289` is
the only place left where the merge axis gates anything.

**Measured, not read** (disposable repo, its own `XDG_STATE_HOME`, its own
`-L amux-probe` socket, `python -m amux.cli` from this tree — not the installed
binary, which predates the change and has no `doctor` subcommand):

| Observation | Result |
|---|---|
| Pass 1, two host agents, one with a commit and one without | both merged: `jolly-lemur` 1 commit, `olive-bear` **0 commits, "no changes"** — and *both* rows moved `active` → `merged` |
| Pass 2, `--all` | refuses: `amux: no active worktrees for task 't0' in workspace 'probe'`, rc=1 |
| Pass 2, `--agent olive-bear` | refuses identically |
| `kg --clean` on merged rows | succeeds, rows move `merged` → `removed` |
| `runtime.sandbox_rows` on a `merged` sandbox row | returns it — cleanup and stop do reach merged rows |

**Correction to an earlier claim in this document.** I wrote that the surviving
consequence is "you cannot re-integrate, but you are no longer stranded". That
is true of *microVMs* and false of *work*. A row is marked merged on the
attempt, not on having contributed anything — so an agent that had not committed
when someone ran `integrate` is marked merged with a zero-commit result, and its
later commits cannot be integrated by any amux command: `--all` and
`--agent <name>` both refuse, and the commit sits on its branch, absent from the
integration branch. Measured above. Recovery is a manual `git merge`.

So the accurate statement is: VMs are recoverable, and work committed after any
integrate pass is not — which makes integrating early, before teammates have
committed, the expensive mistake rather than integrating twice.

**Mechanism** (clever-mole, confirmed at source): `worktree.integrate` computes
`n_commits` and then calls `store.set_worktree_status(wt_id, "merged")`
unconditionally — `n_commits` never gates it — so a zero-commit agent is marked
merged, and the `status == "active"` filter at `worktree.py:289` forecloses it
permanently. Routed as a narrow fix: do not mark merged when `n_commits == 0`.

**Classification: a defect that no scenario covers — not an R4 scenario
failure.** Deciding this needed one fact rather than an argument. R4's no-delta
scenario requires that amux *"does not report those files as integrated and
explains whether the agent must commit or there is no branch delta"*, and the
output does exactly that: `0 commit(s), no changes`. The wrong thing is the
durable record, and `status` is surfaced **nowhere** user-facing — not
`utils.py`, not `monitor.py`, not the roster entries in `core.py`/`cli.py`, not
`sandbox_client.py`. Its only readers are `context_service.py:370`, internally,
to refuse a removed execution, and `integrate`'s own filter. So the record is
internal state, the scenario's stated `THEN` is met, and calling this a scenario
failure would overstate what the spec requires. The requirement sentence does
not fit either: *"MUST NOT treat uncommitted sandbox files as integrated work"*
— at pass 1 there were no files at all, committed or otherwise.

That makes it a gap in the specification as much as in the code: no scenario says
the record must stay active when there is no delta, and one should. **This
classification flips if the record ever becomes user-facing** — if `status`
reaches `ctx`, a list command or the monitor, then a permanent `merged` mark
*would* be reporting uncommitted work as integrated, and this becomes an R4
scenario failure. Worth re-checking whenever visibility work touches those
views.

Worth keeping both halves recorded, because as two separate quirks — "integrate
won't re-run" and "cleanup leaked VMs" — they read as unrelated, and the thing
that connected them was one predicate copied onto the wrong axis.

### R5. Sandbox lifecycle follows amux lifecycle without silent data loss

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Task is stopped without cleanup | `runtime.stop_task` (`runtime.py:~208`), `sandbox.stop` (`sandbox.py:412`), capability deliberately retained | `test_stop_task_stops_each_sandbox_without_removing_it`, `test_a_stopped_sandbox_keeps_its_identity_and_row`, `test_stopping_does_not_revoke_the_capability`, `test_a_later_spawn_reattaches_to_the_same_sandbox`, `test_resuming_checks_out_the_existing_branch_rather_than_creating_it` | Call `sandbox.remove` instead of `stop` → the first two fail; revoke on stop → `test_stopping_does_not_revoke_the_capability` fails (this one is an inverted assertion, so it is the mutation that proves it means something) |
| Clean preserves committed work | `runtime.clean_task` (`runtime.py:239-288`), tip preserved *before* removal, revocation at `runtime.py:288` | `test_the_committed_tip_is_preserved_before_the_sandbox_is_removed`, `test_a_clean_sandbox_is_removed_and_its_row_marked`, `test_the_host_remote_is_dropped_with_the_sandbox`, `test_force_still_preserves_the_committed_tip` | Reorder so removal precedes the fetch → the ordering test fails. Revocation half **executed (M2′): fails.** Covered |
| Clean encounters uncommitted work | dirty check in `runtime.clean_task`; CLI `--force` gate `cli._check_force` (`cli.py:163-169`) | `test_a_dirty_sandbox_is_refused_by_default`, `test_a_refused_removal_changes_nothing`, `test_one_dirty_sandbox_spares_the_others_too`, `test_every_dirty_sandbox_is_listed_at_once`, `test_an_unreadable_working_tree_counts_as_dirty`, `test_force_removes_a_dirty_sandbox`, `test_a_sandbox_that_refuses_to_go_keeps_its_row` | Treat an unreadable working tree as clean → `test_an_unreadable_working_tree_counts_as_dirty` fails; let `--force` work without `--clean` → the CLI guard test fails |

### R6. Runtime state is visible in existing amux views

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| User inspects a mixed installation — **`ctx` and list halves** | `core.runtime_fields` (`core.py:562-580`), `utils.context_to_string` runtime line (`utils.py:57-61`), shared shape via `sandbox_client.runtime_to_string` | `test_runtime_visibility.py` — 16 tests: `test_the_runtime_line_shape_is_exactly_the_agreed_one`, `test_the_host_renderer_uses_that_same_function`, `test_a_host_agent_gets_no_runtime_line`, `test_a_sandbox_row_contributes_its_runtime_identity`, `test_a_host_row_contributes_nothing`, `test_lifecycle_states_are_carried_through`, `test_a_degraded_state_is_marked_not_renamed` | Return the runtime keys for host rows too → `test_a_host_agent_gets_no_runtime_line` and `test_a_host_row_contributes_nothing` fail; change the line format → the shape test fails |
| **monitor half — RESOLVE** | `events.pane_states` now takes the registry in one query (`events.py:407`) and spreads `**runtime_identity(rows.get(pane))` into each entry (`events.py:436`); `runtime_identity` delegates to `core.runtime_fields` so the monitor and `ctx` name the fields identically (`events.py:442-454`); `runtime_aware_state` folds VM lifecycle into the pane state, gated on `runtime != host` (`events.py:457-476`); `AgentState` gains `stopped` (`events.py:20`) | `test_monitor_runtime.py` — 11 tests, incl. `test_a_sandbox_agent_is_distinguishable_in_the_monitor`, `test_a_host_agent_monitor_row_is_unchanged`, `test_a_pane_with_no_execution_row_is_unchanged`, `test_a_stopped_sandbox_reads_as_stopped_not_idle`, `test_a_host_agent_is_never_reported_stopped`, `test_the_monitor_and_ctx_agree_about_runtime_identity`; plus `test_state_reports_the_panes_in_the_callers_workspace` pinning all three pane shapes through `GET /v1/events/state` | **Three executed, all kill tests.** See §2 M-R6a/b/c |
| **monitor half — SHOW** | `tui/src/types.ts` (`stopped` in `AgentState`, with a comment that it has no event kind and comes from the execution row), `theme.ts` (`[STOP ]`, cyan, dim), `useAmuxState.ts:32` (`stopped: 0`, so the NaN bucket is gone), `Header.tsx` (`as const satisfies` + inline `Exclude` guard, additionally excluding `unknown` because it is the TUI's own fallback and legitimately has no header bucket) | The `Exclude` guard is the coverage: it is a compile-time assertion that every `AgentState` has a header bucket. No runtime test exists — `tui/` has no test runner, no test file, and nothing cross-checking `METRICS` against `STATE_STYLE` | Remove the `stopped` entry from `METRICS` → `tsc` fails naming the missing state. **Verified by clever-mole on the pinned TypeScript 5.9.3, by mutation not by claim.** Label: *implemented; state-bucket coverage compiler-enforced; **rendering never executed*** — type-checking the TUI is not running it, and nobody has run the monitor |

### R7. Prototype behavior is verifiable without a live provider session

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Automated sandbox tests run offline | `conftest.fake_sbx` (fake `sbx` on `PATH`, records argv, replays responses), `conftest.no_real_sbx` autouse guard, `conftest.no_sbx`, isolated `git_repo`/`git_factory` | `test_fixtures.py` — 25 tests including `test_the_guard_blocks_docker` and the fake-`sbx` record/replay tests; and the standing fact that all 771 tests pass with no Docker authentication and no provider credentials | Neuter the guard so an unresolvable name passes as "not real" → `test_the_guard_blocks_docker` fails (this exact defect existed and was fixed in `65055d1`, where the test had been passing *because* of the bug); point any sandbox test at the real `sbx` → the guard fires |

---

## 4. `sandbox-context-bridge` — 8 Requirements, 18 Scenarios

### R8. The amux context database remains host-only

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Sandbox requests context | `context_service.ContextService.build_context` (`context_service.py:~660`), `_context` route, `_notes`, `_event_state` — all reading the host store and returning JSON only | `test_context_matches_the_native_call`, `test_context_self_carries_the_runtime_identity`, `test_ctx_json_is_the_services_context_document` | Add a filesystem path or db handle to any response payload → `test_healthz_leaks_no_paths_or_identity` and the context-shape tests fail |
| Sandbox configuration is inspected | `sandbox.create_argv` mounts (`sandbox.py:355`), `sandbox_bootstrap` copies only the shim and config | `test_no_state_directory_or_tmux_socket_is_handed_to_a_sandbox`, `test_nothing_the_bootstrap_copies_comes_from_the_host_state_directory`, `test_no_sandbox_config_names_the_state_directory_the_db_or_the_tmux_socket` | Add `-v $XDG_STATE_HOME:/state` to `create_argv` → all three fail |

### R9. The context service exposes a minimal versioned interface

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Client performs supported context operations | 7 routes registered via `@route` (`context_service.py`), `_ROUTES` | `test_every_documented_operation_is_routed` (asserts the route set is *exactly* the seven), plus the per-endpoint suites | Add an eighth route → the route-set test fails; drop one → both it and that endpoint's suite fail |
| Client requests a host-control operation | authenticate-then-route in `ContextService.resolve` (`context_service.py:610`), no SQL/shell/tmux/lifecycle surface | `test_no_route_exposes_sql_shell_or_lifecycle`, `test_unauthenticated_callers_cannot_map_the_interface`, `test_sql_shaped_input_is_stored_as_text_not_executed`, client-side `test_a_host_only_command_is_refused_locally` | Route after authenticating → `test_unauthenticated_callers_cannot_map_the_interface` fails (proven by mutation M6 in an earlier session); interpolate a note field into SQL → the SQL-shaped-input test fails |
| Request exceeds a bound | `_read_body` size/type gate, `_text_field`, `_int_param`, `_cursor_param`, `_states_param`, `max_wait_s` cap | `test_oversized_body_is_refused_unread`, `test_a_lying_content_length_cannot_smuggle_a_large_body`, `test_an_oversized_note_is_refused_with_the_limit_and_the_length`, `test_out_of_bounds_query_parameters_are_refused`, `test_the_result_limit_cannot_be_raised_past_the_configured_maximum`, `test_wait_never_exceeds_the_configured_cap` | Read the body before checking `Content-Length` → the smuggling test fails; truncate an oversized note instead of refusing → the limit-and-length test fails |

### R10. Every sandbox request is authenticated and host-attributed

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Valid agent records an event | `_add_event` (`context_service.py:~1019`) passing `worktree_id`/`repo` from the capability, never from the body | `test_an_event_is_recorded_and_attributed_to_the_caller`, `test_a_body_cannot_attribute_an_event_to_another_pane`, `test_an_event_stays_attributed_after_the_pane_id_is_recycled`, `test_an_event_is_attributed_to_the_token_not_to_a_body_field` | Take `pane` from `request.body` → the spoofing test fails (executed in an earlier session: **it initially survived**, because the receipt was built from the identity while the store held the attacker's value; both the handler and the test were fixed in `80bf4ae`). Drop `worktree_id=` so the store resolves by pane → the recycled-pane test fails |
| Token is missing, invalid, expired, or revoked | `store_authenticator` (`context_service.py:~376`), `store.context_token_record` constant-time compare | `test_an_unknown_token_is_rejected`, `test_a_revoked_token_is_rejected`, `test_an_expired_token_is_rejected`, `test_unknown_expired_and_revoked_are_indistinguishable`, `test_v1_without_a_token_is_unauthorized`, `test_malformed_authorization_is_unauthorized`, `test_a_token_whose_execution_was_removed_is_rejected` | Return an `Identity` for an unknown token → most of the file fails; distinguish revoked from unknown in the message → the indistinguishability test fails (asserted as a set of size 1) |
| Agent requests inaccessible context | `require_scope` + `deny` (`context_service.py`), `require_permission`, pane-scoped agent notes | `test_a_foreign_workspace_is_refused`, `test_a_foreign_repository_is_refused`, `test_wait_refuses_a_pane_outside_the_scope`, `test_wait_refuses_a_pane_in_another_repository`, `test_an_agent_scoped_note_stays_private_on_the_scoped_route` | Make `require_scope` return unconditionally → the foreign-workspace/repository tests fail (executed in an earlier session, M1); widen the agent-note query to drop the pane filter → the privacy tests fail |

### R11. Context visibility matches native amux semantics

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Task-scoped note is shared | `_notes` → `store.visible_notes`/`query_notes` with the caller's repo (`context_service.py:~840`) | `test_notes_match_the_native_visible_notes` (compares against the same store call made directly), `test_a_posted_note_is_visible_to_a_teammate_and_to_the_native_path`, `test_notes_are_filtered_by_repository`, `test_a_sibling_task_is_readable_but_its_private_notes_are_not` | Drop the `repo=` argument → `test_notes_are_filtered_by_repository` fails; reimplement visibility in the service instead of calling the store → the native-equivalence test fails |
| Agent-scoped note remains private | `pane=caller.pane if scope == "agent"` in `_notes`; `store.visible_notes` rules | `test_an_agent_scoped_note_is_invisible_to_a_teammate` (with a positive control — a shared note the teammate *must* see), `test_an_agent_scoped_note_stays_private_on_the_scoped_route` | Force every posted note to `scope="task"` → the privacy test fails (**executed in an earlier session; before the positive control was added it would have passed on an empty list**) |

### R12. Sandbox events update host coordination state

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Sandbox transitions to needs-input | `_add_event` → `events.publish_state(pane, state, socket)` after the store commit | `test_a_needs_input_event_resolves_the_pane_to_needs_input`, `test_every_event_kind_maps_to_the_native_state`, `test_the_pane_option_is_set_on_the_socket_the_row_names`, `test_wait_is_released_by_a_sandbox_event` | Publish to `config.socket` instead of `identity.socket` → the socket test fails; skip `publish_state` → the needs-input and waiter tests fail |
| Client resumes after interruption | `_cursor` (never rewinds), `_events_after`, store-level `after=` cursor | `test_wait_resumes_from_a_cursor_without_repeating`, `test_wait_events_are_in_identifier_order`, `test_a_cursor_walk_sees_every_note_exactly_once`, `test_the_cursor_is_the_highest_note_seen` | Return `None` for an empty page instead of the incoming cursor → the no-rewind assertions fail; order by `ts` instead of `id` → the identifier-order and exactly-once tests fail |

### R13. Sandbox clients preserve the context command experience

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Agent runs a supported command inside a sandbox | `sandbox_client.py` — `ctx`, `notes`, `note`, `event emit\|state\|wait` | `test_ctx_json_is_the_services_context_document`, `test_ctx_human_output_keeps_the_native_shape`, `test_a_host_agents_ctx_output_is_byte_identical_to_the_native_render`, `test_notes_human_output_matches_the_native_columns`, `test_note_posts_text_scope_and_kind_and_prints_the_receipt` (+46 more in `test_sandbox_client.py`) | Change a column in the human render → the byte-identical and native-columns tests fail. **Also proven live** — note #63, a real sandbox against a real service |
| Agent runs a host-only command inside a sandbox | host-control refusal in `sandbox_client.py` | `test_a_host_only_command_is_refused_locally` family; **proven live**, note #63: `amux integrate` inside a real microVM exits 2 with the full boundary message | Make the client forward an unknown command to the service instead of refusing → the local-refusal tests fail |

### R14. Service and credential lifecycle fail closed

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Context service becomes unavailable | client transient-failure path; service side `probe_health` treating every failure as no answer (`context_service.py:1570`) | `test_a_probe_treats_every_failure_as_no_answer`, `test_something_else_on_the_port_reads_as_unresponsive`, client unreachable-service tests; **proven live**, note #63: `amux ctx` exits 1 under a real policy denial with no fallback and no mention of a database | Narrow the probe's `except` back to `(OSError, ValueError)` → the non-HTTP cases fail on `BadStatusLine` (**executed, red-green verified, `76c9bb9`**); add a local-shadow-database fallback to the client → its no-fallback tests fail |
| Sandbox is removed | revocation at `runtime.py:288` (cleanup), `:593` (resume), `:621` (rollback) | `test_a_clean_sandbox_is_removed_and_its_row_marked`, `test_resuming_supersedes_the_previous_row`, `test_rollback_revokes_every_capability`, `test_removing_a_sandbox_revokes_its_capabilities` | **Executed (M2′): all three revocation sites covered, one test each.** Covered |
| Logs are reviewed | `redact` + `RedactingFormatter` (`context_service.py:~127`), access line carrying ids only, bootstrap diagnostics redaction | `test_the_plaintext_token_is_absent_from_the_logs`, `test_request_logs_never_carry_the_token`, `test_the_log_file_is_written_through_the_redacting_formatter`, `test_the_plaintext_token_is_absent_from_the_database`, `test_a_diagnostic_that_would_have_echoed_the_token_is_redacted` | Log `request.body` in the access line → the never-carry-the-token and no-bodies assertions fail; make `redact` a no-op → the formatter test fails. Note the database test needed a positive control first: the capability lives in `-wal`, so searching `context.db` alone asserted nothing |

### R15. Native behavior remains available during the prototype

| Scenario | Implementation | Test | Mutation |
|---|---|---|---|
| Context service is not needed for a host grid | native commands call `store`/`events` directly; the service is only reached by `--runtime docker-sandbox` | `test_host_grid_snapshot.py` (5 golden tests), `test_build_grid_defaults_to_the_host_runtime`, `test_host_grids_are_unaffected_by_the_unwind_path`, `test_context_service_is_not_started_by_a_host_spawn` behaviour implied by the golden captures | Make `core` or `events` route through `context_service` → the golden tests fail on added calls; make `HostRuntime.prepare` require a healthy service → every host test fails |

---

## 5. Honest limits — what could not be proven offline

Three rows moved **out** of this section on note #63's live evidence: the
host-only-command refusal, the service-unavailable path, and
`amux event emit` staying silent and exiting 0 under a real denial.

What remains genuinely unproven without a live provider session, for 6.4:

| Requirement | What is unproven offline | Why |
|---|---|---|
| R3 — sandboxed agent is created | That a real `sbx create --clone` produces a working clone with the branch checked out and a functioning agent | Fake `sbx` proves the exact argv and the parsing of its output; it cannot prove Docker's behaviour |
| R5 — stop/reattach preserves VM state | That a stopped microVM resumes with its filesystem and credentials intact | Only a real VM has state to preserve |
| R7 — the measurements | Spawn-to-prompt latency, four-agent CPU/memory, disk growth | Measurements, not assertions |
| R2 — Docker authentication | That an unauthenticated host fails the way the fake says | Requires signing out |

Everything else in this map is proven offline.

## 6. Expected to go stale first

1. ~~**F1/F2** — if misty-panda implements the monitor half, R6's rows change and
   F3 resolves with them.~~ **Happened**: fixed in `8786f6c`/`dc487a7`, R6 rows
   re-run against `f30c43b`, F3 resolved with them.
1. The cleanup-leak fix (`stop_task`/`clean_task` selecting `status == "active"`
   and so leaking every microVM of an integrated task) is still outstanding, and
   it lands in `runtime.py:288`/`:593` — the same lines several R5 and R14 rows
   cite.
2. Any row citing `runtime.py` line numbers (`208`, `239-288`, `593`, `621`) —
   these are the newest code in the change.
3. `cli.py:163-169` (`_check_force`) — landed in 5.3, the most recent CLI edit.
4. The `Makefile` row implied by R3's "working context client": clever-mole is
   escalating the pipenv/uv question, and happy-deer's
   `test_the_build_ships_the_shim_as_data` is the guard for whatever lands.

## 7. Open questions for owners

| # | Question | Owner |
|---|---|---|
| Q1 | Is the monitor half of R6 intended to be out of scope for the prototype, or is F1 a gap to close? Either way the deviation needs recording. | misty-panda, via clever-mole |
| Q2 | Should `stopped` become an `AgentState` member, or is `runtime_status` the intended home — in which case the monitor needs to read it (F1/F2 are one fix). | misty-panda, via clever-mole |
| Q3 | `GET /v1/events/state` returns `pane_states` verbatim per the pinned wire contract. If `pane_states` gains runtime fields, that is a wire change happy-deer's client should expect rather than discover. | happy-deer + me |
