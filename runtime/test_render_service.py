from copy import deepcopy
import os
import unittest
from unittest.mock import patch

from render_service import needs_release, service


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        config = patch.dict(os.environ, {"PROJECT": "example-project", "REGION": "us-central1", "SERVICE": "clef"})
        config.start()
        self.addCleanup(config.stop)
        self.desired = service("us-central1-docker.pkg.dev/example-project/clef/api@sha256:" + "a" * 64)
        self.actual = deepcopy(self.desired)

    def test_identical_digest_and_configuration_skip_revision(self):
        self.actual["metadata"]["generation"] = 123
        del self.actual["metadata"]["annotations"]["run.googleapis.com/minScale"]
        del self.actual["metadata"]["annotations"]["run.googleapis.com/invoker-iam-disabled"]
        del self.actual["metadata"]["annotations"]["run.googleapis.com/scalingMode"]
        self.actual["spec"]["template"]["spec"]["containers"][0]["env"].reverse()
        self.assertFalse(needs_release(self.actual, self.desired))

    def test_same_digest_with_changed_probe_does_need_revision(self):
        self.actual["spec"]["template"]["spec"]["containers"][0]["startupProbe"]["periodSeconds"] = 1
        self.assertTrue(needs_release(self.actual, self.desired))

    def test_changed_digest_needs_revision(self):
        self.actual["spec"]["template"]["spec"]["containers"][0]["image"] += "b"
        self.assertTrue(needs_release(self.actual, self.desired))

    def test_paused_service_cannot_be_mistaken_for_automatic_scaling(self):
        self.actual["metadata"]["annotations"]["run.googleapis.com/scalingMode"] = "manual"
        self.assertTrue(needs_release(self.actual, self.desired))

    def test_rejects_another_repository(self):
        with self.assertRaises(ValueError):
            service("example.com/untrusted@sha256:" + "a" * 64)

    def test_project_must_be_explicit(self):
        with patch.dict(os.environ, {"PROJECT": ""}):
            with self.assertRaises(ValueError):
                service("example.com/untrusted@sha256:" + "a" * 64)

    def test_flash_uses_l4_shape_and_pinned_revision(self):
        with patch.dict(os.environ, {"MODEL": "clef-flash", "SERVICE": "clef-flash"}):
            desired = service("us-central1-docker.pkg.dev/example-project/clef-flash/api@sha256:" + "c" * 64)
        self.assertEqual(desired["metadata"]["name"], "clef-flash")
        template_annotations = desired["spec"]["template"]["metadata"]["annotations"]
        self.assertEqual(template_annotations["run.googleapis.com/gpu-zonal-redundancy-disabled"], "false")
        spec = desired["spec"]["template"]["spec"]
        self.assertEqual(spec["nodeSelector"]["run.googleapis.com/accelerator"], "nvidia-l4")
        self.assertEqual(spec["containers"][0]["resources"]["limits"],
                         {"cpu": "8", "memory": "32Gi", "nvidia.com/gpu": "1"})
        env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
        self.assertEqual(env["MODEL"], "clef-flash")
        self.assertEqual(env["MODEL_REVISION"], "8b2e5fd17c09fd49bc2880b805d5323ec7ff4ff3")
        self.assertEqual(env["MODEL_BUCKET"], "example-project-clef-flash-weights")

    def test_unknown_model_is_rejected(self):
        with patch.dict(os.environ, {"MODEL": "unknown-model"}):
            with self.assertRaisesRegex(ValueError, "MODEL must be one of"):
                service("us-central1-docker.pkg.dev/example-project/clef/api@sha256:" + "d" * 64)

    def test_custom_service_names_every_resource_consistently(self):
        with patch.dict(os.environ, {"SERVICE": "my-clef", "REGION": "europe-west4"}):
            desired = service("europe-west4-docker.pkg.dev/example-project/my-clef/api@sha256:" + "b" * 64)
        self.assertEqual(desired["metadata"]["name"], "my-clef")
        spec = desired["spec"]["template"]["spec"]
        self.assertEqual(spec["serviceAccountName"], "my-clef-runtime@example-project.iam.gserviceaccount.com")
        env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
        self.assertEqual(env["MODEL_BUCKET"], "example-project-my-clef-weights")


if __name__ == "__main__":
    unittest.main()
