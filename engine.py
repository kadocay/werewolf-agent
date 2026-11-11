#!/usr/bin/env python3
"""Werewolf game engine: a faithful port of program/env/server.mjs's
src/werewolf/game.ts, for local testing only.

8 seats, 2 werewolves / 1 seer / 1 doctor / 4 villagers. Night then day,
2 debate cycles, one vote each, unique plurality exiles, max day 10.
"""

import copy
import re

ROLES = ["werewolf", "seer", "doctor", "villager"]
SEATS = 8
ROLE_MIX = {"werewolf": 2, "seer": 1, "doctor": 1, "villager": 4}
MAX_STATEMENT_CHARS = 500
DEBATE_CYCLES = 2
MAX_DAYS = 10
DECISION_KINDS = ["speak", "vote", "kill", "protect", "inspect"]


def team_of(role):
    return "werewolves" if role == "werewolf" else "village"


def is_seat(x):
    return isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= SEATS


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def fold_statement(text):
    text = re.sub(r"[\t\n\v\f\r\x85\u2028\u2029]+", " ", text)
    text = _CONTROL.sub("", text)
    return text.strip()


def statement_length(text):
    return len(list(text))


class Game:
    def __init__(self, deal):
        # deal: {"roles": [...8...], "names": [...8...], "start": int}
        self.deal = {"roles": list(deal["roles"]),
                     "names": list(deal["names"]),
                     "start": deal["start"]}
        self.day = 1
        self.phase = "night"
        self.log = []
        self.actions = []
        self.wolf_votes = []   # {"night","seat","target"}
        self.protects = []     # {"night","seat"}
        self.inspections = []  # {"night","seat","werewolf"}
        self.order = []
        self.turn = 0
        self.day_votes = []
        self.alive = [True] * SEATS
        self.outcome = None
        self.push({"day": 1, "kind": "night"})

    # -- basic accessors --------------------------------------------------
    def role_of(self, seat):
        return self.deal["roles"][seat - 1]

    def name_of(self, seat):
        return self.deal["names"][seat - 1]

    def is_alive(self, seat):
        return self.alive[seat - 1]

    def revealed_role(self, seat):
        return None if self.is_alive(seat) else self.role_of(seat)

    def seats(self):
        return list(range(1, SEATS + 1))

    def living(self):
        return [s for s in self.seats() if self.is_alive(s)]

    def seats_of(self, role):
        return [s for s in self.seats() if self.role_of(s) == role]

    def partner_of(self, seat):
        if self.role_of(seat) != "werewolf":
            return None
        for s in self.seats_of("werewolf"):
            if s != seat:
                return s
        return None

    def wolf_order(self, night):
        wolves = [s for s in self.living() if self.role_of(s) == "werewolf"]
        return wolves if night % 2 == 1 else list(reversed(wolves))

    def protect_on(self, night):
        for p in self.protects:
            if p["night"] == night:
                return p
        return None

    @property
    def over(self):
        return self.outcome is not None

    def push(self, entry):
        entry = dict(entry)
        entry["seq"] = len(self.log) + 1
        self.log.append(entry)

    # -- decisions --------------------------------------------------------
    def pending(self):
        if self.outcome:
            return []
        out = []
        living = self.living()
        if self.phase == "night":
            n = self.day
            non_wolves = [s for s in living if self.role_of(s) != "werewolf"]
            wolf = None
            for s in self.wolf_order(n):
                if not any(v["night"] == n and v["seat"] == s
                           for v in self.wolf_votes):
                    wolf = s
                    break
            if wolf is not None:
                out.append({"seat": wolf, "kind": "kill",
                            "options": list(non_wolves)})
            doctor = self.seats_of("doctor")[0]
            if self.is_alive(doctor) and self.protect_on(n) is None:
                last = self.protect_on(n - 1)
                last_seat = last["seat"] if last else None
                out.append({"seat": doctor, "kind": "protect",
                            "options": [s for s in living
                                        if s != doctor and s != last_seat]})
            seer = self.seats_of("seer")[0]
            if self.is_alive(seer) and not any(
                    i["night"] == n for i in self.inspections):
                out.append({"seat": seer, "kind": "inspect",
                            "options": [s for s in living if s != seer]})
            out.sort(key=lambda d: d["seat"])
        elif self.phase == "debate":
            seat = self.order[self.turn % len(self.order)]
            out.append({"seat": seat, "kind": "speak",
                        "round": self.turn // len(self.order) + 1,
                        "maxChars": MAX_STATEMENT_CHARS})
        elif self.phase == "voting":
            seat = self.order[self.turn]
            out.append({"seat": seat, "kind": "vote",
                        "options": [s for s in living if s != seat]})
        return out

    def decision_for(self, seat):
        for d in self.pending():
            if d["seat"] == seat:
                return d
        return None

    # -- applying actions ---------------------------------------------------
    def apply(self, action):
        """Apply one action; returns {"ok": True} or {"ok": False, "reason": ...}."""
        if self.outcome:
            return {"ok": False, "reason": "game_over"}
        shape = self._shape_issue(action)
        if shape:
            return {"ok": False, "reason": "bad_request", "detail": shape}
        decision = None
        for d in self.pending():
            if d["seat"] == action["seat"] and d["kind"] == action["kind"]:
                decision = d
                break
        if decision is None:
            return {"ok": False, "reason": "not_pending",
                    "detail": "seat %s has no %s decision waiting"
                              % (action["seat"], action["kind"])}
        if action["kind"] == "speak":
            if action.get("text") is None:
                self.record(action)
                self.push({"day": self.day, "kind": "pass",
                           "seat": action["seat"], "round": decision["round"]})
            else:
                text = fold_statement(action["text"])
                if len(text) == 0:
                    return {"ok": False, "reason": "empty"}
                if statement_length(text) > MAX_STATEMENT_CHARS:
                    return {"ok": False, "reason": "too_long"}
                self.record(action)
                self.push({"day": self.day, "kind": "speech",
                           "seat": action["seat"],
                           "round": decision["round"], "text": text})
            self.turn += 1
            if self.turn == len(self.order) * DEBATE_CYCLES:
                self._start_voting()
            return {"ok": True}

        target = action.get("target")
        if target is not None and target not in decision["options"]:
            return {"ok": False, "reason": "bad_target"}
        self.record(action)
        n = self.day
        kind = action["kind"]
        if kind == "kill":
            self.wolf_votes.append({"night": n, "seat": action["seat"],
                                    "target": target})
        elif kind == "protect":
            self.protects.append({"night": n, "seat": target})
        elif kind == "inspect":
            self.inspections.append(
                {"night": n, "seat": target,
                 "werewolf": None if target is None
                 else self.role_of(target) == "werewolf"})
        elif kind == "vote":
            self.day_votes.append({"seat": action["seat"], "target": target})
            self.push({"day": n, "kind": "vote", "seat": action["seat"],
                       "target": target})
            self.turn += 1
            if self.turn == len(self.order):
                self._resolve_vote()
            return {"ok": True}
        if len(self.pending()) == 0:
            self._resolve_night()
        return {"ok": True}

    def _shape_issue(self, action):
        if not isinstance(action, dict):
            return "an action is an object"
        if not is_seat(action.get("seat")):
            return "an action's seat is 1 to 8"
        if action.get("kind") not in DECISION_KINDS:
            return "an action's kind is one of " + ", ".join(DECISION_KINDS)
        if action["kind"] == "speak":
            if not (isinstance(action.get("text"), str)
                    or action.get("text") is None):
                return "a speak action's text is a string, or null to pass"
        else:
            t = action.get("target")
            if not (t is None or is_seat(t)):
                return "an action's target is a seat 1 to 8, or null"
        return None

    def record(self, action):
        self.actions.append(copy.deepcopy(action))

    # -- resolution -----------------------------------------------------------
    def kill_target(self, night):
        named = set(v["target"] for v in self.wolf_votes
                    if v["night"] == night and v["target"] is not None)
        return next(iter(named)) if len(named) == 1 else None

    def _resolve_night(self):
        n = self.day
        target = self.kill_target(n)
        protect = self.protect_on(n)
        died = None
        if target is not None and not (protect and protect["seat"] == target):
            died = target
        if died is not None:
            self.alive[died - 1] = False
        self.push({"day": n, "kind": "dawn", "died": died,
                   "role": None if died is None else self.role_of(died)})
        if self._check_win():
            return
        self.phase = "debate"
        self.order = self._day_order(n)
        self.turn = 0
        self.day_votes = []
        self.push({"day": n, "kind": "debate", "order": list(self.order)})

    def _day_order(self, day):
        first = (self.deal["start"] - 1 + (day - 1)) % SEATS + 1
        out = []
        for k in range(SEATS):
            seat = (first - 1 + k) % SEATS + 1
            if self.is_alive(seat):
                out.append(seat)
        return out

    def _start_voting(self):
        self.phase = "voting"
        self.turn = 0
        self.push({"day": self.day, "kind": "voting",
                   "order": list(self.order)})

    def _resolve_vote(self):
        counts = {}
        for v in self.day_votes:
            if v["target"] is not None:
                counts[v["target"]] = counts.get(v["target"], 0) + 1
        tally = [{"seat": s, "votes": counts[s]} for s in sorted(counts)]
        top = max([t["votes"] for t in tally], default=0)
        leaders = [t for t in tally if t["votes"] == top]
        exiled = leaders[0]["seat"] if top >= 1 and len(leaders) == 1 else None
        if exiled is not None:
            self.alive[exiled - 1] = False
        self.push({"day": self.day, "kind": "exile", "seat": exiled,
                   "role": None if exiled is None else self.role_of(exiled),
                   "tally": tally})
        if self._check_win():
            return
        if self.day >= MAX_DAYS:
            self._end(None, "max_days")
            return
        self.day += 1
        self.phase = "night"
        self.order = []
        self.turn = 0
        self.day_votes = []
        self.push({"day": self.day, "kind": "night"})

    def _check_win(self):
        living = self.living()
        wolves = len([s for s in living if self.role_of(s) == "werewolf"])
        if wolves == 0:
            self._end("village", "no_wolves")
        elif wolves >= len(living) - wolves:
            self._end("werewolves", "parity")
        return self.outcome is not None

    def _end(self, winner, reason):
        winners = [] if winner is None else [
            s for s in self.seats() if team_of(self.role_of(s)) == winner]
        self.outcome = {"winner": winner, "reason": reason,
                        "day": self.day, "winners": winners}
        self.phase = "over"
        self.order = []
        self.turn = 0
        self.push({"day": self.day, "kind": "end", "winner": winner,
                   "reason": reason, "winners": list(winners)})

    # -- view -------------------------------------------------------------------
    def view_of(self, seat):
        role = self.role_of(seat)
        partner = self.partner_of(seat)
        last_protect = None
        if role == "doctor" and self.protects:
            last_protect = self.protects[-1]
        return {
            "day": self.day,
            "phase": self.phase,
            "you": {
                "seat": seat,
                "name": self.name_of(seat),
                "role": role,
                "alive": self.is_alive(seat),
                "partner": None if partner is None else {
                    "seat": partner, "name": self.name_of(partner),
                    "alive": self.is_alive(partner)},
            },
            "players": [{"seat": s, "name": self.name_of(s),
                         "alive": self.is_alive(s),
                         "role": self.revealed_role(s)}
                        for s in self.seats()],
            "seerResults": None if role != "seer" else [
                {"night": i["night"], "seat": i["seat"],
                 "werewolf": i["werewolf"]}
                for i in self.inspections if i["seat"] is not None],
            "lastProtect": None if last_protect is None else {
                "night": last_protect["night"], "seat": last_protect["seat"]},
            "wolfVotes": None if role != "werewolf" else [
                {"night": v["night"], "seat": v["seat"], "target": v["target"]}
                for v in self.wolf_votes],
            "log": copy.deepcopy(self.log),
            "outcome": copy.deepcopy(self.outcome),
        }
