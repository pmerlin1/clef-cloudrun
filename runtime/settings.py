"""Shared deployment names. Never default to someone's real cloud project."""
import os
import re

from models import model


def settings():
    project = os.environ.get("PROJECT", "")
    region = os.environ.get("REGION", "us-central1")
    service = os.environ.get("SERVICE", "clef")
    selected_model = model()
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Set PROJECT to your Google Cloud project ID")
    if not re.fullmatch(r"[a-z]+-[a-z]+[0-9]+", region):
        raise ValueError("Invalid REGION")
    # The deployer service-account ID appends '-deployer' (30-character limit).
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}[a-z0-9]", service):
        raise ValueError("SERVICE must be 2–21 lowercase letters, digits, or hyphens")
    return {
        "project": project, "region": region, "service": service,
        "bucket": f"{project}-{service}-weights",
        "repository": f"{region}-docker.pkg.dev/{project}/{service}/api",
        "runtime_account": f"{service}-runtime@{project}.iam.gserviceaccount.com",
        "model_name": selected_model["name"],
        "model_revision": selected_model["revision"],
        "model_manifest": selected_model["manifest"],
        "hf_repo": selected_model["hf_repo"],
        "accelerator": selected_model["accelerator"],
        "cpu": selected_model["cpu"],
        "memory": selected_model["memory"],
        "gpu_zonal_redundancy_disabled": selected_model["gpu_zonal_redundancy_disabled"],
    }
