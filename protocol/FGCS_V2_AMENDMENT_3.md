# FGCS v2 protocol, Amendment 3: the non-LLM baseline

**Date:** 2026-09-28, before any E2 condition was compared with a baseline.

## What was found

The source study's baseline script is
`Layer 3/experiment_logs/non_llm_baseline_v2.py`, dated 2026-09-13 and
committed unchanged as `analysis/non_llm_baseline_v2.py`. It trains
XGBoost when the `xgboost` package is installed and logistic regression
when it is not. It is installed on the authors' machine.

Run unchanged on S1 and S3–S6 (270 cycles, 5-fold stratified CV, seed 42,
sklearn 1.8.0), it gives these fold-mean macro F1 scores:

- XGBoost: 0.996 ± 0.009. This is what runs on the authors' machine.
- Logistic regression: 0.985 ± 0.016. This is what runs when xgboost is
  absent.

The manuscript reported F1 0.966 ± 0.030 and described the classifier as
logistic regression. Neither configuration reproduces that figure. The
S1–S6, S1+S3–S5 and binary-F1 variants do not reproduce it either.

The held-out S6 result does reproduce. Logistic regression trained on S1
and S3–S5 gives 1/60 false positives on S6, as the manuscript reported.

## Decision

- **Primary baseline: logistic regression**, as the manuscript describes
  it. The script is run with `--logreg` in
  `analysis/non_llm_baseline_v2_oof.py`.
- **Secondary: XGBoost**, the script's default when xgboost is installed.
  It is reported alongside the primary baseline in the same table.
- The oof variant differs from the original only in the lines marked
  `[oof]`. Those lines keep each cycle's key, write each cycle's
  out-of-fold prediction, and add the `--logreg` switch.
- The revised paper reports both baselines' F1 as recomputed and states
  that they supersede the manuscript's 0.966 ± 0.030.

## Disclosure

The live-verdict comparison with both baselines was computed while the
paired test was being checked, before this amendment. On 270 cycles, the
live system had F1 0.981.

| Baseline | Baseline F1 | b | c | Exact McNemar p |
|---|---|---|---|---|
| Logistic regression | 0.987 | 1 | 3 | 0.63 |
| XGBoost | 0.997 | 1 | 6 | 0.13 |

No E2 condition has been compared with either baseline.
