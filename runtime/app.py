"""Bounded text/image Clef API with GPU micro-batching and 429 Retry-After backpressure.

Cloud Run enforces caller IAM before this app.
"""

import asyncio
import json
import logging
import math
import os
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from media import prepare_images
from request_body import RequestTooLarge, read_bounded_body
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from telemetry import (
    OUTCOME_CONTEXT_EXCEEDED,
    OUTCOME_ERROR,
    OUTCOME_RATE_LIMITED,
    OUTCOME_SUCCESS,
    OUTCOME_VALIDATION_ERROR,
    configure_logging,
    get_current_span,
    get_tracer,
    init_telemetry,
    set_span_outcome,
)

model = processor = infer = None
collate_records_fn = systemone_answer_fn = encode_record_fn = None
fast_path = None
gpu_lock = threading.Lock()

MODEL_NAME = os.environ.get("MODEL", "clef")
MAX_BATCH_SIZE = 16 if MODEL_NAME == "clef-flash" else 8
MAX_BATCH_PADDED_TOKENS = 8192
MAX_CONTEXT_TOKENS = 2048

CLEF_MAX_QUEUE_DEPTH = int(os.environ.get("CLEF_MAX_QUEUE_DEPTH", "64"))
CLEF_MAX_QUEUE_WAIT_SECONDS = float(os.environ.get("CLEF_MAX_QUEUE_WAIT_SECONDS", "15.0"))

INITIAL_AVG_BATCH_SECONDS = 0.16 if MODEL_NAME == "clef-flash" else 0.25
INITIAL_AVG_IMAGE_BATCH_SECONDS = 1.5

avg_batch_seconds = INITIAL_AVG_BATCH_SECONDS
avg_batch_seconds_image = INITIAL_AVG_IMAGE_BATCH_SECONDS
in_flight_batches = 0


class QueueItem:
    def __init__(self, record, payload, future, has_images=False):
        self.record = record
        self.payload = payload
        self.future = future
        self.has_images = has_images
        self.enqueued_at = time.monotonic()
        self.cancelled = False


class MicroBatchQueue:
    def __init__(self):
        self._deque = deque()
        self._event = asyncio.Event()

    def qsize(self):
        return sum(1 for item in self._deque if not item.cancelled)

    def put_nowait(self, item):
        self._deque.append(item)
        self._event.set()

    async def get_batch(self, max_batch_size, max_padded_tokens):
        while True:
            while not self._deque:
                self._event.clear()
                await self._event.wait()

            active = []
            while self._deque:
                item = self._deque.popleft()
                if not item.cancelled:
                    active.append(item)

            if not active:
                continue

            active.sort(key=lambda it: len(it.record.input_ids) if hasattr(it.record, "input_ids") else 0)

            batch = []
            overflow = []
            max_len = 0
            for item in active:
                tok_len = len(item.record.input_ids) if hasattr(item.record, "input_ids") else 0
                new_max = max(max_len, tok_len)
                if len(batch) < max_batch_size and (len(batch) + 1) * new_max <= max_padded_tokens:
                    batch.append(item)
                    max_len = new_max
                else:
                    overflow.append(item)

            for item in reversed(overflow):
                self._deque.appendleft(item)
            if self._deque:
                self._event.set()

            return batch


batch_queue = MicroBatchQueue()
worker_task = None


def estimate_wait_seconds(has_images=False):
    q = batch_queue.qsize()
    eff_avg = avg_batch_seconds_image if has_images else avg_batch_seconds
    in_flight = 1 if in_flight_batches > 0 else 0
    batches_needed = math.ceil(q / MAX_BATCH_SIZE)
    return (batches_needed + in_flight) * eff_avg


def calculate_retry_after(has_images=False):
    estimated_seconds = estimate_wait_seconds(has_images=has_images)
    return max(1, min(30, math.ceil(estimated_seconds)))


def run_gpu_batch_sync(valid_items):
    if inference is not None and collate_records_fn is None:
        return [inference(item.payload) for item in valid_items]

    with gpu_lock:
        if fast_path is not None:
            fast_path.begin_inference()
        records_to_collate = [
            it.payload if collate_records_fn is _mock_collate else it.record
            for it in valid_items
        ]
        batch = collate_records_fn(records_to_collate)
        results = systemone_answer_fn(model, processor, batch, max_length=MAX_CONTEXT_TOKENS)
        if fast_path is not None:
            fast_path.verify_inference()
        try:
            import torch
            if torch.cuda.is_available():
                print(f"Clef batch inference completed: batch_size={len(valid_items)}; "
                      f"peak_gpu_allocated_bytes={torch.cuda.max_memory_allocated()}", flush=True)
        except ImportError:
            pass
        return results


