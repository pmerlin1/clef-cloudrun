# Validation and measured results

Reviewed 2026-10-07. There are two distinct evidence sets:

1. **The original private deployment was exercised on a real GPU.** Baseline
   measurements and a later image-capable release were tested on 2026-10-07.
2. **This extracted starter was checked without deploying to a second project.** All 22
   unit tests, CPU adapter checks (with a fake model and inline PNG), Terraform
   validation and repository hooks pass. Fresh-project provisioning,
   the new restricted GitHub identity and the extracted runtime have not been
   exercised together end to end. That remains the first adopter's deployment check.

Do not infer production readiness, a latency SLO, model accuracy on natural
images, or maximum-input capacity from the small tests below.

The review checked infrastructure code, request handling, private-identifier and
secret exposure, and live IAM/scaling. It did not include a full scan of the built
container image. Dependency review noted an Accelerate 1.12.0 checkpoint-loading
path traversal advisory with no fixed release and a low-severity PyTorch 2.11.0
JIT advisory. The HTTP API does not accept checkpoints or user JIT code; model
files are pinned and SHA-256 verified. These package findings remain documented
limits, not a claim that the image is free of vulnerabilities.

## Earlier baseline review

- The then-live service configuration matched its recorded immutable runtime release.
- IAM checks were enabled, with automatic scaling and service/revision minimum 0
  and maximum 1.
- Model bucket has uniform access and public access prevention enabled.
- The recorded immutable container remains present in Artifact Registry.
- The original local unit tests pass; recorded CI and the repeated-deployment
  workflow both succeeded. The latter skipped the Cloud Run deployment action.
- An anonymous request returned 403. Earlier instance metrics verified zero
  active and zero idle instances after that test window. A later sparse metrics
  query alone did not establish the current instance count.

## Varied live batch on the image-capable private release

The updated private release passed one bounded authenticated GPU batch. It used
simple generated color squares, a structured text state, and negative input cases.
Its restricted GitHub deployment workflow succeeded, and live service checks
confirmed IAM enforcement, no public invoker, and min=0/max=1 scaling. An
anonymous request returned 403.
The first text request took 30.111 seconds including initial kernel compilation;
subsequent text requests took 0.190–0.201 seconds. The image requests below were
made after the model was ready and do not include cold startup. The image result
times range from 1.353 to 2.917 seconds in this small batch.

| Request | HTTP | Observed answer | Elapsed seconds |
| --- | ---: | --- | ---: |
| Technical outage text | 200 | technical | 30.111 |
| Billing text | 200 | billing | 0.201 |
| Structured state with score question | 200 | typed score returned | 0.190 |
| Red PNG | 200 | red, probability 0.9975 | 2.917 |
| Blue PNG | 200 | blue, probability 0.9971 | 1.357 |
| Red JPEG | 200 | red, probability 0.9975 | 1.384 |
| Red and blue PNGs together | 200 | colors differ, probability 0.9981 | 2.625 |
| Red PNG with a misleading blue caption | 200 | red, probability 0.9974 | 1.353 |
| External image URL | 400 | rejected | 0.098 |
| Malformed image | 400 | rejected | 0.102 |
| Encoded input over 2,048 tokens | 400 | rejected | 0.217 |

All batch assertions passed. Changing the image changed the color answer, and
the red image outweighed the contradictory caption in this test. These synthetic
examples establish that the vision path executes and that the wrapper enforces
its tested bounds; they do not establish performance or accuracy on general
photos, documents, adversarial prompts, or sustained traffic.

## Separate Jev comparison (user-supplied)

The operator reported a five-profile comparison through an SF childcare MCP
using the same family criteria for TypeSafe Jev (`jev-1.13.0`) and this Clef API.
All five profile rankings and categorical verdicts agreed. Reported warm
per-profile latency was 83–190 ms for Jev and 275–378 ms for Clef. This comparison
was supplied separately; it was not reproduced in this repository, and the five
examples cannot establish general model equivalence or decision accuracy.

