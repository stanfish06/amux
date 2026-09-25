---
name: amux
description: Use when orchestrating AI agents in tmux with the amux CLI — spawning or killing agent grids (`amux spw`/`spg`/`kw`/`kg`), listing them (`lsw`/`lsg`), mixed claude+codex grids, the `amux-root` tmux socket, agent state events (`amux event emit`/`state`/`tail`/`wait`), per-agent git worktrees and task integration branches (`amux integrate`), scoped context notes (`amux note`/`notes`), sending messages from one agent pane to another, when an agent needs to discover which workspace, task, and pane it is running in (`amux ctx`), when an agent needs to start work elsewhere — a task whose context no longer fits the current window, or a change that belongs to another repo — or when an agent runs inside a Docker Sandbox microVM (`--runtime docker-sandbox`, `sbx`) and needs to know which commands cross the host boundary, why a context command fails, or how its committed branch reaches the host.
---

# amux

Agent orchestration on top of tmux. One dedicated tmux server (socket `amux-root`)
holds every agent, so amux never touches your interactive tmux sessions.

**amux installed this document when it spawned your pane**, and it is the
authoritative vocabulary for that pane — coordination, spawning work, messaging
teammates, per-agent worktrees, agent state, and, if you are in one, the sandbox
boundary. It is rewritten on every spawn, so it describes the amux you are
actually running rather than whatever was in your skill directory beforehand.
Where it disagrees with your recollection of these commands, it is right.

## Vocabulary

amux renames tmux concepts. Its output uses the right-hand column; raw `tmux`
commands use the left.

| tmux    | amux        | Meaning                        |
|---------|-------------|--------------------------------|
| session | workspace   | one project / repo             |
| window  | task        | one unit of work               |
| pane    | agent       | one running `claude` / `codex` |

## Quick reference

| Command | Does |
|---|---|
| `amux spw <ws> [-p DIR] [-t TASK] [-a SPEC] [-r N] [-c N]` | spawn workspace + its first task grid |
| `amux spg <ws> <task> [-p DIR] [-a SPEC] [-r N] [-c N] [--base REF] [--brief FILE]` | add a task grid to an existing workspace |
| `amux lsw` | list all workspaces, tasks, panes |
| `amux lsg <ws>` | list task grids in one workspace |
| `amux monitor [-W COLS] [-T COLS] [-i MS]` | live read-only dashboard of every workspace and agent |
| `amux kg <ws> <task> [--clean]` | kill one task (--clean also removes its worktrees) |
| `amux kw <ws> [--clean]` | kill a whole workspace |
| `amux ctx [--json] [--pane ID]` | this agent's identity + workspace team roster + visible notes |
| `amux send <pane> <text...> [--timeout S]` | send an attributed message to an idle or busy agent and confirm it was taken |
| `amux send --role ROLE [--task T] <text...>` | same, addressed to the one agent with that role in your task (or task T) |
| `amux messages [-n N] [--status STATUS] [--json]` | list durable messages sent or received by this agent |
| `amux note <text> [--scope agent\|task\|workspace] [--kind note\|decision\|finding\|blocker\|knowledge]` | publish a scoped context note |
| `amux notes [--workspace WS] [--task T] [--scope S] [--kind K] [-n N] [--json]` | list scoped notes |
| `amux integrate <ws> <task> [--agent NAME...] [--all] [--into BRANCH]` | merge agent worktree branches into the task integration branch, then optionally into BRANCH |
| `amux event state [--json]` | resolved state of every agent on the server |
| `amux event tail [-n N] [--pane ID] [--workspace WS] [--task T]` | recent state events as JSONL |
| `amux event wait <pane> [--timeout S]` | block until a pane goes idle / needs-input / dead |
| `amux event emit <kind> [--pane ID] [--agent K] [--detail T]` | record a state change (for hooks) |

`-p` defaults to `$PWD` for `spw`, to the workspace dir for `spg`. `-t` defaults
to `task0`. Global `-L/--socket-name` must come **before** the subcommand.

Every command above runs on the host. Inside a Docker Sandbox agent only `ctx`,
`send`, `messages`, `notes`, `note` and `event emit|state|wait` are available —
the rest refuse locally. See *Sandboxed agents* for why, and for
`--runtime docker-sandbox`.

