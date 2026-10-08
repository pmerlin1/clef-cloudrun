"""No network or multi-GB data: uploader failure paths must fail before download."""
import hashlib
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import upload_weights


class UploadWeightsTest(unittest.TestCase):
    data = b"small pinned test content"
    entry = {"name": "model.safetensors", "size": len(data),
             "sha256": hashlib.sha256(data).hexdigest()}

    def error(self, code):
        return urllib.error.HTTPError("https://storage.googleapis.com/test", code,
                                      "Test response", None, None)

    @patch("upload_weights.subprocess.run")
    @patch("upload_weights.subprocess.check_output", return_value="mock-token")
    @patch("upload_weights.urllib.request.urlopen")
    def test_forbidden_server_and_auth_errors_do_not_download(self, urlopen, token, run):
        for code in (401, 403, 429, 500, 503):
            with self.subTest(code=code):
                urlopen.reset_mock()
                urlopen.side_effect = self.error(code)
                with self.assertRaisesRegex(RuntimeError, f"HTTP {code}; no download attempted"):
                    upload_weights.upload_entry(self.entry, "test-bucket")
                self.assertEqual(urlopen.call_count, 1)
                run.assert_not_called()

    @patch("upload_weights.subprocess.run")
    @patch("upload_weights.subprocess.check_output", return_value="mock-token")
    @patch("upload_weights.urllib.request.urlopen")
    def test_404_downloads_verifies_then_creates_without_overwrite(self, urlopen, token, run):
        urlopen.side_effect = [self.error(404), io.BytesIO(self.data)]
        upload_weights.upload_entry(self.entry, "test-bucket")
        self.assertEqual(urlopen.call_count, 2)
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["gcloud", "storage", "cp"])
        self.assertIn("--if-generation-match=0", command)
        self.assertIn("--custom-metadata=sha256=" + self.entry["sha256"], command)
        self.assertTrue(run.call_args.kwargs["check"])

    @patch("upload_weights.subprocess.run")
    @patch("upload_weights.subprocess.check_output", return_value="mock-token")
    @patch("upload_weights.urllib.request.urlopen")
    def test_matching_existing_object_skips_download(self, urlopen, token, run):
        obj = {"size": str(self.entry["size"]), "metadata": {"sha256": self.entry["sha256"]}}
        urlopen.return_value = io.BytesIO(json.dumps(obj).encode())
        upload_weights.upload_entry(self.entry, "test-bucket")
        self.assertEqual(urlopen.call_count, 1)
        run.assert_not_called()

    @patch("upload_weights.subprocess.run")
    @patch("upload_weights.subprocess.check_output", return_value="mock-token")
    @patch("upload_weights.urllib.request.urlopen")
    def test_existing_object_with_wrong_digest_fails(self, urlopen, token, run):
        obj = {"size": str(self.entry["size"]), "metadata": {"sha256": "different"}}
        urlopen.return_value = io.BytesIO(json.dumps(obj).encode())
        with self.assertRaisesRegex(RuntimeError, "Unexpected existing object"):
            upload_weights.upload_entry(self.entry, "test-bucket")
        self.assertEqual(urlopen.call_count, 1)
        run.assert_not_called()

    @patch("upload_weights.subprocess.run")
    @patch("upload_weights.subprocess.check_output", return_value="mock-token")
    @patch("upload_weights.urllib.request.urlopen")
    def test_bad_or_excessive_download_cannot_upload(self, urlopen, token, run):
        for contents, message in ((b"bad", "Integrity mismatch"),
                                  (self.data + b"extra", "Download exceeds pinned size")):
            with self.subTest(contents=contents):
                urlopen.side_effect = [self.error(404), io.BytesIO(contents)]
                with self.assertRaisesRegex(RuntimeError, message):
                    upload_weights.upload_entry(self.entry, "test-bucket")
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
