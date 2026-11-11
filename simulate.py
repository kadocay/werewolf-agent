#!/usr/bin/env python3
"""Local test harness for the werewolf agent.

Runs the real practice trials (practice/trial-0/1/2) against an env shim
that speaks the same /observe + /act HTTP protocol as program/env/server.mjs
(tables open 10 at a time, house seats play by heuristic rules), with the
agent under test as a subprocess (exactly how the grader runs it).

Usage:
  python3 simulate.py [--agent main.py|baseline.py] [--trials 0,1,2]
                      [--seed SEED] [--out results.json]

The harness itself is deterministic: house play is seeded from
(trial seed, game, seat, label), and the agent is deterministic, so a
rerun of the same trial gives the same result.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Game, team_of  # noqa: E402
from house import HousePlayer  # noqa: E402

KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "werewolf-agent-01-kit")
BOT = os.path.dirname(os.path.abspath(__file__))
TABLES_AT_ONCE = 10


class Slot:
    def __init__(self, n, deal):
        self.n = n
        self.deal = deal
        self.game = None
        self.ended = False
        self.house = {}  # seat -> HousePlayer


class Shim:
    def __init__(self, table, seed):
        self.table = table
        self.seed = seed
        self.slots = [Slot(i + 1, g) for i, g in enumerate(table["games"])]
        self.opened = 0
        self.live = 0
        self.ended = 0
        self.lock = threading.Lock()
        self.fill()

    # -- game flow ---------------------------------------------------------
    def fill(self):
        while self.live < TABLES_AT_ONCE and self.opened < len(self.slots):
            s = self.slots[self.opened]
            self.opened += 1
            self.live += 1
            deal = {"roles": s.deal["roles"], "names": s.deal["names"],
                    "start": s.deal["start"]}
            s.game = Game(deal)
            entry = s.deal["entry"]
            for seat in range(1, 9):
                if seat != entry:
                    s.house[seat] = HousePlayer(self.seed, s.n, seat,
                                                s.deal["roles"][seat - 1])
            self.advance(s)

    def running(self, s):
        return s.game is not None and not s.game.over and not s.ended

    def end(self, s):
        if s.ended:
            return
        s.ended = True
        self.live -= 1
        self.ended += 1
        self.fill()

    def advance(self, slot):
        """Play house turns until the entry has a decision waiting or the
        game is over. House decisions are acted in pending order."""
        while True:
            if not self.running(slot):
                return
            g = slot.game
            if g.over:
                self.end(slot)
                return
            pend = g.pending()
            if not pend:
                return
            entry = slot.deal["entry"]
            if any(d["seat"] == entry for d in pend):
                return  # waits on the entry
            d = pend[0]
            hp = slot.house[d["seat"]]
            view = g.view_of(d["seat"])
            decision = {"seat": d["seat"], "kind": d["kind"],
                        "options": d.get("options"),
                        "maxChars": d.get("maxChars")}
            text = None
            target = None
            if d["kind"] == "speak":
                text = hp.decide(view, decision)
                action = {"seat": d["seat"], "kind": "speak", "text": text}
            else:
                target = hp.decide(view, decision)
                action = {"seat": d["seat"], "kind": d["kind"],
                          "target": target}
            r = g.apply(action)
            if not r.get("ok"):
                # house made an illegal move: fall back to pass/abstain
                if d["kind"] == "speak":
                    action = {"seat": d["seat"], "kind": "speak",
                              "text": None}
                else:
                    action = {"seat": d["seat"], "kind": d["kind"],
                              "target": None}
                r = g.apply(action)
                if not r.get("ok"):
                    raise RuntimeError("house fallback refused: %r" % (r,))
            if g.over:
                self.end(slot)
                return

    # -- routes --------------------------------------------------------------
    def snapshot(self):
        pending = []
        for s in self.slots:
            if not self.running(s):
                continue
            g = s.game
            entry = s.deal["entry"]
            d = g.decision_for(entry)
            if d is None:
                continue
            view = g.view_of(entry)
            if d["kind"] == "speak":
                pending.append({"game": s.n, "decision": "speak",
                                "max_chars": d["maxChars"], "view": view})
            else:
                pending.append({"game": s.n, "decision": d["kind"],
                                "options": list(d["options"]), "view": view})
        return {"games": self.opened, "finished": self.ended,
                "done": self.ended == len(self.slots), "pending": pending}

    def act(self, body):
        game_no = body.get("game")
        if not isinstance(game_no, int) or not (1 <= game_no <= len(self.slots)):
            return 400, {"error": "bad_request", "message": "bad game"}
        s = self.slots[game_no - 1]
        kind = body.get("decision")
        entry = s.deal["entry"]
        if kind not in ("speak", "vote", "kill", "protect", "inspect"):
            return 400, {"error": "bad_request", "message": "bad decision"}
        if not self.running(s):
            return 409, {"error": "not_pending", "message": "game not open"}
        g = s.game
        d = g.decision_for(entry)
        if d is None or d["kind"] != kind:
            return 409, {"error": "not_pending",
                         "message": "no %s decision waits" % kind}
        if kind == "speak":
            if "text" in body and "pass" in body:
                return 400, {"error": "bad_request",
                             "message": "text or pass, not both"}
            if body.get("pass") is True:
                action = {"seat": entry, "kind": "speak", "text": None}
            elif isinstance(body.get("text"), str):
                action = {"seat": entry, "kind": "speak",
                          "text": body["text"]}
            else:
                return 400, {"error": "bad_request",
                             "message": "speak takes text or pass"}
        else:
            if "target" not in body:
                return 400, {"error": "bad_request",
                             "message": "a target is required"}
            target = body["target"]
            if target is not None and not isinstance(target, int):
                return 400, {"error": "bad_request",
                             "message": "target is a seat or null"}
            if target is not None and target not in d["options"]:
                return 400, {"error": "bad_target",
                             "message": "seat is not an option"}
            action = {"seat": entry, "kind": kind, "target": target}
        r = g.apply(action)
        if not r.get("ok"):
            status = 409 if r["reason"] == "not_pending" else 400
            return status, {"error": r["reason"],
                            "message": r.get("detail", "")}
        if g.over:
            self.end(s)
        else:
            self.advance(s)
        return 200, {"ok": True, "game": s.n, "decision": kind}

    def results(self):
        """Per-game outcomes plus the trial score columns."""
        games = []
        for s in self.slots:
            g = s.game
            entry_role = s.deal["roles"][s.deal["entry"] - 1]
            outcome = g.outcome if g else None
            won = bool(outcome and outcome["winner"] is not None
                       and s.deal["entry"] in outcome["winners"])
            games.append({
                "game": s.n, "role": entry_role, "entry_seat": s.deal["entry"],
                "winner": outcome["winner"] if outcome else None,
                "reason": outcome["reason"] if outcome else None,
                "day": outcome["day"] if outcome else None,
                "won": won, "finished": bool(g and g.over),
                "roles": list(s.deal["roles"]),
                "actions": [dict(a) for a in g.actions] if g else [],
                "log": [dict(e) for e in g.log] if g else [],
            })
        return games


def make_handler(shim):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _read_json(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n > 0 else b""
            try:
                return json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                return None

        def _send(self, status, body):
            data = (json.dumps(body) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self._read_json()
            with shim.lock:
                if self.path == "/observe":
                    if not isinstance(body, dict):
                        self._send(400, {"error": "bad_request"})
                        return
                    self._send(200, shim.snapshot())
                elif self.path == "/act":
                    if not isinstance(body, dict):
                        self._send(400, {"error": "bad_request"})
                        return
                    status, resp = shim.act(body)
                    self._send(status, resp)
                elif self.path == "/game":
                    if not isinstance(body, dict):
                        self._send(400, {"error": "bad_request"})
                        return
                    n = body.get("game")
                    if not isinstance(n, int) or not (1 <= n <= len(shim.slots)):
                        self._send(400, {"error": "bad_request"})
                        return
                    s = shim.slots[n - 1]
                    if s.game is None:
                        self._send(409, {"error": "not_pending"})
                        return
                    self._send(200, s.game.view_of(s.deal["entry"]))
                else:
                    self._send(404, {"error": "not_found"})
    return Handler


def run_trial(trial_no, agent_file, seed, timeout=300):
    """Run one practice trial; return (games, agent_rc, agent_stderr)."""
    kit_trial = os.path.join(KIT, "practice", "trial-%d" % trial_no)
    table = json.load(open(os.path.join(kit_trial, "input", "env",
                                        "table.json")))
    prompt = os.path.join(kit_trial, "input", "prompt.txt")
    shim = Shim(table, seed)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(shim))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    env = dict(os.environ)
    env["MAKEFUN_ENV_URL"] = "http://127.0.0.1:%d" % port
    env["MAKEFUN_TRIAL_SEED"] = seed
    env["MAKEFUN_PROMPT_FILE"] = prompt
    env["ANTHROPIC_API_KEY"] = "dummy"
    env["OPENAI_API_KEY"] = "dummy"
    try:
        proc = subprocess.run(
            [sys.executable, "-P", "-s",
             os.path.join(BOT, agent_file)],
            env=env, capture_output=True, text=True, timeout=timeout)
        rc, err = proc.returncode, proc.stderr
    except subprocess.TimeoutExpired as e:
        rc, err = "timeout", (e.stderr or b"").decode("utf-8", "replace") \
            if isinstance(e.stderr, bytes) else (e.stderr or "")
    finally:
        server.shutdown()
        server.server_close()
    return shim.results(), rc, err


def summarize(all_games):
    """Aggregate the scorer's columns over trials."""
    wolf_w = sum(1 for gs in all_games for g in gs
                 if g["role"] == "werewolf" and g["won"])
    wolf_n = sum(1 for gs in all_games for g in gs
                 if g["role"] == "werewolf")
    vil_w = sum(1 for gs in all_games for g in gs
                if g["role"] != "werewolf" and g["won"])
    vil_n = sum(1 for gs in all_games for g in gs
                if g["role"] != "werewolf")
    by_role = {}
    for role in ("seer", "doctor", "villager"):
        w = sum(1 for gs in all_games for g in gs
                if g["role"] == role and g["won"])
        n = sum(1 for gs in all_games for g in gs if g["role"] == role)
        by_role[role] = (w, n)
    finished = sum(1 for gs in all_games for g in gs if g["finished"])
    total = sum(len(gs) for gs in all_games)
    win_days = sum(g["day"] for gs in all_games for g in gs
                   if g["won"] and g["day"] is not None)
    # vote accuracy: the entry's day votes in its village-role games that
    # named a werewolf (per mille)
    tot = hits = 0
    for gs in all_games:
        for g in gs:
            if g["role"] == "werewolf":
                continue
            for a in g["actions"]:
                if a["kind"] == "vote" and a.get("seat") == g["entry_seat"] \
                        and a.get("target") is not None:
                    tot += 1
                    if g["roles"][a["target"] - 1] == "werewolf":
                        hits += 1
    va = (1000 * hits // tot) if tot else 0
    score = round(1000 * ((wolf_w / wolf_n if wolf_n else 0)
                          + (vil_w / vil_n if vil_n else 0)) / 2)
    return {
        "score": score,
        "wolf": (wolf_w, wolf_n),
        "village": (vil_w, vil_n),
        "seer": by_role["seer"], "doctor": by_role["doctor"],
        "villager": by_role["villager"],
        "vote_accuracy": va,
        "finished": (finished, total),
        "win_days": win_days,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="main.py")
    ap.add_argument("--trials", default="0,1,2")
    ap.add_argument("--seed", default="harness-seed-1")
    ap.add_argument("--out", default=None)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    trial_nos = [int(x) for x in args.trials.split(",") if x != ""]
    all_games = []
    for tn in trial_nos:
        games, rc, err = run_trial(tn, args.agent, args.seed,
                                   timeout=args.timeout)
        all_games.append(games)
        w = sum(1 for g in games if g["won"])
        ww = sum(1 for g in games if g["role"] == "werewolf" and g["won"])
        vw = sum(1 for g in games if g["role"] != "werewolf" and g["won"])
        print("trial %d: agent rc=%s, won %d/30 (wolf %d/15, village %d/15), "
              "finished %d/30" % (tn, rc, w, ww, vw,
                                  sum(1 for g in games if g["finished"])))
        if err and rc not in (0,):
            print("  agent stderr (tail):", err.strip().splitlines()[-5:])
    summary = summarize(all_games)
    print("SUMMARY agent=%s trials=%s seed=%s" % (args.agent, trial_nos,
                                                   args.seed))
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"agent": args.agent, "trials": trial_nos,
                       "seed": args.seed, "summary": summary,
                       "games": all_games}, f, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
