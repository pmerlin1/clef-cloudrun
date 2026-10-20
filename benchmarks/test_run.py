"""Offline checks for paid-window safety; never authenticate or contact Google."""
import copy
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("benchmark_run", Path(__file__).with_name("run.py"))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
REAL_SESSION = runner.Session


class FakeSession:
    def __init__(self):
        self.count = 0
        self.scale_calls = []
        self.inference_calls = 0
        self.on_inference = None
        self.public = False
        self.fail_first_pause = False
        self.fail_every_pause = False
        self.refresh_calls = 0

    def authenticate_endpoint(self):
        pass

    def refresh_access(self):
        self.refresh_calls += 1

    def get(self, url):
        if url.endswith(":getIamPolicy"):
            return {"bindings": [{"members": ["allUsers"]}]} if self.public else {"bindings": []}
        return {"name": "projects/example-project/locations/us-central1/services/example-clef",
                "etag": "offline-etag", "uri": "https://example-clef.us-central1.run.app",
                "scaling": {"scalingMode": "MANUAL", "manualInstanceCount": self.count},
                "template": {"scaling": {"maxInstanceCount": 1},
                             "containers": [{"resources": {"limits": {"nvidia.com/gpu": "1"}},
                                             "env": [{"name": "MODEL", "value": "clef-flash"}]}]},
                "invokerIamDisabled": False}

    def call(self, url, body=None, **kwargs):
        if url.endswith(":testIamPermissions"):
            return 200, {"permissions": ["run.routes.invoke", "run.services.update"]}
        if url.endswith("/v1/systemone"):
            self.inference_calls += 1
            if self.on_inference:
                self.on_inference()
            return 200, {"answers": {"decision": {"choice": "review"}}}
        raise AssertionError("Unexpected request")

    def scale(self, url, count):
        self.scale_calls.append(count)
        if not count and (self.fail_first_pause or self.fail_every_pause):
            self.fail_first_pause = False
            raise TimeoutError("simulated cleanup timeout")
        self.count = count
        return copy.deepcopy(self.get(url))


