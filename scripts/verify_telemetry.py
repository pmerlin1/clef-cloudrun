"""Bounded built-image/Collector/Jaeger verification with isolated run IDs.

Run on the host (Python stdlib only); docker compose uses loopback-only random
ports. No cloud credentials, source mounts, model weights or GPU are used.
"""

import concurrent.futures
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def http(url, payload=None, headers=None):
    request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", **(headers or {})})
    try:
        response = urllib.request.urlopen(request, timeout=20)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, dict(response.headers), response.read().decode()


def wait_until(check, seconds=30):
    deadline = time.monotonic() + seconds
    error = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (AssertionError, OSError, ValueError) as exc:
            error = exc
        time.sleep(0.2)
    raise AssertionError(f"Verification deadline exceeded: {error}")


def attrs(values):
    return {value["key"]: next(iter(value["value"].values())) for value in values}


def spans_from(body):
    spans = []
    for line in body.splitlines():
        if not line.strip():
            continue
        document = json.loads(line)
        require("error" not in document, "Jaeger returned an error envelope")
        for resource in document.get("result", document).get("resourceSpans", []):
            identity = attrs(resource["resource"]["attributes"])
            for scope in resource.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    spans.append({**span, "resource": identity, "attrs": attrs(span.get("attributes", []))})
    return spans


def metric_samples(text, prefix):
    samples = {}
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        match = re.fullmatch(r'([^\s{]+)(?:\{(.*)\})?\s+(\S+)(?:\s+\S+)?', line)
        require(match is not None, "Malformed Prometheus sample")
        name, labels, raw = match.groups()
        values = dict(re.findall(r'(\w+)="([^"\\]*)"', labels or ""))
        number = float(raw)
        require(math.isfinite(number), "Non-finite request metric")
        samples[(name, tuple(sorted(values.items())))] = number
    return samples


def trace_contract(spans, requests, revision, build_id):
    by_trace = {}
    for span in spans:
        by_trace.setdefault(span["traceId"], []).append(span)
        require(span["resource"].get("service.version") == revision, "Wrong source revision in telemetry")
        require(span["resource"].get("ccsf.build.id") == build_id, "Wrong immutable build identity")
        require(span["resource"].get("service.name") == "clef", "Wrong service name")
        require(span["resource"].get("deployment.environment.name") == "test", "Wrong environment")
        require(not span.get("events"), "Unvetted exception/event data escaped the export boundary")
    batches = [span for span in spans if span["name"] == "clef.infer_batch"]
    linked = Counter((link["traceId"], link["spanId"]) for batch in batches for link in batch.get("links", []))
    for request in requests:
        if not request["sampled"]:
            require(not by_trace.get(request["trace"]), "Unsampled request was exported")
            continue
        current = by_trace.get(request["trace"], [])
        servers = [s for s in current if s.get("kind") in (2, "SPAN_KIND_SERVER")]
        require(len(servers) == 1, f"Expected exactly one server span for HTTP {request['status']}")
        server = servers[0]
        require(server.get("parentSpanId") == request["parent"], "Incoming traceparent was not preserved")
        require(server["attrs"].get("clef.outcome") == request["outcome"], "Missing or incorrect business outcome")
        require(int(server["attrs"].get("http.status_code", server["attrs"].get("http.response.status_code", 0))) == request["status"], "Wrong HTTP status")
        encoded = [s for s in current if s["name"] == "clef.encode"]
        require(len(encoded) == 1 and encoded[0].get("parentSpanId") == server["spanId"], "Missing/misparented encode span")
        did_infer = request["status"] in (200, 500)
        require(linked[(request["trace"], server["spanId"])] == int(did_infer), "Batch links do not match actual dispatch")
        is_error = server.get("status", {}).get("code") in (2, "STATUS_CODE_ERROR")
        require(is_error == (request["status"] == 500), "Expected rejections must not be server errors")
    for batch in batches:
        links = batch.get("links", [])
        require(len(links) == int(batch["attrs"]["clef.batch_size"]), "Batch link count must equal batch size")
        require(any(link["traceId"] == batch["traceId"] and link["spanId"] == batch.get("parentSpanId") for link in links), "Batch parent must be a participating request")


