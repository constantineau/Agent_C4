"""Exit test for the eval harness itself: oracle lock-step vs the real matcher (where the engine
code is importable), generator/label consistency over a real corpus, held-out content, train ==
serve parity, metrics math, the blind gate, and the context budget. No LLM.

Corpus generation renders seeds with the engine's strategy code, so the generator tests need
vps/agent importable (the dev VM / CI). Elsewhere (the Orin venv) they SKIP — except under C4_CI,
where a skip would hide a break.

    cd pi/orin && PYTHONPATH=../../vps/agent:../.. python3 -m copilot.eval.test_eval
"""

import json
import os
import random
import sys

from . import gen_corpus, gen_train, libgen, metrics, oracle, run_eval, scengen
from .. import config, copilot, llm as llm_mod, playbook as playbook_mod


def _check(label, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    return bool(ok)


def _engine_ok():
    """True when the engine code imports (corpus generation possible); None = skipped here;
    False = missing under CI."""
    try:
        scengen._engine()
        return True
    except RuntimeError as e:
        if os.environ.get("C4_CI"):
            return _check(f"engine importable in CI ({e})", False)
        print(f"  [SKIP] engine not importable here — run on the VM ({e})")
        return None


def test_lockstep():
    """oracle.pred_ok must match matcher._pred_ok on a semantics grid — skipped (honestly) when
    the engine stack isn't importable (the Orin venv)."""
    print("\n== oracle lock-step vs app.matcher ==")
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    sys.path[:0] = [root, os.path.join(root, "vps", "agent")]   # PYTHONPATH=vps/agent:. equivalent
    try:
        from app.matcher import _pred_ok as real
    except Exception as e:
        if os.environ.get("C4_CI"):                  # CI has the engine stack: a skip there hides a break
            return _check(f"engine matcher importable in CI ({type(e).__name__}: {e})", False)
        print(f"  [SKIP] engine matcher not importable here ({type(e).__name__}) — run on the VM")
        return True
    grid = [(op, a, v)
            for op in (">=", "<=", "==")
            for a in (None, 0, 14.3, 15.0, 15.7, -12, True, False, "C0", ["J1", "C0"], [], "15")
            for v in (15, -12, 0.0, True, False, "C0", "c0", "15")]
    bad = [(op, a, v) for op, a, v in grid if oracle.pred_ok(op, a, v) != real(op, a, v)]
    agrees = _check(f"pred_ok agrees on {len(grid)} cases", not bad)
    if bad:     # was `_check(...) or print(...)`: None on a failure, so `ok &=` raised (#55)
        print("   drift:", bad[:5])
    return agrees


def test_generator_labels():
    """Every corpus label must be the literal oracle truth of its own snapshot, every near-miss
    must fail for its declared reason, and the blind label must only hold what the seed shows."""
    print("\n== generator/label consistency (300 examples) ==")
    eng = _engine_ok()
    if eng is not True:
        return eng is None
    rows = gen_corpus.generate(libraries=25, per_lib=12, seed=11)
    ok = True
    n_by_mode = {}
    blind_ok = True
    for r in rows:
        lib, sig, sus = r["bundle"], r["signals"], r["sustained"]
        plays = {p["id"]: p for p in lib["plays"]}
        smap = oracle.status_map(lib, sig, sus)
        ok &= smap == r["status_map"]
        ok &= sorted(p for p, s in smap.items() if s == "armed") == r["oracle"]["armed"]
        for pid, mode in r["oracle"]["near"].items():
            n_by_mode[mode] = n_by_mode.get(mode, 0) + 1
            play = plays[pid]
            if mode == "sustain":
                ok &= smap[pid] == "arming" and scengen._has_sustain(play)
            elif mode == "wrong_leg":
                ok &= smap[pid] == "quiet" and not oracle._applicable(play, sig)
            else:                                    # threshold / confounder stay quiet
                ok &= smap[pid] == "quiet"
        for view in ("hinted", "blind"):
            json.loads(r["views"][view]["seed"].split("\n", 1)[1])   # valid JSON payload
        bl = r["blind_oracle"]
        vis = set(r["visible"])
        blind_ok &= set(bl["armed"]) <= set(r["oracle"]["armed"])
        blind_ok &= all({q["signal"] for q in plays[pid]["conditions"]["predicates"]} <= vis
                        for pid in bl["armed"])
        blind_ok &= all(m in scengen.BLIND_MODES for m in bl["near"].values())
        blind_ok &= not (set(bl["excluded"]) & (set(bl["armed"]) | set(bl["near"])))
    ok = _check("labels == oracle truth on every example", ok)
    ok &= _check("blind labels: armed ⊆ oracle armed, evidence visible, decidable near-misses only",
                 blind_ok)
    ok &= _check(f"all four near-miss modes represented {n_by_mode}",
                 set(n_by_mode) == {"threshold", "sustain", "wrong_leg", "confounder"})
    armed_counts = [len(r["oracle"]["armed"]) for r in rows]
    ok &= _check("mix of quiet / single / multi-armed scenarios",
                 armed_counts.count(0) > 10 and any(c >= 2 for c in armed_counts))
    blind_armed = sum(len(r["blind_oracle"]["armed"]) for r in rows)
    ok &= _check(f"blind view still has armed plays to find ({blind_armed})", blind_armed >= 50)
    return ok


def _narrative_templates(fams, key):
    return {n for f in fams for n in f.get(key, [])}


def test_heldout_content():
    """The eval must not be a copy of training: no eval narrative can come out of the train
    split, held-out families never appear in training, every eval library has one."""
    print("\n== held-out content (eval vs train) ==")
    train_txt, train_fams = set(), set()
    for seed in range(40):
        rng = random.Random(1000 + seed)
        for i in range(10):
            lib = libgen.make_library(rng, n_plays=rng.choice([5, 6, 7]), race_idx=i, split="train")
            train_txt |= {p["conditions"]["narrative"] for p in lib["plays"]}
            train_fams |= {p["family"] for p in lib["plays"]}
    eval_txt, eval_fams, each_has_heldout = set(), set(), True
    held = libgen.heldout_keys()
    rng = random.Random(7)
    for i in range(60):
        lib = libgen.make_library(rng, n_plays=rng.choice([5, 6, 7]), race_idx=i, split="eval")
        eval_txt |= {p["conditions"]["narrative"] for p in lib["plays"]}
        fams = {p["family"] for p in lib["plays"]}
        eval_fams |= fams
        each_has_heldout &= bool(fams & held)
    ok = _check("no eval narrative appears in the train split", not (eval_txt & train_txt))
    ok &= _check("held-out families never generated for training", not (train_fams & held))
    ok &= _check("every eval library carries a held-out family", each_has_heldout)
    ok &= _check("eval covers the seen families too (new wording)", len(eval_fams - held) >= 8)
    # wording, not just numbers: the train and eval phrasing templates share no sentence
    tr = _narrative_templates(libgen._FAMILIES, "narr")
    ev = (_narrative_templates(libgen._FAMILIES, "narr_eval")
          | _narrative_templates(libgen._HELDOUT_FAMILIES, "narr_eval"))
    ok &= _check("train and eval phrasing templates are disjoint", not (tr & ev))
    ok &= _check("every family has held-out phrasings",
                 all(f.get("narr_eval") for f in libgen._FAMILIES + libgen._HELDOUT_FAMILIES))
    return ok


def test_train_serve_parity():
    """The chat the copilot sends in a race (strategy_brief, stubbed engine + LLM) is byte-equal to
    what the eval sends for the same digest, and the SFT row is built the same way. The seed has
    the production shape: picture rows {signal, read, grounded_in[list], confidence}, schema-valid
    vs_playbook, urgency + confidence present, no sail-state row."""
    print("\n== train == serve parity ==")
    eng = _engine_ok()
    if eng is not True:
        return eng is None
    rng = random.Random(5)
    lib = libgen.make_library(rng, n_plays=6, split="eval")
    ex = None
    for _ in range(40):
        sc = scengen.make_scenario(rng, lib, n_armed=1, near_modes=("threshold",))
        if sc["oracle"]["armed"]:
            ex = {"id": "parity", "bundle": lib, **sc}
            break
    ok = _check("built an armed example", ex is not None)
    if not ex:
        return ok

    # production: strategy_brief with the engine returning the digest the seed is rendered from
    reads = scengen.engine_reads(ex["signals"], random.Random(1))
    digest = scengen.render_digest(lib, reads, ex["status_map"])
    sent = {}

    class StubEngine:
        base_url = "http://stub"

        def __init__(self, *a, **k):
            pass

        def strategy(self, route=None):
            return json.loads(json.dumps(digest))

    class StubLLM:
        def __init__(self, *a, **k):
            pass

        def chat(self, messages, **k):
            sent["messages"] = messages
            return {"content": "{}"}

    saved = (copilot.EngineClient, copilot.LLMClient, playbook_mod.load)
    copilot.EngineClient, copilot.LLMClient = StubEngine, StubLLM
    playbook_mod.load = lambda *a, **k: playbook_mod.Playbook(lib)
    try:
        copilot.strategy_brief(use_llm=True)
    finally:
        copilot.EngineClient, copilot.LLMClient, playbook_mod.load = saved

    ex2 = dict(ex)
    ex2["views"] = dict(ex["views"])
    ex2["views"]["hinted"] = {"seed": copilot.strategy_seed(digest),
                              "status_map": copilot.strategy_status_map(digest)}
    _, _, eval_msgs = run_eval.messages_for(ex2, "hinted")
    ok &= _check("eval sends exactly what strategy_brief sends",
                 sent.get("messages") == eval_msgs)
    ok &= _check("strategy_messages == strategy_brief's chat",
                 copilot.strategy_messages(playbook_mod.Playbook(lib), digest)
                 == sent.get("messages"))

    payload = json.loads(ex["views"]["hinted"]["seed"].split("\n", 1)[1])
    rows = payload["picture"]
    ok &= _check("picture rows carry the production keys only",
                 all(set(r) == {"signal", "read", "grounded_in", "confidence"} for r in rows))
    ok &= _check("every grounded_in is a list",
                 all(isinstance(r["grounded_in"], list) for r in rows))
    ok &= _check("no sail-state row (production never sends one)",
                 not any("Sail state" in r["read"] for r in rows))
    rec = payload["recommendation"]
    enum = (copilot._STRATEGY_SCHEMA["properties"]["recommendation"]["properties"]
            ["vs_playbook"]["enum"])
    ok &= _check(f"vs_playbook '{rec.get('vs_playbook')}' is a schema value",
                 rec.get("vs_playbook") in enum)
    ok &= _check("recommendation carries urgency + confidence",
                 rec.get("urgency") and rec.get("confidence"))
    ok &= _check("hinted view: armed play rides as a tag + picture row",
                 any(r["signal"] == "plays" for r in rows)
                 and set(ex["views"]["hinted"]["status_map"]) >= set(ex["oracle"]["armed"]))
    blind = json.loads(ex["views"]["blind"]["seed"].split("\n", 1)[1])
    ok &= _check("blind view: no tags, no plays row",
                 not ex["views"]["blind"]["status_map"]
                 and not any(r["signal"] == "plays" for r in blind["picture"]))

    rows_t, _ = gen_train.generate(libraries=2, per_lib=4, seed=1001, blind_frac=0.5)
    r0 = rows_t[0]
    ok &= _check("SFT rows: system/user/assistant",
                 [m["role"] for m in r0["messages"]] == ["system", "user", "assistant"])
    ok &= _check("every SFT target passes the eval's schema check",
                 all(run_eval._schema_ok(json.loads(r["messages"][2]["content"])) for r in rows_t))
    return ok


def test_prompt_path():
    """The production prompt must render the synthetic bundle: play ids + narratives present,
    digest non-empty, and the validator behaves on known/unknown ids."""
    print("\n== production prompt path on a synthetic bundle ==")
    rng = random.Random(3)
    lib = libgen.make_library(rng, n_plays=6)
    smap = {lib["plays"][0]["id"]: "armed"}
    pb = playbook_mod.Playbook(lib)
    prompt = copilot._strategy_prompt(pb, smap)
    ok = _check("every play id + narrative in the prompt",
                all(p["id"] in prompt and p["conditions"]["narrative"][:40] in prompt
                    for p in lib["plays"]))
    ok &= _check("digest renders the variants", "variant left" in prompt)
    parsed = {"play_matches": [
        {"play_id": lib["plays"][0]["id"], "match": "strong", "why": "w"},
        {"play_id": "invented-play", "match": "strong", "why": "w"}]}
    filt = copilot._filter_play_matches(parsed, pb, smap)
    ok &= _check("validator keeps known id, drops invented id",
                 len(filt) == 1 and filt[0]["play_id"] == lib["plays"][0]["id"])
    return ok


def test_schema_ok():
    print("\n== eval schema check ==")
    good = {"assessment": "a", "play_matches": [{"play_id": "p", "match": "strong", "why": "w"}],
            "recommendation": {"action": "Hold", "rationale": "r", "grounded_in": ["get_tactics"],
                               "urgency": "monitor", "confidence": "med", "vs_playbook": "on-plan"}}
    ok = _check("a full reply passes", run_eval._schema_ok(good))
    ok &= _check("{} fails (it used to pass)", not run_eval._schema_ok({}))
    bad = json.loads(json.dumps(good))
    del bad["recommendation"]["urgency"]
    ok &= _check("missing required urgency fails", not run_eval._schema_ok(bad))
    bad = json.loads(json.dumps(good))
    bad["recommendation"]["vs_playbook"] = "on_plan"
    ok &= _check("'on_plan' (not a schema value) fails", not run_eval._schema_ok(bad))
    for path, val in ((("recommendation", "action"), 123), (("recommendation", "rationale"), []),
                      (("recommendation", "grounded_in"), [17]), (("assessment",), None)):
        bad = json.loads(json.dumps(good))
        d = bad
        for k in path[:-1]:
            d = d[k]
        d[path[-1]] = val
        ok &= _check(f"{'.'.join(path)}={val!r} (wrong type) fails", not run_eval._schema_ok(bad))
    for k, val in (("play_id", 5), ("why", 7), ("why", " ")):
        bad = json.loads(json.dumps(good))
        bad["play_matches"][0][k] = val
        ok &= _check(f"play match {k}={val!r} fails", not run_eval._schema_ok(bad))
    return ok


def test_resume_refuses_other_run():
    print("\n== --resume only reuses the same run ==")
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        corpus = os.path.join(d, "c.jsonl")
        with open(corpus, "w") as f:
            f.write("{}\n")
        out = os.path.join(d, "r.json")
        with open(out, "w") as f:
            json.dump({"run": run_eval.run_fingerprint(corpus, "hinted", True, None),
                       "predictions": [], "example_ids": []}, f)
        r = subprocess.run([sys.executable, "-m", "copilot.eval.run_eval", "--corpus", corpus,
                            "--out", out, "--dry", "--resume"], capture_output=True, text=True,
                           cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        ok = _check("a hinted/dry result can't be resumed into a blind run",
                    r.returncode != 0 and "--resume refused" in (r.stdout + r.stderr))
    same_a = run_eval.run_fingerprint(corpus_path=__file__, mode="blind", dry=False, model="m")
    same_b = run_eval.run_fingerprint(corpus_path=__file__, mode="blind", dry=False, model="m")
    ok &= _check("the same run matches", same_a == same_b)
    ok &= _check("another model doesn't",
                 same_a != run_eval.run_fingerprint(__file__, "blind", False, "other"))
    return ok


def test_metrics_math():
    print("\n== metrics math ==")
    plays = {"plays": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    exs = [{"oracle": {"armed": ["a"], "near": {"b": "threshold"}, "quiet": ["c"]},
            "blind_oracle": {"armed": ["a"], "near": {"b": "threshold"}, "excluded": []},
            "bundle": plays},
           {"oracle": {"armed": [], "near": {"c": "sustain"}, "quiet": ["a", "b"]},
            "blind_oracle": {"armed": [], "near": {}, "excluded": ["c"]},
            "bundle": plays}]
    perfect = [{"parse_ok": True, "schema_ok": True,
                "raw": [{"play_id": "a", "match": "strong", "why": "w"}],
                "filtered": [{"play_id": "a", "match": "strong", "why": "w"}]},
               {"parse_ok": True, "schema_ok": True, "raw": [], "filtered": []}]
    m = metrics.score(exs, perfect)
    ok = _check("perfect predictions pass every blind gate", m["pass"]
                and m["armed_set"]["f1"] == 1.0 and m["near_miss_fp_rate"] == 0.0)
    fooled = [{"parse_ok": True, "schema_ok": True,
               "raw": [{"play_id": "b", "match": "strong", "why": "w"},
                       {"play_id": "ghost", "match": "strong", "why": "w"}],
               "filtered": [{"play_id": "b", "match": "strong", "why": "w"}]},
              {"parse_ok": False, "schema_ok": False, "raw": [], "filtered": []}]
    m2 = metrics.score(exs, fooled)
    ok &= _check("near-miss FP + grounding violation + schema failure all counted",
                 not m2["pass"] and m2["near_miss_fp_rate"] == 1.0
                 and m2["grounding_violation_rate"] == 0.5
                 and m2["reliability"]["schema_rate"] == 0.5)
    ok &= _check("grounding violations are a gate", m2["gates"]["grounding<=2%"] is False)
    # an excluded play (undecidable blind) is neither credited nor penalized
    guess = [perfect[0], {"parse_ok": True, "schema_ok": True,
                          "raw": [{"play_id": "c", "match": "strong", "why": "w"}],
                          "filtered": [{"play_id": "c", "match": "strong", "why": "w"}]}]
    m3 = metrics.score(exs, guess)
    ok &= _check("a claim on an excluded play doesn't count",
                 m3["armed_set"]["precision"] == 1.0 and m3["excluded_n"] == 1)
    m4 = metrics.score(exs, guess, mode="hinted")
    ok &= _check("hinted view: same claim is a false positive, and hinted never gates",
                 m4["armed_set"]["precision"] == 0.5 and m4["pass"] is None)
    return ok


def test_blind_gate_needs_reading():
    """The tag-parrot (no reading) scores F1 1.0 hinted — which is why hinted can't gate — and
    must FAIL the blind gate."""
    print("\n== the blind gate can't be passed by copying tags ==")
    eng = _engine_ok()
    if eng is not True:
        return eng is None
    ev = gen_corpus.generate(libraries=10, per_lib=8, seed=7)
    hinted = metrics.score(ev, [run_eval._predict(e, None, mode="hinted", dry=True) for e in ev],
                           mode="hinted")
    blind = metrics.score(ev, [run_eval._predict(e, None, mode="blind", dry=True) for e in ev])
    ok = _check(f"parrot hinted F1 {hinted['armed_set']['f1']} (tag-copying looks perfect)",
                hinted["armed_set"]["f1"] == 1.0 and hinted["pass"] is None)
    ok &= _check(f"parrot blind F1 {blind['armed_set']['f1']} fails the gate",
                 blind["pass"] is False and blind["gates"]["f1>=0.9"] is False)
    return ok


def test_context_budget():
    """One context number everywhere: eval + SFT prompts fit it, an oversize prompt is refused
    before it reaches the server (Ollama would truncate silently), the eval doesn't retry it."""
    print(f"\n== context budget ({config.LLM_NUM_CTX} tokens) ==")
    ok = True
    big = [{"role": "user",
            "content": "x" * int(config.LLM_NUM_CTX * config.LLM_CHARS_PER_TOKEN)}]
    try:
        llm_mod.check_budget(big)
        ok &= _check("oversize prompt refused", False)
    except llm_mod.PromptTooLong:
        ok &= _check("oversize prompt refused (PromptTooLong)", True)
    client = llm_mod.LLMClient(base_url="http://127.0.0.1:9")   # never reached
    try:
        client.chat(big)
        ok &= _check("LLMClient.chat refuses before any network call", False)
    except llm_mod.PromptTooLong:
        ok &= _check("LLMClient.chat refuses before any network call", True)
    except llm_mod.LLMUnavailable as e:
        ok &= _check(f"LLMClient.chat refuses before any network call (got {e})", False)
    calls = []

    class Counting:
        def chat(self, *a, **k):
            calls.append(1)
            raise llm_mod.PromptTooLong("too long")

        def reachable(self):
            return True
    try:
        run_eval._chat_with_recovery(Counting(), big, attempts=3, recovery_s=0)
    except llm_mod.PromptTooLong:
        pass
    ok &= _check("eval does not retry a too-long prompt", len(calls) == 1)
    ok &= _check("a normal-size prompt passes",
                 llm_mod.check_budget([{"role": "user", "content": "y" * 8000}]) > 0)

    # the training side (runs on a GPU box; its pure parts import without torch)
    import importlib.util
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    spec = importlib.util.spec_from_file_location(
        "train_qlora", os.path.join(root, "training", "matcher_lora", "train_qlora.py"))
    tq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tq)
    if "LLM_NUM_CTX" not in os.environ:
        ok &= _check(f"training max length == serving window ({tq.SERVE_NUM_CTX})",
                     tq.SERVE_NUM_CTX == config.LLM_NUM_CTX)
    ok &= _check("training masks the prompt, keeps the reply",
                 tq.mask_row([1, 2, 9, 8, 5, 6], [9, 8], 10)["labels"] == [-100] * 4 + [5, 6])
    ok &= _check("training skips (never truncates) an overlong row",
                 tq.mask_row([1, 2, 9, 8, 5, 6], [9, 8], 5) is None)
    ok &= _check("training skips a row with no assistant turn (never trains unmasked)",
                 tq.mask_row([1, 2, 3], [9, 8], 10) is None)

    eng = _engine_ok()
    if eng is not True:
        return ok and eng is None
    ev = gen_corpus.generate(libraries=10, per_lib=6, seed=7)
    worst = max(llm_mod.estimate_tokens(run_eval.messages_for(e, "hinted")[2]) for e in ev)
    ok &= _check(f"largest eval prompt ~{worst} tokens + reply reserve fits",
                 worst + config.LLM_REPLY_RESERVE <= config.LLM_NUM_CTX)
    ok &= _check("...and would NOT have fit the old 2048 window with a reply",
                 worst + config.LLM_REPLY_RESERVE > 2048)
    rows, dropped = gen_train.generate(libraries=4, per_lib=4, seed=1001, num_ctx=1000)
    ok &= _check(f"SFT rows over the window are dropped, not truncated ({dropped} dropped)",
                 dropped > 0 and all(llm_mod.estimate_tokens(r["messages"]) <= 1000 for r in rows))
    return ok


def main():
    ok = test_lockstep()
    ok &= test_generator_labels()
    ok &= test_heldout_content()
    ok &= test_train_serve_parity()
    ok &= test_prompt_path()
    ok &= test_schema_ok()
    ok &= test_resume_refuses_other_run()
    ok &= test_metrics_math()
    ok &= test_blind_gate_needs_reading()
    ok &= test_context_budget()
    print("\nALL PASS" if ok else "\nFAIL: see above")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
