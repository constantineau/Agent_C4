"""Read-only client for the Tier-1 deterministic engine (`pi/engine`, :8200).

This is the copilot's ONLY window onto reality. Every number the copilot reports comes from
here — the engine does the math (routing/tactics/sails/nav/fatigue, plain physics on the
boat's own sensors), the copilot only interprets. There are no write methods on purpose: the
copilot can read facts, it cannot take actions or mutate state.

Pure stdlib (urllib) so it runs on a bare Orin with no pip install. Every call degrades
gracefully — a request that fails returns ``{"available": False, "error": ...}`` rather than
raising, so a flaky link or a sleeping endpoint never crashes a brief.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config


class EngineClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or config.ENGINE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else config.ENGINE_TIMEOUT

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                data = json.loads(r.read())
            if isinstance(data, dict):
                return data
            return {"available": True, "value": data}
        except urllib.error.HTTPError as e:
            return {"available": False, "error": f"HTTP {e.code}", "path": path}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {"available": False, "error": str(e), "path": path}
        except json.JSONDecodeError as e:
            return {"available": False, "error": f"bad JSON: {e}", "path": path}

    # --- fact endpoints (all read-only) ----------------------------------------------------
    def health(self) -> dict:
        return self._get("/health")

    def conditions(self) -> dict:
        """Best-value-per-channel instrument strip (own data, always legal in-race)."""
        return self._get("/conditions")

    def conditions_full(self) -> dict:
        """Every source per channel — for cross-checking a suspect reading."""
        return self._get("/conditions/full")

    def sources(self) -> dict:
        return self._get("/sources")

    def fatigue(self) -> dict:
        return self._get("/fatigue")

    def ais(self, max_range_nm=None) -> dict:
        """AIS traffic + live CPA/TCPA vs own ship — collision awareness. Always legal in-race (the
        boat's OWN receiver + OWN computer); threat-sorted (closing, smallest CPA first)."""
        return self._get("/ais", {"max_range_nm": max_range_nm})

    def fleet(self, max_range_nm=None) -> dict:
        """Roster-matched competitors with ORC corrected-time deltas (who beats us on handicap) +
        over-the-horizon tracker rows. Onboard tactical layer — legal in-race (own receiver + own
        computer + frozen roster homework + permitted public tracker). Rivals-first sorted."""
        return self._get("/fleet", {"max_range_nm": max_range_nm})

    def sail(self, tws=None, twa=None, hoisted=None) -> dict:
        return self._get("/sail", {"tws": tws, "twa": twa, "hoisted": hoisted})

    def course(self, route=None) -> dict:
        return self._get("/course", {"route": route})

    def navigator(self, route=None) -> dict:
        return self._get("/navigator", {"route": route})

    def tactics(self, route=None) -> dict:
        return self._get("/tactics", {"route": route})

    def forecast(self, lat=None, lon=None, hours=12) -> dict:
        return self._get("/forecast", {"lat": lat, "lon": lon, "hours": hours})

    def route(self, route=None, target="next") -> dict:
        return self._get("/route", {"route": route, "target": target})

    def deviation(self, route=None, variant=None) -> dict:
        """Route-deviation vs the active playbook variant's frozen optimal track (XTE / along-track /
        time-behind / VMC, fuzzy status). Lab-3 branch trigger (a); `na` with no playbook aboard."""
        return self._get("/deviation", {"route": route, "variant": variant})

    def drift(self, route=None) -> dict:
        """Forecast-drift vs the plan's frozen forecast reference (how far the common forecast has
        moved: veered/backed + speed, fuzzy status). Lab-3 branch trigger (b); `na` with no reference."""
        return self._get("/drift", {"route": route})

    def reachable(self) -> bool:
        return self.health().get("status") == "ok"
