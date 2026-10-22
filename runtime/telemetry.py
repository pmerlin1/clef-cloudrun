"""Portable OTel bootstrap and allowlisted signals; never export model inputs."""

import json
import logging
import os
import sys
import time
from pathlib import Path

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Link, NonRecordingSpan, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

OUTCOME_SUCCESS = "success"
OUTCOME_VALIDATION_ERROR = "validation_error"
OUTCOME_CONTEXT_EXCEEDED = "context_exceeded"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_ERROR = "error"
OUTCOME_CANCELLED = "cancelled"
ALLOWED_OUTCOMES = {OUTCOME_SUCCESS, OUTCOME_VALIDATION_ERROR, OUTCOME_CONTEXT_EXCEEDED,
                    OUTCOME_RATE_LIMITED, OUTCOME_ERROR, OUTCOME_CANCELLED}

# Names, types and units are shared with the verifier. Outcome values are the
# bounded enum above; model is clef/clef-flash. Request IDs are never metric labels.
SAFE_ATTRIBUTES = {
    "gen_ai.request.model", "clef.outcome", "clef.questions.count", "clef.images.count",
    "clef.input_tokens", "clef.batch_size", "clef.max_tokens", "clef.queue_wait_ms",
    "clef.preload.files_count", "clef.preload.bytes", "http.method", "http.status_code",
    "http.request.method", "http.response.status_code", "http.route", "network.protocol.version",
}
_providers = []
_requests = _duration = None


def is_otel_disabled():
    return os.environ.get("OTEL_SDK_DISABLED", "false").lower() in ("true", "1", "yes")


class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        data = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
                "level": record.levelname, "message": record.getMessage(), "logger": record.name}
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            data.update(trace_id=f"{context.trace_id:032x}", span_id=f"{context.span_id:016x}")
        # Allowlist rather than copying arbitrary LogRecord extras. In particular
        # exclude exception text, stack locals, request bodies, headers and baggage.
        data.update({key: value for key, value in record.__dict__.items() if key in SAFE_ATTRIBUTES})
        return json.dumps(data)


def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    logging.getLogger().handlers = [handler]
    logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    for name in ("uvicorn", "uvicorn.error", "clef"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
    logging.getLogger("uvicorn.access").disabled = True


class SafeSpanExporter(SpanExporter):
    """Remove automatic URL/header/exception capture at the export boundary."""
    def __init__(self, delegate):
        self.delegate = delegate

    def export(self, spans):
        safe = [ReadableSpan(
            name=span.name, context=span.context, parent=span.parent, resource=span.resource,
            attributes={key: value for key, value in span.attributes.items() if key in SAFE_ATTRIBUTES},
            events=(), links=span.links, kind=span.kind,
            status=Status(span.status.status_code), start_time=span.start_time, end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        ) for span in spans]
        return self.delegate.export(safe)

    def shutdown(self):
        return self.delegate.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.delegate.force_flush(timeout_millis)


def init_telemetry(app):
    global _requests, _duration
    if is_otel_disabled():
        return
    manifest_path = Path(__file__).with_name("build-manifest.json")
    identity = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
        "build_id": "source", "source_revision": "unknown",
    }
    # Resource() avoids importing unvetted OTEL_RESOURCE_ATTRIBUTES/payload data.
    resource = Resource({
        "service.name": os.environ.get("OTEL_SERVICE_NAME") or os.environ.get("K_SERVICE") or os.environ.get("SERVICE") or "clef",
        "service.namespace": "ccsf-tis-clef-ai", "service.version": identity["source_revision"],
        "ccsf.build.id": identity["build_id"],
        "deployment.environment.name": os.environ.get("ENVIRONMENT", "prod"),
    })
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    if endpoint:
        span_exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", timeout=5)
        metric_exporter = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", timeout=5)
    elif os.environ.get("K_SERVICE"):
        # Supported native GCP exporters use workload ADC, never static tokens.
        from opentelemetry.exporter.cloud_monitoring import (
            CloudMonitoringMetricsExporter,
        )
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        span_exporter = CloudTraceSpanExporter(project_id=project)
        metric_exporter = CloudMonitoringMetricsExporter(project_id=project)
    else:
        logging.getLogger("clef").warning("Telemetry export unconfigured: set OTEL_EXPORTER_OTLP_ENDPOINT")
        return
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(SafeSpanExporter(span_exporter)))
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "60000")))
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader], views=[
        View(instrument_name="*", attribute_keys=SAFE_ATTRIBUTES),
    ])
    trace.set_tracer_provider(provider)
    metrics.set_meter_provider(meter_provider)
    _providers.extend([meter_provider, provider])
    set_global_textmap(TraceContextTextMapPropagator())
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, meter_provider=meter_provider,
        excluded_urls="healthz", exclude_spans=["receive", "send"],
        http_capture_headers_server_request=[], http_capture_headers_server_response=[],
    )
    # GCS uses requests; download URL/headers/exceptions are stripped above.
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    RequestsInstrumentor().instrument(tracer_provider=provider, meter_provider=meter_provider)
    meter = meter_provider.get_meter("clef")
    _requests = meter.create_counter("clef.requests", unit="{request}", description="Completed SystemOne requests by bounded outcome")
    _duration = meter.create_histogram("clef.request.duration", unit="s", description="SystemOne wall-clock latency")


def shutdown_telemetry():
    for provider in _providers:
        provider.force_flush(timeout_millis=5000)
        provider.shutdown()
    _providers.clear()


def get_tracer(name="clef"):
    return trace.NoOpTracerProvider().get_tracer(name) if is_otel_disabled() else trace.get_tracer(name)


def get_current_span():
    return None if is_otel_disabled() else trace.get_current_span()


def batch_trace_context(items):
    contexts = [item.span_context for item in items if item.span_context and item.span_context.is_valid]
    if not contexts:
        return {}
    # One parent plus links to EVERY participating request. A multi-request
    # batch cannot be a child of multiple server spans.
    parent = next((context for context in contexts if context.trace_flags.sampled), contexts[0])
    return {"context": trace.set_span_in_context(NonRecordingSpan(parent)),
            "links": [Link(context) for context in contexts]}


def set_span_outcome(span, outcome, error_detail=None):
    if span is not None and outcome in ALLOWED_OUTCOMES:
        span.set_attribute("clef.outcome", outcome)
        if outcome == OUTCOME_ERROR:
            span.set_status(Status(StatusCode.ERROR))


def record_request(model, outcome, status, seconds):
    attributes = {"gen_ai.request.model": model, "clef.outcome": outcome, "http.response.status_code": status}
    if _requests is not None:
        _requests.add(1, attributes)
        _duration.record(seconds, attributes)
    logging.getLogger("clef").info("Systemone request finished", extra=attributes)
