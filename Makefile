.PHONY: test host sync-hermes verify-hermes offline-release

test:
	scripts/run_tests.sh

host:
	PYTHONPATH=src python -m networkclaw_harness.host

sync-hermes:
	@test -n "$(HERMES_SOURCE)" || (echo "set HERMES_SOURCE=/path/to/networkclaw-hermes-fork" >&2; exit 2)
	python scripts/sync-hermes-runtime.py "$(HERMES_SOURCE)"

verify-hermes:
	python scripts/verify-hermes-vendor.py

offline-release:
	python scripts/build-offline-release.py

