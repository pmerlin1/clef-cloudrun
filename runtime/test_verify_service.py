"""Offline IAM/scaling validation in automatic and explicitly paused modes."""
from copy import deepcopy
import unittest

from verify_service import validate


class VerifyServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = {
            "metadata": {"annotations": {
                "run.googleapis.com/invoker-iam-disabled": "false",
                "run.googleapis.com/scalingMode": "automatic",
                "run.googleapis.com/minScale": "0", "run.googleapis.com/maxScale": "1",
            }},
            "spec": {"template": {"metadata": {"annotations": {
                "autoscaling.knative.dev/minScale": "0", "autoscaling.knative.dev/maxScale": "1",
                "run.googleapis.com/gpu-zonal-redundancy-disabled": "true",
            }}}},
        }
        self.policy = {"bindings": [{"role": "roles/run.invoker", "members": ["user:caller@example.com"]}]}

    def manual(self, count="0"):
        self.service["metadata"]["annotations"].update({
            "run.googleapis.com/scalingMode": "manual", "run.googleapis.com/manualInstanceCount": count,
        })

    def test_automatic_is_default(self):
        validate(self.service, self.policy)
        del self.service["metadata"]["annotations"]["run.googleapis.com/scalingMode"]
        validate(self.service, self.policy)

    def test_manual_zero_accepted_only_with_explicit_paused_flag(self):
        self.manual()
        with self.assertRaises(ValueError):
            validate(self.service, self.policy)
        validate(self.service, self.policy, paused=True)

    def test_paused_flag_rejects_automatic_nonzero_and_unknown_targets(self):
        with self.assertRaises(ValueError):
            validate(self.service, self.policy, paused=True)
        for count in ("1", "-1", "unknown"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.manual(count)
                validate(self.service, self.policy, paused=True)
        self.manual()
        del self.service["metadata"]["annotations"]["run.googleapis.com/manualInstanceCount"]
        with self.assertRaises(ValueError):
            validate(self.service, self.policy, paused=True)

    def test_unknown_mode_fails_in_both_modes(self):
        self.service["metadata"]["annotations"]["run.googleapis.com/scalingMode"] = "unknown"
        for paused in (False, True):
            with self.subTest(paused=paused), self.assertRaises(ValueError):
                validate(self.service, self.policy, paused=paused)

    def test_public_iam_and_disabled_checks_rejected_in_both_modes(self):
        for paused in (False, True):
            if paused:
                self.manual()
            for principal in ("allUsers", "allAuthenticatedUsers"):
                policy = {"bindings": [{"members": [principal]}]}
                with self.subTest(paused=paused, principal=principal), self.assertRaises(ValueError):
                    validate(self.service, policy, paused=paused)
            service = deepcopy(self.service)
            service["metadata"]["annotations"]["run.googleapis.com/invoker-iam-disabled"] = "true"
            with self.subTest(paused=paused), self.assertRaises(ValueError):
                validate(service, self.policy, paused=paused)

    def test_service_and_revision_limits_rejected_in_both_modes(self):
        for paused in (False, True):
            if paused:
                self.manual()
            for scope, key, value in (
                ("service", "run.googleapis.com/minScale", "1"),
                ("service", "run.googleapis.com/maxScale", "2"),
                ("revision", "autoscaling.knative.dev/minScale", "1"),
                ("revision", "autoscaling.knative.dev/maxScale", "2"),
            ):
                service = deepcopy(self.service)
                values = service["metadata"]["annotations"] if scope == "service" else service["spec"]["template"]["metadata"]["annotations"]
                values[key] = value
                with self.subTest(paused=paused, scope=scope, key=key), self.assertRaises(ValueError):
                    validate(service, self.policy, paused=paused)


if __name__ == "__main__":
    unittest.main()
