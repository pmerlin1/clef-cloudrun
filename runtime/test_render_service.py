import json
import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from models import model
from render_service import needs_release, service
from settings import settings


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        config = patch.dict(os.environ, {"PROJECT": "example-project", "REGION": "us-central1", "SERVICE": "clef",
                                         "MODEL": "clef", "GPU_PROFILE": "model-default"})
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
        with patch.dict(os.environ, {"PROJECT": ""}), self.assertRaises(ValueError):
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
        with patch.dict(os.environ, {"MODEL": "unknown-model"}), self.assertRaisesRegex(ValueError, "MODEL must be one of"):
            service("us-central1-docker.pkg.dev/example-project/clef/api@sha256:" + "d" * 64)

    def test_flash_rtx_profile_changes_hardware_and_keeps_pinned_weights(self):
        default = model("clef-flash", "model-default")
        with patch.dict(os.environ, {"MODEL": "clef-flash", "SERVICE": "clef-flash", "GPU_PROFILE": "rtx-pro-6000"}):
            selected = settings()
            desired = service("us-central1-docker.pkg.dev/example-project/clef-flash/api@sha256:" + "e" * 64)
        self.assertEqual(selected["gpu_profile"], "rtx-pro-6000")
        self.assertEqual(selected["model_revision"], default["revision"])
        self.assertEqual(selected["model_manifest"], default["manifest"])
        self.assertEqual(selected["hf_repo"], default["hf_repo"])
        template = desired["spec"]["template"]
        self.assertEqual(template["metadata"]["annotations"]["run.googleapis.com/gpu-zonal-redundancy-disabled"], "true")
        spec = template["spec"]
        self.assertEqual(spec["nodeSelector"]["run.googleapis.com/accelerator"], "nvidia-rtx-pro-6000")
        self.assertEqual(spec["containers"][0]["resources"]["limits"],
                         {"cpu": "20", "memory": "80Gi", "nvidia.com/gpu": "1"})
        env = {entry["name"]: entry["value"] for entry in spec["containers"][0]["env"]}
        self.assertEqual(env["MODEL"], "clef-flash")
        self.assertEqual(env["MODEL_REVISION"], default["revision"])

    def test_27b_default_and_explicit_rtx_profile_keep_same_shape(self):
        default, explicit = model("clef", "model-default"), model("clef", "rtx-pro-6000")
        for key in ("revision", "manifest", "hf_repo", "accelerator", "cpu", "memory", "gpu_zonal_redundancy_disabled"):
            self.assertEqual(default[key], explicit[key])

    def test_unknown_or_l4_override_profile_is_rejected(self):
        for profile in ("unknown", "nvidia-l4", "l4", ""):
            with self.subTest(profile=profile), patch.dict(os.environ, {"GPU_PROFILE": profile}), self.assertRaisesRegex(ValueError, "GPU_PROFILE must be one of"):
                service("us-central1-docker.pkg.dev/example-project/clef/api@sha256:" + "f" * 64)

    def test_custom_service_names_every_resource_consistently(self):
        with patch.dict(os.environ, {"SERVICE": "my-clef", "REGION": "europe-west4"}):
            desired = service("europe-west4-docker.pkg.dev/example-project/my-clef/api@sha256:" + "b" * 64)
        self.assertEqual(desired["metadata"]["name"], "my-clef")
        spec = desired["spec"]["template"]["spec"]
        self.assertEqual(spec["serviceAccountName"], "my-clef-runtime@example-project.iam.gserviceaccount.com")
        env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
        self.assertEqual(env["MODEL_BUCKET"], "example-project-my-clef-weights")

    def test_dedicated_project_shares_resources_for_both_models(self):
        shared = {"RESOURCE_PREFIX": "clef", "WEIGHTS_BUCKET": "example-project-weights",
                  "VPC_NETWORK": "clef-vpc", "VPC_SUBNET": "clef-subnet"}
        for name in ("clef", "clef-flash"):
            with self.subTest(name=name), patch.dict(os.environ, {**shared, "MODEL": name, "SERVICE": ""}):
                desired = service("us-central1-docker.pkg.dev/example-project/clef/api@sha256:" + "a" * 64)
                self.assertEqual(desired["metadata"]["name"], name)
                template = desired["spec"]["template"]
                self.assertEqual(json.loads(template["metadata"]["annotations"]["run.googleapis.com/network-interfaces"]),
                                 [{"network": "clef-vpc", "subnetwork": "clef-subnet"}])
                self.assertEqual(template["spec"]["serviceAccountName"], "clef-runtime@example-project.iam.gserviceaccount.com")
                env = {e["name"]: e["value"] for e in template["spec"]["containers"][0]["env"]}
                self.assertEqual(env["MODEL_BUCKET"], "example-project-weights")


if __name__ == "__main__":
    unittest.main()
