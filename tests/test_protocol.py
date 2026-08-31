from __future__ import annotations

import socketserver
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qtc_tunnel.clipboard import ClipboardEndpoint, MemoryClipboard
from qtc_tunnel.git_transport import (ClipboardGitClient, ClipboardGitServer,
                                      StaleRequest, ensure_request_fresh,
                                      pack_request, request_created_at,
                                      unpack_request, pack_response,
                                      unpack_response)
from qtc_tunnel.protocol import Frame, ProtocolError, digest, make_frame
from qtc_tunnel.transfer import frame_chunks, reassemble


class ClientReceiveSetTests(unittest.TestCase):
    def test_skips_retransmitted_frame(self):
        clipboard = MemoryClipboard()
        writer = ClipboardEndpoint(clipboard, poll_interval=0.001)
        client = ClipboardGitClient(
            ClipboardEndpoint(clipboard, poll_interval=0.001),
            chunk_bytes=3, ack_timeout=1, retries=3)
        session = "dup-sess"
        f0 = make_frame("resp_data", session, b"ab", seq=0, total=2)
        f1 = make_frame("resp_data", session, b"cd", seq=1, total=2)

        def feed():
            time.sleep(0.05)
            writer.write_frame(f0)
            time.sleep(0.05)
            writer.write_frame(replace(f0, retry=1))  # ACK-loss retransmission
            time.sleep(0.05)
            writer.write_frame(f1)

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        frames = client._receive_set("resp_data", session, 3)
        thread.join(1)
        self.assertEqual(reassemble(frames), b"abcd")

    def test_out_of_order_rejected(self):
        clipboard = MemoryClipboard()
        writer = ClipboardEndpoint(clipboard, poll_interval=0.001)
        client = ClipboardGitClient(
            ClipboardEndpoint(clipboard, poll_interval=0.001),
            chunk_bytes=3, ack_timeout=1, retries=3)
        session = "ooo-sess"
        f1 = make_frame("resp_data", session, b"ab", seq=1, total=2)

        def feed():
            time.sleep(0.05)
            writer.write_frame(f1)

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        with self.assertRaises(ProtocolError):
            client._receive_set("resp_data", session, 3)
        thread.join(1)


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


class PackUnpackTests(unittest.TestCase):
    def test_request_round_trip(self):
        packed = pack_request("POST", "/repo.git/git-upload-pack",
                              [("Content-Type", "application/octet-stream"),
                               ("X-Foo", "bar")],
                              b"hello body")
        method, path, headers, body = unpack_request(packed)
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/repo.git/git-upload-pack")
        self.assertEqual(headers, [("Content-Type", "application/octet-stream"),
                                   ("X-Foo", "bar")])
        self.assertEqual(body, b"hello body")

    def test_request_empty_body(self):
        packed = pack_request("GET", "/info/refs?service=git-upload-pack", [], b"")
        method, path, headers, body = unpack_request(packed)
        self.assertEqual((method, path, body), ("GET", "/info/refs?service=git-upload-pack", b""))
        self.assertEqual(headers, [])

    def test_response_round_trip_and_digest(self):
        body = b"packfile payload" * 100
        packed = pack_response(200, [("Content-Type", "application/octet-stream")], body)
        status, headers, unpacked = unpack_response(packed)
        self.assertEqual(status, 200)
        self.assertEqual(headers, [("Content-Type", "application/octet-stream")])
        self.assertEqual(unpacked, body)

    def test_response_digest_mismatch_rejected(self):
        packed = bytearray(pack_response(200, [], b"original"))
        # Corrupt one body byte in place: the SHA-256 check must fail.
        packed[-1] ^= 0xFF
        with self.assertRaises(ProtocolError):
            unpack_response(bytes(packed))

    def test_truncated_payload_rejected(self):
        with self.assertRaises(ProtocolError):
            unpack_request(b"\x00\x00")
        with self.assertRaises(ProtocolError):
            unpack_response(b"\x00\x00")


