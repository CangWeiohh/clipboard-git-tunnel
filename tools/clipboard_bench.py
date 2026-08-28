#!/usr/bin/env python3
"""Measure programmatic HSR clipboard capacity/latency on Windows.

Run this utility independently on A and B after bidirectional clipboard is
known to work. It intentionally prints only sizes and timing, never payloads.
The two processes share a clipboard and alternate writer/reader roles via the
``--role`` argument.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qtc_tunnel.clipboard import WindowsClipboard
from qtc_tunnel.logging_utils import log_event, setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("writer", "reader"), required=True)
    parser.add_argument("--sizes", default="1024,65536,262144,524288,1048576")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--log-level", default="INFO",
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--log-dir", default="",
                        help="log directory (default: <project>\\logs)")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parent.parent
    logger, log_path = setup_logging(
        side="BENCH", project_root=project_root, log_level=args.log_level,
        log_dir=Path(args.log_dir) if args.log_dir else None)
    log_event(logger, logging.INFO, "benchmark.start", role=args.role,
              sizes=args.sizes, rounds=args.rounds, timeout_s=args.timeout,
              log_path=str(log_path))
    clipboard = WindowsClipboard(logger=logger)
    sizes = [int(value) for value in args.sizes.split(",") if value.strip()]
    prefix = "QTC-BENCH:"

    if args.role == "writer":
        for size in sizes:
            for round_no in range(args.rounds):
                payload = os.urandom(size)
                marker = f"{prefix}{size}:{round_no}:" + hashlib.sha256(payload).hexdigest()
                expected_ack = prefix + "ACK:" + ":".join(marker.split(":")[1:])
                started = time.monotonic()
                clipboard.write(marker + ":" + payload.hex())
                deadline = started + args.timeout
                while time.monotonic() < deadline:
                    if clipboard.read() == expected_ack:
                        log_event(logger, logging.INFO, "benchmark.round",
                                  size=int(size), round=round_no, status="ok",
                                  elapsed_ms=round((time.monotonic()-started)*1000, 1))
                        break
                    time.sleep(0.05)
                else:
                    log_event(logger, logging.WARNING, "benchmark.round",
                              size=int(size), round=round_no, status="timeout",
                              elapsed_ms=round((time.monotonic()-started)*1000, 1))
        return 0

    log_event(logger, logging.INFO, "benchmark.reader_ready")
    while True:
        text = clipboard.read()
        if text.startswith(prefix) and not text.startswith(prefix + "ACK:"):
            fields = text.split(":", 4)
            if len(fields) == 5:
                size, round_no, expected, encoded = fields[1:]
                try:
                    payload = bytes.fromhex(encoded)
                    actual = hashlib.sha256(payload).hexdigest()
                    status = "ok" if len(payload) == int(size) and actual == expected else "bad"
                    log_event(logger, logging.INFO, "benchmark.round",
                              size=int(size), round=int(round_no), status=status)
                    clipboard.write(prefix + "ACK:" + ":".join(fields[1:4]))
                except ValueError:
                    log_event(logger, logging.WARNING, "benchmark.round",
                              status="malformed")
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
