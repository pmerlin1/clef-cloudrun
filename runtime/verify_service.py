"""Fail deployment if authentication or zero-minimum scaling is weakened."""
import json
import sys

service = json.load(open(sys.argv[1]))
policy = json.load(open(sys.argv[2]))
annotations = service["metadata"].get("annotations", {})
template = service["spec"]["template"]["metadata"].get("annotations", {})
assert annotations.get("run.googleapis.com/invoker-iam-disabled", "false") == "false"
assert annotations.get("run.googleapis.com/scalingMode", "automatic") == "automatic"
assert int(annotations.get("run.googleapis.com/minScale", 0)) == 0
assert int(template.get("autoscaling.knative.dev/minScale", 0)) == 0
assert int(annotations["run.googleapis.com/maxScale"]) == 1
assert int(template["autoscaling.knative.dev/maxScale"]) == 1
assert template["run.googleapis.com/gpu-zonal-redundancy-disabled"] in ("true", "false")
for binding in policy.get("bindings", []):
    assert not {"allUsers", "allAuthenticatedUsers"}.intersection(binding.get("members", []))
print("IAM enforced; service/revision min=0 max=1")
