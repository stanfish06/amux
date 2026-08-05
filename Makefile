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

dev:
	uv sync --extra dev

# The sandbox shim must ship as DATA, not just as a compiled module. PyInstaller
# puts amux's modules in the archive inside the executable and no .py on disk, but
# `docker-sandbox` spawning copies sandbox_client.py into the microVM as a file --
# so without this a packaged amux dies at shim installation on every sandbox spawn.
# Affects --onefile and --onedir alike, so both get it from PYINSTALLER_MODE.
#
# Two things this line cannot get wrong quietly:
#   $(CURDIR) is required. --add-data resolves a relative source against
#   --specpath, which is `build` below, so the relative form fails the build with
#   "Unable to find build/src/amux/sandbox_client.py".
#   The `:amux` destination must stay. It is what makes the unpacked file land
#   exactly where sandbox_client.__file__ points; changing it re-breaks shim
#   installation with no build error at all.
build: dev
	env -u PYTHONPATH $(VENV)/bin/pyinstaller $(PYINSTALLER_MODE) --name amux \
		--paths src \
		--add-data $(CURDIR)/src/amux/sandbox_client.py:amux \
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
