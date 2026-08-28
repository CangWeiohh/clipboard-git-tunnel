#!/usr/bin/env python3
"""B-end Git forwarder for the bidirectional clipboard Git tunnel."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qtc_tunnel.clipboard import ClipboardEndpoint, WindowsClipboard
from qtc_tunnel.git_transport import ClipboardGitServer
from qtc_tunnel.logging_utils import log_event, log_exception, setup_logging


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="internal Git host:port")
    parser.add_argument("--chunk-bytes", type=int, default=256 * 1024)
    parser.add_argument("--ack-timeout", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--log-dir", default="",
                        help="log directory (default: <project>\\logs)")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent.parent
    logger, log_path = setup_logging(
        side="B", project_root=project_root, log_level=args.log_level,
        log_dir=Path(args.log_dir) if args.log_dir else None)
    log_event(logger, logging.INFO, "process.start", version="0.1",
              target=args.target, chunk_bytes=args.chunk_bytes,
              ack_timeout_s=args.ack_timeout, retries=args.retries,
              timeout_s=args.timeout, log_path=str(log_path))
    target_host, target_port = args.target.rsplit(":", 1)
    endpoint = ClipboardEndpoint(WindowsClipboard(logger=logger), logger=logger)
    server = ClipboardGitServer(endpoint, chunk_bytes=args.chunk_bytes,
                                ack_timeout=args.ack_timeout, retries=args.retries,
                                target_host=target_host, target_port=int(target_port),
                                upstream_timeout=args.timeout, logger=logger)
    log_event(logger, logging.INFO, "listener.ready", target=args.target,
              log_path=str(log_path))
    while True:
        try:
            server.serve_one(args.timeout)
        except KeyboardInterrupt:
            log_event(logger, logging.INFO, "process.stop", reason="keyboard_interrupt")
            break
        except Exception as exc:
            log_exception(logger, "process.request_failed", exc)
            time.sleep(0.5)


if __name__ == "__main__":
    main()
