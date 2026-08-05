# Sandbox smoke test — run of 2026-08-05

Task 6.4. Executed from `docs/sandbox-smoke-test.md` against a real
authenticated Docker Sandboxes host, on a disposable repository. Findings that
changed the guide are folded back into it; this file is the record of what was
observed.

```
Date: 2026-08-05            Host: macOS (Darwin 25.5.0), arm64
sbx version: v0.37.1 2d4f3244        Policy profile: local-policy (balanced)
amux commit: efe8672 (integration) merged into amux/amux/docker/happy-deer
Suite before starting: 771 passed
Images: claude-code-docker (Claude Code 2.1.221), codex-docker (codex-cli 0.146.0)
```

## What was verified working

| # | Behaviour | Evidence |
|---|---|---|
| 1 | Preflight is read-only and actionable | 7 ok, 1 `[FAIL] network-policy: Denied: localhost:47317` with the exact fix; exit 1; "amux changes nothing on its own" |
| 2 | Loopback-only service | off-loopback connect refused; `/healthz` ok; unauthenticated `/v1/context` → **401** |
| 3 | One capped sandbox per pane | 4 sandboxes for a 2×2 `-a claude:2 -a codex:2` grid, names `amux-<ws>-<task>-<agent>-<repohash>` |
| 4 | Clone isolation holds | agent wrote `CANARY` to its repo path inside the VM; **host checkout unaffected** |
| 5 | No host state reachable | no `~/.local/state/amux`, no `context.db` anywhere, no tmux socket, no amux/tmux env |
| 6 | Capability delivered correctly | `context.json` `agent:agent 600`, readable by the agent; shim `agent:agent 755` |
| 7 | Boundary refusal, live | `amux integrate` in-VM → exit 2 with the host-only message |
| 8 | Context exchange, 2 sandboxes | A published a task note; B read it, attributed to `claude` |
| 9 | Agent-scoped privacy | A's `--scope agent` note invisible to B, visible to A |
| 10 | Roster + runtime line | all four agents with branches; `runtime: docker-sandbox running <name>` |
| 11 | Sandbox event → host tmux | in-VM `event emit notify` → host pane option `needs-input` → host `event state` |
| 12 | Cross-boundary wait | B blocked on A; A's `stop` released it with `idle` rc=0; emit ~0.4s during the poll; reproduced twice |
| 13 | Committed-branch integration | `--no-ff` merge of `refs/amux/sandboxes/<box>/<branch>`; 1 commit, 1 file changed |
| 14 | Uncommitted work excluded | dirty file → "0 commit(s), no changes"; absent from the integration branch |
| 15 | No secrets in logs or db | 0 tokens ≥30 chars in the service log; 5/5 `context_tokens` rows are sha256 |
| 16 | Isolation of the run itself | 0 `sbxsmoke` rows in the live `context.db`; live tmux sessions untouched |

## Measurements

```
spawn, 4-agent mixed grid (cold-ish, images already pulled): 48.2 s for `spw` to return
spawn-to-prompt, 1 agent, warm:                             14.4 s, measured from pane
                                                            output (NOT `event state`)
per-microVM host footprint, 4 VMs, agents idle:
    rss   939 / 817 / 760 / 757 MB      total 3274 MB      mean 818 MB
    cpu   0.2 / 1.9 / 2.3 / 0.2 %       total 3.8 %
disk growth, 4 sandboxes:                9489 MB           mean 2372 MB per sandbox
disk after teardown:                     11532 MB reclaimed (more than consumed: `sbx rm`
                                         also collected earlier probe sandboxes)
sandbox -> host context call latency:    0.42 – 0.80 s per command, 6/6 succeeded
```

`sbx ls --json` exposes **no** cpu or memory fields — one of the change's open
questions, now answered. Per-sandbox figures above come from the host process
table instead.

## Findings

### 1. Cleanup silently does nothing for an integrated task — leak on the normal path

`integrate` marks each worktree row `merged`. Both `runtime.stop_task` and
`runtime.clean_task` only act on rows whose `status == "active"`. So after
integrating, `kw --clean --force` **printed `killed workspace 'sbxsmoke'` and left
all five microVMs in place** — four still `running`. Nothing warned.

The sequence that hits this is the intended workflow: spawn → work → commit →
integrate → tear down. Cost per orphan is ~818 MB RSS and ~2.4 GB disk, and only
`sbx rm -f` clears it. `kg` without `--clean` is affected the same way: the tmux
window went away, the VM stayed `running`, and `runtime_status` still read
`running`.

Not mine to fix (`runtime.py`). The narrow fix is for the stop and clean paths to
select on `runtime_status`/sandbox presence rather than on the merge lifecycle —
`status` tracks whether work was merged, which is a different question from
whether a VM exists.

### 2. `integrate` is one-shot per worktree row

