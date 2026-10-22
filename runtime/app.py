"""Bounded Clef API. Cloud Run enforces caller IAM before this application."""

import asyncio
import json
import logging
import math
import os
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from media import prepare_images
from request_body import RequestTooLarge, read_bounded_body
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse
from telemetry import (
    OUTCOME_CANCELLED,
    OUTCOME_CONTEXT_EXCEEDED,
    OUTCOME_ERROR,
    OUTCOME_RATE_LIMITED,
    OUTCOME_SUCCESS,
    OUTCOME_VALIDATION_ERROR,
    batch_trace_context,
    configure_logging,
    get_current_span,
    get_tracer,
    init_telemetry,
    record_request,
    set_span_outcome,
    shutdown_telemetry,
)

model = processor = None
collate_records_fn = systemone_answer_fn = encode_record_fn = None
inference_context = nullcontext
fast_path = None
# Compatibility injection for the credential-free adapter check. Production and
# the container fixture use the same collation/forward/serialization path below.
inference = None
gpu_lock = threading.Lock()
encode_lock = threading.Lock()
logger = logging.getLogger("clef")

MODEL_NAME = os.environ.get("MODEL", "clef")
MAX_BATCH_SIZE = 16 if MODEL_NAME == "clef-flash" else 8
MAX_BATCH_PADDED_TOKENS = 8192
MAX_CONTEXT_TOKENS = 2048
CLEF_MAX_QUEUE_DEPTH = int(os.environ.get("CLEF_MAX_QUEUE_DEPTH", "64"))
CLEF_MAX_QUEUE_WAIT_SECONDS = float(os.environ.get("CLEF_MAX_QUEUE_WAIT_SECONDS", "15.0"))
if CLEF_MAX_QUEUE_DEPTH < 1 or not 0 < CLEF_MAX_QUEUE_WAIT_SECONDS <= 900:
    raise ValueError("Queue depth must be positive and queue wait must be in (0, 900] seconds")

avg_batch_seconds = 0.16 if MODEL_NAME == "clef-flash" else 0.25
avg_batch_seconds_image = 1.5
in_flight_batches = 0
in_flight_has_images = False


class QueueExpired(Exception):
    """Work expired before it was dispatched to the GPU."""


class InputRejected(Exception):
    def __init__(self, message, outcome=OUTCOME_VALIDATION_ERROR):
        super().__init__(message)
        self.outcome = outcome


class QueueItem:
    def __init__(self, record, payload, future, has_images=False):
        self.record = record
        self.payload = payload
        self.future = future
        self.has_images = has_images
        self.enqueued_at = time.monotonic()
        self.deadline = self.enqueued_at + CLEF_MAX_QUEUE_WAIT_SECONDS
        self.cancelled = False
        self.started = asyncio.Event()
        self.queue_wait_ms = 0.0
        span = get_current_span()
        self.span_context = span.get_span_context() if span else None


def token_length(item):
    return len(item.record.input_ids) if item.record is not None else 0


def split_batch(items, max_batch_size, max_padded_tokens):
    batch, overflow = [], []
    max_len = 0
    for item in sorted(items, key=token_length):
        new_max = max(max_len, token_length(item))
        if len(batch) < max_batch_size and (len(batch) + 1) * new_max <= max_padded_tokens:
            batch.append(item)
            max_len = new_max
        else:
            overflow.append(item)
    return batch, overflow


class MicroBatchQueue:
    def __init__(self):
        self._deque = deque()
        self._event = asyncio.Event()

    def active(self):
        return [item for item in self._deque if not item.cancelled]

    def qsize(self):
        return len(self.active())

    def put_nowait(self, item):
        if token_length(item) > MAX_BATCH_PADDED_TOKENS:
            raise ValueError("Record exceeds the batch token budget")
        self._deque.append(item)
        self._event.set()

    def discard(self, item):
        item.cancelled = True
        if item in self._deque:
            self._deque.remove(item)

    async def get_batch(self, max_batch_size, max_padded_tokens):
        while True:
            while not self._deque:
                self._event.clear()
                await self._event.wait()
            active = []
            now = time.monotonic()
            for item in self._deque:
                if item.cancelled:
                    continue
                if item.deadline <= now:
                    item.cancelled = True
                    if item.future is not None and not item.future.done():
                        item.future.set_exception(QueueExpired())
                else:
                    active.append(item)
            batch, overflow = split_batch(active, max_batch_size, max_padded_tokens)
            self._deque = deque(overflow)
            if batch:
                return batch


