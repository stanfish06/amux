## Context

The sandbox path already solved half of this. `sandbox_bootstrap.skill_source()` resolves amux's own `skills/amux/SKILL.md` from either a checkout or a PyInstaller bundle, `sandbox_bootstrap.install_skill()` copies it into the sandbox at `$HOME/<skills_relpath>/amux/SKILL.md`, and `sandbox_hooks.AgentHooks.skills_relpath` already carries the per-agent directory (`.claude/skills`, `.codex/skills`) — "the same pair `make install_skills` links on a host", as its own comment says. Failure is captured in `SkillInstalled.reason` and reported per agent without failing the grid.

The host has none of it. A host agent's skill comes from `make install_skills`, which symlinks `skills/<name>` into `~/.claude/skills` and `~/.codex/skills`. If amux was installed any other way, there is no document. And presence is not activation: Claude Code and Codex both load skills lazily by description matching, so even an installed document may never be opened — which is the failure the amux skill's own "Common mistakes" section is a list of.

Launch composition happens in two places, the same two the `choose-agent-model-and-effort` change touches:

- `HostRuntime.prepare` — `AGENT_COMMANDS.get(spec.agent, spec.agent)`, sent to the pane as `send-keys` text.
- `sandbox.attach_argv(name, agent)` — `sbx run --name <name>`, plus `<agent> -- <extra>` when the agent needs extra arguments.

Verified against the installed CLIs (`claude` 2.1.223, `codex-cli` 0.146.0): `claude` has `--append-system-prompt <prompt>`, and also an `--append-system-prompt-file` variant — undocumented in the flag table, named only in the `--bare` entry's prose, and confirmed real by giving it a missing path (`Error: Append system prompt file not found:` rather than `error: unknown option`). `codex` has no system-prompt append of any kind, only a positional prompt and `-c key=value` overrides. That asymmetry is what forces two different activation mechanisms.

The inline flag is still the right one: a file would need a lifetime as long as the agent's session plus cleanup, and it does nothing for `codex`, which is where the asymmetry actually lives.

## Goals / Non-Goals

**Goals:**

- Every host-spawned `claude` or `codex` agent has amux's skill on disk, with no prerequisite step.
- Every such agent is told the skill applies to it, rather than left to match on it.
- Host and `docker-sandbox` runtimes behave the same.
- A failure in either step costs that agent its document, not the grid.

**Non-Goals:**

- Installing amux's *other* skills (`skills/` currently holds only `amux`, but the Makefile globs; this change ships one document, deliberately).
- Inlining the whole document into a system prompt. Rejected during proposal: ~500 lines in every pane, and codex could not accept it anyway.
- Teaching amux to update the document mid-session.
- Changing how `make install_skills` works. It stays, and stays useful; it is simply no longer the only path, and no longer survives a spawn.

## Decisions

### Reuse the sandbox's source resolution and per-agent paths

`skill_source()` and `sandbox_hooks.hooks_for(agent).skills_relpath` already answer "which document" and "which directory". The host install is a third caller, not a second implementation. A `HostSkillInstalled` result mirroring `SkillInstalled` (a path, or a reason) keeps the reporting shape identical, so `runtime.py` prints host and sandbox failures the same way.

Naming: the existing `install_skill` takes `SandboxOps`; the host version needs no ops object, so it becomes a sibling — `install_host_skill(agent, *, home=None, source=None)` — with `home` injectable so tests never touch a real `$HOME`.

### Unconditional overwrite, and replace a symlink rather than write through it

The chosen policy is to overwrite whatever is at the destination. The one refinement the policy needs is that a symlinked `~/.claude/skills/amux` must be *replaced*, not followed: writing through it would edit `skills/amux/SKILL.md` inside the developer's checkout, which is a working tree amux has no business modifying. So the install unlinks the destination path when it is a symlink, then writes a real directory and file.

Dedupe by destination: a grid of four `claude` agents resolves one destination and writes once. Failure is still attributed to every agent that wanted it, since that is what the operator needs to know.

*Alternatives considered.* "Never clobber a symlink" was considered and rejected in favour of a single unconditional rule — it made behaviour depend on how the machine happened to be set up, which is the ambiguity this change exists to remove. The cost is recorded under Risks.

### This reverses the `--share-skills` rule, and that closes a gap

`DockerSandboxRuntime.prepare` currently skips the in-sandbox install when `share_skills` is on, reasoning that the sandbox's skill directory is then backed by `~/.claude/skills`, "where `make install_skills` keeps a symlink into this repository", and writing there "would push a file across the boundary the wrong way".

