"""Bounded text/image Clef API. Cloud Run enforces caller IAM before this app."""
import os
import sys
from contextlib import asynccontextmanager
import threading
import time
from starlette.concurrency import run_in_threadpool

from fastapi import FastAPI, HTTPException, Request
import uvicorn
from request_body import read_bounded_body, RequestTooLarge

model = processor = infer = None
fast_path = None
lock = threading.BoundedSemaphore(2)


@asynccontextmanager
async def lifespan(app):
    global model, processor, infer, fast_path
    from fast_path import FastPathAudit, require_fast_path
    require_fast_path()
    from preload import preload
    path = preload()
    sys.path.insert(0, path)
    from joint_schema_model import load_release_model, systemone
    model, processor = load_release_model(path, device="cuda", local_files_only=True)
    fast_path = FastPathAudit(model)
    infer = systemone
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def health():
    return {"ready": model is not None}


@app.post("/v1/systemone")
async def systemone(request: Request):
    try:
        body = await read_bounded_body(request.stream())
    except RequestTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    try:
        import json
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("Expected an object")
        if payload.get("model") != "clef":
            raise ValueError("model must be clef")
        if any(k in payload for k in ("videos", "media_kwargs")):
            raise ValueError("Videos and media overrides are not supported")
        questions = payload.get("questions")
        if not isinstance(questions, dict) or not 1 <= len(questions) <= 16:
            raise ValueError("Provide 1 to 16 questions")
        for q in questions.values():
            if not isinstance(q, dict):
                raise ValueError("Questions must be objects")
            kind = q.get("type")
            criteria = q.get("criteria")
            if kind not in ("choice", "score", "noul"):
                raise ValueError("Unsupported question type")
            if kind == "choice" and (not isinstance(criteria, dict) or not 1 <= len(criteria) <= 32):
                raise ValueError("choice requires 1 to 32 named options")
            if kind == "score" and (not isinstance(criteria, list) or not 1 <= len(criteria) <= 32):
                raise ValueError("score requires 1 to 32 descriptions")
        return await run_in_threadpool(inference, payload)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HTTPException(400, str(exc)) from exc


def inference(payload):
    with lock:
        from media import prepare_images
        from joint_schema_model import encode_record
        payload = prepare_images(payload)
        # The pinned upstream encoder silently truncates state when max_length
        # is exceeded. Count the full request first so the API rejects it.
        full = encode_record(processor.tokenizer, payload, max_length=1_000_000,
                             processor=processor)
        if len(full.input_ids) > 2048:
            raise ValueError("Input exceeds the 2048-token context; shorten the state, schema, or images")
        started = time.monotonic()
        fast_path.begin_inference()
        result = infer(model, processor, payload, max_length=2048)
        fast_path.verify_inference()
        import torch
        print(f"Clef inference completed: seconds={time.monotonic()-started:.3f}; "
              f"peak_gpu_allocated_bytes={torch.cuda.max_memory_allocated()}", flush=True)
        return result


if __name__ == "__main__":
    class StartupServer(uvicorn.Server):
        def handle_exit(self, sig, frame):
            # Model startup is synchronous. A graceful-exit flag would wait
            # for the model to finish loading after SIGTERM.
            if not self.started:
                os._exit(0)
            super().handle_exit(sig, frame)

    StartupServer(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))).run()
