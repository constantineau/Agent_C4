#!/usr/bin/env python3
"""Smoke-test the Orin's OpenAI-compatible LLM API (Phase 9.4 runtime bring-up).

Confirms the local server returns a coherent, grounded answer fully offline, and measures
latency + effective decode tok/s. This is the *exit test* for the runtime-bring-up milestone —
it deliberately does NO SR33 tool-calling (that's the next 9.4 increment); it just sends the
shape of prompt the copilot will use (narrate a few engine facts) and times it.

Run from the Orin or any host on the LAN:
    python3 pi/orin/smoke_api.py --base-url http://<orin-ip>:9000/v1

Pure stdlib (urllib) so it runs anywhere with no pip install — including a bare Orin.
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# A stand-in for the real engine facts the copilot will be fed. The numbers are illustrative;
# the point is to confirm the model narrates grounded facts without inventing strategy.
SYSTEM = (
    "You are the SR33's onboard sailing copilot. Speak only from the facts given. "
    "Be concise (2-3 sentences). Never invent numbers or tactics not present in the facts."
)
FACTS = (
    "Engine facts right now: TWS 12.4 kn, TWA 42 deg (upwind, starboard), boatspeed 6.8 kn "
    "= 96% of polar target 7.1 kn, heel 18 deg, next mark 'W' bearing 015 deg at 1.3 nm, "
    "ETA 11 min, layline not yet reached (3 deg below). Tell the crew how we're doing."
)


def post_chat(base_url: str, model: str, timeout: float):
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": FACTS},
        ],
        "temperature": 0.3,
        "max_tokens": 160,
        "stream": False,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        # most local servers ignore auth; send a dummy so OpenAI-style clients are happy
        "Authorization": "Bearer local",
    })
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    dt = time.monotonic() - t0
    return data, dt


def discover_model(base_url: str, timeout: float):
    """Ask the server which model id it loaded, so --model is optional."""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=timeout) as r:
            data = json.loads(r.read())
        return data["data"][0]["id"]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:9000/v1",
                    help="OpenAI-compatible base URL (…/v1)")
    ap.add_argument("--model", default=None,
                    help="model id (default: auto-discover via /models)")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    model = args.model or discover_model(args.base_url, args.timeout)
    if not model:
        print("!! could not discover a model id from /models — pass --model explicitly", file=sys.stderr)
        model = "local"
    print(f">> server: {args.base_url}   model: {model}")

    try:
        data, dt = post_chat(args.base_url, model, args.timeout)
    except urllib.error.URLError as e:
        print(f"!! request failed: {e}\n   is `serve.sh` up? check `docker logs sr33-orin-llm`",
              file=sys.stderr)
        sys.exit(1)

    try:
        msg = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError):
        print("!! unexpected response shape:", json.dumps(data)[:500], file=sys.stderr)
        sys.exit(1)

    usage = data.get("usage", {})
    out_tok = usage.get("completion_tokens")
    tps = (out_tok / dt) if (out_tok and dt > 0) else None

    print("\n--- answer " + "-" * 50)
    print(msg)
    print("-" * 61)
    print(f"total latency : {dt:5.2f} s")
    if out_tok:
        print(f"output tokens : {out_tok}")
    if tps:
        print(f"effective rate: {tps:5.1f} tok/s   (NVIDIA Super-mode ref: Qwen2.5-7B ~21.8)")
    # crude pass/fail for an automated run
    ok = len(msg) > 20 and ("polar" in msg.lower() or "kn" in msg.lower() or "mark" in msg.lower()
                            or "knot" in msg.lower() or "96" in msg)
    print("\nPASS: grounded answer returned offline." if ok else
          "\n⚠️  answer returned but didn't reference the facts — inspect above.")
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
