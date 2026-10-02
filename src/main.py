import sys
import json
import time
import argparse
import subprocess
from datetime import datetime

sys.path.append(".")

from collectors.metric_collector import MetricCollector
from collectors.trace_analyser import TraceAnalyser
from collectors.log_parser import LogParser
from collectors.context_builder import ContextBuilder
from agents.runtime_anomaly_agent import RuntimeAnomalyAgent
from shared.memory_store import SharedMemoryStore
from shared.experiment_logger import ExperimentLogger
from shared.safety import validate_command  # NEW


# Threshold theta = 0.85, as reported in the paper (Section 4.6).
CONFIDENCE_THRESHOLD = 0.85


class AIOpsMainPipeline:
    """
    Main pipeline.

    TWO CONFIRMATION MODES:

    1. interactive_confirmation=False (DEFAULT -- formal experiment mode):
       LOW-risk steps are logged with status "PENDING_OPERATOR_CONFIRMATION".
       Nothing is executed and the pipeline proceeds immediately to the next
       cycle. This matches the paper's definition of the DPL metric
       (Section 6.3) exactly: the interval from fault injection to a
       confidence-scored plan being surfaced for operator confirmation.
       Confirmation wait time is deliberately excluded from DPL, so blocking
       the pipeline on it during automated runs would both misreport the
       metric and make it non-reproducible, since operator wait time varies
       from run to run.

    2. interactive_confirmation=True (DEMO/MANUAL mode, --interactive flag):
       Each LOW-risk step is shown to the operator in the terminal with a
       real y/n prompt. Even after operator approval, the command must still
       pass the allow-list in shared/safety.py -- both conditions (operator
       approval AND allow-list) must hold before subprocess.run() is called.
       This mode is NOT used for the automated experiment runs; it exists for
       qualitative demonstration and testing.
    """

    def __init__(
        self,
        scenario="UNLABELLED",
        run_id=0,
        expected_root_cause=None,
        expected_fault_class=None,
        confidence_threshold=CONFIDENCE_THRESHOLD,
        interactive_confirmation=False,
        ablation_suppress=None,  # NEW: 'metrics' | 'logs' | 'traces' | None
        ablation_target_service=None,  # NEW: which service's signal to
                                        # suppress. Defaults to
                                        # expected_root_cause, but must
                                        # be set EXPLICITLY for
                                        # scenarios where the ground-
                                        # truth root cause isn't itself
                                        # a monitored service (e.g. S5:
                                        # redis-cart is never monitored
                                        # at all -- the OBSERVABLE proxy
                                        # signal lives on cartservice/
                                        # frontend instead, so THAT is
                                        # what must be suppressed for a
                                        # meaningful test).
    ):
        self.memory = SharedMemoryStore()
        self.metric_collector = MetricCollector()
        self.trace_analyser = TraceAnalyser()
        self.log_parser = LogParser()
        self.context_builder = ContextBuilder()
        self.runtime_agent = RuntimeAnomalyAgent(
    monitored_services=self.metric_collector.services
)
        self.run_count = 0

        self.scenario = scenario
        self.run_id = run_id
        self.expected_root_cause = expected_root_cause
        self.expected_fault_class = expected_fault_class
        self.confidence_threshold = confidence_threshold
        self.interactive_confirmation = interactive_confirmation  # NEW
        self.ablation_suppress = ablation_suppress  # NEW
        self.ablation_target_service = ablation_target_service or expected_root_cause  # NEW
        self.logger = ExperimentLogger()

    def collect_signals(self):
        print("\n[Pipeline] STEP 1: Collecting signals...")
        metric_result = self.metric_collector.run()
        trace_result = self.trace_analyser.run()
        log_result = self.log_parser.run()

        # NEW: ablation suppression. Collection runs NORMALLY (the
        # real, already-validated collectors are untouched) -- we
        # discard one signal's results AFTER collection, simulating
        # "this signal type was never available" to the LLM, rather
        # than modifying any collector's own logic.
        #
        # FIX (real ablation data): suppressing ALL 5 services'
        # metrics simultaneously created an artificial "mass outage"
        # pattern that doesn't resemble any real condition -- the LLM
        # correctly read "everyone silent at once" as a genuine
        # multi-service crash (its own root_cause text said so
        # explicitly), which is a reasonable inference from that
        # PATTERN, but the pattern itself was an artifact of blanket
        # suppression, not a real signal-availability scenario. A
        # realistic "we lack this signal for the fault-relevant
        # service" ablation should suppress ONLY the target service's
        # entry, leaving the other services' genuine, normal readings
        # intact -- exactly as a real partial monitoring gap would
        # look. This targets the suppression correctly instead of
        # simulating a scenario that never occurs in practice.
        if self.ablation_suppress in ("metrics", "logs", "traces"):
            # Support one or more comma-separated target services
            # (needed for S5: both cartservice AND frontend show the
            # observable proxy signal for the redis-cart root cause).
            targets = [s.strip() for s in self.ablation_target_service.split(",")]

        if self.ablation_suppress == "metrics":
            print(f"[Ablation] Suppressing METRICS signal for {targets} only")
            metric_result["anomalies"] = [
                a for a in metric_result.get("anomalies", [])
                if a.get("service") not in targets
            ]
            metric_result["all_metrics"] = [
                m for m in metric_result.get("all_metrics", [])
                if m.get("service") not in targets
            ]
        elif self.ablation_suppress == "logs":
            print(f"[Ablation] Suppressing LOGS signal for {targets} only")
            log_result["anomalies"] = [
                a for a in log_result.get("anomalies", [])
                if a.get("service") not in targets
            ]
            log_result["log_analysis"] = [
                l for l in log_result.get("log_analysis", [])
                if l.get("service") not in targets
            ]
        elif self.ablation_suppress == "traces":
            print(f"[Ablation] Suppressing TRACES signal for {targets} only")
            trace_result["anomalies"] = [
                a for a in trace_result.get("anomalies", [])
                if a.get("service") not in targets
            ]

        return metric_result, trace_result, log_result

    def build_context(self, metric_result, trace_result, log_result):
        print("\n[Pipeline] STEP 2: Building context...")
        return self.context_builder.build_context(
            metric_result, trace_result, log_result
        )

    def run_agents(self, context):
        print("\n[Pipeline] STEP 3: Running LLM agents...")

        analysis = self.runtime_agent.analyze(context)
        self.memory.write("runtime_anomaly_agent", analysis)

        # If the Groq daily token quota (TPD) is exhausted, stop the whole
        # batch gracefully here. Otherwise every remaining cycle and run
        # would produce only "ERROR" verdicts, wasting time and cluster
        # resources on unusable data. Exit code 2 is a sentinel that
        # run_formal_experiment.ps1 recognises in order to halt the loop.
        if analysis.get("quota_exhausted"):
            print("\n" + "="*60)
            print("[Pipeline] FATAL: Groq daily token quota (TPD) exhausted.")
            print("[Pipeline] Stopping to avoid wasting time on failed calls.")
            print("[Pipeline] Resume after quota resets (~24h) using:")
            print(f"[Pipeline]   .\\run_formal_experiment.ps1 -StartFrom <N>")
            print("="*60)
            sys.exit(2)

        verdict = analysis.get("verdict", "UNKNOWN")
        confidence = analysis.get("confidence", 0)
        severity = analysis.get("severity", "UNKNOWN")
        root_cause = analysis.get("root_cause", "Unknown")
        affected = analysis.get("affected_services", [])
        steps = analysis.get("remediation_steps", [])
        auto_rem = analysis.get("auto_remediate", False)
        escalate = analysis.get("escalate_to_human", True)

        if verdict == "ANOMALY_CONFIRMED":
            self.memory.update_incident(
                active=True,
                severity=severity,
                affected_services=affected,
                root_cause=root_cause,
                status="INCIDENT_DETECTED",
            )
            self.memory.write("remediation_agent", {
                "steps": steps,
                "auto_remediate": auto_rem,
                "escalate": escalate,
            })

            if confidence >= self.confidence_threshold and not escalate:
                # This status is deliberately NOT "AUTO_REMEDIATION".
                # "PENDING_OPERATOR_CONFIRMATION" is accurate, because actual
                # execution depends on operator confirmation. This matches
                # the paper's wording exactly.
                self.memory.update_incident(status="PENDING_OPERATOR_CONFIRMATION")
                self.memory.write("validation_agent", {
                    "approved_for_confirmation": True,
                    "confidence": confidence,
                    "action": "QUEUE_FOR_CONFIRMATION",
                })
            else:
                self.memory.update_incident(status="ESCALATED_TO_HUMAN")
                self.memory.write("validation_agent", {
                    "approved_for_confirmation": False,
                    "confidence": confidence,
                    "action": "HUMAN_REVIEW_REQUIRED",
                })
        else:
            self.memory.update_incident(
                active=False, severity="NORMAL", status="MONITORING"
            )

        return analysis

    def execute_remediation(self, analysis):
        """
        Layer 4: remediation decision, plus real confirmation-gated
        execution when interactive mode is enabled.
        """
        validation = self.memory.read("validation_agent")
        if not validation:
            return

        print("\n[Pipeline] STEP 4: Remediation decision...")

        if validation.get("action") == "QUEUE_FOR_CONFIRMATION":
            steps = analysis.get("remediation_steps", [])
            low_risk_steps = [s for s in steps if s.get("risk") == "LOW"]
            high_risk_steps = [s for s in steps if s.get("risk") != "LOW"]

            for step in high_risk_steps:
                print(f"  [SKIP] High/Medium risk step skipped: {step['action']}")

            if not low_risk_steps:
                print("[Pipeline] No LOW-risk steps to confirm.")
                return

            if not self.interactive_confirmation:
                # --- FORMAL EXPERIMENT MODE (default) ---
                # DPL has been measured at this point: the plan has been
                # surfaced. Waiting for confirmation does not block the
                # experiment.
                print(f"[Pipeline] {len(low_risk_steps)} step(s) QUEUED FOR "
                      f"OPERATOR CONFIRMATION (not executed — automated "
                      f"experiment mode, no blocking wait):")
                for step in low_risk_steps:
                    print(f"  [PENDING] {step['action']}")
                return

            # --- INTERACTIVE / DEMO MODE ---
            print(f"[Pipeline] {len(low_risk_steps)} step(s) awaiting your "
                  f"confirmation (interactive mode):\n")
            for step in low_risk_steps:
                action = step["action"]
                reason = step.get("reason", "")
                print(f"  Proposed action: {action}")
                print(f"  Reason:          {reason}")

                is_safe, why = validate_command(action)
                if not is_safe:
                    print(f"  [BLOCKED by safety allow-list] {why}\n")
                    self._log_confirmation_decision(step, approved=False,
                                                      reason=f"allow-list rejected: {why}")
                    continue

                resp = input("  Approve execution? [y/N]: ").strip().lower()
                if resp == "y":
                    print(f"  [EXECUTING] {action}")
                    try:
                        result = subprocess.run(
                            action.split(), capture_output=True, text=True,
                            timeout=30,
                        )
                        print(f"  [DONE] exit_code={result.returncode}")
                        if result.stdout:
                            print(f"  stdout: {result.stdout[:500]}")
                        if result.stderr:
                            print(f"  stderr: {result.stderr[:500]}")
                        self._log_confirmation_decision(
                            step, approved=True,
                            reason=f"operator approved; exit_code={result.returncode}",
                        )
                    except Exception as e:
                        print(f"  [EXECUTION ERROR] {e}")
                        self._log_confirmation_decision(
                            step, approved=True, reason=f"execution error: {e}"
                        )
                else:
                    print("  [DECLINED by operator]\n")
                    self._log_confirmation_decision(
                        step, approved=False, reason="operator declined"
                    )
        else:
            print("[Pipeline] HUMAN REVIEW REQUIRED")
            print("[Pipeline] Sending alert...")
            self._send_alert(analysis)

    def _log_confirmation_decision(self, step, approved, reason):
        """Record every confirmation decision (approve / reject / blocked)
        in the experiment log, so a concrete worked example can be traced
        end to end."""
        self.logger.log_cycle(
            scenario=self.scenario,
            run_id=self.run_id,
            cycle_num=self.run_count,
            context={"confirmation_event": True},
            llm_verdict=None,
            pipeline_status=f"CONFIRMATION_{'APPROVED' if approved else 'REJECTED'}",
            confidence_threshold=self.confidence_threshold,
            expected_root_cause=self.expected_root_cause,
            expected_fault_class=self.expected_fault_class,
        )

    def _send_alert(self, analysis):
        print("\n" + "!" * 50)
        print("ALERT: Human intervention required!")
        print(f"Severity: {analysis.get('severity')}")
        print(f"Services: {analysis.get('affected_services')}")
        print(f"Root cause: {analysis.get('root_cause')}")
        print("\nRecommended steps:")
        for step in analysis.get("remediation_steps", []):
            print(f"  {step['step']}. {step['action']}")
        print("!" * 50)

    def _log_current_cycle(self, context, llm_verdict):
        incident = self.memory.get_incident()
        self.logger.log_cycle(
            scenario=self.scenario,
            run_id=self.run_id,
            cycle_num=self.run_count,
            context=context,
            llm_verdict=llm_verdict,
            pipeline_status=incident.get("status"),
            confidence_threshold=self.confidence_threshold,
            expected_root_cause=self.expected_root_cause,
            expected_fault_class=self.expected_fault_class,
        )

    def run_once(self):
        self.run_count += 1
        print(f"\n{'='*60}")
        print(f"AIOPS PIPELINE RUN #{self.run_count} — {datetime.now().strftime('%H:%M:%S')}")
        print(f"Scenario: {self.scenario} | Run ID: {self.run_id} | "
              f"theta={self.confidence_threshold} | "
              f"interactive={self.interactive_confirmation}")
        print(f"{'='*60}")

        metric_result, trace_result, log_result = self.collect_signals()
        context = self.build_context(metric_result, trace_result, log_result)

        if not context.get("requires_action") and \
                context.get("total_anomalies", {}).get("total", 0) == 0:
            print("\n[Pipeline] ✓ All systems normal — no action needed")
            # FIX (found via real S2 data): without this, a genuinely
            # normal cycle after a real incident leaves pipeline_status
            # stuck on whatever it was last set to (e.g.
            # ESCALATED_TO_HUMAN), because this skip branch returns
            # before run_agents()'s status-setting logic ever runs.
            # Confirmed against real S2 runs: 8+ consecutive NORMAL
            # cycles all logged pipeline_status="ESCALATED_TO_HUMAN"
            # even though the incident had clearly resolved. Explicitly
            # clear incident state here too, not just in run_agents().
            self.memory.update_incident(
                active=False, severity="NORMAL", status="MONITORING"
            )
            self.memory.print_summary()
            self._log_current_cycle(context, llm_verdict=None)
            return

        analysis = self.run_agents(context)
        self.execute_remediation(analysis)
        self.memory.print_summary()
        self._log_current_cycle(context, llm_verdict=analysis)

        return analysis

    def run_continuous(self, interval_seconds=30, max_runs=10):
        print(f"\n[Pipeline] Starting continuous monitoring...")
        print(f"[Pipeline] Scenario: {self.scenario} | Run ID: {self.run_id}")
        print(f"[Pipeline] Interval: {interval_seconds}s | Max runs: {max_runs}")
        if self.interactive_confirmation:
            print("[Pipeline] WARNING: interactive confirmation is ON. "
                  "The pipeline will pause at every LOW-risk step and ask "
                  "for real confirmation. Do NOT use this flag for the "
                  "formal automated experiment runs.")

        for i in range(max_runs):
            self.run_once()
            if i < max_runs - 1:
                print(f"\n[Pipeline] Waiting {interval_seconds}s for next run...")
                time.sleep(interval_seconds)

        print("\n[Pipeline] Monitoring complete!")
        print(f"[Pipeline] Logs saved to: experiment_logs/{self.scenario}.jsonl")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["once", "continuous"], default="once")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--scenario", type=str, default="UNLABELLED")
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument("--expected-root-cause", type=str, default=None)
    parser.add_argument(
        "--expected-fault-class", type=str, default=None,
        choices=["resource", "crash", "network", "dependency", "none"],
    )
    parser.add_argument("--confidence-threshold", type=float, default=CONFIDENCE_THRESHOLD)
    parser.add_argument(
        "--interactive", action="store_true",
        help=(
            "Enable the real operator-confirmation prompt (y/n per step, "
            "then the safety allow-list check, then execution). Do NOT use "
            "this flag for the formal experiment runs; it is intended for "
            "manual demonstration and testing only."
        ),
    )
    parser.add_argument(
        "--ablation-suppress", type=str, default=None,
        choices=["metrics", "logs", "traces"],
        help=(
            "Ablation mode: discard one signal type's results for the "
            "target service(s) only, simulating that signal being "
            "unavailable specifically for the fault-relevant service. "
            "Other services' genuine, normal readings remain intact -- "
            "matching a realistic partial-monitoring-gap scenario, not "
            "an unrealistic simultaneous multi-service blackout."
        ),
    )
    parser.add_argument(
        "--ablation-target-service", type=str, default=None,
        help=(
            "Which service's signal to suppress. Defaults to "
            "--expected-root-cause. MUST be set explicitly when the "
            "ground-truth root cause is not itself a monitored service "
            "(e.g. S5: redis-cart is never monitored -- the observable "
            "proxy signal is on cartservice/frontend instead). Accepts "
            "a comma-separated list for multiple target services."
        ),
    )
    args = parser.parse_args()

    pipeline = AIOpsMainPipeline(
        scenario=args.scenario,
        run_id=args.run_id,
        expected_root_cause=args.expected_root_cause,
        expected_fault_class=args.expected_fault_class,
        confidence_threshold=args.confidence_threshold,
        interactive_confirmation=args.interactive,
        ablation_suppress=args.ablation_suppress,
        ablation_target_service=args.ablation_target_service,
    )

    if args.mode == "continuous":
        pipeline.run_continuous(interval_seconds=args.interval, max_runs=args.runs)
    else:
        pipeline.run_once()