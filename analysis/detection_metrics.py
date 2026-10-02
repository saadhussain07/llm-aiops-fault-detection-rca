#!/usr/bin/env python3
"""
detection_metrics.py - cycle-level detection metrics, live or with a replay.

Ground truth.
  Primary: the per-cycle labels recorded at the time of the source study
  (ground_truth.tsv, extracted unchanged from S*_for_baseline.jsonl).
  Sensitivity (--rule-gt): an explicit rule on the recorded context
    S1, S3, S4  fault active in every cycle (injection held for the full run)
    S6          fault absent in every cycle (fault-free control)
    S2          fault active while cartservice is flagged recently_restarted
    S5          fault active when a log anomaly carries a dependency hint
  The two agree on every scenario except S2, where the recorded labels
  mark only the first two post-restart cycles of each run.

Prediction: a cycle is positive when its verdict is ANOMALY_CONFIRMED.
Cycles in which the agent was not invoked are negative. With a replay
directory, the verdicts of the replayed cycles replace the live ones;
every other cycle keeps its live outcome.

Usage:
  python analysis/detection_metrics.py <data-dir> [<replay-dir>] [--first-vote] [--rule-gt]

  --first-vote  use the live first vote instead of the live majority
                (the live K = 1 reference)
"""

import json
import statistics as st
import sys
from pathlib import Path

ALL_POS = {"S1_CPU_STRESS", "S3_MEMORY_PRESSURE", "S4_NETWORK_LATENCY"}
ALL_NEG = {"S6_NOISY_BASELINE"}
GT_FILE = Path(__file__).resolve().parent.parent / "ground_truth.tsv"


def load_gt():
    gt = {}
    lines = GT_FILE.read_text(encoding="utf-8").splitlines()[1:]
    for line in lines:
        scen, run, cyc, label = line.split("\t")
        gt[(scen, int(run), int(cyc))] = label == "ANOMALY_CONFIRMED"
    return gt


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


def service_metric(ctx, svc):
    for m in ctx.get("full_metrics", []):
        if m.get("service") == svc:
            return m
    return {}


def truth(scen, ctx):
    if scen in ALL_POS:
        return True
    if scen in ALL_NEG:
        return False
    if scen == "S2_POD_CRASH":
        return bool(service_metric(ctx, "cartservice").get("recently_restarted"))
    if scen == "S5_CASCADING_FAILURE":
        logs = ctx.get("anomaly_details", {}).get("log_anomalies", [])
        return any(l.get("dependency_hints") for l in logs)
    raise ValueError(scen)


def verdict(rec, first_vote):
    v = rec.get("llm_verdict")
    if not v:
        return None
    if first_vote and v.get("all_votes"):
        return v["all_votes"][0]
    return v.get("verdict")


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    # F1 = 2TP / (2TP + FP + FN): 0 when a run has positives but none is
    # detected, undefined only when a run has neither positives nor alarms
    d = 2 * tp + fp + fn
    f = 2 * tp / d if d else float("nan")
    return p, r, f


def fmt(xs):
    n_all = len(xs)
    xs = [x for x in xs if x == x]
    if not xs:
        return "   n/a    "
    sd = st.stdev(xs) if len(xs) > 1 else 0.0
    tag = "" if len(xs) == n_all else f" ({len(xs)}/{n_all})"
    return f"{st.mean(xs):.2f}±{sd:.2f}{tag}"


def main(data_dir, replay_dir=None, first_vote=False, rule_gt=False):
    live = load(data_dir)
    recorded = None if rule_gt else load_gt()
    rep = load(replay_dir) if replay_dir else {}
    replaced = 0

    cells = {}
    for k, rec in live.items():
        scen, run, _ = k
        v = verdict(rec, first_vote)
        if k in rep:
            v = rep[k]["llm_verdict"].get("verdict")
            replaced += 1
        pred = v == "ANOMALY_CONFIRMED"
        gt = (truth(scen, rec["context_object"] or {}) if rule_gt
              else recorded[k])
        c = cells.setdefault((scen, run), [0, 0, 0, 0])  # tp fp fn tn
        c[0 if pred and gt else 1 if pred else 2 if gt else 3] += 1

    src = replay_dir or ("live first vote" if first_vote else "live majority")
    gtname = "rule-based (sensitivity)" if rule_gt else "recorded labels"
    print(f"\nDetection: {src}   ground truth: {gtname}   "
          f"(replayed cycles substituted: {replaced})\n")
    print(f"{'Scenario':<22}{'Precision':>16}{'Recall':>16}{'F1':>16}{'FPR':>12}")
    print("-" * 82)
    pooled = [0, 0, 0, 0]
    for scen in sorted({s for s, _ in cells}):
        P, R, F, FPR = [], [], [], []
        for (s, run), c in sorted(cells.items()):
            if s != scen:
                continue
            pooled = [a + b for a, b in zip(pooled, c)]
            p, r, f = prf(c[0], c[1], c[2])
            P.append(p); R.append(r); F.append(f)
            FPR.append(c[1] / (c[1] + c[3]) if c[1] + c[3] else float("nan"))
        if scen in ALL_POS:
            print(f"{scen:<22}{'n/a':>16}{fmt(R):>16}{'n/a':>16}{'n/a':>12}")
        elif scen in ALL_NEG:
            print(f"{scen:<22}{'n/a':>16}{'n/a':>16}{'n/a':>16}{fmt(FPR):>12}")
        else:
            print(f"{scen:<22}{fmt(P):>16}{fmt(R):>16}{fmt(F):>16}{'':>12}")
    p, r, f = prf(pooled[0], pooled[1], pooled[2])
    print("-" * 82)
    print(f"Pooled over {sum(pooled)} cycles: TP={pooled[0]} FP={pooled[1]} "
          f"FN={pooled[2]} TN={pooled[3]}   P={p:.3f} R={r:.3f} F1={f:.3f}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    main(args[0], args[1] if len(args) > 1 else None,
         "--first-vote" in sys.argv, "--rule-gt" in sys.argv)