## Agent specs and grid shape

`-a AGENT[@MODEL][/EFFORT][:COUNT]` is repeatable and fills panes row-major.
`AGENT` is `claude`, `codex`, or any raw shell command. Every other part is
optional. Default is one `claude`.

```sh
amux spw myproj -p ~/Git/myproj -r 2 -c 2   # 2x2, all claude
amux spg myproj review -a codex:2           # 1x2, both codex
amux spg myproj fix -a claude:3 -a codex    # 2x2, 3 claude + 1 codex
amux spg myproj shell -a bash               # raw command instead of an agent
amux spg myproj plan -a claude@opus/high -a codex@gpt-5.6-sol/xhigh   # per-spec model + effort
```

## Model and reasoning effort

Each spec carries its own model and effort, so one grid can mix a cheap
reviewer with an expensive implementer. Both work identically under the host
and `docker-sandbox` runtimes, and `amux ctx` reports what your own pane was
launched with, so you can check rather than guess.

They become each CLI's own flags — `claude --model X --effort Y`, `codex -m X
-c model_reasoning_effort=Y` — appended to the command amux already runs.

**amux does not check the values.** A model or effort level released after this
amux was built works immediately, and amux never refuses one it has not heard
of. The cost is that **a typo is not caught by anything, and it fails quietly
rather than loudly** — do not expect a dead pane to tell you. Measured live
against `claude` 2.1.224 and `codex-cli` 0.146.0:

- `claude --effort hgih` prints `Warning: Unknown --effort value 'hgih' —
  ignoring it and using the default effort`, then runs normally on its default.
- `codex -c model_reasoning_effort=hgih` accepts the string silently and
  displays it as if it were real.
- A bad *model* on either agent starts normally and fails at the first API call.

So the pane comes up alive, and it is running on a configuration that is not the
one you asked for. **`amux ctx` will not save you here**: it reports what the
pane was *launched* with, and cannot know what the agent did with that. After a
typo the two disagree.

**Be careful which banner you trust — the two agents differ.** claude's startup
box reports the *effective* value: launch it with `--effort hgih` and the box
reads `with high effort`, the default it actually fell back to, so it is a real
confirmation. Codex's `model:` row is a bare **echo of what you passed** —
`codex -m totally-bogus-model-zzz` prints that nonexistent model verbatim and
runs until the first API call fails. So for codex the row confirms only that
your flag arrived, never that the model exists or was accepted. To know a codex
model is real, send it a prompt and see whether the request succeeds; nothing
before that first call can tell you. That last part is measured, not assumed: a
bogus-model pane left idle shows nothing in its whole scrollback, and codex's
`⚠ Model metadata for ... not found` warning does not appear at startup — it
arrives with the first request, alongside the `400 ... model is not supported`
that is the actual answer. **An untroubled-looking fresh codex pane is not
evidence of anything.**

What amux does reject is a malformed *shape* — `claude@`, `claude/`, and
`claude@opus/` are errors, and nothing is created.

**Escape hatch.** `@`, `/` and `:` are the delimiters, so a model id containing
`/` (`openai/gpt-5`) or ending in `:<digits>` (a Bedrock-style `…-v1:0`) cannot
go in a spec — it parses as an effort level or an invalid count. Launch it as a
raw command instead:

```sh
amux spg myproj fix -a 'claude --model openai/gpt-5 --dangerously-skip-permissions'
```

You are trading the grammar for the flag. A raw spec carries no `@MODEL` or
`/EFFORT` of its own, so it must spell out every flag itself — including the
ones amux normally adds, which is why the example above repeats
`--dangerously-skip-permissions`. It also cannot run under `docker-sandbox` at
all, and `ctx` and `monitor` print the whole command string in the agent column
instead of `claude` or `codex`. Prefer the spec grammar when your model id fits
it.

What a raw-command pane does **not** lose is its work. It still gets its own
worktree and branch like everyone else, and `amux integrate` merges its branch
normally — so commit and integrate exactly as you otherwise would.

Shape resolution, given `n` total agents:

- both `-r` and `-c`: must satisfy `r*c == n`, else error.
- one of them: the other is `n / given`; must divide evenly, else error.
- neither: the factor pair closest to square, rows ≤ cols (so 6 → 2x3).

Counts and shape interact: **when both `-r` and `-c` are given, exactly one spec
may omit its count** and it absorbs the remaining panes (`-r 2 -c 2 -a claude:3
-a codex` → the lone `codex` fills 1). With unknown or partial shape, a missing
count means 1.

## Roles

A spec may start with `ROLE=`: `-a worker=claude -a supervisor=codex/xhigh`.
The role is a markdown file whose body becomes that agent's system prompt, so
two panes running the same CLI can be told to do different jobs.

```sh
amux spg rt t0062 -a worker=claude -a supervisor=claude --base amux/rt/record
amux ctx          # you: brave-hawk  worker=claude @r0c0 %7 ...
amux send --role supervisor "plan ready at 3f2a1c9"
```

**Where roles come from.** `<repo>/.amux/roles/<role>.md` in the workspace
repo, then `$XDG_CONFIG_HOME/amux/roles/<role>.md`. The repo copy wins. amux
ships no built-in roles; `templates/research-campaign/.amux/roles/` has a
worker / supervisor / curator / editor / scout set to copy.

```
+++
description = "reviews the paired worker's plan and results"
model = "opus"
effort = "xhigh"
subagents = ["curator"]
+++
You are the supervisor of one amux task. ...
```

A role listed under `subagents` is not a pane. It runs inside your session
with a fresh context each time you call it, and its answer comes back to you.
Use one for work that should not share your context, such as curating
results or running a web search. Under codex, spawn it with `spawn_agent`,
using that `agent_type` and `fork_turns="none"`. A subagent runs in your
pane, so its `amux note` calls are attributed to you.

A pane spawned with a role or a `--brief` runs unattended, so nobody is there
to answer a prompt meant for a human. amux therefore launches it with its
skill pointer in the system prompt instead of a typed bootstrap message.

**claude** gets `--disallowedTools SendFeedback,AskUserQuestion`. Both tools
open a prompt that waits for a human and swallows any message typed while it
is up. Ask a teammate with `amux send` instead.

**codex** gets the equivalents, checked live on codex 0.156.1:

- `-c tools.experimental_request_user_input.enabled=false` removes the
  question selector.
- `-c check_for_update_on_startup=false` stops the update menu.
- `--dangerously-bypass-hook-trust` skips the hook review prompt.
- `-c 'projects={"<worktree>"={trust_level="trusted"}}'` skips the folder-trust
  prompt.

No setting stops codex's plan-mode "Implement this plan?" prompt, so
unattended panes stay in the default mode.

A subagent cannot start subagents of its own. A role used as a subagent must
therefore have no `subagents` key, or the spawn fails before anything starts.

**Refusals, all before anything is spawned.**
- An unknown role is an error: `no role 'wroker': looked for wroker.md in ...`.
- A role on a raw command (`worker=bash`) is an error.
- A lowercase environment assignment such as `x=1 claude` reads as a role, so
  write `env x=1 claude` for that.
- Roles run on the host runtime only.

**Record branch.** `integrate --into BRANCH` merges the task integration branch
into BRANCH. It creates BRANCH at the task's base if missing and never checks
it out, so BRANCH must not be checked out anywhere. `spg --base BRANCH` starts
a new task from it. Together they give a campaign one branch that every later
task can read, which is why follow-up tasks should be spawned with `--base`.
Merging to the repo's main line is still a human act, so `--into` refuses
`main`, `master` and `origin`'s default branch.

**Brief.** `spw`/`spg --brief FILE` hands every claude or codex agent in the
new grid FILE as its first prompt, so the pair starts working without anyone
messaging it. That matters because `amux send` never crosses workspaces, so a
brief is the only way to start a grid you spawned in another workspace. amux
copies the file into the prompts directory at spawn, and later edits to FILE
do not reach the agents.

**Knowledge.** `amux note --scope workspace --kind knowledge "..."` records a
reusable fact for later tasks, and `amux notes --kind knowledge` reads the
knowledge base.

## Spawning work yourself

Spawning is not reserved for the human. **Any agent may run `spw` and `spg`**
when the work no longer fits where it is. You do not need permission to do this;
you need a real reason, since every grid you spawn costs panes and tokens.

Two cases come up constantly:

**Your context window no longer suits the task.** A fresh task grid starts with
a clean context. When the thing in front of you is a genuinely new unit of work
and dragging the current history into it would only dilute it, spawn a task
rather than continuing in place:

```sh
amux spg myproj perf-audit -a claude    # new task, same project, clean context
```

**The work crosses into another project.** A workspace is one project / repo, so
a change that lands in a *different* repo belongs in a different workspace. This
is the common shape: fixing your project surfaces a bug in a library it depends
on, and that fix has its own repo, its own branches, its own worktrees.

```sh
amux spw thatlib -p ~/Git/thatlib -a claude   # separate repo -> separate workspace
```

Do not point a task at another repo with `-p` to dodge this. It appears to work
— the repo is taken from the task's directory, so worktrees and `integrate` do
function — but the workspace stops meaning one project, and two things quietly
break. The other repo gets branches named `amux/<this-workspace>/<task>/<name>`,
naming a project it has nothing to do with. And note visibility is filtered by
repo, so that task's agents and the rest of the workspace stop seeing each
other's notes while still appearing on the same `amux ctx` roster.

Before you spawn, leave a note saying why (`--kind decision`) — it is the only
record connecting the new workspace back to the work that caused it, and
`amux ctx` will not show one workspace's roster inside another:

```sh
amux note "spawning workspace thatlib: the retry bug is in ~/Git/thatlib, not here" \
  --scope task --kind decision
