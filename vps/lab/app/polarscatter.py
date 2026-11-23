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

FIELDS = ("t", "tws", "twa", "stw", "config", "recording")
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


def decimate(points, tws_step, per_group=PER_GROUP, total=TOTAL):
    """Thin `points` (dicts with at least t, tws, recording) to at most `per_group` per
    (TWS bucket, recording) and at most `total` overall. Returns `(kept, n_in)`.

    When the per-group caps still add up to more than `total`, every group is scaled by the same
    factor (each keeps at least one point), so a short race isn't squeezed out by a long one more
    than its share."""
    groups = {}
    for p in points:
        k = (round(p["tws"] / tws_step) * tws_step, p.get("recording"))
        groups.setdefault(k, []).append(p)
    sizes = {k: min(len(v), per_group) for k, v in groups.items()}
    have = sum(sizes.values())
    if have > total:
        f = total / float(have)
        sizes = {k: max(1, int(math.floor(n * f))) for k, n in sizes.items()}
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
             p.get("config"), p.get("recording")] for p in kept]
    return {"fields": list(FIELDS), "rows": rows, "kept": len(rows), "total": n_in,
            "per_group": per_group, "cap": total}
