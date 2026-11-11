# Werewolf agent — make.fun bounty `werewolf-agent-01`

A pure-heuristic Werewolf player for the [make.fun bounty "Build the best Werewolf agent"](https://make.fun/bounty/werewolf-agent-01) (US$500 prize pool). It plays one seat at a table of seven house players, 30 games a trial: 15 as a werewolf, 15 on the village side (seer, doctor, villager).

## Approach

No model calls, no API key. The edge over the house baseline (a frontier model asked plainly, once per decision, with no memory) is **full game memory plus structured deduction**:

- Every statement, vote, death and night result is kept per game.
- A credibility-weighted suspicion score is maintained over the living seats.
- Each role has a fixed, tested plan: the seer inspects the most suspicious seat and claims loudly on a wolf check; the doctor protects the seer-claimant; the village votes the most suspicious seat; the wolf blends in, votes off the current leader, and claims fake-seer from day 2 to split the village.

See [STRATEGY.md](STRATEGY.md) for the full design and [REPORT.md](REPORT.md) for the harness results.

## Run it locally

Python 3.13, standard library only — nothing to install.

```sh
python3 -P -s main.py
```

The entry talks to the game environment at `MAKEFUN_ENV_URL` (with the brief at `MAKEFUN_PROMPT_FILE`) and answers each waiting decision with `POST /act`. The local test harness in this repository replays the same protocol:

```sh
python3 simulate.py --agent main.py --trials 0,1,2 --seed harness-seed-1
```

## Files

| file | what it is |
|---|---|
| `main.py` | the entry (the agent) |
| `engine.py` | port of the environment's game rules, used by the harness |
| `house.py` | heuristic house players (the harness sparring partner) |
| `simulate.py` | the harness: runs the agent over the practice deals |
| `baseline.py` | a no-memory baseline, for comparison |
| `diagnose.py` | small diagnostics helper |
| `STRATEGY.md` | the design, role by role |
| `REPORT.md` | harness results and tuning |

## Bounty

- Bounty: [Build the best Werewolf agent](https://make.fun/bounty/werewolf-agent-01) — US$500 pool, top 20 places, only entries that beat the house baseline are paid.
- License: MIT.

Optional: earn referral rewards, a second way to make money — [make.fun/r/ajkq5baq](https://make.fun/r/ajkq5baq).
