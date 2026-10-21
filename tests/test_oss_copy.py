"""The Alibaba Cloud OSS backup copy stays off until it's fully configured."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import copy_builds_to_s3

FULL = {
    "OSS_BUCKET": "example-builds",
    "OSS_REGION": "ap-southeast-1",
    "OSS_KEY_ID": "FICTIONAL_KEY_ID",
    "OSS_SECRET": "FICTIONAL_SECRET",
}


class OssCopy(unittest.TestCase):
    def test_off_without_settings(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(copy_builds_to_s3.oss_client())

    def test_off_when_any_setting_is_missing(self):
        for missing in FULL:
            partial = {k: v for k, v in FULL.items() if k != missing}
            with mock.patch.dict(os.environ, partial, clear=True):
                self.assertIsNone(copy_builds_to_s3.oss_client(), missing)

    def test_regional_endpoint_and_bucket(self):
        with mock.patch.dict(os.environ, FULL, clear=True):
            client, bucket = copy_builds_to_s3.oss_client()
        self.assertEqual(bucket, "example-builds")
        self.assertEqual(client.meta.endpoint_url, "https://s3.oss-ap-southeast-1.aliyuncs.com")
        link = client.generate_presigned_url(
            "head_object", Params={"Bucket": bucket, "Key": "a/b.ipa"}, ExpiresIn=60
        )
        self.assertTrue(link.startswith("https://example-builds.s3.oss-ap-southeast-1.aliyuncs.com/a/b.ipa?"))
        self.assertIn("Signature=", link)
        self.assertIn("AWSAccessKeyId=FICTIONAL_KEY_ID", link)


if __name__ == "__main__":
    unittest.main()


class SupabaseTooLarge(unittest.TestCase):
    def test_a_build_over_the_size_limit_is_skipped_not_fatal(self):
        import io
        import urllib.error

        error = urllib.error.HTTPError(
            "https://x.invalid",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"statusCode":"413","error":"Payload too large"}'),
        )
        store = copy_builds_to_s3.Supabase("https://x.invalid", "fictional-key")
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertFalse(store.upload("app/1.0.ipa", io.BytesIO(b"ipa")))
        other = urllib.error.HTTPError("https://x.invalid", 500, "Error", {}, io.BytesIO(b"boom"))
        with mock.patch("urllib.request.urlopen", side_effect=other), self.assertRaises(SystemExit):
            store.upload("app/1.0.ipa", io.BytesIO(b"ipa"))
