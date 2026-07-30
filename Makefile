.PHONY: shell sync_pylock start_fresh clean_full dev build install uninstall

BIN_DIR := $(HOME)/.local/bin

dev:
	pipenv run pip install -e ".[dev]"

build: dev
	pipenv run env -u PYTHONPATH pyinstaller --onefile --name amux \
		--paths src \
		--specpath build --workpath build --distpath dist \
		-y src/amux/cli.py

install: build
	mkdir -p $(BIN_DIR)
	ln -sf $(CURDIR)/dist/amux $(BIN_DIR)/amux
	@echo "linked $(BIN_DIR)/amux -> $(CURDIR)/dist/amux"

uninstall:
	rm -f $(BIN_DIR)/amux

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
