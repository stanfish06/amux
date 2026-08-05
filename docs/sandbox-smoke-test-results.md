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
