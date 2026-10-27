# `make ci` is the single definition of the checks: every test file in the repo, each in a
# throwaway container off its own service's image built from this tree (tools/ci/run.sh).
# make ci            — everything (~3 min warm; the first build of the lab image takes longer)
# make ci ONLY=lab   — only test files whose path contains "lab"
.PHONY: ci
ci:
	tools/ci/run.sh $(ONLY)
