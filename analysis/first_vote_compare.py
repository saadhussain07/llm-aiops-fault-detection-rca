#!/usr/bin/env python3
"""
first_vote_compare.py - compare a replay against the live source verdicts.

The live records store every vote. Their FIRST vote is a live single-sample
(K = 1) verdict, so a K = 1 replay can be compared with it directly, as
well as with the live five-vote majority. Cycles are split by whether the
recorded context carries a NOTE line, so that a NOTE-stripped replay can be
read against its own controls.

Usage:
  python analysis/first_vote_compare.py <data-dir> <replay-dir>

  e.g. python analysis/first_vote_compare.py ../fgcs-aiops-llm-agent/data \
           replay/openai__gpt-oss-120b__nonote__k1
"""

import collections
import json
import sys
from pathlib import Path


def load(d, live):
    recs = {}
    for path in sorted(Path(d).glob("S*.jsonl")):
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if live and not r.get("llm_verdict"):
                continue
            scen = r.get("scenario") or path.stem.upper()
            recs[(scen, r["run_id"], r["cycle_num"])] = r
    return recs


def notes_in(ctx):
    return sum(1 for l in ctx.get("summary_for_llm", "").split("\n")
               if l.lstrip().startswith("NOTE:"))


def main(data_dir, replay_dir):
    live = load(data_dir, live=True)
    rep = load(replay_dir, live=False)
    if not rep:
        sys.exit(f"No replay records in {replay_dir}")

    rows = []
    for k, r in sorted(rep.items()):
        src = live.get(k)
        if src is None:
            continue
        lv = src["llm_verdict"]
        votes = lv.get("all_votes") or []
        rows.append({
            "key": k,
            "notes": notes_in(src["context_object"]),
            "live_first": votes[0] if votes else None,
            "live_major": lv.get("verdict"),
            "replay": r["llm_verdict"].get("verdict"),
        })

    by = collections.defaultdict(lambda: {"n": 0, "note": 0, "note_chg": 0,
                                          "ctrl": 0, "ctrl_chg": 0, "maj_chg": 0})
    pairs = collections.Counter()
    for x in rows:
        b = by[x["key"][0]]
        b["n"] += 1
        changed = x["replay"] != x["live_first"]
        if x["replay"] != x["live_major"]:
            b["maj_chg"] += 1
        if x["notes"]:
            b["note"] += 1
            b["note_chg"] += changed
        else:
            b["ctrl"] += 1
            b["ctrl_chg"] += changed
        if changed:
            pairs[(x["live_first"], x["replay"])] += 1

    print(f"\nReplay: {replay_dir}\nAgainst live first vote (K=1) and live majority (K=5)\n")
    hdr = (f"{'Scenario':<22}{'n':>5}{'NOTE':>7}{'NOTE chg':>10}"
           f"{'ctrl':>7}{'ctrl chg':>10}{'vs K=5 chg':>12}")
    print(hdr)
    print("-" * len(hdr))
    tot = collections.Counter()
    for scen in sorted(by):
        b = by[scen]
        tot.update(b)
        print(f"{scen:<22}{b['n']:>5}{b['note']:>7}{b['note_chg']:>10}"
              f"{b['ctrl']:>7}{b['ctrl_chg']:>10}{b['maj_chg']:>12}")
    print("-" * len(hdr))
    print(f"{'TOTAL':<22}{tot['n']:>5}{tot['note']:>7}{tot['note_chg']:>10}"
          f"{tot['ctrl']:>7}{tot['ctrl_chg']:>10}{tot['maj_chg']:>12}")
    if pairs:
        print("\nChanges vs live first vote (live -> replay):")
        for (a, b), c in pairs.most_common():
            print(f"   {str(a):<20} -> {str(b):<20} {c}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
