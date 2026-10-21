"""Bake a public-safe inventory using the final image's Python interpreter."""

import json
import os
import platform
import re
from importlib import metadata
from pathlib import Path


def build_manifest(build_id, source_revision):
    for label, value in (("build_id", build_id), ("source_revision", source_revision)):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+:-]*", value):
            raise ValueError(f"Invalid {label}")
    if build_id == source_revision:
        raise ValueError("build_id must identify a build, not its source revision")

    packages = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        version = distribution.version
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name):
            raise ValueError("Invalid installed package name")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version):
            raise ValueError(f"Invalid installed package version: {name}")
        name = re.sub(r"[-_.]+", "-", name).lower()
        if name in packages:
            raise ValueError(f"Duplicate installed package: {name}")
        packages[name] = version

    return {
        "schema_version": 1,
        "build_id": build_id,
        "source_revision": source_revision,
        "python_version": platform.python_version(),
        "packages": dict(sorted(packages.items())),
    }


if __name__ == "__main__":
    manifest = build_manifest(
        os.environ.get("BUILD_ID", "development"),
        os.environ.get("SOURCE_REVISION", "unknown"),
    )
    path = Path("/app/build-manifest.json")
    # Exclusive creation prevents accidental regeneration of an image's identity.
    with path.open("x", encoding="utf-8") as output:
        json.dump(manifest, output, indent=2, sort_keys=True)
        output.write("\n")
    path.chmod(0o444)
    # Self-cleanup so build script does not remain in final hardened image
    try:
        Path(__file__).unlink()
    except OSError:
        pass
