"""Test helper: route on sr33's OWN profile polar, and say so (#303).

Since #303 the Lab has no default polar: every route, play and debrief is handed the boat's own
polar block (`boats.polar_for(boat)`), and a call with none refuses. The older routing tests were
written against sr33's polar and call the optimizer, the sail plan and the polar helpers bare.
`install()` makes exactly those bare calls use sr33's polar, resolved from vps/lab/boats/sr33.json
the same way as any boat's, so those tests keep testing what they test.

The polar plumbing itself (a second boat, the refusal, "no sr33 file is opened for Bravo") is
tested in test_boat_polar_lab.py, which never installs this.
"""
import functools
import inspect
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_SR33 = None
_INSTALLED = False


def sr33():
    """sr33's polar block, from its bundled profile (whatever BOATS_DIR a test points at)."""
    global _SR33
    if _SR33 is None:
        from shared import boat_polar
        prof = None
        for d in (os.path.join(_HERE, "boats"), "/srv/boats"):
            p = os.path.join(d, "sr33.json")
            if os.path.exists(p):
                with open(p) as f:
                    prof = json.load(f)
                break
        _SR33 = boat_polar.resolve(prof)
        assert _SR33, "sr33's polar files not found (BOAT_DATA_DIR / repo seed)"
    return _SR33


def _default(fn, name, value_fn, unless=()):
    """Wrap `fn` so a call that leaves parameter `name` unset/None gets `value_fn()` (positional or
    keyword, by the signature), unless one of `unless` is given."""
    if getattr(fn, "__wrapped_polar_fixture__", False):
        return fn
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            ba = sig.bind_partial(*a, **kw)
        except TypeError:
            return fn(*a, **kw)
        args = ba.arguments
        if args.get(name) is None and not any(args.get(u) is not None for u in unless):
            args[name] = value_fn()
        return fn(*ba.args, **ba.kwargs)
    wrapper.__wrapped_polar_fixture__ = True
    return wrapper


def install():
    """Bare optimizer / sail-plan / polar calls in this process use sr33's polar. Safe to call
    again (a test that reloads the optimizer calls it after the reload)."""
    global _INSTALLED
    from app import judge, optimizer, playbook, polars, sailplan, synthesis
    pol = sr33
    xo = lambda: sr33()["crossovers"]                                         # noqa: E731
    optimizer.optimize_course = _default(optimizer.optimize_course, "boat_polar", pol,
                                            unless=("polar",))
    optimizer.route_leg = _default(optimizer.route_leg, "xo", xo)
    playbook.build_playbook = _default(playbook.build_playbook, "boat_polar", pol)
    synthesis._sail_guidance_plays = _default(synthesis._sail_guidance_plays, "boat_polar", pol)
    synthesis._boat_model = _default(synthesis._boat_model, "boat_polar", pol)
    synthesis.synthesize = _default(synthesis.synthesize, "boat_polar", pol)
    polars.polars_stw = _default(polars.polars_stw, "block", pol)
    polars.sail_polars = _default(polars.sail_polars, "block", pol)
    sailplan.optimal_sail = _default(sailplan.optimal_sail, "xo", xo)
    sailplan.crossovers_specialized = _default(sailplan.crossovers_specialized, "xo", xo)
    sailplan.overlaps_specialized = _default(sailplan.overlaps_specialized, "xo", xo)
    sailplan.crossovers = _default(sailplan.crossovers, "xo", xo)
    sailplan.model = _default(sailplan.model, "xo", xo)
    sailplan.available = _default(sailplan.available, "xo", xo)
    # the debrief scores the active boat; a test that never picked one scores on sr33's polar
    if not getattr(judge.boats.polar_for, "__wrapped_polar_fixture__", False):
        _polar_for = judge.boats.polar_for

        def polar_for(b):
            return _polar_for(b) or sr33()
        polar_for.__wrapped_polar_fixture__ = True
        judge.boats.polar_for = polar_for
    _INSTALLED = True
    return sr33()
