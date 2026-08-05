# Docker Sandbox smoke test

A manual, end-to-end verification of the `docker-sandbox` runtime on a real
authenticated host, run against a **disposable repository**. It exists because
the automated suite deliberately never touches Docker: every `sbx` call in the
tests is a fake, so nothing in CI can tell you whether the real backend works.

Run it top to bottom and fill in the [recording sheet](#recording-sheet) as you
go. Steps are ordered so that anything which can fail cheaply fails first.

- **Time:** about 45 minutes, most of it waiting on sandbox creation.
- **You need:** a macOS or Linux host with Docker Sandboxes installed and signed
  in, and roughly 15 GB of free disk (measured: ~2.4 GB per sandbox).
- **Provider credentials must be set on the host first**, or every agent comes up
  logged out and no agent-driven step can run:
  `sbx secret set -g openai --oauth` (and the Claude equivalent). A sandbox
  created without them still spawns, installs its shim and exchanges context — but
  its agent sits at a login prompt, so hooks never fire and pane state never
  changes by itself.
- **You do not need:** any knowledge of the amux internals. Where a step depends
  on something subtle, the step says so.

---

## Safety rules

Read these once. Two of them can destroy work.

1. **Use a throwaway repository. Never a real checkout, and never this one.**
   Step 1 creates one. The amux repository is where the agents doing this work
   live; pointing a sandbox grid at it puts branches and worktrees into the
   checkout that is coordinating you.
2. **`sbx rm -f` discards the sandbox's filesystem**, including commits that
   were never fetched to the host. The `-f` in this guide appears only in
   cleanup steps, after the branch tip has been fetched. Do not add it earlier
   to get past an error — record the error instead.
3. **`sbx policy init` is a one-time, host-wide decision.** It is not scoped to
   amux and it is not undone by cleanup. Step 0 asks you to make it deliberately.
4. Sandboxes hold live provider credentials. Treat a sandbox you cannot account
   for as you would a stray shell with your tokens in it: `sbx rm` it.
5. **Isolate the run from your live amux state.** The smoke test writes registry
   rows, notes and events, and creates tmux sessions. Against the default state
   that is the same `context.db` and the same `amux-root` server your real work
   uses. Give it its own of both:

   ```sh
   export XDG_STATE_HOME=/tmp/amux-smoke-state   # its own context.db + worktrees
   alias sm='amux -L amux-smoke'                 # its own tmux server
   ```

   Every `amux` below means `sm`. Confirm before spawning anything:

   ```sh
   python3 -c 'from amux import store; print(store.DB_PATH)'   # must be under /tmp
   ```

---

## Step 0 — Preconditions

### 0a. Which tasks must have landed

This guide describes the finished runtime. Several steps need CLI surface that
may not exist yet in the branch you are testing. Check before you start, so you
find out now rather than in step 6:

| Step | Needs | Check |
|---|---|---|
| 4, 5 | 5.5 — `--runtime`, `--cpus`, `--memory`, doctor | `amux spw --help \| grep runtime` |
| 3 | 2.5 — context service lifecycle | `amux --help \| grep -i context` |
| 9 | 5.1 — sandbox branch integration | `amux integrate --help` |
| 10 | 5.2, 5.3 — stop, reattach, cleanup | `amux kg --help \| grep -i clean` |

If a command is missing, **do not improvise an equivalent** — run the steps you
can, and record the rest as `blocked: <task> not landed`. A guide that reports
"unverified" is useful; one that reports a substitute you invented is not.

### 0b. Docker Sandboxes is installed and supported

```sh
sbx version
```

amux requires **0.37.0 or newer** (`sandbox.MIN_VERSION`). Record the version:
the `sbx` CLI is evolving, and a future failure elsewhere in this guide is much
easier to interpret against a known version.

```sh
sbx diagnose -o json | tee /tmp/sbx-diagnose-before.json
```

Every check should pass. A failing `login`/`auth` check means step 5 will fail
with a credential error rather than anything to do with amux.

### 0c. Initialize the global network policy

**This is the one-time host-wide decision.** No sandbox will start without it,
and amux will not make it for you:

```sh
sbx policy ls          # errors if uninitialized
sbx policy init balanced
```

`balanced` is the right choice for this test: `allow-all` would make step 7
(blocked-network diagnostics) vacuous, and `deny-all` requires an allow rule for
every step.

> If `sbx policy ls` already succeeds, policy was initialized earlier. Record
> which profile is active — it changes what step 7 should show.

### 0d. If you are testing a packaged amux, check the shim shipped

Skip if you are running from a source checkout. Otherwise do this **before**
anything else, because it is invisible to the automated suite by construction.

A packaged amux is a PyInstaller bundle, which compiles amux's modules into the
executable and ships no `.py` files. But spawning a sandbox copies
`sandbox_client.py` into the microVM *as a file*, so the build has to ship that
one as data (`--add-data src/amux/sandbox_client.py:amux`). Without it every
sandbox spawn dies at shim installation.

```sh
# onedir (macOS default): the shim must be there as a real file
ls -l dist/amux/_internal/amux/sandbox_client.py

# either layout: ask amux itself, which resolves it the same way spawning does
python3 -c 'from amux import sandbox_bootstrap as sb; print(sb.client_source())'
```

A `BootstrapError` naming `--add-data` means the build is mis-packaged, not that
the sandbox runtime is broken. Two automated tests cover the halves of this — the
resolver's frozen branch, and the Makefile carrying the flag — but neither can
prove a *built* binary works, which is why it is a step here.

### 0e. Record the baseline

You cannot measure growth without a before. Take these **now**, with no
sandboxes running:

```sh
sbx ls --json
df -k / | tail -1                                  # disk; see note below
ps -Ao pid,pcpu,rss,comm | grep containerd-shim-nerdbox   # per-VM footprint
```

`docker system df` is **not** usable here: Docker Sandboxes ships no `docker`
CLI, and there is no Docker Desktop data directory to size. Use `df -k /` for
disk. Do not use `vm_stat` "pages free" for memory — macOS compresses and
reclaims, so the delta is meaningless. Each microVM is one host
`containerd-shim-nerdbox-v1` process, so its RSS *is* the per-sandbox figure.

---

## Step 1 — Create the disposable repository

```sh
export SMOKE=/tmp/amux-sbx-smoke
export WS=sbxsmoke
export TASK=t0
export PORT=47317

rm -rf "$SMOKE" && mkdir -p "$SMOKE" && cd "$SMOKE"
git init -q
printf 'smoke test\n' > README.md
git add README.md
git -c user.email=smoke@amux.invalid -c user.name=smoke commit -qm "initial commit"
git log --oneline
```

The initial commit matters: amux skips worktree isolation in a repository with
no commits, which is a different code path and not the one under test.

`$PORT` is the context service default (`context_service.DEFAULT_PORT`). If you
run it elsewhere, substitute throughout.

---

## Step 2 — Probe the agent images

Do this **before** creating a grid. It answers questions that the automated
tests could not, and it is cheap to redo if it fails.

### 2a. Where does agent configuration actually live?

The hook adapters in `sandbox_hooks.py` were written against verified *formats*
but **assumed** *locations* — `AgentHooks.paths_are_assumed` is `True` precisely
because no image could be inspected when they were written.

```sh
sbx create --name probe-claude claude "$SMOKE"
sbx exec probe-claude sh -lc 'echo "HOME=$HOME"; ls -la "$HOME"'
sbx exec probe-claude sh -lc 'cat "$HOME/.claude/settings.json" 2>/dev/null || echo "ABSENT"'
sbx rm -f probe-claude
```

```sh
sbx create --name probe-codex codex "$SMOKE"
sbx exec probe-codex sh -lc 'echo "HOME=$HOME"; codex --version'
sbx exec probe-codex sh -lc 'cat "$HOME/.codex/hooks.json" 2>/dev/null || echo "ABSENT"'
sbx exec probe-codex sh -lc 'cat "$HOME/.codex/config.toml" 2>/dev/null || echo "ABSENT"'
sbx rm -f probe-codex
```

Record `HOME`, each file's presence, and the Codex version. Then re-record the
fixtures per
[`tests/test_sandbox_client_hook_fixtures/README.md`](../tests/test_sandbox_client_hook_fixtures/README.md):
overwrite the `*_template*` files, correct `settings_relpath` / `enable_relpath`
if a location differs, set `paths_are_assumed=False`, and re-run
`pytest tests/test_sandbox_client_hooks.py`. **Failures there are findings about
the image, not test breakage** — write them down before fixing anything.

If the probed Codex is older than `CODEX_HOOKS_MIN_VERSION` but still ships
`hooks.json`, lower that constant to the probed version. It is set to the
earliest version actually verified (0.146.0), so it is deliberately too high
rather than too low.

### 2b. Does an untrusted Codex hook fire? — the open question

Codex records a `trusted_hash` per hook entry in `config.toml`'s
`[hooks.state]`. amux does not write those: what is hashed is undocumented, and
a wrong hash is worse than an absent one. So nobody knows what Codex does with a
hook it has never trusted, and **a headless sandbox cannot answer an approval
prompt.**

This is a measured step, not a footnote. Until it is answered, the
documentation must not claim Codex/Claude parity.

```sh
sbx create --name probe-trust codex "$SMOKE"
# install a hook that only proves it ran
sbx exec probe-trust sh -lc 'mkdir -p "$HOME/.codex"'
sbx exec probe-trust sh -lc 'cat > "$HOME/.codex/hooks.json" <<JSON
{"hooks":{"UserPromptSubmit":[{"matcher":"","hooks":[{"type":"command","command":"touch /tmp/hook-fired","timeout":10}]}]}}
JSON'
sbx exec probe-trust sh -lc 'printf "\n[features]\nhooks = true\n" >> "$HOME/.codex/config.toml"'
```

Now attach interactively, send one prompt, and watch what happens:

```sh
sbx run --name probe-trust
# type any prompt, e.g. "say hi", then observe:
#   - does Codex ask you to approve or trust the hook?
#   - after the turn, does /tmp/hook-fired exist?
```

```sh
sbx exec probe-trust sh -lc 'ls -l /tmp/hook-fired 2>/dev/null || echo "DID NOT FIRE"'
sbx exec probe-trust sh -lc 'grep -A3 "hooks.state" "$HOME/.codex/config.toml" || echo "no trust state"'
sbx rm -f probe-trust
```

Record **one** of these three outcomes:

| Outcome | What it means |
|---|---|
| Fired, no prompt | Codex sandbox state is genuinely at parity. Trust is advisory. |
| Prompted, then fired | Degraded in practice: a headless sandbox cannot approve. The version fallback in `install_hooks` is masking a different cause, and `missing_kinds` should be driven by this instead. |
| Silently skipped | Same as above, and worse — nothing reports it. Sandboxed Codex state is unusable until amux can write a trusted hash or Codex offers a pre-trust mechanism. |

If the answer is anything but the first, **stop and report it** before running
step 5 with Codex. The rest of the guide will otherwise show a Codex sandbox
that looks permanently idle, and you will spend the afternoon debugging amux.

---

## Step 3 — Start the context service

The service is host-only and the sole context path for a sandbox. It owns
`context.db`; nothing here mounts a database into a VM.

`amux context-service` takes `serve`, `start`, `status` and `stop`. `start` is
idempotent — sandbox preflight calls it on every spawn — so running it twice is
not an error, and `serve` runs in the foreground of another terminal when you
want to watch it. Both accept `--port`; the default is 47317.

```sh
amux context-service status || amux context-service start
amux context-service status
curl -sS "http://127.0.0.1:$PORT/healthz" | tee /tmp/healthz.json
```

If `status` reports a stale run file, `start` clears it. If it reports a live
pid that answers nothing, it says so and refuses to start a second service
beside it: stop that one first (`amux context-service stop`, or `--force` for
SIGKILL).

Expect `{"ok": true, "schema_version": 3, ...}`. Then confirm the two things
that must be true of it:

```sh
# 1. Loopback only. This must FAIL to connect.
curl -sS --max-time 3 "http://$(ipconfig getifaddr en0 2>/dev/null || hostname -I | awk '{print $1}'):$PORT/healthz" \
  && echo "PROBLEM: reachable off loopback" || echo "OK: loopback only"

# 2. Unauthenticated requests are refused.
curl -sS -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:$PORT/v1/context"
```

The second must print `401`. A `200` means the capability check is not wired and
every later step's isolation result is meaningless.

Record the service PID and log path (under the amux state directory), and check
the log for plaintext tokens at the end of the run — see step 11.

---

## Step 4 — Preflight, before anything mutates

Preflight is read-only by construction: it never touches tmux, git, the
database, or `sbx` state. Run it on its own first, so a failure leaves the host
exactly as it was.

`amux doctor` defaults to `--runtime docker-sandbox`, since checking the
optional backend is what it is for. It reports and never fixes: each failure
prints the exact command to run, and no amux command installs `sbx`, signs you
in, or widens Docker's network policy.

```sh
amux doctor --runtime docker-sandbox --path "$SMOKE"
```

Add `--cpus`, `--memory`, `--share-skills` or `--context-port` to check the
values a real spawn would use; they are the same flags `spw` and `spg` take.

Expect a check for each of: `sbx` present and supported, agent kinds supported,
`$SMOKE` is a primary checkout, context service healthy, Docker
authentication, localhost policy reachable, resource values valid.

The policy check is the one most likely to fail on a fresh `balanced` host. It
is read-only, and amux prints the exact remediation rather than applying it:

```sh
sbx policy check network "localhost:$PORT"          # what preflight asks
sbx policy allow network "localhost:$PORT"          # only if preflight says to
sbx policy check network "localhost:$PORT"          # confirm
```

**amux must never have widened this for you.** If preflight passed without you
running the allow command, that is a finding: record it.

---

## Step 5 — Spawn a four-agent mixed grid

Four agents on one laptop is the case that has to stay usable, and a mixed grid
proves the per-pane agent composition survives a grid-scoped runtime.

Take the "before" numbers from step 0d again if time has passed, then:

```sh
date +%s.%N > /tmp/spawn-start
amux spw "$WS" -p "$SMOKE" -t "$TASK" \
  --runtime docker-sandbox --cpus 2 --memory 4g \
  -a claude:2 -a codex:2
date +%s.%N > /tmp/spawn-returned
```

`--cpus 2 --memory 4g` are amux's defaults, stated explicitly here because
`sbx`'s own defaults are *not* caps: `--cpus 0` means every host CPU and default
memory is half the host's.

### 5a. Spawn-to-prompt latency

`amux spw` returning is not the agent being ready. Measure both:

```sh
# command latency
echo "$(cat /tmp/spawn-returned) - $(cat /tmp/spawn-start)" | bc
```

For prompt readiness, poll the pane's own output — **do not use `amux event
state` for this.** A pane stamped `starting` settles to `idle` on a 10-second
grace timer (`events.STARTUP_GRACE_S`) whether or not the agent is ready, so
state would measure the timer, not the agent.

`amux event state` prints `pane<TAB>state<TAB>workspace/task<TAB>name`, so pick a
pane belonging to this task rather than whatever is first on the server:

```sh
PANE=$(amux event state | awk -F'\t' -v scope="$WS/$TASK" '$3 == scope {print $1; exit}')
echo "measuring $PANE"
start=$(date +%s.%N)
until tmux -L amux-root capture-pane -p -t "$PANE" | grep -qiE 'try |^\s*>|╭'; do
  sleep 1
  [ $(echo "$(date +%s.%N) - $start > 300" | bc) = 1 ] && { echo TIMEOUT; break; }
done
echo "prompt after $(echo "$(date +%s.%N) - $start" | bc)s"
```

Record the slowest of the four, not the average — that is the number a user
feels.

### 5b. One capped sandbox per pane

```sh
sbx ls --json | tee /tmp/sbx-ls-after-spawn.json
```

Expect exactly four sandboxes. Verify for each: the name is derived from
workspace, task, agent name and a repository fingerprint; the recorded
`sandbox_id` in amux matches; caps are applied; skills are not shared.

```sh
amux ctx --json --pane "$PANE" | python3 -m json.tool | grep -E 'runtime|sandbox'
```

`amux ctx` inside a pane should print a `runtime:` line immediately after the
identity line — `runtime: docker-sandbox running <sandbox-name>`. Its absence on
a sandbox pane is a bug; its **presence on a host pane** is a worse one.

### 5c. Four-agent resource use

Per-microVM attribution is not something `sbx` is known to expose, so measure
the host-level delta rather than inventing an attribution:

```sh
docker system df                      # compare to step 0d
vm_stat | head -5                     # macOS
df -h / | tail -1
sbx ls --json | python3 -c 'import json,sys; [print(s.get("name"), {k:v for k,v in s.items() if k in ("cpus","memory","state","status")}) for s in json.load(sys.stdin)["sandboxes"]]'
```

Record the deltas and, separately, whatever per-sandbox resource fields
`sbx ls --json` actually reports. If it reports none, say so — that answers one
of the change's open questions.

---

## Step 6 — Verify the boundary on a real sandbox

This is a spec requirement with its own scenario, and until now it has only been
proven in tests. Prove it on a live VM.

```sh
BOX=$(sbx ls --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["sandboxes"][0]["name"])')

# The amux state directory, the database, and the tmux socket must be absent.
sbx exec "$BOX" sh -lc 'ls -la ~/.local/state/amux 2>&1 | head -3'
sbx exec "$BOX" sh -lc 'find / -xdev -name "context.db*" 2>/dev/null | head'
sbx exec "$BOX" sh -lc 'find / -xdev -name "amux-root*" -o -name "tmux-*" 2>/dev/null | head'
sbx exec "$BOX" sh -lc 'mount | grep -iE "amux|tmux|\.git" || echo "OK: no amux/tmux/git mounts"'
sbx exec "$BOX" sh -lc 'env | grep -iE "tmux|amux" || echo "OK: no amux/tmux env"'
```

Every one of these must come back empty or `OK`. A hit is a spec violation, not
a curiosity.

Then check what the sandbox *does* have, and that it is only what it needs:

```sh
sbx exec "$BOX" sh -lc 'ls -l "$HOME/.config/amux/context.json"; cat "$HOME/.config/amux/context.json"'
```

Expect mode `-rw-------` and exactly two keys, `endpoint` and `token`. A host
path anywhere in that file is a finding.

```sh
# The shim is installed and is the context client, not the host CLI.
sbx exec "$BOX" sh -lc '/usr/local/bin/amux --help'
sbx exec "$BOX" sh -lc '/usr/local/bin/amux integrate ws task; echo "exit=$?"'
```

The last must refuse locally with a boundary message and `exit=2`, without
reaching the service.

---

## Step 7 — Blocked-network diagnostics

Confirm the failure mode is *clear*, and that nothing falls back to a weaker
path when the service is unreachable.

```sh
# What the sandbox may reach, per policy:
sbx policy check network --sandbox "$BOX" "localhost:$PORT"
sbx policy check network --sandbox "$BOX" api.github.com

# Something policy should refuse under `balanced`:
sbx exec "$BOX" sh -lc 'curl -sS --max-time 5 https://example.invalid 2>&1 | head -2'
sbx policy log | tail -20
```

Record the denial as the sandbox sees it and as `sbx policy log` reports it —
those two being different is worth knowing.

Now break the context path deliberately and confirm the client's own diagnostic:

```sh
amux context-service stop
sbx exec "$BOX" sh -lc '/usr/local/bin/amux ctx; echo "exit=$?"'
amux context-service start
```

Expect `exit=1` and a message naming the context service and endpoint. It must
**not** mention a database, a mount, or a fallback, and must not succeed.

---

## Step 8 — Context exchange between two sandboxes

The same exchange the automated suite proves against a fake, now over the real
service. Use two panes from step 5; call them A and B.

Pick two panes of this task, and the sandbox each one fronts. `amux ctx --json`
reports the sandbox name for a pane, so nothing here has to be matched by eye:

```sh
read -r A B <<<"$(amux event state | awk -F'\t' -v s="$WS/$TASK" '$3==s {print $1}' | head -2 | tr '\n' ' ')"
BOX_A=$(amux ctx --json --pane "$A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["self"].get("sandbox_name") or "NOT-A-SANDBOX")')
BOX_B=$(amux ctx --json --pane "$B" | python3 -c 'import json,sys; print(json.load(sys.stdin)["self"].get("sandbox_name") or "NOT-A-SANDBOX")')
echo "A=$A ($BOX_A)  B=$B ($BOX_B)"
# `NOT-A-SANDBOX` means you picked a host pane: check $WS/$TASK.

# A publishes; B reads.
sbx exec "$BOX_A" sh -lc '/usr/local/bin/amux note "smoke: task note from A" --kind finding'
sbx exec "$BOX_B" sh -lc '/usr/local/bin/amux notes'

# A's private note stays private.
sbx exec "$BOX_A" sh -lc '/usr/local/bin/amux note "smoke: private to A" --scope agent'
sbx exec "$BOX_B" sh -lc '/usr/local/bin/amux notes --scope agent'   # must NOT show it
sbx exec "$BOX_A" sh -lc '/usr/local/bin/amux notes --scope agent'   # must show it

# The host sees both, correctly attributed.
amux notes --workspace "$WS"

# Roster and state cross the boundary.
sbx exec "$BOX_A" sh -lc '/usr/local/bin/amux ctx'
sbx exec "$BOX_B" sh -lc '/usr/local/bin/amux event state'
```

Then the transition path, which is what the hooks from step 2 are for:

```sh
# Send a real prompt to A and watch B observe it.
tmux -L amux-root send-keys -t "$A" 'list the files in this repo'
sleep 0.3; tmux -L amux-root send-keys -t "$A" Enter

sbx exec "$BOX_B" sh -lc '/usr/local/bin/amux event wait '"$A"' --timeout 120; echo "exit=$?"'
amux event state
```

Record whether A ever reads `busy`. For Claude it should. **For Codex, this is
where step 2b's answer shows up:** if hooks need approval, Codex will sit at
`idle` throughout, and that is the finding, not a bug to chase here.

---

## Step 9 — Commit in a sandbox and integrate it

`--clone` mounts the workspace read-only and the agent works on a private clone;
its commits are reachable on the host through a `sandbox-<name>` git remote.
Integration never imports uncommitted files.

> **Order matters, and not the way you would guess.** `integrate` marks every
> worktree row `merged`, and both the stop and cleanup paths only act on `active`
> rows — so running it before the real commit consumes the rows and nothing
> afterwards can integrate, stop, or clean that task. Commit *first*, integrate
> *once*, and do the uncommitted-work check in the same pass.

```sh
# Uncommitted work must NOT appear on the host. Left in place deliberately: the
# commit below uses `-a`, which does not add an untracked file, so one integrate
# proves both halves.
sbx exec "$BOX_A" sh -lc 'cd ~/*/ && echo "dirty" > uncommitted.txt && git status --short'

# Now commit properly.
sbx exec "$BOX_A" sh -lc 'cd ~/*/ && git checkout -b smoke-change 2>/dev/null; \
  echo "from the sandbox" >> README.md && \
  git -c user.email=a@amux.invalid -c user.name=a commit -aqm "sandbox commit" && \
  git log --oneline -1'

cd "$SMOKE" && git remote -v | grep sandbox
amux integrate "$WS" "$TASK"
git -C "$SMOKE" log --oneline --graph -5 "amux/$WS/$TASK/integration"
```

Record: the merge summary line, the commit count, the shortstat, and that
`uncommitted.txt` is **absent** from the integration branch.

Then the cases that are easy to get wrong:

```sh
amux integrate "$WS" "$TASK"        # no-delta: must be a clean no-op, not an error
sbx stop "$BOX_B"
amux integrate "$WS" "$TASK"        # stopped sandbox: clear message, no crash
```

A merge conflict is worth provoking if you have time — edit the same line on the
integration branch and in a sandbox. Expect the merge to abort, a `blocker` note
to be recorded, and the working tree to be left clean.

---

## Step 10 — Stop, reattach, and clean up to a fixed point

> Flag names here (`--clean`, `--force`) come from tasks 5.2/5.3/5.5 and were not
> landed when this was written. `--clean` exists today; the forced-clean flag's
> name is whatever 5.5 chose. Check `amux kw --help` and substitute.

```sh
# Stop preserves VM state and credentials.
amux kg "$WS" "$TASK"
sbx ls --json | grep -E 'name|state|status'      # still inspectable, stopped
```

Reattach and confirm it is the *same* sandbox, by recorded ID rather than name:

```sh
amux spg "$WS" "$TASK" --runtime docker-sandbox -a claude
sbx ls --json | python3 -c 'import json,sys; [print(s["name"], s.get("id")) for s in json.load(sys.stdin)["sandboxes"]]'
```

Compare against `/tmp/sbx-ls-after-spawn.json`. A new ID means reattachment
created a fresh VM and silently lost the old one's state.

### Cleanup, including the refusals

```sh
# A dirty sandbox must be REFUSED without an explicit force flag.
sbx exec "$BOX_A" sh -lc 'cd ~/*/ && echo more >> uncommitted.txt'
amux kg "$WS" "$TASK" --clean          # expect refusal naming the dirty sandbox
```

That refusal is the point of the step. Then confirm the registry was **not**
marked removed:

```sh
amux lsw
```

Now clean properly, and twice — cleanup must be a fixed point:

```sh
amux kw "$WS" --clean --force
amux kw "$WS" --clean --force          # second run: no-op, no error
sbx ls --json                          # no smoke sandboxes
git -C "$SMOKE" remote -v | grep sandbox && echo "PROBLEM: remote left behind" || echo "OK"
git -C "$SMOKE" branch -a | grep "amux/$WS"   # host branch tips preserved
# A *sandbox* branch is not a local branch: its tip is fetched before removal
# into a durable ref, which is what survives `sbx rm`.
git -C "$SMOKE" for-each-ref --format='%(refname)' 'refs/amux/sandboxes/**'
```

Branches are kept deliberately — cleanup removes worktrees and sandboxes, not
commits. Check that last command actually lists a ref per sandbox that had
commits: it is the only copy of that work once the VM is gone.

### The capability must be dead

```sh
TOKEN=$(cat /tmp/captured-token 2>/dev/null)   # if you saved one in step 6
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:$PORT/v1/context"
```

Expect `401`. A `200` means removing a sandbox left a working credential.

---

## Step 11 — Check the logs for secrets

```sh
grep -rE '[A-Za-z0-9_-]{32,}' "${XDG_STATE_HOME:-$HOME/.local/state}/amux/context-service.log" | head
```

Then eyeball the shell scrollback from this whole run. Plaintext capability
tokens and provider credentials must appear in neither. The token you read
deliberately in step 6 is the only acceptable occurrence, and it is now revoked.

---

## Step 12 — Restore the host

```sh
# amux cleanup does NOT remove a sandbox whose row is `merged`, and it reports
# success anyway -- so check, and finish the job by hand.
sbx ls --json
sbx rm -f $(sbx ls --json | python3 -c 'import json,sys; print(" ".join(s["name"] for s in json.load(sys.stdin)["sandboxes"]))')
sbx ls --json                      # must now be empty

amux context-service stop
sbx policy rm network --id "$RULE_ID"   # printed when you added it
sbx policy ls                      # allow count back to its pre-test value
rm -rf "$SMOKE" "$XDG_STATE_HOME"
df -k / | tail -1                  # compare to step 0e
```

`sbx policy init` is **not** undone — it is a host-wide decision you made in
step 0c, and reversing it is out of scope for this test.

---

## Recording sheet

Copy this, fill it in, and attach it to the change.

```
Date:                       Host (OS/arch/RAM):
sbx version:                codex version in image:
Policy profile:             amux commit:

MISSING SURFACE (steps skipped, and which task they need)
  -

IMAGE PROBE (step 2a)
  claude HOME:              settings.json present:
  codex HOME:               hooks.json present:      config.toml present:
  fixtures re-recorded:     paths_are_assumed now:

CODEX HOOK TRUST (step 2b)  fired silently / prompted / skipped:
  trusted_hash written by codex:
  consequence for parity claim:

LATENCY (step 5a)
  spw returned after:            s
  slowest prompt-ready:          s      (fastest:      s)

FOUR-AGENT RESOURCES (step 5c)
  docker system df   before:            after:            delta:
  free memory        before:            after:            delta:
  disk /             before:            after:            delta:
  per-sandbox fields exposed by sbx ls --json:

BOUNDARY (step 6)          state dir / context.db / tmux socket / mounts / env:
  config.json mode + keys:
  host-only command refused with exit 2:

NETWORK (step 7)
  policy verdict localhost:PORT:        arbitrary host:
  sandbox-visible denial text:
  service-down client message + exit:

CONTEXT EXCHANGE (step 8)
  task note A->B:            agent note stayed private:
  claude reached busy:       codex reached busy:
  event wait released:

INTEGRATION (step 9)
  merge summary:
  uncommitted excluded:      no-delta re-run:      stopped sandbox:
  conflict behaviour:

LIFECYCLE (step 10)
  stopped sandbox inspectable:      reattached same id:
  dirty cleanup refused:            registry NOT marked removed:
  cleanup idempotent:               remotes gone:       branches kept:
  old token now 401:

LOGS (step 11)   secrets found:

FINDINGS / DEVIATIONS
  -
```

---

## What this guide cannot tell you

State these alongside the results rather than leaving them implied.

- **One host, one profile.** Nothing here says anything about a different OS,
  less RAM, another `sbx` version, or a different policy profile.
- **Resource attribution comes from the host process table, not from `sbx`.**
  `sbx ls --json` reports no cpu or memory fields at all (confirmed). Per-sandbox
  figures come from each microVM's `containerd-shim-nerdbox-v1` process; disk is a
  whole-volume `df` delta and cannot be split per sandbox.
- **Latency is one cold run.** Image pulls, provider warm-up and disk cache make
  the first spawn unrepresentative. Note whether images were already local.
- **The conflict and rollback paths are only sampled.** Transactional rollback
  of a partially created grid is covered by automated tests with a fake `sbx`;
  provoking a real mid-creation failure is not part of this guide.
- **No adversarial testing.** This verifies that the boundary holds under normal
  operation. It is not an attempt to break out of it.
- **Nothing agent-driven is covered without provider credentials.** Hooks fire on
  agent activity, so a logged-out sandbox can never demonstrate a hook-driven
  state transition. Every context operation is still testable by invoking the shim
  through `sbx exec`, which is what the steps above do — but that exercises the
  *service* path, not the *hook* path.
- **Kill your own long-running probes with `timeout`.** A killed process group can
  leave an orphaned `sbx exec` behind, and the next measurement then blocks on it
  and looks like a service bug. Two apparent concurrency failures in the first run
  of this guide were exactly that.
