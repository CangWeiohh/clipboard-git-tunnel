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
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qtc_tunnel.clipboard import WindowsClipboard


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("writer", "reader"), required=True)
    parser.add_argument("--sizes", default="1024,65536,262144,524288,1048576")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    clipboard = WindowsClipboard()
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
                        print(f"size={size} round={round_no} status=ok elapsed_ms={(time.monotonic()-started)*1000:.1f}", flush=True)
                        break
                    time.sleep(0.05)
                else:
                    print(f"size={size} round={round_no} status=timeout", flush=True)
        return 0

    print("Reader ready; start the writer in the other endpoint.", flush=True)
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
                    print(f"size={size} round={round_no} status={status}", flush=True)
                    clipboard.write(prefix + "ACK:" + ":".join(fields[1:4]))
                except ValueError:
                    print("status=bad malformed", flush=True)
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
