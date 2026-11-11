# REPORT — Werewolf agent, make.fun `werewolf-agent-01`

## What was built

`main.py` — a pure-heuristic Werewolf agent (Python 3.13, standard library
only, no model calls, no API key). It keeps full per-game memory (every
statement, vote, death, night result), maintains a suspicion score over the
living seats, and plays each role with a fixed, tested plan (see
`STRATEGY.md`). It is fully deterministic (seeded RNG from
`MAKEFUN_TRIAL_SEED`; fixed lowest-seat tie-breaks; no wall clock).

`engine.py` — a faithful port of the env's game rules (`game.ts`), including
ordered speaker turns, two debate rounds, night resolution order (kill →
protect → inspect), exile by plurality (tied vote = no exile), win checks
(parity for wolves, no wolves left for village, max day 10 = draw), and the
exact statement folding / max-chars rules.

`house.py` — heuristic house players standing in for the real house
(claude-haiku-5-5): per-seat priors, vote-record analysis, parse claims the
way a real reader would, speak/vote/kill/protect/inspect by simple rules,
with a small amount of seeded noise. They are the sparring partner, not the
target; the real house is an LLM and will read statements more subtly than
these regexes.

`simulate.py` — the harness: runs the same `/observe` + `/act` HTTP
protocol the real env uses (a tiny in-process HTTP shim), 10 games at once,
against the 30 practice deals from `practice/trial-0/1/2`, and reports the
score in the competition's own formula:
`score = round(1000 × (wolf win rate + village win rate) / 2)`.

`baseline.py` — a plain heuristic baseline for comparison: votes and acts
from the same decision shapes but with **no memory** (no statement parsing,
no vote tracking, no role plan) — the closest local stand-in for the house
baseline (a frontier model asked plainly, once per decision, no memory).

## Harness results

12 seeds × 90 games (the 30 practice deals from trial-0/1/2) = **1,080 games
per agent**, paired (same seed for both agents), same heuristic house
opponents. The defaults in `main.py` are the measured-best configuration
(`WF_FAKE_SEER=1`, `WF_WOLF_VOTE=offleader`, `WF_SEER_CLAIM_DAY1=0`).

| agent | score (mean) | wolf wins | village wins | seer | doctor | villager | vote accuracy |
|---|---|---|---|---|---|---|---|
| **main.py (defaults)** | **506.4** | 386/540 (71.5%) | 161/540 (29.8%) | 54/180 (30%) | 47/180 (26%) | 60/180 (33%) | 461 |
| baseline.py (no memory) | 432.3 | 352/540 (65.2%) | 115/540 (21.3%) | 28/180 (16%) | 34/180 (19%) | 53/180 (29%) | 340 |

Main wins **12 of 12 seeds** (per-seed scores: 500, 500, 489, 544, 489,
633, 511, 433, 456, 489, 544, 489 vs baseline 489, 389, 478, 422, 433,
544, 422, 400, 356, 378, 444, 433). The edge is largest on the village
side (+46 games), and the seer's win rate nearly doubles (30% vs 16%) —
the seer's wolf checks, claimed loudly and followed up, are the single
biggest contributor. Vote accuracy (share of my agent's day-votes that name
a true wolf, in per-mille) is 461 vs the baseline's 340: the suspicion
model names wolves far more often.

### Tuning table (each cell = mean score, paired seeds, current house)

| configuration | score |
|---|---|
| fake seer ON, wolf vote `offleader`, seer quiet day 1 (**final**) | **512.4** (8 seeds) / **506.4** (12 seeds) |
| fake seer OFF, `offleader`, seer quiet day 1 | 501.4 (8 seeds) / 494.5 (12 seeds) |
| fake seer OFF, `consolidate`, seer quiet day 1 | 493.1 (8 seeds) |
| fake seer OFF, `offleader`, seer claims day 1 | 484.8 (8 seeds) |
| baseline (no memory) | 447.1 (8 seeds) / 432.3 (12 seeds) |

`vote_accuracy` is measured over the games in which the agent votes; it is
not part of the competition score, it is a diagnostic of the deduction
model.

## Reproducing the numbers

`results-final.json` is the canonical run (pure defaults, seed
`harness-seed-1`, 90 games). The tuning scripts `run_matrix.py`,
`run_ab.py`, `run_ab2.py`, `run_final.py`, `run_final2.py` rerun the
paired multi-seed comparisons above (each takes ~2–3 minutes): e.g.
`python3 run_final2.py` reproduces the 12-seed main-vs-baseline table.

## What the tuning found

- **The wolves' biggest tell is piling onto a mislynch.** In the first
  runs, every lost wolf game had both wolves exiled by day 2–4, because a
  wolf that votes for the same villager the village is already exiling is
  trivially identifiable by vote tracking. The fix (vote `offleader`: my
  top suspect, never the current vote leader) was the single biggest wolf
  improvement.
- **Fake-seer claims backfire when the real seer has already spoken, but
  pay when the seer is still quiet.** With the seer claiming on day 1, a
  fake claim was a net loss; with the seer staying quiet until it has a
  wolf check, the fake claim (from day 2) wins the wolf side a few more
  games.
- **Claiming seer on day 1 with only a clear check costs more than it
  gains.** It makes the seer the night-2 target; the seer dies with its
  later inspections unmade.
- **The village side is the hard side.** Even with much better vote
  accuracy than the baseline, the village wins only ~30% of games: it has
  to exile both wolves before parity, and once the seer is dead the village
  has no reliable information source. Finding the *second* wolf (from the
  first wolf's vote record) is the weakest link.
- **House-player tuning is a moving target.** The harness house's herding
  and prior strength moved scores by ±40 between variants; conclusions were
  only trusted after being confirmed across 8–12 seeds (paired, same seed
  for both sides).

## Notable games

- A seer game in which the agent found a wolf on night 1, claimed it, and
  the village exiled the wolf — but the agent (seer) was then killed at
  night and the village could not find the second wolf. The seer's
  information dies with it; protecting the seer after a claim is the
  village's most valuable move, and it is a *house* move the agent cannot
  control.
- A wolf game in which the agent's fake-seer claim (day 2) split the village
  into a counter-claim standoff and the village exiled a villager instead
  of either wolf.

## Known weaknesses

- The night-1 inspect is arbitrary (all scores are 0, so the lowest seat is
  inspected) — there is no information yet, so no choice is better, but it
  means the first check is a 2-in-7 coin flip.
- The village cannot force the house players to follow a claim; if the
  house (or the real LLMs) decide not to vote for a claimed wolf, the
  agent's information is wasted.
- The seer is a night target after claiming; survival depends on the
  doctor's protection, which the agent controls only when *it* is the
  doctor.
- The harness house is a heuristic stand-in, not an LLM: it reads claims by
  regex, not by understanding, and its priors/noise are arbitrary. Results
  against the real haiku house players will differ; the direction of the
  edge (memory + deduction vs no memory) is what transfers, not the exact
  numbers.
- Max day 10 draws: a game that reaches day 10 without a win is a draw
  (0 wins for both sides in the score formula's per-game count). Long games
  are rare.
