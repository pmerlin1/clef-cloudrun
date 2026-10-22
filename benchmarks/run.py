"""Optional paid Cloud Run benchmark; explicitly resumes then pauses one service.

No deployment, credential-file access, inference retries, or automatic unpausing.
The deadline is best effort: API operations and instance shutdown take time.
Only service-policy bindings are inspected; inherited project/folder/organization
IAM is outside this preflight and must be reviewed separately.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def utc():
    return dt.datetime.now(dt.UTC).isoformat()


def token(kind):
    """Let gcloud manage credentials; tokens remain in memory and are never printed."""
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-" + kind + "-token", "--quiet"],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError):
        raise RuntimeError("gcloud authentication failed; authenticate separately before running") from None
    value = result.stdout.strip()
    if not value or any(character.isspace() for character in value):
        raise RuntimeError("gcloud returned an invalid token")
    return value


class Session:
    def __init__(self):
        self.access = token("access")
        self.identity = None
        self.resume_timeout = 480

    def authenticate_endpoint(self):
        self.identity = token("identity")

    def refresh_access(self):
        self.access = token("access")

    def call(self, url, body=None, method="GET", identity=False, timeout=30):
        bearer = self.identity if identity else self.access
        if not bearer:
            raise RuntimeError("Identity token unavailable")
        request = urllib.request.Request(
            url, data=json.dumps(body).encode() if body is not None else None,
            method=method, headers={"Authorization": "Bearer " + bearer,
                                    "Content-Type": "application/json"},
        )
        # Disable redirects so neither bearer token can be forwarded elsewhere.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise RuntimeError("Response exceeded one MiB")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            # Do not print request headers, tokens or server-provided error text.
            exc.close()
            return exc.code, {}

    def get(self, url):
        status, result = self.call(url)
        if status != 200 or not isinstance(result, dict):
            raise RuntimeError(f"Cloud Run metadata request failed (HTTP {status})")
        return result

    def scale(self, service_url, count):
        deadline = time.monotonic() + (self.resume_timeout if count else 90)
        before = self.get(service_url)
        scaling = dict(before.get("scaling", {}))
        scaling.update({"scalingMode": "MANUAL", "manualInstanceCount": count})
        status, operation = self.call(
            service_url + "?updateMask=scaling",
            {"name": before["name"], "etag": before["etag"], "scaling": scaling},
            method="PATCH",
        )
        if status != 200 or not isinstance(operation, dict):
            raise RuntimeError(f"Scaling update failed (HTTP {status})")
        while not operation.get("done"):
            if time.monotonic() >= deadline:
                raise TimeoutError("Scaling operation exceeded its startup/cleanup allowance")
            name = operation.get("name", "")
            if not re.fullmatch(r"projects/[^/]+/locations/[^/]+/operations/[^/]+", name):
                raise RuntimeError("Unexpected scaling operation resource")
            time.sleep(2)
            operation = self.get("https://run.googleapis.com/v2/" + name)
        if operation.get("error"):
            raise RuntimeError("Cloud Run scaling operation failed")
        return self.get(service_url)


def paused(service):
    scaling = service.get("scaling", {})
    return scaling.get("scalingMode") == "MANUAL" and scaling.get("manualInstanceCount", 0) == 0


def check_service(service, model=None):
    if not paused(service):
        raise RuntimeError("Start at MANUAL zero; refusing to interrupt an existing workload")
    if service.get("invokerIamDisabled", False):
        raise RuntimeError("Cloud Run IAM invocation checks must be enabled")
    template = service.get("template", {})
    scaling = template.get("scaling", {})
    if scaling.get("maxInstanceCount") != 1 or scaling.get("minInstanceCount", 0) != 0:
        raise RuntimeError("Require revision min=0/max=1")
    if service.get("scaling", {}).get("minInstanceCount", 0) != 0:
        raise RuntimeError("Require service minimum zero")
    containers = template.get("containers", [])
    if len(containers) != 1 or str(containers[0].get("resources", {}).get("limits", {}).get("nvidia.com/gpu")) != "1":
        raise RuntimeError("Require exactly one container with one GPU")
    if model is not None:
        deployed = [item.get("value") for item in containers[0].get("env", []) if item.get("name") == "MODEL"]
        # The legacy 27B runtime defaulted to clef before this variable existed.
        if deployed != [model] and not (model == "clef" and not deployed):
            raise RuntimeError("Configured MODEL does not match the deployed runtime; no paid change made")
    endpoint = service.get("uri", "")
    parsed = urllib.parse.urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".run.app") or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise RuntimeError("Refusing to send identity token to an unexpected endpoint")
    return endpoint.rstrip("/")


def load_dataset(path, model):
    raw = path.read_bytes()
    dataset = json.loads(raw)
    cases = dataset.get("cases", []) if isinstance(dataset, dict) else []
    if not 1 <= len(cases) <= 100:
        raise ValueError("Require 1–100 cases")
    ids = set()
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in ids:
            raise ValueError("Case IDs must be unique, nonempty strings")
        ids.add(case_id)
        if not isinstance(case.get("gold"), dict):
            raise TypeError("Each case needs gold answers")
        payload = case.get("payload")
        if not isinstance(payload, dict) or "state" not in payload or not isinstance(payload.get("questions"), dict):
            raise ValueError("Each case needs a state/questions payload")
        if len(json.dumps({**payload, "model": model}).encode()) > 65536:
            raise ValueError("A case exceeds the 64-KiB API limit")
    return raw, cases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT / "synthetic-cases.json")
    parser.add_argument("--output", type=Path, help="Result path under benchmarks/data/; never overwritten")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if not args.preflight_only and not args.allow_paid:
        parser.error("Use --allow-paid to incur costs, or --preflight-only for metadata checks")
    if not 300 <= args.max_seconds <= 600:
        parser.error("--max-seconds must be 300–600, including startup; cleanup may take longer")
    config = {key: os.environ.get(key, "") for key in ("PROJECT", "REGION", "SERVICE", "MODEL")}
    if (not re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", config["PROJECT"])
            or not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", config["REGION"])
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]", config["SERVICE"])
            or config["MODEL"] not in ("clef", "clef-flash")):
        parser.error("Set explicit valid PROJECT, REGION, SERVICE and MODEL=clef|clef-flash")
    raw_dataset, cases = load_dataset(args.dataset, config["MODEL"])
    result_path = (args.output or ROOT / "data" / ("results-" + config["MODEL"] + ".json")).resolve()
    if not result_path.is_relative_to((ROOT / "data").resolve()):
        parser.error("Results must stay under ignored benchmarks/data/; do not publish raw responses")
    if result_path.exists():
        parser.error("Result already exists; refusing an accidental paid rerun")
    session = Session()
    session.resume_timeout = args.max_seconds - 120
    service_url = ("https://run.googleapis.com/v2/projects/" + config["PROJECT"]
                   + "/locations/" + config["REGION"] + "/services/" + config["SERVICE"])
    before = session.get(service_url)
    endpoint = check_service(before, config["MODEL"])
    policy = session.get(service_url + ":getIamPolicy")
    if any(member in ("allUsers", "allAuthenticatedUsers")
           for binding in policy.get("bindings", []) for member in binding.get("members", [])):
        raise RuntimeError("Public IAM binding found; refusing to resume")
    status, permission_data = session.call(
        service_url + ":testIamPermissions",
        {"permissions": ["run.routes.invoke", "run.services.update"]}, method="POST",
    )
    if status != 200 or not {"run.routes.invoke", "run.services.update"}.issubset(permission_data.get("permissions", [])):
        raise RuntimeError("Current gcloud principal requires invoke/update permissions")
    session.authenticate_endpoint()
    # No request to the application is needed for preflight; metadata only.
    print("Preflight passed: IAM checks enabled; no public service-policy binding; "
          "one GPU, min=0/max=1, MANUAL zero", flush=True)
    if args.preflight_only:
        return
    result_path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the output before any mutation; a second runner cannot overwrite it.
    with result_path.open("x", encoding="utf-8") as stream:
        stream.write("{}\n")
    output = {"model": config["MODEL"], "started_at": utc(),
              "ready_revision": before.get("latestReadyRevision"),
              "traffic_revisions": [item.get("revision") for item in before.get("trafficStatuses", [])],
              "dataset_sha256": hashlib.sha256(json.dumps(json.loads(raw_dataset), sort_keys=True,
                                                          separators=(",", ":")).encode()).hexdigest(),
              "max_window_seconds": args.max_seconds, "results": [], "predictions": []}
    scaling_lock, save_lock = threading.Lock(), threading.Lock()
    stop = threading.Event()
    window = {"resume_attempted": False}

    def save():
        with save_lock:
            result_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")

    def save_cleanup():
        # A full disk must never prevent the next emergency pause attempt.
        try:
            save()
        except OSError:
            print("Unable to save cleanup status; inspect Cloud Run scaling and instance metrics", file=sys.stderr, flush=True)

    def pause():
        # Always recheck independently, including after a watchdog already ran.
        with scaling_lock:
            output["manual_zero_confirmed"] = False
            after = session.get(service_url)
            if not paused(after):
                if not window["resume_attempted"]:
                    raise RuntimeError("Another workload appeared before resume; leaving it untouched")
                session.scale(service_url, 0)
            after = session.get(service_url)
            if not paused(after):
                raise RuntimeError("Unable to confirm MANUAL zero; stop the service manually")
            if after.get("template") != before.get("template") or after.get("invokerIamDisabled", False):
                raise RuntimeError("Runtime template or IAM enforcement changed during this window")
            output["manual_zero_confirmed"] = True
            output["paused_at"] = utc()
            save_cleanup()

    def watchdog():
        stop.set()
        try:
            pause()
        except Exception as exc:  # noqa: BLE001
            output["watchdog_error"] = type(exc).__name__
            save_cleanup()
            print("Watchdog pause failed; final cleanup will retry", flush=True)

    def interrupted(signum, frame):
        raise KeyboardInterrupt("Signal received; pausing the paid window")

    old_handler = signal.signal(signal.SIGTERM, interrupted)
    started = time.monotonic()
    timer = threading.Timer(args.max_seconds, watchdog)
    timer.daemon = True
    timer.start()
    try:
        save()
        with scaling_lock:
            # Recheck just before mutation; stale preflight must not stop another workload.
            if stop.is_set() or not paused(session.get(service_url)):
                raise RuntimeError("Service changed or deadline reached before resume")
            # Mark before PATCH: a timeout can occur after the update was applied.
            window["resume_attempted"] = True
            session.scale(service_url, 1)
        for case in cases:
            remaining = args.max_seconds - (time.monotonic() - started)
            if remaining < 5 or stop.is_set():
                raise TimeoutError("Paid window deadline reached; no further requests")
            began = time.monotonic()
            status, body = session.call(
                endpoint + "/v1/systemone", {**case["payload"], "model": config["MODEL"]},
                method="POST", identity=True, timeout=min(remaining, 120),
            )
            output["results"].append({"id": case["id"], "status": status,
                                      "seconds": round(time.monotonic() - began, 3), "response": body})
            answers = body.get("answers", {}) if isinstance(body, dict) else {}
            output["predictions"].append({"id": case["id"], "answers": {
                name: answer.get("choice") for name, answer in answers.items() if isinstance(answer, dict)
            }})
            save()
            print(f"{len(output['results'])}/{len(cases)} HTTP {status}", flush=True)
            if status != 200:
                raise RuntimeError("Inference failed; stopping without retries")
    except BaseException as exc:
        output["error"] = type(exc).__name__
        raise
    finally:
        cleanup_error = None
        try:
            # Final cleanup retries independently of the watchdog. A timeout may
            # mean the first PATCH succeeded; each attempt starts with a new GET.
            for attempt in range(1, 4):
                try:
                    pause()
                    cleanup_error = None
                    break
                except Exception as exc:  # noqa: BLE001
                    cleanup_error = exc
                    output.setdefault("cleanup_attempt_errors", []).append(
                        {"attempt": attempt, "error": type(exc).__name__})
                    output["cleanup_error"] = type(exc).__name__
                    save_cleanup()
                    if not window["resume_attempted"]:
                        # Never stop a workload created by another operator.
                        break
                    if attempt < 3:
                        print("Pause verification failed; refreshing access and retrying cleanup", flush=True)
                        try:
                            session.refresh_access()
                        except Exception as refresh_error:  # noqa: BLE001
                            output.setdefault("cleanup_refresh_errors", []).append(type(refresh_error).__name__)
            if cleanup_error is not None:
                if window["resume_attempted"]:
                    print("CLEANUP FAILED: GPU shutdown is unconfirmed. Run immediately: "
                          f"gcloud run services update {config['SERVICE']} --project={config['PROJECT']} "
                          f"--region={config['REGION']} --scaling=0 --quiet; then verify instance metrics.",
                          file=sys.stderr, flush=True)
                else:
                    print("Cleanup did not alter the other operator's workload.", file=sys.stderr, flush=True)
                raise cleanup_error
            output.pop("cleanup_error", None)
            print("Cleanup confirmed: MANUAL zero; verify delayed instance metrics for shutdown", flush=True)
        finally:
            timer.cancel()
            signal.signal(signal.SIGTERM, old_handler)
            output["finished_at"] = utc()
            output["wall_seconds"] = round(time.monotonic() - started, 3)
            save_cleanup()


if __name__ == "__main__":
    main()
