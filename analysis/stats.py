#!/usr/bin/env python3
"""
stats.py - E3 of the FGCS v2 protocol: bootstrap confidence intervals and
exact McNemar tests for cycle-level detection.

Conditions (any number, in order):
  live            live five-vote majority (the deployed system)
  first           live first vote (the live K = 1 reference)
  <replay-dir>    a replay; its cycles replace the live ones, every other
                  cycle keeps its live outcome (as in detection_metrics.py)

Confidence intervals. Cluster bootstrap over runs: within each scenario the
five runs are resampled with replacement, the cycle counts of the sampled
runs are pooled, and the metric is computed from the pooled counts
(10 000 resamples, fixed seed, percentile interval). The point estimate is
the same pooled-count metric on the observed runs. With five runs per
scenario the intervals are coarse and, if anything, too narrow.

McNemar. For each pair of conditions, the cycles on which exactly one of the
two is correct (b, c) give an exact two-sided binomial p-value; Holm's
adjustment across all pairs is printed alongside.

Baseline (--lr FILE; lr_predictions.tsv or xgb_predictions.tsv). A tab-separated file with header
  scenario  run_id  cycle_num  pred        (pred 1 = anomaly)
holding the baseline's out-of-fold prediction for every cycle it was
evaluated on. Each condition is then compared with it on exactly those
cycles.

Usage:
  python analysis/stats.py <data-dir> live first replay/<dir> ... [--rule-gt] [--lr FILE]
"""

import math
import random
import sys
from itertools import combinations
from pathlib import Path

from detection_metrics import ALL_NEG, ALL_POS, load, load_gt, truth, verdict

B = 10_000
SEED = 20260928
MIXED = ("S2_POD_CRASH", "S5_CASCADING_FAILURE")


def predictions(live, spec):
    if spec in ("live", "first"):
        return {k: verdict(r, spec == "first") == "ANOMALY_CONFIRMED"
                for k, r in live.items()}
    rep = load(spec)
    if not rep:
        sys.exit(f"No replay records in {spec}")
    out = {}
    for k, r in live.items():
        v = rep[k]["llm_verdict"].get("verdict") if k in rep else verdict(r, False)
        out[k] = v == "ANOMALY_CONFIRMED"
    return out


def counts(keys, pred, gt):
    c = [0, 0, 0, 0]                                  # tp fp fn tn
    for k in keys:
        p, g = pred[k], gt[k]
        c[0 if p and g else 1 if p else 2 if g else 3] += 1
    return c


def metrics(c):
    tp, fp, fn, tn = c
    nan = float("nan")
    return {"P": tp / (tp + fp) if tp + fp else nan,
            "R": tp / (tp + fn) if tp + fn else nan,
            "F1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else nan,
            "FPR": fp / (fp + tn) if fp + tn else nan}


def wanted(scen):
    if scen in ALL_POS:
        return ["R"]
    if scen in ALL_NEG:
        return ["FPR"]
    return ["P", "R", "F1"]


def pct(xs, q):
    xs = sorted(x for x in xs if x == x)
    if not xs:
        return float("nan")
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def bootstrap(live, pred, gt):
    runs = {}
    for k in live:
        runs.setdefault(k[0], {}).setdefault(k[1], []).append(k)
    per_run = {s: {r: counts(ks, pred, gt) for r, ks in rs.items()}
               for s, rs in runs.items()}
    rng = random.Random(SEED)
    obs, draws = {}, {}
    for s, pr in per_run.items():
        obs[s] = [sum(x) for x in zip(*pr.values())]
    obs["POOLED"] = [sum(x) for x in zip(*obs.values())]
    for _ in range(B):
        tot = [0, 0, 0, 0]
        for s, pr in sorted(per_run.items()):
            ids = list(pr)
            c = [0, 0, 0, 0]
            for _r in ids:
                c = [a + b for a, b in zip(c, pr[rng.choice(ids)])]
            tot = [a + b for a, b in zip(tot, c)]
            for m, v in metrics(c).items():
                draws.setdefault((s, m), []).append(v)
        for m, v in metrics(tot).items():
            draws.setdefault(("POOLED", m), []).append(v)
    out = {}
    for s, c in obs.items():
        ms = wanted(s) if s != "POOLED" else ["P", "R", "F1"]
        for m in ms:
            d = draws[(s, m)]
            out[(s, m)] = (metrics(c)[m], pct(d, 0.025), pct(d, 0.975))
    return out


