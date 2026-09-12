"""Loopback-only management proxy; use SSH forwarding for a remote gateway."""

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

p = argparse.ArgumentParser()
p.add_argument("--env", default="gateway.env")
p.add_argument("--port", type=int, default=18441)
p.add_argument("--upstream", default="http://127.0.0.1:18440")
a = p.parse_args()
config = dict(line.split("=", 1) for line in Path(a.env).read_text().splitlines() if "=" in line)
upstream = urlsplit(a.upstream)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def forward(self):
        if self.headers.get("Host") not in {f"127.0.0.1:{a.port}", f"localhost:{a.port}"}:
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > 26 * 1024 * 1024:
            self.send_error(413)
            return
        data = self.rfile.read(length) if length else None
        headers = {"X-Admin-Token": config["ADMIN_TOKEN"]}
        if self.headers.get("Content-Type"):
            headers["Content-Type"] = self.headers["Content-Type"]
        if self.command == "POST" and self.headers.get("Origin") not in {
            f"http://127.0.0.1:{a.port}",
            f"http://localhost:{a.port}",
        }:
            self.send_error(403)
            return
        connection = http.client.HTTPConnection(upstream.hostname, upstream.port or 80, timeout=60)
        try:
            connection.request(self.command, "/admin" + self.path, body=data, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            self.send_header(
                "Content-Type", response.getheader("Content-Type", "application/octet-stream")
            )
            for name in ("Content-Length", "Content-Disposition"):
                value = response.getheader(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while chunk := response.read(128 * 1024):
                self.wfile.write(chunk)
        finally:
            connection.close()

    do_GET = forward
    do_POST = forward


print(f"Management UI: http://127.0.0.1:{a.port}/", flush=True)
ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
