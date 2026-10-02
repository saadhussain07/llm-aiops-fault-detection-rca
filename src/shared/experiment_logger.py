import json
import os
from datetime import datetime


class ExperimentLogger:
    """
    Appends a complete record of every monitoring cycle -- the fused context
    object, the LLM verdict, and the pipeline's final decision -- to a JSONL
    file.

    Previously this data lived only in RAM (SharedMemoryStore) and was lost
    when the process exited. Each scenario now gets its own file:

        experiment_logs/S1_CPU_STRESS.jsonl
        experiment_logs/S2_POD_CRASH.jsonl
        ...

    Each line is one complete, self-contained monitoring cycle, which allows:
      (a) the statistics in Tables 2-5 to be recomputed from raw records
      (b) features and labels to be extracted for training the non-LLM
          baseline classifier (logistic regression, Section 6.2)
      (c) the LLM verdict and the baseline verdict to be compared
          side by side
    """

    def __init__(self, output_dir="experiment_logs"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def log_cycle(
        self,
        scenario,
        run_id,
        cycle_num,
        context,
        llm_verdict,
        pipeline_status,
        confidence_threshold,
        expected_root_cause=None,
        expected_fault_class=None,
    ):
        """
        scenario:              e.g. "S1_CPU_STRESS" (fault-injection scenario tag)
        run_id:                independent run index for this scenario (1-5)
        cycle_num:              monitoring cycle index within this run (1, 2, 3, ...)
        context:                full output of context_builder.build_context()
        llm_verdict:            full output of runtime_anomaly_agent.analyze()
                                 (None for a normal, no-anomaly cycle)
        pipeline_status:        "PENDING_OPERATOR_CONFIRMATION" /
                                 "ESCALATED_TO_HUMAN" / "MONITORING" /
                                 "CONFIRMATION_APPROVED" / "CONFIRMATION_REJECTED".
                                 This is the authoritative record of what the
                                 pipeline did. Note that no value corresponds to
                                 autonomous execution: the prototype has no
                                 autonomous execution path.
        confidence_threshold:   the theta value in force for this run (0.85)
        expected_root_cause:    the service the fault was injected into
                                 (e.g. "cartservice"). This is the
                                 injection-controlled ground truth described in
                                 Section 6.4 -- known in advance because the
                                 fault is deliberately injected.
        expected_fault_class:   "resource" / "crash" / "network" / "dependency" / "none"
        """
        record = {
            "logged_at": datetime.now().isoformat(),
            "scenario": scenario,
            "run_id": run_id,
            "cycle_num": cycle_num,
            "confidence_threshold_used": confidence_threshold,
            "expected_root_cause_service": expected_root_cause,
            "expected_fault_class": expected_fault_class,
            "context_object": context,
            "llm_verdict": llm_verdict,
            "pipeline_status": pipeline_status,
        }

        path = os.path.join(self.output_dir, f"{scenario}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        print(f"[ExperimentLogger] Logged cycle {cycle_num} (run {run_id}) → {path}")