batch_queue = MicroBatchQueue()
worker_task = None


def estimate_wait_seconds(has_images=False):
    # Price the work ahead of this caller, including image batches and padding.
    # A text caller behind image work must not inherit the text-only estimate.
    remaining = batch_queue.active()
    estimate = 0.0
    if in_flight_batches:
        estimate += avg_batch_seconds_image if in_flight_has_images or has_images else avg_batch_seconds
    while remaining:
        batch, remaining = split_batch(remaining, MAX_BATCH_SIZE, MAX_BATCH_PADDED_TOKENS)
        if not batch:
            return CLEF_MAX_QUEUE_WAIT_SECONDS + 1
        estimate += avg_batch_seconds_image if has_images or any(it.has_images for it in batch) else avg_batch_seconds
    return estimate


def calculate_retry_after(has_images=False):
    return max(1, min(30, math.ceil(estimate_wait_seconds(has_images))))


def run_gpu_batch_sync(valid_items):
    if inference is not None and collate_records_fn is None:
        return [inference(item.payload) for item in valid_items]
    # This is the pinned upstream systemone() algorithm applied to already
    # encoded records. systemone_answer() serializes ONE question; it is not a
    # model forward function. Inference mode is thread-local and belongs here.
    with gpu_lock, inference_context():
        if fast_path is not None:
            fast_path.begin_inference()
        records = [item.record for item in valid_items]
        device = next(model.parameters()).device
        batch = collate_records_fn(records, processor.tokenizer.pad_token_id, device)
        logits = model(batch)
        results = []
        for item, record_logits in zip(valid_items, logits, strict=True):
            answers = {
                question.question_id: systemone_answer_fn(
                    item.payload["questions"][question.question_id],
                    dict(zip(question.option_ids, values.float().softmax(-1).tolist(), strict=True)),
                )
                for question, values in zip(item.record.questions, record_logits, strict=True)
            }
            results.append({
                "model": item.payload["model"], "answers": answers,
                "usage": {"input_tokens": token_length(item), "output_tokens": 0},
            })
        if fast_path is not None:
            fast_path.verify_inference()
        return results


async def gpu_batch_worker():
    global in_flight_batches, in_flight_has_images, avg_batch_seconds, avg_batch_seconds_image
    while True:
        try:
            items = await batch_queue.get_batch(MAX_BATCH_SIZE, MAX_BATCH_PADDED_TOKENS)
            # No await between deadline checking and dispatch/start notification.
            # The queue timeout ends here; running kernels have no queue deadline.
            in_flight_batches = 1
            in_flight_has_images = any(item.has_images for item in items)
            now = time.monotonic()
            for item in items:
                item.queue_wait_ms = (now - item.enqueued_at) * 1000
                item.started.set()
            with get_tracer().start_as_current_span(
                "clef.infer_batch", **batch_trace_context(items),
                record_exception=False, set_status_on_exception=False,
            ) as span:
                span.set_attribute("gen_ai.request.model", MODEL_NAME)
                span.set_attribute("clef.batch_size", len(items))
                span.set_attribute("clef.max_tokens", max(map(token_length, items)))
                began = time.monotonic()
                try:
                    # Separate from Starlette's CPU validation thread limiter.
                    results = await asyncio.to_thread(run_gpu_batch_sync, items)
                    if len(results) != len(items):
                        raise RuntimeError("Inference result count mismatch")
                    elapsed = time.monotonic() - began
                    if in_flight_has_images:
                        avg_batch_seconds_image = 0.2 * elapsed + 0.8 * avg_batch_seconds_image
                    else:
                        avg_batch_seconds = 0.2 * elapsed + 0.8 * avg_batch_seconds
                    set_span_outcome(span, OUTCOME_SUCCESS)
                    for item, result in zip(items, results, strict=True):
                        if not item.future.done():
                            item.future.set_result(result)
                except Exception:  # noqa: BLE001 - GPU errors must never expose payloads.
                    set_span_outcome(span, OUTCOME_ERROR)
                    logger.error("Batch inference failed", extra={"clef.outcome": OUTCOME_ERROR})
                    for item in items:
                        if not item.future.done():
                            item.future.set_exception(RuntimeError("Inference failed"))
                finally:
                    in_flight_batches = 0
        except asyncio.CancelledError:
            return


def ensure_worker_started():
    global worker_task
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(gpu_batch_worker())


