"""Routing fidelity 2f: island ROUNDING-SIDE enforcement — only for islands that are MARKS OF THE
RACE (rounding port/starboard); plain hazard islands stay avoided either side.

We block the ILLEGAL side of a marked island with a wrong-side barrier (perpendicular to the leg's
transit axis) so the route can only pass on the legal hand. Tests:
  1. scoping — _island_rounding_marks returns ONLY islands with a port/starboard rounding (a 'none'
     hazard island is excluded);
  2. the controlled flip — on open water the natural route passes a marked island on the WRONG side;
     the barrier flips it to the legal side, still reaching the mark (both port and starboard);
  3. the real race — the cove_island Duck(starboard)/BoisBlanc(port) barriers build, leaving the legal
     side open and the illegal side blocked.
"""
import math
from app import store, polars as POL
from app.geo import obstacles as OB
from app import optimizer as OPT

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

def _side(path, clat, clon):
    """which hand the island (clat,clon) is on as the boat passes closest (port = boat's left)."""
    k = min(range(len(path)), key=lambda i: OPT._hav_nm(path[i]["lat"], path[i]["lon"], clat, clon))
    i, j = (k, k + 1) if k + 1 < len(path) else (k - 1, k)
    H = math.radians(OPT._bearing(path[i]["lat"], path[i]["lon"], path[j]["lat"], path[j]["lon"]))
    ce = (clon - path[k]["lon"]) * 60 * math.cos(math.radians(path[k]["lat"]))
    cn = (clat - path[k]["lat"]) * 60
    return ("port" if math.sin(H) * cn - math.cos(H) * ce > 0 else "starboard",
            OPT._hav_nm(path[k]["lat"], path[k]["lon"], clat, clon))

class WF:
    def wind_at(self, lat, lon, t): return (12.0, 180.0)     # FROM south → beam reach along a due-east leg
    def detail_at(self, lat, lon, t): return {"tws": 12.0, "twd": 180.0, "confidence": 1.0}

P = POL.polars_stw()

# 1) SCOPING — only race-mark islands (port/starboard) get a side; a 'none' hazard island does not.
synthetic = {"courses": [{"id": "x", "start": {"lat": 44.0, "lon": -82.0}, "marks": [
    {"name": "HazardIsle", "type": "island", "lat": 44.05, "lon": -81.9, "rounding": "none", "radius_nm": 1.0},
    {"name": "MarkIsle", "type": "island", "lat": 44.05, "lon": -81.7, "rounding": "port", "radius_nm": 1.0}],
    "finish": {"points": [{"lat": 44.0, "lon": -81.5}, {"lat": 44.02, "lon": -81.5}]}}]}
rm = OB._island_rounding_marks(synthetic, "x")
print(f"     scoping: {[(r['side']) for r in rm]} (HazardIsle excluded)")
check("only the port/starboard island is a rounding mark (hazard 'none' excluded)",
      len(rm) == 1 and rm[0]["side"] == "port")

# 2) CONTROLLED FLIP — island just off the rhumb so the natural route takes the WRONG side; the barrier
#    must flip it to the legal side and still reach the finish. Run both port and starboard.
def flip_case(side):
    s, f = (44.0, -82.0), (44.0, -81.6)                       # due-east open-water leg, transit brg ~90
    # port wants island to the boat's port (north of a due-east track) → legal pass is SOUTH of island;
    # so place the island SOUTH of the rhumb (natural shorter pass = north = wrong). Mirror for starboard.
    clat = 43.965 if side == "port" else 44.035
    clon, rad, bbox = -81.8, 1.0, (44.3, 43.6, -82.3, -81.3)
    def route(barrier):
        fld = OB.ObstacleField(bbox); fld._fill_disk(clat, clon, rad, "islands")
        if barrier:
            fld._fill_wrong_side_barrier(clat, clon, rad, 90.0, side, OB.ROUNDSIDE_BAND_NM)
        leg = OPT.route_leg(WF(), P, s[0], s[1], 0.0, f[0], f[1], obstacles=fld)
        got, dist = _side(leg["path"], clat, clon)
        end = OPT._hav_nm(leg["path"][-1]["lat"], leg["path"][-1]["lon"], f[0], f[1])
        return got, dist, end < 0.5
    nat_side, _, nat_reach = route(False)
    enf_side, enf_dist, enf_reach = route(True)
    print(f"     flip[{side}]: natural={nat_side} (wrong)  enforced={enf_side} @ {enf_dist:.1f}nm reach={enf_reach}")
    check(f"[{side}] natural route takes the WRONG side (barrier is needed)", nat_side != side)
    check(f"[{side}] barrier flips the route to the LEGAL side", enf_side == side)
    check(f"[{side}] enforced route still reaches the mark", enf_reach)
    check(f"[{side}] enforced route clears the island (outside the disk)", enf_dist >= rad - 0.2)

flip_case("port")
flip_case("starboard")

# 3) REAL RACE — cove_island Duck(starboard)/BoisBlanc(port): barriers build; legal side open, wrong
#    blocked (isolated from coastline so the side test isn't confounded by surrounding land).
d = store.get_race("bayview-mackinac-2026")
if d:
    cid = "cove_island"; bbox = OPT.course_bbox(d, cid)
    rms = OB._island_rounding_marks(d, cid)
    check("cove_island has the two marked islands (Duck stbd + BoisBlanc port)",
          {r["side"] for r in rms} == {"port", "starboard"} and len(rms) == 2)
    field = OB.build_for_course(d, cid, bbox, coastline_on=False, zones_on=False, islands_on=True,
                                use_cache=False)
    def off(lat, lon, brg, nm):
        b = math.radians(brg)
        return (lat + nm * math.cos(b) / 60.0, lon + nm * math.sin(b) / (60.0 * math.cos(math.radians(lat))))
    geo_ok = field.layers.get("rounding_barrier", 0) > 0
    for r in rms:
        b = r["transit_brg"]; dd = r["radius_nm"] + OB.ROUNDSIDE_BAND_NM + 1.0
        legal = (b + 90) % 360 if r["side"] == "port" else (b - 90) % 360
        la1, lo1 = off(r["lat"], r["lon"], legal, dd)
        la2, lo2 = off(r["lat"], r["lon"], (legal + 180) % 360, dd)
        geo_ok = geo_ok and (not field.blocked(la1, lo1)) and field.blocked(la2, lo2)
    check("real-race barriers: legal side open, illegal side blocked", geo_ok)

print("RESULT:", "PASS" if ok else "FAIL")