class StaleAndBaselineTests(unittest.TestCase):
    def test_created_at_round_trip(self):
        packed = pack_request("GET", "/repo.git/info/refs", [], b"",
                              created_at=12345.678)
        self.assertAlmostEqual(request_created_at(packed), 12345.678, places=3)

    def test_legacy_request_without_timestamp_is_fresh(self):
        packed = pack_request("GET", "/x", [], b"")
        ensure_request_fresh(packed)  # must not raise

    def test_recent_request_is_fresh(self):
        packed = pack_request("GET", "/x", [], b"", created_at=time.time())
        ensure_request_fresh(packed)

    def test_old_request_is_stale(self):
        packed = pack_request("GET", "/x", [], b"",
                              created_at=time.time() - 90.0)
        with self.assertRaises(StaleRequest):
            ensure_request_fresh(packed)

    def test_startup_ignores_preloaded_clipboard_request(self):
        # A request left in the clipboard by a previous run must be treated as
        # the receive baseline, not as a brand-new request after B starts.
        preloaded = make_frame("req_single", "previous-run",
                               pack_request("GET", "/stale", [], b""))
        clipboard = MemoryClipboard(initial=preloaded.to_text())
        endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        server = ClipboardGitServer(
            endpoint, chunk_bytes=819200, ack_timeout=1, retries=3,
            target_host="127.0.0.1", target_port=1, upstream_timeout=1)
        self.assertFalse(server.serve_one(timeout=0.05))
        # A changed value after startup is still a valid new frame.
        new_frame = make_frame("req_single", "new-session",
                               pack_request("GET", "/new", [], b""))
        clipboard.write(new_frame.to_text())
        frame = endpoint.wait_frame(
            lambda item: item.kind in {"req_meta", "req_single"}, 1.0)
        self.assertEqual(frame.session, "new-session")

    def test_unrelated_frame_is_stashed_for_later_phase(self):
        # A frame that does not match the active predicate must not be dropped;
        # a later wait with a matching predicate returns it first.
        clipboard = MemoryClipboard()
        endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        other = make_frame("resp_single", "zzz", b"ignored")
        clipboard.write(other.to_text())
        with self.assertRaises(TimeoutError):
            endpoint.wait_frame(lambda item: item.kind == "ack", 0.05)
        # The stashed frame must now be returned by a matching predicate.
        recovered = endpoint.wait_frame(lambda item: item.session == "zzz", 1.0)
        self.assertEqual(recovered.kind, "resp_single")
        self.assertEqual(recovered.session, "zzz")

    def test_single_frame_barrier_timeout_returns_false(self):
        # B ACKs a req_single but the client never sends resp_begin. serve_one
        # must abandon the dead session (return False) within the barrier
        # window instead of holding the loop for the full client timeout.
        clipboard = MemoryClipboard()
        b_endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        server = ClipboardGitServer(
            b_endpoint, chunk_bytes=819200, ack_timeout=1, retries=3,
            target_host="127.0.0.1", target_port=1, upstream_timeout=1)
        req = make_frame("req_single", "barrier-sess",
                         pack_request("GET", "/x", [], b"", created_at=time.time()))
        clipboard.write(req.to_text())
        # Serve in a thread; serve_one should return after ~min(60, timeout).
        result: list[bool | Exception] = []

        def run():
            try:
                result.append(server.serve_one(timeout=5))
            except Exception as exc:  # pragma: no cover - should not raise
                result.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        started = time.monotonic()
        thread.start()
        # Fire a resp_begin for the wrong session to verify B does not
        # mistake it for a match.
        time.sleep(0.05)
        clipboard.write(make_frame("resp_begin", "other-sess").to_text())
        thread.join(15)
        elapsed = time.monotonic() - started
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertLess(elapsed, 10.0)


