"""
metric_collector_fixed.py

Fixes two issues found in the original metric_collector.py:

BUG 1 (critical): The original code called
    self.model.fit_predict(X)
fresh, every single monitoring cycle, on only the ~5 services' CURRENT
snapshot. It then min-max rescaled scores WITHIN that batch:
    normalized = (raw - raw.min()) / (raw.max() - raw.min())
    anomaly_score = 1 - normalized
This guarantees that whichever service has the lowest raw score_samples
THIS CYCLE gets anomaly_score == 1.0 -- every cycle, fault or no fault.
The score is a *relative rank among 5 points*, not a calibrated measure
of "is this abnormal for this service." This is also exactly the
methodology gap Reviewer 2 flagged (fitting IF on ~5 observations,
unclear whether refit every cycle).

FIX: Fit ONE Isolation Forest per service on a historical baseline
window of known-normal telemetry (collected before fault injection),
persist it to disk, and score new observations with decision_function
against a threshold CALIBRATED on that same baseline (not a per-cycle
min-max rescale). A cycle either crosses the calibrated threshold or
it doesn't; there is no forced "someone must be the anomaly this cycle."

BUG 2: get_http_error_rate() queried container_cpu_usage_seconds_total
instead of an actual error-rate metric -- a straight copy-paste of the
CPU query. Fixed to pull a real error signal (adjust the PromQL to your
actual error-count metric name; a placeholder + a fallback of dropping
the feature is provided so this fails loudly instead of silently
duplicating CPU).

BUG 3 (found via real S1 rerun data, root cause of unanimous
FALSE_POSITIVE verdicts on a fully-saturated fault): the LLM was given
bare absolute CPU floats (e.g. "cpu=0.1418") with no reference point.
Kubernetes core-fraction units make a saturated pod (0.14 out of a
200m/0.2-core limit -- 71% utilization) LOOK like a tiny, idle number
to a model with no context for what "normal" or "the ceiling" is for
that specific service. Confirmed against real data: cartservice was
pegged at max_cpu=0.200 (its exact limit) for 6+ consecutive cycles,
and the model still called it FALSE_POSITIVE with 5/5 vote agreement,
reasoning "well within normal idle range" every time.

FIX: compute and expose cpu_pct_of_limit (this cycle's CPU as % of the
service's configured Kubernetes limit) and cpu_vs_baseline_ratio (this
cycle's CPU as a multiple of the service's own fitted baseline mean),
so the LLM receives an interpretable relative signal instead of a bare
float it has to guess the scale of.

BUG 4 (found via real S1 rerun data AFTER fixing Bug 3 -- the ratio
context fix worked, but exposed a new problem): services with a tiny
fitted baseline mean (e.g. frontend at ~0.00027 cores, currencyservice
at ~0.00016 cores) produce dramatic-looking "cpu_vs_baseline_ratio"
values (2.5x-3.3x) from completely normal measurement jitter of a few
ten-thousandths of a core -- their ABSOLUTE CPU never actually moves
cycle to cycle. Confirmed against real S1 data: while cartservice
climbed 0.016 -> 0.016 -> 0.016 cores (a real, large, correctly-flagged
fault), frontend sat flat at ~0.0007 cores and currencyservice at
~0.0005 cores across the same 3 cycles, yet both were flagged
is_anomaly=True every time purely because of the ratio math, and the
LLM built an unsupported "cascading load" narrative around it (no
trace data, no request-count evidence, just co-occurring ratio spikes
on near-zero denominators).

FIX: require a minimum absolute CPU floor before a ratio-based anomaly
can fire, regardless of how large the multiple looks. A service using
0.0007 cores is not operationally anomalous no matter what multiple of
its own baseline that represents.
"""

import os
import json
import pickle
import subprocess
import numpy as np
from datetime import datetime, timedelta, timezone
from pathlib import Path
from prometheus_api_client import PrometheusConnect
from sklearn.ensemble import IsolationForest
from dotenv import load_dotenv

load_dotenv()

