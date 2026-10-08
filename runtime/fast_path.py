"""Require accelerated Qwen kernels and audit their actual inference use."""
from collections import Counter
from functools import wraps
import json
import threading


def check_installation():
    import torch
    # Dynamo queries this even with cuDNN dispatch disabled. Exercise the native
    # version symbol in the CPU build gate, retaining the cuDNN graph/ABI shim.
    if not torch.backends.cudnn.version():
        raise RuntimeError("cuDNN version-query ABI is unavailable")
    # The bounded text endpoint uses native SDPA, FLA and causal-conv1d.
    # Disable cuDNN before any model work: its unused engine plugins are omitted.
    torch.backends.cudnn.enabled = False
    torch.backends.cuda.enable_cudnn_sdp(False)
    from causal_conv1d import causal_conv1d_fn, causal_conv1d_update
    from fla.modules import FusedRMSNormGated
    from fla.ops.gated_delta_rule import chunk_gated_delta_rule, fused_recurrent_gated_delta_rule

    if not torch.__version__.startswith("2.11.") or torch.version.cuda != "12.8":
        raise RuntimeError("Fast-path extension requires the pinned PyTorch 2.11 / CUDA 12.8 runtime")
    assert all((causal_conv1d_fn, causal_conv1d_update, FusedRMSNormGated,
                chunk_gated_delta_rule, fused_recurrent_gated_delta_rule))


def require_fast_path():
    import torch
    from transformers.models.qwen3_5 import modeling_qwen3_5 as qwen

    check_installation()
    if not torch.cuda.is_available():
        raise RuntimeError("Clef accelerated inference requires an available CUDA GPU")
    if not qwen.is_fast_path_available or qwen.FusedRMSNormGated is None:
        raise RuntimeError("Clef requires causal-conv1d and FLA; refusing the PyTorch fallback")
    return qwen


class FastPathAudit:
    def __init__(self, model):
        qwen = require_fast_path()
        self.context = threading.local()
        self.layers = 0
        bindings = {
            "causal_conv1d_fn": qwen.causal_conv1d_fn,
            "causal_conv1d_update": qwen.causal_conv1d_update,
            "chunk_gated_delta_rule": qwen.chunk_gated_delta_rule,
            "recurrent_gated_delta_rule": qwen.fused_recurrent_gated_delta_rule,
        }
        for layer in model.modules():
            if not isinstance(layer, qwen.Qwen3_5GatedDeltaNet):
                continue
            self.layers += 1
            for name, kernel in bindings.items():
                if getattr(layer, name) is not kernel:
                    raise RuntimeError(f"Unexpected fallback binding: {name}")
                setattr(layer, name, self._counted(name, kernel))
        if not self.layers:
            raise RuntimeError("No Qwen gated-delta layers found to verify")
        print(f"Clef fast path enabled: causal-conv1d + FLA; layers={self.layers}", flush=True)

    def _counted(self, name, kernel):
        @wraps(kernel)
        def call(*args, **kwargs):
            result = kernel(*args, **kwargs)
            if hasattr(self.context, "calls"):
                self.context.calls[name] += 1
            return result
        return call

    def begin_inference(self):
        self.context.calls = Counter()

    def verify_inference(self):
        used = self.context.calls
        if not used["causal_conv1d_fn"] or not used["chunk_gated_delta_rule"]:
            raise RuntimeError("Inference did not execute the required CUDA/FLA prefill kernels")
        print("Clef fast-path inference: " + json.dumps(dict(used), sort_keys=True), flush=True)