async def gpu_batch_worker():
    global in_flight_batches, avg_batch_seconds, avg_batch_seconds_image
    tracer = get_tracer()
    while True:
        try:
            batch_items = await batch_queue.get_batch(MAX_BATCH_SIZE, MAX_BATCH_PADDED_TOKENS)
            valid_items = [it for it in batch_items if not it.cancelled]
            if not valid_items:
                continue

            in_flight_batches = 1
            has_images = any(it.has_images for it in valid_items)
            now = time.monotonic()
            for it in valid_items:
                it.queue_wait_ms = (now - it.enqueued_at) * 1000.0

            with tracer.start_as_current_span("clef.infer_batch") as span:
                span.set_attribute("gen_ai.request.model", MODEL_NAME)
                span.set_attribute("clef.batch_size", len(valid_items))
                max_tokens = max((len(it.record.input_ids) if hasattr(it.record, "input_ids") else 0)
                                 for it in valid_items)
                span.set_attribute("clef.max_tokens", max_tokens)

                started = time.monotonic()
                try:
                    results = await run_in_threadpool(run_gpu_batch_sync, valid_items)
                    elapsed = time.monotonic() - started
                    if has_images:
                        avg_batch_seconds_image = 0.2 * elapsed + 0.8 * avg_batch_seconds_image
                    else:
                        avg_batch_seconds = 0.2 * elapsed + 0.8 * avg_batch_seconds

                    for item, res in zip(valid_items, results):
                        if not item.cancelled and not item.future.done():
                            item.future.set_result(res)
                    logging.getLogger("clef").info(
                        "Batch inference finished: batch_size=%d",
                        len(valid_items),
                    )
                except Exception as exc:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    span.record_exception(exc)
                    for item in valid_items:
                        if not item.cancelled and not item.future.done():
                            item.future.set_exception(exc)
                finally:
                    in_flight_batches = 0
        except asyncio.CancelledError:
            break
        except Exception as worker_exc:  # noqa: BLE001
            print(f"Error in gpu_batch_worker: {worker_exc}", file=sys.stderr, flush=True)
            await asyncio.sleep(0.05)


def ensure_worker_started():
    global worker_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if worker_task is None or worker_task.done():
        worker_task = loop.create_task(gpu_batch_worker())


def _mock_collate(records):
    return records


def _mock_answer(m, p, batch, max_length=2048):
    return [
        {
            "accepted": it.get("state", "ok") if isinstance(it, dict) else "ok",
            "image_modes": [img.mode for img in it.get("images", [])] if isinstance(it, dict) else [],
        }
        for it in batch
    ]


def _mock_encode(tokenizer, payload, max_length=1_000_000, processor=None):
    return SimpleNamespace(input_ids=[0] * 16)


@asynccontextmanager
async def lifespan(app):
    global model, processor, infer, fast_path, worker_task
    global collate_records_fn, systemone_answer_fn, encode_record_fn

    configure_logging()
    init_telemetry(app)
    tracer = get_tracer()

    if os.environ.get("CLEF_MOCK_MODEL", "false").lower() in ("true", "1", "yes"):
        with tracer.start_as_current_span("clef.preload"):
            pass
        model = SimpleNamespace(name=MODEL_NAME)
        processor = SimpleNamespace(tokenizer=SimpleNamespace())
        collate_records_fn = _mock_collate
        systemone_answer_fn = _mock_answer
        encode_record_fn = _mock_encode
    else:
        from fast_path import FastPathAudit, require_fast_path
        require_fast_path()
        from preload import preload
        path = preload()
        sys.path.insert(0, path)
        import joint_schema_model
        load_release_model = joint_schema_model.load_release_model
        systemone = joint_schema_model.systemone
        collate_records_fn = joint_schema_model.collate_records
        systemone_answer_fn = joint_schema_model.systemone_answer
        encode_record_fn = joint_schema_model.encode_record

        model, processor = load_release_model(path, device="cuda", local_files_only=True)
        fast_path = FastPathAudit(model)
        infer = systemone

        # Free /tmp/clef-model/*.safetensors after load_release_model()
        for sf in Path(path).glob("*.safetensors"):
            try:
                sf.unlink()
            except OSError:
                pass

        # Warmup pass so Triton compiles GPU kernels during startup probe
        try:
            warmup_payload = {
                "model": MODEL_NAME,
                "state": "warmup",
                "questions": {"q": {"type": "noul"}},
            }
            warmup_rec = encode_record_fn(
                processor.tokenizer, warmup_payload, max_length=1_000_000, processor=processor
            )
            warmup_batch = collate_records_fn([warmup_rec])
            fast_path.begin_inference()
            systemone_answer_fn(model, processor, warmup_batch, max_length=MAX_CONTEXT_TOKENS)
            fast_path.verify_inference()
        except Exception as warmup_exc:  # noqa: BLE001
            print(f"Warning: Clef warmup pass failed: {warmup_exc}", file=sys.stderr, flush=True)

    loop = asyncio.get_running_loop()
    worker_task = loop.create_task(gpu_batch_worker())
    yield
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
inference = None


@app.get("/healthz")
def health():
    return {"ready": model is not None, "model": MODEL_NAME}


