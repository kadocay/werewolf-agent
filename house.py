#!/usr/bin/env python3
"""Heuristic house players for the local harness: a stand-in for the
claude-haiku-5-5 house seats.

Each house seat decides from its own view only, with a simple suspicion
model (noisy, incomplete compared with the agent's), templated statements
and per-seat style variation (aggressiveness, noise) drawn from a seeded
RNG. They are deliberately NOT the agent's logic: the harness must not
reward mirroring the agent.

A house seat's decisions:
  speak    accuse its top suspect (with a reason), claim seer with a wolf
           check, counter-claim a false seer, or pass sometimes
  vote     its top suspect most of the time, noise otherwise
  kill     the seer claimant if one is known, else the most trusted
           villager; second wolf matches the partner's pick
  protect  a claimed seer, else the most trusted villager
  inspect  the most suspicious uninspected seat
"""

import hashlib
import random
import re

from engine import team_of
from main import parse_speech


def _rng(seed, game, seat, label):
    h = hashlib.sha256(
        (str(seed) + "|" + str(game) + "|" + str(seat) + "|" + label)
        .encode("utf-8")).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


class HousePlayer:
    def __init__(self, seed, game_no, seat, role):
        self.seed = seed
        self.game = game_no
        self.seat = seat
        self.role = role
        # style, fixed per seat per game
        r = _rng(seed, game_no, seat, "style")
        self.aggression = r.uniform(0.5, 1.0)   # how often it accuses
        self.noise = r.uniform(0.05, 0.25)      # how often a vote is random
        self.fake_seer_p = r.uniform(0.0, 0.15)

    # -- analysis ---------------------------------------------------------
    def _analyse(self, view):
        """Simple suspicion scores over living seats (not self)."""
        me = self.seat
        players = {p["seat"]: p for p in view["players"]}
        living = [s for s in sorted(players) if players[s]["alive"]]
        # a small per-seat prior, so day 1 is not a deterministic pile-on
        # of the lowest seat: every seat reads the same empty table
        # differently, as real players do
        prior = _rng(self.seed, self.game, me, "prior")
        scores = {s: prior.uniform(-0.15, 0.15) for s in living if s != me}
        revealed = {s: players[s]["role"] for s in players
                    if players[s].get("role")}
        # accusations count once per (speaker, target): a bandwagon is
        # one voice each, not the same voice counted six times
        accused_by = {}
        # my own seer results are exact
        for r in view.get("seerResults") or []:
            if r["werewolf"]:
                scores[r["seat"]] = 99.0
            else:
                scores[r["seat"]] = -99.0
        # parse statements with the same reader the agent uses: a claim is
        # what the words say, however they are phrased
        name_to_seat = {p["name"].lower(): p["seat"]
                        for p in view["players"]}
        facts = []
        for e in view["log"]:
            if e.get("kind") == "speech" and e.get("text"):
                facts.extend(parse_speech(e["text"], e["seat"],
                                          name_to_seat))
        for f in facts:
            if f["kind"] == "wolf_check":
                sp, tgt = f["speaker"], f["target"]
                if tgt in revealed:
                    if revealed[tgt] == "werewolf":
                        scores[sp] = scores.get(sp, 0.0) - 2.0
                    else:
                        scores[sp] = scores.get(sp, 0.0) + 2.0
                elif tgt in scores:
                    # a check from a claimed seer weighs far more than a
                    # bare accusation, as it does for a real player
                    claimants = {g["speaker"] for g in facts
                                 if g["kind"] == "seer_claim"}
                    scores[tgt] += 1.5 if sp in claimants else 0.8
            elif f["kind"] == "clear_check":
                tgt = f["target"]
                if tgt in revealed:
                    if revealed[tgt] == "werewolf":
                        scores[f["speaker"]] = \
                            scores.get(f["speaker"], 0.0) + 2.0
                elif tgt in scores:
                    scores[tgt] -= 1.0  # a cleared seat is trusted
            elif f["kind"] == "accuse":
                tgt = f["target"]
                if tgt in scores and (f["speaker"], tgt) not in accused_by:
                    accused_by[(f["speaker"], tgt)] = True
                    # cap the herding a target can attract from bare
                    # accusations: the first few voices matter, not the echo
                    if sum(1 for (a, b) in accused_by if b == tgt) <= 5:
                        scores[tgt] += 0.1
        # exile outcomes
        for e in view["log"]:
            if e.get("kind") != "exile" or e.get("seat") is None:
                continue
            day = e["day"]
            voters = [v["seat"] for v in view["log"]
                      if v.get("kind") == "vote" and v.get("day") == day
                      and v.get("target") == e["seat"]]
            if e["role"] == "werewolf":
                for v in voters:
                    if v in scores:
                        scores[v] -= 1.0
            elif e["role"] in ("villager", "seer", "doctor"):
                for v in voters:
                    if v in scores:
                        scores[v] += 0.7
        return scores, revealed, living, players

    def _top(self, scores, exclude=()):
        cands = [s for s in scores if s not in exclude]
        if not cands:
            return None
        cands.sort(key=lambda s: (scores[s], -s), reverse=True)
        return cands[0]

    def _name(self, view, seat):
        for p in view["players"]:
            if p["seat"] == seat:
                return p["name"]
        return "seat %d" % seat

    def _who(self, view, seat):
        return "seat %d (%s)" % (seat, self._name(view, seat))

    def _seer_claimants(self, view):
        out = []
        for e in view["log"]:
            if e.get("kind") == "speech" and e.get("text"):
                t = e["text"].lower()
                if ("i am the seer" in t or "i'm the seer" in t) \
                        and e["seat"] not in out:
                    out.append(e["seat"])
        return out

    def _trust_rank(self, view, scores):
        """Most trusted living seats first (low suspicion, vocal)."""
        counts = {}
        for e in view["log"]:
            if e.get("kind") == "speech":
                counts[e["seat"]] = counts.get(e["seat"], 0) + 1
        cands = [s for s in scores]
        cands.sort(key=lambda s: (-scores[s], counts.get(s, 0), -s),
                   reverse=True)
        return cands

    # -- decisions ----------------------------------------------------------
    def decide(self, view, decision):
        kind = decision["kind"]
        rng = _rng(self.seed, self.game, self.seat, "d:%d:%s" % (view["day"], kind))
        if kind == "speak":
            return self._speak(view, rng)
        if kind == "vote":
            return self._vote(view, decision, rng)
        if kind == "kill":
            return self._kill(view, decision, rng)
        if kind == "protect":
            return self._protect(view, decision, rng)
        if kind == "inspect":
            return self._inspect(view, decision, rng)
        return None

    def _speak(self, view, rng):
        me = self.seat
        scores, revealed, living, players = self._analyse(view)
        top = self._top(scores)
        my_checks = {r["seat"]: r["werewolf"]
                     for r in view.get("seerResults") or []}
        wolf_found = [s for s, w in my_checks.items() if w]
        # seer with a wolf check: claim
        if self.role == "seer" and wolf_found and rng.random() < 0.9:
            t = wolf_found[0]
            return ("I am the seer. I inspected %s and they are a "
                    "werewolf. Vote %s." % (self._who(view, t),
                                            self._who(view, t)))
        # seer counter-claim
        if self.role == "seer":
            others = [c for c in self._seer_claimants(view)
                      if c != me and c in living]
            if others and rng.random() < 0.8:
                return ("I am the seer, not %s. They are lying."
                        % self._who(view, others[0]))
        # wolf fake seer claim (rare, day 2+, nobody claimed yet)
        if self.role == "werewolf" and view["day"] >= 2 \
                and not self._seer_claimants(view) \
                and rng.random() < self.fake_seer_p and top is not None:
            return ("I am the seer. I inspected %s and they are a "
                    "werewolf. Vote %s." % (self._who(view, top),
                                            self._who(view, top)))
        if top is None or rng.random() > self.aggression:
            return None  # pass
        # accuse the top suspect, with a reason if there is one
        reason = ""
        for e in view["log"]:
            if e.get("kind") == "exile" and e.get("seat") is not None \
                    and e.get("role") in ("villager", "seer", "doctor"):
                voters = [v["seat"] for v in view["log"]
                          if v.get("kind") == "vote"
                          and v.get("day") == e["day"]
                          and v.get("target") == e["seat"]]
                if top in voters:
                    reason = ("they voted to exile %s, who was a %s"
                              % (self._who(view, e["seat"]), e["role"]))
                    break
        if not reason:
            reason = "their story does not add up"
        templates = [
            "I think %s is a wolf: %s. I am voting %s.",
            "%s looks the most suspicious to me: %s. Vote %s.",
            "Let's look at %s. %s. My vote is %s.",
        ]
        return rng.choice(templates) % (self._who(view, top), reason,
                                         self._who(view, top))

    def _vote(self, view, decision, rng):
        scores, _rev, _liv, _pl = self._analyse(view)
        options = decision["options"]
        if rng.random() < self.noise:
            return rng.choice(options)
        # a strong signal (a seer check, a settled record) is followed
        # outright; otherwise consolidate on the vote leader when it is
        # close to my own top suspect or my pick cannot win
        ordered = sorted(scores, key=lambda s: (scores[s], -s), reverse=True)
        top = ordered[0] if ordered else None
        tally = {}
        for e in view["log"]:
            if e.get("kind") == "vote" and e.get("day") == view["day"] \
                    and e.get("target") is not None:
                tally[e["target"]] = tally.get(e["target"], 0) + 1
        if top is not None and scores[top] >= 1.0 and top in options:
            return top
        if tally:
            leader = max(tally, key=lambda s: (tally[s], -s))
            # join the leader only when it already has a real lead (at
            # least 2 votes) and either it carries a real signal or my
            # own pick cannot win anyway; a bare 1-vote lead on an empty
            # table is not a reason to follow anyone
            if leader in options and tally[leader] >= 2 and top is not None \
                    and (scores[leader] >= 0.5
                         and scores[leader] >= scores[top] - 0.5
                         or tally.get(top, 0) == 0):
                return leader
        if top is not None and top in options:
            return top
        return rng.choice(options)

    def _kill(self, view, decision, rng):
        options = decision["options"]
        # second wolf: match the partner's pick (it is in the view)
        partner = view["you"].get("partner")
        if partner:
            for v in view.get("wolfVotes") or []:
                if v.get("night") == view["day"] \
                        and v.get("seat") == partner["seat"] \
                        and v.get("target") in options:
                    return v["target"]
        # seer claimant first
        claimants = [c for c in self._seer_claimants(view) if c in options]
        if claimants and rng.random() < 0.85:
            return claimants[0]
        scores, _rev, _liv, _pl = self._analyse(view)
        trusted = self._trust_rank(view, scores)
        for seat in trusted:
            if seat in options:
                return seat
        return rng.choice(options)

    def _protect(self, view, decision, rng):
        options = decision["options"]
        claimants = [c for c in self._seer_claimants(view) if c in options]
        if claimants and rng.random() < 0.7:
            return claimants[0]
        scores, _rev, _liv, _pl = self._analyse(view)
        trusted = self._trust_rank(view, scores)
        for seat in trusted:
            if seat in options:
                return seat
        return rng.choice(options) if options else None

    def _inspect(self, view, decision, rng):
        options = decision["options"]
        checked = {r["seat"] for r in view.get("seerResults") or []}
        scores, _rev, _liv, _pl = self._analyse(view)
        rest = [s for s in scores if s not in checked]
        if rest:
            top = max(rest, key=lambda s: (scores[s], -s))
            return top
        return rng.choice(options) if options else None