def main():
    run_id = uuid.uuid4().hex
    project = f"clef-verify-{run_id[:12]}"
    # Never forward production cloud credentials or telemetry settings to Compose.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AWS_", "GOOGLE_", "GCP_", "OTEL_"))}
    compose = ["docker", "compose", "-p", project, "-f", str(ROOT / "compose.observability.yml")]

    def dc(*args):
        return subprocess.check_output([*compose, *args], cwd=ROOT, env=env, text=True, stderr=subprocess.STDOUT, timeout=120)

    def url(service, port):
        address = dc("port", service, str(port)).strip()
        require(address.startswith("127.0.0.1:"), "Verification port is not loopback-only")
        return f"http://{address}"

    try:
        dc("up", "-d")
        app, disabled = url("app", 8080), url("uninstrumented", 8080)
        jaeger = url("jaeger", 16686)
        metrics = url("otel-collector", 8889) + "/metrics"
        collector_metrics = url("otel-collector", 8888) + "/metrics"
        wait_until(lambda: http(app + "/healthz")[0] == 200 and http(disabled + "/healthz")[0] == 200 and http(jaeger + "/api/v3/services")[0] == 200)
        manifest = json.loads(dc("exec", "-T", "app", "/opt/venv/bin/python", "-c", "from pathlib import Path; print(Path('/app/build-manifest.json').read_text())"))
        require(manifest["source_revision"] != "unknown", "Source revision missing from built image")
        before = metric_samples(http(metrics)[2], "clef_")
        secret = "sentinel-" + run_id
        base = {"model": "clef", "state": secret, "questions": {"q": {"type": "noul"}}}

        def send(payload=base, endpoint=app, sampled=True):
            trace, parent = uuid.uuid4().hex, uuid.uuid4().hex[:16]
            status, headers, body = http(endpoint + "/v1/systemone?private=" + secret, payload, {
                "traceparent": f"00-{trace}-{parent}-{'01' if sampled else '00'}",
                "Authorization": "Bearer " + secret, "Cookie": "test=" + secret,
            })
            outcome = {200: "success", 400: "validation_error", 429: "rate_limited", 500: "error"}.get(status)
            require(outcome is not None, f"Unexpected HTTP status {status}")
            if status == 429:
                value = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
                require(value.isdigit() and 1 <= int(value) <= 30, "Missing/invalid Retry-After")
                require(json.loads(body)["retry_after_seconds"] == int(value), "Retry-After/body disagreement")
            return {"trace": trace, "parent": parent, "status": status, "outcome": outcome, "sampled": sampled}

        negative = send(endpoint=disabled)
        requests = [send(), send({**base, "questions": {}}),
                    send({**base, "state": {"mock_failure": True, "private": secret}})]
        require([r["status"] for r in requests] == [200, 400, 500], "Success/rejection/backend-failure fixture failed")
        with concurrent.futures.ThreadPoolExecutor(max_workers=75) as pool:
            burst = list(pool.map(lambda _: send(), range(75)))
        require(any(r["status"] == 429 for r in burst) and any(r["status"] == 200 for r in burst), "Burst must exercise both admission and 429")
        requests.extend(burst)
        requests.append(send(sampled=False))

        def retrieve(request):
            status, _, body = http(f"{jaeger}/api/v3/traces/{request['trace']}")
            return spans_from(body) if status == 200 else []

        def verify_traces():
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                all_spans = [s for trace in pool.map(retrieve, requests) for s in trace]
            # Jaeger can return a batch with the parent's trace. De-duplicate.
            all_spans = list({(s["traceId"], s["spanId"]): s for s in all_spans}.values())
            trace_contract(all_spans, requests, manifest["source_revision"], manifest["build_id"])
            return all_spans

        spans = wait_until(verify_traces)
        require(not retrieve(negative), "OTEL_SDK_DISABLED did not disable export")
        try:
            trace_contract([], [negative], manifest["source_revision"], manifest["build_id"])
        except AssertionError:
            pass
        else:
            raise AssertionError("Negative control failed: missing export was accepted")
        try:
            trace_contract([s for s in spans if s["name"] != "clef.encode"], requests, manifest["source_revision"], manifest["build_id"])
        except AssertionError:
            pass
        else:
            raise AssertionError("Negative control failed: missing business span was accepted")

        expected = Counter(r["outcome"] for r in requests)

        def verify_metrics():
            body = http(metrics)[2]
            samples = metric_samples(body, "clef_")
            actual = Counter()
            duration_count = 0
            for key, value in samples.items():
                name, labels = key
                labels = dict(labels)
                delta = value - before.get(key, 0)
                if name == "clef_requests_total":
                    actual[labels["clef_outcome"]] += delta
                if name == "clef_request_duration_seconds_count":
                    duration_count += delta
            require(actual == expected, f"Request metric deltas {dict(actual)} != {dict(expected)}")
            require(duration_count == len(requests), "Latency histogram count differs from completed requests")
            return body

        metrics_body = wait_until(verify_metrics)
        logs = dc("logs", "--no-color", "--no-log-prefix", "app")
        entries = []
        for line in logs.splitlines():
            if line.startswith("{"):
                entries.append(json.loads(line))
        for request in requests:
            matches = [entry for entry in entries if entry.get("trace_id") == request["trace"] and entry.get("message") == "Systemone request finished"]
            require(len(matches) == 1, "Each request needs exactly one correlated completion log")
            require(re.fullmatch("[0-9a-f]{16}", matches[0].get("span_id", "")) is not None, "Missing log span ID")
            if request["sampled"]:
                require(any(s["traceId"] == request["trace"] and s["spanId"] == matches[0]["span_id"] for s in spans), "Log cannot be joined to emitted trace")
        diagnostics = dc("logs", "--no-color", "otel-collector")
        counters = http(collector_metrics)[2]
        for line in counters.splitlines():
            if not line.startswith("#") and re.search(r"otelcol_.*(?:refused|send_failed|enqueue_failed)", line):
                require(float(line.rsplit(" ", 1)[-1]) == 0, "Collector dropped or failed to export telemetry")
        require('"level":"error"' not in diagnostics and "Exporting failed" not in diagnostics, "Collector reported export errors")
        require(secret not in json.dumps(spans) + logs + metrics_body + diagnostics, "Private sentinel leaked into telemetry")
        print(json.dumps({"run_id": run_id, "build": manifest["build_id"], "revision": manifest["source_revision"],
                          "requests": len(requests), "outcomes": dict(expected), "spans": len(spans),
                          "negative_controls": ["SDK disabled", "required encode removed"],
                          "trace_topology": "PASS", "metric_deltas": "PASS", "per_request_logs": "PASS", "privacy": "PASS"}, indent=2))
    finally:
        dc("down", "--remove-orphans")


if __name__ == "__main__":
    main()
