from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qtc_tunnel.clipboard import ClipboardEndpoint, MemoryClipboard
from qtc_tunnel.git_transport import ClipboardGitClient, ClipboardGitServer
from qtc_tunnel.protocol import Frame, ProtocolError, digest, make_frame
from qtc_tunnel.transfer import frame_chunks, reassemble


class ProtocolTests(unittest.TestCase):
    def test_round_trip_and_checksum(self):
        frame = make_frame("resp_data", "session", b"hello", seq=1, total=2)
        self.assertEqual(Frame.from_text(frame.to_text()), frame)

    def test_tamper_is_rejected(self):
        frame = make_frame("resp_data", "session", b"hello")
        text = frame.to_text()
        text = text[:-2] + ("A" if text[-2] != "A" else "B") + text[-1]
        with self.assertRaises(ProtocolError):
            Frame.from_text(text)

    def test_chunk_reassembly(self):
        frames = list(frame_chunks("req_data", "s", b"0123456789", 3))
        self.assertEqual(reassemble(frames), b"0123456789")
        self.assertEqual(frames[-1].total, 4)

    def test_write_gap_sleeps_when_required(self):
        import time as _time
        endpoint = ClipboardEndpoint(MemoryClipboard(), min_write_gap=0.2)
        endpoint.write_frame(make_frame("ack", "s1"))
        started = _time.monotonic()
        endpoint.wait_write_gap()
        self.assertGreaterEqual(_time.monotonic() - started, 0.18)

    def test_write_gap_no_sleep_when_elapsed(self):
        import time as _time
        endpoint = ClipboardEndpoint(MemoryClipboard(), min_write_gap=0.01)
        endpoint.write_frame(make_frame("ack", "s1"))
        _time.sleep(0.05)
        started = _time.monotonic()
        endpoint.wait_write_gap()
        self.assertLess(_time.monotonic() - started, 0.01)


class _GitHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(b"echo:" + body)

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"git-info")

    def log_message(self, *_args):
        pass


class TransportTests(unittest.TestCase):
    def test_bidirectional_http_round_trip(self):
        clipboard = MemoryClipboard()
        a_endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        b_endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _GitHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        host, port = upstream.server_address
        server = ClipboardGitServer(
            b_endpoint, chunk_bytes=3, ack_timeout=1, retries=3,
            target_host=host, target_port=port, upstream_timeout=3)
        server_thread = threading.Thread(target=server.serve_one, kwargs={"timeout": 10}, daemon=True)
        server_thread.start()
        client = ClipboardGitClient(a_endpoint, chunk_bytes=3, ack_timeout=1, retries=3)
        response = client.request("session-1", "POST", "/repo.git/git-upload-pack",
                                 [("Content-Type", "application/octet-stream")],
                                 b"hello clipboard", 10)
        server_thread.join(3)
        upstream.shutdown()
        upstream.server_close()
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"echo:hello clipboard")


if __name__ == "__main__":
    unittest.main()
