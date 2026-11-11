#!/usr/bin/env python3
"""Diagnose wolf losses from a simulate.py results file: for each wolf-role
game the agent lost, show when/how it ended and whether the agent (or its
partner) was exiled, plus which seats the agent accused/killed.
"""
import json
import sys


def main(path):
    data = json.load(open(path))
    games = [g for trial in data["games"] for g in trial]
    wolf_games = [g for g in games if g["role"] == "werewolf"]
    lost = [g for g in wolf_games if not g["won"]]
    print("wolf games: %d, won %d, lost %d" % (len(wolf_games),
          len(wolf_games) - len(lost), len(lost)))
    exiled_me = 0
    exiled_partner = 0
    for g in lost:
        me = g["entry_seat"]
        partner = [s for s, r in enumerate(g["roles"], 1)
                   if r == "werewolf" and s != me]
        partner = partner[0] if partner else None
        exiles = [(e["day"], e["seat"], e["role"]) for e in g["log"]
                  if e.get("kind") == "exile" and e.get("seat") is not None]
        my_exile = [e for e in exiles if e[1] == me]
        p_exile = [e for e in exiles if e[1] == partner]
        if my_exile:
            exiled_me += 1
        if p_exile:
            exiled_partner += 1
        print("trial game %d: %s day %s | exiles %s | me=%d partner=%d"
              % (g["game"], g["winner"], g["day"], exiles, me, partner))
    print("lost games where I was exiled: %d/%d; partner exiled: %d/%d"
          % (exiled_me, len(lost), exiled_partner, len(lost)))


if __name__ == "__main__":
    main(sys.argv[1])
