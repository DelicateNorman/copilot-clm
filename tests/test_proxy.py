import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from copilot_clm.engine import Engine
from copilot_clm.proxy import Proxy
from copilot_clm.storage import read_json
from test_engine import request


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.server.received = (self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        if getattr(self.server, "redirect", None):
            self.send_response(307)
            self.send_header("Location", self.server.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":123,"output_tokens":4}}}\n\n'
        if getattr(self.server, "long_stream", False):
            payload += b': padding' + b' ' * 131072 + b'\n\ndata: [DONE]\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.proxy = Proxy(Engine(self.temp.name), f"http://127.0.0.1:{self.upstream.server_port}/v1", "local-token", "upstream-key")
        for server in (self.upstream, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        for server in (self.proxy, self.upstream):
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def send(self, body=None, token="local-token", path="/v1/responses"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.proxy.server_port}{path}",
            data=json.dumps(body or request()).encode(), headers={"Authorization": "Bearer " + token,
                                                               "X-Untrusted": "must not forward"})
        return urllib.request.urlopen(req, timeout=5)

    def test_streaming_and_credentials_and_usage(self):
        with self.send() as response:
            self.assertIn(b'"input_tokens":123', response.read())
        path, headers, body = self.upstream.received
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer upstream-key")
        self.assertNotIn("X-Untrusted", headers)
        self.assertFalse(body["store"])
        usage = next(Path(self.temp.name).glob("stream-*/usage/0001.json"))
        self.assertEqual(read_json(usage)["usage"]["input_tokens"], 123)
        for file in Path(self.temp.name).rglob("*.json"):
            self.assertNotIn("upstream-key", file.read_text())
            self.assertNotIn("local-token", file.read_text())

    def test_auth_and_path(self):
        for kwargs, expected in (({"token": "wrong"}, 401), ({"path": "/v1/unknown"}, 404)):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.send(**kwargs)
            self.assertEqual(cm.exception.code, expected)
        self.assertFalse(hasattr(self.upstream, "received"))

    def test_invalid_context_does_not_reach_provider(self):
        body = request()
        body["previous_response_id"] = "unsupported"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.send(body)
        self.assertEqual(cm.exception.code, 400)
        self.assertFalse(hasattr(self.upstream, "received"))

    def test_connection_failure_returns_json_even_when_client_requests_close(self):
        with patch.object(self.proxy.opener, "open", side_effect=urllib.error.URLError("private network details")):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.send()
            self.assertEqual(cm.exception.code, 502)
            data = json.loads(cm.exception.read())
            self.assertIn("upstream connection failed", data["error"]["message"])
            self.assertNotIn("private network details", str(data))

    def test_loopback_listener_does_not_require_reverse_dns(self):
        with patch("socket.getfqdn", side_effect=OSError("reverse DNS unavailable")):
            server = Proxy(Engine(self.temp.name), "http://127.0.0.1:1/v1", "local-token")
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
                self.assertGreater(server.server_port, 0)
            finally:
                server.server_close()

    def test_utility_usage_is_recorded_without_creating_a_context_stream(self):
        body = {"model": "test", "input": [{"role": "user", "content": "summarize"}]}
        with self.send(body) as response:
            response.read()
        self.assertEqual(self.upstream.received[2], body)
        self.assertFalse(list(Path(self.temp.name).glob("stream-*")))
        usage = Path(self.temp.name) / "utility/usage/0001.json"
        self.assertEqual(read_json(usage)["usage"]["input_tokens"], 123)

    def test_redirect_does_not_forward_provider_credentials(self):
        destination = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=destination.serve_forever, daemon=True).start()
        self.upstream.redirect = f"http://127.0.0.1:{destination.server_port}/stolen"
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.send()
            self.assertEqual(cm.exception.code, 502)
            self.assertFalse(hasattr(destination, "received"))
        finally:
            destination.shutdown()
            destination.server_close()

    def test_usage_io_failure_does_not_cut_off_the_provider_stream(self):
        # State validation still succeeds; only the optional usage observer fails.
        self.upstream.long_stream = True
        with patch("copilot_clm.proxy.write_json", side_effect=OSError("disk unavailable")):
            with self.send() as response:
                data = response.read()
                self.assertIn(b'"input_tokens":123', data)
                self.assertTrue(data.endswith(b'data: [DONE]\n\n'))


if __name__ == "__main__":
    unittest.main()
