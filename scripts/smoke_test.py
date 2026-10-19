"""One explicit, paid authenticated request. Never called by CI or deployment."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from settings import settings

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--allow-paid-request", action="store_true")
parser.add_argument("--image", type=Path, help="Optional local PNG/JPEG, 16–512 px and at most 48 KiB")
args = parser.parse_args()
if not args.allow_paid_request:
    parser.error("This starts a billable GPU if cold; pass --allow-paid-request")
config = settings()
url = subprocess.check_output([
    "gcloud", "run", "services", "describe", config["service"],
    f"--project={config['project']}", f"--region={config['region']}", "--format=value(status.url)"], text=True).strip()
token = subprocess.check_output(["gcloud", "auth", "print-identity-token"], text=True).strip()
payload = {"model": config["model_name"], "state": "Our checkout is down and orders are blocked.", "questions": {
    "team": {"type": "choice", "criteria": {"billing": "Invoices and payments", "technical": "Bugs and outages"}},
    "outage": {"type": "noul", "instructions": "Is a service down?"},
}}
if args.image:
    from media import prepare_images
    extension = args.image.suffix.lower()
    media_type = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}.get(extension)
    if media_type is None or args.image.stat().st_size > 48 * 1024:
        parser.error("Image must be a PNG/JPEG no larger than 48 KiB")
    payload["images"] = [f"data:image/{media_type};base64," + base64.b64encode(args.image.read_bytes()).decode()]
    prepare_images(payload)  # Fail locally before making a billable request.
    payload["state"] = "Inspect the attached image and classify its dominant color."
    payload["questions"] = {"dominant_color": {"type": "choice", "criteria": {
        "red": "The image is predominantly red",
        "blue": "The image is predominantly blue",
        "other": "The image is neither predominantly red nor predominantly blue",
    }}}
body = json.dumps(payload).encode()
if len(body) > 65536:
    parser.error("JSON request exceeds 64 KiB")
request = urllib.request.Request(url + "/v1/systemone", data=body, headers={
    "Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
started = time.monotonic()
try:
    with urllib.request.urlopen(request, timeout=960) as response:
        result = json.load(response)
except urllib.error.HTTPError as exc:
    raise SystemExit(f"HTTP {exc.code}: {exc.read(4096).decode(errors='replace')}") from None
print(json.dumps({"elapsed_seconds": round(time.monotonic() - started, 3), "result": result}, indent=2))
print("One request sent. No retries. The instance may remain billable while idle; see README for stopping a test batch.")
