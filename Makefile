# `make ci` is the single definition of the checks; GitHub CI runs exactly this
# (.github/workflows/ci.yml).
# make ci            — lint + every test file, each in a throwaway container off its own
#                      service's image built from this tree (tools/ci/run.sh). ~3 min warm.
# make ci ONLY=lab   — lint + only test files whose path contains "lab"
.PHONY: ci lint test
ci: lint test

.venv/bin/ruff:
	python3 -m venv .venv && .venv/bin/pip install -q ruff==0.16.10

lint: .venv/bin/ruff
	.venv/bin/ruff check .

test:
	tools/ci/run.sh $(ONLY)