def validate_questions(questions):
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 16:
        raise ValueError("Provide 1 to 16 questions")
    for q in questions.values():
        if not isinstance(q, dict):
            raise TypeError("Questions must be objects")
        kind = q.get("type")
        criteria = q.get("criteria")
        if kind not in ("choice", "score", "noul"):
            raise ValueError("Unsupported question type")
        if kind == "choice" and (not isinstance(criteria, dict) or not 1 <= len(criteria) <= 32):
            raise ValueError("choice requires 1 to 32 named options")
        if kind == "score" and (not isinstance(criteria, list) or not 1 <= len(criteria) <= 32):
            raise ValueError("score requires 1 to 32 descriptions")


@app.post("/v1/systemone")
async def systemone(request: Request):
    tracer = get_tracer()
    server_span = get_current_span()
    try:
        body = await read_bounded_body(request.stream())
    except RequestTooLarge as exc:
        set_span_outcome(server_span, OUTCOME_VALIDATION_ERROR, str(exc))
        raise HTTPException(413, str(exc)) from exc

    # Fast CPU schema validation outside GPU lock
    try:
        try:
            payload = json.loads(body)
        except Exception as json_err:
            raise ValueError("Invalid JSON") from json_err

        if not isinstance(payload, dict):
            raise TypeError("Expected an object")
        if payload.get("model") != MODEL_NAME:
            raise ValueError(f"model must be {MODEL_NAME}")
        if any(k in payload for k in ("videos", "media_kwargs")):
            raise ValueError("Videos and media overrides are not supported")

        validate_questions(payload.get("questions"))
        payload = prepare_images(payload)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        set_span_outcome(server_span, OUTCOME_VALIDATION_ERROR, str(exc))
        raise HTTPException(400, str(exc)) from exc

    # Fast CPU token encoding and context check outside GPU lock
    has_images = bool(payload.get("images"))
    with tracer.start_as_current_span("clef.encode") as encode_span:
        encode_span.set_attribute("gen_ai.request.model", MODEL_NAME)
        encode_span.set_attribute("clef.questions.count", len(payload["questions"]))
        encode_span.set_attribute("clef.images.count", len(payload.get("images", [])))

        if processor is not None and encode_record_fn is not None:
            full = encode_record_fn(
                processor.tokenizer, payload, max_length=1_000_000, processor=processor
            )
            input_tokens = len(full.input_ids)
            encode_span.set_attribute("clef.input_tokens", input_tokens)
            if input_tokens > MAX_CONTEXT_TOKENS:
                set_span_outcome(encode_span, OUTCOME_CONTEXT_EXCEEDED, "context limit exceeded")
                set_span_outcome(server_span, OUTCOME_CONTEXT_EXCEEDED, "context limit exceeded")
                raise HTTPException(
                    400,
                    "Input exceeds the 2048-token context; shorten the state, schema, or images",
                )
        else:
            full = SimpleNamespace(input_ids=[0] * 16)
            encode_span.set_attribute("clef.input_tokens", 16)

    # Stage A: Fast Admission Control (Pre-Queue Gate)
    queue_depth = batch_queue.qsize()
    estimated_wait = estimate_wait_seconds(has_images=has_images)
    retry_after = calculate_retry_after(has_images=has_images)

    if queue_depth >= CLEF_MAX_QUEUE_DEPTH or estimated_wait > CLEF_MAX_QUEUE_WAIT_SECONDS:
        set_span_outcome(server_span, OUTCOME_RATE_LIMITED, "server busy")
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"Server busy; retry after {retry_after} seconds",
                "retry_after_seconds": retry_after,
                "queue_depth": queue_depth,
            },
            headers={"Retry-After": str(retry_after)},
        )

    # Stage B: Enqueue & Wait Deadline / Cancellation
    ensure_worker_started()
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    item = QueueItem(record=full, payload=payload, future=future, has_images=has_images)
    batch_queue.put_nowait(item)

    try:
        result = await asyncio.wait_for(future, timeout=CLEF_MAX_QUEUE_WAIT_SECONDS)
        if server_span and hasattr(item, "queue_wait_ms"):
            server_span.set_attribute("clef.queue_wait_ms", item.queue_wait_ms)
        set_span_outcome(server_span, OUTCOME_SUCCESS)
        logging.getLogger("clef").info("Systemone request finished: outcome=%s", OUTCOME_SUCCESS)
        return result
    except TimeoutError:
        item.cancelled = True
        set_span_outcome(server_span, OUTCOME_RATE_LIMITED, "queue deadline expired")
        retry_after = calculate_retry_after(has_images=has_images)
        q_now = batch_queue.qsize()
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"Server busy; retry after {retry_after} seconds",
                "retry_after_seconds": retry_after,
                "queue_depth": q_now,
            },
            headers={"Retry-After": str(retry_after)},
        )
    except asyncio.CancelledError:
        item.cancelled = True
        set_span_outcome(server_span, OUTCOME_RATE_LIMITED, "caller disconnected")
        raise
    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        traceback.print_exc()
        set_span_outcome(server_span, OUTCOME_ERROR, str(exc))
        raise HTTPException(500, "Internal server error") from exc


if __name__ == "__main__":
    class StartupServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            if not self.started:
                os._exit(0)
            super().handle_exit(sig, frame)

    StartupServer(
        uvicorn.Config(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "8080")),
            timeout_keep_alive=75,
        )
    ).run()
