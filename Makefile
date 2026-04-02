.PHONY: shell start_fresh clean_full

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
