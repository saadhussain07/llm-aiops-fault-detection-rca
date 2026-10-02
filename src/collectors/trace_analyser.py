import os
import json
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

class TraceAnalyser:
    def __init__(self):
        self.jaeger_url = os.getenv("JAEGER_URL", "http://localhost:16686")
        self.services = [
            "frontend",
            "cartservice",
            "currencyservice", 
            "paymentservice",
            "productcatalogservice"
        ]
        self.latency_threshold_ms = 2000
        self.error_rate_threshold = 0.05

        # NEW (real S5 data): frontend's own code calls several
        # services from the full Google microservices-demo app
        # (recommendationservice, adservice, shippingservice,
        # checkoutservice) that were never deployed in this 6-service
        # testbed. Every such call fails, producing a CONSTANT
        # background error_rate of ~18-20% -- confirmed directly in
        # last night's S4 data (error_rate held steady at 0.18-0.20
        # across every cycle, unrelated to any injected fault) and
        # again this morning in S5 (cycles 3-4 flip-flopped between
        # FALSE_POSITIVE/ANOMALY_CONFIRMED purely from this noise,
        # with the LLM's own reasoning correctly attributing it to
        # "ads service DeadlineExceeded", not redis-cart). Since
        # 18-20% is already far above the 5% error_rate_threshold,
        # TraceAnalyser was effectively flagging frontend as
        # anomalous on almost every cycle regardless of any real
        # fault. Filtered the same way as log_parser.py's fix
        # yesterday: exclude spans referencing a known-undeployed
        # service from the ERROR count (not from latency stats --
        # p99/avg still reflect true overall responsiveness).
        self.known_undeployed_services = [
            "recommendationservice",
            "adservice",
            "shippingservice",
            "checkoutservice",
        ]

        # NEW (real S5 data): the exact DNS name matches span TAGS
        # (e.g. an RPC target host tag), but human-readable span LOG
        # messages use natural language instead -- confirmed real
        # text: "failed to get ads", "failed to retrieve ads", never
        # the literal substring "adservice". Broader phrase list so
        # log-field matching (not just tag matching) catches both
        # forms, same fix applied to log_parser.py.
        self.undeployed_service_log_phrases = [
            "recommendationservice", "recommendation",
            "adservice", "get ads", "retrieve ads", "ads provider",
            "shippingservice", "shipping",
            "checkoutservice", "checkout",
        ]

    def get_services(self):
        """Fetch the list of available services from Jaeger."""
        try:
            response = requests.get(
                f"{self.jaeger_url}/api/services",
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("data", [])
            return []
        except Exception as e:
            print(f"[TraceAnalyser] Services fetch error: {e}")
            return []

    def get_traces(self, service, limit=100):
        """Fetch traces for a service."""
        try:
            end_time = datetime.now()
            start_time = end_time - timedelta(minutes=10)
            
            params = {
                "service": service,
                "limit": limit,
                "start": int(start_time.timestamp() * 1_000_000),
                "end": int(end_time.timestamp() * 1_000_000)
            }
            
            response = requests.get(
                f"{self.jaeger_url}/api/traces",
                params=params,
                timeout=15
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get("data", [])
            return []
        except Exception as e:
            print(f"[TraceAnalyser] Traces fetch error for {service}: {e}")
            return []

    def _span_mentions_undeployed_service(self, span):
        """
        Scan a span's tags AND log fields for any reference to a
        known-undeployed service. Jaeger spans carry error detail in
        different places depending on the instrumentation -- checking
        both tags and logs defensively, since we don't have a fixed
        schema guarantee for exactly where a given SDK puts the
        error message text.

        Tags are checked against the exact DNS/service name (e.g. a
        gRPC target host tag literally says "adservice"). Log fields
        are checked against the broader natural-language phrase list,
        since real app log messages say things like "failed to get
        ads" rather than the literal service name.
        """
        for tag in span.get("tags", []):
            value = str(tag.get("value", "")).lower()
            if any(svc in value for svc in self.known_undeployed_services):
                return True
        for log_entry in span.get("logs", []):
            for field in log_entry.get("fields", []):
                value = str(field.get("value", "")).lower()
                if any(phrase in value for phrase in self.undeployed_service_log_phrases):
                    return True
        return False

    def analyse_traces(self, service, traces):
        """Analyse traces for latency and errors."""
        if not traces:
            return {
                "service": service,
                "trace_count": 0,
                "avg_latency_ms": 0,
                "p99_latency_ms": 0,
                "max_latency_ms": 0,
                "error_count": 0,
                "error_rate": 0.0,
                "slowest_operation": None,
                "is_anomaly": False,
                "anomaly_reason": None
            }

        durations = []
        error_count = 0
        filtered_noise_error_count = 0  # NEW: tracked separately for visibility, not counted as real errors
        operations = {}

        for trace in traces:
            spans = trace.get("spans", [])
            for span in spans:
                duration_us = span.get("duration", 0)
                duration_ms = duration_us / 1000
                durations.append(duration_ms)

                operation = span.get("operationName", "unknown")
                if operation not in operations:
                    operations[operation] = []
                operations[operation].append(duration_ms)

                is_error_span = False
                tags = span.get("tags", [])
                for tag in tags:
                    if tag.get("key") == "error" and tag.get("value") == True:
                        is_error_span = True

                if is_error_span:
                    # NEW: don't count this error if it's caused by a
                    # call to a service that was deliberately never
                    # deployed in this testbed -- that's permanent,
                    # fault-independent noise, not a real anomaly
                    # signal.
                    if self._span_mentions_undeployed_service(span):
                        filtered_noise_error_count += 1
                    else:
                        error_count += 1

        if not durations:
            return {
                "service": service,
                "trace_count": len(traces),
                "avg_latency_ms": 0,
                "p99_latency_ms": 0,
                "max_latency_ms": 0,
                "error_count": 0,
                "error_rate": 0.0,
                "slowest_operation": None,
                "is_anomaly": False,
                "anomaly_reason": None
            }

        durations.sort()
        total = len(durations)
        p99_idx = int(total * 0.99)
        p99_latency = durations[min(p99_idx, total-1)]
        avg_latency = sum(durations) / total
        max_latency = max(durations)
        error_rate = error_count / total if total > 0 else 0

        # Find the slowest operation
        slowest_op = None
        max_op_time = 0
        for op, times in operations.items():
            avg_op = sum(times) / len(times)
            if avg_op > max_op_time:
                max_op_time = avg_op
                slowest_op = op

        # Anomaly check
        is_anomaly = False
        anomaly_reason = None

        if p99_latency > self.latency_threshold_ms:
            is_anomaly = True
            anomaly_reason = f"p99 latency {p99_latency:.0f}ms exceeds threshold {self.latency_threshold_ms}ms"
        elif error_rate > self.error_rate_threshold:
            is_anomaly = True
            anomaly_reason = f"Error rate {error_rate:.1%} exceeds threshold {self.error_rate_threshold:.1%}"

        return {
            "service": service,
            "trace_count": len(traces),
            "avg_latency_ms": round(avg_latency, 2),
            "p99_latency_ms": round(p99_latency, 2),
            "max_latency_ms": round(max_latency, 2),
            "error_count": error_count,
            "error_rate": round(error_rate, 4),
            "filtered_noise_error_count": filtered_noise_error_count,
            "slowest_operation": slowest_op,
            "is_anomaly": is_anomaly,
            "anomaly_reason": anomaly_reason
        }

    def run(self):
        """Main function"""
        print(f"\n[TraceAnalyser] Analysing traces at {datetime.now().strftime('%H:%M:%S')}")
        
        # Check which services Jaeger knows about
        available = self.get_services()
        print(f"[TraceAnalyser] Services available in Jaeger: {available}")

        results = []
        anomalies = []

        for service in self.services:
            traces = self.get_traces(service)
            analysis = self.analyse_traces(service, traces)
            analysis["timestamp"] = datetime.now().isoformat()
            results.append(analysis)

            status = "ANOMALY" if analysis["is_anomaly"] else "OK"
            print(f"  [{status}] {service}: "
                  f"traces={analysis['trace_count']}, "
                  f"p99={analysis['p99_latency_ms']}ms, "
                  f"errors={analysis['error_count']}")

            if analysis["is_anomaly"]:
                anomalies.append(analysis)
                print(f"    Reason: {analysis['anomaly_reason']}")

        result = {
            "timestamp": datetime.now().isoformat(),
            "total_services_checked": len(results),
            "anomaly_count": len(anomalies),
            "trace_analysis": results,
            "anomalies": anomalies
        }

        print(f"\n[TraceAnalyser] Result: {len(anomalies)} anomalies in {len(results)} services")
        return result


if __name__ == "__main__":
    analyser = TraceAnalyser()
    result = analyser.run()
    print("\n" + "="*50)
    print("FULL RESULT:")
    print(json.dumps(result, indent=2))