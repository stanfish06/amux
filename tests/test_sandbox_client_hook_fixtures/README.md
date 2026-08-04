# Sandbox agent hook fixtures

Each file stands in for the agent configuration a Docker Sandbox image ships
with. The merge logic in `src/amux/sandbox_hooks.py` is tested entirely against
these, so it needs no live sandbox.

## What is verified and what is not

The **formats are verified**, taken from real working configuration on the
development host — the files amux relies on today:

| Fixture | Reference | Verified against |
|---|---|---|
| `claude_template*.json` | `~/.claude/settings.json` | Claude Code, live host config |
| `codex_hooks_template*.json` | `~/.codex/hooks.json` | codex-cli 0.146.0 |
| `codex_config_hooks_*.toml` | `~/.codex/config.toml` `[features]` | codex-cli 0.146.0 |
| `codex_template*.toml` | `~/.codex/config.toml` `notify` | the older, superseded slot |

The **file locations inside Docker's agent images are ASSUMED, not recorded.**
When these were written, `sbx policy init` had not been run on the host, so no
sandbox could be created and no image could be inspected.
`AgentHooks.paths_are_assumed` is `True` and `HooksInstalled.location_verified`
is `False` to carry that, so preflight can say "installed, location unverified"
instead of implying it checked.

The **image's Codex version is also unverified**, which is why the adapter
detects it rather than assuming. A Codex with `hooks.json` reaches full parity
with Claude; one old enough to have only the single `notify` slot reports `stop`
alone, and `HooksInstalled.missing_kinds` says which kinds are absent.

## Codex specifics worth knowing before you touch this

- `hooks.json` is **inert** unless `config.toml` carries `hooks = true` under
  `[features]`. It is not a top-level key.
- Codex has no `Notification` event. Its equivalent is `PermissionRequest`.
- `config.toml` also grows a `[hooks.state]` table where Codex records a
  `trusted_hash = "sha256:..."` per hook entry, keyed
  `<hooks.json path>:<event_snake_case>:<group>:<index>`. amux does **not** write
  those — what is hashed is undocumented, and a wrong hash is worse than an
  absent one. **Open question for the live smoke test:** whether an untrusted
  hook fires, prompts, or is skipped. A headless sandbox cannot answer a prompt,
  so if approval is required, Codex sandbox state is degraded in practice
  regardless of version.

## Re-recording against a real image

    sbx create --name probe-claude claude /tmp/disposable-repo
    sbx exec probe-claude sh -lc 'cat "$HOME/.claude/settings.json"'
    sbx exec probe-claude sh -lc 'ls -la "$HOME"'
    sbx rm -f probe-claude

    sbx create --name probe-codex codex /tmp/disposable-repo
    sbx exec probe-codex sh -lc 'codex --version; cat "$HOME/.codex/hooks.json"'
    sbx exec probe-codex sh -lc 'cat "$HOME/.codex/config.toml"'
    sbx rm -f probe-codex

Use a disposable repository, never a real checkout.

Then, for each agent:

1. Overwrite the `*_template*` file with what the image actually ships.
2. Correct `settings_relpath` / `enable_relpath` on `CLAUDE` / `CODEX` in
   `sandbox_hooks.py` if a location differs, and set `paths_are_assumed=False`.
3. If the probed `codex --version` is older than `CODEX_HOOKS_MIN_VERSION` but
   still ships `hooks.json`, lower that constant to the probed version — it is
   deliberately conservative, set to the earliest version actually verified.
4. Re-run `pytest tests/test_sandbox_client_hooks.py`. Failures are real findings
   about the image, not test breakage.

The remaining fixtures here are deliberate edge cases and stay as they are: they
exist to prove the merge preserves settings it did not write.
