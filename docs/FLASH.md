# Deploy Clef-Flash with Terraform and GitHub Actions

This is the complete 9B model path through the same infrastructure and runtime
used for Clef 27B. It provisions a private weights bucket, IAM identities and
GitHub OIDC, registry, networking and a CPU bootstrap service. The manual action
publishes pinned weights, builds the accelerated runtime and deploys the GPU
revision. No shared endpoint, service-account key or prebuilt image is required.

## Choose the hardware deliberately

| Terraform `gpu_profile` | GPU and resources | GPU zonal redundancy | Compute list-rate calculation |
|---|---|---|---:|
| `model-default` | L4, 8 vCPU, 32 GiB | enabled | about $1.80/hour |
| `rtx-pro-6000` | RTX PRO 6000, 20 vCPU, 80 GiB | disabled | about $3.19/hour |

Rates are for `us-central1`, checked 2026-10-20 against
[Cloud Run pricing](https://cloud.google.com/run/pricing), including allocated
CPU, memory and GPU time. Startup and idle time cost money; storage, networking
and other charges are separate. There is no automatic hardware fallback.

**Flash executed successfully on RTX PRO 6000. L4 remains unverified.** The
recorded benchmarks used RTX because L4 quota was denied in the test project.
They do not prove L4 memory fit, kernel execution, image support or concurrency.
For the hardware used in those tests, select `rtx-pro-6000` explicitly and accept
its higher rate. The default retains the intended smaller L4 configuration;
validate it with one controlled request after obtaining quota.

Confirm one Cloud Run GPU of the selected type, region and redundancy mode in
[Google's GPU quota guidance](https://docs.cloud.google.com/run/docs/configuring/services/gpu#request_a_quota_increase).
A quota for Compute Engine GPUs or the other redundancy mode is insufficient.
Do not repeatedly dispatch deployments while quota is denied.

## Bootstrap your own service

Start with the [repository prerequisites](../README.md#1-prepare-a-repository-and-project):
publish the starter at the root of your own repository on `main`, authenticate
Google Cloud and GitHub, and obtain your numeric repository/owner IDs.
Terraform and Python commands below run from that repository root in WSL,
Linux or macOS.

```bash
cp infra/terraform.flash.tfvars.example infra/terraform.tfvars
```

Edit the copied file with your project, repository IDs and explicit invoker.
Keep `model_variant = "clef-flash"` and `service_name = "clef-flash"`, and choose
the GPU profile above. Never commit the populated file or state.

```bash
terraform -chdir=infra init
terraform -chdir=infra plan -out=bootstrap.tfplan
# Review the plan, then provision the private infrastructure:
terraform -chdir=infra apply bootstrap.tfplan
python3 scripts/configure_github.py OWNER/REPOSITORY
```

Terraform creates a **CPU-only bootstrap**, with service and revision minimum
zero, maximum one and IAM invocation checks enabled. It also creates the GCS
bucket with public access prevention, a read-only runtime identity and a
separate restricted deployer. The helper transfers seven non-secret outputs,
including `CLEF_MODEL` and `CLEF_GPU_PROFILE`, to repository variables.
Review inherited project/organization IAM too: the service policy alone does
not establish effective invocation access in a shared project.

For an additional service alongside an existing 27B service, use a separate
checkout/state and deployment repository configuration. Do not overwrite an
existing service's state or change its `service_name` to create a second one.
The helper sets repository-wide deployment variables; two service states
pointing to one repository would overwrite each other's defaults.

## Publish weights and deploy

```bash
gh workflow run deploy.yaml --ref main --repo OWNER/REPOSITORY
gh run list --workflow deploy.yaml --repo OWNER/REPOSITORY --limit 1
```

The action reads the Terraform-generated model and profile variables. Its
optional `model` and `gpu_profile` inputs override those values for one run;
keeping Terraform and repository defaults aligned is the repeatable path.

The action downloads the files listed in
[the Flash manifest](../runtime/manifest-flash.json) from the pinned
`Cloudflare/clef-flash` Hugging Face revision, checks sizes and SHA-256, and
uploads them to your private regional GCS bucket. It streams one file at a
time, needing room for the largest shard rather than all approximately 19 GB.
Matching objects are reused; mismatches fail rather than silently replacing
weights. No Cloud Shell weight-transfer step is needed.

The two-stage build compiles causal-conv1d for both `sm_89` and `sm_120`, then
pushes the runtime image and deploys its immutable digest through the Cloud Run
GitHub action. Rendering applies the selected model, pinned revision, GPU
profile, startup probe, IAM enforcement and min=0/max=1. Terraform ignores
subsequent runtime-template changes so it does not revert the GPU release to
the CPU bootstrap. Neither CI nor the deployment workflow invokes inference,
but creating a changed GPU revision still incurs startup charges.

## Verify, test once, and stop

```bash
export PROJECT=your-gcp-project
export REGION=us-central1
export SERVICE=clef-flash
export MODEL=clef-flash
export GPU_PROFILE=model-default  # Use rtx-pro-6000 if that is what you deployed.

python3 scripts/verify.py
# One authenticated, billable request, with no retries:
python3 scripts/smoke_test.py --allow-paid-request
# Request shutdown after your planned batch:
python3 scripts/set_scaling.py 0
python3 scripts/verify.py --paused
```

The caller needs `roles/run.invoker` or equivalent effective permissions.
`verify.py` reads configuration and Monitoring without calling the application;
instance samples are delayed. Keep IAM enabled and check active/idle metrics
until shutdown completes. Manual zero disables request-triggered starts; only
restore automatic scaling when you intend to accept requests:

```bash
python3 scripts/set_scaling.py auto
```

The endpoint is the Terraform `service_url` output followed by `/v1/systemone`.
Requests use `"model": "clef-flash"` and the same bounded schema as 27B. Flash's
recorded validation covers text; its image execution has not been proven.
See the [benchmark package](../benchmarks/README.md) for optional controlled
text batches and [validation notes](VALIDATION.md) for the measured limits.

## Files that make this work

| Component | Source |
|---|---|
| Infrastructure, IAM, GCS and bootstrap | [infra/main.tf](../infra/main.tf), [infra/github.tf](../infra/github.tf) |
| Model/profile inputs and deployment variables | [infra/variables.tf](../infra/variables.tf), [infra/outputs.tf](../infra/outputs.tf) |
| Pinned model and hardware selection | [runtime/models.json](../runtime/models.json), [runtime/models.py](../runtime/models.py) |
| Verified weight publisher | [runtime/upload_weights.py](../runtime/upload_weights.py) |
| Dual-architecture CUDA build and runtime | [runtime/build_kernel.py](../runtime/build_kernel.py), [runtime/Dockerfile](../runtime/Dockerfile) |
| GPU release rendering and manual action | [runtime/render_service.py](../runtime/render_service.py), [deploy.yaml](../.github/workflows/deploy.yaml) |

The extracted starter has CPU/unit/Terraform checks. Fresh-project provisioning
and deployment have not been exercised together end to end; this guide does
not claim otherwise.
