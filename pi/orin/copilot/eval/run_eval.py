"""Baseline/eval runner — drives the REAL production prompt path (copilot._strategy_prompt →
LLMClient → copilot._filter_play_matches) over a generated corpus and scores it against the
oracle labels (§4). Run on the Orin so the number measured is q4_K_M on the real hardware.

    python3 -m copilot.eval.run_eval --corpus /tmp/matcher_corpus.jsonl --out /tmp/baseline.json
    ... --dry     # no LLM: a hint-parrot floor (predict strong for every [armed] hint) + pipe sanity
    ... --blind   # status hints withheld (all [quiet]) — pure narrative matching, the §3.4 axis

Two hint modes, because production has both regimes: normally the deterministic matcher's
[armed]/[arming] tags ride in the prompt (agreeing with them is most of the job); but the
compound/fuzzy cases the predicates can't see arrive tagged [quiet] — --blind measures whether
the model can out-read a hint that is present and unhelpful, exactly that regime."""

import argparse
import json
import os
import time

from .. import copilot, playbook as playbook_mod
from ..llm import LLMClient, LLMUnavailable
from . import metrics


def _schema_ok(parsed):
    if not isinstance(parsed, dict) or not isinstance(parsed.get("play_matches", []), list):
        return False
    return all(isinstance(m, dict) and m.get("play_id") and m.get("match") in ("strong", "partial")
               and m.get("why") for m in parsed.get("play_matches", []))


def _chat_with_recovery(llm, messages, attempts=3, recovery_s=180):
    """A multi-hour eval must survive an Ollama restart (the 2026-07-10 baseline lost 337 of 480
    examples to one OOM-kill): on LLM trouble, wait for the server to answer /models again (systemd
    restarts it) and retry, up to `attempts`. An example fails only when the LLM is truly gone."""
    last = None
    for i in range(attempts):
        try:
            return llm.chat(messages, schema=copilot._STRATEGY_SCHEMA)
        except LLMUnavailable as e:
            last = e
            deadline = time.time() + recovery_s
            while time.time() < deadline:
                if llm.reachable():
                    break
                time.sleep(10)
            print(f"  ! llm trouble ({e}) — retry {i + 1}/{attempts}", flush=True)
    raise last


def _predict(ex, llm, blind=False, dry=False):
    pb = playbook_mod.Playbook(ex["bundle"])
    smap = ({pid: "quiet" for pid in pb.play_ids()} if blind else ex["status_map"])
    if dry:
        raw = [{"play_id": pid, "match": "strong", "why": "hint"}
               for pid, st in smap.items() if st == "armed"]
        parsed = {"assessment": "dry", "recommendation": {"rationale": "dry", "grounded_in": []},
                  "play_matches": raw}
        return {"parse_ok": True, "schema_ok": True, "raw": raw,
                "filtered": copilot._filter_play_matches(parsed, pb, smap), "latency_s": 0.0}
    t0 = time.time()
    try:
        msg = _chat_with_recovery(
            llm, [{"role": "system", "content": copilot._strategy_prompt(pb, smap)},
                  {"role": "user", "content": ex["seed"]}])
        parsed = copilot._extract_json(msg.get("content") or "")
    except LLMUnavailable as e:
        return {"parse_ok": False, "schema_ok": False, "raw": [], "filtered": [],
                "latency_s": round(time.time() - t0, 1), "error": str(e)}
    raw = list((parsed or {}).get("play_matches") or []) if isinstance(parsed, dict) else []
    return {"parse_ok": parsed is not None, "schema_ok": _schema_ok(parsed),
            "raw": raw,
            "filtered": copilot._filter_play_matches(parsed or {}, pb, smap),
            "latency_s": round(time.time() - t0, 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out")
    ap.add_argument("--n", type=int, help="cap examples (quick pass)")
    ap.add_argument("--blind", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="reuse non-error predictions already in --out; re-run the rest")
    args = ap.parse_args()

    with open(args.corpus) as f:
        examples = [json.loads(l) for l in f if l.strip()]
    if args.n:
        examples = examples[:args.n]

    done = {}
    if args.resume and args.out and os.path.exists(args.out):
        with open(args.out) as f:
            prev = json.load(f)
        done = {eid: p for eid, p in zip(prev.get("example_ids", []), prev.get("predictions", []))
                if not p.get("error")}
        print(f"resume: {len(done)} prior predictions reused")

    llm = None if args.dry else LLMClient()
    preds = []
    for i, ex in enumerate(examples):
        if ex["id"] in done:
            preds.append(done[ex["id"]])
            continue
        pr = _predict(ex, llm, blind=args.blind, dry=args.dry)
        preds.append(pr)
        if not args.dry:
            print(f"[{i + 1}/{len(examples)}] {ex['id']} {pr['latency_s']}s "
                  f"raw={len(pr['raw'])} armed={ex['oracle']['armed']}", flush=True)

    m = metrics.score(examples, preds)
    m["mode"] = "dry" if args.dry else ("blind" if args.blind else "hinted")
    if not args.dry:
        lats = sorted(p["latency_s"] for p in preds)
        m["latency_s"] = {"p50": lats[len(lats) // 2], "max": lats[-1]}
    print(json.dumps(m, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"metrics": m, "predictions": preds,
                       "example_ids": [e["id"] for e in examples]}, f, indent=1)
        print(f"-> {args.out}")


if __name__ == "__main__":
    main()
