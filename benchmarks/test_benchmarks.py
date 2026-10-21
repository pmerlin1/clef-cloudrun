"""Offline checks for fixture provenance, scoring, and incomplete-run rejection."""
import json
import unittest
from copy import deepcopy
from pathlib import Path

from make_synthetic import build, fingerprint
from score import score_new_run, score_recorded, validate_cases

HERE = Path(__file__).resolve().parent


class BenchmarkTests(unittest.TestCase):
    def test_generated_fixture_is_reproducible_and_gold_is_in_declared_choices(self):
        dataset = build()
        self.assertEqual(dataset, json.loads((HERE / "synthetic-cases.json").read_text()))
        validate_cases(dataset["cases"])
        for case in dataset["cases"]:
            for name, gold in case["gold"].items():
                self.assertIn(gold, case["payload"]["questions"][name]["criteria"])
        self.assertTrue(all(c["id"].startswith("demo-") for c in dataset["cases"]))

    def test_published_outcomes_reproduce_aggregate_and_paired_results(self):
        scores = score_recorded(json.loads((HERE / "recorded-results.json").read_text()))
        flash, full = scores["clef-flash"], scores["clef"]
        self.assertEqual((flash["decision_correct"], flash["evidence_correct"], flash["both_correct"]), (67, 66, 61))
        self.assertEqual((flash["both_decisions_correct_in_pair"], flash["both_answers_correct_in_pair"]), (31, 27))
        self.assertEqual((full["decision_correct"], full["evidence_correct"], full["both_correct"]), (72, 72, 72))
        self.assertEqual([flash["tasks"][t]["decision_correct"] for t in ("route", "evidence", "check")], [24, 21, 22])

    def test_observation_ledger_cannot_be_scored_as_the_synthetic_fixture(self):
        dataset = build()
        ledger = json.loads((HERE / "recorded-results.json").read_text())
        predictions = [{"id": c["id"], "answers": c["predictions"]["clef"]} for c in ledger["cases"]]
        with self.assertRaisesRegex(ValueError, "Missing, duplicate, or unknown"):
            score_new_run(dataset, {"dataset_sha256": fingerprint(dataset), "predictions": predictions})

    def test_observation_ledger_contains_only_anonymous_selected_choices(self):
        ledger = json.loads((HERE / "recorded-results.json").read_text())
        for row in ledger["cases"]:
            self.assertEqual(set(row), {"id", "task", "family", "gold", "predictions"})
            self.assertRegex(row["id"], r"^observed-(route|evidence|check)-\d{2}-[ab]$")
            for answer in [row["gold"], *row["predictions"].values()]:
                self.assertEqual(set(answer), {"decision", "evidence"})
        encoded = json.dumps(ledger)
        for marker in ("https://", "http://", "projects/", "/mnt/", "serviceAccount:", "@"):
            self.assertNotIn(marker, encoded)

    def test_new_runs_require_exact_fingerprint_and_complete_unique_cases(self):
        dataset = build()
        run = {"dataset_sha256": fingerprint(dataset),
               "predictions": [{"id": c["id"], "answers": c["gold"]} for c in dataset["cases"]]}
        self.assertEqual(score_new_run(dataset, run)["both_correct"], 72)
        for mutation in ("missing", "duplicate", "fingerprint", "invalid_choice"):
            changed = deepcopy(run)
            if mutation == "missing":
                changed["predictions"].pop()
            elif mutation == "duplicate":
                changed["predictions"][-1] = changed["predictions"][0]
            elif mutation == "fingerprint":
                changed["dataset_sha256"] = "0" * 64
            else:
                changed["predictions"][0]["answers"] = {"decision": "unknown", "evidence": "E1"}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                score_new_run(dataset, changed)


if __name__ == "__main__":
    unittest.main()
