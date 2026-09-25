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

build: dev
	env -u PYTHONPATH $(VENV)/bin/pyinstaller $(PYINSTALLER_MODE) --name amux \
		--paths src \
		--add-data $(CURDIR)/src/amux/sandbox_client.py:amux \
		--add-data $(CURDIR)/skills/amux/SKILL.md:amux/skills/amux \
		--specpath build --workpath build --distpath dist \
		-y src/amux/cli.py

install: build install_skills
	mkdir -p $(BIN_DIR)
	ln -sfn $(AMUX_BIN) $(BIN_DIR)/amux
	@echo "linked $(BIN_DIR)/amux -> $(AMUX_BIN)"

install_skills:
	@for dir in $(SKILL_DIRS); do \
		mkdir -p $$dir; \
		for skill in $(SKILLS); do \
			rm -rf $$dir/$$skill; \
			ln -sfn $(CURDIR)/skills/$$skill $$dir/$$skill; \
			echo "linked $$dir/$$skill -> $(CURDIR)/skills/$$skill"; \
		done; \
	done

uninstall:
	rm -f $(BIN_DIR)/amux
	@for dir in $(SKILL_DIRS); do \
		for skill in $(SKILLS); do \
			rm -rf $$dir/$$skill; \
			echo "removed $$dir/$$skill"; \
		done; \
	done
