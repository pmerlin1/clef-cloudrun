import unittest

from build_manifest import build_manifest


class BuildManifestTest(unittest.TestCase):
    def test_build_manifest_structure(self):
        manifest = build_manifest("build-123", "rev-abc")
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["build_id"], "build-123")
        self.assertEqual(manifest["source_revision"], "rev-abc")
        self.assertIn("python_version", manifest)
        self.assertIsInstance(manifest["packages"], dict)

    def test_same_build_and_revision_rejected(self):
        with self.assertRaises(ValueError):
            build_manifest("identical", "identical")

    def test_invalid_characters_rejected(self):
        with self.assertRaises(ValueError):
            build_manifest("invalid build id!", "rev-123")


if __name__ == "__main__":
    unittest.main()
