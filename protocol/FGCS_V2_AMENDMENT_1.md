# FGCS v2 protocol, Amendment 1

**Date:** 2026-09-28, before any call to the model named here and before
any E1 or E2 outcome was inspected. E1 had started; its outputs have not
been analysed.

## 1. Third model for E2

`openai/gpt-oss-20b`.

The free tier currently lists five general text models: `allam-2-7b`,
`openai/gpt-oss-120b` (the source model), `openai/gpt-oss-20b`,
`openai/gpt-oss-safeguard-20b` and `qwen/qwen3.8-27b`. The safeguard model
is a safety classifier, not a general reasoning model, and `allam-2-7b` is
a 7B model oriented to Arabic. `gpt-oss-20b` is therefore the only
remaining general model. It shares a family with the source model, so it
tests the effect of model scale within a family, while `qwen3.8-27b` tests
a different family. This limitation will be stated in the paper.

Settings: `--votes 1`, runs 1–5, contexts as recorded, no token cap unless
free-tier limits force one, in which case the cap is recorded before it is
applied.

## 2. Detection ground truth

**Primary.** The per-cycle labels recorded at the time of the source study
(`S*_for_baseline.jsonl`, field `ground_truth_verdict`), extracted unchanged
into `ground_truth.tsv`. The recorded records are otherwise identical to the
published cycle records (checked: 0 of 330 differ). These labels mark S1,
S3 and S4 positive throughout, S6 negative throughout, S5 positive in the 10
cycles carrying the dependency-failure log entry, and S2 positive in the
first two post-restart cycles of each run (10 cycles).

**Sensitivity.** An explicit rule on the recorded context, identical except
for S2, which it marks positive while `cartservice` is flagged
`recently_restarted` (20 cycles). `analysis/detection_metrics.py --rule-gt`.

**Discrepancy with the source manuscript, found before any replayed
condition was scored.** With the recorded labels, the live verdicts give
S2 precision 0.36 ± 0.14 and pooled F1 0.921; with the sensitivity rule,
0.71 ± 0.28 and 0.951. The source manuscript reported 0.73 ± 0.23 and
0.965, which neither reproduces, and the procedure that produced them is not
in the recorded material. All other detection figures reproduce exactly.
The revised paper will report figures recomputed with the published script
under both ground truths, state that they supersede the S2 precision and
pooled F1 of the earlier manuscript, and use the same ground truth for every
live and replayed condition.