amux spw thatlib -p ~/Git/thatlib -a claude
```

Then hand off deliberately. The new agents start cold and share no context with
you — they cannot see your notes, since notes are scoped per workspace. Send the
first one a message with your identity and the task (see *Messaging teammates*),
or it will never learn what you spawned it for.

One thing does reach a new agent without you: amux installs this skill and points
the agent at it. A `codex` agent is pointed by a **bootstrap message typed into
its pane**, so it wakes on its own and spends its first turn reading this
document — it is not idle, and it is not waiting for you. A `claude` agent gets
the same pointer in its system prompt and costs no turn. Either way the pointer
says nothing about *your* task; that part is still yours to send.

The exception, and it is common rather than rare: both agents ask whether to
trust a directory they have not seen before, and every agent gets a fresh
worktree. An agent sitting on that prompt never reaches its input box, so amux
waits, gives up, and prints that it could not point that agent at the skill. If
you spawned it, answer the prompt in its pane — then say what the task is.

Reach for `spg` (a task in your workspace) before `spw` (a whole new workspace).
Same repo means same workspace; only a different repo justifies `spw`.

## Agent identity

Every pane gets a stable `adjective-noun` name (e.g. `brave-hawk`), a grid label
`r<row>c<col>`, and tmux pane options `@amux_agent` / `@amux_label` /
`@amux_name` / `@amux_state`. Pane titles are locked (`allow-set-title off`) so
apps can't overwrite the name.

An agent discovers itself and its teammates with no arguments:

```sh
amux ctx           # human-readable: you: brave-hawk claude @r0c1 %7 task:fix ...
amux ctx --json    # {"self": {...}, "team": [{"task": ..., "agents": [...]}]}
```

Addresses in `ctx` output — `@r0c1` (label) and `%7` (tmux pane id) — are what
you use to target a specific teammate pane.

## State events

`amux event emit <kind>` appends to the amux context store
(`~/.local/state/amux/context.db`), sets the pane's `@amux_state`, and signals
a tmux `wait-for` channel. Kinds map to states:

| kind | state |
|---|---|
| `spawn` | starting |
| `busy` | busy |
| `stop` | idle |
| `notify` | needs-input |
| `exit` | dead |

One `notify` is classified differently: Claude Code fires its Notification
hook both for prompts that need a human (permissions, trust dialogs) and for
its ~60s "waiting for your input" idle reminder. The reminder means the agent
is parked at an empty prompt, so amux resolves it to `idle`, not
`needs-input` — otherwise every parked agent would drift out of reach of
`amux send` a minute after finishing a turn.

`spawn` is amux's own: spawning a grid stamps every new pane with it, so an
agent has a state from its first moment. The rest come from agent hooks — Claude
Code's `PreToolUse` → `busy`, `Stop` → `stop`, `Notification` → `notify`,
`SessionEnd` → `exit`. With `--detail` omitted, the detail is pulled from the
hook's JSON on stdin (`message` / `tool_name` / `reason`). `emit` swallows all
errors so a hook never looks like agent failure.

An agent that has come up but has never been prompted reads `idle`, not
`starting`: nothing on the agent side announces "my prompt is ready", so
`starting` settles to `idle` a few seconds after spawn. A freshly spawned `codex`
may then go `busy` on its own, with nobody having prompted it — that is amux's
bootstrap message being read, so "busy right after spawn" no longer implies a
human sent something.

`amux event state` reads the other way — the resolved state of every pane, which
is what `monitor` and `lsw` render:

```sh
amux event state          # %7  idle  myproj/task0  brave-hawk
amux event state --json   # same, with each pane's last event
```

Read state from there rather than from `event tail`. A `%N` id is only unique
while the tmux server that issued it lives, so the newest raw event for `%1` may
belong to an agent that is long gone; `event state` discards anything older than
the pane in front of it, and treats a pane tmux has lost as dead however its
events end.

To coordinate, block on a teammate instead of polling:

```sh
amux event wait %7 --timeout 120   # prints the state; exit 0 ok, 1 timeout, 2 dead
```

## Per-agent worktrees

When a workspace spawns into a git repo, every agent pane runs in its **own
private worktree** with its **own branch** — never in the shared checkout, so
agents can't collide on the same files. The task gets an **integration branch**
and integration worktree; your branch is based on it.

```
amux/<ws>/<task>/integration   task integration branch (_integration/ worktree)
amux/<ws>/<task>/<name>        your branch and worktree
```

Layout on disk (outside your repo, under `$XDG_STATE_HOME/amux/worktrees/`):
`<ws>/<task>/_integration/` and `<ws>/<task>/<your-name>/`.

- Your working directory **is** your worktree. Commit there as usual.
- To see your branch: `git branch --show-current` or `amux ctx`.
- Non-repo targets (or repos with no commits) fall back to the shared
  directory — `amux spw` prints a warning when worktree isolation is skipped.
- `kg`/`kw` keep worktrees and branches by default. Add `--clean` to remove the
  worktree directories (branches are still kept).

### Integrate

When your work is ready, merge it into the task integration branch:

```sh
amux integrate <ws> <task>                 # merge every active worktree
amux integrate <ws> <task> --agent brave-hawk   # just one agent
```

This runs `git merge --no-ff` in the integration worktree, records a context
note ("merged brave-hawk — 3 commit(s), +120/−40"), and marks your worktree
`merged`. A conflict aborts the merge and records a `blocker` note instead.
Merging the integration branch back to the repo's main line is a human act.

## Sandboxed agents (the `docker-sandbox` runtime)

Agents normally run on the host. A grid may instead be spawned with
`--runtime docker-sandbox`, which puts each agent inside its own Docker
Sandboxes microVM. amux keeps every coordination concern on the host — workspace,
task, roster, notes, events, integration — and the agent gets a boundary it
cannot reach across.

This is an opt-in backend. `docs/sandbox-smoke-test.md` is the procedure that
verifies it on a real host.

### Am I in a sandbox?

Check, don't assume — it changes what you can run and how you share work:

```sh
amux ctx
# you: brave-hawk  claude @r0c1 %7  task:fix  workspace:myproj  busy  ...
# runtime: docker-sandbox running amux-myproj-fix-brave-hawk-ab12cd
```

A `runtime:` line appears immediately after your identity line **only** when you
are not on the host. Host agents never see it, so its absence means host. In
`--json`, `self` carries `runtime`, `runtime_status`, `sandbox_name` and
`sandbox_id`.

### What works, and what refuses

Inside a sandbox, `amux` is a small standalone client that talks to a host
service over HTTP. It supports exactly this messaging and context subset:

| Works in a sandbox | Refuses locally |
|---|---|
| `ctx`, `send`, `messages`, `notes`, `note` | `spw`, `spg`, `kg`, `kw` |
| `event emit`, `event state`, `event wait` | `integrate`, `monitor`, `lsw`, `lsg`, `event tail` |

A refusal is immediate, exits 2, and names the command — it is not a transient
error to retry, and there is no flag that unlocks it. **Host control is not
expressible in the sandbox's capability vocabulary at all**, so nothing you can
do from inside escalates into it. If you need one of those, say so in a note or
message a host agent; do not try to work around it.

Two flags are also refused, because your identity is fixed by your credential
rather than by an argument: `--pane` (on `ctx`, `notes`, `note`) and
`--workspace` / `--repo` (on `notes`). You can still *wait on* a teammate —
`amux event wait %9` is fine for a pane in your own workspace and task.

### Sharing work: commit, or it does not exist

You are on a private clone, not a shared worktree. Docker mounts the repository
read-only and gives you your own copy, which is why ordinary git works and why
nothing you do can touch the human's checkout. It also means:

- **Uncommitted work is invisible to everyone.** `amux integrate` fetches your
  *committed* branch through a host-side `sandbox-<name>` git remote and never
  imports a dirty working tree. An uncommitted change is not "not yet reviewed",
  it is not there.
- **A teammate's files are not at your paths.** They are in a different VM. Read
  their work by integrating, or ask them to commit and say so in a note.
- **Cleanup refuses to remove a dirty sandbox** without an explicit force flag,
  and preserves your committed branch tip first (pending). That protects you, but
  only for work you committed.

So commit early and often, and leave a note when a branch is ready:

```sh
git add -A && git commit -m "auth: retry on 429"
amux note "auth retry ready on my branch, 3 commits" --kind finding
```

### The trust model, as it actually is

Worth understanding, because the honest version is narrower than it sounds.

Your credential is a high-entropy capability token delivered to your VM in a
mode-`0600` file. The host stores only its SHA-256 hash, compares in constant
time, binds it to exactly one execution record, and revokes it when your sandbox
is removed. The host derives your workspace, task, repository, pane, agent and
name from that record — identity fields in a request body are rejected, not
trusted, so you cannot post as a teammate even by accident.

The vocabulary has four permissions: `context:read`, `notes:write`,
`events:write`, `messages:write`. **Every agent token is currently minted with
all four, so this is not least privilege today** and should not be described as
if it were. Two things about it are true and load-bearing:

- **Host control is inexpressible.** Spawning, killing, integrating, cleaning and
  monitoring have no permission that could grant them, so no token — leaked,
  stolen, or misused — can be escalated into them.
- **The per-route `requires=` field is the seam** that makes narrowing possible
  later without touching handlers.

The service binds to `127.0.0.1` only, with no configurable bind address, and
every request is size-, count- and time-bounded.

### The context database never leaves the host

amux does not mount, copy, or synchronise `context.db`, its WAL/shm files, the
amux state directory, or the tmux socket into a sandbox — not read-only, not
ever. The HTTP service is the only context path.

This is why a service outage is a hard failure. If `amux ctx` reports that it
cannot reach the context service, that is the designed behaviour: there is no
fallback to a mounted database, a local shadow copy, or an unauthenticated
write, and you should not build one. Wait, retry, or report it.

### Troubleshooting from inside a sandbox

| Symptom | Cause and fix |
|---|---|
| `cannot reach the amux context service at ...` | The host service is down or network policy blocks it. Not yours to fix — report it. Nothing you write is being recorded meanwhile. |
| `the amux context service refused the request: unauthorized: ...` | Your token expired or your sandbox was removed. Report it; do not look for another credential. |
| `... forbidden: pane %99 is not in myproj/fix` | You named a pane outside your own workspace and task. Get pane ids from `amux ctx` or `amux event state`. |
| `'integrate' runs only on the amux host` | Working as intended. Ask a host agent, or leave a note. |
| `--pane is host-only` | Your identity is your token. Drop the flag. |
| `no sandbox context configuration at ...` | Bootstrap did not finish. This is a host-side failure; report it rather than writing the file yourself. |
| Teammate stuck at `idle` while clearly working | Their agent's hooks may not be reporting. For Codex specifically this is a known open question (hook trust); treat their state as unreliable rather than concluding they are done. |

### For the human on the host

```sh
amux doctor --runtime docker-sandbox -p ~/Git/myproj   # read-only preflight  (pending)
sbx ls --json                                          # the VMs themselves
sbx policy init balanced                               # one-time, host-wide
sbx policy allow network localhost:47317               # only if preflight says so
```

`sbx` is optional and external: amux detects it, reports its version, and never
installs it, signs you in, or widens Docker policy for you. Resource caps default
to 2 CPUs and 4 GiB per agent because `sbx`'s own defaults are not caps —
`--cpus 0` means every host CPU.

## Containerized agents (the `apple-container` runtime)

A third backend, macOS-only: `--runtime apple-container` runs each agent inside
an Apple `container` Linux VM (github.com/apple/container). The shape is *host
worktrees, containerized execution* — the agent keeps its normal per-agent
worktree and branch, bind-mounted into the VM at its host path, so commits made
inside land directly on the host branch and `amux integrate`, `kg` and
`kg --clean` behave exactly as they do for host agents. There is no private
clone, no context service, and no preserve-tips pass: the container holds
nothing that is not already on the host.

You will only ever read this section as a host agent. The containerized agent
has no amux client, no skill pointer and no tmux — so it emits **no state
events** (it reads `idle` while working, exactly like a raw-command agent),
`amux send` to its pane will report `undelivered` even when the text reached
it, and it cannot write notes. Coordinate with it through git: its commits are
on its branch the moment they happen.

```sh
amux doctor --runtime apple-container -p ~/Git/myproj   # read-only preflight
amux spg myproj heavy -a claude:2 --runtime apple-container
amux spg myproj heavy2 --runtime apple-container --image my/prebaked:latest
```

Supported agents are `claude` and `codex`; the default image is
`docker.io/library/node:22` and the agent CLI arrives via `npx` at launch, so
the first spawn pulls the image and package — a prebaked `--image` skips that.
Credentials are inherited from the pane's environment when set
(`ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `OPENAI_API_KEY`), never
embedded in the command. `--cpus` and `--memory` cap each VM as they do for
`docker-sandbox`; `--share-skills` and `--context-port` do not apply.

