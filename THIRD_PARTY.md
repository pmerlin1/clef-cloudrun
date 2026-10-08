# Attribution and upstream components

This repository contains deployment and adapter code. Model weights, the upstream
model implementation, CUDA wheels and dependencies are downloaded from their
original distributors; they are not included in the source archive.

- [Cloudflare Clef](https://huggingface.co/Cloudflare/clef): model and decision API.
  Pinned release `2f3de3dd85f379784083b0814d997ab627200f0c`. The release's LICENSE
  is included in `runtime/manifest.json` and downloaded/verified with the model.
  The model card identifies its license as Apache-2.0. Preserve its license and
  notices if redistributing the model or upstream implementation.
- [flash-linear-attention](https://github.com/fla-org/flash-linear-attention):
  gated-delta kernels and fused modules.
- [causal-conv1d](https://github.com/Dao-AILab/causal-conv1d): CUDA convolution
  extension. The build changes only the architecture target list to `sm_120`.
- [PyTorch](https://github.com/pytorch/pytorch),
  [Transformers](https://github.com/huggingface/transformers), and the other packages
  listed in `runtime/requirements.txt` retain their respective licenses.
- [NVIDIA CUDA terms](https://docs.nvidia.com/cuda/eula/index.html) apply to NVIDIA
  components. The starter's code license does not replace those terms.

See each upstream distribution for its full license and attribution requirements,
particularly before redistributing built images. This project is not affiliated
with or endorsed by Cloudflare, Google, Hugging Face, or NVIDIA.