def exact_mcnemar(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj, run = [0.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (len(ps) - rank) * ps[i]))
        adj[i] = run
    return adj


def main(argv):
    rule_gt = "--rule-gt" in argv
    lr_file = None
    if "--lr" in argv:
        lr_file = argv[argv.index("--lr") + 1]
        argv = [a for a in argv if a != lr_file]
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2:
        sys.exit(__doc__)
    data_dir, specs = args[0], args[1:]
    live = load(data_dir)
    if rule_gt:
        gt = {k: truth(k[0], r["context_object"] or {}) for k, r in live.items()}
    else:
        rec = load_gt()
        gt = {k: rec[k] for k in live}
    preds = {s: predictions(live, s) for s in specs}
    names = {s: Path(s).name if s not in ("live", "first") else s for s in specs}

    print(f"\nGround truth: {'rule-based (sensitivity)' if rule_gt else 'recorded labels'}"
          f"   bootstrap: {B} cluster resamples over runs, seed {SEED}\n")
    print("Point estimate from pooled counts [95% CI]\n")
    for s in specs:
        res = bootstrap(live, preds[s], gt)
        print(f"== {names[s]}")
        for (scen, m), (e, lo, hi) in sorted(res.items(),
                                            key=lambda x: (x[0][0] == "POOLED", x[0])):
            print(f"   {scen:<22}{m:<4}{e:6.3f}  [{lo:.3f}, {hi:.3f}]")
        print()

    tests = []
    for a, b in combinations(specs, 2):
        pa, pb = preds[a], preds[b]
        bb = sum(1 for k in live if pa[k] == gt[k] and pb[k] != gt[k])
        cc = sum(1 for k in live if pa[k] != gt[k] and pb[k] == gt[k])
        tests.append((f"{names[a]}  vs  {names[b]}", len(live), bb, cc))

    if lr_file:
        lr = {}
        lines = Path(lr_file).read_text(encoding="utf-8").splitlines()[1:]
        for line in lines:
            scen, run, cyc, p = line.split("\t")
            lr[(scen, int(run), int(cyc))] = p.strip() == "1"
        keys = [k for k in live if k in lr]
        lrc = counts(keys, lr, gt)
        bname = Path(lr_file).stem
        print(f"Baseline {bname} on its {len(keys)} cycles: "
              + "  ".join(f"{m}={v:.3f}" for m, v in metrics(lrc).items()))
        for s in specs:
            p = preds[s]
            c = counts(keys, p, gt)
            print(f"   {names[s]:<28} on the same cycles: "
                  + "  ".join(f"{m}={v:.3f}" for m, v in metrics(c).items()))
            bb = sum(1 for k in keys if p[k] == gt[k] and lr[k] != gt[k])
            cc = sum(1 for k in keys if p[k] != gt[k] and lr[k] == gt[k])
            tests.append((f"{names[s]}  vs  {bname}", len(keys), bb, cc))
        print()

    if tests:
        ps = [exact_mcnemar(b, c) for _, _, b, c in tests]
        adj = holm(ps)
        print("Exact McNemar on cycle-level correctness")
        print("(b = only the first is correct, c = only the second is correct)\n")
        print(f"{'Comparison':<62}{'cycles':>7}{'b':>5}{'c':>5}{'p':>9}{'p Holm':>9}")
        print("-" * 97)
        for (name, n, b, c), p, h in zip(tests, ps, adj):
            print(f"{name:<62}{n:>7}{b:>5}{c:>5}{p:>9.4f}{h:>9.4f}")


if __name__ == "__main__":
    main(sys.argv[1:])
