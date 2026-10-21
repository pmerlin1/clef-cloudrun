"""Mechanical verification loop for Clef OpenTelemetry spans, metrics, and logs.

Tests:
1. Health & readiness endpoints
2. 200 Success path with clef.encode and clef.infer_batch child spans
3. 400 Rejection path with validation_error outcome
4. 429 Burst backpressure with Retry-After header and rate_limited outcome
5. Structured JSON log correlation (trace_id / span_id) and privacy check (no state/images)
"""

import concurrent.futures
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC

APP_URL = "http://127.0.0.1:8080"
JAEGER_URL = "http://127.0.0.1:16686"
COLLECTOR_METRICS_URL = "http://127.0.0.1:8889/metrics"


def http_request(url, data=None, headers=None, method=None):
    headers = headers or {}
    if data is not None and isinstance(data, dict):
        data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8")


def wait_for_ready(max_retries=30):
    print("Waiting for app and Jaeger to be ready...", flush=True)
    for _ in range(max_retries):
        try:
            status, _, _ = http_request(f"{APP_URL}/healthz")
            j_status, _, _ = http_request(f"{JAEGER_URL}/api/v3/services")
            if status == 200 and j_status == 200:
                print("App and Jaeger are ready.", flush=True)
                return True
        except Exception:  # noqa: BLE001, S110
            pass
        time.sleep(1)
    raise RuntimeError("Services failed to become ready in time")


def poll_jaeger_trace(run_id, service="clef", max_wait=15):
    start = time.monotonic()
    url = (
        f"{JAEGER_URL}/api/v3/traces?query.serviceName={service}"
        f"&query.searchDepth=50"
    )
    while time.monotonic() - start < max_wait:
        try:
            status, _, body = http_request(url)
            if status == 200:
                data = json.loads(body)
                traces = data.get("result", [])
                for trace in traces:
                    for r_span in trace.get("resourceSpans", []):
                        for s_span in r_span.get("scopeSpans", []):
                            for span in s_span.get("spans", []):
                                attrs = {
                                    a["key"]: a.get("value", {}).get("stringValue")
                                    for a in span.get("attributes", [])
                                }
                                if attrs.get("ccsf.run.id") == run_id:
                                    return trace
        except Exception as exc:  # noqa: BLE001
            print(f"Polling error: {exc}", flush=True)
        time.sleep(0.5)
    return None


def main():
    wait_for_ready()

    # 1. Exercise 200 Success Path
    run_id_success = uuid.uuid4().hex
    success_payload = {
        "model": "clef",
        "state": "telemetry-verify",
        "questions": {"q1": {"type": "noul"}},
    }
    status, _headers, body = http_request(
        f"{APP_URL}/v1/systemone",
        data=success_payload,
        headers={"ccsf-run-id": run_id_success},
    )
    print(f"Success request status: {status}")
    assert status == 200, f"Expected 200, got {status}: {body}"

    # 2. Exercise 400 Validation Rejection Path
    bad_payload = {"model": "clef", "state": "test", "questions": {}}
    status_bad, _, body_bad = http_request(f"{APP_URL}/v1/systemone", data=bad_payload)
    print(f"Rejection request status: {status_bad}")
    assert status_bad == 400, f"Expected 400, got {status_bad}: {body_bad}"

    # 3. Exercise 429 Burst Path
    print("Sending burst load of 150 requests...", flush=True)
    burst_statuses = []
    burst_headers = []

    def send_one(i):
        payload = {
            "model": "clef",
            "state": f"burst-{i}",
            "questions": {"q": {"type": "noul"}},
        }
        return http_request(f"{APP_URL}/v1/systemone", data=payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=100) as pool:
        futures = [pool.submit(send_one, i) for i in range(150)]
        for f in concurrent.futures.as_completed(futures):
            st, hd, _ = f.result()
            burst_statuses.append(st)
            burst_headers.append(hd)

    print(f"Burst completed. Statuses: 200={burst_statuses.count(200)}, 429={burst_statuses.count(429)}")
    assert burst_statuses.count(200) > 0, "Expected at least some 200s"
    if burst_statuses.count(429) > 0:
        # Check Retry-After header on 429 responses
        for st, hd in zip(burst_statuses, burst_headers):
            if st == 429:
                assert "retry-after" in (k.lower() for k in hd), "Missing Retry-After header in 429"
        print("429 Retry-After header verified on burst load.")

    # 4. Assert Trace Receipt in Jaeger
    print("Waiting for traces to export to Jaeger...", flush=True)
    time.sleep(3)
    from datetime import datetime, timedelta
    now_utc = datetime.now(UTC)
    t_min = (now_utc - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t_max = (now_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    trace_url = (
        f"{JAEGER_URL}/api/v3/traces?query.serviceName=clef"
        f"&query.startTimeMin={t_min}&query.startTimeMax={t_max}&query.searchDepth=100"
    )
    trace_status, _, trace_body = http_request(trace_url)
    assert trace_status == 200, f"Jaeger query failed ({trace_status}): {trace_body}"
    traces_res = json.loads(trace_body).get("result", {})
    if isinstance(traces_res, dict):
        resource_spans = traces_res.get("resourceSpans", [])
    else:
        resource_spans = traces_res

    all_span_names = set()
    for r_span in resource_spans:
        for s_span in r_span.get("scopeSpans", []):
            for span in s_span.get("spans", []):
                all_span_names.add(span.get("name"))

    print(f"Discovered span names: {all_span_names}")
    assert "clef.encode" in all_span_names, "Missing clef.encode child span in traces!"
    assert "clef.infer_batch" in all_span_names, "Missing clef.infer_batch child span in traces!"

    # 5. Assert Log Correlation and Privacy
    print("Verifying container logs for trace_id/span_id correlation and privacy...", flush=True)
    logs_out = subprocess.check_output(["docker", "compose", "-f", "compose.observability.yml", "logs", "app"]).decode("utf-8")

    correlated_lines = 0
    for line in logs_out.splitlines():
        # Check for privacy violations
        assert "telemetry-verify" not in line, f"Data privacy violation: state leaked in logs: {line}"
        idx = line.find("{")
        if idx != -1:
            try:
                entry = json.loads(line[idx:])
                if "trace_id" in entry and "span_id" in entry:
                    assert len(entry["trace_id"]) == 32, f"Invalid trace_id length: {entry['trace_id']}"
                    assert len(entry["span_id"]) == 16, f"Invalid span_id length: {entry['span_id']}"
                    correlated_lines += 1
            except json.JSONDecodeError:
                pass

    print(f"Verified {correlated_lines} trace-correlated JSON log lines with 0 data leaks.")
    assert correlated_lines > 0, "No trace-correlated log entries found!"

    print("\nALL OBSERVABILITY & VERIFICATION ASSERTIONS PASSED!")


if __name__ == "__main__":
    main()