class ManualTimer:
    def __init__(self, seconds, callback):
        self.callback = callback
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class PaidWindowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.dataset = self.root / "fixture.json"
        self.dataset.write_text(json.dumps({"cases": [
            {"id": f"case-{i}", "payload": {"state": "offline", "questions": {}},
             "gold": {"decision": "review"}} for i in range(2)
        ]}))
        self.session = FakeSession()
        self.timer = None

        def timer_factory(seconds, callback):
            self.timer = ManualTimer(seconds, callback)
            return self.timer

        self.stack.enter_context(mock.patch.object(runner, "ROOT", self.root))
        self.stack.enter_context(mock.patch.object(runner, "Session", return_value=self.session))
        self.stack.enter_context(mock.patch.object(runner.threading, "Timer", timer_factory))
        self.stack.enter_context(mock.patch.object(runner.signal, "signal"))
        self.stack.enter_context(mock.patch.object(runner.subprocess, "run", side_effect=AssertionError("No authentication")))
        self.stack.enter_context(mock.patch.object(runner.urllib.request, "build_opener", side_effect=AssertionError("No network")))
        self.stack.enter_context(mock.patch.dict(runner.os.environ, {
            "PROJECT": "example-project", "REGION": "us-central1", "SERVICE": "example-clef", "MODEL": "clef-flash",
        }))
        self.stack.enter_context(redirect_stdout(io.StringIO()))

    def run_window(self, *extra):
        return runner.main(["--dataset", str(self.dataset), "--allow-paid", *extra])

    def result(self):
        return json.loads((self.root / "data/results-clef-flash.json").read_text())

    def test_timeout_pauses_once_without_inference_retry(self):
        def timeout():
            raise TimeoutError("simulated inference timeout")
        self.session.on_inference = timeout
        with self.assertRaisesRegex(TimeoutError, "inference timeout"):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [1, 0])
        self.assertEqual(self.session.inference_calls, 1)
        self.assertEqual(self.session.count, 0)
        self.assertTrue(self.result()["manual_zero_confirmed"])

    def test_normal_final_pause_retries_transient_failure_without_watchdog(self):
        self.session.fail_first_pause = True
        self.run_window()
        self.assertEqual(self.session.scale_calls, [1, 0, 0])
        self.assertEqual(self.session.refresh_calls, 1)
        self.assertEqual(self.session.count, 0)
        self.assertTrue(self.result()["manual_zero_confirmed"])
        self.assertNotIn("cleanup_error", self.result())
        self.assertEqual(self.result()["cleanup_attempt_errors"], [{"attempt": 1, "error": "TimeoutError"}])

    def test_exhausted_cleanup_records_failure_and_prints_emergency_command(self):
        self.session.fail_every_pause = True
        stderr, stdout = io.StringIO(), io.StringIO()
        with self.assertRaisesRegex(TimeoutError, "cleanup timeout"), redirect_stderr(stderr), redirect_stdout(stdout):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [1, 0, 0, 0])
        self.assertEqual(self.session.refresh_calls, 2)
        self.assertEqual(self.session.count, 1)
        self.assertFalse(self.result()["manual_zero_confirmed"])
        self.assertEqual(self.result()["cleanup_error"], "TimeoutError")
        self.assertIn("CLEANUP FAILED", stderr.getvalue())
        self.assertIn("--scaling=0", stderr.getvalue())
        self.assertNotIn("Cleanup confirmed", stdout.getvalue())

    def test_disk_write_failure_cannot_prevent_cleanup_retry(self):
        self.session.fail_first_pause = True

        def fill_disk():
            self.stack.enter_context(mock.patch.object(Path, "write_text", side_effect=OSError("disk full")))

        self.session.on_inference = fill_disk
        with self.assertRaisesRegex(OSError, "disk full"), redirect_stderr(io.StringIO()):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [1, 0, 0])
        self.assertEqual(self.session.count, 0)
        self.assertTrue(self.timer.cancelled)
        self.assertTrue(self.timer.cancelled)

    def test_final_cleanup_rechecks_after_watchdog_pause(self):
        def resume_after_watchdog():
            self.timer.callback()
            self.session.count = 1
        self.session.on_inference = resume_after_watchdog
        with self.assertRaisesRegex(TimeoutError, "deadline reached"):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [1, 0, 0])
        self.assertEqual(self.session.inference_calls, 1)
        self.assertTrue(self.result()["manual_zero_confirmed"])

    def test_failed_watchdog_pause_stops_requests_and_finally_retries(self):
        self.session.fail_first_pause = True
        self.session.on_inference = lambda: self.timer.callback()
        with self.assertRaises(TimeoutError):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [1, 0, 0])
        self.assertEqual(self.session.inference_calls, 1)
        self.assertEqual(self.result()["watchdog_error"], "TimeoutError")
        self.assertTrue(self.result()["manual_zero_confirmed"])

    def test_rejects_existing_workload_without_scaling(self):
        self.session.count = 1
        with self.assertRaisesRegex(RuntimeError, "existing workload"):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [])

    def test_rejects_public_iam_without_scaling(self):
        self.session.public = True
        with self.assertRaisesRegex(RuntimeError, "Public IAM"):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [])

    def test_preflight_only_never_invokes_or_changes_service(self):
        self.run_window("--preflight-only")
        self.assertEqual(self.session.scale_calls, [])
        self.assertEqual(self.session.inference_calls, 0)
        self.assertFalse((self.root / "data").exists())

    def test_success_records_canonical_fingerprint_and_scorer_predictions(self):
        self.run_window()
        result = self.result()
        canonical = json.dumps(json.loads(self.dataset.read_text()), sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(result["dataset_sha256"], hashlib.sha256(canonical).hexdigest())
        self.assertEqual(result["predictions"], [
            {"id": f"case-{i}", "answers": {"decision": "review"}} for i in range(2)
        ])
        self.assertTrue(result["manual_zero_confirmed"])
        self.assertEqual(self.session.scale_calls, [1, 0])

    def test_missing_paid_flag_exits_before_authentication(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            runner.main(["--dataset", str(self.dataset)])
        self.assertEqual(self.session.scale_calls, [])

    def test_existing_result_refuses_accidental_paid_rerun(self):
        (self.root / "data").mkdir()
        (self.root / "data/results-clef-flash.json").write_text("{}")
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [])
        self.assertEqual(self.session.inference_calls, 0)

    def test_model_mismatch_refuses_before_resume(self):
        original_get = self.session.get

        def wrong_model(url):
            document = original_get(url)
            if "template" in document:
                document["template"]["containers"][0]["env"][0]["value"] = "clef"
            return document

        self.session.get = wrong_model
        with self.assertRaisesRegex(RuntimeError, "MODEL does not match"):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [])

    def test_only_legacy_clef_can_omit_model_environment(self):
        service = self.session.get("offline")
        del service["template"]["containers"][0]["env"]
        runner.check_service(service, "clef")
        with self.assertRaisesRegex(RuntimeError, "MODEL does not match"):
            runner.check_service(service, "clef-flash")

    def test_successful_full_output_can_be_consumed_by_offline_scorer(self):
        fixture = json.loads(Path(__file__).with_name("synthetic-cases.json").read_text())
        self.dataset.write_text(json.dumps(fixture))
        original_call = self.session.call

        def gold_response(url, body=None, **kwargs):
            if not url.endswith("/v1/systemone"):
                return original_call(url, body, **kwargs)
            case = fixture["cases"][self.session.inference_calls]
            self.session.inference_calls += 1
            return 200, {"answers": {name: {"choice": value} for name, value in case["gold"].items()}}

        self.session.call = gold_response
        self.run_window()
        spec = importlib.util.spec_from_file_location("offline_score", Path(__file__).with_name("score.py"))
        scorer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scorer)
        score = scorer.score_new_run(fixture, self.result())
        self.assertEqual(score["cases"], 72)
        self.assertEqual(score["both_correct"], 72)
        self.assertTrue(self.result()["manual_zero_confirmed"])

    def test_scaling_update_preserves_service_limits_and_uses_etag(self):
        session = object.__new__(REAL_SESSION)
        session.resume_timeout = 480
        before = self.session.get("offline")
        before["scaling"].update({"minInstanceCount": 0, "maxInstanceCount": 1})
        session.get = mock.Mock(return_value=before)
        session.call = mock.Mock(return_value=(200, {"done": True}))
        session.scale("https://run.googleapis.com/v2/offline", 1)
        body = session.call.call_args.args[1]
        self.assertEqual(body["etag"], "offline-etag")
        self.assertEqual(body["scaling"], {"scalingMode": "MANUAL", "manualInstanceCount": 1,
                                            "minInstanceCount": 0, "maxInstanceCount": 1})

    def test_rejects_workload_appearing_after_preflight_without_pausing_it(self):
        original_authenticate = self.session.authenticate_endpoint

        def racing_workload():
            original_authenticate()
            self.session.count = 1

        self.session.authenticate_endpoint = racing_workload
        with self.assertRaisesRegex(RuntimeError, "Another workload"), redirect_stderr(io.StringIO()):
            self.run_window()
        self.assertEqual(self.session.scale_calls, [])
        self.assertEqual(self.session.count, 1)

    def test_service_endpoint_rejects_redirect_target_and_userinfo(self):
        for uri in ("https://run.app.attacker.example", "https://user@example.run.app", "http://example.run.app"):
            service = self.session.get("offline")
            service["uri"] = uri
            with self.subTest(uri=uri), self.assertRaises(RuntimeError):
                runner.check_service(service)


if __name__ == "__main__":
    unittest.main()
