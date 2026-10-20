"""Validate IAM and bounded scaling for a deployment or an explicitly paused service."""
import argparse
import json
from pathlib import Path


def validate(service, policy, *, paused=False):
    """Pure metadata validation; default requires request-driven automatic scaling."""
    annotations = service["metadata"].get("annotations", {})
    template = service["spec"]["template"]["metadata"].get("annotations", {})
    if annotations.get("run.googleapis.com/invoker-iam-disabled", "false") != "false":
        raise ValueError("IAM invocation checks must be enabled")
    mode = annotations.get("run.googleapis.com/scalingMode", "automatic")
    if paused:
        if mode != "manual" or annotations.get("run.googleapis.com/manualInstanceCount") is None:
            raise ValueError("--paused requires MANUAL scaling with an explicit zero target")
        if int(annotations["run.googleapis.com/manualInstanceCount"]) != 0:
            raise ValueError("--paused requires manual instance count zero")
    elif mode != "automatic":
        raise ValueError("Default validation requires automatic scaling; use --paused for MANUAL zero")
    for values, minimum, maximum in (
        (annotations, "run.googleapis.com/minScale", "run.googleapis.com/maxScale"),
        (template, "autoscaling.knative.dev/minScale", "autoscaling.knative.dev/maxScale"),
    ):
        if int(values.get(minimum, 0)) != 0 or int(values.get(maximum, -1)) != 1:
            raise ValueError("Both service and revision must retain min=0/max=1")
    if template.get("run.googleapis.com/gpu-zonal-redundancy-disabled") not in ("true", "false"):
        raise ValueError("GPU zonal redundancy setting must be explicit")
    for binding in policy.get("bindings", []):
        if {"allUsers", "allAuthenticatedUsers"}.intersection(binding.get("members", [])):
            raise ValueError("Public service-policy IAM binding found")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", type=Path)
    parser.add_argument("policy", type=Path)
    parser.add_argument("--paused", action="store_true", help="Require MANUAL zero rather than automatic scaling")
    args = parser.parse_args(argv)
    validate(json.loads(args.service.read_text()), json.loads(args.policy.read_text()), paused=args.paused)
    mode = "MANUAL target zero" if args.paused else "automatic scaling"
    print(f"IAM checks enabled; no public service-policy binding; service/revision min=0 max=1; {mode}")


if __name__ == "__main__":
    main()
