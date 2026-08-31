#!/usr/bin/env python3
"""A-end HTTP proxy for the bidirectional clipboard Git tunnel."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qtc_tunnel.clipboard import ClipboardEndpoint, WindowsClipboard
from qtc_tunnel.config import load_config, side_defaults
from qtc_tunnel.focus import WindowsHSRFocus
from qtc_tunnel.git_transport import ClipboardGitClient
from qtc_tunnel.logging_utils import log_event, log_exception, safe_http_path, setup_logging


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
        started = time.monotonic()
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > tunnel.max_request_bytes:
            log_event(tunnel.logger, logging.WARNING, "http.request.rejected",
                      method=self.command, path=safe_http_path(self.path),
                      request_bytes=length, reason="request_too_large")
            self.send_error(413, "request body too large")
            return
        body = self.rfile.read(length) if length else b""
        headers = [(str(k), str(v)) for k, v in self.headers.items()]
        session = uuid.uuid4().hex
        try:
            with tunnel.lock:
                tunnel.client.endpoint.wait_write_gap()
                response = tunnel.client.request(
                    session, self.command, self.path, headers, body, tunnel.timeout)
        except TimeoutError as exc:
            log_exception(tunnel.logger, "http.request.timeout", exc,
                          session=session[:8], method=self.command,
                          path=safe_http_path(self.path), request_bytes=len(body),
                          elapsed_ms=int((time.monotonic() - started) * 1000))
            self.send_error(504, str(exc))
            return
        except Exception as exc:
            log_exception(tunnel.logger, "http.request.failed", exc,
                          session=session[:8], method=self.command,
                          path=safe_http_path(self.path), request_bytes=len(body),
                          elapsed_ms=int((time.monotonic() - started) * 1000))
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
        log_event(tunnel.logger, logging.INFO, "http.response.sent",
                  session=session[:8], method=self.command,
                  path=safe_http_path(self.path), status=response.status,
                  request_bytes=len(body), response_bytes=len(response.body),
                  elapsed_ms=int((time.monotonic() - started) * 1000))

    def log_message(self, fmt, *args):
        # HTTP access logging is emitted by _handle after the tunnel response
        # is known, so it has structured status/size/timing fields.
        return


class AProxy(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, address, client, timeout, max_request_bytes, logger):
        super().__init__(address, ProxyHandler)
        self.client = client
        self.timeout = timeout
        self.max_request_bytes = max_request_bytes
        self.logger = logger
        self.lock = threading.Lock()


def main():
    project_root = Path(__file__).resolve().parent.parent
    preparse = argparse.ArgumentParser(add_help=False)
    preparse.add_argument("--config", default=None,
                          help="config.yaml 路径（默认：<项目根>\\config.yaml 若存在）")
    known, _ = preparse.parse_known_args()
    config_path = Path(known.config) if known.config else (
        project_root / "config.yaml" if (project_root / "config.yaml").is_file() else None)
    defaults = side_defaults(load_config(config_path), "a")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(config_path) if config_path else None,
                        help="config.yaml 路径（默认：<项目根>\\config.yaml 若存在）")
    parser.add_argument("--listen", default=defaults.get("listen", "127.0.0.1:9999"))
    parser.add_argument("--chunk-bytes", type=int,
                        default=defaults.get("chunk_bytes", 800 * 1024))
    parser.add_argument("--ack-timeout", type=float,
                        default=defaults.get("ack_timeout", 5.0))
    parser.add_argument("--retries", type=int, default=defaults.get("retries", 5))
    parser.add_argument("--timeout", type=float, default=defaults.get("timeout", 300.0))
    parser.add_argument("--write-gap", type=float, default=defaults.get("write_gap", 4.0),
                        help="min seconds between A-side clipboard writes across "
                             "requests (HSR single-slot guard; ~propagation time)")
    parser.add_argument("--max-request-bytes", type=int,
                        default=defaults.get("max_request_bytes", 64 * 1024 * 1024))
    parser.add_argument("--window-keywords",
                        default=defaults.get("window_keywords", ""),
                        help="comma-separated HSRClient window title keywords")
    parser.add_argument("--log-level", default=defaults.get("log_level", "INFO"),
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--log-dir", default=defaults.get("log_dir", ""),
                        help="log directory (default: <project>\\logs)")
    args = parser.parse_args()
    logger, log_path = setup_logging(
        side="A", project_root=project_root, log_level=args.log_level,
        log_dir=Path(args.log_dir) if args.log_dir else None)
    log_event(logger, logging.INFO, "process.start", version="0.1",
              listen=args.listen, chunk_bytes=args.chunk_bytes,
              ack_timeout_s=args.ack_timeout, retries=args.retries,
              timeout_s=args.timeout, write_gap_s=args.write_gap,
              log_path=str(log_path))
    host, port = args.listen.rsplit(":", 1)
    keywords = [item.strip() for item in args.window_keywords.split(",") if item.strip()]
    focus = WindowsHSRFocus(keywords=keywords or None, logger=logger)
    endpoint = ClipboardEndpoint(WindowsClipboard(logger=logger), focus=focus,
                                 min_write_gap=args.write_gap, logger=logger)
    client = ClipboardGitClient(endpoint, chunk_bytes=args.chunk_bytes,
                                ack_timeout=args.ack_timeout, retries=args.retries,
                                logger=logger)
    # Do not expose the HTTP listener until B plus both HSR clipboard
    # directions are actually ready. This replaces the manual ritual of
    # starting B, waiting for several heartbeat lines, then starting A.
    log_event(logger, logging.INFO, "listener.waiting_for_peer",
              address=args.listen)
    try:
        client.wait_for_peer(uuid.uuid4().hex)
    except KeyboardInterrupt:
        log_event(logger, logging.INFO, "process.stop",
                  reason="keyboard_interrupt_while_waiting_for_peer")
        return
    server = AProxy((host, int(port)), client, args.timeout, args.max_request_bytes, logger)
    log_event(logger, logging.INFO, "listener.ready", address=args.listen,
              log_path=str(log_path))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
