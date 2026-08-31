#!/usr/bin/env python3
"""B-end Git forwarder for the bidirectional clipboard Git tunnel."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qtc_tunnel.clipboard import ClipboardEndpoint, WindowsClipboard
from qtc_tunnel.config import load_config, side_defaults
from qtc_tunnel.git_transport import ClipboardGitServer
from qtc_tunnel.logging_utils import log_event, log_exception, setup_logging


def main():
    project_root = Path(__file__).resolve().parent.parent
    preparse = argparse.ArgumentParser(add_help=False)
    preparse.add_argument("--config", default=None,
                          help="config.yaml 路径（默认：<项目根>\\config.yaml 若存在）")
    known, _ = preparse.parse_known_args()
    config_path = Path(known.config) if known.config else (
        project_root / "config.yaml" if (project_root / "config.yaml").is_file() else None)
    defaults = side_defaults(load_config(config_path), "b")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(config_path) if config_path else None,
                        help="config.yaml 路径（默认：<项目根>\\config.yaml 若存在）")
    parser.add_argument("--target", default=defaults.get("target"),
                        help="internal Git host:port (or set b_target in config.yaml)")
    parser.add_argument("--chunk-bytes", type=int,
                        default=defaults.get("chunk_bytes", 800 * 1024))
    parser.add_argument("--ack-timeout", type=float,
                        default=defaults.get("ack_timeout", 5.0))
    parser.add_argument("--retries", type=int, default=defaults.get("retries", 5))
    parser.add_argument("--timeout", type=float, default=defaults.get("timeout", 300.0))
    parser.add_argument("--upstream-header-timeout", type=float,
                        default=defaults.get("upstream_header_timeout", 30.0),
                        help="seconds to wait for upstream connect/response headers")
    parser.add_argument("--upstream-idle-timeout", type=float,
                        default=defaults.get("upstream_idle_timeout", 2.0),
                        help="max seconds without upstream response body data")
    parser.add_argument("--log-level", default=defaults.get("log_level", "INFO"),
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--log-dir", default=defaults.get("log_dir", ""),
                        help="log directory (default: <project>\\logs)")
    args = parser.parse_args()
    if not args.target:
        parser.error("--target is required (or set b_target in config.yaml)")
    logger, log_path = setup_logging(
        side="B", project_root=project_root, log_level=args.log_level,
        log_dir=Path(args.log_dir) if args.log_dir else None)
    log_event(logger, logging.INFO, "process.start", version="0.1",
              target=args.target, chunk_bytes=args.chunk_bytes,
              ack_timeout_s=args.ack_timeout, retries=args.retries,
              timeout_s=args.timeout,
              upstream_header_timeout_s=args.upstream_header_timeout,
              upstream_idle_timeout_s=args.upstream_idle_timeout,
              log_path=str(log_path))
    target_host, target_port = args.target.rsplit(":", 1)
    # NOTE: no focus controller on B. The HSR client (and its foreground
    # requirement) exists only in the A-end environment; the cloud desktop B
    # has no HSR window by design, so scanning for one would only produce
    # misleading focus.hsr_not_found noise (round 10 lesson).
    endpoint = ClipboardEndpoint(WindowsClipboard(logger=logger), logger=logger)
    server = ClipboardGitServer(endpoint, chunk_bytes=args.chunk_bytes,
                                ack_timeout=args.ack_timeout, retries=args.retries,
                                target_host=target_host, target_port=int(target_port),
                                upstream_timeout=args.timeout,
                                 upstream_header_timeout=args.upstream_header_timeout,
                                 upstream_idle_timeout=args.upstream_idle_timeout,
                                 logger=logger)
    log_event(logger, logging.INFO, "listener.ready", target=args.target,
              log_path=str(log_path))
    # Heartbeat: with a pure-clipboard transport, "no frames received" is
    # indistinguishable from "B wedged in an OpenClipboard loop" from the
    # logs alone. A periodic liveness line makes the distinction. Beat
    # immediately so short sessions always carry at least one heartbeat,
    # then every 15s.
    started_at = time.monotonic()

    def _heartbeat():
        while True:
            log_event(logger, logging.INFO, "process.heartbeat",
                      uptime_s=int(time.monotonic() - started_at))
            time.sleep(15.0)

    threading.Thread(target=_heartbeat, daemon=True, name="b-heartbeat").start()
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
