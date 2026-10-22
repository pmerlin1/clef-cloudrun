"""Pinned local weights with separately measured transfer and SHA-256 phases."""
import contextvars
import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.cloud import storage
from google.cloud.storage import transfer_manager
from models import model
from requests.adapters import HTTPAdapter
from telemetry import OUTCOME_ERROR, OUTCOME_SUCCESS, get_tracer, set_span_outcome

FILE_WORKERS = 4
CANDIDATES = (8, 12, 16, 20)


def bucket():
    client = storage.Client(project=os.environ["GOOGLE_CLOUD_PROJECT"])
    client._http.mount("https://", HTTPAdapter(pool_connections=32, pool_maxsize=32))
    return client.bucket(os.environ["MODEL_BUCKET"])


def manifest():
    selected = model()
    configured_revision = os.environ.get("MODEL_REVISION")
    if configured_revision and configured_revision != selected["revision"]:
        raise RuntimeError("MODEL and MODEL_REVISION select different model releases")
    return json.loads(Path(__file__).with_name(selected["manifest"]).read_text())


def worker_budget():
    workers = int(os.environ.get("MODEL_DOWNLOAD_WORKERS", "20"))
    if workers not in CANDIDATES:
        raise ValueError("MODEL_DOWNLOAD_WORKERS must be 8, 12, 16, or 20")
    return workers


def download_set(model_bucket, entries, destination, total_workers):
    if total_workers not in CANDIDATES:
        raise ValueError("Unsupported worker budget")
    started = time.monotonic()
    revision = os.environ["MODEL_REVISION"]

    def download(entry):
        began = time.monotonic()
        blob = model_bucket.get_blob(f"{revision}/{entry['name']}", timeout=120)
        if blob is None or blob.size != entry["size"] or (blob.metadata or {}).get("sha256") != entry["sha256"]:
            raise RuntimeError(f"Model object mismatch: {entry['name']}")
        path = destination / entry["name"]
        if blob.size > 64 * 1024 * 1024:
            transfer_manager.download_chunks_concurrently(
                blob, str(path), max_workers=total_workers // FILE_WORKERS,
                worker_type=transfer_manager.THREAD, deadline=600,
                download_kwargs={"if_generation_match": blob.generation, "timeout": 120},
            )
        else:
            blob.download_to_filename(str(path), if_generation_match=blob.generation, timeout=120)
        return {"name": entry["name"], "transfer_seconds": round(time.monotonic() - began, 3)}

    with ThreadPoolExecutor(max_workers=FILE_WORKERS) as pool:
        futures = [pool.submit(contextvars.copy_context().run, download, entry) for entry in entries]
        files = [future.result() for future in futures]
    transfer_seconds = time.monotonic() - started
    checksum_started = time.monotonic()

    def verify(entry):
        began = time.monotonic()
        path = destination / entry["name"]
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(8 * 1024 * 1024):
                digest.update(chunk)
        if path.stat().st_size != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise RuntimeError(f"Model checksum mismatch: {entry['name']}")
        return round(time.monotonic() - began, 3)

    with ThreadPoolExecutor(max_workers=FILE_WORKERS) as pool:
        checksums = list(pool.map(verify, entries))
    for file, seconds in zip(files, checksums):
        file["checksum_seconds"] = seconds
    result = {
        "total_workers": total_workers, "file_workers": FILE_WORKERS,
        "bytes": sum(e["size"] for e in entries),
        "transfer_seconds": round(transfer_seconds, 3),
        "checksum_seconds": round(time.monotonic() - checksum_started, 3),
        "elapsed_seconds": round(time.monotonic() - started, 3), "files": files,
    }
    print("Clef model transfer: " + json.dumps(result, sort_keys=True), flush=True)
    return result


def preload():
    tracer = get_tracer()
    with tracer.start_as_current_span("clef.preload", record_exception=False, set_status_on_exception=False) as span:
        destination = Path(tempfile.mkdtemp(prefix="clef-model-"))
        model_bucket = bucket()
        manifest_entries = manifest()
        span.set_attribute("gen_ai.request.model", os.environ.get("MODEL", "clef"))
        span.set_attribute("clef.preload.files_count", len(manifest_entries))
        try:
            res = download_set(model_bucket, manifest_entries, destination, worker_budget())
        except Exception:
            set_span_outcome(span, OUTCOME_ERROR)
            raise
        span.set_attribute("clef.preload.bytes", res.get("bytes", 0))
        set_span_outcome(span, OUTCOME_SUCCESS)
        return str(destination)