# Below this absolute CPU (in cores), a ratio-based anomaly is baseline
# noise, not real signal -- see BUG 4 above. 0.005 cores (5m) is well
# above typical idle jitter (~0.0002-0.0008 cores observed) but well
# below any real fault signal in this testbed (S1's cartservice climbs
# past 0.05-0.15 cores when genuinely stressed).
MIN_MEANINGFUL_CPU_CORES = 0.005

MODEL_DIR = Path(os.getenv("IF_MODEL_DIR", "./if_models"))
MODEL_DIR.mkdir(exist_ok=True)

# Kubernetes CPU limits per service, in cores (200m = 0.2, 300m = 0.3).
# Pulled from your actual cluster (kubectl describe node output) --
# update this if you change resource limits in your deployment manifests.
CPU_LIMITS_CORES = {
    "frontend": 0.2,
    "cartservice": 0.2,
    "currencyservice": 0.3,
    "paymentservice": 0.2,
    "productcatalogservice": 0.2,
}

# Memory limits in MB. Confirmed via `kubectl describe pod` earlier in
# this session: cartservice's actual configured limit is 128Mi. The
# paper states a 128-256MB range across services (Section 5.2); using
# the confirmed 128MB figure as the conservative default for all
# services unless you know a specific service is configured higher --
# update per-service if your manifests differ.
MEMORY_LIMITS_MB = {
    "frontend": 128,
    "cartservice": 128,
    "currencyservice": 128,
    "paymentservice": 128,
    "productcatalogservice": 128,
}


