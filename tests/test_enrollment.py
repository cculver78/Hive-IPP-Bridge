import io
import json
import unittest
from unittest.mock import patch

from hive_ipp_bridge import enrollment


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class EnrollmentTests(unittest.TestCase):
    def test_parses_regional_setup_link(self):
        link = enrollment.parse_setup_link(
            "https://uk.hive.papercut.com/setup-instructions?t=one-time&orgId=school"
        )
        self.assertEqual(link.auth_token, "one-time")
        self.assertEqual(link.organization_id, "school")
        self.assertEqual(link.service_base, "https://uk.pmitc.papercut.com")

    def test_rejects_untrusted_setup_host(self):
        with self.assertRaisesRegex(enrollment.EnrollmentError, "recognized"):
            enrollment.parse_setup_link("https://example.com/setup?t=secret")

    @patch("hive_ipp_bridge.enrollment.uuid.uuid4", return_value="generated-client")
    @patch("hive_ipp_bridge.enrollment.urllib.request.urlopen")
    def test_claims_credentials(self, urlopen, _uuid):
        urlopen.return_value = Response(json.dumps({"token": "user-jwt", "orgId": "school"}).encode())

        credentials = enrollment.claim("https://hive.papercut.com/setup?t=one-time")

        self.assertEqual(credentials.jwt, "user-jwt")
        self.assertEqual(credentials.client_id, "generated-client")
        self.assertEqual(credentials.organization_id, "school")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://pmitc.papercut.com" + enrollment.CLAIM_PATH)
        self.assertEqual(request.get_header("Authorization"), "Bearer one-time")
        self.assertEqual(request.get_header("Client-id"), "generated-client")
        self.assertEqual(json.loads(request.data), {"appInfo": "Hive IPP Bridge"})

    @patch("hive_ipp_bridge.enrollment.urllib.request.urlopen")
    def test_rejects_incomplete_response(self, urlopen):
        urlopen.return_value = Response(b"{}")
        with self.assertRaisesRegex(enrollment.EnrollmentError, "incomplete"):
            enrollment.claim("one-time-token")


if __name__ == "__main__":
    unittest.main()