Once a row is `merged`, `integrate` refuses with "no active worktrees", including
with an explicit `--agent`. An agent that commits more after a first integrate
cannot integrate again. Pre-existing host behaviour, not a sandbox regression, but
the smoke test is where it becomes visible — and it is what made finding 1 bite.

### 3. `spg --runtime docker-sandbox` fails preflight without `-p`

`[FAIL] repository: no git repository`, even though the workspace has one.
`_cmd_spg` passes `cwd=args.path`, which is `None` when `-p` is omitted, and the
workspace-directory default is resolved later inside `spawn_agent_grid` — after
preflight has already run. `spw` is unaffected because its `-p` defaults to
`os.getcwd()`. Fix belongs where the path is resolved, before preflight.

### 4. A fresh sandbox is logged out unless host credentials were set first

`sbx create` warns "no OpenAI credentials available. Codex will start logged-out"
and the same holds for Claude. Claude panes reached a usable prompt showing "Not
logged in"; Codex panes sat at a sign-in menu. Everything not requiring a model
call still worked — so this blocks exactly one class of measurement, below.

### 5. What could not be measured

- **Hook-driven state transitions.** Hooks fire on agent activity, and a
  logged-out agent has none. Rows 11 and 12 above were driven by invoking the shim
  through `sbx exec`, which exercises the shim → service → tmux path but not the
  agent → hook → shim path. Combined with the separately-known Codex hook-trust
  requirement (`--dangerously-bypass-hook-trust`), no hook has yet been observed
  firing from a real agent turn in a real sandbox.
- **Merge-conflict integration.** Not provoked; time went to findings 1–3.
- **Reattachment to the same sandbox id.** `kg` did not stop the VM (finding 1),
  so the stop/reattach cycle could not be exercised as specified.
- **Four-agent behaviour under load.** All four agents were idle. The 3.8% CPU
  figure is an idle-cost measurement, not a working-agents one.

### 6. Two apparent bugs that were my own harness

Recorded so nobody re-derives them. An early run showed a concurrent event write
starving for 45 s and a waiter timing out. Both were artefacts: my 5-minute tool
timeout killed a process group and left an orphaned `sbx exec`, which the next
measurement then blocked behind. With `timeout` wrappers the same test passes
twice in a row (row 12). Verified separately that concurrent `sbx exec` into
different sandboxes really do run in parallel, and that host requests answer in
~2 ms while a sandbox long-poll is in flight.

## Machine policy changed, and restored

The smoke test needs the sandbox to reach the host service, which `balanced`
denies by default. Ran the remediation amux itself prints:

```
sbx policy allow network localhost:47317
  -> rule 37656dcc-0713-402c-95ab-3559efac521b, allow count 192 -> 193
```

Removed afterwards:

```
sbx policy rm network --id 37656dcc-0713-402c-95ab-3559efac521b
  -> allow count back to 192; `sbx policy check network localhost:47317` = Denied
```

The host is back to its pre-test policy. Anyone running the sandbox runtime for
real has to make this decision themselves — amux prints the command and does not
run it.

## Teardown

`sbx ls --json` empty; smoke tmux server gone; disposable repository and isolated
state directory removed; live `context.db` unchanged at 26 worktree rows with zero
`sbxsmoke` rows; live tmux sessions intact.

---

# Re-run of 2026-08-05 — the two rows finding 1 blocked

Scope: teardown-after-integrate and reattachment only. Everything else in the 16
above stands unrepeated. Run on the integration tree containing misty-panda
`49b222d` (select sandboxes by VM presence, not merge status) and `dc487a7`, with
800 tests passing. Same isolation discipline: own `XDG_STATE_HOME`, own tmux
socket, disposable repo outside every amux worktree.

## Finding 1 is fixed

Reproduced the exact sequence — spawn 2 agents, commit in one, `integrate` (both
rows became `status=merged`, `runtime_status=running`), then
`kw --clean --force`. Result: **`sbx ls --json` empty, both microVMs actually
gone**, and the registry now reads `status=merged` with
`runtime_status='removed'` — each axis answering its own question.

Also verified in the same pass: host `sandbox-*` remotes dropped (0 remaining),
2/2 capabilities revoked, the integration branch keeps the merged work, and the
committed tip survives as `refs/amux/sandboxes/<box>/<branch>` after its VM is
destroyed.

`kw` a second time exits 1 with "workspace not found" — the tmux session is gone,
so the command has nothing to address. Idempotent at the sandbox level (nothing
left to remove), not re-runnable as a command.

## Reattachment: the stop half works, the reattach half is unreachable

`kg` without `--clean` now **stops** the VM and leaves it inspectable at the same
id, with `status=active` and `runtime_status='stopped'`. That is the half that
could not be measured at all before.