@asynccontextmanager
async def lifespan(app):
    global model, processor, fast_path, inference_context
    global collate_records_fn, systemone_answer_fn, encode_record_fn
    if os.environ.get("CLEF_MOCK_MODEL", "false").lower() in ("true", "1", "yes"):
        from mock_model import (
            MockModel,
            collate_records,
            encode_record,
            systemone_answer,
        )
        model = MockModel()
        processor = SimpleNamespace(tokenizer=SimpleNamespace(pad_token_id=0))
        collate_records_fn, encode_record_fn, systemone_answer_fn = collate_records, encode_record, systemone_answer
    else:
        import torch
        from fast_path import FastPathAudit, require_fast_path, seed_triton_cache
        from preload import preload
        seed_triton_cache()
        require_fast_path()
        path = preload()
        sys.path.insert(0, path)
        import joint_schema_model
        collate_records_fn = joint_schema_model.collate_records
        systemone_answer_fn = joint_schema_model.systemone_answer
        encode_record_fn = joint_schema_model.encode_record
        inference_context = torch.inference_mode
        model, processor = joint_schema_model.load_release_model(path, device="cuda", local_files_only=True)
        fast_path = FastPathAudit(model)
        for weights in Path(path).glob("*.safetensors"):
            weights.unlink()
        # Dropping loaded shard files is safe after the tensors reside on GPU.
        # Model Python code/config remains available for the running process.
        warmup_payload = {"model": MODEL_NAME, "state": "warmup", "questions": {"q": {"type": "noul"}}}
        warmup_record = encode_record_fn(processor.tokenizer, warmup_payload, max_length=1_000_000, processor=processor)
        # A failed warmup must fail readiness, not silently publish a broken GPU.
        run_gpu_batch_sync([QueueItem(warmup_record, warmup_payload, None)])
    ensure_worker_started()
    try:
        yield
    finally:
        if worker_task:
            worker_task.cancel()
            await worker_task
        shutdown_telemetry()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def health():
    return JSONResponse({"ready": model is not None, "model": MODEL_NAME}, status_code=200 if model is not None else 503)


def validate_questions(questions):
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 16:
        raise ValueError("Provide 1 to 16 questions")
    for key, question in questions.items():
        if not isinstance(key, str) or not key or not isinstance(question, dict):
            raise ValueError("Questions must be named objects")
        kind, criteria = question.get("type"), question.get("criteria")
        if kind not in ("choice", "score", "noul"):
            raise ValueError("Unsupported question type")
        if kind == "choice" and (not isinstance(criteria, dict) or not 1 <= len(criteria) <= 32):
            raise ValueError("choice requires 1 to 32 named options")
        if kind == "score" and (not isinstance(criteria, list) or not 1 <= len(criteria) <= 32):
            raise ValueError("score requires 1 to 32 descriptions")
        if kind == "noul" and criteria is not None and (not isinstance(criteria, dict) or not set(criteria) <= {"true", "false"}):
            raise ValueError("noul criteria must be an object containing true and/or false")


def reject_nonfinite(value):
    raise ValueError("Non-finite JSON number")


def validate_and_encode(body):
    with get_tracer().start_as_current_span("clef.encode", record_exception=False, set_status_on_exception=False) as span:
        span.set_attribute("gen_ai.request.model", MODEL_NAME)
        try:
            payload = json.loads(body, parse_constant=reject_nonfinite)
            if not isinstance(payload, dict) or "state" not in payload:
                raise ValueError("Provide an object with model, state and questions")
            if payload.get("model") != MODEL_NAME:
                raise ValueError(f"model must be {MODEL_NAME}")
            if any(key in payload for key in ("videos", "media_kwargs")):
                raise ValueError("Videos and media overrides are not supported")
            validate_questions(payload.get("questions"))
            payload = prepare_images(payload)
            span.set_attribute("clef.questions.count", len(payload["questions"]))
            span.set_attribute("clef.images.count", len(payload.get("images", [])))
            if processor is not None and encode_record_fn is not None:
                # Tokenizer/processor internals are mutable. CPU validation can
                # run while inference is busy without racing this shared object.
                with encode_lock:
                    encoded = encode_record_fn(processor.tokenizer, payload, max_length=1_000_000, processor=processor)
            else:
                encoded = SimpleNamespace(input_ids=[0] * 16)
            span.set_attribute("clef.input_tokens", len(encoded.input_ids))
            if len(encoded.input_ids) > MAX_CONTEXT_TOKENS:
                raise InputRejected("Input exceeds the 2048-token context; shorten the state, schema, or images", OUTCOME_CONTEXT_EXCEEDED)
        except InputRejected as exc:
            set_span_outcome(span, exc.outcome)
            raise
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            set_span_outcome(span, OUTCOME_VALIDATION_ERROR)
            # Library exceptions may interpolate raw input; return a fixed error.
            raise InputRejected("Invalid JSON, schema, or inline image") from exc
        except Exception:
            set_span_outcome(span, OUTCOME_ERROR)
            raise
        set_span_outcome(span, OUTCOME_SUCCESS)
        return payload, encoded


