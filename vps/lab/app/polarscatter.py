"""Log points behind the observed polar (#376) — the raw evidence the p80 cells are built from.

The Polar tab draws one point per (TWS, TWA, config) cell: the p80 of every sample in it. A cell
says nothing about how the samples spread, so a navigator can't tell a tight cluster from a
smear. This module keeps a THINNED copy of the same samples the cells were built from — after
the trust gate, the TWA floor and the per-recording second de-duplication — so the scatter can
never show a point the polar refused.

Thinning is even in time within each (TWS bucket, race) group, so a group keeps its shape (the
share of time on each sail, each angle), and capped in total so the page stays light: a 7-hour
race at 1 Hz is 25 000 fixes, and the tab loads the whole artifact.

Packed as rows of a fixed field list, not dicts: a few thousand points as dicts is most of the
payload in key names.
"""
import math

# `band` / `twa_bin` are the polar cell's own TWS and TWA bins (obspolar's rounding), so the page
# never re-derives them: Python rounds 9 kn to the 8 kn cell, JS Math.round would say 10.
# `pooled` is 1 when the sample also counted in the all-races polar (overlapping windows give a
# second to each race's own view but to the pooled view once).
FIELDS = ("t", "tws", "twa", "stw", "config", "recording", "band", "twa_bin", "pooled")
PER_GROUP = 250      # points kept per (TWS bucket, race); a bucket the boat sat in all day
TOTAL = 6000         # ...and in all, so many races can't grow the page without bound


def _thin(rows, n):
    """`n` rows of `rows`, spread evenly through it (first and last kept). Order is kept."""
    if n >= len(rows):
        return list(rows)
    if n <= 0:
        return []
    if n == 1:
        return [rows[0]]
    step = (len(rows) - 1) / (n - 1)
    return [rows[int(round(i * step))] for i in range(n)]


def _band(p, tws_step):
    return p["band"] if p.get("band") is not None else round(p["tws"] / tws_step) * tws_step


def _share(sizes, total):
    """Scale group sizes to sum to at most `total` — floors first, then the leftover to the
    largest remainders. A group may get zero when there are more groups than points to give."""
    have = sum(sizes.values())
    if have <= total:
        return sizes
    f = total / float(have)
    out = {k: int(math.floor(n * f)) for k, n in sizes.items()}
    left = total - sum(out.values())
    for k in sorted(sizes, key=lambda k: (-(sizes[k] * f - out[k]), k[0], k[1] or 0))[:left]:
        out[k] += 1
    return out


def decimate(points, tws_step, per_group=PER_GROUP, total=TOTAL):
    """Thin `points` (dicts with at least t, tws, recording) to at most `per_group` per
    (TWS band, recording) and at most `total` overall. Returns `(kept, n_in)`.

    When the per-group caps still add up to more than `total`, every group is scaled by the same
    factor, so a short race isn't squeezed out by a long one more than its share."""
    groups = {}
    for p in points:
        k = (_band(p, tws_step), p.get("recording"))
        groups.setdefault(k, []).append(p)
    sizes = _share({k: min(len(v), per_group) for k, v in groups.items()}, total)
    kept = []
    for k in sorted(groups, key=lambda k: (k[0], k[1] or 0)):
        rows = sorted(groups[k], key=lambda p: p["t"])
        kept.extend(_thin(rows, sizes[k]))
    return kept, len(points)


def pack(points, tws_step, per_group=PER_GROUP, total=TOTAL):
    """The artifact block: thinned rows under `FIELDS`, plus what was left out (counted, like
    every other gate on this tab)."""
    kept, n_in = decimate(points, tws_step, per_group, total)
    rows = [[round(p["t"], 1), round(p["tws"], 1), round(p["twa"], 1), round(p["stw"], 2),
             p.get("config"), p.get("recording"), _band(p, tws_step), p.get("twa_bin"),
             p.get("pooled", 1)]
            for p in kept]
    return {"fields": list(FIELDS), "rows": rows, "kept": len(rows), "total": n_in,
            "per_group": per_group, "cap": total}
