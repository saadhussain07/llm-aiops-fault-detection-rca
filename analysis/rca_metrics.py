#!/usr/bin/env python3
"""
rca_metrics.py - root-cause attribution, live or with a replay, by fixed
mechanical criteria (FGCS v2 protocol, Amendment 2).

Unit. The first ANOMALY_CONFIRMED cycle of each run (the source study's
unit). A run with no confirmed cycle counts as a miss, so every scenario
has denominator 5. The same criteria are also reported over all confirmed
cycles.

Criteria, applied to the verdict's `root_cause` text after Unicode
hyphens (U+2010-2015, U+2212) are normalised to "-":
  listed   the expected service appears in `affected_services`
           (the permissive criterion of the source repository)
  service  the FIRST service of the deployment named in `root_cause`
           is the expected service
  class    the FIRST fault-class term found in `root_cause` belongs to
           the expected fault class (lexicon below)
  both     service and class

With a replay directory, replayed cycles replace the live ones; every
other cycle keeps its live outcome.

Usage:
  python analysis/rca_metrics.py <data-dir> [<replay-dir>] [--show]

  --show   print, per run, the first confirmed cycle, the service and
           class the rule extracted, and the start of the narrative
"""

import json
import re
import sys
from pathlib import Path

SERVICES = ["frontend", "cartservice", "currencyservice",
            "paymentservice", "productcatalogservice", "redis-cart"]

# Fault-class lexicon. Fixed in Amendment 2 after reading the live
# narratives and before any replayed narrative was scored.
CLASS_TERMS = {
    "resource":   [r"\bcpu\b", r"throttl", r"\bmemory\b", r"memory[- ]pressure",
                   r"\bleak", r"\bheap\b", r"resource exhaustion"],
    "crash":      [r"crash", r"restart", r"\bterminated\b", r"\bkilled\b",
                   r"oomkill"],
    "network":    [r"\bnetwork\b", r"\blatency\b", r"\bdelay", r"netem",
                   r"packet"],
    "dependency": [r"\bredis\b", r"dependenc", r"connection (?:reset|refused|lost)",
                   r"unreachable"],
}
HYPHENS = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
SVC_RE = {s: re.compile(rf"(?<![\w-]){re.escape(s)}(?![\w-])", re.I)
          for s in SERVICES}
CLS_RE = {c: [re.compile(t, re.I) for t in ts] for c, ts in CLASS_TERMS.items()}


def norm(text):
    return (text or "").translate(HYPHENS)


def first_service(text):
    text = norm(text)
    hits = [(m.start(), s) for s, rx in SVC_RE.items()
            for m in [rx.search(text)] if m]
    return min(hits)[1] if hits else None


def first_class(text):
    text = norm(text)
    hits = [(m.start(), c) for c, rxs in CLS_RE.items() for rx in rxs
            for m in [rx.search(text)] if m]
    return min(hits)[1] if hits else None


def load(d):
    recs = {}
    for path in sorted(Path(d).glob("S*.jsonl")):
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if line:
                r = json.loads(line)
                scen = r.get("scenario") or path.stem.upper()
                recs[(scen, r["run_id"], r["cycle_num"])] = r
    return recs


def score(rec, v):
    exp_s = rec["expected_root_cause_service"]
    exp_c = rec["expected_fault_class"]
    rc = v.get("root_cause", "")
    s, c = first_service(rc), first_class(rc)
    ok = {"listed": exp_s in (v.get("affected_services") or []),
          "service": s == exp_s,
          "class": c == exp_c}
    ok["both"] = ok["service"] and ok["class"]
    return ok, s, c


def main(data_dir, replay_dir=None, show=False):
    live = load(data_dir)
    rep = load(replay_dir) if replay_dir else {}
    verdicts = {}
    for k, rec in live.items():
        v = rep[k]["llm_verdict"] if k in rep else rec.get("llm_verdict")
        verdicts[k] = v

    scen_runs = {}
    for (scen, run, cyc) in live:
        scen_runs.setdefault(scen, set()).add(run)

    src = replay_dir or "live (majority verdict record)"
    print(f"\nRoot-cause attribution: {src}   (replayed cycles substituted: "
          f"{sum(1 for k in live if k in rep)})\n")
    keys = ["listed", "service", "class", "both"]
    hdr = f"{'Scenario':<22}{'confirmed':>10}" + "".join(f"{k:>10}" for k in keys)
    print("First confirmed cycle per run (misses count against)")
    print(hdr); print("-" * len(hdr))
    tot = dict.fromkeys(keys + ["n", "conf"], 0)
    allc = {}
    for scen in sorted(scen_runs):
        if live[next(k for k in live if k[0] == scen)]["expected_root_cause_service"] == "none":
            continue
        cnt = dict.fromkeys(keys + ["conf"], 0)
        n = len(scen_runs[scen])
        for run in sorted(scen_runs[scen]):
            cyc = sorted(k for k in live if k[0] == scen and k[1] == run)
            conf = [k for k in cyc if verdicts[k] and
                    verdicts[k].get("verdict") == "ANOMALY_CONFIRMED"]
            for k in conf:                      # all confirmed cycles
                ok, _, _ = score(live[k], verdicts[k])
                a = allc.setdefault(scen, dict.fromkeys(keys + ["n"], 0))
                a["n"] += 1
                for kk in keys:
                    a[kk] += ok[kk]
            if not conf:
                if show:
                    print(f"   {scen} run {run}: no confirmed cycle")
                continue
            k = conf[0]
            ok, s, c = score(live[k], verdicts[k])
            cnt["conf"] += 1
            for kk in keys:
                cnt[kk] += ok[kk]
            if show:
                rc = norm(verdicts[k].get("root_cause", ""))[:160].replace("\n", " ")
                print(f"   {scen} run {run} cycle {k[2]}: service={s} class={c}"
                      f"  {'OK' if ok['both'] else '--'}\n      {rc}")
        print(f"{scen:<22}{cnt['conf']:>6}/{n:<3}"
              + "".join(f"{str(cnt[kk]) + '/' + str(n):>10}" for kk in keys))
        for kk in keys + ["conf"]:
            tot[kk] += cnt[kk]
        tot["n"] += n
    print("-" * len(hdr))
    print(f"{'TOTAL':<22}{tot['conf']:>6}/{tot['n']:<3}"
          + "".join(f"{str(tot[kk]) + '/' + str(tot['n']):>10}" for kk in keys))

    print("\nAll confirmed cycles")
    hdr2 = f"{'Scenario':<22}{'n':>10}" + "".join(f"{k:>10}" for k in keys)
    print(hdr2); print("-" * len(hdr2))
    t2 = dict.fromkeys(keys + ["n"], 0)
    for scen, a in sorted(allc.items()):
        print(f"{scen:<22}{a['n']:>10}"
              + "".join(f"{a[kk] / a['n']:>10.2f}" for kk in keys))
        for kk in keys + ["n"]:
            t2[kk] += a[kk]
    if t2["n"]:
        print("-" * len(hdr2))
        print(f"{'POOLED':<22}{t2['n']:>10}"
              + "".join(f"{t2[kk] / t2['n']:>10.2f}" for kk in keys))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    main(args[0], args[1] if len(args) > 1 else None, "--show" in sys.argv)
