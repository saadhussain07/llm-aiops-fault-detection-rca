# FGCS v2 protocol, Amendment 2

**Date:** 2026-09-28. Written before root-cause attribution was scored on
any replayed cycle, and before E3 was run on any condition. The E1 detection
outcomes had been seen when this was written; the E2 outcomes had not.

## 1. Root-cause attribution: mechanical criteria

The protocol said root-cause accuracy would use "the source study's
definitions". The source manuscript scored service and fault class by
hand, and it did not record the procedure, so those definitions cannot be
applied to new verdicts. From here on, attribution is scored by
`analysis/rca_metrics.py`, and every condition, live or replayed, uses the
same rules.

- **Unit.** The first `ANOMALY_CONFIRMED` cycle of each run. A run with no
  confirmed cycle counts as a miss, so each scenario has denominator 5.
  The same criteria are also reported over all confirmed cycles.
- **Normalisation.** Unicode hyphens (U+2010–2015, U+2212) are mapped to
  `-` before matching. The model often writes `redis‑cart` with a
  non-breaking hyphen, and the strict criterion in the source repository
  (`rca_criteria_check.py`) missed those.
- **listed.** The expected service appears in `affected_services`. This is
  the permissive criterion in the source repository, kept for continuity.
- **service.** The first service of the deployment named in `root_cause`
  is the expected service.
- **class.** The first fault-class term found in `root_cause` belongs to
  the expected fault class (`resource`, `crash`, `network`, `dependency`).
  The lexicon is fixed in the script. It was written after reading the
  live narratives and before any replayed narrative was scored.
- **both.** service and class. This is the headline attribution figure.

**Secondary check.** Two authors will rate the first-confirmed narratives
of every condition for service and fault class. They will see a shuffled
sheet with the condition hidden. Agreement with each other (Cohen's κ) and
with the mechanical rule will be reported.

## 2. Discrepancy with the source manuscript

The finding below comes from the live verdicts, before any replay was
scored. Under the rules in §1, the live system gets:

| Scenario | service | class | both |
|---|---|---|---|
| S1 | 5/5 | 5/5 | 5/5 |
| S2 | 5/5 | 5/5 | 5/5 |
| S3 | 5/5 | 5/5 | 5/5 |
| S4 | 3/5 | 0/5 | 0/5 |
| S5 | 4/5 | 5/5 | 4/5 |
| Total | 22/25 | 20/25 | 19/25 |

The source manuscript reported service 24/25 and service and class 22/25,
with S4 at 5/5 and 3/5. Its footnote says the two S4 misses gave a
crash/OOMKill account.

The recorded narratives say something different:

- In three S4 runs (2–4), the model blames a crash of the `frontend` pod.
  The service is right; the class is wrong.
- In two S4 runs (1, 5), it blames CPU saturation in `cartservice` and
  `currencyservice`. Both service and class are wrong.
- No S4 narrative names the injected network delay as the cause.

The S5 figures (4/5, run 2 misses) agree with the manuscript.

The revised paper will:

- report the mechanical figures;
- state that they supersede the manuscript's attribution table;
- report the permissive `listed` criterion alongside them.

That criterion is trivially satisfied in S4, where the agent lists all
three services in every cycle.

## 3. E3 details

- **Intervals.** Cluster bootstrap over runs: within each scenario the five
  runs are resampled with replacement and their cycle counts are pooled.
  Settings: 10 000 resamples, seed 20260928, percentile interval. The
  metric and its point estimate are computed from pooled counts. This
  replaces the undefined per-run cases that the protocol's "over cycles"
  clause was meant to cover. Tables keep reporting per-run mean ± std next
  to the interval.
- **Multiplicity.** Exact McNemar p-values are reported raw and with
  Holm's adjustment across all comparisons in a table.
- **LR baseline.** The comparison uses the source study's own logistic
  regression script, changed only to write its out-of-fold prediction for
  each cycle. It covers the same cycles as the source comparison (S1,
  S3–S6), so the recorded labels and the sensitivity rule coincide there.
  If the original script cannot be recovered, a re-implementation from
  the manuscript's description will be used. It will be labelled as a
  re-implementation, and its fold-mean F1 will be reported next to the
  manuscript's 0.966 ± 0.030.
