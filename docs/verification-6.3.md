# Verification record — task 6.3

What was run to verify `prototype-sandbox-agents-context-service`, with the
exact commands and their outcomes. Written so the result is reproducible and so
the limits of it are visible; the spec-by-spec coverage map is a separate
document, and the live smoke test is task 6.4.

**Tree under test:** branch `amux/amux/docker/integration` at **`efe8672`**,
worktree `$XDG_STATE_HOME/amux/worktrees/amux/docker/_integration`.

This is the first tree that contains the whole change. Every check below was
run against it, because a single agent branch passes while proving nothing — an
earlier package build on one branch produced a frozen binary with none of the
sandbox CLI surface in it, and looked entirely healthy.

## How the tree was assembled

```sh
amux integrate amux docker --all
```

```
merged clever-mole  (amux/amux/docker/clever-mole)  — 13 commit(s),  9 files, +2153/−30
merged swift-crane  (amux/amux/docker/swift-crane)  — 35 commit(s), 45 files, +15251/−51
merged happy-deer   (amux/amux/docker/happy-deer)   —  7 commit(s), 21 files,  +814/−169
merged misty-panda  (amux/amux/docker/misty-panda)  — 13 commit(s), 18 files, +2408/−113
```

Zero conflicts.

**Correction, and it matters for how much this proves.** An earlier draft said
this "used the feature this change adds, on its own change". It did not. `amux`
on `PATH` is a binary built before any of this work — it contains no `doctor`
and no `context-service` subcommand at all — so every `amux integrate` run here
exercised the *pre-change* host-worktree path, not the sandbox-branch
integration this change adds. It is evidence that host integration still works,
and nothing more.

A second `amux integrate` refused with `no active worktrees for task 'docker'`,
because the first pass marks rows `merged` and `worktree.py` selects
`status == "active"`. The remaining merges here were done by hand with `git`.
That refusal was also observed with the pre-change binary, so treat it as
consistent with reading `worktree.py:289` rather than as a measurement of this
tree.

## Automated suite

```sh
uv sync --extra dev
.venv/bin/python -m pytest tests/ -q
```

| Run | Result |
|---|---|
| 1 | `771 passed in 123.27s` |
| 2 | `771 passed in 120.75s` |

Two complete runs, identical counts, no failures and no flakes between them.

Runtime is worth recording because it was 118.7s for 579 tests earlier in the
change and is now ~121s for 771. The difference is a fixed `serve_forever`
poll interval: the service was shut down with the library default of 0.5s while
`shutdown()` blocked on it, costing 84.6s of pure teardown wait across 167
teardowns. See `shutdown_poll_s`.

## Package build

```sh
make build
```

Exit 0. Produces `dist/amux/amux` (2883376 bytes) **and** ships the sandbox
shim as data:

```
dist/amux/_internal/amux/sandbox_client.py   26459 bytes
```

That file is the point of the build check. Without it a packaged amux cannot
spawn a sandbox at all — PyInstaller compiles modules into the PYZ and keeps no
`.py` on disk, while sandbox bootstrap copies `sandbox_client.py` into the
microVM *as a file*. Source checkouts and the host runtime were unaffected,
which is why 731 passing tests never saw it.

Two things about that build line cannot go wrong quietly, and both were
measured rather than reasoned about:

- `--add-data` resolves a relative source against `--specpath`, which is
  `build`, so the relative spelling **fails the build**.
- destination `amux` is what puts the file where `sandbox_client.__file__`
  already points, so no frozen-specific code is needed — and changing the
  destination re-breaks shim resolution with *no build error at all*. Sandbox
  preflight checks the shim resolves for exactly that reason.

`make dev`/`make build` previously invoked `pipenv`, which is not installed
here, so the documented build path could not be executed at all. Both now use
the `uv` venv the suite already uses. The `pipenv`-based `shell`/`sync_pylock`
targets are untouched — that is a separate Pipfile workflow, not the build.

## Frozen CLI probes

Run against `dist/amux/amux`, the packaged binary — not the source tree. A
harness control ran first, because a broken probe and a broken product look
identical in a summary:

| Probe | rc |
|---|---|
| `definitely-not-a-subcommand` (control, must fail) | 2 |
| `--help` | 0 |
| `spw --help` | 0 |
| `spg --help` | 0 |
| `doctor --help` | 0 |
| `context-service --help` | 0 |
| `context-service status` | 0 |
| `lsw` | 0 |
| `ctx --pane %34` | 0 |
| `event state` | 0 |

No `ModuleNotFoundError` and no missing hidden imports, so `context_service`,
`core`, `events`, `store`, `worktree`, `runtime`, `sandbox` and `libtmux` all
survive freezing.

### Frozen `doctor`, against the real `sbx`

The most informative single result, because every line is correct:

```
runtime docker-sandbox (optional backend) for …/_integration:
  [ok]   resources: 2 cpu, 4g
  [ok]   context-client: …/dist/amux/_internal/amux/sandbox_client.py
  [ok]   agents: claude
  [FAIL] repository: …/_integration is not a primary checkout
         fix: sbx create --clone cannot clone a secondary git worktree; …
  [ok]   sbx: v0.37.1
  [ok]   docker: all checks pass
  [FAIL] context-service: amux-context is not running
         fix: start it with `amux context-service start`
  [FAIL] network-policy: Denied: localhost:47317
         fix: sbx policy allow network localhost:47317

3 check(s) failed. amux changes nothing on its own: run the fixes above yourself.
```

