"""Pinned Clef model variants and their Cloud Run GPU shapes."""
import json
import os
from pathlib import Path

GPU_PROFILES = {
    "rtx-pro-6000": {
        "accelerator": "nvidia-rtx-pro-6000",
        "cpu": "20",
        "memory": "80Gi",
        "gpu_zonal_redundancy_disabled": True,
    },
}


def model(name=None, gpu_profile=None):
    selected = (name or os.environ.get("MODEL", "clef")).strip()
    registry = json.loads(Path(__file__).with_name("models.json").read_text())
    if selected not in registry:
        choices = ", ".join(sorted(registry))
        raise ValueError(f"MODEL must be one of: {choices}")
    profile = gpu_profile if gpu_profile is not None else os.environ.get("GPU_PROFILE", "model-default")
    if profile not in {"model-default", *GPU_PROFILES}:
        raise ValueError("GPU_PROFILE must be one of: model-default, rtx-pro-6000")
    # The override changes only the deployment shape. Model identity, immutable
    # revision, and weight manifest always come from the selected registry row.
    return {"name": selected, **registry[selected], "gpu_profile": profile,
            **GPU_PROFILES.get(profile, {})}
