"""OpenTelemetry configuration, child spans, and structured JSON logging for Clef."""

import json
import logging
import os
import sys
import time

try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.trace import Status, StatusCode
    OTEL_AVAILABLE = True
except ImportError:
    trace = None
    Status = None
    StatusCode = None
    OTEL_AVAILABLE = False


OUTCOME_SUCCESS = "success"
OUTCOME_VALIDATION_ERROR = "validation_error"
OUTCOME_CONTEXT_EXCEEDED = "context_exceeded"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_ERROR = "error"
ALLOWED_OUTCOMES = {
    OUTCOME_SUCCESS,
    OUTCOME_VALIDATION_ERROR,
    OUTCOME_CONTEXT_EXCEEDED,
    OUTCOME_RATE_LIMITED,
    OUTCOME_ERROR,
}


def is_otel_disabled():
    return os.environ.get("OTEL_SDK_DISABLED", "false").lower() in ("true", "1", "yes")


class JsonLogFormatter(logging.Formatter):
    """Structured JSON formatter with trace_id / span_id correlation.

    Explicitly redacts 'state' and 'images' to prevent data leakage.
    """

    def format(self, record):
        log_data = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if OTEL_AVAILABLE and trace:
            current_span = trace.get_current_span()
            ctx = current_span.get_span_context() if current_span else None
            if ctx and ctx.is_valid:
                log_data["trace_id"] = f"{ctx.trace_id:032x}"
                log_data["span_id"] = f"{ctx.span_id:016x}"

        # Attach extra safe attributes if provided
        for key, val in record.__dict__.items():
            if key not in (
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module", "msecs",
                "message", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
                "state", "images",  # Strictly never log state or images
            ):
                log_data[key] = val

        return json.dumps(log_data)


def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "clef"):
        log = logging.getLogger(name)
        log.handlers = [handler]
        log.propagate = False


def init_telemetry(app=None):
    """Initialize OpenTelemetry SDK, OTLP HTTP exporter, and FastAPI instrumentation."""
    if not OTEL_AVAILABLE or is_otel_disabled():
        return None

    service_name = os.environ.get("OTEL_SERVICE_NAME", os.environ.get("SERVICE", "clef"))
    resource = Resource.create({
        "service.name": service_name,
        "service.namespace": "ccsf-tis-clef-ai",
        "service.version": "1.0.0",
        "deployment.environment.name": os.environ.get("ENVIRONMENT", "prod"),
    })

    provider = TracerProvider(resource=resource)

    # Base OTLP endpoint (default to http://localhost:4318/v1/traces or from env)
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if not endpoint:
        base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318").rstrip("/")
        endpoint = f"{base}/v1/traces" if not base.endswith("/v1/traces") else base

    exporter = OTLPSpanExporter(endpoint=endpoint)
    # Use SimpleSpanProcessor if requested (useful in synchronous/test loops)
    if os.environ.get("OTEL_SPAN_PROCESSOR", "batch").lower() == "simple":
        processor = SimpleSpanProcessor(exporter)
    else:
        processor = BatchSpanProcessor(exporter)
    provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)

    if app is not None:
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)

    return provider


def get_tracer(name="clef"):
    if OTEL_AVAILABLE and trace and not is_otel_disabled():
        return trace.get_tracer(name)

    class _NoOpSpan:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def set_attribute(self, key, value):
            pass
        def set_status(self, status, description=None):
            pass
        def record_exception(self, exception):
            pass

    class _NoOpTracer:
        def start_as_current_span(self, name, *args, **kwargs):
            return _NoOpSpan()

    return _NoOpTracer()


def get_current_span():
    if OTEL_AVAILABLE and trace and not is_otel_disabled():
        return trace.get_current_span()
    return None


def set_span_outcome(span, outcome, error_detail=None):
    if not span or outcome not in ALLOWED_OUTCOMES:
        return
    span.set_attribute("clef.outcome", outcome)
    if outcome == OUTCOME_ERROR:
        if OTEL_AVAILABLE and Status and StatusCode:
            span.set_status(Status(StatusCode.ERROR, description=str(error_detail or "Internal Error")))
    elif outcome in (OUTCOME_VALIDATION_ERROR, OUTCOME_CONTEXT_EXCEEDED) and span:
        span.set_attribute("clef.rejection_reason", str(error_detail or outcome))
