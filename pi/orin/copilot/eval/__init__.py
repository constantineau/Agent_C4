"""Matcher eval harness — docs/MATCHER_LORA_PLAN.md step 0 (build the eval FIRST, train only if
the stock 7B fails the gates). Dual-use: this is also Phase D's test rig for the Tier-2 matcher.

    export PYTHONPATH=../../vps/agent:../..         # generation renders seeds with engine code
    python3 -m copilot.eval.gen_corpus --out /tmp/matcher_corpus.jsonl     # dev VM, eval split
    python3 -m copilot.eval.run_eval --corpus ... --dry                    # pipe sanity
    python3 -m copilot.eval.run_eval --corpus ...                          # on the Orin: BLIND gate
    python3 -m copilot.eval.run_eval --corpus ... --hinted                 # diagnostic only
    python3 -m copilot.eval.test_eval                                      # harness tests
"""
