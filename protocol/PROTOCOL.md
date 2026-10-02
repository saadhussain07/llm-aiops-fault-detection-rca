# Protocol: strengthening the source study (detection and RCA)

**Committed:** 2026-09-28, before any call described here was made.
**Applies to:** `analysis/replay.py` (`--votes`, `--strip-notes`),
`analysis/first_vote_compare.py`, and the detection/RCA analyses built on
their outputs.

## Why

The source study (FGCS-D-26-05108, declined 2026-09-27 with the comment that
the conclusions need more substantiation) reports detection and root-cause
attribution for one model, `gpt-oss-120b`, under one prompt pipeline. The
companion study (ISSE submission, 2026-09-27) found, for a second model on
run 1, that deleting the pipeline's interpretive NOTE lines reversed 15 of
16 annotated verdicts. Some NOTE lines name the verdict outright (all S3
contexts; some S2 and S4 contexts). The source study's detection figures
therefore need to be separated into what the model decides and what the
context supplies, and tested beyond a single model.

## What has and has not been observed

Observed: the live source records (330 cycles, 190 agent-invoking); the
companion study's results, including the `gpt-oss-120b` fidelity replay
(42/42), the `qwen3.8-27b` replay of 42 cycles and its NOTE ablation on
29 cycles of run 1.
Not observed: any `gpt-oss-120b` response to a NOTE-stripped context; any
model's response to runs 2–5 in replay.

## Design choice: one vote per cycle

All experiments here use `--votes 1`. The companion study showed that the
first live vote equals the five-vote majority on all 190 agent-invoking
cycles, so K = 1 changes no verdict for the source model, and it cuts
free-tier API use fivefold. Because every live record stores its votes,
the live first vote is itself a live K = 1 verdict and serves as the
reference for E1.

## E1 — NOTE ablation of the source model, full dataset

- Model: `openai/gpt-oss-120b`; `--strip-notes --votes 1`; runs 1–5.
- Selection (mechanical, by the script): every run in which at least one
  agent-invoking context carries a `NOTE:` line. Whole runs are replayed so
  history evolves as in the live run.
- Groups: NOTE cycles (context carried a NOTE; only those lines are
  removed) and control cycles (no NOTE; identical text).
- Reference: live first vote. Secondary reference: live majority.
- Primary outcome: number of NOTE cycles whose verdict changes, per
  scenario, against the number of control cycles that change.
- Secondary outcomes: detection recall/precision/F1 and root-cause accuracy
  recomputed with the ablated verdicts, using the source study's
  definitions and ground truth unchanged.

## E2 — Second and third models, full dataset, deployed contexts

- Models: `qwen/qwen3.8-27b` (cap `--max-tokens 1000`, as in the companion
  study) and one further model available on the same free tier, to be named
  in an amendment **before its first call**, chosen for availability and
  limits only.
- `--votes 1`; runs 1–5; contexts exactly as recorded (NOTE lines kept).
- Outcomes: per-scenario detection recall (S1, S3, S4), precision/recall/F1
  (S2, S5), false-positive rate (S6), and root-cause accuracy on the first
  `ANOMALY_CONFIRMED` cycle per run, all with the source study's
  definitions and ground truth.

## E3 — Statistics

- 95% confidence intervals by bootstrap over runs (and over cycles where a
  per-run estimate is undefined).
- Paired comparisons between models, and between each model and the
  source study's logistic-regression baseline, by exact McNemar tests on
  the same cycles.
- No test is added after results are seen without an amendment saying so.

## Reporting

- Every completed run is reported. If free-tier limits stop an experiment
  early, the coverage achieved is stated and nothing is extrapolated.
- Cycles decided on fewer votes than requested (API failures) are kept and
  reported, as in the source study.
- No run is repeated once complete. Interrupted runs resume from their
  per-cycle checkpoint.
- Results that weaken the source study's claims are reported with the same
  prominence as results that support them.

## Overlap with the companion study

Results already reported in the companion study (its execution study,
allow-list replay, voting counterfactual, and the run-1 `qwen3.8-27b`
replay and ablation) are cited, not re-reported. E1 and E2 are new data.