That premise no longer holds: writing there is now what amux does on every host spawn. Keeping the skip while adding nothing in its place would leave exactly one configuration — a sandbox grid with `--share-skills` on a machine that never ran `make install_skills` — with no document at all. So the skip stays (writing from inside the VM into the host's directory really is the wrong direction) and the host-side install runs for that case instead. The comment at that site must be rewritten, not just re-flagged; as written it now argues against behaviour amux has adopted.

### Activation: a pointer, not the document

A short pointer — the agent is in an amux pane, amux's skill is the vocabulary for coordination, spawning, messaging, worktrees, and state, read it before doing any of those, and it is invocable by name — lives as one constant in `shared.py`. Both mechanisms carry the same text, so there is one thing to keep true.

- `claude`: `--append-system-prompt <pointer>`, appended to the existing flags at both seams, `shlex`-quoted for the host's `send-keys` text and for `attach_command`'s flattening.
- `codex`: a bootstrap message sent into the pane.

Using codex's positional `[PROMPT]` argument instead of a message was rejected: it is indistinguishable from the human's own first prompt, and it puts the pointer *before* the session exists rather than as an instruction the agent can act on and then report about.

### `Launch` grows a post-readiness payload

Today `Launch.keys` is typed into a *shell*, before the agent process exists. A bootstrap message must reach the *agent*, after its TUI is up. Those are different lifecycles, so they cannot share a field: add `Launch.bootstrap: str = ""`, and have `core._build_grid` send it after the launch keys, through a helper that waits for readiness.

The helper follows the discipline amux's own skill mandates for messaging a teammate, for the same reason — the failure mode is identical:

1. Poll `capture-pane` until the pane looks like a ready agent interface, bounded by a timeout.
2. `send-keys` the text with no trailing `Enter` (`enter=False`, and without libtmux's history-suppressing leading space).
3. Pause, then `send-keys Enter` on its own.
4. `capture-pane` again; if the text is still on the input line, send `Enter` again rather than re-sending the text, which would duplicate it.
5. On timeout or unconfirmed submission, report the agent and move on.

Readiness has no signal to subscribe to. `amux event wait` cannot serve: a freshly spawned pane is stamped `starting` and settles to `idle` a few seconds later precisely *because* nothing on the agent side announces "my prompt is ready", so waiting on state would return before the TUI exists. A bounded `capture-pane` poll is the only honest mechanism available.

### Bootstrap sends are serialized after the grid is built

`_build_grid` sends launch keys to every pane, then does bootstrap sends. Doing them inline per pane would make each codex pane's readiness wait delay the next pane's launch. The panes are already all created by then, so a wait costs only activation latency, not grid construction.

### Installed skills are not rolled back

`GridCreationError` unwinding removes what the grid created. The skill document is not that: it is shared with every other grid on the machine and is idempotent. Removing it during rollback could take the document away from a healthy grid. It stays.

## Risks / Trade-offs

- **Overwrite breaks this repo's own dogfooding loop.** After any spawn, `~/.claude/skills/amux` is a frozen copy, so editing `skills/amux/SKILL.md` in the checkout no longer affects newly spawned agents — on the one machine where that edit loop matters most, since amux is developed with amux. → Mitigation: the install reports the path when it replaces a symlink or differing file, the `install_skills` target says it is transient, and re-running `make install_skills` restores the link. If this proves painful in practice the smallest fix is an opt-out for developers rather than reintroducing a conditional rule; noted as an open question, not designed in.
- **A file lands in `$HOME` as a side effect of spawning.** Spawning previously wrote only under `$XDG_STATE_HOME`; now it writes into a directory the user curates by hand. → Mitigation: exactly one path per agent kind, named in output, never written through a symlink, and never removed by amux.
- **The codex bootstrap consumes a turn.** The agent leaves its idle-waiting-for-a-prompt state on its own and spends a turn reading a document, which costs tokens and means "idle right after spawn" no longer implies "untouched". → Mitigation: the pointer is short and asks for one read; the skill's own claim that new agents sit idle until prompted needs updating for codex, or it becomes a lie the skill tells about itself.
- **Readiness detection is a heuristic.** Matching a TUI's rendered input box is inherently fragile and will drift when codex changes its interface. → Mitigation: the failure is a reported timeout, never a hang and never a spawn failure; keep the match as loose as it can be while still distinguishing a ready interface from a starting one, and test it against captured fixtures rather than a live agent.
- **A swallowed or duplicated message is worse than none.** Sending too early loses the text; retrying the text instead of `Enter` sends it twice. → Mitigation: the verify-then-`Enter`-only-again rule above, which is the same rule the skill gives humans and agents.
- **Two changes edit the same lines.** `choose-agent-model-and-effort` also appends arguments in `HostRuntime.prepare` and `sandbox.attach_argv`. → Mitigation: both compose into an argument list rather than concatenating strings, so whichever lands second appends to the same seam; if that change lands first, reuse its render-and-quote helper instead of adding a second one.
- **The document is not the same as the installed document.** An agent may read a stale copy if amux was upgraded but its skill source moved. → Mitigation: overwrite on every spawn is exactly what makes this self-correcting; it is the upside of the chosen policy.

## Migration Plan

1. Add `install_host_skill` and its result type beside the existing sandbox install; unit-test against an injected `home`.
2. Wire it into `HostRuntime.prepare`, with per-agent reporting.
3. Add the pointer constant and append `--append-system-prompt` for `claude` at both seams.
4. Add `Launch.bootstrap`, the readiness-and-send helper, and codex's bootstrap message at both seams.
5. Rewrite the `--share-skills` branch and its comment so the host-side install covers that case.
6. Update `README.md`, the `Makefile` comment, and `skills/amux/SKILL.md`.

Rollback is a revert. The only durable artifact is a file in the user's skill directory, which a revert leaves in place and which `make install_skills` can turn back into a symlink.

## Open Questions

- Should there be an opt-out (`AMUX_SKILL_INSTALL=0` or similar) for developers editing the checkout copy? Deferred: it reintroduces configuration-dependent behaviour, and the reported path may be enough. Revisit after living with it.
- Should the bootstrap mechanism be generalized into the `amux send` command the skill lists as a project goal? It is the same primitive — wait for readiness, send text, send `Enter`, verify — and building it here twice would be waste. Out of scope, but the helper should be shaped so that command can use it unchanged.
- Should `install_skills`' glob over `skills/*` mean amux installs every skill it ships, if it ever ships more than one? This change installs `amux` only.
