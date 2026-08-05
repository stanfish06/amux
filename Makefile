.PHONY: shell sync_pylock start_fresh clean_full dev build install install_skills uninstall

BIN_DIR := $(HOME)/.local/bin
SKILL_DIRS := $(HOME)/.claude/skills $(HOME)/.codex/skills
SKILLS := $(notdir $(wildcard skills/*))
UNAME_S := $(shell uname -s)
PYINSTALLER_MODE := --onefile
AMUX_BIN := $(CURDIR)/dist/amux

ifeq ($(UNAME_S),Darwin)
PYINSTALLER_MODE := --onedir
AMUX_BIN := $(CURDIR)/dist/amux/amux
endif

VENV := $(CURDIR)/.venv
PY := $(VENV)/bin/python

# The sandbox context client is read as a FILE at runtime and copied into a
# sandbox, so it must ship as data: PyInstaller compiles modules into the PYZ
# and keeps no source, and without this every sandbox spawn from a packaged
# amux dies installing the shim. Destination `amux` is load-bearing — it puts
# the file at exactly the path PyInstaller reports as sandbox_client.__file__,
# so the module resolves itself with no frozen-specific code. Moving the
# destination breaks that silently, with no build error; sandbox preflight
# checks the shim resolves for exactly that reason.
#
# The path must be ABSOLUTE: --add-data resolves relative to --specpath, which
# is `build` below, so a relative source path fails the build outright.
SHIM_DATA := $(CURDIR)/src/amux/sandbox_client.py:amux

dev:
	uv sync --extra dev

build: dev
	env -u PYTHONPATH $(PY) -m PyInstaller $(PYINSTALLER_MODE) --name amux \
		--paths src \
		--add-data "$(SHIM_DATA)" \
		--specpath build --workpath build --distpath dist \
		-y src/amux/cli.py

install: build install_skills
	mkdir -p $(BIN_DIR)
	ln -sfn $(AMUX_BIN) $(BIN_DIR)/amux
	@echo "linked $(BIN_DIR)/amux -> $(AMUX_BIN)"

# -n so an existing symlink is replaced, not followed into as a directory
install_skills:
	@for dir in $(SKILL_DIRS); do \
		mkdir -p $$dir; \
		for skill in $(SKILLS); do \
			ln -sfn $(CURDIR)/skills/$$skill $$dir/$$skill; \
			echo "linked $$dir/$$skill -> $(CURDIR)/skills/$$skill"; \
		done; \
	done

uninstall:
	rm -f $(BIN_DIR)/amux
	@for dir in $(SKILL_DIRS); do \
		for skill in $(SKILLS); do \
			rm -f $$dir/$$skill; \
			echo "removed $$dir/$$skill"; \
		done; \
	done

shell: sync_pylock
	pipenv shell

start_fresh: clean_full
	pipenv pylock --from-pyproject

clean_full: archive
	rm -f \
		Pipfile \
		Pipfile.lock \
		pylock.toml
	pipenv uninstall --all

archive:
	[ -f "pylock.toml" ] && cp -f pylock.toml pylock.toml.old || true
	[ -f "Pipfile" ] && cp -f Pipfile Pipfile.old || true
	[ -f "Pipfile.lock" ] && cp -f Pipfile.lock Pipfile.lock.old || true

sync_pylock:
	rm -f Pipfile.lock
	pipenv uninstall --all
	pipenv sync
	pipenv run pip install -e ".[dev]"
