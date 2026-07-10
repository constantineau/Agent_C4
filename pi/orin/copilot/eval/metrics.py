"""§4 metrics + gates. Predictions are scored twice: RAW (straight from the model, before the
production validator) for the grounding/reliability axes — the validator would mask exactly what
we're measuring — and FILTERED (post `_filter_play_matches`) for the accuracy axes, because that
is the surface the crew actually sees."""

GATES = {"f1": 0.90, "top1": 0.90, "near_fp": 0.10, "schema": 0.98}


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if (tp + fp) else 1.0
    r = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return round(p, 3), round(r, 3), round(f1, 3)


def score(examples, predictions):
    """examples[i]['oracle'] vs predictions[i] = {parse_ok, schema_ok, raw: [...], filtered: [...]}
    (raw/filtered entries: {play_id, match, why}). Returns the aggregate metrics + gate verdicts."""
    tp = fp = fn = ltp = lfp = lfn = 0
    top1_num = top1_den = 0
    near_fp = near_n = 0
    ground_bad = ground_total = 0
    parse_ok = schema_ok = 0
    cal = {"strong": [0, 0], "partial": [0, 0]}         # [correct, total]

    for ex, pr in zip(examples, predictions):
        armed = set(ex["oracle"]["armed"])
        near = ex["oracle"]["near"]
        known = {str(p["id"]) for p in ex["bundle"]["plays"]}
        parse_ok += bool(pr.get("parse_ok"))
        schema_ok += bool(pr.get("schema_ok"))

        raw = pr.get("raw") or []
        ground_total += len(raw)
        ground_bad += sum(1 for m in raw if str(m.get("play_id")) not in known)

        filt = pr.get("filtered") or []
        strong = {m["play_id"] for m in filt if m.get("match") == "strong"}
        listed = {m["play_id"] for m in filt}
        tp += len(strong & armed); fp += len(strong - armed); fn += len(armed - strong)
        ltp += len(listed & armed); lfp += len(listed - armed); lfn += len(armed - listed)

        if armed:
            top1_den += 1
            top1_num += bool(filt) and filt[0]["play_id"] in armed

        for pid in near:
            near_n += 1
            near_fp += pid in strong

        for m in filt:
            b = cal.get(m.get("match"))
            if b is not None:
                b[1] += 1
                b[0] += m["play_id"] in armed

    n = max(len(examples), 1)
    p, r, f1 = _prf(tp, fp, fn)
    lp, lr, lf1 = _prf(ltp, lfp, lfn)
    strong_acc = cal["strong"][0] / cal["strong"][1] if cal["strong"][1] else None
    partial_acc = cal["partial"][0] / cal["partial"][1] if cal["partial"][1] else None
    out = {
        "n": len(examples),
        "armed_set": {"precision": p, "recall": r, "f1": f1},
        "armed_set_lenient": {"precision": lp, "recall": lr, "f1": lf1},
        "top1": round(top1_num / top1_den, 3) if top1_den else None,
        "near_miss_fp_rate": round(near_fp / near_n, 3) if near_n else None,
        "near_miss_n": near_n,
        "calibration": {"strong_acc": None if strong_acc is None else round(strong_acc, 3),
                        "partial_acc": None if partial_acc is None else round(partial_acc, 3),
                        "monotone": (None if None in (strong_acc, partial_acc)
                                     else strong_acc >= partial_acc)},
        "grounding_violation_rate": (round(ground_bad / ground_total, 3) if ground_total else 0.0),
        "reliability": {"parse_rate": round(parse_ok / n, 3), "schema_rate": round(schema_ok / n, 3)},
    }
    out["gates"] = {
        "f1>=0.9": f1 >= GATES["f1"],
        "top1>=0.9": (out["top1"] or 0) >= GATES["top1"] if top1_den else None,
        "near_fp<=10%": (out["near_miss_fp_rate"] or 0) <= GATES["near_fp"] if near_n else None,
        "calibration_monotone": out["calibration"]["monotone"],
        "schema>=98%": out["reliability"]["schema_rate"] >= GATES["schema"],
    }
    out["pass"] = all(v for v in out["gates"].values() if v is not None)
    return out
