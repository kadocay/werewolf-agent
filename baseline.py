#!/usr/bin/env python3
"""Plain heuristic baseline agent, for the harness comparison only.

No game memory, no deduction: it speaks a generic line or passes, and
picks targets at random (seeded) among the options. It plays the same
/env protocol as main.py, so a harness score for it is directly comparable
with the full agent's score.
"""

import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

ENV_URL = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
TRIAL_SEED = os.environ.get("MAKEFUN_TRIAL_SEED") or "default-seed"


def rng_for(game, label):
    h = hashlib.sha256(
        (TRIAL_SEED + "|" + str(game) + "|" + label).encode("utf-8")).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def post_json(url, body, timeout=30.0):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def decide(p):
    kind = p["decision"]
    game = p["game"]
    if kind == "speak":
        rng = rng_for(game, "speak:%d" % p["view"]["day"])
        if rng.random() < 0.5:
            return {"game": game, "decision": "speak", "pass": True}
        return {"game": game, "decision": "speak",
                "text": "I don't have a strong read yet. Let's see how the votes go."}
    rng = rng_for(game, kind)
    options = p.get("options") or []
    target = rng.choice(options) if options else None
    return {"game": game, "decision": kind, "target": target}


def fallback(p):
    if p["decision"] == "speak":
        return {"game": p["game"], "decision": "speak", "pass": True}
    return {"game": p["game"], "decision": p["decision"], "target": None}


def run():
    if not ENV_URL:
        sys.stderr.write("baseline: MAKEFUN_ENV_URL is not set\n")
        return 1
    while True:
        status, obs = post_json(ENV_URL + "/observe", {}, timeout=30.0)
        if status is None or not isinstance(obs, dict):
            time.sleep(0.25)
            continue
        if obs.get("done"):
            break
        for p in obs.get("pending") or []:
            act = decide(p)
            status2, _ = post_json(ENV_URL + "/act", act, timeout=30.0)
            if status2 != 200:
                post_json(ENV_URL + "/act", fallback(p), timeout=30.0)
        if not obs.get("pending"):
            time.sleep(0.05)
    return 0


if __name__ == "__main__":
    sys.exit(run())
