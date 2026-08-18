import http.client
import threading
import unittest

from http.server import ThreadingHTTPServer

from server import PayPulseHandler


class QuietPayPulseHandler(PayPulseHandler):
    def log_message(self, format, *args):
        pass


class StaticServingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), QuietPayPulseHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_allowlisted_assets_are_public(self):
        expected = {
            "/": b"PayPulse",
            "/index.html": b"PayPulse",
            "/app.js": b"function",
            "/styles.css": b":root",
            "/vendor/chart.umd.min.js": b"Chart",
        }
        for path, marker in expected.items():
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn(marker, body)
                self.assertEqual(int(headers["Content-Length"]), len(body))

    def test_query_strings_do_not_break_public_assets(self):
        status, _, body = self.request("/app.js?v=cache-bust")
        self.assertEqual(status, 200)
        self.assertIn(b"function", body)

    def test_api_routes_still_bypass_static_asset_rules(self):
        status, headers, body = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertIn(b'"status": "ok"', body)

    def test_sensitive_files_and_directories_are_not_public(self):
        blocked_paths = (
            "/.git/HEAD",
            "/.git/config",
            "/.gitignore",
            "/server.py",
            "/database.py",
            "/requirements.txt",
            "/data/",
            "/deploy/",
            "/docs/",
            "/tests/",
            "/tmp/",
            "/vendor/",
        )
        for path in blocked_paths:
            with self.subTest(path=path):
                status, _, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_encoded_and_normalized_bypass_attempts_are_blocked(self):
        bypass_paths = (
            "/%2egit/HEAD",
            "/.git%2fHEAD",
            "/%2Egit%2Fconfig",
            "//.git/HEAD",
            "/vendor/%2e%2e/server.py",
            "/%252e%252e/server.py",
            "/styles.css/../server.py",
            "/server.py?download=1",
        )
        for path in bypass_paths:
            with self.subTest(path=path):
                status, _, _ = self.request(path)
                self.assertEqual(status, 404)

    def test_head_requests_use_the_same_allowlist(self):
        status, headers, body = self.request("/styles.css?v=1", method="HEAD")
        self.assertEqual(status, 200)
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(body, b"")

        for path in ("/.git/HEAD", "/server.py", "/vendor/"):
            with self.subTest(path=path):
                status, _, body = self.request(path, method="HEAD")
                self.assertEqual(status, 404)
                self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