## Context notes

Share status without pinging a teammate. Notes live in a per-scope store and
are queryable on demand — this is the replacement for pasting status into
someone's pane.

```sh
amux note "decided: use sqlite, not jsonl" --scope task --kind decision
amux note "auth tests pass; blocked on review" --kind finding
amux note "waiting on reviewer, see PR #12" --scope task --kind blocker
amux notes                 # notes visible to you (workspace + task + agent)
amux ctx                   # roster + your visible notes
```

Scopes, least-to-most visible: `agent` (only you) → `task` (your task window)
→ `workspace` (whole session). `amux ctx` shows notes you can see. Use
`--scope workspace` for a note everyone in the session should read.

### Note or message?

Both reach your teammates, but they cost the receiver very differently. A note
is *pull*: it lands in the store and a teammate reads it when they next run
`amux ctx`. `amux send` is *push*: it types into the receiver's prompt, and the
receiver acts on it right away, or at its next turn boundary if it is busy.

**Prefer `amux note`** — the default — when you are recording rather than
asking:

- progress on the job you are already on ("migration applied, 26 tests green")
- a summary of what you did and why, for whoever picks this up next
- a decision worth preserving (`--kind decision`), a finding (`--kind finding`),
  or something that blocks you (`--kind blocker`)

**Prefer a message** when the receiver needs to act, and act now:

