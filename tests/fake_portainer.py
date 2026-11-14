"""A Portainer that answers like the parts of it the scripts use, and keeps what it got."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KEY = "ptr_secret-token-1234567890"


class FakePortainer:
    """Answers like the parts of Portainer the script uses, and keeps what it got."""

    def __init__(self):
        self.environments = [{"Id": 3, "Name": "docker-host"}]
        self.build_stream = [
            {"stream": "Step 1/3 : FROM python:3.12-slim\n"},
            {"stream": "Successfully built abc123\n"},
        ]
        self.image_status = 200
        self.fail_with = None  # (status, body) for every request
        self.requests = []

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status, body=b"", content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                url = urlparse(self.path)
                owner.requests.append({
                    "method": self.command, "path": url.path,
                    "query": parse_qs(url.query),
                    "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body,
                })

                if owner.fail_with:
                    status, text = owner.fail_with
                    return self._answer(status, json.dumps({"message": text}).encode())
                if self.headers.get("X-API-Key") != KEY:
                    return self._answer(401, b'{"message": "Invalid API key"}')

                routed = owner.route(self.command, url.path, parse_qs(url.query), body)
                if routed is not None:
                    status, payload = routed
                    if not isinstance(payload, bytes):
                        payload = json.dumps(payload).encode()
                    return self._answer(status, payload)

                if url.path == "/api/endpoints":
                    return self._answer(200, json.dumps(owner.environments).encode())
                if url.path.endswith("/docker/build") and self.command == "POST":
                    lines = "".join(json.dumps(m) + "\r\n" for m in owner.build_stream)
                    return self._answer(200, lines.encode())
                if "/docker/images/" in url.path and url.path.endswith("/json") \
                        and not url.path.endswith("/images/json"):
                    return self._answer(owner.image_status, b"{}")
                return self._answer(404, b'{"message": "not found"}')

            do_GET = do_POST = do_PUT = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def route(self, method, path, query, body):
        """For a subclass: (status, body) for a call it knows, else None."""
        return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def calls(self, method, suffix):
        return [r for r in self.requests if r["method"] == method and r["path"].endswith(suffix)]