- The shim resolves **from inside the bundle**, which is the packaging fix
  verified end to end.
- The three failures are all true of this host, each with an exact
  remediation, and nothing was silently repaired. "amux changes nothing on its
  own" is the no-silent-policy-widening requirement honoured in output.
- `repository: … is not a primary checkout` is a real constraint discovered
  here: the smoke test cannot run from any amux worktree, because
  `sbx create --clone` refuses a secondary git worktree.
- `docker: all checks pass` alongside `network-policy: Denied` is worth
  keeping. **Authenticated does not imply reachable** — `sbx diagnose` passes
  every check, including Authentication, while policy still blocks the port.

## Other checks

```sh
git diff --check master...HEAD     # clean
openspec validate prototype-sandbox-agents-context-service
```

```
Change 'prototype-sandbox-agents-context-service' is valid
```

## Test shapes: one that survived a later change, and one that did not

Worth recording because it decides who pays for a change later, and it came out
of an actual collision rather than a style preference.

The same test file asserts the `/v1/events/state` payload two ways:

```python
# pinned to a literal shape
assert set(payload["panes"][0]) == {"pane", "kind", …, "last_event"}

# equivalent to the native call
assert payload["panes"] == [
    p for p in events.pane_states("amux-root") if p["workspace"] == "proj"
]
```

Adding runtime identity to `pane_states` breaks the first and leaves the second
untouched, because both sides of an equivalence move together. The endpoints
written as native-equivalence assertions — `/v1/context`, `/v1/notes` — never
came up while working out the consequences of that change; the one pinned
literal was the only place it bit.

So: **a test pinned to a literal shape becomes someone else's maintenance
burden, while the same test written as an equivalence to the native call does
not.** That is not an argument against pinning — the pinned shape is exactly
what guards host output from gaining fields, and it should stay for that. It is
an argument for pinning deliberately, at the boundary you actually mean to
freeze, rather than by default.

Two more from the same suite, which together make the rule concrete rather than
a slogan.

**An equality can be an equivalence.** The note-receipt test asserts

```python
assert set(note) == set(stored) | {"name"}
```

That looks pinned but is not: it compares the receipt's keys to the *store
row's* keys, so a new `notes` column moves both sides and it survives. It is the
only pinned-looking assertion in those note tests, and it is pinned-looking for
exactly that reason.

**And deliberate pinning looks identical to accidental pinning.** Two adjacent
lines:

```python
assert cs.DEFAULT_WAIT_STATES == ("idle", "needs-input", "dead")   # deliberate
assert set(cs.DEFAULT_WAIT_STATES) <= set(cs.AGENT_STATES)        # accidental
```

The first pins a constant the service itself owns, so adding `stopped` to it
*should* fail and the owner should answer for it — that is the qualification
above, working as intended. The second stays true when `stopped` joins the state
vocabulary, where an equality would not have; it was subset-shaped by luck, and
is being left subset-shaped now that the reason is known.

The two sit one line apart in the same test, which is a fair picture of how
little of this was designed up front. The rule is worth having precisely because
the shapes are indistinguishable on sight — you can only tell them apart by
asking whose boundary is being frozen.

## The question that caught the most

Three claims in this change were overstated, and the same question found all
three. It is not "did you verify it" — everyone answers yes to that — but:

> **Verified against what?**

- The integration merge in this document was offered as dogfooding. It ran the
  `amux` on `PATH`, a binary built before this work with no `doctor` subcommand
  at all, so it exercised the pre-change host path.
- "Integrate is one-shot, so cleanup leaves you stranded" rested on reading a
  filter. Measured, half of it was already fixed, and the surviving half was
  *worse* than described in a different way — see below.
- A three-pane assertion was said to guard host output from gaining fields. It
  guarded the *no-execution-row* case; the host-row path was never reached, and
  a mutation passed against it.

A fourth is the same question applied to a property rather than a fact: the
spec-coverage map was reported complete because it had been built
scenario-by-scenario. That was true, but unknown to be true until a checker
extracted all 35 scenario titles and tested each for representation. Not "is it
complete" but *against what did you check completeness*.

The related habit worth keeping: **a test that arrives with its own fix is the
most likely place for a test that cannot fail**, because it was written against
already-correct code and nobody has seen it red. Every such test in this change
was mutated before its row was marked covered.

## What this record does not establish

- **Whether a real sandboxed agent works end to end.** Every check above is
  either offline or host-side. Task 6.4 is the live smoke test.
- **Anything about a Linux `--onefile` build.** `PYINSTALLER_MODE` is
  `--onefile` except on Darwin, and this host is Darwin, so only `--onedir` was
  built here. The packaging break was measured in both modes, but only
  `--onedir` was built by `make` and probed.
- **Four-agent resource behaviour.** That is a 6.4 measurement.
- **Anything about the monitor.** `amux monitor` was never run during this
  verification. `monitor.py` is only a launcher; the renderer is the Ink TUI in
  `tui/`, whose build output is gitignored (`tui/.gitignore`) and absent from the
  integration worktree — so the command cannot start there. Only the monitor's
  Python inputs are covered by tests. It is the one surface in this change that
  nobody has run.

Three defects in this change were reachable *only* by leaving the test suite —
Codex hooks silently skipped without `--dangerously-bypass-hook-trust`,
`sbx cp` landing files under the host uid so an agent could not read its own
capability, and the packaging break above. None was visible from a green suite,
and two of them made sandbox spawning impossible. Treat a green run here as
necessary and not sufficient.