- delegating work to a specific agent
- requesting a review, or sending one back
- anything genuinely urgent or time-ordered

The test is whether you need someone to *do* something. "Here is what happened"
is a note. "Please review `%67`'s branch and reply" is a message. Status pushed
into a pane interrupts an agent mid-task to tell it something it never asked
for; the same text as a note costs nothing until it is wanted.

Both are durable. `amux messages` retains the sender, receiver, body, deadline,
and delivery result; `amux notes` additionally retains an explicit scope and
kind for knowledge meant to be pulled later.

## Messaging teammates

Address the receiver by the `%N` pane id shown by `amux ctx`:

```sh
amux send %9 "auth tests pass; please review src/auth and reply to %7"
```

Do not build an envelope yourself. Amux resolves your live identity and types:

```text
[amux brave-hawk @r0c1 %7 message #42] auth tests pass; please review src/auth and reply to %7
```

The command waits behind any other sender, waits while the target is starting
or waiting on input (`needs-input`), revalidates the pane, and submits once.
It does not wait for a busy target to finish: claude and codex both take a
message typed during a turn and act on it at the next turn boundary.

- **Idle target.** `delivered` means that pane emitted a fresh `busy` event
  after submission, so it started working on the message.
- **Busy target.** `delivered` with reason `queued` means the target's
  interface took the text off its input line. Its own busy events cannot
  prove more, because it was already working.
