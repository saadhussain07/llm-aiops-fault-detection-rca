import os
import json
import requests
from datetime import datetime, timedelta
from collections import Counter
from dotenv import load_dotenv

load_dotenv()

class LogParser:
    def __init__(self):
        self.loki_url = os.getenv("LOKI_URL", "http://localhost:3100")
        self.services = [
            "frontend",
            "cartservice",
            "currencyservice",
            "paymentservice",
            "productcatalogservice"
        ]
        self.error_threshold = 10
        self.new_pattern_alert = True

        # NEW (real S5 data, first pilot cycle): frontend's own code
        # calls several services from the FULL Google microservices-
        # demo app (recommendationservice, adservice, shippingservice,
        # checkoutservice) that were NEVER deployed in this 6-service
        # testbed. This produces a constant, permanent stream of "no
        # such host" DNS errors on every real request -- confirmed:
        # 132 errors in one 5-minute window, unrelated to any fault
        # injection. This pre-existing noise was invisible to S1-S4
        # (metric/trace-based detection), but directly confounds S5's
        # log-based dependency-hint detection: it drowned out the real
        # redis-cart signal and caused the LLM to incorrectly blame
        # "CoreDNS failing" instead of the actual injected fault.
        # Filtered out here so only genuine anomalies (from services
        # that actually exist in this testbed) count.
        self.known_undeployed_services = [
            "recommendationservice",
            "adservice",
            "shippingservice",
            "checkoutservice",
        ]

        # NEW (real S5 data, second bug in the same fix): the exact
        # Kubernetes DNS name ("adservice") appears in trace span tags
        # (e.g. a gRPC target host), but the APPLICATION's own
        # human-readable log messages use natural language instead --
        # confirmed real log text: "failed to get ads: rpc error: code
        # = DeadlineExceeded" and "failed to retrieve ads" -- neither
        # contains the literal substring "adservice" at all, so the
        # log-side filter above missed it while the trace-side filter
        # (which happened to match on span tag content) caught the
        # same underlying noise correctly. Added natural-language stems
        # for each undeployed service so log-text matching covers both
        # forms.
        self.undeployed_service_log_phrases = [
            "recommendationservice", "recommendation",
            "adservice", "get ads", "retrieve ads", "ads provider",
            "shippingservice", "shipping",
            "checkoutservice", "checkout",
        ]

        # Try Drain3; fall back to the simple parser if unavailable.
        #
        # FIX (observed in every scenario's console output tonight):
        # config.load_defaults() doesn't exist on this installed
        # drain3 version's TemplateMinerConfig -- Drain3 has been
        # silently falling back to the simple parser in EVERY run all
        # session, not just occasionally. TemplateMinerConfig() alone
        # (no explicit load_defaults call) already initializes with
        # sane defaults in current drain3 releases, so this call was
        # both unnecessary and broken.
        try:
            from drain3 import TemplateMiner
            from drain3.template_miner_config import TemplateMinerConfig
            config = TemplateMinerConfig()
            config.profiling_enabled = False
            self.miner = TemplateMiner(config=config)
            self.use_drain = True
            print("[LogParser] Drain3 loaded successfully")
        except Exception as e:
            print(f"[LogParser] Drain3 not available, using simple parser: {e}")
            self.miner = None
            self.use_drain = False

        self.seen_patterns = set()

    def query_loki(self, query, minutes=5):
        """Fetch logs from Loki."""
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(minutes=minutes)

            params = {
                "query": query,
                "start": str(int(start_time.timestamp() * 1e9)),
                "end": str(int(end_time.timestamp() * 1e9)),
                "limit": "500"
            }

            response = requests.get(
                f"{self.loki_url}/loki/api/v1/query_range",
                params=params,
                timeout=15
            )

            if response.status_code == 200:
                data = response.json()
                results = data.get("data", {}).get("result", [])
                logs = []
                for stream in results:
                    for entry in stream.get("values", []):
                        logs.append(entry[1])
                return logs
            else:
                print(f"[LogParser] Loki error: {response.status_code}")
                return []

        except Exception as e:
            print(f"[LogParser] Query error: {e}")
            return []

    def extract_patterns_simple(self, logs):
        """Simple pattern extraction without Drain3"""
        patterns = Counter()
        for log in logs:
            log_lower = log.lower()
            if "error" in log_lower:
                patterns["ERROR pattern"] += 1
            elif "warn" in log_lower:
                patterns["WARN pattern"] += 1
            elif "exception" in log_lower:
                patterns["EXCEPTION pattern"] += 1
            elif "timeout" in log_lower:
                patterns["TIMEOUT pattern"] += 1
            elif "refused" in log_lower:
                patterns["CONNECTION REFUSED pattern"] += 1
            elif "failed" in log_lower:
                patterns["FAILED pattern"] += 1
            else:
                patterns["OTHER"] += 1
        return dict(patterns)

    def extract_patterns_drain(self, logs):
        """Extract log patterns using Drain3."""
        patterns = Counter()
        for log in logs:
            try:
                result = self.miner.add_log_message(log)
                if result:
                    template = result["template_mined"]
                    patterns[template] += 1
            except:
                patterns["unparsed"] += 1
        return dict(patterns)

    def analyse_service_logs(self, service):
        """Analyse the logs for a single service."""
        # Fetch error logs
        error_query = f'{{namespace="microservices-demo", app="{service}"}}'
        all_logs = self.query_loki(error_query, minutes=5)

        # NEW: filter out known-undeployed-service noise BEFORE any
        # error counting or dependency-hint detection happens. A log
        # line naming ONLY an undeployed service (e.g. "lookup
        # adservice ... no such host") with no OTHER anomaly-worthy
        # content is permanent background noise in this testbed, not
        # a real fault signal. A log line that ALSO mentions redis-cart
        # or another real, deployed service's dependency is kept --
        # only filtered when the undeployed-service reference is the
        # sole content driving the match.
        filtered_logs = []
        for log in all_logs:
            log_lower = log.lower()
            mentions_undeployed = any(
                svc in log_lower for svc in self.undeployed_service_log_phrases
            )
            mentions_real_dependency = "redis" in log_lower or "stackexchange.redis" in log_lower
            if mentions_undeployed and not mentions_real_dependency:
                continue  # skip: pure known-missing-service noise
            filtered_logs.append(log)
        all_logs = filtered_logs

        # Split into error and warning logs
        error_logs = []
        warn_logs = []
        for log in all_logs:
            log_lower = log.lower()
            if any(w in log_lower for w in ["error", "exception", "failed", "refused", "timeout"]):
                error_logs.append(log)
            elif "warn" in log_lower:
                warn_logs.append(log)

        # NEW: surface DEPENDENCY-specific keywords found in error logs,
        # so a downstream consumer (e.g. cartservice) failing because an
        # UPSTREAM dependency (e.g. redis-cart) is unreachable gets a
        # concrete, nameable clue passed forward -- not just a generic
        # "ERROR pattern" bucket count. Without this, nothing in the
        # pipeline's output ever contains the word "redis" even when
        # the actual exception text does, making correct root-cause
        # attribution to the failed dependency (vs. the service that
        # merely couldn't reach it) very unlikely. This directly
        # targets the paper's own documented E2 misattribution finding
        # -- addressed here as a fixable context gap, the same pattern
        # as S2's restart-trigger and S3's memory-pressure trigger.
        dependency_keywords = {
            "redis": ["redis", "stackexchange.redis"],
            "grpc": ["grpc", "unavailable", "deadline exceeded"],
            "dns": ["no such host", "name resolution", "dns"],
        }
        dependency_hints = set()
        for log in error_logs:
            log_lower = log.lower()
            for dep_name, keywords in dependency_keywords.items():
                if any(kw in log_lower for kw in keywords):
                    dependency_hints.add(dep_name)

        # Extract patterns
        if self.use_drain and error_logs:
            patterns = self.extract_patterns_drain(error_logs)
        else:
            patterns = self.extract_patterns_simple(all_logs)

        # Anomaly checks
        is_anomaly = False
        anomaly_reasons = []
        new_patterns = []

        total_errors = len(error_logs)
        if total_errors > self.error_threshold:
            is_anomaly = True
            anomaly_reasons.append(
                f"High error count: {total_errors} errors in last 5 minutes"
            )

        if dependency_hints:
            is_anomaly = True
            anomaly_reasons.append(
                f"Error logs mention possible failed dependency: {', '.join(sorted(dependency_hints))}"
            )

        # New pattern check
        for pattern in patterns:
            if pattern not in self.seen_patterns and pattern != "OTHER":
                new_patterns.append(pattern)
                if self.new_pattern_alert:
                    is_anomaly = True
                    anomaly_reasons.append(f"New log pattern detected: {pattern}")
            self.seen_patterns.add(pattern)

        top_patterns = sorted(
            patterns.items(),
            key=lambda x: x[1],
            reverse=True
        )[:5]

        # Include one real, verbatim error log line (not just the
        # bucket name) so the LLM has actual evidence text to reason
        # over, capped to avoid flooding the prompt.
        sample_error_log = error_logs[0][:300] if error_logs else None

        return {
            "service": service,
            "total_logs": len(all_logs),
            "error_count": total_errors,
            "warn_count": len(warn_logs),
            "top_patterns": [
                {"pattern": p, "count": c} for p, c in top_patterns
            ],
            "new_patterns": new_patterns,
            "dependency_hints": sorted(dependency_hints),
            "sample_error_log": sample_error_log,
            "is_anomaly": is_anomaly,
            "anomaly_reasons": anomaly_reasons,
            "timestamp": datetime.now().isoformat()
        }

    def run(self):
        """Main function"""
        print(f"\n[LogParser] Parsing logs at {datetime.now().strftime('%H:%M:%S')}")

        # Check the Loki connection first
        try:
            test = requests.get(f"{self.loki_url}/ready", timeout=5)
            print(f"[LogParser] Loki status: {test.status_code}")
        except:
            print("[LogParser] Warning: Loki not reachable — check port forward")

        results = []
        anomalies = []

        for service in self.services:
            analysis = self.analyse_service_logs(service)
            results.append(analysis)

            status = "ANOMALY" if analysis["is_anomaly"] else "OK"
            print(f"  [{status}] {service}: "
                  f"total={analysis['total_logs']}, "
                  f"errors={analysis['error_count']}, "
                  f"warns={analysis['warn_count']}")

            if analysis["anomaly_reasons"]:
                for reason in analysis["anomaly_reasons"]:
                    print(f"    Reason: {reason}")

            if analysis["is_anomaly"]:
                anomalies.append(analysis)

        result = {
            "timestamp": datetime.now().isoformat(),
            "total_services": len(results),
            "anomaly_count": len(anomalies),
            "log_analysis": results,
            "anomalies": anomalies
        }

        print(f"\n[LogParser] Result: {len(anomalies)} anomalies in {len(results)} services")
        return result


if __name__ == "__main__":
    parser = LogParser()
    result = parser.run()
    print("\n" + "="*50)
    print("FULL RESULT:")
    print(json.dumps(result, indent=2))