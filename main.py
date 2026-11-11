#!/usr/bin/env python3
"""Werewolf agent for make.fun competition werewolf-agent-01.

Pure heuristic agent: no model calls, no API key needed. Its edge over the
house baseline (a frontier model asked plainly, once per decision, with no
memory and no strategy) is full game memory plus structured deduction:
it rebuilds every game's state from the env's log on every decision,
maintains suspicion scores over the living seats, and plays each role with
a fixed, tested plan (see STRATEGY.md).

Determinism: every decision is a pure function of the view the env served
for that game plus MAKEFUN_TRIAL_SEED. No wall clock is read, no unseeded
randomness is used (a per-game, per-label seeded RNG only), and decisions
are sent to the env one at a time, in the order /observe lists them.

Protocol: POST /observe (long poll) -> POST /act per pending decision,
until "done" is true. Refusals fall back to pass/abstain and the loop
re-observes.
"""

import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

ENV_URL = (os.environ.get("MAKEFUN_ENV_URL") or "").rstrip("/")
TRIAL_SEED = os.environ.get("MAKEFUN_TRIAL_SEED") or "default-seed"
MAX_CHARS = 500

# Strategy knobs (environment overrides for harness A/B tests; the trial
# environment sets none of these, so the defaults below are what is graded).
# WF_FAKE_SEER: 1 = a wolf may fake-claim seer from day 2 when nobody has
#   claimed it yet; 0 = never fake-claim seer.
# WF_WOLF_VOTE: "offleader" = vote my top suspect but never the current
#   leader (a wolf that piles onto a mislynched villager is the easiest wolf
#   to spot in a vote-tracking village); "consolidate" = vote the current
#   vote leader when it is a suspect; "abstain" = never vote as a wolf.
WOLF_FAKE_SEER = os.environ.get("WF_FAKE_SEER", "1") == "1"
WOLF_VOTE = os.environ.get("WF_WOLF_VOTE", "offleader")
# WF_SEER_CLAIM_DAY1=1: the seer claims on day 1 with whatever check it
# has (clear or wolf). "0": it claims only when it has found a wolf or
# is under fire. Measure said 0: claiming on day 1 with only a clear made
# the seer the night-2 target and lost more games than the day-1 mislynch
# it was meant to prevent (harness, 720 games: 501.4 vs 484.8).
SEER_CLAIM_DAY1 = os.environ.get("WF_SEER_CLAIM_DAY1", "0") == "1"

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

