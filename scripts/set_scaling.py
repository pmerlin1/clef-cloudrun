"""Explicit operator controls; pausing at zero disables the API until resumed."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from settings import settings

if len(sys.argv) != 2 or sys.argv[1] not in ("0", "auto"):
    raise SystemExit("Usage: PROJECT=your-project python3 scripts/set_scaling.py 0|auto")
mode = sys.argv[1]
config = settings()
common = [f"--project={config['project']}", f"--region={config['region']}"]


def service():
    return json.loads(subprocess.check_output(
        ["gcloud", "run", "services", "describe", config["service"], *common, "--format=json"], text=True))


before = service()
limits = ["--min=0", "--max=1"] if mode == "auto" else []
subprocess.run(["gcloud", "run", "services", "update", config["service"], *common,
                "--scaling=" + mode, *limits, "--quiet"], check=True)
after = service()
assert before["status"]["latestCreatedRevisionName"] == after["status"]["latestCreatedRevisionName"]
assert before["spec"]["template"]["spec"] == after["spec"]["template"]["spec"]
annotations = after["metadata"]["annotations"]
assert annotations.get("run.googleapis.com/invoker-iam-disabled", "false") == "false"
expected = "manual" if mode == "0" else "automatic"
assert annotations.get("run.googleapis.com/scalingMode", "automatic") == expected
if mode == "0":
    assert int(annotations["run.googleapis.com/manualInstanceCount"]) == 0
else:
    assert int(annotations.get("run.googleapis.com/minScale", 0)) == 0
    assert int(annotations["run.googleapis.com/maxScale"]) == 1
print(json.dumps({"mode": expected, "revision": after["status"]["latestCreatedRevisionName"],
                  "iam_enforced": True}))
print("Manual zero requests shutdown; use instance metrics to confirm it has finished.")
