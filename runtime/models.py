"""Pinned Clef model variants and their Cloud Run GPU shapes."""
import json
import os
from pathlib import Path


def model(name=None):
    selected = (name or os.environ.get("MODEL", "clef")).strip()
    registry = json.loads(Path(__file__).with_name("models.json").read_text())
    if selected not in registry:
        choices = ", ".join(sorted(registry))
        raise ValueError(f"MODEL must be one of: {choices}")
    return {"name": selected, **registry[selected]}
