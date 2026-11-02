"""shared/gridwalk.py: the exact segment-vs-grid walk every land/obstacle mask uses (#145).

The router used to SAMPLE a step every half cell, so a step that clipped the corner of a blocked
cell could pass. The walk must find every cell the segment touches, and nothing else. Checked
against an independent reference: segment vs closed square (Liang-Barsky clip), on fixed edge
cases and on thousands of random segments.

Run:  python3 shared/test_gridwalk.py
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import gridwalk as G  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def seg_hits_square(x0, y0, x1, y1, c, r, one=1.0, zero=0.0):
    """Reference: does the segment meet the CLOSED square [c, c+1] x [r, r+1]? (Liang-Barsky)"""
    t0, t1 = zero, one
    dx, dy = x1 - x0, y1 - y0
    for p, q in ((-dx, x0 - c), (dx, c + 1 - x0), (-dy, y0 - r), (dy, r + 1 - y0)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
            if t0 > t1:
                return False
    return True


def seg_hits_square_q(x0, y0, x1, y1, c, r):
    """The same test in exact rational arithmetic on the float inputs: no rounding at all."""
    from fractions import Fraction as F
    return seg_hits_square(F(x0), F(y0), F(x1), F(y1), c, r, one=F(1), zero=F(0))


N = 6


def reference(x0, y0, x1, y1):
    return {(c, r) for c in range(-1, N + 1) for r in range(-1, N + 1)
            if seg_hits_square(x0, y0, x1, y1, c, r)}


def walk(x0, y0, x1, y1):
    return {(c, r) for c, r in G.cells_touched(x0, y0, x1, y1) if -1 <= c <= N and -1 <= r <= N}


def mask_of(cells):
    m = bytearray(N * N)
    for c, r in cells:
        m[r * N + c] = 1
    return m


def sampled(mask, x0, y0, x1, y1):
    """The old router check (ObstacleField.crosses before #145), for the control."""
    steps = max(2, int(math.hypot(x1 - x0, y1 - y0) / 0.5) + 1)
    for k in range(steps + 1):
        x, y = x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps
        c, r = int(x), int(y)
        if 0 <= c < N and 0 <= r < N and mask[r * N + c] == 1:
            return True
    return False


print("the clipped corner (#145):")
M = mask_of([(1, 1)])
CORNER = (1.09, 0.1, 3.09, 2.1)              # clips cell (1,1)'s lower-right corner
check("(control) it really meets the cell", seg_hits_square(*CORNER, 1, 1))
check("(control) the old half-cell sampling misses it", not sampled(M, *CORNER))
check("the walk lists the cell", (1, 1) in walk(*CORNER))
check("any_set finds it, both directions",
      G.any_set(M, N, N, *CORNER) and G.any_set(M, N, N, CORNER[2], CORNER[3], CORNER[0], CORNER[1]))

print("edges and corners count on both sides:")
check("along y = 1", G.any_set(M, N, N, 0.5, 1.0, 3.5, 1.0))
check("along x = 2", G.any_set(M, N, N, 2.0, 0.2, 2.0, 3.8))
check("through the corner point", G.any_set(M, N, N, 0.0, 0.0, 4.0, 4.0)
      and G.any_set(M, N, N, 0.5, 2.5, 2.5, 0.5))
check("a point segment inside the cell", G.any_set(M, N, N, 1.5, 1.5, 1.5, 1.5))
check("a near miss is clear", not G.any_set(M, N, N, 0.2, 0.9, 0.9, 0.2)
      and not G.any_set(M, N, N, 2.1, 0.1, 3.9, 1.9))
check("off the grid is never set", not G.any_set(M, N, N, -3, -3, -1, -1)
      and not G.any_set(M, N, N, 7, 0.5, 9, 5.5))
check("a segment that leaves the grid still sees the cell it crosses",
      G.any_set(M, N, N, -2.0, 1.5, 9.0, 1.5))

print("against the reference:")
fixed = [(0.5, 1.0, 3.5, 1.0), (2.0, 0.2, 2.0, 3.8), (0, 0, 4, 4), (0.5, 2.5, 2.5, 0.5),
         (1, 1, 1, 1), (1, 1, 3, 1), (1, 1, 1, 3), (3, 0, 3, 0), (0.25, 0.5, 2.0, 0.5),
         (1.0, 0.5, 1.0, 0.5), (2.5, 2.0, 2.5, 2.0), CORNER, (5.75, 0.25, 0.25, 5.75)]
bad = [s for s in fixed if walk(*s) != reference(*s)]
check(f"{len(fixed)} edge cases: same cells", not bad)
for s in bad:
    print("     ", s, sorted(walk(*s) ^ reference(*s)))
rng = random.Random(145)
bad, misses_old = [], 0
for i in range(6000):
    if i % 3 == 0:     # on the half-grid: endpoints on edges and corners
        s = tuple(rng.randint(-2, 2 * N + 2) / 2 for _ in range(4))
    else:
        s = tuple(rng.uniform(-1, N + 1) for _ in range(4))
    if walk(*s) != reference(*s):
        bad.append(s)
        continue
    cells = {(rng.randrange(N), rng.randrange(N)) for _ in range(3)}
    m = mask_of(cells)
    want = bool(reference(*s) & cells)
    if G.any_set(m, N, N, *s) != want:
        bad.append(("any_set",) + s)
    if want and not sampled(m, *s):
        misses_old += 1
check("6000 random segments: the walk = the reference, any_set agrees", not bad)
for s in bad[:5]:
    print("     ", s)
print(f"      (the old sampling missed {misses_old} real hits in the same set)")
check("(control) the old sampling misses some of them", misses_old > 0)

print("float error never drops a cell (Codex on #200):")
M22 = bytearray(16)
M22[2 * 4 + 2] = 1                           # only cell (2,2): x in [2,3], y in [2,3]
CASE = (0.8111390619505175, 0.5633405356310336, 2, 3)   # ends exactly on (2,2)'s corner (2,3)
check("(control) the exact reference says it touches (2,2)", seg_hits_square_q(*CASE, 2, 2))
check("any_set finds it", G.any_set(M22, 4, 4, *CASE)
      and G.any_set(M22, 4, 4, CASE[2], CASE[3], CASE[0], CASE[1]))
check("cells_touched lists it", (2, 2) in set(G.cells_touched(*CASE)))
# endpoints exactly on grid corners/edges, the other end an arbitrary float: the walk must cover
# every cell the EXACT (rational) geometry touches
rng = random.Random(200)
missed = []
for i in range(4000):
    a = (float(rng.randint(0, N)), float(rng.randint(0, N)))
    if i % 2:
        a = (a[0], rng.uniform(0, N))        # on a vertical grid line only
    b = (rng.uniform(-0.5, N + 0.5), rng.uniform(-0.5, N + 0.5))
    s_ = a + b if i % 3 else b + a
    want = {(c, r) for c in range(-1, N + 1) for r in range(-1, N + 1)
            if seg_hits_square_q(*s_, c, r)}
    got = set(G.cells_touched(*s_))
    if not want <= got:
        missed.append((s_, sorted(want - got)))
check("4000 segments ending on grid lines: no exactly-touched cell is ever dropped", not missed)
for m in missed[:3]:
    print("     ", m)

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
