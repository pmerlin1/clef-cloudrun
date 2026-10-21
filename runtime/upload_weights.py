"""Download one pinned HF file at a time, verify, upload, then release disk space."""
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from models import model

SELECTED_MODEL = model()
REVISION = SELECTED_MODEL["revision"]


def default_bucket():
    from settings import settings
    return settings()["bucket"]


def inspect_object(bucket, name):
    """Only a genuine HTTP 404 allows a new download; all other errors stop."""
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    object_name = urllib.parse.quote(f"{REVISION}/{name}", safe="")
    request = urllib.request.Request(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{object_name}",
        headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(
                f"Cannot inspect gs://{bucket}/{REVISION}/{name}: "
                f"HTTP {exc.code}; no download attempted") from exc
        return None


def upload_entry(entry, bucket):
    name = entry["name"]
    dest = f"gs://{bucket}/{REVISION}/{name}"
    obj = inspect_object(bucket, name)
    if obj is not None:
        metadata = obj.get("metadata", {})
        if int(obj.get("size", -1)) == entry["size"] and metadata.get("sha256") == entry["sha256"]:
            print(f"Verified existing {name}", flush=True)
            return
        raise RuntimeError(f"Unexpected existing object: {dest}")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        digest = hashlib.sha256()
        url = f"https://huggingface.co/{SELECTED_MODEL['hf_repo']}/resolve/{REVISION}/{name}"
        print(f"Downloading {name} ({entry['size']} bytes)", flush=True)
        with urllib.request.urlopen(url, timeout=120) as response, path.open("wb") as output:
            downloaded = 0
            while chunk := response.read(8 * 1024 * 1024):
                downloaded += len(chunk)
                if downloaded > entry["size"]:
                    raise RuntimeError(f"Download exceeds pinned size: {name}")
                output.write(chunk)
                digest.update(chunk)
        if path.stat().st_size != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise RuntimeError(f"Integrity mismatch: {name}")
        # Parallel composite uploads need permission to delete temporary
        # components; the publisher role is intentionally create-only.
        subprocess.run([
            "gcloud", "storage", "cp", str(path), dest,
            f"--custom-metadata=sha256={entry['sha256']}", "--if-generation-match=0",
        ], check=True, env={
            **os.environ,
            "CLOUDSDK_STORAGE_PARALLEL_COMPOSITE_UPLOAD_ENABLED": "False",
        })


def main():
    bucket = default_bucket()
    manifest = json.loads(Path(__file__).with_name(SELECTED_MODEL["manifest"]).read_text())
    subprocess.run(["gcloud", "storage", "buckets", "describe", f"gs://{bucket}"],
                   check=True, stdout=subprocess.DEVNULL)
    for entry in manifest:
        upload_entry(entry, bucket)
    print("All pinned model files verified and uploaded", flush=True)


if __name__ == "__main__":
    main()
