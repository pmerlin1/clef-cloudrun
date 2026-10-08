"""Read Cloud Run metadata and instance metrics without invoking the service."""
import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "runtime"))
from settings import settings

config = settings()
common = [f"--project={config['project']}", f"--region={config['region']}", "--format=json"]
with tempfile.TemporaryDirectory() as directory:
    paths = []
    for command, filename in (("describe", "service.json"), ("get-iam-policy", "iam.json")):
        content = subprocess.check_output(
            ["gcloud", "run", "services", command, config["service"], *common], text=True)
        path = Path(directory) / filename
        path.write_text(content)
        paths.append(str(path))
    subprocess.run([sys.executable, str(root / "runtime" / "verify_service.py"), *paths], check=True)

now = datetime.datetime.now(datetime.timezone.utc)
params = {
    "filter": 'metric.type="run.googleapis.com/container/instance_count" '
              f'AND resource.labels.service_name="{config["service"]}" '
              f'AND resource.labels.location="{config["region"]}"',
    "interval.startTime": (now - datetime.timedelta(minutes=15)).isoformat(),
    "interval.endTime": now.isoformat(), "view": "FULL",
}
token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
url = f'https://monitoring.googleapis.com/v3/projects/{config["project"]}/timeSeries?'
request = urllib.request.Request(url + urllib.parse.urlencode(params), headers={"Authorization": "Bearer " + token})
with urllib.request.urlopen(request, timeout=30) as response:
    data = json.load(response)
series = data.get("timeSeries", [])
for item in series:
    print(json.dumps({"revision": item["resource"]["labels"].get("revision_name"),
                      "state": item["metric"].get("labels"), "latest": item.get("points", [])[:1]}))
if not series:
    print("No recent instance samples. Absence of metrics does not prove zero running instances.")
print("Metrics arrive with a delay. Inspect timestamps and both active and idle states across revisions.")