class TransportTests(unittest.TestCase):
    def test_server_idle_timeout_is_normal(self):
        endpoint = ClipboardEndpoint(MemoryClipboard(), poll_interval=0.001)
        server = ClipboardGitServer(
            endpoint, chunk_bytes=3, ack_timeout=1, retries=3,
            target_host="127.0.0.1", target_port=1, upstream_timeout=1)
        self.assertFalse(server.serve_one(timeout=0.02))

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

    def test_upstream_request_forces_connection_close(self):
        # The tunnel buffers the full upstream response before forwarding it,
        # so B must ask the Git server to close the connection. Otherwise an
        # HTTP/1.1 reply without Content-Length keeps response.read() blocked.
        clipboard = MemoryClipboard()
        a_endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        b_endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        captured: dict[str, str] = {}

        class _CaptureHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                captured["connection"] = self.headers.get("Connection", "")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        host, port = upstream.server_address
        server = ClipboardGitServer(
            b_endpoint, chunk_bytes=1024 * 1024, ack_timeout=1, retries=3,
            target_host=host, target_port=port, upstream_timeout=3)
        server_thread = threading.Thread(
            target=server.serve_one, kwargs={"timeout": 10}, daemon=True)
        server_thread.start()
        client = ClipboardGitClient(a_endpoint, chunk_bytes=1024 * 1024,
                                    ack_timeout=1, retries=3)
        response = client.request("sess-close", "GET", "/info/refs", [], b"", 10)
        server_thread.join(3)
        upstream.shutdown()
        upstream.server_close()
        self.assertEqual(response.status, 200)
        self.assertEqual(captured.get("connection"), "close")

    def test_upstream_response_headers_timeout_is_bounded(self):
        class _HeaderStall(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.recv(4096)
                time.sleep(1.0)  # accept request, never send an HTTP response

        upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _HeaderStall)
        upstream.daemon_threads = True
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        host, port = upstream.server_address
        server = ClipboardGitServer(
            ClipboardEndpoint(MemoryClipboard()),
            chunk_bytes=1024, ack_timeout=1, retries=1,
            target_host=host, target_port=port, upstream_timeout=2,
            upstream_header_timeout=0.1, upstream_idle_timeout=0.1)
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "response headers timeout"):
                server._forward("GET", "/stall", [], b"")
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_unknown_length_keepalive_uses_idle_boundary(self):
        class _NoLengthKeepAlive(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(401)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Unauthorized")
                self.wfile.flush()
                # Deliberately ignore the client's Connection: close request.
                self.close_connection = False

            def log_message(self, *_args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _NoLengthKeepAlive)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        host, port = upstream.server_address
        server = ClipboardGitServer(
            ClipboardEndpoint(MemoryClipboard()),
            chunk_bytes=1024, ack_timeout=1, retries=1,
            target_host=host, target_port=port, upstream_timeout=2,
            upstream_header_timeout=0.5, upstream_idle_timeout=0.1)
        started = time.monotonic()
        try:
            response = server._forward("GET", "/auth", [], b"")
            self.assertEqual(response.status, 401)
            self.assertEqual(response.body, b"Unauthorized")
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_declared_length_truncation_is_not_silently_accepted(self):
        class _TruncatedKeepAlive(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "20")
                self.end_headers()
                self.wfile.write(b"short")
                self.wfile.flush()
                self.close_connection = False

            def log_message(self, *_args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _TruncatedKeepAlive)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        host, port = upstream.server_address
        server = ClipboardGitServer(
            ClipboardEndpoint(MemoryClipboard()),
            chunk_bytes=1024, ack_timeout=1, retries=1,
            target_host=host, target_port=port, upstream_timeout=2,
            upstream_header_timeout=0.5, upstream_idle_timeout=0.1)
        try:
            with self.assertRaisesRegex(TimeoutError, "body idle timeout"):
                server._forward("GET", "/truncated", [], b"")
        finally:
            upstream.shutdown()
            upstream.server_close()

    def test_single_frame_round_trip(self):
        """Small request+response must use the single-frame path (req_single /
        resp_single) and yield the same HTTP result as multi-frame."""
        clipboard = MemoryClipboard()
        a_endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        b_endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _GitHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        host, port = upstream.server_address
        server = ClipboardGitServer(
            b_endpoint, chunk_bytes=1024 * 1024, ack_timeout=1, retries=3,
            target_host=host, target_port=port, upstream_timeout=3)
        server_thread = threading.Thread(target=server.serve_one, kwargs={"timeout": 10}, daemon=True)
        server_thread.start()
        client = ClipboardGitClient(a_endpoint, chunk_bytes=1024 * 1024,
                                    ack_timeout=1, retries=3)
        response = client.request("session-single", "POST",
                                  "/repo.git/git-upload-pack",
                                  [("Content-Type", "application/octet-stream")],
                                  b"hello single frame", 10)
        server_thread.join(3)
        upstream.shutdown()
        upstream.server_close()
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"echo:hello single frame")
        terminal = Frame.from_text(clipboard.read())
        self.assertIsNotNone(terminal)
        self.assertEqual(terminal.kind, "resp_single")

    def test_large_response_uses_multi_frame_with_big_chunk(self):
        """A response larger than the chunk must still fall back to
        resp_data multi-frame even when the request used single-frame."""
        clipboard = MemoryClipboard()
        a_endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)
        b_endpoint = ClipboardEndpoint(clipboard, poll_interval=0.001)

        class _BigHandler(_GitHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"B" * 2000)  # exceeds 1000-byte chunk

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _BigHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        host, port = upstream.server_address
        server = ClipboardGitServer(
            b_endpoint, chunk_bytes=1000, ack_timeout=1, retries=3,
            target_host=host, target_port=port, upstream_timeout=3)
        server_thread = threading.Thread(target=server.serve_one, kwargs={"timeout": 10}, daemon=True)
        server_thread.start()
        client = ClipboardGitClient(a_endpoint, chunk_bytes=1000,
                                    ack_timeout=1, retries=3)
        response = client.request("session-big", "GET", "/big.bin", [], b"", 10)
        server_thread.join(3)
        upstream.shutdown()
        upstream.server_close()
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b"B" * 2000)


if __name__ == "__main__":
    unittest.main()
