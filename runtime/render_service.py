"""Render the sole GPU revision specification, and detect identical releases."""
import json
from copy import deepcopy
import re
import sys
from settings import settings


def service(image):
    config = settings()
    project = config["project"]
    repository = config["repository"]
    if not re.fullmatch(re.escape(repository) + r"@sha256:[a-f0-9]{64}", image):
        raise ValueError("Use a Clef repository image digest")
    return {
        "apiVersion": "serving.knative.dev/v1", "kind": "Service",
        "metadata": {"name": config["service"], "annotations": {
            "run.googleapis.com/ingress": "all", "run.googleapis.com/minScale": "0",
            "run.googleapis.com/maxScale": "1", "run.googleapis.com/invoker-iam-disabled": "false",
            "run.googleapis.com/scalingMode": "automatic",
        }},
        "spec": {"template": {"metadata": {"annotations": {
            "autoscaling.knative.dev/minScale": "0", "autoscaling.knative.dev/maxScale": "1",
            "run.googleapis.com/execution-environment": "gen2",
            "run.googleapis.com/gpu-zonal-redundancy-disabled": "true",
            "run.googleapis.com/cpu-throttling": "false",
            "run.googleapis.com/vpc-access-egress": "all-traffic",
            "run.googleapis.com/network-interfaces": json.dumps([
                {"network": config["service"], "subnetwork": config["service"]}]),
        }}, "spec": {
            "containerConcurrency": 8, "timeoutSeconds": 900,
            "serviceAccountName": config["runtime_account"],
            "nodeSelector": {"run.googleapis.com/accelerator": "nvidia-rtx-pro-6000"},
            "containers": [{
                "image": image, "ports": [{"containerPort": 8080}],
                "env": [
                    {"name": "GOOGLE_CLOUD_PROJECT", "value": project},
                    {"name": "MODEL_BUCKET", "value": config["bucket"]},
                    {"name": "MODEL_REVISION", "value": "2f3de3dd85f379784083b0814d997ab627200f0c"},
                ],
                "resources": {"limits": {"cpu": "20", "memory": "80Gi", "nvidia.com/gpu": "1"}},
                "startupProbe": {"timeoutSeconds": 1, "periodSeconds": 5, "failureThreshold": 180,
                                 "httpGet": {"path": "/healthz", "port": 8080}},
            }],
        }}, "traffic": [{"latestRevision": True, "percent": 100}]},
    }


def contains(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and contains(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(contains(a, e) for a, e in zip(actual, expected))
    return actual == expected


def needs_release(actual, desired):
    actual, desired = deepcopy(actual), deepcopy(desired)
    annotations = actual["metadata"].setdefault("annotations", {})
    annotations.setdefault("run.googleapis.com/minScale", "0")
    annotations.setdefault("run.googleapis.com/invoker-iam-disabled", "false")
    annotations.setdefault("run.googleapis.com/scalingMode", "automatic")
    template_annotations = actual["spec"]["template"]["metadata"].setdefault("annotations", {})
    template_annotations.setdefault("autoscaling.knative.dev/minScale", "0")
    for document in (actual, desired):
        template = document["spec"]["template"]
        key = "run.googleapis.com/network-interfaces"
        value = template["metadata"]["annotations"].get(key)
        if value:
            template["metadata"]["annotations"][key] = json.loads(value)
        for container in template["spec"].get("containers", []):
            container["env"] = sorted(container.get("env", []), key=lambda e: e["name"])
    return not contains(actual, desired)


if __name__ == "__main__":
    desired = service(sys.argv[1])
    if len(sys.argv) == 3:
        actual = json.load(open(sys.argv[2]))
        print("true" if needs_release(actual, desired) else "false")
    else:
        # JSON is valid YAML; no additional rendering dependency is needed.
        print(json.dumps(desired, indent=2))