The reattach itself never happens through the CLI. Sandbox identity is
`sandbox_name(workspace, task, agent_name, repo)`, and agent names are drawn by
`core.random_name(_taken_names(session))` — from **live tmux panes only, never the
registry**. After `kg` the prior name is neither taken nor preferred, so a respawn
picks randomly from ~1600 combinations:

```
before kg:   amux-...-t0-ruby-gecko-...   id ce5e6670-...  running
after kg:    same name, same id,                          stopped
after spw:   amux-...-t0-dusty-deer-...   id 94f1cb9f-...  running   <- NEW VM
             amux-...-t0-ruby-gecko-...   id ce5e6670-...  stopped   <- orphaned
```

The mechanism itself is correct, and that distinction matters — driving the
runtime's own lookups directly, `_prior_row` **and** `sandbox.find` both resolve
for the stopped sandbox, so the reattach branch would fire if a pane were ever
given that agent name. Nothing steers one there.

## Three further findings from the same two steps

**Cleanup cannot preserve the tip of a stopped sandbox.** The durable-ref fetch
needs a running VM: a sandbox cleaned while `running` got its ref, one cleaned
while `stopped` got none, and the failure surfaced only as a bare
`fatal: Could not read from remote repository` with no mention of which sandbox or
that its tip was unsaved. With `--force` that is a data-loss path. Confirmed the
commit was intact inside the VM at the time, so it is unpreserved rather than
lost — until the VM is removed.

**Non-force cleanup protects the work but strands the VMs.** `sbx rm` without
`-f` refuses a clone-mode sandbox, so `kw --clean` left both VMs in place and
correctly did **not** mark the rows removed. But it printed
`killed workspace` and exited **0**, and it killed the tmux session anyway — after
which `kw` reports "workspace not found" and amux can no longer address those
sandboxes at all. Only `sbx rm -f` clears them.

**A refused removal restarts a stopped VM.** `purple-fox` was `stopped`; after the
failed cleanup `sbx ls` reported it `running` while the registry still said
`stopped`, so the two disagree until something re-syncs.

## The monitor, run against a sandbox for the first time

It starts and works (`tui/` needed `npm install && npm run build`; both clean).
The tree renders workspace → task → agent, and **pane preview shows the agent's
real terminal from inside the microVM**.

Sandbox identity renders *only if the `amux` on PATH is current*. The TUI shells
out via `execFile` to `amux`, and this host's installed binary is an older frozen
build whose `event state --json` stops at `last_event`. With that binary the agent
detail shows no runtime; with a current `amux` first on PATH the same panel shows
`Runtime: docker-sandbox  Status: running  Sandbox: amux-<ws>-<task>-<agent>-<hash>`.
So verifying TUI work from a checkout can silently measure the installed build.

Two rendering observations, neither about sandboxes: labels and values collide
without a gap (`Task: t0Workspace:sbxsmoke5`, and the runtime row wraps
mid-word), consistent with the known Ink `Fragment`/`gap` problem. And a stopped
sandbox is **absent** from the tree rather than badged — `kg` removes the pane, and
the tree is built from panes, so there is no row for a stopped-state marker to
attach to. A live pane whose VM was stopped out-of-band still renders `[IDLE ]`,
because pane state comes from `@amux_state`, which nothing updates when the VM
stops outside amux.

## A leaked daemon of mine, and whether it touched any of this

`context-service start` detaches on purpose, and this run's service (pid 4259)
survived to the end and squatted the default port 47317, failing two of
swift-crane's tests. My fault, same class as the orphaned `sbx exec` in run 1: a
cleanup gap in my own procedure, not a product defect. Stopped through
`context-service stop`, port confirmed free.

It did not affect any measurement, for two independent reasons. Nothing in this
re-run went through the service — integrate, cleanup, registry state, `sbx ls`,
durable refs and reattachment are all host-side — and the service was only needed
for preflight's health check to pass. And it was *my* run's service on *this*
run's isolated database: `context-service start` reported pid 4259 itself, with
47317 verified free beforehand, so nothing else was adopted. The run-1 resource
figures are also unaffected: they were taken after run 1's service was stopped,
and per-VM attribution greps `containerd-shim-nerdbox-v1`, which a Python service
does not match.

It also exposed a real test-isolation defect in swift-crane's file — two
lifecycle tests exercised the flagless `start` path against the global default
port — which they fixed with a private port and an unconditional teardown, and
both now pass with the squatter still listening. Better evidence than removing it
would have been.

## Teardown

`sbx ls --json` empty; policy rule `92f2c973-0138-4a79-8032-048f05582b80`
(localhost:47317) added and removed, allow count 192 → 193 → 192 with
`sbx policy check` back to `Denied`; context service stopped and port free; smoke
tmux server killed; disposable repo and isolated state removed; live `context.db`
unchanged at 26 worktree rows with zero `sbxsmoke*` rows; live tmux sessions
intact.