- **A prompt meant for a human on screen,** such as a numbered chooser or
  Claude Code's `1 to review · 2 to send · 0 to dismiss`, means nothing is
  typed. The send fails as undelivered and says so, because keys typed into
  such a prompt would answer it.

The default 300 second timeout covers the whole transaction; use
`--timeout S` to change it.

Failures print `undelivered` to stderr, exit nonzero, and remain queryable:

```sh
amux messages --status undelivered
amux messages --json -n 20
```

This evidence is intentionally conservative. If an idle receiver processed the
prompt but its hooks emitted no `busy` event, amux still records `undelivered`.
Do not bypass that result with raw `tmux send-keys`; report it and inspect
`amux messages` so the sender remains aware that processing was not confirmed.

## Attaching

```sh
tmux -L amux-root attach -t myproj      # attach to a workspace
tmux -L amux-root ls                    # raw view of the amux server
```

## Common mistakes

- **`amux -L foo spw` vs `amux spw -L foo`** — the socket flag is global and
  must precede the subcommand.
- **Using a name or label as a message target.** `amux send` takes the explicit
  `%N` pane id from `amux ctx`, avoiding name and grid-label collisions.
- **Treating tmux keystroke acceptance as delivery.** Use `amux send`, not raw
  `send-keys`; only a fresh target `busy` event records `delivered`.