## Earlier baseline measurements from the original deployment

Configuration: one RTX PRO 6000 (96 GB VRAM), 20 vCPU, 80 GiB RAM, `us-central1`,
55 GB BF16 model, eight HTTP slots and two model-execution slots. The short
synthetic requests used 216–220 tokens. Successful requests logged 48 actual
causal-conv1d calls and 48 FLA chunk calls. There were no generated chat tokens.

| Measurement | Observed result |
| --- | ---: |
| One authenticated request starting from zero | 275.987 seconds |
| Full model transfer in that cold start | 218.342 seconds |
| SHA-256 verification | 8.712 seconds |
| Representative warm requests | 0.211–0.243 seconds |
| One burst of eight simultaneous requests | 0.777 seconds total |
| First inference kernel compilation, earlier run | about 26.8 seconds |
| Compressed container before / after | 4.32 / 3.65 GB |

The first request includes startup and kernel compilation. The warm measurements
do not include them. The input used in an earlier fallback run differed, so
these results do not support a matched-input kernel speedup ratio. Output sanity
checks covered technical/outage and billing/no-outage examples, not model accuracy.

Download experiments used the same four largest shards, totaling 19,882,969,096
bytes, with files deleted between rounds. One pass each, in order 16, 8, 20, 12:

| Total range workers | Transfer seconds | SHA-256 seconds | Total seconds |
| --- | ---: | ---: | ---: |
| 8 | 78.138 | 2.879 | 81.017 |
| 12 | 85.616 | 2.967 | 88.583 |
| 16 | 79.049 | 2.921 | 81.970 |
| 20 | 73.760 | 2.893 | 76.653 |

Twenty workers won this experiment and are the starter's default, not a universal
optimum. A single pass cannot establish statistical significance. Transfer time
includes the SDK's integrity checks; the separately reported checksum is SHA-256.
Sanitized detailed responses and measurements are in [measurements.json](measurements.json).

## Cost interpretation

Using the [published rates](https://cloud.google.com/run/pricing) checked on
2026-10-07, for this region and disabled GPU zonal redundancy:

`20 × $0.000018 + 80 × $0.000002 + $0.00036522 = $0.00088522/second`

That is approximately $0.0531/minute or $3.1868/hour per running instance. A
276-second cold request alone is about $0.24 of compute, followed by any paid
idle tail. One observed 8m23s idle tail was about $0.45. The original optimization
pass, including a failed cuDNN candidate, was estimated at $2.29 of Cloud Run
compute from observed instance lifetimes, not from a final billing export.
Storage, image registry, CI, networking, taxes, rounding and discounts are separate.

Batching nearby requests amortizes startup. Keeping an instance warm costs about
$0.053 for every extra minute; whether that is worthwhile depends on request gaps
and tolerance for cold latency. Cloud Run chooses automatic scale-down timing;
manual zero is useful after planned test batches but disables incoming service.

## Extraction changes

- Replaced personal project/account/repository values with validated settings.
- Split Terraform bootstrap from GitHub deployment, so storage and IAM exist first.
- Restricted GitHub WIF and deployment permissions; kept IAM and min=0/max=1 checks.
- Removed the paid benchmark HTTP route from the basic inference service.
- Enforced 64 KiB request buffering while reading chunked bodies.
- Added a bounded, inline PNG/JPEG image adapter and local CPU tests; no
  caller-provided image URL or file path is fetched by the service.
- Added a full encoded-context check that rejects requests over 2,048 tokens
  rather than letting the upstream encoder truncate state silently.
- Used the measured 20-worker download default directly, without a tuning object.
- Kept the tested dependency pins and accelerated kernel implementation.
- Added explicit operator test/stop scripts, including an optional image request.

This recipe uses no shared public inference endpoint or prebuilt image. Each user
builds into their own project and grants their own callers access.
