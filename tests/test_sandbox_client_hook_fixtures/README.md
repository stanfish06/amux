# Sandbox agent hook fixtures

Each file stands in for the agent configuration a Docker Sandbox image ships
with. The merge logic in `src/amux/sandbox_hooks.py` is tested entirely against
these, so it needs no live sandbox.

**These are ASSUMED, not recorded.** When they were written, `sbx policy init`
had not been run on the host, so no sandbox could be created and no real image
could be inspected. The *formats* are taken from verified working host
configuration (a real `~/.claude/settings.json` that amux relies on today, and a
real `~/.codex/config.toml`); the *contents* are representative.

## Re-recording against a real image

    sbx create --name probe-claude claude /tmp/disposable-repo
    sbx exec probe-claude sh -lc 'cat "$HOME/.claude/settings.json"'
    sbx rm -f probe-claude

Then, for each agent:

1. Overwrite the `*_template.*` file with what the image actually ships.
2. Correct `settings_relpath` on `CLAUDE` / `CODEX` in `sandbox_hooks.py` if the
   location differs, and set `paths_are_assumed=False`.
3. Re-run `pytest tests/test_sandbox_client_hooks.py`. Failures are real
   findings about the image, not test breakage.

The other fixtures here are deliberate edge cases and stay as they are: they
exist to prove the merge preserves settings it did not write.
