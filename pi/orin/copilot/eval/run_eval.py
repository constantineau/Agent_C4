"""Baseline/eval runner — drives the REAL production prompt path (copilot._strategy_prompt +
copilot.strategy_seed → LLMClient → copilot._filter_play_matches) over a generated corpus and
scores it against the oracle labels (§4). Run on the Orin so the number measured is q4_K_M on the
real hardware.

    python3 -m copilot.eval.run_eval --corpus /tmp/matcher_corpus.jsonl --out /tmp/eval.json
    ... --dry      # no LLM: a hint-parrot (strong for every [armed] tag) — pipe sanity; blind it
                   #   scores ~0, which is the point: the blind gate can't be passed without reading
    ... --hinted   # the matcher's tags ride along — DIAGNOSTIC only (a tag-copier scores 1.0)

The default and the only GATED view is blind (#54): no matcher tags, no "Armed play(s)" row; the
model must match each play's narrative against the engine picture. The corpus must come from
gen_corpus of this version (it carries both rendered views and the blind labels)."""

import argparse
import hashlib
import json
import os
import time

from .. import copilot, playbook as playbook_mod
from ..llm import LLMClient, LLMUnavailable, PromptTooLong
from . import metrics


_TYPES = {"object": dict, "array": list, "string": str}


def _fits(v, spec):
    """`v` matches a JSON-schema node of the kinds _STRATEGY_SCHEMA uses: type (object, array,
    string), enum, required, properties, items."""
    want = _TYPES.get(spec.get("type"))
    if want is not None and not isinstance(v, want):
        return False
    if "enum" in spec and v not in spec["enum"]:
        return False
    if isinstance(v, dict):
        if any(k not in v for k in spec.get("required", [])):
            return False
        props = spec.get("properties", {})
        if not all(_fits(v[k], props[k]) for k in props if k in v):
            return False
    if isinstance(v, list) and "items" in spec:
        return all(_fits(x, spec["items"]) for x in v)
    return True


def _schema_ok(parsed):
    """The reply matches the copilot's _STRATEGY_SCHEMA: required keys, every field's type
    (action=123 or grounded_in=[17] fail), enums; plus a play match needs a non-empty id and why.
    (`{}` used to pass.)"""
    if not _fits(parsed, copilot._STRATEGY_SCHEMA):
        return False
    return all(m["play_id"].strip() and m["why"].strip() for m in parsed.get("play_matches", []))


def _chat_with_recovery(llm, messages, attempts=3, recovery_s=180):
    """A multi-hour eval must survive an Ollama restart (the 2026-07-10 baseline lost 337 of 480
    examples to one OOM-kill): on LLM trouble, wait for the server to answer /models again (systemd
    restarts it) and retry, up to `attempts`. An example fails only when the LLM is truly gone."""
    last = None
    for i in range(attempts):
        try:
            return llm.chat(messages, schema=copilot._STRATEGY_SCHEMA)
        except PromptTooLong:
            raise                                 # same prompt, same size: don't wait and retry
        except LLMUnavailable as e:
            last = e
            deadline = time.time() + recovery_s
            while time.time() < deadline:
                if llm.reachable():
                    break
                time.sleep(10)
            print(f"  ! llm trouble ({e}) — retry {i + 1}/{attempts}", flush=True)
    raise last


def messages_for(ex, mode):
    """The chat the copilot would send for this example's view: system = the production strategy
    prompt over the example's play library + the view's status tags; user = the rendered seed."""
    if "views" not in ex:
        raise ValueError(f"example {ex.get('id')} predates #54 (no rendered views) — regenerate "
                         "the corpus with this version of gen_corpus")
    view = ex["views"][mode]
    pb = playbook_mod.Playbook(ex["bundle"])
    return pb, view["status_map"], [
        {"role": "system", "content": copilot._strategy_prompt(pb, view["status_map"])},
        {"role": "user", "content": view["seed"]}]


def _predict(ex, llm, mode="blind", dry=False):
    pb, smap, messages = messages_for(ex, mode)
    if dry:
        raw = [{"play_id": pid, "match": "strong", "why": "hint"}
               for pid, st in smap.items() if st == "armed"]
        parsed = {"assessment": "dry", "recommendation": {"rationale": "dry", "grounded_in": []},
                  "play_matches": raw}
        return {"parse_ok": True, "schema_ok": True, "raw": raw,
                "filtered": copilot._filter_play_matches(parsed, pb, smap), "latency_s": 0.0}
    t0 = time.time()
    try:
        msg = _chat_with_recovery(llm, messages)
        parsed = copilot._extract_json(msg.get("content") or "")
    except LLMUnavailable as e:
        return {"parse_ok": False, "schema_ok": False, "raw": [], "filtered": [],
                "latency_s": round(time.time() - t0, 1), "error": str(e)}
    raw = list((parsed or {}).get("play_matches") or []) if isinstance(parsed, dict) else []
    return {"parse_ok": parsed is not None, "schema_ok": _schema_ok(parsed),
            "raw": raw,
            "filtered": copilot._filter_play_matches(parsed or {}, pb, smap),
            "latency_s": round(time.time() - t0, 1)}


def run_fingerprint(corpus_path, mode, dry, model):
    """What a saved prediction depends on. --resume only reuses predictions from the same run."""
    with open(corpus_path, "rb") as f:
        corpus = hashlib.sha256(f.read()).hexdigest()
    return {"mode": mode, "dry": bool(dry), "model": model, "corpus_sha256": corpus}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out")
    ap.add_argument("--n", type=int, help="cap examples (quick pass)")
    ap.add_argument("--hinted", action="store_true",
                    help="score the hinted view (diagnostic; never a gate)")
    ap.add_argument("--blind", action="store_true", help="(the default; kept for old commands)")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="reuse non-error predictions already in --out; re-run the rest")
    args = ap.parse_args()

    with open(args.corpus) as f:
        examples = [json.loads(l) for l in f if l.strip()]
    if args.n:
        examples = examples[:args.n]

    mode = "hinted" if args.hinted else "blind"
    llm = None if args.dry else LLMClient()
    run = run_fingerprint(args.corpus, mode, args.dry, llm.model if llm else None)

    done = {}
    if args.resume and args.out and os.path.exists(args.out):
        with open(args.out) as f:
            prev = json.load(f)
        if prev.get("run") != run:
            # a hinted or dry run's predictions would sail through the blind gate
            raise SystemExit(f"--resume refused: {args.out} is from a different run "
                             f"({prev.get('run')} vs {run}); use a new --out")
        done = {eid: p for eid, p in zip(prev.get("example_ids", []), prev.get("predictions", []))
                if not p.get("error")}
        print(f"resume: {len(done)} prior predictions reused")

    preds = []
    for i, ex in enumerate(examples):
        if ex["id"] in done:
            preds.append(done[ex["id"]])
            continue
        pr = _predict(ex, llm, mode=mode, dry=args.dry)
        preds.append(pr)
        if not args.dry:
            print(f"[{i + 1}/{len(examples)}] {ex['id']} {pr['latency_s']}s "
                  f"raw={len(pr['raw'])} armed={ex['oracle']['armed']}", flush=True)

    m = metrics.score(examples, preds, mode=mode)
    m["dry"] = bool(args.dry)
    if not args.dry:
        lats = sorted(p["latency_s"] for p in preds)
        m["latency_s"] = {"p50": lats[len(lats) // 2], "max": lats[-1]}
    print(json.dumps(m, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"run": run, "metrics": m, "predictions": preds,
                       "example_ids": [e["id"] for e in examples]}, f, indent=1)
        print(f"-> {args.out}")


if __name__ == "__main__":
    main()
