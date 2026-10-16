"""Local HTTP contract tests; these do not establish iPhone connectivity."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


class ProbeTests(unittest.TestCase):
    def run_probe(self, ready=True, screenshot=None, action="status"):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = {"value": {"ready": ready}} if self.path == "/status" else {"value": screenshot}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "screen.png"
                result = subprocess.run([
                    sys.executable, str(Path(__file__).with_name("wda_probe.py")), action,
                    "--url", f"http://127.0.0.1:{server.server_port}", "--output", str(output),
                ], capture_output=True, text=True, timeout=10)
                data = output.read_bytes() if output.exists() else None
                return result.returncode, json.loads(result.stdout), data
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_status_ready(self):
        code, result, _ = self.run_probe()
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])

    def test_status_not_ready(self):
        code, result, _ = self.run_probe(ready=False)
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])

    def test_screenshot_png(self):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aP1kAAAAASUVORK5CYII=")
        code, result, output = self.run_probe(screenshot=base64.b64encode(png).decode(), action="screenshot")
        self.assertEqual(code, 0)
        self.assertEqual(output, png)

    def test_screenshot_invalid(self):
        code, result, output = self.run_probe(screenshot=base64.b64encode(b"not PNG").decode(), action="screenshot")
        self.assertEqual(code, 2)
        self.assertFalse(result["ok"])
        self.assertIsNone(output)


if __name__ == "__main__":
    unittest.main()
