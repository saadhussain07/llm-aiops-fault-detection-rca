"""
runtime_anomaly_agent_fixed.py  (v2 -- see CHANGELOG)

Additions to the original runtime_anomaly_agent.py:

1. Self-consistency voting: call the LLM N times (default 5) on the
   IDENTICAL context, take the majority verdict.

2. Metric-silence flag: when a service that is ACTUALLY MONITORED is
   missing from a cycle's telemetry, flag it explicitly so the LLM can
   reason about disappearance-as-signal (targets the paper's E2 / S5
   root-cause misattribution finding).

   IMPORTANT (v2 fix): "monitored" here means "a service
   metric_collector.py actually queries Prometheus for" -- i.e. the
   SAME service list metric_collector.py uses (self.services), NOT a
   separately-hardcoded list that includes services metric_collector
   never scrapes in the first place. v1 of this file hardcoded
   ALL_SERVICES to include "redis-cart", which metric_collector.py's
   self.services list has NEVER included (in the original file or the
   fixed one). That meant "redis-cart" showed up as "silent" on EVERY
   cycle of EVERY scenario, fault or not, and the LLM was reliably
   blaming a phantom redis-cart crash regardless of the real fault
   (confirmed against a real S1 CPU-stress run: the LLM's root cause on
   every cycle was "the redis-cart pod stopped emitting metrics",
   correctly ignoring the ACTUAL cartservice CPU anomaly). Pass the
   real monitored-services list in from the caller (context_builder's
   MetricCollector().services) instead of a second, independent list
   that can drift out of sync.

   If you want redis-cart's silence to be a real, meaningful signal
   for S5 (Cascading Failure), it needs to be added to
   MetricCollector.services and actually scraped -- container-level
   CPU/memory via cAdvisor doesn't require the app to expose its own
   /metrics endpoint, so this should be feasible. Until that's done,
   don't list it here; a permanently-true "silence" flag carries zero
   information and actively misleads root-cause attribution.

CHANGELOG (v1 -> v2):
- FIXED: `result.get("verdict", "ERROR")` in the voting loop silently
  manufactured a fake "ERROR" vote whenever the model's JSON response
  omitted the "verdict" key (a schema-adherence slip, NOT a network
  or API failure). This is indistinguishable from a real error in the
  original code, and can win a majority vote (observed: 4/5 "ERROR"
  votes in a real run, all of which were actually successful API
  calls with full root_cause/evidence/remediation_steps present --
  just missing the verdict key specifically). Fixed by validating the
  required keys explicitly and retrying once before counting a vote
  as unusable, and by tracking schema failures separately from actual
  API/network exceptions so they're never silently conflated.
- FIXED: quota-exhaustion detection (the `quota_exhausted` flag that
  main.py checks to gracefully sys.exit(2) and pause a whole batch)
  was dropped when this file replaced the original single-call agent.
  Restored: any exception is inspected for the same
  "rate_limit_exceeded" + "tokens per day" signature the original
  agent checked for, and quota_exhausted is propagated up through
  analyze() so main.py's existing safety mechanism still works.
- FIXED: redis-cart false-silence, see above.
"""

import os
import json
from collections import Counter
from datetime import datetime
from groq import Groq
from dotenv import load_dotenv

load_dotenv()

REQUIRED_KEYS = ["verdict", "confidence", "severity", "root_cause"]


def _is_quota_exhausted(error_str: str) -> bool:
    return "rate_limit_exceeded" in error_str and "tokens per day" in error_str


