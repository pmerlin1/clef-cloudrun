"""Score published observations or a future synthetic-fixture run; no network calls."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path


HERE = Path(__file__).resolve().parent
CHOICES = {
    "route": {"compute", "plain_input", "cache_input", "output", "storage"},
    "evidence": {"supported", "missing"},
    "check": {"consistent", "contradicted"},
}
EVIDENCE = {"E1", "E2", "E3"}


def validate_answer(answer, task):
    if set(answer) != {"decision", "evidence"}:
        raise ValueError("Answers must contain exactly decision and evidence")
    if answer["decision"] not in CHOICES[task] or answer["evidence"] not in EVIDENCE:
        raise ValueError("Answer is outside the declared choice sets")


def summarize(rows):
    """Rows already contain validated gold and selected choices."""
    by_task = {}
    for task in sorted(CHOICES):
        selected = [row for row in rows if row["task"] == task]
        by_task[task] = {
            "cases": len(selected),
            "decision_correct": sum(r["actual"]["decision"] == r["gold"]["decision"] for r in selected),
            "evidence_correct": sum(r["actual"]["evidence"] == r["gold"]["evidence"] for r in selected),
            "both_correct": sum(r["actual"] == r["gold"] for r in selected),
        }
    families = defaultdict(list)
    for row in rows:
        families[(row["task"], row["family"])].append(row)
    if any(len(pair) != 2 for pair in families.values()):
        raise ValueError("Each family must have two cases")
    return {
        "cases": len(rows),
        "decision_correct": sum(t["decision_correct"] for t in by_task.values()),
        "evidence_correct": sum(t["evidence_correct"] for t in by_task.values()),
        "both_correct": sum(t["both_correct"] for t in by_task.values()),
        "families": len(families),
        "both_decisions_correct_in_pair": sum(all(r["actual"]["decision"] == r["gold"]["decision"] for r in pair) for pair in families.values()),
        "both_answers_correct_in_pair": sum(all(r["actual"] == r["gold"] for r in pair) for pair in families.values()),
        "tasks": by_task,
    }


def validate_cases(cases):
    if len(cases) != 72 or len({r["id"] for r in cases}) != 72:
        raise ValueError("Expected exactly 72 unique cases")
    if Counter(r["task"] for r in cases) != {"route": 24, "evidence": 24, "check": 24}:
        raise ValueError("Expected 24 cases per task")
    for row in cases:
        validate_answer(row["gold"], row["task"])


def score_recorded(document):
    if document.get("kind") != "private_prompt_outcome_ledger":
        raise ValueError("This scorer requires the published observation ledger")
    rows = document["cases"]
    validate_cases(rows)
    result = {}
    for model in ("clef-flash", "clef"):
        selected = []
        for row in rows:
            if set(row["predictions"]) != {"clef", "clef-flash"}:
                raise ValueError("Every case must contain both model observations")
            actual = row["predictions"][model]
            validate_answer(actual, row["task"])
            selected.append({**row, "actual": actual})
        result[model] = summarize(selected)
    return result


def score_new_run(dataset, run):
    if dataset.get("kind") != "independent_synthetic_examples":
        raise ValueError("Expected an independent synthetic fixture")
    cases = dataset["cases"]
    validate_cases(cases)
    if run.get("dataset_sha256") != hashlib.sha256(
        json.dumps(dataset, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest():
        raise ValueError("Prediction file does not match the canonical fixture fingerprint")
    predictions = run["predictions"]
    ids = [r["id"] for r in predictions]
    if len(ids) != len(set(ids)) or set(ids) != {r["id"] for r in cases}:
        raise ValueError("Missing, duplicate, or unknown predictions; refusing a complete-run score")
    indexed = {r["id"]: r["answers"] for r in predictions}
    rows = []
    for case in cases:
        actual = indexed[case["id"]]
        validate_answer(actual, case["task"])
        rows.append({**case, "actual": actual})
    return summarize(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recorded", type=Path, default=HERE / "recorded-results.json")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--predictions", type=Path)
    args = parser.parse_args()
    if bool(args.dataset) != bool(args.predictions):
        parser.error("--dataset and --predictions must be supplied together")
    if args.dataset:
        score = score_new_run(json.loads(args.dataset.read_text()), json.loads(args.predictions.read_text()))
    else:
        score = score_recorded(json.loads(args.recorded.read_text()))
    print(json.dumps(score, indent=2))


if __name__ == "__main__":
    main()
