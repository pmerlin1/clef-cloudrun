"""Publish non-secret Terraform outputs as GitHub Actions repository variables."""
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
if len(sys.argv) != 2 or len(sys.argv[1].split("/")) != 2:
    raise SystemExit("Usage: python3 scripts/configure_github.py OWNER/REPOSITORY")
variables = json.loads(subprocess.check_output(
    ["terraform", f"-chdir={root / 'infra'}", "output", "-json", "github_variables"], text=True))
expected = {"GCP_PROJECT_ID", "GCP_REGION", "CLEF_SERVICE", "CLEF_MODEL", "CLEF_GPU_PROFILE", "WIF_PROVIDER", "DEPLOY_SERVICE_ACCOUNT"}
if set(variables) != expected or not all(isinstance(v, str) and v for v in variables.values()):
    raise SystemExit("Unexpected Terraform outputs; bootstrap infra first")
for name, value in variables.items():
    subprocess.run(["gh", "variable", "set", name, "--repo", sys.argv[1], "--body", value], check=True)
print("Configured seven repository variables; no service-account key is required.")