class RuntimeAnomalyAgent:
    def __init__(self, n_votes: int = 5, monitored_services=None):
        """
        monitored_services: pass the SAME list metric_collector.py
        actually queries (e.g. MetricCollector().services), so the
        metric-silence check only flags services that are genuinely
        expected to report telemetry. Do not hardcode a second list
        here -- it will drift out of sync with the collector, as v1
        of this file did with redis-cart.
        """
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY"))
        self.model = "openai/gpt-oss-120b"
        self.conversation_history = []
        self.n_votes = n_votes
        self.monitored_services = monitored_services or [
            "frontend", "cartservice", "currencyservice",
            "paymentservice", "productcatalogservice"
        ]

        self.system_prompt = """You are an expert AIOps Runtime Anomaly Detection Agent
for a Kubernetes cloud-native environment.

Your role is to:
1. Analyze runtime telemetry data (metrics, traces, logs)
2. Identify genuine anomalies vs normal behavior
3. Determine root cause of detected anomalies
4. Recommend specific, actionable remediation steps
5. Assess confidence level of your analysis

You have access to data from:
- Prometheus metrics (CPU, memory, error rates)
- Jaeger distributed traces (latency, errors)
- Loki log streams (error patterns, warnings)

Services in the cluster:
- frontend (HTTP, port 8080)
- cartservice (gRPC, port 7070, uses Redis)
- currencyservice (gRPC, port 7000)
- paymentservice (gRPC, port 50051)
- productcatalogservice (gRPC, port 3550)
- redis-cart (Redis, port 6379)

IMPORTANT: If a service that is normally expected to report metrics is
MISSING from the telemetry entirely (metrics_present: false), treat that
absence itself as evidence -- a terminated or crashed pod stops emitting
metrics, which is a distinct signature from a running pod with normal
metrics. Do not default root-cause attribution to whichever service DOES
have visible anomalous metrics if a silent service is a more likely
origin (e.g. a dependency the visible service calls).

Always respond in this exact JSON format:
{
  "verdict": "ANOMALY_CONFIRMED" or "FALSE_POSITIVE" or "NORMAL",
  "confidence": 0.0 to 1.0,
  "severity": "CRITICAL" or "HIGH" or "MEDIUM" or "LOW" or "NORMAL",
  "affected_services": ["service1", "service2"],
  "root_cause": "Detailed explanation of root cause",
  "evidence": ["evidence1", "evidence2"],
  "remediation_steps": [
    {"step": 1, "action": "specific kubectl command", "reason": "...", "risk": "LOW/MEDIUM/HIGH"}
  ],
  "auto_remediate": true or false,
  "escalate_to_human": true or false,
  "explanation": "Human readable summary"
}"""

    # ---------- metric-silence encoding ----------

    def _inject_metric_silence(self, context):
        """
        Add explicit metrics_present: false entries for any MONITORED
        service that produced no metrics this cycle, so the LLM sees
        silence as a signal rather than the service simply vanishing
        from the context. Only checks self.monitored_services -- a
        service the collector never scrapes to begin with is not
        "silent," it's out of scope, and must not be reported as if
        it just went missing.
        """
        full_metrics = context.get("full_metrics", [])
        reporting = {m["service"] for m in full_metrics}
        silent = [s for s in self.monitored_services if s not in reporting]

        context = dict(context)  # shallow copy, don't mutate caller's dict
        context["silent_services"] = silent
        if silent:
            context["summary_for_llm"] = (
                context.get("summary_for_llm", "")
                + f" SILENT_SERVICES (no telemetry this cycle, possible "
                  f"termination): {silent}."
            )
        return context

    # ---------- single LLM call (unchanged logic, refactored out) ----------

    def _validate_schema(self, result: dict):
        """
        Returns (ok, missing_keys). A response missing required keys is
        a genuine model schema-adherence failure -- distinct from a
        network/API exception -- and must be surfaced as such, not
        silently relabeled "ERROR" and counted as an equal vote.
        """
        missing = [k for k in REQUIRED_KEYS if k not in result]
        return (len(missing) == 0, missing)

    def _single_call(self, context):
        llm_summary = context.get("summary_for_llm", "")
        affected = context.get("affected_services", [])
        full_metrics = context.get("full_metrics", [])
        full_logs = context.get("full_logs", [])
        anomalous_metrics = [m for m in full_metrics if m.get("is_anomaly")]
        anomalous_logs = [l for l in full_logs if l.get("is_anomaly")]
        silent_services = context.get("silent_services", [])

        user_message = f"""
Analyze the following runtime telemetry from our Kubernetes cluster:

{llm_summary}

Additional context:
- Total metric anomalies: {context.get('total_anomalies', {}).get('metrics', 0)}
- Total trace anomalies: {context.get('total_anomalies', {}).get('traces', 0)}
- Total log anomalies: {context.get('total_anomalies', {}).get('logs', 0)}
- Affected services: {affected}
- Silent services (no telemetry this cycle): {silent_services}
- Requires immediate action: {context.get('requires_action', False)}

Anomalous metrics (only flagged services):
{json.dumps(anomalous_metrics, indent=2)}

Anomalous logs (only flagged services):
{json.dumps(anomalous_logs, indent=2)}

Please analyze and provide your verdict in the exact JSON format specified.
"""
        messages = [{"role": "system", "content": self.system_prompt}]
        messages += self.conversation_history[-4:]
        messages.append({"role": "user", "content": user_message})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=3000,
            temperature=0.1
        )
        response_text = response.choices[0].message.content

        try:
            start = response_text.find("{")
            end = response_text.rfind("}") + 1
            result = json.loads(response_text[start:end]) if start != -1 and end > start else {"raw_response": response_text}
        except json.JSONDecodeError:
            result = {"raw_response": response_text}

        return result, user_message, response_text

    # ---------- self-consistency voting ----------

    def analyze(self, context):
        """
        Runs n_votes independent calls on the SAME context and takes the
        majority verdict. Confidence is averaged across votes that agree
        with the majority verdict (a form of agreement-weighted
        confidence, reported alongside raw vote counts so this is
        auditable, not just a black-box average).
        """
        print(f"\n[RuntimeAnomalyAgent] Analyzing at {datetime.now().strftime('%H:%M:%S')} "
              f"with {self.n_votes}-way self-consistency voting")

        context = self._inject_metric_silence(context)

        votes = []            # only real verdicts (schema-valid responses)
        raw_results = []      # only real verdicts
        schema_failures = 0   # model returned JSON but missing required keys
        api_errors = []       # genuine network/API exceptions
        quota_exhausted = False

        for i in range(self.n_votes):
            try:
                result, user_msg, raw_text = self._single_call(context)
            except Exception as e:
                error_str = str(e)
                print(f"  vote {i+1}/{self.n_votes}: API EXCEPTION: {error_str}")
                api_errors.append(error_str)
                if _is_quota_exhausted(error_str):
                    quota_exhausted = True
                    print(f"  [QUOTA] Daily token quota signature detected, "
                          f"stopping remaining votes for this cycle.")
                    break
                continue

            ok, missing = self._validate_schema(result)
            if not ok:
                # ONE retry on schema failure, since this is a model
                # sampling issue, not an infrastructure issue -- don't
                # burn it as an unusable vote without a second try.
                print(f"  vote {i+1}/{self.n_votes}: SCHEMA INCOMPLETE "
                      f"(missing {missing}), retrying once...")
                try:
                    result, user_msg, raw_text = self._single_call(context)
                    ok, missing = self._validate_schema(result)
                except Exception as e:
                    error_str = str(e)
                    api_errors.append(error_str)
                    if _is_quota_exhausted(error_str):
                        quota_exhausted = True
                        break
                    ok = False

            if not ok:
                schema_failures += 1
                print(f"  vote {i+1}/{self.n_votes}: SCHEMA INCOMPLETE after "
                      f"retry (missing {missing}) -- excluded from vote tally")
                continue

            verdict = result["verdict"]
            votes.append(verdict)
            raw_results.append(result)
            print(f"  vote {i+1}/{self.n_votes}: {verdict} "
                  f"(confidence={result.get('confidence')})")

        if quota_exhausted:
            return {
                "verdict": "ERROR",
                "quota_exhausted": True,
                "error": "Groq daily token quota exhausted mid-voting",
                "timestamp": datetime.now().isoformat(),
            }

        if not votes:
            # every vote either errored or failed schema validation twice
            return {
                "verdict": "ERROR",
                "quota_exhausted": False,
                "error": f"No usable votes this cycle "
                         f"({schema_failures} schema failures, "
                         f"{len(api_errors)} API errors)",
                "schema_failures": schema_failures,
                "api_errors": api_errors,
                "timestamp": datetime.now().isoformat(),
            }

        vote_counts = Counter(votes)
        majority_verdict, majority_count = vote_counts.most_common(1)[0]

        agreeing = [r for r in raw_results if r.get("verdict") == majority_verdict]
        confidences = [r.get("confidence") for r in agreeing if isinstance(r.get("confidence"), (int, float))]
        avg_confidence = sum(confidences) / len(confidences) if confidences else None

        # Use the highest-confidence agreeing response as the "representative"
        # result for root_cause / remediation_steps / evidence, since those
        # are free-text/structured fields that don't majority-vote cleanly.
        representative = max(agreeing, key=lambda r: r.get("confidence", 0)) if agreeing else raw_results[0]

        final_result = dict(representative)
        final_result["verdict"] = majority_verdict
        final_result["confidence"] = avg_confidence
        final_result["vote_agreement"] = f"{majority_count}/{len(votes)}"
        final_result["votes_attempted"] = self.n_votes
        final_result["votes_usable"] = len(votes)
        final_result["schema_failures"] = schema_failures
        final_result["api_errors"] = api_errors
        final_result["all_votes"] = votes
        final_result["timestamp"] = datetime.now().isoformat()
        final_result["model_used"] = self.model
        final_result["input_severity"] = context.get("severity", "UNKNOWN")

        # update conversation history with a compact summary, not all N calls,
        # to keep the bounded-history window meaningful
        self.conversation_history.append({
            "role": "user",
            "content": f"[cycle summary] {context.get('summary_for_llm', '')}"
        })
        self.conversation_history.append({
            "role": "assistant",
            "content": f"Majority verdict: {majority_verdict} ({majority_count}/{self.n_votes} votes)"
        })
        self.conversation_history = self.conversation_history[-4:]

        return final_result

    def write_to_shared_memory(self, analysis_result, shared_memory):
        shared_memory["runtime_anomaly_agent"] = {
            "timestamp": datetime.now().isoformat(),
            "verdict": analysis_result.get("verdict"),
            "confidence": analysis_result.get("confidence"),
            "vote_agreement": analysis_result.get("vote_agreement"),
            "schema_failures": analysis_result.get("schema_failures"),
            "severity": analysis_result.get("severity"),
            "affected_services": analysis_result.get("affected_services", []),
            "root_cause": analysis_result.get("root_cause"),
            "remediation_steps": analysis_result.get("remediation_steps", []),
            "auto_remediate": analysis_result.get("auto_remediate", False),
            "escalate_to_human": analysis_result.get("escalate_to_human", True),
            "full_analysis": analysis_result
        }
        print(f"[RuntimeAnomalyAgent] Written to shared memory")
        return shared_memory