## Why

An agent amux spawns on the host only knows amux's vocabulary if a human previously ran `make install_skills`, and even then the skill is merely *discoverable* — nothing makes the agent read it. So the common failure is silent and expensive: the agent invents `amux send`, runs plain `tmux` against the wrong server, pastes status into a teammate's pane instead of leaving a note, edits in the shared checkout, or tries to route around a sandbox refusal, and only discovers the boundary by tripping over it. Sandboxes already get the document installed explicitly at bootstrap because nothing else would deliver it; host agents have no such guarantee, and neither runtime does anything to make sure the document is actually opened.

## What Changes

- Install amux's own `SKILL.md` at spawn for host-runtime agents, into the skill directory the agent actually reads (`~/.claude/skills/amux/` or `~/.codex/skills/amux/`), so the document's presence no longer depends on a Makefile target having been run.
- Write it unconditionally, overwriting whatever occupies that path, including a `make install_skills` symlink into this repository. **BREAKING** for this repo's own development loop: after a spawn, editing `skills/amux/SKILL.md` no longer affects newly spawned agents until `make install_skills` is re-run.
- **BREAKING** (internal rationale): reverse the rule at `runtime.py` that refuses to write into the host's skills directory under `--share-skills`. Writing there is now amux's declared behaviour, so under `--share-skills` the host-side install serves the sandbox instead of the in-VM write being skipped and nothing taking its place.
- Make the skill *active*, not merely present. A `claude` agent is launched with `--append-system-prompt` carrying a short pointer telling it that it is running inside an amux pane and that the amux skill governs coordination, spawning, messaging, and state.
- Give `codex` the same activation by the only route it has — `codex` has no system-prompt append flag — by sending a signed bootstrap message into its pane once its interface is ready, following amux's own messaging discipline: text first, then `Enter` separately, then verify submission.
- Apply activation under both runtimes: the host runtime appends to the launched command, and the `docker-sandbox` runtime appends to the agent's `sbx run ... <agent> -- <args>` argument list, with the codex bootstrap message sent into the pane in both cases.
- Never fail a spawn over it. A missing or unwritable skill document, or a bootstrap message that could not be confirmed, is reported per affected agent and the grid continues.
- Add a per-agent readiness-and-send mechanism to the launch seam, since a message typed at an agent TUI that has not finished starting is silently swallowed.

## Capabilities

### New Capabilities

- `agent-skill-injection`: Guaranteed delivery and activation of amux's own skill document for every spawned agent — installation into the skill directory the agent reads, per-agent activation through a system-prompt pointer or a bootstrap message, identical treatment across the host and Docker Sandbox runtimes, and degraded-but-continuing behaviour when either step fails.

### Modified Capabilities

None. This repository has no published OpenSpec capability specifications yet. The in-flight `sandbox-agent-runtime` change owns the existing in-sandbox skill install, whose `--share-skills` rule this change reverses; that reversal is specified here rather than by amending the other change.

## Impact

- Affected Python modules: `sandbox_bootstrap.py` (reuse `skill_source`, add a host-side install), `sandbox_hooks.py` (`skills_relpath` becomes a host-side path too), `runtime.py` (`HostRuntime.prepare` installs and appends, `DockerSandboxRuntime.prepare` loses the `--share-skills` skip), `sandbox.py` (`attach_argv` carries the pointer), `core.py` (send the bootstrap message after readiness), and `shared.py` (the pointer text and the skill-relative paths).
- `runtime.Launch` grows a field for text to send only after the agent's interface is ready, which is a different lifecycle from today's `keys` — those are typed into a shell before the agent exists.
- Writes into `$HOME` become part of spawning. amux already writes under `$XDG_STATE_HOME`, but not into a directory the user curates by hand, so the write must be reported and must not follow a symlink out to somewhere unexpected.
- Touches the same two command-composition sites as the `choose-agent-model-and-effort` change (`HostRuntime.prepare` and `sandbox.attach_argv`). Whichever lands second must compose with the first rather than replace it.
- Documentation: `README.md` gains the spawn-time install and its overwrite behaviour, the `Makefile` comment on `install_skills` needs to say the symlink is transient, and `skills/amux/SKILL.md` itself should state that amux installs and points at it, since agents read that file as authoritative.
- Startup cost per pane: one file write per distinct agent kind, and for codex a bounded readiness poll plus one consumed turn, since the bootstrap message takes the agent out of its idle-waiting-for-a-prompt state.
