# LLM-based fault detection and root-cause analysis for Kubernetes: what the model contributes

Code, records and protocol for a pre-registered evaluation of an LLM agent that diagnoses injected faults in a Kubernetes microservice testbed. The agent, its pipeline and its 330 recorded monitoring cycles come from [`fgcs-aiops-llm-agent`](https://github.com/saadhussain07/fgcs-aiops-llm-agent) (DOI [10.5281/zenodo.22816805](https://doi.org/10.5281/zenodo.22816805)). This repository asks which part of that system produces its accuracy: the model, the pipeline's interpretive annotations, or the engineered features.

> Manuscript: *LLM-based fault detection and root-cause analysis for Kubernetes: a pre-registered evaluation of what the model contributes.* M. S. Hussain and A. Farrukh, 2026. Under review.

---

## Findings

| Question | Result |
| --- | --- |
| **E1.** Remove the pipeline's `NOTE` annotations from the source model (`gpt-oss-120b`) | 17 of 20 annotated pod-crash verdicts reversed. CPU, memory and dependency-failure detection largely intact. |
| **E2.** Replay two further models (`gpt-oss-20b`, `qwen3.8-27b`) on the same inputs | Annotated cycles: 0 and 4 of 96 verdicts changed. Unannotated cycles: 8 and 41 of 94. |
| **E3.** Compare with classifiers on the same features (exact McNemar) | No model is significantly better than logistic regression (F1 0.987) or XGBoost (0.997). `qwen3.8-27b` is significantly worse (0.861, *p* < 0.001). |
| **Root-cause attribution** (rule-based, first confirmed cycle per run) | CPU and memory faults correct in every condition. Network delay correct in none. In three runs, a restart left by the test harness reached the model through an annotation and produced a crash diagnosis. |

Several figures in the source study's version 1.0 do not reproduce. The corrected values are listed in that repository's README (version 1.1).

---

## Pre-registration

The protocol and Amendments 1–3 were committed before the analyses they govern. Amendment 4 records a deviation after the fact: only one of two planned raters took part. The commits were made in a private working repository that also holds unrelated drafts. `protocol/REGISTRATION_LOG.txt` lists them with full hashes and times. The repository itself is available to editors on request.

| File | Content |
| --- | --- |
| `protocol/FGCS_V2_PROTOCOL.md` | E1–E3, outcomes, reporting rules |
| `protocol/FGCS_V2_AMENDMENT_1.md` | Third model; primary and sensitivity ground truth |
| `protocol/FGCS_V2_AMENDMENT_2.md` | Rule-based root-cause scoring; bootstrap and multiplicity |
| `protocol/FGCS_V2_AMENDMENT_3.md` | Non-LLM baselines |
| `protocol/FGCS_V2_AMENDMENT_4.md` | Single rater (written after the rating) |

---

## Layout

```
.
├── data/              Live cycle records (copied unchanged from fgcs-aiops-llm-agent v1.0)
├── ground_truth.tsv   Per-cycle labels recorded with the source study (primary ground truth)
├── replay/
│   ├── openai__gpt-oss-120b__nonote__k1/   E1: NOTE lines removed (177 cycles)
│   ├── openai__gpt-oss-20b__k1/            E2 (190 agent-invoking cycles)
│   └── qwen__qwen3.8-27b__k1/              E2 (190 agent-invoking cycles)
├── baseline/          Source study's baseline script (unchanged), its inputs, out-of-fold predictions
├── rating/            Blinded root-cause rating: blank sheet, completed sheet, key
├── protocol/          Protocol, Amendments 1–4, registration log
├── src/agents/        The agent class, unchanged, used by the replay harness
└── analysis/          All analysis scripts
```

---

## Reproducing

Python 3.11+ with `scipy`, `scikit-learn`, `openpyxl` and `matplotlib`; `xgboost` is needed only for the secondary baseline. Run from the repository root:

```bash
pip install scipy scikit-learn openpyxl matplotlib xgboost python-dotenv groq

# detection, per condition (add --rule-gt for the sensitivity ground truth)
python analysis/detection_metrics.py data --first-vote
python analysis/detection_metrics.py data replay/openai__gpt-oss-120b__nonote__k1

# verdict changes on annotated vs unannotated cycles
python analysis/first_vote_compare.py data replay/qwen__qwen3.8-27b__k1

# root-cause attribution
python analysis/rca_metrics.py data replay/openai__gpt-oss-20b__k1

# bootstrap intervals and exact McNemar tests, models vs logistic regression
python analysis/stats.py data live replay/openai__gpt-oss-120b__nonote__k1 \
    replay/openai__gpt-oss-20b__k1 replay/qwen__qwen3.8-27b__k1 --lr baseline/lr_predictions.tsv

# exploratory analyses reported in the manuscript
python analysis/exploratory.py data replay/openai__gpt-oss-120b__nonote__k1 \
    replay/openai__gpt-oss-20b__k1 replay/qwen__qwen3.8-27b__k1

# blinded rating vs rules
python analysis/rca_kappa.py rating/rca_rating_A_filled.xlsx rating/rca_rating_key.csv

# baseline predictions (writes lr_predictions.tsv / xgb_predictions.tsv)
python analysis/non_llm_baseline_v2_oof.py baseline/S1_for_baseline.jsonl baseline/S3_for_baseline.jsonl \
    baseline/S4_for_baseline.jsonl baseline/S5_for_baseline.jsonl baseline/S6_for_baseline.jsonl --logreg
```

`analysis/replay.py` re-runs the unmodified agent on recorded contexts:

```bash
python analysis/replay.py run openai/gpt-oss-20b --votes 1
python analysis/replay.py run openai/gpt-oss-120b --votes 1 --strip-notes
```

It needs a Groq API key. Put it in the file named by `GROQ_ENV_FILE` (default `.env`), which is ignored by git.

---

## Licence

Code: MIT (`LICENSE`). Data (`data/`, `replay/`, `baseline/`, `rating/`, `ground_truth.tsv`): CC BY 4.0 (`LICENSE-DATA`).