- **Ignoring an undelivered result.** It is durable and intentionally
  conservative. Inspect `amux messages --status undelivered`; do not silently
  resend through tmux.
- **Expecting events outside amux.** `emit` and `wait` are no-ops unless `$TMUX`
  points at the `amux-root` socket. `ctx` without `--pane` needs `$TMUX_PANE`;
  from outside, pass `--pane %7` explicitly.
- **Reusing a workspace name.** `spw` errors if the session exists — use `spg`
  to add a task to it, or `kw` first.
- **Counting panes wrong.** `-a claude:3` in a `-r 2 -c 2` grid leaves one pane;
  a second countless spec is an error, not a fill.
- **Using plain `tmux`.** Without `-L amux-root` you're looking at a different
  server and won't see any agents.
- **Editing in the shared checkout.** In a git-repo workspace your cwd is your
  private worktree; `amux ctx` shows your branch. Never assume a teammate's
  files exist at your path — check `amux ctx` for their worktree, or just
  integrate and read the integration branch.
- **Pinging a teammate for status.** `amux ctx` shows state, branch, last
  commit, and notes for the whole team. Reach for messaging only when you need
  a conversation, not a status read.
- **Messaging what should have been a note.** Progress and summaries go in
  `amux note`; it waits until the reader wants it. Push into a pane only to
  delegate, request or return a review, or when it is urgent — see
  *Note or message?*
- **Cramming unrelated work into your current task.** You may spawn. A new unit
  of work that would only be diluted by your history wants `spg`; a change in
  another repo wants `spw` — see *Spawning work yourself*.
- **Assuming you are on the host.** Check `amux ctx` for a `runtime:` line. In a
  sandbox, half the commands refuse, your teammates' files are in other VMs, and
  uncommitted work is invisible to everyone.
- **Trying to route around a sandbox boundary refusal.** It is not a transient
  error and no flag unlocks it — host control has no permission that could grant
  it. Leave a note or message a host agent instead.
- **Leaving work uncommitted in a sandbox.** `integrate` fetches committed
  branches only, and cleanup can discard the VM. Uncommitted is not "in review",
  it is gone.
- **Treating a context-service outage as something to work around.** There is no
  fallback path by design — no mounted database, no local copy, no unauthenticated
  write. Report it; do not invent one.
- **Spawning and walking away.** New agents start cold, and notes do not cross
  workspaces. Say why in a note before you spawn, then message the new agent its
  task, or it will never learn what you spawned it for. (A `codex` agent does
  wake by itself to read amux's skill — that is amux's bootstrap message, not
  your task reaching it.)
