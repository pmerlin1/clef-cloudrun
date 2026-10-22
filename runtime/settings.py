"""Shared deployment names. Never default to someone's real cloud project."""
import os
import re

from models import model


def settings():
    project = os.environ.get("PROJECT", "")
    region = os.environ.get("REGION", "us-central1")
    selected_model = model()
    service = os.environ.get("SERVICE") or selected_model["name"]
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Set PROJECT to your Google Cloud project ID")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region):
        raise ValueError("Invalid REGION")
    # The deployer service-account ID appends '-deployer' (30-character limit).
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}[a-z0-9]", service):
        raise ValueError("SERVICE must be 2–21 lowercase letters, digits, or hyphens")
    # A single service starter can retain its original resource prefix. The
    # dedicated project shares these resources between clef and clef-flash.
    resource_prefix = os.environ.get("RESOURCE_PREFIX") or service
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}[a-z0-9]", resource_prefix):
        raise ValueError("Invalid RESOURCE_PREFIX")
    bucket = os.environ.get("WEIGHTS_BUCKET") or f"{project}-{resource_prefix}-weights"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise ValueError("Invalid WEIGHTS_BUCKET")
    network = os.environ.get("VPC_NETWORK") or resource_prefix
    subnet = os.environ.get("VPC_SUBNET") or resource_prefix
    if not all(re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]", value) for value in (network, subnet)):
        raise ValueError("Invalid VPC_NETWORK or VPC_SUBNET")
    return {
        "project": project, "region": region, "service": service,
        "bucket": bucket,
        "repository": f"{region}-docker.pkg.dev/{project}/{resource_prefix}/api",
        "runtime_account": f"{resource_prefix}-runtime@{project}.iam.gserviceaccount.com",
        "network": network, "subnet": subnet,
        "model_name": selected_model["name"],
        "model_revision": selected_model["revision"],
        "model_manifest": selected_model["manifest"],
        "hf_repo": selected_model["hf_repo"],
        "gpu_profile": selected_model["gpu_profile"],
        "accelerator": selected_model["accelerator"],
        "cpu": selected_model["cpu"],
        "memory": selected_model["memory"],
        "gpu_zonal_redundancy_disabled": selected_model["gpu_zonal_redundancy_disabled"],
    }
