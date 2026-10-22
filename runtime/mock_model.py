"""CPU-only local fixture implementing the pinned model's actual call signatures.

Selected only by CLEF_MOCK_MODEL. It proves the HTTP/queue/telemetry plumbing;
real upstream encoding, collation and response conversion have a separate check.
"""

import json
import os
import time
from types import SimpleNamespace


def encode_record(tokenizer, record, max_length=16384, processor=None):
    questions = []
    for name, question in record["questions"].items():
        if question["type"] == "noul":
            options = ("true", "false")
        elif question["type"] == "choice":
            options = tuple(sorted(question["criteria"]))
        else:
            options = tuple(str(i) for i in range(len(question["criteria"])))
        questions.append(SimpleNamespace(question_id=name, option_ids=options))
    length = 64 + len(json.dumps(record["state"]).split()) + 256 * len(record.get("images", []))
    return SimpleNamespace(input_ids=[0] * length, questions=questions, payload=record)


def collate_records(records, pad_token_id, device):
    return {"records": records}


class MockLogits:
    def __init__(self, count):
        self.count = count

    def float(self):
        return self

    def softmax(self, axis):
        return self

    def tolist(self):
        return [1 / self.count] * self.count


class MockModel:
    def parameters(self):
        return iter([SimpleNamespace(device="cpu")])

    def __call__(self, batch):
        time.sleep(float(os.environ.get("CLEF_MOCK_BATCH_SECONDS", "0.05")))
        for record in batch["records"]:
            state = record.payload["state"]
            if isinstance(state, dict) and state.get("mock_failure"):
                raise RuntimeError(f"Synthetic backend failure: {state}")
        return [[MockLogits(len(q.option_ids)) for q in record.questions] for record in batch["records"]]


def systemone_answer(question, probabilities):
    if question["type"] == "noul":
        return {"type": "noul", "noul": probabilities["true"]}
    if question["type"] == "choice":
        choice = max(probabilities, key=probabilities.__getitem__)
        return {"type": "choice", "choice": choice, "confidence": probabilities[choice], "probabilities": probabilities}
    return {"type": "score", "score": sum(int(k) * v for k, v in probabilities.items()),
            "confidence": max(probabilities.values()), "probabilities": probabilities,
            "legend": dict(enumerate(question["criteria"]))}
