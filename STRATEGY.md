# STRATEGY.md — Werewolf agent for make.fun `werewolf-agent-01`

## Approach in one paragraph

A pure heuristic agent (standard library only, no model calls). Its edge over
the house baseline is **memory**: it keeps a complete per-game record —
every statement, vote, death and night result — and reasons from that record
with a structured suspicion model, whereas the baseline (claude-opus-5-5,
asked plainly once per decision) has no memory and no strategy. The agent is
fully deterministic: every tie-break is a fixed rule (lowest seat, etc.) or a
seeded RNG taken from `MAKEFUN_TRIAL_SEED`; it never reads the wall clock.

## The suspicion model

`GameState` stores, per game: the players and their (revealed or claimed)
roles, every speech (day, seat, round, text), every vote (day, voter, target),
every death (day, seat, revealed role), the night-kill targets, the wolf
checks I have made as seer, and every parsed statement fact (wolf check,
clear check, seer claim, accusation, defense) extracted with `parse_speech`.

`suspicion()` scores every living seat (never my own seat) as a wolf
probability-ish weight:

| signal | weight |
|---|---|
| A seer check (mine) that a seat is a wolf | +inf — decisive |
| Seer/clear checks from other players that target a seat | uses the source's credibility (see below) |
| A seat that defended a seat later revealed as a wolf | +1.0 |
| A seat that voted for a villager on the day a wolf was exiled (pushing a mislynch while a wolf sat on the block) | +0.8 |
| A seat that voted elsewhere on the day a wolf was exiled | +0.3 |
| A seat that voted for the same target as a confirmed wolf, same day (wolves vote together) | +0.2 |
| A seat accused by two or more distinct speakers | +0.3 per accused seat (max +0.6) |
| A night kill: the victim's accuser/defender becomes less/more suspect | ∓0.3 / +0.4 |
| A seat that voted FOR an exiled wolf | −1.2 (wolves do not vote out their own) |
| A seat that voted FOR a villager who was exiled, later revealed | +0.8 per wrong vote |
| A seat that was cleared by a trusted seer check | −2.0 (trusted) |
| A seat that was killed at night and revealed a villager | −5.0 (dead, irrelevant) |
| Dead seats | removed from the ranking entirely |

A `suspicion()` call also *repairs* its own model when a check is found to be
wrong: the checking seat gets +2.0 (liar) and the "cleared" seat's score is
restored.

### Credibility of other players' claims

A wolf-check claim from another seat is weighted by the speaker's own
suspicion: a **trusted** speaker (score ≤ 0.05) makes the claim decisive
(the target becomes the only suspect); a **suspicious** speaker (score ≥ 0.6)
gets their claim inverted (the target is treated as cleared, the speaker
becomes the suspect); a normal speaker's claim adds +1.0 to the target. This
is what makes fake seer claims by wolves backfire on them, and real seer
claims by villagers worth following.

## Role play

**Seer.** Each night, inspect the most suspicious uninspected seat. Claim a
wolf check the next day, loudly and repeatedly, until the wolf is exiled. If
another seat claims seer first and I am the seer, counter-claim immediately
(their claim is a wolf's cover). I do **not** claim on day 1 with only a
clear check: measured over 720 harness games, that early claim cost more
than it gained — it makes the seer the night-2 target, and the seer dies
with its later inspections unmade. Instead the seer claims only when it has
a wolf check, or when the village is turning on it (≥ 2 distinct accusers)
and the claim would stop a mislynch. Clear checks are still shared once the
seer has claimed, as trust anchors ("seat X is not a werewolf").

**Doctor.** Protect the seer claimant (the highest-value villager) whenever
one is alive; otherwise protect the most-trusted villager (lowest suspicion,
not myself); never protect the same seat twice in a row; never protect a seat
that was killed the previous night if its protection failed. On day 1 (no
information yet) protect a random villager.

**Villager.** Vote the most suspicious seat; use statements to coordinate the
village. Direct wolf accusation ("seat X is a werewolf") only when the
evidence score is ≥ 1.0 — weaker evidence uses a soft lead ("seat X looks
the most off to me") so a wrong call does not destroy my credibility. If
another seat claims seer and names a wolf, follow that call. If the village
is turning on me (≥ 2 distinct accusers), defend: claim seer if I am one,
otherwise point at a checked-clear seat or the actual top suspect.

**Werewolf.** Blend in: vote with the village consensus when the target is a
villager (that is how a wolf helps exile villagers without looking guilty),
but never vote for my partner. Speak like a villager — soft leads, shared
suspicion — and push suspicion onto villagers, never onto the partner.
Fake-seer claim: only from day 2 onward (a day-1 claim is too easy to
counter), and only when the real seer's call is already on the table; a
fake claim after the real seer is dead is free. Night kill: follow my
partner's vote when I vote second (it is in the view/log); when I vote
first, kill the seer claimant or the most trusted villager.

## Voting

At vote time the agent votes for its top suspect, with two safeguards:

- **Vote with the village when the village is right.** If another seat
  claims a wolf check and I have no check of my own, I follow their call.
- **Do not waste the vote.** If my top suspect has no support and the
  current leader is a real suspect (their evidence score is within 0.5 of
  mine, or my pick has no votes), I consolidate on the leader so the village
  actually exiles someone instead of splitting 1-1-1.

## Determinism

- No wall clock, no unseeded randomness anywhere.
- Seeded RNG: `random.Random(int.from_bytes(hashlib.sha256(seed).digest()[:8], "little"))` where `seed` is `MAKEFUN_TRIAL_SEED` (default `"default"`); it is only used for the night-1 protect (no information yet).
- Fixed tie-breaks everywhere else: lowest seat wins.
- The agent never reads files outside the protocol and never calls a model.

## Tuning knobs (environment variables; defaults are the measured-best)

| env var | default | meaning |
|---|---|---|
| `WF_FAKE_SEER` | `1` | wolf fake-seer claims on/off |
| `WF_WOLF_VOTE` | `offleader` | wolf day-vote policy: `consolidate` / `offleader` / `abstain` |
| `WF_SEER_CLAIM_DAY1` | `0` | seer claims on day 1 with only a clear check |

Final defaults are set at the top of `main.py`.
