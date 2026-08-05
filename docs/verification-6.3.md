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

Zero conflicts. The merge used the feature this change adds, on its own change.

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

## What this record does not establish

- **Whether a real sandboxed agent works end to end.** Every check above is
  either offline or host-side. Task 6.4 is the live smoke test.
- **Anything about a Linux `--onefile` build.** `PYINSTALLER_MODE` is
  `--onefile` except on Darwin, and this host is Darwin, so only `--onedir` was
  built here. The packaging break was measured in both modes, but only
  `--onedir` was built by `make` and probed.
- **Four-agent resource behaviour.** That is a 6.4 measurement.

Three defects in this change were reachable *only* by leaving the test suite —
Codex hooks silently skipped without `--dangerously-bypass-hook-trust`,
`sbx cp` landing files under the host uid so an agent could not read its own
capability, and the packaging break above. None was visible from a green suite,
and two of them made sandbox spawning impossible. Treat a green run here as
necessary and not sufficient.
