# LinkedIn draft

I built a repeatable recipe for running Cloudflare’s Clef decision model on a
Google Cloud Run GPU, behind IAM. Terraform creates the private infrastructure;
GitHub Actions builds and deploys an immutable image. The model weights live in
private GCS, and the service is set to min=0 / max=1 instances.

I wanted to check more than whether the API returned HTTP 200. On the private
deployment, Clef made the expected choices for billing and outage text, red and
blue PNGs, a red JPEG, and a two-image color comparison. It chose red for a red
image even when the accompanying caption said “blue.” External image URLs,
malformed images, and inputs beyond the 2,048-token context were rejected.

These are small synthetic checks, not a claim about accuracy on real-world
images. In an earlier baseline, the cold request took about 4m36s, largely
because the instance had to download about 55 GB of weights. Warm text requests
were around 0.2s. The image requests in the later batch took about 1.4–2.9s
after the model was ready.

In a separate five-profile comparison through our SF childcare MCP, Jev and
self-hosted Clef returned the same ranking and categorical verdicts for all five
profiles. Reported warm per-profile latency was 83–190 ms for Jev and 275–378 ms
for Clef. That small application test is a useful check, not evidence of broad
model equivalence or real-world decision accuracy.

At the tested configuration, running compute is about $0.053/minute, including
startup and idle time. Scale-to-zero helps, but shutdown is not immediate.

The starter includes Terraform, the deployment workflow, a one-request smoke
test, security and cost notes, and the measured evidence. It passes local checks;
a fresh-project deployment of the extracted starter remains to be tested.

Code and measurements: https://github.com/pmerlin1/clef-cloudrun

---

Publish this post only after the new repository exists and its README and
validation notes are visible at the URL above.