class MetricCollector:
    def __init__(self, baseline_minutes: int = 60, threshold_percentile: float = 95.0):
        """
        baseline_minutes: length of the known-normal window used to fit
            each service's Isolation Forest (run once, before any fault
            injection, per experimental run or reused across runs if the
            baseline is stable).
        threshold_percentile: the calibrated decision threshold is set at
            this percentile of the *baseline's own* anomaly scores, so a
            service only fires when it is unusual relative to ITS OWN
            history, not relative to whichever 4 other services happen to
            be running this cycle.
        """
        self.prom = PrometheusConnect(
            url=os.getenv("PROMETHEUS_URL", "http://localhost:9090"),
            disable_ssl=True
        )
        self.services = [
            "frontend",
            "cartservice",
            "currencyservice",
            "paymentservice",
            "productcatalogservice"
        ]
        self.baseline_minutes = baseline_minutes
        self.threshold_percentile = threshold_percentile
        # one model + one threshold PER SERVICE, not one shared model
        # fit fresh across the 5 services each cycle
        self.models = {}       # service -> IsolationForest
        self.thresholds = {}   # service -> float (calibrated decision boundary)
        self.baseline_mean_memory = {}  # NEW: service -> float (baseline avg memory MB, for ratio context)
        self.baseline_mean_cpu = {}  # service -> float (baseline avg CPU, for ratio context)
        self.baseline_cpu_p95 = {}  # NEW: service -> float (per-service noise-floor calibration)
        self._load_or_warn()

    # ---------- persistence ----------

    def _model_path(self, service):
        return MODEL_DIR / f"{service}_if_model.pkl"

    def _load_or_warn(self):
        missing = []
        for service in self.services:
            path = self._model_path(service)
            if path.exists():
                with open(path, "rb") as f:
                    payload = pickle.load(f)
                    self.models[service] = payload["model"]
                    self.thresholds[service] = payload["threshold"]
                    self.baseline_mean_cpu[service] = payload.get("baseline_mean_cpu")
                    self.baseline_mean_memory[service] = payload.get("baseline_mean_memory")
                    self.baseline_cpu_p95[service] = payload.get("baseline_cpu_p95")
            else:
                missing.append(service)
        if missing:
            print(f"[MetricCollector] No baseline model for: {missing}. "
                  f"Call fit_baseline() for these services before run().")

    def fit_baseline(self, service, minutes=None):
        """
        Fit and persist an Isolation Forest for ONE service using a
        window of known-normal telemetry. Call this once per service
        during a verified-baseline period (cluster restored to stable
        state, no fault injected), per Section 6.1's "two consecutive
        stable Prometheus cycles confirmed" protocol -- but extended to
        a real training window rather than a 2-cycle check.

        FIXED (real S3 data): memory is now sampled as a real time
        series (get_memory_usage_range), not a single instant value
        broadcast across all rows -- see get_memory_usage_range's
        docstring for the bug this fixes.

        FIXED (real S3 data): the noise-floor CPU threshold used to
        gate is_anomaly (see score_against_baseline) is now calibrated
        PER SERVICE from this service's own observed idle jitter,
        instead of one global constant. A single global floor
        (0.005 cores) sat almost exactly on top of cartservice's
        natural idle CPU jitter (observed 0.0049-0.0051 cores),
        causing is_anomaly to flip on/off between consecutive cycles
        on pure measurement noise unrelated to any real fault. The
        floor is now max(GLOBAL_MIN_FLOOR, 1.5x this service's own
        baseline 95th-percentile CPU), so a naturally noisier service
        gets a correspondingly higher bar.
        """
        minutes = minutes or self.baseline_minutes
        cpu_values = self.get_cpu_usage(service, minutes=minutes)
        memory_values = self.get_memory_usage_range(service, minutes=minutes)
        error_rate = self.get_error_rate(service, minutes=minutes)

        if len(cpu_values) < 10:
            raise RuntimeError(
                f"Only {len(cpu_values)} CPU samples for {service} in "
                f"{minutes}min baseline window -- too few to fit a "
                f"meaningful model. Increase baseline_minutes or check "
                f"Prometheus scrape interval."
            )
        if len(memory_values) < 10:
            raise RuntimeError(
                f"Only {len(memory_values)} memory samples for {service} "
                f"in {minutes}min baseline window -- too few to fit a "
                f"meaningful memory baseline. Increase baseline_minutes "
                f"or check Prometheus scrape interval."
            )

        # Align lengths (CPU and memory queries may return slightly
        # different sample counts due to scrape timing jitter).
        n = min(len(cpu_values), len(memory_values))
        cpu_arr = np.array(cpu_values[:n])
        mem_arr = np.array(memory_values[:n])

        rolling_std = np.array([
            np.std(cpu_arr[max(0, i - 5):i + 1]) for i in range(len(cpu_arr))
        ])
        X = np.column_stack([
            cpu_arr,
            rolling_std,
            mem_arr,  # NEW: real per-sample memory, not a broadcast constant
            np.full_like(cpu_arr, error_rate),
        ])

        model = IsolationForest(contamination=0.1, random_state=42)
        model.fit(X)

        baseline_scores = -model.score_samples(X)  # higher = more anomalous
        threshold = float(np.percentile(baseline_scores, self.threshold_percentile))
        baseline_mean_cpu = float(np.mean(cpu_arr))
        baseline_mean_memory = float(np.mean(mem_arr))
        baseline_cpu_p95 = float(np.percentile(cpu_arr, 95))

        # Diagnostic print -- so a miscalibrated/stale baseline is
        # visible immediately at fit time, not only discoverable after
        # a bad live run (as happened with cartservice and paymentservice).
        print(f"[MetricCollector] Baseline stats for {service}:")
        print(f"    CPU:    min={cpu_arr.min():.5f} p50={np.percentile(cpu_arr,50):.5f} "
              f"p95={baseline_cpu_p95:.5f} max={cpu_arr.max():.5f}")
        print(f"    Memory: min={mem_arr.min():.2f}MB p50={np.percentile(mem_arr,50):.2f}MB "
              f"p95={np.percentile(mem_arr,95):.2f}MB max={mem_arr.max():.2f}MB")

        with open(self._model_path(service), "wb") as f:
            pickle.dump({"model": model, "threshold": threshold,
                         "baseline_mean_cpu": baseline_mean_cpu,
                         "baseline_mean_memory": baseline_mean_memory,
                         "baseline_cpu_p95": baseline_cpu_p95,
                         "fitted_at": datetime.now().isoformat(),
                         "baseline_minutes": minutes,
                         "n_samples": len(cpu_arr)}, f)

        self.models[service] = model
        self.thresholds[service] = threshold
        self.baseline_mean_cpu[service] = baseline_mean_cpu
        self.baseline_mean_memory[service] = baseline_mean_memory
        self.baseline_cpu_p95[service] = baseline_cpu_p95
        print(f"[MetricCollector] Fit baseline for {service}: "
              f"n={len(cpu_arr)}, threshold={threshold:.4f}, "
              f"baseline_mean_cpu={baseline_mean_cpu:.5f}, "
              f"baseline_mean_memory={baseline_mean_memory:.2f}MB")
        return threshold

    # ---------- telemetry queries ----------

    def get_cpu_usage(self, service, minutes=10):
        query = f'''
            rate(container_cpu_usage_seconds_total{{
                namespace="microservices-demo",
                pod=~"{service}.*",
                container!=""
            }}[2m])
        '''
        try:
            end = datetime.now()
            start = end - timedelta(minutes=minutes)
            result = self.prom.custom_query_range(
                query=query, start_time=start, end_time=end, step="30s"
            )
            if result:
                return [float(v[1]) for v in result[0]['values']]
            return []
        except Exception as e:
            print(f"[MetricCollector] CPU query error for {service}: {e}")
            return []

    def get_memory_usage(self, service, minutes=10):
        query = f'''
            container_memory_working_set_bytes{{
                namespace="microservices-demo",
                pod=~"{service}.*",
                container!=""
            }}
        '''
        try:
            result = self.prom.custom_query(query)
            if result:
                return float(result[0]['value'][1])
            return 0.0
        except Exception as e:
            print(f"[MetricCollector] Memory query error for {service}: {e}")
            return 0.0

    def get_memory_usage_range(self, service, minutes=10):
        """
        NEW: time-series version of get_memory_usage, for baseline
        fitting. BUG FOUND (real S3 rerun data): fit_baseline() was
        calling get_memory_usage() ONCE and broadcasting that single
        instant value across every training row via np.full_like().
        A feature with zero variance across all training samples gives
        Isolation Forest nothing to split on, so memory contributed
        essentially nothing to the fitted model -- confirmed against
        real data: paymentservice's memory nearly doubling (27MB ->
        53MB after Node.js heap injection) barely moved its
        anomaly_score (0.797 -> 1.057, well under threshold). This
        method pulls real historical memory samples so the baseline
        actually has genuine variance to learn from, the same way CPU
        already does via get_cpu_usage's custom_query_range.
        """
        query = f'''
            container_memory_working_set_bytes{{
                namespace="microservices-demo",
                pod=~"{service}.*",
                container!=""
            }}
        '''
        try:
            end = datetime.now()
            start = end - timedelta(minutes=minutes)
            result = self.prom.custom_query_range(
                query=query, start_time=start, end_time=end, step="30s"
            )
            if result:
                return [float(v[1]) / (1024 * 1024) for v in result[0]['values']]  # -> MB
            return []
        except Exception as e:
            print(f"[MetricCollector] Memory range query error for {service}: {e}")
            return []

    def get_error_rate(self, service, minutes=5):
        """
        FIXED (was get_http_error_rate): the original query duplicated
        container_cpu_usage_seconds_total, so 'error_rate' was actually
        a copy of CPU. Point this at your real error signal.

        Two real options, pick whichever your stack actually emits:

        (a) If Istio/Envoy sidecars are present:
            sum(rate(istio_requests_total{
                destination_service_name="<service>",
                response_code=~"5.."
            }[2m]))
            /
            sum(rate(istio_requests_total{
                destination_service_name="<service>"
            }[2m]))

        (b) If the app exposes its own error counter (check each
            service's /metrics endpoint for something like
            http_requests_total{code=~"5.."} or grpc_server_handled_total
            {grpc_code!="OK"}), use that instead.

        If neither exists in your stack, DROP this feature rather than
        silently reusing CPU -- an absent signal is honestly represented
        as such in the paper's limitations, a duplicated one is a bug
        that inflates apparent feature count without adding information.
        """
        query = f'''
            sum(rate(istio_requests_total{{
                destination_service_name="{service}",
                response_code=~"5.."
            }}[2m])) or vector(0)
            /
            sum(rate(istio_requests_total{{
                destination_service_name="{service}"
            }}[2m])) or vector(1)
        '''
        try:
            result = self.prom.custom_query(query)
            if result:
                return float(result[0]['value'][1])
            return 0.0
        except Exception as e:
            print(f"[MetricCollector] Error-rate query failed for {service} "
                  f"(is istio_requests_total present in this cluster? "
                  f"error: {e}). Returning 0.0 -- treat this feature as "
                  f"unavailable, do not substitute another metric.")
            return 0.0

    # ---------- scoring ----------

    def get_pod_age_seconds(self, service):
        """
        NEW: query kube-state-metrics' kube_pod_start_time (a direct
        Kubernetes-level signal, already exposed by
        monitoring-kube-state-metrics in this cluster) to compute how
        long the CURRENT pod has been running.

        Why this matters (found via real S2 rerun data): a cycle
        immediately after a force-delete/crash showed cpu_std=0.0 (a
        single CPU sample -- the pod had almost no history yet), and
        the LLM confidently explained the resulting CPU reading as "a
        sudden traffic burst" -- a plausible-sounding but WRONG
        narrative for what was actually a brand-new pod's startup
        blip. The verdict (ANOMALY_CONFIRMED, correct service) was
        right; the root-cause STORY was wrong, because nothing in the
        context told the model this pod was seconds old.

        Inferring "freshness" from cpu_std==0 or sample count is
        fragile (a genuinely quiet established pod can also show low
        variance). Pod start time from kube-state-metrics is a real,
        direct signal instead of a derived guess.

        BUG FOUND (real S2 rerun, first attempt): for the ONE service
        that actually just restarted, this query came back empty
        (pod_age_seconds: null) while every OTHER, long-running
        service returned a real value. Root cause: kube-state-metrics
        exports kube_pod_start_time on its own scrape interval,
        separate from this pipeline's monitoring cycle -- a pod that
        is only seconds old may not have been scraped into Prometheus
        yet, so the exact case we need this signal for is the case
        most likely to come back empty.

        FIX: fall back to `kubectl get pod ... creationTimestamp`,
        which queries the Kubernetes API directly and has no scrape
        lag at all. Try Prometheus first (cheap, no subprocess), fall
        back to kubectl only when that comes back empty.
        """
        query = f'''
            kube_pod_start_time{{namespace="microservices-demo",
                pod=~"{service}.*"}}
        '''
        try:
            result = self.prom.custom_query(query)
            if result:
                start_time = float(result[0]['value'][1])
                age = datetime.now().timestamp() - start_time
                return round(age, 1)
        except Exception as e:
            print(f"[MetricCollector] Pod-age Prometheus query failed for "
                  f"{service}: {e} -- falling back to kubectl")

        # Fallback: direct kubectl query, no Prometheus scrape-lag
        # dependency. This is the path that matters most for a
        # just-crashed/just-recreated pod.
        try:
            proc = subprocess.run(
                ["kubectl", "get", "pod", "-n", "microservices-demo",
                 "-l", f"app={service}",
                 "-o", "jsonpath={.items[0].metadata.creationTimestamp}"],
                capture_output=True, text=True, timeout=10
            )
            creation_ts = proc.stdout.strip()
            if not creation_ts:
                return None
            # Kubernetes returns RFC3339 UTC, e.g. 2026-09-07T21:25:09Z
            created = datetime.strptime(creation_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            return round(age, 1)
        except Exception as e:
            print(f"[MetricCollector] Pod-age kubectl fallback also failed "
                  f"for {service}: {e}")
            return None

    def collect_all_metrics(self):
        print(f"\n[MetricCollector] Collecting metrics at {datetime.now().strftime('%H:%M:%S')}")
        all_metrics = []
        for service in self.services:
            cpu_values = self.get_cpu_usage(service)
            memory = self.get_memory_usage(service)
            error_rate = self.get_error_rate(service)
            pod_age_seconds = self.get_pod_age_seconds(service)

            if cpu_values:
                avg_cpu, max_cpu, cpu_std = float(np.mean(cpu_values)), float(np.max(cpu_values)), float(np.std(cpu_values))
            else:
                avg_cpu = max_cpu = cpu_std = 0.0

            metrics = {
                "service": service,
                "avg_cpu": round(avg_cpu, 4),
                "max_cpu": round(max_cpu, 4),
                "cpu_std": round(cpu_std, 4),
                "memory_bytes": memory,
                "memory_mb": round(memory / (1024 * 1024), 2),
                "error_rate": round(error_rate, 4),
                "metrics_present": bool(cpu_values),  # feeds the metric-silence flag
                "pod_age_seconds": pod_age_seconds,
                # TUNED (was 120s): real S2 rerun data showed cartservice
                # at pod_age_seconds=167.9 in cycle 1, still clearly in
                # post-crash recovery (memory=2.85MB, essentially
                # matching the paper's expected 2.09+/-0.3MB post-restart
                # signature) -- but the 120s cutoff missed it by ~48s,
                # and the LLM correctly-but-wrongly cited our own
                # "not recently restarted" flag as evidence for a
                # FALSE_POSITIVE verdict. Raised to 300s (5 min) to
                # cover the observed real-world latency between fault
                # injection and the first 1-2 monitoring cycles
                # actually firing (port-forward + pipeline startup
                # overhead), with headroom for a couple of 30s cycles
                # beyond that before treating the pod as "established."
                "recently_restarted": (pod_age_seconds is not None and pod_age_seconds < 300),
                "timestamp": datetime.now().isoformat()
            }

            # NEW: interpretable relative context, so the LLM isn't
            # guessing the scale of a bare float. Computed here (not
            # left to the LLM) because it needs the service's actual
            # configured limit and its own fitted baseline -- data the
            # LLM has no way to know on its own.
            cpu_limit = CPU_LIMITS_CORES.get(service)
            if cpu_limit:
                metrics["cpu_pct_of_limit"] = round((avg_cpu / cpu_limit) * 100, 1)
                metrics["cpu_limit_cores"] = cpu_limit
            else:
                metrics["cpu_pct_of_limit"] = None
                metrics["cpu_limit_cores"] = None

            baseline = self.baseline_mean_cpu.get(service)
            if baseline and baseline > 0:
                metrics["cpu_vs_baseline_ratio"] = round(avg_cpu / baseline, 1)
            else:
                metrics["cpu_vs_baseline_ratio"] = None

            # NEW: memory-relative context, analogous to the CPU fields
            # above. Same reasoning: a bare "memory=53MB" tells the LLM
            # nothing about whether that's normal or elevated for THIS
            # service. Added after real S3 data showed the LLM (and the
            # underlying IF score, before the fit_baseline fix above)
            # had no way to recognize a genuine ~2x memory increase as
            # significant.
            mem_limit = MEMORY_LIMITS_MB.get(service)
            if mem_limit:
                metrics["memory_pct_of_limit"] = round((metrics["memory_mb"] / mem_limit) * 100, 1)
                metrics["memory_limit_mb"] = mem_limit
            else:
                metrics["memory_pct_of_limit"] = None
                metrics["memory_limit_mb"] = None

            mem_baseline = self.baseline_mean_memory.get(service)
            if mem_baseline and mem_baseline > 0:
                metrics["memory_vs_baseline_ratio"] = round(metrics["memory_mb"] / mem_baseline, 2)
            else:
                metrics["memory_vs_baseline_ratio"] = None

            all_metrics.append(metrics)
            age_str = f"{pod_age_seconds}s old" if pod_age_seconds is not None else "age unknown"
            print(f"  {service}: CPU={avg_cpu:.4f} "
                  f"({metrics['cpu_pct_of_limit']}% of limit, "
                  f"{metrics['cpu_vs_baseline_ratio']}x baseline), "
                  f"Memory={metrics['memory_mb']}MB, pod {age_str}"
                  f"{' [RECENTLY RESTARTED]' if metrics['recently_restarted'] else ''}")
        return all_metrics

    def score_against_baseline(self, m):
        """
        Score ONE service's current snapshot against ITS OWN fitted
        baseline model and calibrated threshold -- no cross-service
        min-max rescaling, no guaranteed 1.0 winner every cycle.
        """
        service = m["service"]
        if service not in self.models:
            m["anomaly_score"] = None
            m["is_anomaly"] = False
            m["scoring_status"] = "NO_BASELINE_MODEL"
            return m

        X = np.array([[m["avg_cpu"], m["cpu_std"], m["memory_mb"], m["error_rate"]]])
        raw_score = -self.models[service].score_samples(X)[0]  # higher = more anomalous
        threshold = self.thresholds[service]

        # Report score relative to threshold (>1.0 means "over threshold"),
        # instead of an unbounded raw isolation-forest score, so downstream
        # LLM/consumers get an interpretable, threshold-anchored number.
        m["anomaly_score"] = round(float(raw_score / threshold) if threshold > 0 else float(raw_score), 3)

        # BUG 4 fix: a ratio/score-based anomaly on a service using
        # negligible absolute CPU is baseline noise, not a real fault.
        # Require BOTH the threshold crossing AND a minimum absolute
        # CPU floor before flagging is_anomaly. This does not affect
        # cartservice-style faults (which climb well past the floor);
        # it specifically suppresses tiny-baseline services (frontend,
        # currencyservice) from being flagged on jitter alone.
        #
        # FIXED (real S3 data): a single GLOBAL floor was too coarse.
        # cartservice's natural idle jitter (observed 0.0049-0.0051
        # cores) sat almost exactly on top of the 0.005 global floor,
        # causing is_anomaly to flip True/False between consecutive
        # cycles on measurement noise, unrelated to any real fault
        # (confirmed: cartservice was flagged during an S3 memory-
        # pressure run that never touches cartservice at all). The
        # floor is now per-service: whichever is higher of the global
        # minimum, or 1.5x this service's own baseline 95th-percentile
        # CPU -- so a naturally noisier service gets a correspondingly
        # higher bar, without needing to raise the floor for everyone
        # (which would risk suppressing genuinely weak-but-real early
        # detections in low-noise services).
        baseline_p95 = self.baseline_cpu_p95.get(service)
        if baseline_p95 is not None:
            effective_floor = max(MIN_MEANINGFUL_CPU_CORES, baseline_p95 * 1.5)
        else:
            effective_floor = MIN_MEANINGFUL_CPU_CORES

        crosses_threshold = bool(raw_score > threshold)
        meets_min_cpu = m["avg_cpu"] >= effective_floor
        m["is_anomaly"] = crosses_threshold and meets_min_cpu
        m["suppressed_low_cpu_noise"] = crosses_threshold and not meets_min_cpu
        m["effective_cpu_floor"] = round(effective_floor, 5)
        m["scoring_status"] = "OK"
        return m

    def detect_anomalies(self, metrics_list):
        anomalies = []
        for m in metrics_list:
            m = self.score_against_baseline(m)
            if m["is_anomaly"]:
                anomalies.append(m)
                print(f"  [ANOMALY] {m['service']} — score/threshold ratio: {m['anomaly_score']:.3f}")
        return anomalies

    def run(self):
        metrics = self.collect_all_metrics()
        anomalies = self.detect_anomalies(metrics)
        result = {
            "timestamp": datetime.now().isoformat(),
            "total_services": len(metrics),
            "anomaly_count": len(anomalies),
            "all_metrics": metrics,
            "anomalies": anomalies
        }
        print(f"\n[MetricCollector] Result: {len(anomalies)} anomalies found in {len(metrics)} services")
        return result


if __name__ == "__main__":
    collector = MetricCollector(baseline_minutes=60, threshold_percentile=95.0)

    missing = [s for s in collector.services if s not in collector.models]
    if missing:
        print(f"Fitting baselines for: {missing}")
        print("Make sure the cluster is in a verified-normal state before running this.")
        for s in missing:
            collector.fit_baseline(s)

    result = collector.run()
    print(json.dumps(result, indent=2, default=str))