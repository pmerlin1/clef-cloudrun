"""Generate independent public examples with invented values; no source telemetry."""
import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHOICES = {
    "route": {
        "compute": "Allocated CPU, memory, or GPU time.",
        "plain_input": "Ordinary input-token usage.",
        "cache_input": "Cache-read or cache-write input-token usage.",
        "output": "Output-token usage.",
        "storage": "Persisted object bytes.",
    },
    "evidence": {"supported": "All explicitly required fields are supplied.", "missing": "At least one required field is absent or null."},
    "check": {"consistent": "Follows from the supplied facts and rule.", "contradicted": "Conflicts with the supplied facts or rule."},
}


def fingerprint(dataset):
    return hashlib.sha256(json.dumps(dataset, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build():
    cases = []

    def add(task, family, variant, request, packet, rule, answer):
        index = len(cases)
        evidence_id = ("E1", "E2", "E3")[index % 3]
        evidence = {key: {"note": "No additional measurements are supplied."} for key in ("E1", "E2", "E3")}
        evidence[evidence_id] = packet
        labels = list(CHOICES[task])
        labels = labels[index % len(labels):] + labels[:index % len(labels)]
        cases.append({
            "id": f"demo-{task}-{family:02d}-{variant}", "task": task,
            "family": f"demo-{task}-{family:02d}",
            "payload": {"model": "clef", "state": {"request": request, "evidence": evidence, "rule": rule},
                        "questions": {
                            "decision": {"type": "choice", "instructions": "Select the answer to the exact request.",
                                         "criteria": {label: CHOICES[task][label] for label in labels}},
                            "evidence": {"type": "choice", "instructions": "Select the evidence item containing the decisive facts or missing fields.",
                                         "criteria": {key: f"Evidence item {key}" for key in evidence}},
                        }},
            "gold": {"decision": answer, "evidence": evidence_id},
        })

    meters = [
        ({"platform": "Cloud Run", "meter": "allocated_cpu_time", "value": 14400, "unit": "s{CPU}"}, "compute"),
        ({"platform": "Vertex AI", "token_category": "input", "value": 123456}, "plain_input"),
        ({"platform": "Vertex AI", "token_category": "cache_read_input", "value": 98765}, "cache_input"),
        ({"platform": "Vertex AI", "token_category": "cache_write_input", "value": 54321}, "cache_input"),
        ({"platform": "Vertex AI", "token_category": "output", "value": 4321}, "output"),
        ({"platform": "GCS", "object_bytes": 3000000000}, "storage"),
    ]
    for family in range(1, 13):
        i = (family - 1) % len(meters)
        j = next((i + step) % len(meters) for step in range(1, len(meters)) if meters[(i + step) % len(meters)][1] != meters[i][1])
        for variant, (packet, answer) in zip(("a", "b"), (meters[i], meters[j])):
            add("route", family, variant, "Which usage category applies to the supplied meter?", packet,
                "Keep input, cache, output, allocated compute time, and persisted storage separate. Do not infer dollars.", answer)

    fields = [
        ({"invoice_rows": [{"gross_dollars": 10, "credits_dollars": -2}]}, ["invoice_rows"]),
        ({"token_count": 123456, "category": "input"}, ["token_count", "category"]),
        ({"cpu_seconds": 14400, "unit": "s{CPU}"}, ["cpu_seconds", "unit"]),
        ({"memory_gib_seconds": 57600, "unit": "GiBy.s"}, ["memory_gib_seconds", "unit"]),
        ({"request_count": 1800}, ["request_count"]),
        ({"charged_dollars": 8, "request_count": 1800}, ["charged_dollars", "request_count"]),
        ({"object_count": 12, "object_bytes": 3000000000}, ["object_count", "object_bytes"]),
        ({"accelerator": "nvidia-l4"}, ["accelerator"]),
        ({"token_counts": {"input": 123456}, "category_rates": {"input": 1}}, ["token_counts", "category_rates"]),
        ({"billable_seconds": 3600}, ["billable_seconds"]),
        ({"scaling": {"mode": "MANUAL", "target": 0}}, ["scaling"]),
        ({"invocation_count": 200}, ["invocation_count"]),
    ]
    for family, (available, required) in enumerate(fields, 1):
        for variant in ("a", "b"):
            present = dict(available)
            if variant == "b":
                present.pop(required[0])
            add("evidence", family, variant, "Can the specifically required observations be reported from this packet?",
                {"available_fields": present, "required_fields": required},
                "Every named required field must be present and non-null; never infer a missing value.",
                "supported" if variant == "a" else "missing")

    checks = [
        ({"minimum_vcpu": 8}, "A proposed 8-vCPU shape meets the supplied minimum.", "A proposed 4-vCPU shape meets the supplied minimum.", "Use the supplied minimum, not an inferred platform default."),
        ({"accelerator": "nvidia-l4"}, "The configured accelerator is nvidia-l4.", "The configured accelerator is nvidia-rtx-pro-6000.", "Read the configured accelerator."),
        ({"manual_target": 0, "persisted_objects": 12}, "Objects can remain present with target zero.", "Target zero means the persisted object count is zero.", "Pausing compute does not delete persisted storage."),
        ({"output_tokens": 0, "compute_meter": "allocated GPU time"}, "Zero output tokens alone does not establish zero compute cost.", "Zero output tokens proves zero compute cost.", "Compute is charged using allocated resources and duration."),
        ({"pytorch_allocated_bytes": 18000000000, "total_reserved_and_driver_bytes": None}, "This allocation counter alone cannot prove total device fit.", "This allocation counter alone proves total device fit.", "Allocated memory excludes some reserved, driver, and framework memory."),
        ({"input_tokens": 123456, "invoice_rows": None}, "Token counters alone do not reconstruct the invoice including credits.", "Token counters alone reconstruct the invoice including credits.", "Charged dollars require billing evidence."),
        ({"categories": ["input", "cache_read_input"], "rates": None}, "Equal category prices cannot be established from this packet.", "Both categories have equal prices.", "No category prices are supplied."),
        ({"metric": "allocated_cpu_time", "unit": "s{CPU}"}, "This unit measures CPU-seconds.", "This unit measures tokens.", "s{CPU} is CPU allocation time."),
        ({"object_bytes": 3000000000}, "The inventory contains 3 decimal GB.", "The inventory contains 3 decimal GiB.", "Decimal GB is 1,000,000,000 bytes; GiB is 1,073,741,824 bytes."),
        ({"request_count": None, "observed_input_tokens": 123456}, "A missing request count does not establish zero usage.", "A missing request count proves zero usage.", "Missing means unknown; supplied token evidence still indicates activity."),
        ({"mode": "MANUAL", "target": 0}, "The packet specifies a manual target of zero.", "The packet specifies automatic request-driven scaling.", "MANUAL and AUTOMATIC are distinct modes."),
        ({"current_object_bytes": 3000000000, "historical_byte_hours": None, "billed_dollars": None}, "Current bytes alone do not establish the exact historical invoice.", "Current bytes alone establish the exact historical invoice.", "Historical storage charges require more than a current inventory."),
    ]
    for family, (packet, good, bad, rule) in enumerate(checks, 1):
        for variant, statement, answer in (("a", good, "consistent"), ("b", bad, "contradicted")):
            add("check", family, variant, "Check this statement: " + statement, packet, rule, answer)
    return {"schema_version": 1, "kind": "independent_synthetic_examples",
            "notice": "All numeric values are invented. This fixture was NOT used for the published recorded model outcomes.",
            "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "synthetic-cases.json")
    args = parser.parse_args()
    dataset = build()
    args.output.write_text(json.dumps(dataset, indent=2) + "\n")
    print(json.dumps({"cases": len(dataset["cases"]), "canonical_dataset_sha256": fingerprint(dataset)}))


if __name__ == "__main__":
    main()