def busy_response(has_images):
    retry = calculate_retry_after(has_images)
    return JSONResponse(
        status_code=429,
        content={"detail": f"Server busy; retry after {retry} seconds", "retry_after_seconds": retry, "queue_depth": batch_queue.qsize()},
        headers={"Retry-After": str(retry)},
    )


async def disconnected(request):
    # Uvicorn does not cancel the handler when a socket disappears. Consume the
    # ASGI disconnect only after the entire bounded body has been read.
    if not hasattr(request, "receive"):
        await asyncio.Future()  # Direct-handler test fixtures have no transport.
    while True:
        if (await request.receive())["type"] == "http.disconnect":
            return


async def wait_for_item(item, request):
    started = asyncio.create_task(item.started.wait())
    gone = asyncio.create_task(disconnected(request))
    try:
        done, _ = await asyncio.wait(
            [item.future, started, gone],
            timeout=max(0, item.deadline - time.monotonic()), return_when=asyncio.FIRST_COMPLETED,
        )
        if gone in done:
            raise ClientDisconnect()
        if item.future in done:
            return item.future.result()
        if not item.started.is_set():
            raise QueueExpired()
        done, _ = await asyncio.wait([item.future, gone], return_when=asyncio.FIRST_COMPLETED)
        if gone in done:
            raise ClientDisconnect()
        return item.future.result()
    finally:
        started.cancel()
        gone.cancel()
        batch_queue.discard(item)
        if not item.future.done():
            item.future.cancel()
        elif not item.future.cancelled():
            item.future.exception()  # Consume a raced failure after disconnect.
        await asyncio.gather(started, gone, return_exceptions=True)


@app.post("/v1/systemone")
async def systemone(request: Request):
    span = get_current_span()
    began = time.monotonic()
    status, outcome = 500, OUTCOME_ERROR
    item = None
    try:
        body = await read_bounded_body(request.stream())
        payload, encoded = await run_in_threadpool(validate_and_encode, body)
        if hasattr(request, "is_disconnected") and await request.is_disconnected():
            raise ClientDisconnect()
        has_images = bool(payload.get("images"))
        if batch_queue.qsize() >= CLEF_MAX_QUEUE_DEPTH or estimate_wait_seconds(has_images) > CLEF_MAX_QUEUE_WAIT_SECONDS:
            status, outcome = 429, OUTCOME_RATE_LIMITED
            return busy_response(has_images)
        ensure_worker_started()
        item = QueueItem(encoded, payload, asyncio.get_running_loop().create_future(), has_images)
        batch_queue.put_nowait(item)
        try:
            result = await wait_for_item(item, request)
        except QueueExpired:
            status, outcome = 429, OUTCOME_RATE_LIMITED
            return busy_response(has_images)
        status, outcome = 200, OUTCOME_SUCCESS
        return result
    except RequestTooLarge as exc:
        status, outcome = 413, OUTCOME_VALIDATION_ERROR
        raise HTTPException(status, "Request exceeds the 64 KiB limit") from exc
    except InputRejected as exc:
        status, outcome = 400, exc.outcome
        raise HTTPException(status, str(exc)) from exc
    except ClientDisconnect:
        status, outcome = 499, OUTCOME_CANCELLED
        return JSONResponse({"detail": "Caller disconnected"}, status_code=status)
    except asyncio.CancelledError:
        status, outcome = 499, OUTCOME_CANCELLED
        raise
    except Exception as exc:
        raise HTTPException(500, "Internal server error") from exc
    finally:
        if span is not None:
            span.set_attribute("gen_ai.request.model", MODEL_NAME)
            if item is not None:
                span.set_attribute("clef.queue_wait_ms", item.queue_wait_ms)
            set_span_outcome(span, outcome)
        record_request(MODEL_NAME, outcome, status, time.monotonic() - began)


# Instrument before Starlette constructs its middleware stack for lifespan.
configure_logging()
init_telemetry(app)


if __name__ == "__main__":
    class StartupServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            if not self.started:
                os._exit(0)
            super().handle_exit(sig, frame)

    StartupServer(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                                timeout_keep_alive=75, access_log=False)).run()
