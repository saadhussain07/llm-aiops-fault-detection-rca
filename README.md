# LLM-based fault detection and root-cause analysis for Kubernetes: what the model contributes

An LLM agent that detects faults in a Kubernetes microservice system, explains their root cause and proposes remediation, together with a pre-registered study of where its accuracy comes from. The repository holds:
- the complete system: telemetry collection, context fusion, the LLM agent, risk-stratified remediation proposals and fault injection;
- the 330 recorded monitoring cycles it produced;
- every experiment, measurement and analysis script behind the findings.

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.23107449.svg)](https://doi.org/10.5281/zenodo.23107449)

> **Manuscript:** *LLM-based fault detection and root-cause analysis for Kubernetes: a pre-registered evaluation of what the model contributes.* M. S. Hussain and A. Farrukh, 2026. Under review.

---

## Contents

- [The system](#the-system)
- [Experiments](#experiments)
- [What was measured](#what-was-measured)
- [Findings](#findings)
- [Pre-registration](#pre-registration)
- [Layout](#layout)
- [Running the system](#running-the-system)
- [Reproducing the analysis](#reproducing-the-analysis)
- [Data format](#data-format)
- [Provenance and related work](#provenance-and-related-work)
- [Licence](#licence)

---

## The system

```
 Prometheus ─┐
 Loki ───────┼─► collectors ─► context builder ─► LLM agent ─► verdict + root cause ─► remediation proposal ─► operator
 Jaeger ─────┘   (scoring)     (+ NOTE lines)    (K votes)                             (risk-tagged kubectl)     (confirms)
```

**Fault detection (`src/collectors/`).**
- `metric_collector.py` scores per-service CPU and memory against per-service baselines with Isolation Forest. It also flags recently restarted pods (age < 300 s).
- `trace_analyser.py` computes p99 latency and error-span rates from Jaeger, with thresholds of 2 s and 5%.
- `log_parser.py` mines error-log templates from Loki with Drain3.

**Context fusion (`src/collectors/context_builder.py`).** The collectors' results are fused into one context object. Its text summary is the model's main input. The builder also appends interpretive `NOTE` lines for some patterns, such as CPU near its limit, a recent restart, memory above baseline, or a dependency mentioned in an error. Some of these lines prescribe a fault class or name the expected verdict, and the study measures their effect.

**Diagnosis (`src/agents/runtime_anomaly_agent.py`).**
- The agent sends the context to the model under a fixed system prompt with a mandatory JSON schema: verdict, confidence, affected services, root cause, evidence and remediation steps.
- It draws five samples at temperature 0.1 and takes the majority verdict.
- It carries the two previous cycles as history.
- This runtime anomaly agent is the only agent in the system.

**Remediation (`src/main.py`, `src/shared/safety.py`).**
- Each proposed step is a `kubectl` command tagged LOW, MEDIUM or HIGH risk.
- A LOW-risk step is queued for operator confirmation only when the verdict is confirmed and mean confidence is at least 0.85. Every other step escalates to a human.
- Before any confirmed command could run, it must also pass the allow-list in `src/shared/safety.py`.
- **Nothing executes automatically.** The record of what the pipeline did is `pipeline_status`.

---

## Experiments

Six scenarios on a two-node Kind cluster running six services of the Online Boutique benchmark, five runs each. Fault-injection scripts are in `experiments/fault_injection/`.

| | Fault | Injection | Runs × cycles | Agent calls |
| --- | --- | --- | --- | --- |
| S1 | CPU stress | Busy-loop via `kubectl exec` into `cartservice` | 5 × 12 | 58 |
| S2 | Pod crash | Force-delete `cartservice` | 5 × 12 | 32 |
| S3 | Memory pressure | Heap allocation in `paymentservice` | 5 × 6 | 29 |
| S4 | Network latency | 500 ms delay via `tc netem` on `frontend` | 5 × 12 | 60 |
| S5 | Dependency failure | Force-delete `redis-cart` | 5 × 12 | 10 |
| S6 | None (fault-free control) | — | 5 × 12 | 1 |

Each cycle records three things: the full context object, every vote of the model, and the injected ground truth. In total that is 330 cycles, 190 of them with an agent call, in `data/`.

---

## What was measured

- **E1: annotation ablation.** The source model (`openai/gpt-oss-120b`) was replayed with every `NOTE` line removed.
- **E2: model choice.** Two further models (`openai/gpt-oss-20b`, `qwen/qwen3.8-27b`) were replayed on the same 190 inputs.
- **E3: non-LLM baselines.** Logistic regression and XGBoost were trained on eight features from the same context. Every condition is compared on the same cycles with exact McNemar tests, cluster-bootstrap intervals and Holm adjustment.

All replays use `analysis/replay.py`, which runs the unmodified agent on the recorded inputs.

| What | How |
| --- | --- |
| Detection | Recall (S1, S3, S4); precision, recall and F1 (S2, S5); false-positive rate (S6); pooled over 330 cycles. Scored under the recorded labels (`ground_truth.tsv`) and a sensitivity ground truth. |
| Root-cause attribution | Fixed rules on the first confirmed cycle of each run: service and fault class named first in the narrative. Checked against a blinded human rating (`rating/`). |

---

## Findings

| Question | Result |
| --- | --- |
| Detection, live system | Pooled F1 0.921 (recorded labels), 0.951 (sensitivity). S6 false-positive rate 0%. |
| E1: what the annotations contribute | Removing them reversed 17 of 20 annotated pod-crash verdicts. CPU, memory and dependency-failure detection stayed largely intact. |
| E2: what the model contributes | Annotated cycles: 0 and 4 of 96 verdicts changed. Unannotated cycles: 8 and 41 of 94. |
| E3: models vs classifiers | No model is significantly better than logistic regression (F1 0.987) or XGBoost (0.997). `qwen3.8-27b` is significantly worse (0.861, *p* < 0.001). |
| Root-cause attribution | CPU and memory faults are correct in every condition. The network delay is never correct on the first confirmed cycle. In three runs, a pod restart left by the test harness reached the model through an annotation and produced a crash diagnosis. |
| Remediation | Of 330 cycles, 49 queued a LOW-risk step for operator confirmation, 138 escalated to a human and 143 needed no action. None executed automatically. The safety of the proposed actions is audited in the companion study (see below). |

---

## Pre-registration

The protocol and Amendments 1–3 were committed before the analyses they govern. Amendment 4 records a deviation after the fact: only one of two planned raters took part.

| File | Content |
| --- | --- |
| `protocol/PROTOCOL.md` | E1–E3, outcomes, reporting rules |
| `protocol/AMENDMENT_1.md` | Third model; primary and sensitivity ground truth |
| `protocol/AMENDMENT_2.md` | Rule-based root-cause scoring; bootstrap and multiplicity |
| `protocol/AMENDMENT_3.md` | Non-LLM baselines |
| `protocol/AMENDMENT_4.md` | Single rater (written after the rating) |
| `protocol/REGISTRATION_LOG.txt` | The registration commits, with full hashes and times |

**Provenance.**
- The commits were made in a private working repository that also holds unrelated drafts. That repository is available to editors on request.
- This study began as a revision of a manuscript previously submitted to *Future Generation Computer Systems*. The protocol documents therefore say "FGCS v2", and in the working repository they were named `FGCS_V2_PROTOCOL.md` and `FGCS_V2_AMENDMENT_1.md` to `FGCS_V2_AMENDMENT_4.md`.
- Here they are renamed only. Their text is exactly as committed, because changing a registered document after the fact would defeat its purpose.

---

## Layout

```
.
├── data/              Live cycle records, 330 cycles (S1–S6)
├── ground_truth.tsv   Per-cycle labels recorded with the source study (primary ground truth)
├── src/
│   ├── collectors/    metric_collector, trace_analyser, log_parser, context_builder (writes the NOTE lines)
│   ├── agents/        runtime_anomaly_agent (the LLM agent; used unchanged by the replay harness)
│   ├── shared/        experiment logger, memory store, safety checks
│   └── main.py        pipeline loop
├── experiments/fault_injection/   Injection scripts for S1–S6
├── deploy/            Cluster and observability manifests
├── replay/
│   ├── openai__gpt-oss-120b__nonote__k1/   E1: NOTE lines removed (177 cycles)
│   ├── openai__gpt-oss-20b__k1/            E2 (190 agent-invoking cycles)
│   └── qwen__qwen3.8-27b__k1/              E2 (190 agent-invoking cycles)
├── baseline/          Source study's baseline script (unchanged), its inputs, out-of-fold predictions
├── rating/            Blinded root-cause rating: blank sheet, completed sheet, key
├── protocol/          Protocol, Amendments 1–4, registration log
└── analysis/          All analysis scripts
```

---

## Running the system

Requirements:
- Python 3.13
- Docker Desktop (at least 6 GB)
- [Kind](https://kind.sigs.k8s.io/) (tested on Kubernetes v1.32.2)
- `kubectl` and Helm
- A [Groq API key](https://console.groq.com/keys)

```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                  # then put your Groq key in .env (never commit it)

kind create cluster --name aiops --config deploy/kind-config.yaml
helm install prometheus prometheus-community/kube-prometheus-stack --version 65.x -f deploy/prometheus-values.yaml
helm install loki grafana/loki-stack --version 2.10 -f deploy/loki-values.yaml
kubectl apply -f deploy/jaeger.yaml
kubectl apply -f deploy/microservices-lite.yaml -f deploy/loadgenerator.yaml
kubectl wait --for=condition=ready pod --all --timeout=300s

kubectl port-forward svc/prometheus-operated 9090:9090 &
kubectl port-forward svc/loki 3100:3100 &
kubectl port-forward svc/jaeger-query 16686:16686 &

python src/main.py                                    # one monitoring cycle every 30 s
```

To reproduce a scenario, run its script in `experiments/fault_injection/` (PowerShell). Each script injects the fault, runs the pipeline for the configured cycles and appends records to `data/`.

---

## Reproducing the analysis

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

## Data format

Each line in a `data/*.jsonl` file is one complete monitoring cycle. Records carry `run_id` and `cycle_num`, so per-run statistics are fully recoverable from the raw files.

| Field | Meaning |
| --- | --- |
| `run_id`, `cycle_num` | Run index (1–5) and cycle index within that run |
| `expected_root_cause_service` | Injected ground-truth service, for RCA scoring |
| `expected_fault_class` | Injected ground-truth fault class |
| `context_object` | The fused multi-signal context passed to the LLM |
| `context_object.full_metrics` | Per-service metrics and Isolation Forest scores |
| `context_object.full_logs` | Per-service log counts and Drain3 templates |
| `context_object.summary_for_llm` | Natural-language summary injected into the prompt |
| `llm_verdict` | The agent's aggregated structured output |
| `llm_verdict.vote_agreement` | Self-consistency agreement, e.g. `"5/5"` |
| `llm_verdict.all_votes` | The individual verdicts from all five votes |
| `pipeline_status` | **What the pipeline actually did with the verdict** |

### Two fields that are easy to misread

**`llm_verdict.auto_remediate` is a model output, not an execution record.** It is the LLM's own suggestion flag, captured verbatim for transparency and analysis. It does not gate anything. The authoritative record of what the pipeline did is `pipeline_status`, which takes values such as `ESCALATED_TO_HUMAN`, `QUEUED_FOR_CONFIRMATION`, or `MONITORING`. No cycle in this dataset resulted in autonomous execution, because the prototype contains no autonomous execution path.

**`llm_verdict.confidence` is the mean across the five votes**, not a single sample. A cycle can therefore show `ANOMALY_CONFIRMED` with confidence below the 0.85 gate — in which case it escalates to a human rather than being queued for confirmation. Cycles of exactly this kind are present in the data and are the confidence gate working as designed.

---

## Provenance and related work

- **First deposit.** The 330 cycle records and the system code were first deposited with an earlier, superseded manuscript (DOI [10.5281/zenodo.22816805](https://doi.org/10.5281/zenodo.22816805)). The files in `data/` are unchanged copies. The study here supersedes that manuscript's headline figures; the corrections are listed in the manuscript.
- **Companion study.** A companion study audits the same agent's safety mechanisms: the confidence gate, command allow-list, operator confirmation and voting. It asks what the agent would have done to the cluster. Its material is at [`llm-aiops-safety-audit`](https://github.com/saadhussain07/llm-aiops-safety-audit) (DOI [10.5281/zenodo.22974350](https://doi.org/10.5281/zenodo.22974350)).

---

## Licence

Code: MIT (`LICENSE`). Data (`data/`, `replay/`, `baseline/`, `rating/`, `ground_truth.tsv`): CC BY 4.0 (`LICENSE-DATA`).
