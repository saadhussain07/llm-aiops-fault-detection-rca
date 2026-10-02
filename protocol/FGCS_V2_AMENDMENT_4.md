# FGCS v2 protocol, Amendment 4: human rating with one rater

**Date:** 2026-09-29. This amendment was written and committed **after** the
human rating was complete. It records a deviation; it does not govern any
analysis that had not yet been run.

## Deviation

Amendment 2 (section 1) planned two raters for the blinded rating of the
first-confirmed-cycle root-cause narratives, with Cohen's κ between them.
Only one rater took part: the first author. No second rater was used.

## What was done

- The sheet `rca_rating_A.xlsx` (94 unique narratives, seed 20260929) was
  committed blank before rating. The key was withheld from the rater until
  the rating was complete.
- The first author rated all 94 items by hand (`rca_rating_A_filled.xlsx`,
  committed unchanged after rating).
- The key was then regenerated with `analysis/rca_blind_sheet.py`. It is
  byte-identical to the key held back during rating.
- κ is reported between the rater and the rules only
  (`analysis/rca_kappa.py rca_rating_A_filled.xlsx rca_rating_key.csv`).

## Disclosures

- The rater had seen some narratives during the analysis (`rca_metrics.py
  --show`) before rating.
- The rater's S4 class judgements are not consistent with one another.
  Six crash explanations were accepted as the correct class, on the
  reasoning that the restart followed from the injected delay. Two of the
  same kind were rejected.
- The records show that the restart was performed by the harness before
  the delay was injected. The rule-based S4 figures are therefore kept,
  and the paper reports both.
- The completed sheet has not been edited after rating.