_CC = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def fold_statement(text):
    """Fold a statement the env's way: runs of tabs/line breaks become one
    space, other control characters are dropped, ends are trimmed."""
    text = re.sub(r"[\t\n\v\f\r\x85\u2028\u2029]+", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc")
    return text.strip()


def clip_statement(text, max_chars):
    """Fold and cut to max_chars code points, preferring a word boundary."""
    folded = fold_statement(text)
    chars = list(folded)
    if len(chars) <= max_chars:
        return folded
    head = "".join(chars[:max_chars])
    # prefer ending at a sentence boundary, else a word boundary
    cut = -1
    for m in re.finditer(r"[.!?][\"')\]\u201d\u2019]*(?=\s|$)", head):
        cut = m.end()
    if cut < len(head) // 2:
        space = head.rfind(" ")
        cut = space if space > 0 else len(head)
    return head[:cut].strip()


def rng_for(game, label):
    """Deterministic RNG stream for one game and one label."""
    h = hashlib.sha256(
        (TRIAL_SEED + "|" + str(game) + "|" + label).encode("utf-8")
    ).digest()
    return random.Random(int.from_bytes(h[:8], "big"))


def post_json(url, body, timeout=30.0):
    """POST one JSON object, return (status, parsed body or None)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        return e.code, _try_parse(raw)
    except Exception:
        return None, None
    return 200, _try_parse(raw)


def _try_parse(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def log_err(msg):
    sys.stderr.write("werewolf-agent: " + msg + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# statement parsing: claims, accusations, defenses in other players' speeches
# ---------------------------------------------------------------------------

_SEAT_RE = re.compile(r"\bseat\s+(\d{1,2})\b")


def _seats_mentioned(text, name_to_seat):
    """Seats mentioned in a statement, via 'seat N' or a player's name."""
    out = []
    for m in _SEAT_RE.finditer(text):
        try:
            out.append(int(m.group(1)))
        except ValueError:
            pass
    lowered = text.lower()
    for name, seat in name_to_seat.items():
        if re.search(r"\b" + re.escape(name.lower()) + r"\b", lowered):
            if seat not in out:
                out.append(seat)
    return out


def parse_speech(text, speaker, name_to_seat):
    """Return a list of parsed facts from one statement.

    Each fact is a dict: {"kind": "seer_claim"|"doctor_claim"|"wolf_check"|
    "clear_check"|"accuse"|"defend", "speaker": seat, "target": seat|None}
    """
    facts = []
    t = text.lower()
    mentioned = _seats_mentioned(text, name_to_seat)

    def has(*words):
        return any(w in t for w in words)

    # role claims by the speaker about themself
    if has("i am the seer", "i'm the seer", "i am seer", "i'm seer",
           "seer here", "i claim seer", "i'm your seer"):
        facts.append({"kind": "seer_claim", "speaker": speaker, "target": None})
    if has("i am the doctor", "i'm the doctor", "i am doctor",
           "i'm doctor", "doctor here", "i claim doctor"):
        facts.append({"kind": "doctor_claim", "speaker": speaker, "target": None})

    # check results: "seat X is a werewolf" / "X is a wolf" / "inspected X"
    # (a "(Name)" may sit between the seat and the verb)
    wolf_pat = re.compile(
        r"(seat\s+(\d{1,2})\s*(?:\([^)]*\)\s*)?|([a-z]+)\s+)"
        r"(is|was)\s+(a\s+)?(werewolf|wolf)\b")
    for m in wolf_pat.finditer(t):
        target = None
        if m.group(2):
            target = int(m.group(2))
        elif m.group(3) and m.group(3) in name_to_seat:
            target = name_to_seat[m.group(3)]
        if target is not None and target != speaker:
            facts.append({"kind": "wolf_check", "speaker": speaker,
                          "target": target})

    # "inspected seat X" / "checked seat X" (a check claim; wolf-ness decided
    # by the rest of the sentence)
    insp_pat = re.compile(r"(inspected|checked)\s+(seat\s+)?(\d{1,2})")
    for m in insp_pat.finditer(t):
        target = int(m.group(3))
        if target == speaker:
            continue
        if "werewolf" in t or "wolf" in t:
            if not any(f["kind"] == "wolf_check" and f["target"] == target
                       for f in facts):
                facts.append({"kind": "wolf_check", "speaker": speaker,
                              "target": target})
        elif ("not" in t and ("werewolf" in t or "wolf" in t)) or \
                "innocent" in t or "clear" in t or "villager" in t:
            facts.append({"kind": "clear_check", "speaker": speaker,
                          "target": target})

    # clear claims: "seat X is not a werewolf" / "X is innocent/clear"
    clear_pat = re.compile(
        r"(seat\s+(\d{1,2})\s*(?:\([^)]*\)\s*)?|([a-z]+)\s+)"
        r"(is|was)\s+not\s+(a\s+)?(werewolf|wolf)\b")
    for m in clear_pat.finditer(t):
        target = None
        if m.group(2):
            target = int(m.group(2))
        elif m.group(3) and m.group(3) in name_to_seat:
            target = name_to_seat[m.group(3)]
        if target is not None and target != speaker:
            facts.append({"kind": "clear_check", "speaker": speaker,
                          "target": target})

    # accusations: "vote X" / "suspect X" / "X is suspicious" / "X is the wolf"
    if has("vote ", "suspect", "suspicious", "wolfy", "vote for", "exile "):
        for target in mentioned:
            if target != speaker:
                facts.append({"kind": "accuse", "speaker": speaker,
                              "target": target})
    # defenses: "trust X" / "X is innocent" / "don't vote X"
    if has("i trust", "trust seat", "don't vote", "do not vote", "not a wolf",
           "is innocent", "is clear"):
        for target in mentioned:
            if target != speaker:
                facts.append({"kind": "defend", "speaker": speaker,
                              "target": target})
    return facts


# ---------------------------------------------------------------------------
# game state, rebuilt from the view on every decision
# ---------------------------------------------------------------------------

class GameState:
    """Everything the agent knows about one game, rebuilt from the view.

    The view's log holds every public statement, vote, death and exile;
    seerResults / wolfVotes / lastProtect hold the agent's private record.
    Nothing is carried in mutable state between decisions except the
    trial seed (decisions are pure functions of the view).
    """

    def __init__(self, game_no, view):
        self.game = game_no
        self.view = view
        self.day = view.get("day", 1)
        self.phase = view.get("phase", "night")
        you = view.get("you", {})
        self.me = you.get("seat")
        self.my_name = you.get("name", "?")
        self.role = you.get("role", "villager")
        self.alive = bool(you.get("alive", True))
        self.partner = you.get("partner")  # {"seat","name","alive"} or None
        self.players = {}
        for p in view.get("players", []):
            self.players[p["seat"]] = p
        self.name_to_seat = {
            p["name"].lower(): p["seat"] for p in view.get("players", [])
        }
        self.living = [s for s in sorted(self.players)
                       if self.players[s].get("alive")]
        self.dead = [s for s in sorted(self.players)
                     if not self.players[s].get("alive")]
        # role once revealed by death, else None
        self.revealed = {s: self.players[s].get("role")
                         for s in self.players if self.players[s].get("role")}
        self.log = view.get("log", [])
        self.outcome = view.get("outcome")
        self.seer_results = view.get("seerResults") or []
        self.wolf_votes = view.get("wolfVotes") or []
        self.last_protect = view.get("lastProtect")

        self.deaths = []        # (day, seat, role or None) night deaths
        self.speeches = []      # (day, seat, round, text)
        self.votes = []         # (day, voter, target or None)
        self.exiles = []        # (day, seat or None, role or None, tally)
        self.facts = []         # parsed speech facts, in log order
        self._parse_log()

    # -- log parsing ------------------------------------------------------
    def _parse_log(self):
        for e in self.log:
            kind = e.get("kind")
            if kind == "dawn":
                if e.get("died") is not None:
                    self.deaths.append((e.get("day"), e["died"], e.get("role")))
            elif kind == "speech":
                if e.get("text"):
                    self.speeches.append(
                        (e.get("day"), e["seat"], e.get("round"), e["text"]))
                    self.facts.extend(parse_speech(
                        e["text"], e["seat"], self.name_to_seat))
            elif kind == "vote":
                self.votes.append((e.get("day"), e["seat"], e.get("target")))
            elif kind == "exile":
                self.exiles.append((e.get("day"), e.get("seat"),
                                    e.get("role"), e.get("tally") or []))

    # -- derived views ----------------------------------------------------
    def name(self, seat):
        p = self.players.get(seat)
        return p["name"] if p else "seat %d" % seat

    def who(self, seat):
        return "seat %d (%s)" % (seat, self.name(seat))

    def votes_today(self):
        """Votes cast so far in the current day's voting, in order."""
        return [(v, t) for (d, v, t) in self.votes if d == self.day]

    def tally_today(self):
        counts = {}
        for (_v, t) in self.votes_today():
            if t is not None:
                counts[t] = counts.get(t, 0) + 1
        return counts

    def my_speeches_today(self):
        return [txt for (d, s, _r, txt) in self.speeches
                if d == self.day and s == self.me]

    def have_i_claimed_seer(self):
        """Have I already claimed to be the seer in any earlier speech?"""
        for (_d, s, _r, txt) in self.speeches:
            if s == self.me and ("i am the seer" in txt.lower()
                                 or "i'm the seer" in txt.lower()):
                return True
        return False

    def seer_claimants(self):
        """Seats that claimed to be the seer (any day), in claim order."""
        out = []
        for f in self.facts:
            if f["kind"] == "seer_claim" and f["speaker"] not in out:
                out.append(f["speaker"])
        return out

    def doctor_claimants(self):
        out = []
        for f in self.facts:
            if f["kind"] == "doctor_claim" and f["speaker"] not in out:
                out.append(f["speaker"])
        return out

    def wolf_checks(self):
        """(claimant, target) pairs where someone said target is a wolf."""
        return [(f["speaker"], f["target"]) for f in self.facts
                if f["kind"] == "wolf_check"]

    def clear_checks(self):
        return [(f["speaker"], f["target"]) for f in self.facts
                if f["kind"] == "clear_check"]

    def my_checked(self):
        """My own seer results: {seat: is_wolf}."""
        return {r["seat"]: bool(r["werewolf"]) for r in self.seer_results}

    def partner_vote_tonight(self):
        """My partner's kill pick for the current night, or None."""
        if not self.partner:
            return None
        for v in self.wolf_votes:
            if v.get("night") == self.day and v.get("seat") == self.partner["seat"]:
                return v.get("target")
        return None

    def i_voted_tonight(self):
        return any(v.get("night") == self.day and v.get("seat") == self.me
                   for v in self.wolf_votes)

    # -- suspicion --------------------------------------------------------
    def suspicion(self, include_self_actions=True):
        """Score every living seat (higher = more likely a werewolf).

        Returns (scores, confirmed_wolf, confirmed_clear). Scores are
        log-odds-ish evidence sums; confirmed sets come from the seer's own
        inspections and from checks that a revealed role has settled.
        """
        scores = {s: 0.0 for s in self.living if s != self.me}
        confirmed_wolf = set()
        confirmed_clear = set()

        # my own seer inspections settle seats
        for seat, is_wolf in self.my_checked().items():
            if is_wolf:
                confirmed_wolf.add(seat)
            else:
                confirmed_clear.add(seat)

        # checks in statements, settled by revealed roles when possible
        # (my own seat is never a candidate, so it is never scored)
        for (claimant, target) in self.wolf_checks():
            if claimant == self.me and not include_self_actions:
                continue
            if target in self.revealed:
                if self.revealed[target] == "werewolf":
                    # claimant called it right: probably the real seer
                    if claimant != self.me:
                        confirmed_clear.add(claimant)
                    if claimant in scores:
                        scores[claimant] -= 3.0
                else:
                    # claimant's "wolf" was a villager: the claim was a lie
                    if claimant in scores:
                        scores[claimant] += 4.0
            elif target in self.living and target != self.me:
                scores[target] = scores.get(target, 0.0) + 1.0
        for (claimant, target) in self.clear_checks():
            if claimant == self.me and not include_self_actions:
                continue
            if target in self.revealed:
                if self.revealed[target] == "werewolf":
                    # claimant cleared a wolf: lying or sloppy
                    if claimant in scores:
                        scores[claimant] += 2.0
                else:
                    if claimant in scores:
                        scores[claimant] -= 0.5

        # exile outcomes: who pushed a mislynch, who helped catch a wolf
        for (day, exiled, role, _tally) in self.exiles:
            if exiled is None:
                continue
            voters_for = [v for (d, v, t) in self.votes
                          if d == day and t == exiled]
            voters_that_day = [v for (d, v, t) in self.votes if d == day]
            if role == "werewolf":
                for v in voters_for:
                    if v in scores:
                        scores[v] -= 1.2
                # the partner is likely among the seats that voted for a
                # villager that day instead: they were pushing a mislynch
                # while a wolf sat on the block
                villager_targets = {t for (d, v, t) in self.votes
                                    if d == day and t is not None
                                    and t in self.revealed
                                    and self.revealed[t] != "werewolf"}
                for v in voters_that_day:
                    if v not in voters_for and v in scores:
                        voted_villager = any(
                            t in villager_targets
                            for (d, vv, t) in self.votes
                            if d == day and vv == v)
                        scores[v] += 0.8 if voted_villager else 0.3
            elif role in ("villager", "seer", "doctor"):
                for v in voters_for:
                    if v in scores:
                        scores[v] += 0.8  # pushed a villager out
                for v in voters_that_day:
                    if v not in voters_for and v in scores:
                        scores[v] += 0.15  # abstained / voted elsewhere
        # accusations, settled by revealed roles
        for f in self.facts:
            if f["kind"] != "accuse":
                continue
            accuser, target = f["speaker"], f["target"]
            if accuser == self.me and not include_self_actions:
                continue
            if target in self.revealed:
                if self.revealed[target] == "werewolf":
                    if accuser in scores:
                        scores[accuser] -= 0.8
                elif accuser in scores:
                    scores[accuser] += 0.4
            elif f["speaker"] != self.me:
                # an open accusation against a living seat: mild signal
                if target in scores:
                    scores[target] += 0.15
        # defending a revealed wolf is wolfy
        for f in self.facts:
            if f["kind"] != "defend":
                continue
            if f["target"] in self.revealed and \
                    self.revealed[f["target"]] == "werewolf":
                if f["speaker"] in scores:
                    scores[f["speaker"]] += 1.0
        # wolves vote together: a living seat that voted for the same
        # target as a confirmed wolf, on the same day, is more likely
        # the partner
        confirmed = {s for s, r in self.revealed.items()
                     if r == "werewolf"}
        for w in confirmed:
            for (d, v, t) in self.votes:
                if v == w and t is not None:
                    for (d2, v2, t2) in self.votes:
                        if d2 == d and v2 != w and v2 != self.me \
                                and t2 == t and v2 in scores:
                            scores[v2] += 0.2
        # a bare seer claim with no check attached is cheap; wolves fake it
        for claimant in self.seer_claimants():
            if claimant == self.me:
                continue
            checks = [t for (c, t) in self.wolf_checks() if c == claimant]
            if not checks:
                if claimant in scores:
                    scores[claimant] += 0.3
        # two seer claims: at most one is real; both carry some suspicion
        # unless one is already settled by a revealed role
        claimants = [c for c in self.seer_claimants()
                     if c in self.living and c != self.me]
        if len(claimants) >= 2:
            for c in claimants:
                if c not in confirmed_clear and c in scores:
                    scores[c] += 0.5
        # my own inspections, and any settled check, override the sums
        # (only seats that are candidates are ever touched)
        for seat in confirmed_wolf:
            if seat in scores:
                scores[seat] = 99.0
        for seat in confirmed_clear:
            if seat in scores:
                scores[seat] = -99.0
        return scores, confirmed_wolf, confirmed_clear

    def trust_rank(self):
        """Living non-wolf seats ordered most-trusted first, for the
        werewolf's kill choice and the doctor's protect choice.

        Trust = low suspicion + being a vocal, followed voice.
        """
        scores, _cw, _cc = self.suspicion(include_self_actions=False)
        speech_count = {}
        for (_d, s, _r, _t) in self.speeches:
            speech_count[s] = speech_count.get(s, 0) + 1
        # how often others followed this seat's accusations with a vote
        followed = {}
        for f in self.facts:
            if f["kind"] == "accuse":
                for (d, v, t) in self.votes:
                    if v != f["speaker"] and t == f["target"]:
                        followed[f["speaker"]] = followed.get(f["speaker"], 0) + 1
        def trust_key(seat):
            s = scores.get(seat, 0.0)
            return (-s, speech_count.get(seat, 0) + 2 * followed.get(seat, 0))
        cands = [s for s in self.living if s != self.me]
        cands.sort(key=lambda s: (trust_key(s), -s), reverse=True)
        return cands

    def accusers_of(self, target):
        """Distinct living seats that have accused a seat."""
        return sorted({f["speaker"] for f in self.facts
                       if f["kind"] == "accuse" and f["target"] == target
                       and f["speaker"] in self.living
                       and f["speaker"] != target})

    def heat_on_me(self):
        """How many distinct living seats have accused me."""
        return len(self.accusers_of(self.me))

    def top_suspect(self, exclude=None):
        """Most suspicious living seat (not me, not in exclude), or None."""
        exclude = set(exclude or []) | {self.me}
        scores, _cw, _cc = self.suspicion()
        cands = [s for s in scores if s not in exclude]
        if not cands:
            return None
        cands.sort(key=lambda s: (scores[s], -s), reverse=True)
        return cands[0]

    def sorted_suspects(self, exclude=None):
        exclude = set(exclude or []) | {self.me}
        scores, _cw, _cc = self.suspicion()
        cands = [s for s in scores if s not in exclude]
        cands.sort(key=lambda s: (scores[s], -s), reverse=True)
        return cands


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------

# A wolf fake-claims seer only when no one has claimed seer yet and the
# game is past day 1, and only when WOLF_FAKE_SEER is on. Tuned in the
# harness (see STRATEGY.md).
WOLF_FAKE_SEER_FROM_DAY = 2


def _reason_for(state, target):
    """A short evidence phrase for suspecting a seat, for statements."""
    # a settled seer check
    for (claimant, t) in state.wolf_checks():
        if t == target and claimant != state.me:
            return "%s said they inspected %s and found a werewolf" % (
                state.who(claimant), state.who(target))
    if state.my_checked().get(target):
        return "I inspected %s and they are a werewolf" % state.who(target)
    # voted for an exiled villager
    for (day, exiled, role, _tally) in state.exiles:
        if role in ("villager", "seer", "doctor"):
            voters = [v for (d, v, t) in state.votes
                      if d == day and t == exiled]
            if target in voters:
                return "%s voted to exile %s, who was a %s" % (
                    state.who(target), state.who(exiled), role)
        if role == "werewolf":
            voters = [v for (d, v, t) in state.votes
                      if d == day and t == exiled]
            if target not in voters and target in state.living:
                return "%s never voted for %s when they were exiled as a wolf" % (
                    state.who(target), state.who(exiled))
    # accused by others
    accusers = [f["speaker"] for f in state.facts
                if f["kind"] == "accuse" and f["target"] == target]
    if accusers:
        return "%s has been accused by %s" % (
            state.who(target), state.who(accusers[0]))
    return "%s's story doesn't add up" % state.who(target)


def _pick_template(state, templates):
    """Pick a statement template deterministically by day and round."""
    rnd = rng_for(state.game, "speak:%d:%d" % (state.day, _speak_index(state)))
    return rnd.choice(templates)


def _speak_index(state):
    """How many times I have spoken today so far."""
    return len(state.my_speeches_today())


def decide_speak(state, max_chars):
    """One statement (or a pass) for the debate."""
    me = state.me
    role = state.role
    scores, confirmed_wolf, confirmed_clear = state.suspicion()
    suspects = state.sorted_suspects()
    top = suspects[0] if suspects else None
    round_no = _speak_index(state) + 1
    my_checks = state.my_checked()
    wolf_found = [s for s, w in my_checks.items() if w]

    # -- seer: claim a found wolf, counter a false seer, or claim early --
    if role == "seer":
        if wolf_found:
            target = wolf_found[0]
            # claim on my first turn of the day; repeat the call later
            if round_no == 1:
                text = ("I am the seer. I inspected %s on night %d and they "
                        "are a werewolf. Vote %s today."
                        % (state.who(target),
                           _check_night(state, target), state.who(target)))
            else:
                text = ("I already said it: I am the seer, %s is a werewolf "
                        "from my inspection. Vote %s, please."
                        % (state.who(target), state.who(target)))
            return clip_statement(text, max_chars)
        # someone else claims seer and I am the seer: counter-claim
        others = [c for c in state.seer_claimants()
                  if c != me and c in state.living]
        if others:
            liar = others[0]
            text = ("No, I am the seer, and %s is lying about it. "
                    "My inspections so far found no werewolf; %s's claim "
                    "is a wolf's cover." % (state.who(liar), state.who(liar)))
            return clip_statement(text, max_chars)
        # claim early with what I have: a seer exiled on day 1 with its
        # inspections still secret is the worst outcome in the game, and
        # a day-1 claim is unopposed (wolves have not claimed seer yet).
        # The doctor protects claimed seers, so the claim is survivable.
        if not state.have_i_claimed_seer() and \
                (state.day == 1 and SEER_CLAIM_DAY1
                 or state.heat_on_me() >= 2):
            clears = [s for s, w in my_checks.items() if not w]
            if clears:
                cleared = ", ".join(state.who(s) for s in clears)
                text = ("I am the seer. I have not found a werewolf yet, "
                        "but I inspected %s and they are not werewolves. "
                        "I am not the wolf you are looking for."
                        % cleared)
                return clip_statement(text, max_chars)
            text = ("I am the seer and I have not found a werewolf yet. "
                    "Exiling me throws away our inspections.")
            return clip_statement(text, max_chars)
        # no wolf found yet: speak like a villager, do not claim
        if top is not None:
            text = _pick_template(state, [
                "No solid lead yet. %s looks the most off to me: %s. "
                "Let's hear from them before we vote."
                % (state.who(top), _reason_for(state, top)),
                "I am still weighing it. %s: %s. I will vote on the record, "
                "not on vibes." % (state.who(top), _reason_for(state, top)),
            ])
            return clip_statement(text, max_chars)
        return None  # pass

    # -- werewolf --
    if role == "werewolf":
        partner = state.partner["seat"] if state.partner else None
        # fake seer claim, only when nobody has claimed seer yet
        if WOLF_FAKE_SEER and state.day >= WOLF_FAKE_SEER_FROM_DAY and \
                not state.seer_claimants() and round_no == 1:
            # name the villager the village already suspects most
            cands = [s for s in suspects
                     if s != partner and s not in confirmed_clear]
            if cands:
                target = cands[0]
                text = ("I am the seer. I inspected %s last night and they "
                        "are a werewolf. That is the truth — vote %s."
                        % (state.who(target), state.who(target)))
                return clip_statement(text, max_chars)
        # push suspicion onto the village's top suspect (never partner)
        cands = [s for s in suspects if s != partner]
        target = cands[0] if cands else None
        if target is None:
            return None
        if round_no == 1:
            text = _pick_template(state, [
                "I agree %s is the lead: %s. I am voting %s."
                % (state.who(target), _reason_for(state, target),
                   state.who(target)),
                "Look at %s. %s. I think we exile %s today."
                % (state.who(target), _reason_for(state, target),
                   state.who(target)),
            ])
        else:
            text = _pick_template(state, [
                "Still %s for me: %s. Let's not split the vote."
                % (state.who(target), _reason_for(state, target)),
                "The case on %s has not changed: %s. Vote %s."
                % (state.who(target), _reason_for(state, target),
                   state.who(target)),
            ])
        return clip_statement(text, max_chars)

    # -- villager and doctor: accuse the top suspect, coordinate the vote --
    # if the village is turning on me, defend first: name the bandwagon's
    # starter and redirect to a real suspect instead of ignoring it
    if state.heat_on_me() >= 2 and round_no == 1:
        accusers = state.accusers_of(state.me)
        starter = accusers[0] if accusers else None
        others = [s for s in suspects if s != state.me]
        redirect = others[0] if others else None
        if redirect is not None:
            text = ("I am not a werewolf. %s started this with no "
                    "evidence at all, and everyone is just following. "
                    "Look at %s instead: %s."
                    % (state.who(starter) if starter else "Someone",
                       state.who(redirect), _reason_for(state, redirect)))
            return clip_statement(text, max_chars)
    if top is not None:
        if confirmed_wolf:
            wolf = sorted(confirmed_wolf)[0]
            text = ("We have a named werewolf: %s. %s. "
                    "Everyone vote %s today — no splitting."
                    % (state.who(wolf), _reason_for(state, wolf),
                       state.who(wolf)))
            return clip_statement(text, max_chars)
        if round_no == 1:
            if scores.get(top, 0.0) >= 1.0:
                # strong evidence: a direct wolf accusation, which the
                # village can rally on (and a wrong one costs credibility,
                # so it is only used with real evidence)
                text = ("%s is a werewolf: %s. Vote %s today."
                        % (state.who(top), _reason_for(state, top),
                           state.who(top)))
            else:
                text = _pick_template(state, [
                    "My lead is %s: %s. I am voting %s — join me if you agree."
                    % (state.who(top), _reason_for(state, top),
                       state.who(top)),
                    "%s is my top suspect: %s. Let's put our votes on %s "
                    "instead of scattering them."
                    % (state.who(top), _reason_for(state, top),
                       state.who(top)),
                ])
        else:
            text = _pick_template(state, [
                "Still %s: %s. Vote %s."
                % (state.who(top), _reason_for(state, top),
                   state.who(top)),
                "I have not heard anything that clears %s. %s — voting %s."
                % (state.who(top), _reason_for(state, top),
                   state.who(top)),
            ])
        return clip_statement(text, max_chars)
    # day 1, no evidence at all: a method statement, not a blind accusation
    if state.day == 1 and not state.speeches:
        return clip_statement(
            "No one has a real lead yet. Let's vote on voting records, not "
            "vibes — I will be watching who pushes whom.", max_chars)
    return None


def _check_night(state, target):
    for r in state.seer_results:
        if r["seat"] == target:
            return r["night"]
    return state.day - 1 if state.day > 1 else 1


def decide_vote(state, options):
    """Vote to exile one living seat (never self), or abstain."""
    partner = state.partner["seat"] if state.partner else None
    if state.role == "werewolf" and WOLF_VOTE == "abstain":
        return None
    # as a wolf, never vote my partner unless the partner is doomed anyway
    exclude = set()
    if state.role == "werewolf" and partner is not None:
        tally = state.tally_today()
        partner_votes = tally.get(partner, 0)
        living = len(state.living)
        # partner doomed if they already hold a strict plurality lead
        if partner_votes <= max(tally.values(), default=0) or \
                partner_votes < (living // 2):
            exclude.add(partner)
    suspects = state.sorted_suspects(exclude=exclude)
    if not suspects:
        return None
    # consolidate: if the current vote leader is among my top suspects,
    # vote them so the village does not split -- except as a wolf in
    # "offleader" mode, where piling onto a mislynched villager is the
    # easiest way to be spotted, so the wolf votes its top suspect that
    # is not the leader instead.
    tally = state.tally_today()
    leader = None
    if tally:
        leader = max(tally, key=lambda s: (tally[s], -s))
    if state.role == "werewolf" and WOLF_VOTE == "offleader":
        for cand in suspects:
            if cand != leader and cand in options:
                return cand
        return None
    if leader is not None and leader in suspects[:3] and leader != state.me:
        return leader
    # seer with a found wolf: always that wolf
    if state.role == "seer":
        for s, w in state.my_checked().items():
            if w and s in options:
                return s
    top = suspects[0]
    return top if top in options else None


def decide_kill(state, options):
    """The werewolves' night kill."""
    # second voter: match my partner's pick (it is in the view)
    partner_pick = state.partner_vote_tonight()
    if partner_pick is not None and partner_pick in options:
        return partner_pick
    # first (or only) voter: the seer-claimant first, else the most trusted
    # villager; never a seat the village already suspects (they may be
    # exiled for free) and never my partner.
    partner = state.partner["seat"] if state.partner else None
    claimants = [c for c in state.seer_claimants()
                 if c in options and c != partner]
    if claimants:
        return claimants[0]
    trusted = state.trust_rank()
    suspects = set(state.sorted_suspects()[:2])
    for seat in trusted:
        if seat in options and seat != partner and seat not in suspects:
            return seat
    # fallback: any living non-wolf, preferring the most trusted
    for seat in trusted:
        if seat in options and seat != partner:
            return seat
    if options:
        return options[0]
    return None


def decide_protect(state, options):
    """The doctor's night protection."""
    # protect a claimed seer first (the wolves' top target)
    claimants = [c for c in state.seer_claimants()
                 if c in options and c != state.me]
    if claimants:
        return claimants[0]
    # else the most trusted villager
    trusted = state.trust_rank()
    for seat in trusted:
        if seat in options:
            return seat
    if options:
        return options[0]
    return None


def decide_inspect(state, options):
    """The seer's night inspection: most suspicious uninspected seat."""
    checked = set(state.my_checked())
    suspects = [s for s in state.sorted_suspects() if s not in checked]
    if suspects:
        return suspects[0]
    # nothing suspicious left: an uninspected seat, seeded choice
    rest = [s for s in options if s not in checked]
    if rest:
        return rng_for(state.game, "inspect:%d" % state.day).choice(rest)
    if options:
        return rng_for(state.game, "inspect-any:%d" % state.day).choice(options)
    return None


def decide(state, pending):
    """Answer one pending decision: returns the /act body."""
    kind = pending["decision"]
    game = pending["game"]
    if kind == "speak":
        text = decide_speak(state, pending.get("max_chars") or MAX_CHARS)
        if text is None:
            return {"game": game, "decision": "speak", "pass": True}
        return {"game": game, "decision": "speak", "text": text}
    if kind == "vote":
        target = decide_vote(state, pending.get("options") or [])
        return {"game": game, "decision": "vote", "target": target}
    if kind == "kill":
        target = decide_kill(state, pending.get("options") or [])
        return {"game": game, "decision": "kill", "target": target}
    if kind == "protect":
        target = decide_protect(state, pending.get("options") or [])
        return {"game": game, "decision": "protect", "target": target}
    if kind == "inspect":
        target = decide_inspect(state, pending.get("options") or [])
        return {"game": game, "decision": "inspect", "target": target}
    # unknown decision kind: fall back to a pass/abstention shape
    return {"game": game, "decision": kind, "target": None}


def fallback_act(pending):
    """The pass/abstention for a decision."""
    if pending["decision"] == "speak":
        return {"game": pending["game"], "decision": "speak", "pass": True}
    return {"game": pending["game"], "decision": pending["decision"],
            "target": None}


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def run():
    if not ENV_URL:
        log_err("MAKEFUN_ENV_URL is not set")
        return 1
    # The brief is read only to honour the protocol (the view carries
    # everything the agent needs); a missing brief does not stop play.
    brief_file = os.environ.get("MAKEFUN_PROMPT_FILE") or ""
    if brief_file:
        try:
            with open(brief_file, "r", encoding="utf-8") as f:
                f.read()
        except Exception as e:
            log_err("could not read brief %s: %s" % (brief_file, e))

    decisions = 0
    acted = 0
    fallbacks = 0
    while True:
        status, obs = post_json(ENV_URL + "/observe", {}, timeout=30.0)
        if status is None or not isinstance(obs, dict):
            # env unreachable for a moment: wait and re-observe
            time.sleep(0.25)
            continue
        if obs.get("done"):
            break
        pending = obs.get("pending") or []
        if not pending:
            time.sleep(0.05)
            continue
        for p in pending:
            # one decision at a time, in the order the env lists them
            try:
                state = GameState(p["game"], p["view"])
                act = decide(state, p)
            except Exception as e:
                log_err("game %s decide failed: %s" % (p.get("game"), e))
                act = fallback_act(p)
            decisions += 1
            status2, _body = post_json(ENV_URL + "/act", act, timeout=30.0)
            if status2 == 200:
                acted += 1
            else:
                # refusal (400/409) or unreachable: fall back, then loop
                fallbacks += 1
                status3, _b = post_json(ENV_URL + "/act", fallback_act(p),
                                        timeout=30.0)
                if status3 == 200:
                    acted += 1
    sys.stderr.write(
        "werewolf-agent: done after %d decisions (%d acted, %d fallbacks)\n"
        % (decisions, acted, fallbacks))
    return 0


if __name__ == "__main__":
    sys.exit(run())
