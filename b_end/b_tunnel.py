#!/usr/bin/env python3
"""B-end Git forwarder for the bidirectional clipboard Git tunnel."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qtc_tunnel.clipboard import ClipboardEndpoint, WindowsClipboard
from qtc_tunnel.git_transport import ClipboardGitServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, help="internal Git host:port")
    parser.add_argument("--chunk-bytes", type=int, default=256 * 1024)
    parser.add_argument("--ack-timeout", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    target_host, target_port = args.target.rsplit(":", 1)
    endpoint = ClipboardEndpoint(WindowsClipboard())
    server = ClipboardGitServer(endpoint, chunk_bytes=args.chunk_bytes,
                                ack_timeout=args.ack_timeout, retries=args.retries,
                                target_host=target_host, target_port=int(target_port),
                                upstream_timeout=args.timeout)
    print(f"[B] Clipboard Git Tunnel forwarding to {args.target}", flush=True)
    while True:
        try:
            server.serve_one(args.timeout)
            print("[B] request completed", flush=True)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            import traceback
            print(f"[B] request failed: {exc!r}", flush=True)
            traceback.print_exc()
            time.sleep(0.5)


if __name__ == "__main__":
    main()
