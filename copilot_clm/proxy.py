"""Authenticated loopback BYOK endpoint. Streams provider bytes unchanged."""
import hmac
import json
import logging
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .storage import MAX_BYTES, encode, private_dir, write_json


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # urllib otherwise carries Authorization to redirected hosts.
        return None


class Proxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, engine, upstream, token, api_key="", timeout=180):
        parsed = urlsplit(upstream)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("--upstream must be an HTTP(S) base URL without credentials/query/fragment")
        self.engine, self.upstream, self.token = engine, upstream.rstrip("/"), token
        self.api_key, self.timeout = api_key, timeout
        # Explicit loopback connection must not go through a corporate HTTP proxy.
        self.opener = urllib.request.build_opener(
            NoRedirect(),
            urllib.request.ProxyHandler({}) if parsed.hostname in ("localhost", "127.0.0.1", "::1")
            else urllib.request.ProxyHandler())
        self.mutex = threading.Lock()
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # Never log request headers or upstream credentials.

    def error(self, status, message):
        body = encode({"error": {"message": message, "type": "copilot_clm_error"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        server = self.server
        authorization = self.headers.get("Authorization", "")
        if not hmac.compare_digest(authorization, "Bearer " + server.token):
            return self.error(401, "Invalid local CLM token")
        routes = {("GET", "/v1/models"): "/models",
                  ("POST", "/v1/responses"): "/responses",
                  ("POST", "/v1/chat/completions"): "/chat/completions"}
        route = routes.get((self.command, self.path))
        if route is None:
            return self.error(404, "Unsupported CLM endpoint")
        status = None
        payload = None
        response_started = False
        try:
            if self.command == "POST":
                if self.headers.get("Transfer-Encoding"):
                    return self.error(400, "Use a Content-Length JSON request")
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BYTES:
                    return self.error(413, "Request must be between 1 byte and 32 MiB")
                self.connection.settimeout(30)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                with server.mutex:
                    body, status = server.engine.transform(body, "responses" if route == "/responses" else "completions")
                    if status is not None:
                        server.engine.busy.add(status["stream"])
                    else:
                        status = server.engine.track_utility(body)
                payload = encode(body).encode()
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if server.api_key:
                headers["Authorization"] = "Bearer " + server.api_key
            request = urllib.request.Request(server.upstream + route, data=payload,
                                             headers=headers, method=self.command)
            try:
                response = server.opener.open(request, timeout=server.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                if 300 <= response.status < 400:
                    return self.error(502, "Upstream redirects are unsupported; configure the final provider URL")
                self.send_response(response.status)
                response_started = True
                content_type = response.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", content_type)
                if response.headers.get("Content-Encoding"):
                    self.send_header("Content-Encoding", response.headers["Content-Encoding"])
                self.send_header("Connection", "close")
                if response.headers.get("Retry-After"):
                    self.send_header("Retry-After", response.headers["Retry-After"])
                self.end_headers()
                self.close_connection = True
                pending = b""
                json_body = bytearray()
                while chunk := response.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if "text/event-stream" in content_type:
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            if line.startswith(b"data:"):
                                self.record_usage(line[5:].strip(), status)
                        if len(pending) > MAX_BYTES:
                            pending = b""  # Usage observation is bounded; stream is still forwarded.
                    elif len(json_body) + len(chunk) <= MAX_BYTES:
                        json_body.extend(chunk)
                if json_body:
                    self.record_usage(json_body, status)
        except (ValueError, TypeError, KeyError) as exc:
            if not response_started:
                self.error(400, str(exc))
        except (urllib.error.URLError, TimeoutError, OSError):
            # Do not expose URLs containing secrets, credential headers or network internals.
            if not response_started:
                self.error(502, "CLM upstream connection failed or timed out")
        finally:
            if status is not None:
                with server.mutex:
                    server.engine.busy.discard(status["stream"])

    def record_usage(self, data, status):
        if status is None:
            return
        try:
            event = json.loads(data)
            response = event.get("response") or event
            usage = response.get("usage")
            if usage:
                directory = self.server.engine.root / status["stream"]
                record = {"request": status["requests"], "usage": usage}
                write_json(directory / "usage.json", record)
                history = private_dir(directory / "usage")
                write_json(history / f'{status["requests"]:04d}.json', record)
        except (ValueError, AttributeError):
            pass
        except OSError:
            logging.warning("copilot-clm: could not save provider usage; accounting is incomplete")
