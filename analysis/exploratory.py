#!/usr/bin/env python3
"""
exploratory.py - the analyses the manuscript labels exploratory (not named
in the protocol or its amendments).

  1. Fisher exact tests: verdict changes on annotated vs unannotated cycles
  2. S1 CPU-limit share on annotated vs unannotated cycles (signal confound)
  3. Control cycles whose two-cycle history carries NOTE text
  4. S4: first-confirmed-cycle attributions per run and condition, with the
     annotations and trace evidence of that cycle
  5. S4: network attributions over all confirmed cycles, per run
  6. S5: positive-labelled cycles without any Redis evidence
  7. Record facts quoted in the text: collection dates, usable votes,
     cycle spacing, S4 frontend pod age at cycle 1, E1 S2 annotated
     cycles still confirmed

Usage:
  python analysis/exploratory.py <data-dir> replay/<E1> replay/<20b> replay/<qwen>
"""

import json
import re
import statistics as st
import sys
from pathlib import Path

from scipy.stats import fisher_exact

from rca_metrics import first_class, first_service, load


def notes(rec):
    return [l for l in rec["context_object"]["summary_for_llm"].split("\n")
            if l.lstrip().startswith("NOTE:")]


def kind(line):
    if "started" in line:
        return "restart"
    if "memory" in line:
        return "memory"
    if "CPU limit" in line:
        return "cpu"
    return "dependency"


def verdict(conds, name, k, live):
    rep = conds[name]
    if k in rep:
        return rep[k]["llm_verdict"]
    return live[k].get("llm_verdict")


def main(data_dir, e1, m20, qwen):
    live = load(data_dir)
    inv = {k: r for k, r in live.items() if r.get("llm_verdict")}
    conds = {"live": {}, "E1": load(e1), "20b": load(m20), "qwen": load(qwen)}

    print("1. Fisher exact, changed vs unchanged, NOTE vs control")
    for name in ("E1", "20b", "qwen"):
        rep = conds[name]
        tab = {True: [0, 0], False: [0, 0]}
        for k in rep:
            first = inv[k]["llm_verdict"]["all_votes"][0]
            ch = rep[k]["llm_verdict"]["verdict"] != first
            tab[bool(notes(inv[k]))][0 if ch else 1] += 1
        p = fisher_exact([tab[True], tab[False]])[1]
        print(f"   {name:<5} NOTE {tab[True][0]}/{sum(tab[True])}  "
              f"control {tab[False][0]}/{sum(tab[False])}  p={p:.2e}")

    print("\n2. S1: cartservice CPU share of limit, annotated vs not")
    for flag in (True, False):
        xs = [m["cpu_pct_of_limit"] for k, r in inv.items()
              if k[0] == "S1_CPU_STRESS" and bool(notes(r)) == flag
              for m in r["context_object"]["full_metrics"]
              if m["service"] == "cartservice"]
        print(f"   {'NOTE' if flag else 'control':<8} n={len(xs)} "
              f"median={st.median(xs):.1f}% max={max(xs):.1f}%")

    print("\n3. Control cycles with NOTE text in the two-cycle history")
    # history = the two previous agent-invoking cycles of the same run
    by = {}
    for (s, run, c), r in sorted(inv.items()):
        earlier = sorted(x for x in inv if x[0] == s and x[1] == run and x[2] < c)
        prev = [inv[x] for x in earlier[-2:]]
        if not notes(r) and any(notes(p) for p in prev):
            by[s] = by.get(s, 0) + 1
    print("   ", by, "total", sum(by.values()))

    print("\n4. S4 first confirmed cycle per run")
    for run in range(1, 6):
        ks = sorted(k for k in inv if k[0] == "S4_NETWORK_LATENCY" and k[1] == run)
        row = []
        for name in conds:
            for k in ks:
                v = verdict(conds, name, k, live)
                if v and v["verdict"] == "ANOMALY_CONFIRMED":
                    rc = v.get("root_cause", "")
                    tr = inv[k]["context_object"]["anomaly_details"].get("trace_anomalies") or []
                    p99 = ",".join(f"{t['p99_latency_ms']:.0f}ms" for t in tr)
                    row.append(f"{name}: c{k[2]} {first_service(rc)}/{first_class(rc)} "
                               f"notes={[kind(n) for n in notes(inv[k])]} p99={p99}")
                    break
        print(f"   run {run}"); [print("     ", x) for x in row]

    print("\n5. S4 network attributions (frontend + network) over all confirmed cycles")
    for name in conds:
        per, tot, n = {}, 0, 0
        for k in inv:
            if k[0] != "S4_NETWORK_LATENCY":
                continue
            v = verdict(conds, name, k, live)
            if v and v["verdict"] == "ANOMALY_CONFIRMED":
                n += 1
                rc = v.get("root_cause", "")
                if first_service(rc) == "frontend" and first_class(rc) == "network":
                    tot += 1
                    per[k[1]] = per.get(k[1], 0) + 1
        print(f"   {name:<5} {tot}/{n} confirmed cycles; by run {dict(sorted(per.items()))}")

    print("\n6. S5 positive-labelled cycles without Redis evidence")
    for k, r in sorted(inv.items()):
        if k[0] != "S5_CASCADING_FAILURE":
            continue
        s = r["context_object"]["summary_for_llm"].lower()
        s = s.replace("specifically references redis", "")
        if "redis" not in s:
            vs = {n: (verdict(conds, n, k, live) or {}).get("verdict", "-")[:3]
                  for n in conds}
            print(f"   run {k[1]} cycle {k[2]}: {[kind(n) for n in notes(r)]} {vs}")

    print("\n7. Record facts")
    from datetime import datetime
    ts = sorted(r["logged_at"] for r in live.values())
    print(f"   collected {ts[0][:16]} .. {ts[-1][:16]}")
    votes = {}
    for r in inv.values():
        u = r["llm_verdict"]["votes_usable"]
        votes[u] = votes.get(u, 0) + 1
    print(f"   usable votes per invocation: {dict(sorted(votes.items()))}")
    gaps = []
    runs = {}
    for k, r in live.items():
        runs.setdefault(k[:2], []).append((k[2], datetime.fromisoformat(r["logged_at"])))
    for cyc in runs.values():
        cyc.sort()
        gaps += [(b[1] - a[1]).total_seconds() for a, b in zip(cyc, cyc[1:])]
    print(f"   gap between logged cycles: min {min(gaps):.0f}s "
          f"median {st.median(gaps):.0f}s max {max(gaps):.0f}s")
    for run in range(1, 6):
        r = live[("S4_NETWORK_LATENCY", run, 1)]
        fe = [m for m in r["context_object"]["full_metrics"] if m["service"] == "frontend"][0]
        print(f"   S4 run {run} cycle 1: frontend pod age {fe.get('pod_age_seconds'):.0f}s")
    e1 = conds["E1"]
    kept = sum(1 for k in e1 if k[0] == "S2_POD_CRASH" and notes(inv[k])
               and e1[k]["llm_verdict"]["verdict"] == "ANOMALY_CONFIRMED")
    print(f"   E1: annotated S2 cycles still confirmed: {kept}/20")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
