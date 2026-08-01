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

dev:
	pipenv run pip install -e ".[dev]"

build: dev
	pipenv run env -u PYTHONPATH pyinstaller $(PYINSTALLER_MODE) --name amux \
		--paths src \
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
