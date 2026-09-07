import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orpheus.server import storage


class StorageChecks(unittest.TestCase):
    def test_signed_upload_and_download_contract(self):
        blob = SimpleNamespace(
            generate_signed_url=lambda **kwargs: "https://signed.example/object"
        )
        bucket = SimpleNamespace(blob=lambda name: blob)
        with patch.object(storage.config, "STORAGE_BACKEND", "gcs"), patch.object(
            storage.config, "GCS_BUCKET", "bucket"
        ), patch.object(storage, "_bucket", return_value=bucket):
            upload = storage.upload_url("a" * 16, "video", "video/mp4")
            download = storage.download_url("a" * 16, "video.mp4")
        self.assertEqual(upload["method"], "PUT")
        self.assertEqual(download["method"], "GET")
        self.assertIn("projects/" + "a" * 16, upload["object"])

    def test_invalid_object_is_rejected(self):
        with self.assertRaises(ValueError):
            storage.download_url("a" * 16, "../.env")
        with self.assertRaises(ValueError):
            storage.upload_url("bad", "video")

    def test_compute_credentials_use_iam_sign_blob(self):
        calls = []

        class Credentials:
            service_account_email = "api@example.iam.gserviceaccount.com"
            token = "access-token"

            def refresh(self, request):
                calls.append("refresh")

        class Blob:
            def generate_signed_url(self, **kwargs):
                calls.append(kwargs)
                if "access_token" not in kwargs:
                    raise AttributeError("metadata credentials cannot sign locally")
                return "https://signed.example/object"

        bucket = SimpleNamespace(
            blob=lambda name: Blob(), client=SimpleNamespace(_credentials=Credentials())
        )
        with patch.object(storage.config, "STORAGE_BACKEND", "gcs"), patch.object(
            storage.config, "GCS_BUCKET", "bucket"
        ), patch.object(storage, "_bucket", return_value=bucket):
            result = storage.download_url("a" * 16, "video.mp4")
        self.assertEqual(result["url"], "https://signed.example/object")
        self.assertEqual(calls[0].get("method"), "GET")
        self.assertEqual(calls[2]["service_account_email"], Credentials.service_account_email)
        self.assertEqual(calls[2]["access_token"], Credentials.token)
        self.assertEqual(calls.count("refresh"), 1)


if __name__ == "__main__":
    unittest.main()
