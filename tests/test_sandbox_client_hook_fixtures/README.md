# Sandbox agent hook fixtures

Each file stands in for the agent configuration a Docker Sandbox image ships
with. The merge logic in `src/amux/sandbox_hooks.py` is tested entirely against
these, so it needs no live sandbox.

Two kinds of file, distinguished by name:

- `*_template.*` — **recorded** verbatim from a real image.
- `*_synthetic_*` — **invented**, for edge cases the real images do not exhibit.
  They are not stale recordings and should not be "corrected" to match an image.

## Recorded, and what was observed

Verified 2026-08-05 against `docker/sandbox-templates`, so
`AgentHooks.paths_are_assumed` is now `False` for both agents and
`HooksInstalled.location_verified` is `True`.

| | Claude | Codex |
|---|---|---|
| image | `claude-code-docker` | `codex-docker` |
| version | Claude Code 2.1.221 | codex-cli 0.146.0 |
| `HOME` / user | `/home/agent`, `agent:agent` | `/home/agent`, `agent:agent` |
| hook document | `~/.claude/settings.json` **ships**, 4 UI keys, no `hooks` | `~/.codex/hooks.json` **absent** — amux creates it |
| feature switch | n/a | `~/.codex/config.toml` ships, `[features]` absent |
| fixture | `claude_template.json` | `codex_template.toml` |

Neither image runs as `root`, which is why `install_client` resolves `$HOME`,
`id -un` and `id -gn` at run time rather than assuming.

## Findings from the live probe that shaped the code

- **An untrusted Codex hook is silently skipped** — it does not fire and does not
  prompt. Codex reads and validates `hooks.json` (it warns about timeouts) and
  then runs nothing, saying nothing about trust. The only supported way through
  is `codex --dangerously-bypass-hook-trust`, documented as "intended only for
  automation that already vets hook sources", which amux is. **Without that flag
  on the attach command, a sandboxed Codex reports no state at all.** Codex
  writes no `trusted_hash` of its own, so waiting for self-trust is not an
  option.
- **Codex caps `SessionEnd` at 3s** and warns on every run when asked for more
  ("clamping SessionEnd hook timeout to 3s"), hence
  `HOOK_TIMEOUT_OVERRIDES`. Verified live: asking for 3 removes the warning while
  hooks still fire.
- **`sbx cp` sets the owner to the host uid**, so every copied file must be
  chowned to the agent — see `sandbox_bootstrap._deliver`.
- **A fresh sandbox is logged out** unless the host ran
  `sbx secret set -g openai` / `claude` equivalents first. Hook dispatch does not
  depend on it: `UserPromptSubmit` fires before the model call, which is what
  made the trust probe possible at all with no credentials.
- `codex features list` is a usable oracle — it reported `hooks stable true` from
  the `config.toml` edit, confirming `enable_codex_hooks` works on the real
  image. `codex features enable hooks` would let codex write that itself and is a
  strictly more robust follow-up than editing TOML.

## Re-recording against a real image

    sbx create --name probe-claude claude /tmp/disposable-repo
    sbx exec probe-claude sh -lc 'echo $HOME; id -un; id -gn; claude --version'
    sbx exec probe-claude sh -lc 'cat "$HOME/.claude/settings.json"'
    sbx rm -f probe-claude

    sbx create --name probe-codex codex /tmp/disposable-repo
    sbx exec probe-codex sh -lc 'echo $HOME; id -un; id -gn; codex --version'
    sbx exec probe-codex sh -lc 'cat "$HOME/.codex/config.toml"; ls ~/.codex'
    sbx rm -f probe-codex

Use a disposable repository, never a real checkout. Requires
`sbx policy init <profile>` to have been run once on the host.

Then:

1. Overwrite the `*_template.*` files with what the image actually ships. Leave
   the `*_synthetic_*` ones alone.
2. Correct `settings_relpath` / `enable_relpath` on `CLAUDE` / `CODEX` if a
   location moved, and keep `paths_are_assumed=False` accurate.
3. If the probed `codex --version` is older than `CODEX_HOOKS_MIN_VERSION` but
   still ships `hooks.json`, lower that constant to the probed version — it is
   set to the earliest version actually verified.
4. Re-run `pytest tests/test_sandbox_client_hooks.py`. Failures are findings
   about the image, not test breakage.

To re-test a hook change in a *live* sandbox, delete the installed hook document
first: the merge is idempotent and will otherwise leave the existing amux entry
in place, including its old command and timeout.
