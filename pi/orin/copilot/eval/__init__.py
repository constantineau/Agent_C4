"""Matcher eval harness — docs/MATCHER_LORA_PLAN.md step 0 (build the eval FIRST, train only if
the stock 7B fails the gates). Dual-use: this is also Phase D's test rig for the Tier-2 matcher.

    python3 -m copilot.eval.gen_corpus --out /tmp/matcher_corpus.jsonl     # anywhere
    python3 -m copilot.eval.run_eval --corpus ... --dry                    # oracle/pipe sanity
    python3 -m copilot.eval.run_eval --corpus ...                          # on the Orin (real 7B)
    python3 -m copilot.eval.test_eval                                      # unit + lock-step tests
"""
