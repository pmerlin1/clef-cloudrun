"""Exercise checksum-pinned upstream producers on CPU, without model weights.

Downloads only joint_schema_model.py and its license. The real encode_record,
collate_records and systemone_answer run against small synthetic tensors, then
the batching adapter is compared to the upstream single-request entry point.
"""

import hashlib
import importlib.util
import json
import sys
import tempfile
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
import app


class Processor:
    def __init__(self):
        self.tokenizer = self
        self.pad_token_id = 0

    def __call__(self, text, **kwargs):
        if isinstance(text, str):
            return SimpleNamespace(input_ids=list(range(1, len(text.split()) + 1)))
        return {"input_ids": torch.tensor([[91, 92]]), "pixel_values": torch.ones((1, 4)),
                "image_grid_thw": torch.tensor([[1, 1, 1]]), "mm_token_type_ids": torch.tensor([[1, 1]])}


class Model(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.anchor = torch.nn.Parameter(torch.zeros(1))
        self.calls = 0

    def forward(self, batch):
        if torch.is_grad_enabled():
            raise AssertionError("Inference must disable gradients in the executing thread")
        self.calls += 1
        return [[torch.arange(len(question.option_ids), dtype=torch.float32)
                 for question in record.questions] for record in batch["records"]]


def fetch(selected, directory):
    entries = json.loads((ROOT / "runtime" / selected["manifest"]).read_text())
    for name in ("joint_schema_model.py", "LICENSE"):
        entry = next(item for item in entries if item["name"] == name)
        url = f"https://huggingface.co/{selected['hf_repo']}/resolve/{selected['revision']}/{name}"
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(entry["size"] + 1)
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise RuntimeError("Pinned upstream checksum mismatch")
        (directory / name).write_bytes(data)
    spec = importlib.util.spec_from_file_location("joint_schema_model", directory / "joint_schema_model.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main():
    registry = json.loads((ROOT / "runtime/models.json").read_text())
    for name, selected in registry.items():
        with tempfile.TemporaryDirectory() as directory:
            upstream = fetch(selected, Path(directory))
            processor, model = Processor(), Model()
            requests = [
                {"model": name, "state": "small synthetic work order", "questions": {
                    "approved": {"type": "noul"},
                    "category": {"type": "choice", "criteria": {"a": "labor", "b": "material"}},
                    "score": {"type": "score", "criteria": ["low", "high"]},
                }},
                {"model": name, "state": "a longer synthetic work order description", "images": ["synthetic"],
                 "questions": {"approved": {"type": "noul"}}},
            ]
            expected = [upstream.systemone(model, processor, request, max_length=2048) for request in requests]
            records = [upstream.encode_record(processor.tokenizer, request, max_length=1_000_000, processor=processor)
                       for request in requests]
            items = [SimpleNamespace(record=record, payload=request) for record, request in zip(records, requests, strict=True)]
            before = model.calls
            with patch.multiple(app, model=model, processor=processor, collate_records_fn=upstream.collate_records,
                                systemone_answer_fn=upstream.systemone_answer, inference_context=torch.inference_mode):
                actual = app.run_gpu_batch_sync(items)
            if actual != expected or model.calls != before + 1:
                raise AssertionError("Batched response differs from the pinned upstream entry point")
            print(f"{name}: verified source hashes, real encode/collate/answer, mixed text/image batch, one forward, upstream parity")


if __name__ == "__main__":
    main()
