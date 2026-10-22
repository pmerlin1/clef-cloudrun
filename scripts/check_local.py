"""Credential-free checks executed from the declared container environment."""

import os
import subprocess
import sys


def main():
    # Deployment jobs can select Flash and shared resources. Unit tests exercise
    # their own configurations and must not inherit the production target.
    env = {key: value for key, value in os.environ.items() if key not in {
        "MODEL", "SERVICE", "GPU_PROFILE", "RESOURCE_PREFIX", "WEIGHTS_BUCKET", "VPC_NETWORK", "VPC_SUBNET",
    }}
    env.update(PROJECT="example-clef-project", OTEL_SDK_DISABLED="true")
    for args in (
        ["-m", "unittest", "discover", "-s", "runtime", "-p", "test_*.py"],
        ["-m", "unittest", "discover", "-s", "benchmarks", "-p", "test_*.py"],
        ["scripts/check_adapter.py"],
        ["-m", "ruff", "check", "--no-cache", "runtime", "scripts", "benchmarks"],
        ["-m", "pip", "check"],
    ):
        subprocess.run([sys.executable, *args], check=True, env=env)


if __name__ == "__main__":
    main()
