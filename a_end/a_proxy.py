#!/usr/bin/env python3
"""A-end HTTP proxy for the bidirectional clipboard Git tunnel."""

from __future__ import annotations

import argparse
import sys
import threading
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qtc_tunnel.clipboard import ClipboardEndpoint, WindowsClipboard
from qtc_tunnel.git_transport import ClipboardGitClient


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = "ClipboardGitTunnel/0.1"

    def do_GET(self): self._handle()
    def do_HEAD(self): self._handle(head_only=True)
    def do_POST(self): self._handle()
    def do_PUT(self): self._handle()
    def do_PATCH(self): self._handle()
    def do_DELETE(self): self._handle()

    def _handle(self, head_only: bool = False):
        tunnel: "AProxy" = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > tunnel.max_request_bytes:
            self.send_error(413, "request body too large")
            return
        body = self.rfile.read(length) if length else b""
        headers = [(str(k), str(v)) for k, v in self.headers.items()]
        session = uuid.uuid4().hex
        try:
            with tunnel.lock:
                response = tunnel.client.request(
                    session, self.command, self.path, headers, body, tunnel.timeout)
        except TimeoutError as exc:
            self.send_error(504, str(exc))
            return
        except Exception as exc:
            self.send_error(502, f"clipboard tunnel error: {exc}")
            return
        self.send_response(response.status)
        blocked = {"connection", "transfer-encoding", "content-length", "content-encoding"}
        for name, value in response.headers:
            if name.lower() not in blocked:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(response.body)

    def log_message(self, fmt, *args):
        print("[A] " + (fmt % args), flush=True)


class AProxy(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, client, timeout, max_request_bytes):
        super().__init__(address, ProxyHandler)
        self.client = client
        self.timeout = timeout
        self.max_request_bytes = max_request_bytes
        self.lock = threading.Lock()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:9998")
    parser.add_argument("--chunk-bytes", type=int, default=256 * 1024)
    parser.add_argument("--ack-timeout", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--max-request-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    host, port = args.listen.rsplit(":", 1)
    endpoint = ClipboardEndpoint(WindowsClipboard())
    client = ClipboardGitClient(endpoint, chunk_bytes=args.chunk_bytes,
                                 ack_timeout=args.ack_timeout, retries=args.retries)
    server = AProxy((host, int(port)), client, args.timeout, args.max_request_bytes)
    print(f"[A] Clipboard Git Tunnel listening on {args.listen}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
