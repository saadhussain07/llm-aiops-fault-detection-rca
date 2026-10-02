"""
non_llm_baseline_v2.py

REWRITE of non_llm_baseline.py, correcting schema assumptions that
didn't match reality. The original version assumed fields like
"silent_services" (top-level), "avg_cpu"/"error_rate" per metric row,
and a generic log-pattern-only summary -- none of which exist in the
actual context_object produced by tonight's real pipeline runs.
Verified against real S1-S6 data before writing this:

  full_metrics[i] keys: service, cpu_mb, cpu_pct_of_limit,
    cpu_vs_baseline_ratio, memory_mb, anomaly_score, is_anomaly
    (some cycles also carry pod_age_seconds, recently_restarted,
    memory_pct_of_limit, memory_vs_baseline_ratio, trigger, depending
    on scenario)

  full_logs[i] keys: service, total_logs, error_count, top_pattern,
    is_anomaly

  total_anomalies: {metrics, traces, logs, total}

Addresses the gap named explicitly by TNSM Reviewer 2 (point 7) and
the Associate Editor: no non-LLM baseline was ever compared against
the LLM agent on the same fused context. This trains a simple
classifier (XGBoost if available, else logistic regression) on
real, ground-truth-labeled cycles from all 6 scenarios.

Usage:
    python non_llm_baseline_v2.py S1_for_baseline.jsonl S2_for_baseline.jsonl ... S6_for_baseline.jsonl
"""

import json
import sys
import numpy as np
import pandas as pd
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import precision_recall_fscore_support, classification_report
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

try:
    from xgboost import XGBClassifier
    HAS_XGB = True
except ImportError:
    HAS_XGB = False
    print("[non_llm_baseline_v2] xgboost not installed, falling back to "
          "LogisticRegression. `pip install xgboost --break-system-packages` "
          "for the tree-based baseline reviewers likely expect.")


FEATURE_COLUMNS = [
    "max_anomaly_score",
    "n_metric_anomalies",
    "n_log_anomalies",
    "n_trace_anomalies",
    "max_cpu_pct_of_limit",
    "max_cpu_vs_baseline_ratio",
    "max_error_count",
    "total_anomalies_count",
]

LABEL_COLUMN = "ground_truth_verdict"


def build_feature_row(context: dict, ground_truth_verdict: str) -> dict:
    """
    Convert one real fused-context object (the SAME object the LLM
    receives) into a flat feature row, using the ACTUAL schema
    confirmed from real pipeline output -- not assumed field names.
    """
    full_metrics = context.get("full_metrics", [])
    full_logs = context.get("full_logs", [])
    total_anoms = context.get("total_anomalies", {})

    scores = [m.get("anomaly_score", 0) or 0 for m in full_metrics]
    cpu_pcts = [m.get("cpu_pct_of_limit", 0) or 0 for m in full_metrics]
    cpu_ratios = [m.get("cpu_vs_baseline_ratio", 0) or 0 for m in full_metrics]
    error_counts = [l.get("error_count", 0) or 0 for l in full_logs]

    row = {
        "max_anomaly_score": max(scores) if scores else 0.0,
        "n_metric_anomalies": sum(1 for m in full_metrics if m.get("is_anomaly")),
        "n_log_anomalies": sum(1 for l in full_logs if l.get("is_anomaly")),
        "n_trace_anomalies": total_anoms.get("traces", 0) or 0,
        "max_cpu_pct_of_limit": max(cpu_pcts) if cpu_pcts else 0.0,
        "max_cpu_vs_baseline_ratio": max(cpu_ratios) if cpu_ratios else 0.0,
        "max_error_count": max(error_counts) if error_counts else 0,
        "total_anomalies_count": total_anoms.get("total", 0) or 0,
        LABEL_COLUMN: ground_truth_verdict,
    }
    return row


def load_dataset(jsonl_paths):
    rows = []
    skipped = 0
    for path in jsonl_paths:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                gt = obj.get("ground_truth_verdict")
                context = obj.get("context_object", {})
                if gt is None or not context:
                    skipped += 1
                    continue
                rows.append(build_feature_row(context, gt))
    if skipped:
        print(f"[non_llm_baseline_v2] Skipped {skipped} cycles missing "
              f"ground_truth_verdict or context_object.")
    return pd.DataFrame(rows)


def train_and_evaluate(df: pd.DataFrame, n_splits: int = 5, seed: int = 42):
    X = df[FEATURE_COLUMNS].values
    y = df[LABEL_COLUMN].values

    print(f"\nDataset: {len(df)} cycles")
    print(df[LABEL_COLUMN].value_counts().to_string())

    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    fold_metrics = []
    all_y_true, all_y_pred = [], []

    for fold, (train_idx, test_idx) in enumerate(skf.split(X, y)):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_test_s = scaler.transform(X_test)

        if HAS_XGB:
            # binary classification: ANOMALY_CONFIRMED vs NORMAL
            y_train_bin = (y_train == "ANOMALY_CONFIRMED").astype(int)
            clf = XGBClassifier(
                n_estimators=100, max_depth=4, learning_rate=0.1,
                random_state=seed, eval_metric="logloss"
            )
            clf.fit(X_train_s, y_train_bin)
            y_pred_bin = clf.predict(X_test_s)
            y_pred = np.where(y_pred_bin == 1, "ANOMALY_CONFIRMED", "NORMAL")
        else:
            clf = LogisticRegression(max_iter=1000, random_state=seed)
            clf.fit(X_train_s, y_train)
            y_pred = clf.predict(X_test_s)

        precision, recall, f1, _ = precision_recall_fscore_support(
            y_test, y_pred, average="macro", zero_division=0,
            labels=["ANOMALY_CONFIRMED", "NORMAL"]
        )
        fold_metrics.append({"fold": fold, "precision": precision, "recall": recall, "f1": f1})
        all_y_true.extend(y_test)
        all_y_pred.extend(y_pred)
        print(f"  fold {fold}: precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}")

    metrics_df = pd.DataFrame(fold_metrics)
    summary = {
        "precision_mean": round(metrics_df["precision"].mean(), 4),
        "precision_std": round(metrics_df["precision"].std(), 4),
        "recall_mean": round(metrics_df["recall"].mean(), 4),
        "recall_std": round(metrics_df["recall"].std(), 4),
        "f1_mean": round(metrics_df["f1"].mean(), 4),
        "f1_std": round(metrics_df["f1"].std(), 4),
        "classifier": "XGBClassifier" if HAS_XGB else "LogisticRegression",
        "n_samples": len(df),
        "n_folds": n_splits,
    }

    print("\n=== NON-LLM BASELINE SUMMARY (for Table III/IV comparison) ===")
    print(json.dumps(summary, indent=2))

    print("\n=== Full classification report (pooled across folds) ===")
    print(classification_report(all_y_true, all_y_pred, labels=["ANOMALY_CONFIRMED", "NORMAL"], zero_division=0))

    return summary, metrics_df


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python non_llm_baseline_v2.py <file1.jsonl> <file2.jsonl> ...")
        sys.exit(1)

    df = load_dataset(sys.argv[1:])
    if df.empty:
        print("No usable rows found -- check ground_truth_verdict/context_object presence.")
        sys.exit(1)

    train_and_evaluate(df)
