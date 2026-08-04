---
name: amux
description: Use when orchestrating AI agents in tmux with the amux CLI — spawning or killing agent grids (`amux spw`/`spg`/`kw`/`kg`), listing them (`lsw`/`lsg`), mixed claude+codex grids, the `amux-root` tmux socket, agent state events (`amux event emit`/`state`/`tail`/`wait`), per-agent git worktrees and task integration branches (`amux integrate`), scoped context notes (`amux note`/`notes`), sending messages from one agent pane to another, when an agent needs to discover which workspace, task, and pane it is running in (`amux ctx`), or when an agent needs to start work elsewhere — a task whose context no longer fits the current window, or a change that belongs to another repo.
---

# amux

Agent orchestration on top of tmux. One dedicated tmux server (socket `amux-root`)
holds every agent, so amux never touches your interactive tmux sessions.

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
| `amux spg <ws> <task> [-p DIR] [-a SPEC] [-r N] [-c N]` | add a task grid to an existing workspace |
| `amux lsw` | list all workspaces, tasks, panes |
| `amux lsg <ws>` | list task grids in one workspace |
| `amux monitor [-W COLS] [-T COLS] [-i MS]` | live read-only dashboard of every workspace and agent |
| `amux kg <ws> <task> [--clean]` | kill one task (--clean also removes its worktrees) |
| `amux kw <ws> [--clean]` | kill a whole workspace |
| `amux ctx [--json] [--pane ID]` | this agent's identity + workspace team roster + visible notes |
| `amux note <text> [--scope agent\|task\|workspace] [--kind note\|decision\|finding\|blocker]` | publish a scoped context note |
| `amux notes [--workspace WS] [--task T] [--scope S] [--kind K] [-n N] [--json]` | list scoped notes |
| `amux integrate <ws> <task> [--agent NAME...] [--all]` | merge agent worktree branches into the task integration branch |
| `amux event state [--json]` | resolved state of every agent on the server |
| `amux event tail [-n N] [--pane ID] [--workspace WS] [--task T]` | recent state events as JSONL |
| `amux event wait <pane> [--timeout S]` | block until a pane goes idle / needs-input / dead |
| `amux event emit <kind> [--pane ID] [--agent K] [--detail T]` | record a state change (for hooks) |

`-p` defaults to `$PWD` for `spw`, to the workspace dir for `spg`. `-t` defaults
to `task0`. Global `-L/--socket-name` must come **before** the subcommand.

## Agent specs and grid shape

`-a AGENT[:COUNT]` is repeatable and fills panes row-major. `AGENT` is `claude`,
`codex`, or any raw shell command. Default is one `claude`.

```sh
amux spw myproj -p ~/Git/myproj -r 2 -c 2   # 2x2, all claude
amux spg myproj review -a codex:2           # 1x2, both codex
amux spg myproj fix -a claude:3 -a codex    # 2x2, 3 claude + 1 codex
amux spg myproj shell -a bash               # raw command instead of an agent
```

Shape resolution, given `n` total agents:

- both `-r` and `-c`: must satisfy `r*c == n`, else error.
- one of them: the other is `n / given`; must divide evenly, else error.
- neither: the factor pair closest to square, rows ≤ cols (so 6 → 2x3).

Counts and shape interact: **when both `-r` and `-c` are given, exactly one spec
may omit its count** and it absorbs the remaining panes (`-r 2 -c 2 -a claude:3
-a codex` → the lone `codex` fills 1). With unknown or partial shape, a missing
count means 1.

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
or they will sit idle waiting for a prompt.

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

`spawn` is amux's own: spawning a grid stamps every new pane with it, so an
agent has a state from its first moment. The rest come from agent hooks — Claude
Code's `PreToolUse` → `busy`, `Stop` → `stop`, `Notification` → `notify`,
`SessionEnd` → `exit`. With `--detail` omitted, the detail is pulled from the
hook's JSON on stdin (`message` / `tool_name` / `reason`). `emit` swallows all
errors so a hook never looks like agent failure.

An agent that has come up but has never been prompted reads `idle`, not
`starting`: nothing on the agent side announces "my prompt is ready", so
`starting` settles to `idle` a few seconds after spawn.

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
`amux ctx`. A `send-keys` message is *push*: it types into a running agent's
prompt and interrupts whatever it was doing.

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

Notes also outlive the pane. A message exists only in that agent's scrollback,
so it is gone when the pane dies; a note keeps its worktree, repo, scope, and
kind, and is still queryable afterwards with `amux notes`.

## Messaging teammates

There is no `amux send`/`read` yet — it's a project goal. Today you push text
into a teammate's pane with raw tmux:

```sh
tmux -L amux-root send-keys -t %9 'your message' Enter
```

**Always lead with your own identity.** `send-keys` types raw keystrokes into
the target agent's prompt — there is no envelope, no sender field, nothing that
distinguishes your message from the receiver's own human operator or from a
third teammate. An unattributed message leaves the receiver unable to reply or
to weigh who is asking. Take your name, label, and pane id from `amux ctx` and
prefix every message:

```sh
# from brave-hawk (@r0c1 %7) asking golden-owl (%9) for a review
tmux -L amux-root send-keys -t %9 \
  '[amux brave-hawk @r0c1 %7] auth tests pass; please review src/auth and reply to %7' Enter
```

Include the pane id, not just the name — it's what the other agent needs to
address you back. When you expect a reply, say so explicitly and name your pane;
the receiver has no return channel otherwise.

### Send the text, then Enter separately

A message with no submit key just sits in the receiver's input box. Nothing
errors, the sender sees success, and the other agent never wakes up. Agent TUIs
also read a trailing `Enter` in the *same* `send-keys` call inconsistently — it
can be absorbed into the input as a literal newline instead of submitting. Send
the text, pause, then send `Enter` on its own:

```sh
tmux -L amux-root send-keys -t %9 '[amux brave-hawk @r0c1 %7] please review src/auth and reply to %7'
sleep 0.3
tmux -L amux-root send-keys -t %9 Enter
```

Then confirm it actually went through — the receiver's state should leave `idle`
(`amux ctx`), or look at the pane and check the box is empty:

```sh
tmux -L amux-root capture-pane -p -t %9 | tail -5
```

If your text is still visible on the input line, it was never submitted; send
`Enter` again rather than re-sending the message (that would duplicate it).

Before sending, check the target's state in `amux ctx` (or block on
`amux event wait %9`) — keystrokes sent to a `busy` agent land mid-run and may
be swallowed by whatever prompt is active.

## Attaching

```sh
tmux -L amux-root attach -t myproj      # attach to a workspace
tmux -L amux-root ls                    # raw view of the amux server
```

## Common mistakes

- **`amux -L foo spw` vs `amux spw -L foo`** — the socket flag is global and
  must precede the subcommand.
- **Inventing a messaging command.** There is no `amux send`/`read` yet — see
  *Messaging teammates* for the `send-keys` workaround.
- **Sending an unsigned message.** A bare `send-keys` payload arrives with no
  sender. Prefix `[amux <name> @<label> <pane>]` so the receiver knows who wrote
  it and where to reply.
- **Forgetting to submit.** Text sent without a separate `Enter` sits in the
  receiver's input box forever and silently succeeds from your side. Send the
  text, pause, send `Enter`, then verify the input line is clear.
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
- **Spawning and walking away.** New agents start cold, and notes do not cross
  workspaces. Say why in a note before you spawn, then message the new agent its
  task, or it will sit idle.
