"""Bidirectional clipboard transport for Git Smart HTTP request/response bytes.

Protocol overview
-----------------
The tunnel carries Git Smart HTTP requests/responses over the HSR clipboard
channel.  Two transport modes are used, chosen automatically per request:

**Single-frame mode** (``req_single`` / ``resp_single``)
    When the packed request (meta JSON + body) or packed response (meta JSON
    + body + SHA-256) fits in one clipboard frame, it is sent as a single
    ``req_single`` or ``resp_single`` frame.  This replaces the four-frame
    request handshake (``req_meta`` + ``req_data`` + ``req_end`` +
    ``req_commit``) or the multi-frame response sequence (``resp_meta`` +
    ``resp_data`` + ``resp_end``), saving several round-trips.  A typical
    Git fetch (info/refs GET, small upload-pack POST) fits entirely in one
    frame, reducing latency from ~28 s to ~10 s.

**Multi-frame mode** (legacy kinds, unchanged)
    Large requests or responses that exceed ``chunk_bytes`` fall back to the
    original multi-frame protocol.  The 800 KiB default chunk size means
    responses up to ~800 KiB use single-frame mode; only large clones need
    multi-frame.

Both modes share the ``resp_begin`` barrier: after B ACKs the request, A
writes ``resp_begin`` (un-ACKed) so B knows A is ready for the response and
the two B-side writes (ACK + response) are separated by a full propagation
round-trip.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass
from typing import Any

from .clipboard import ClipboardEndpoint
from .logging_utils import log_event, safe_http_path
from .protocol import Frame, ProtocolError, digest, json_payload, make_frame, parse_json_payload
from .transfer import frame_chunks, reassemble


# ---------------------------------------------------------------------------
# Packed single-frame payload helpers
# ---------------------------------------------------------------------------
# Binary layout: [4-byte big-endian meta length] [meta JSON] [body bytes]
# The entire blob is base64-encoded by Frame.to_text(), so the body is NOT
# double-encoded — it rides inside the frame payload alongside the meta JSON.

def pack_request(method: str, path: str,
                 headers: list[tuple[str, str]], body: bytes) -> bytes:
    """Pack a complete HTTP request into a single binary payload."""
    meta = json_payload({
        "method": method,
        "path": path,
        "headers": [[name, value] for name, value in headers],
    })
    return struct.pack(">I", len(meta)) + meta + body


def unpack_request(payload: bytes) -> tuple[str, str, list[tuple[str, str]], bytes]:
    """Unpack a request from a single ``req_single`` payload."""
    if len(payload) < 4:
        raise ProtocolError("request payload too short")
    meta_len = struct.unpack(">I", payload[:4])[0]
    if len(payload) < 4 + meta_len:
        raise ProtocolError("request payload truncated")
    meta = parse_json_payload(payload[4:4 + meta_len])
    body = payload[4 + meta_len:]
    method = str(meta.get("method", "GET"))
    path = str(meta.get("path", "/"))
    raw_headers = meta.get("headers", [])
    headers = [
        (str(pair[0]), str(pair[1]))
        for pair in raw_headers
        if isinstance(pair, list) and len(pair) == 2
    ]
    return method, path, headers, body


def pack_response(status: int, headers: list[tuple[str, str]],
                  body: bytes) -> bytes:
    """Pack a complete HTTP response (with SHA-256) into one binary payload."""
    meta = json_payload({
        "status": status,
        "headers": headers,
        "sha256": digest(body),
    })
    return struct.pack(">I", len(meta)) + meta + body


def unpack_response(payload: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    """Unpack a response from a single ``resp_single`` payload."""
    if len(payload) < 4:
        raise ProtocolError("response payload too short")
    meta_len = struct.unpack(">I", payload[:4])[0]
    if len(payload) < 4 + meta_len:
        raise ProtocolError("response payload truncated")
    meta = parse_json_payload(payload[4:4 + meta_len])
    body = payload[4 + meta_len:]
    expected = meta.get("sha256")
    if expected and digest(body) != expected:
        raise ProtocolError("response digest mismatch")
    status = int(meta.get("status", 502))
    raw_headers = meta.get("headers", [])
    headers = [(str(k), str(v)) for k, v in raw_headers]
    return status, headers, body


@dataclass
class HttpMessage:
    status: int
    headers: list[tuple[str, str]]
    body: bytes


class ClipboardGitClient:
    def __init__(self, endpoint: ClipboardEndpoint, *, chunk_bytes: int,
                 ack_timeout: float, retries: int,
                 logger: logging.Logger | None = None) -> None:
        self.endpoint = endpoint
        self.chunk_bytes = chunk_bytes
        self.ack_timeout = ack_timeout
        self.retries = retries
        self.logger = logger or logging.getLogger("clipboard_git_tunnel.client")

    def _send_frames(self, kind: str, session: str, payload: bytes) -> None:
        for frame in frame_chunks(kind, session, payload, self.chunk_bytes):
            self.endpoint.send_and_wait_ack(frame, self.ack_timeout, self.retries)

    def _receive_set(self, kind: str, session: str, timeout: float) -> list[Frame]:
        frames: list[Frame] = []
        while True:
            try:
                frame = self.endpoint.wait_frame(
                    lambda item: item.session == session and item.kind in {kind, "error"},
                    timeout,
                )
            except TimeoutError as exc:
                raise TimeoutError(
                    f"clipboard receive timeout kind={kind} session={session[:8]}") from exc
            self.endpoint.acknowledge(frame)
            if frame.kind == "error":
                error = parse_json_payload(frame.payload)
                raise RuntimeError(error.get("message", "remote tunnel error"))
            if frame.seq == len(frames) - 1 and frames:
                # ACK loss makes the peer retransmit the last frame;
                # re-acknowledge but do not append a duplicate.
                continue
            if frame.seq != len(frames):
                raise ProtocolError(
                    f"clipboard frames out of order: expected seq={len(frames)}, got {frame.seq}")
            frames.append(frame)
            if frame.seq == frame.total - 1:
                return frames

    def _receive_set_from(self, first: Frame, kind: str, timeout: float) -> list[Frame]:
        """Continue receiving a frame set given an already-received first frame.

        Unlike ``_receive_set``, the first frame is supplied by the caller
        (it was fetched alongside ``resp_single`` in the same ``wait_frame``
        call).  This method ACKs the first frame, then receives the rest.
        """
        if first.kind != kind:
            raise ProtocolError(f"expected {kind}, got {first.kind}")
        frames = [first]
        self.endpoint.acknowledge(first)
        current = first
        while current.seq < current.total - 1:
            frame = self.endpoint.wait_frame(
                lambda item: item.session == first.session
                and item.kind in {kind, "error"},
                timeout,
            )
            if frame.kind == "error":
                self.endpoint.acknowledge(frame)
                error = parse_json_payload(frame.payload)
                raise RuntimeError(error.get("message", "remote tunnel error"))
            if frame.seq == frames[-1].seq:
                # ACK-loss retransmission of the last frame: re-ACK, skip.
                self.endpoint.acknowledge(frame)
                continue
            if frame.seq != frames[-1].seq + 1:
                raise ProtocolError(
                    f"clipboard frames out of order: expected seq={frames[-1].seq + 1}, "
                    f"got {frame.seq}")
            self.endpoint.acknowledge(frame)
            frames.append(frame)
            current = frame
        return frames

    def request(self, session: str, method: str, path: str,
                headers: list[tuple[str, str]], body: bytes,
                timeout: float) -> HttpMessage:
        started = time.monotonic()
        log_event(self.logger, logging.INFO, "http.request.begin",
                  session=session[:8], method=method, path=safe_http_path(path),
                  request_bytes=len(body))

        # ---- Request phase: single-frame vs multi-frame ----
        packed_req = pack_request(method, path, headers, body)
        if len(packed_req) <= self.chunk_bytes:
            # Single-frame request: one write + one ACK replaces
            # req_meta + req_data + req_end + req_commit (4 round-trips → 1).
            single = make_frame("req_single", session, packed_req)
            self.endpoint.send_and_wait_ack(single, self.ack_timeout, self.retries)
        else:
            # Multi-frame request (large body): existing protocol.
            meta = json_payload({
                "method": method,
                "path": path,
                "headers": [[name, value] for name, value in headers],
            })
            self._send_frames("req_meta", session, meta)
            self._send_frames("req_data", session, body)
            end = Frame("req_end", session, 0, 1, b"", None)
            self.endpoint.send_and_wait_ack(end, self.ack_timeout, self.retries)
            # Barrier for the final request ACK. HSR clipboard propagation is
            # slower than the local B-end code path; without the commit + begin
            # handshake below, B's req_commit ACK could be overwritten by the
            # response before A observes it (single-slot clipboard + event-driven
            # sync: consecutive same-side writes within the propagation window
            # drop the earlier one).
            commit = Frame("req_commit", session, 0, 1, b"", None)
            self.endpoint.send_and_wait_ack(commit, self.ack_timeout, self.retries)

        # ---- resp_begin barrier (always needed) ----
        # Announce readiness to receive the response. B must observe this
        # frame before writing the response. It is deliberately NOT acknowledged:
        # an ACK here would be a second B-side write racing the response. If the
        # response does not start arriving, resend the marker and keep waiting.
        begin_deadline = time.monotonic() + timeout
        begin_attempt = 0
        while True:
            begin_attempt += 1
            self.endpoint.write_frame(
                Frame("resp_begin", session, 0, 1, b"", None, retry=begin_attempt - 1))
            try:
                first = self.endpoint.wait_frame(
                    lambda item: item.session == session
                    and item.kind in {"resp_single", "resp_meta", "error"},
                    self.ack_timeout,
                )
                break
            except TimeoutError:
                if time.monotonic() >= begin_deadline:
                    raise TimeoutError(
                        f"clipboard resp_begin timeout session={session[:8]} "
                        f"attempts={begin_attempt}") from None
                # keep waiting; resend the begin marker

        # ---- Response phase ----
        if first.kind == "error":
            self.endpoint.acknowledge(first)
            error = parse_json_payload(first.payload)
            raise RuntimeError(error.get("message", "remote tunnel error"))

        if first.kind == "resp_single":
            # Single-frame response: meta + body + SHA-256 in one frame.
            # The response terminal frame is intentionally fire-and-forget on B;
            # its ACK is not consumed by B and would only create another A-side
            # clipboard write immediately before the next Git request. The body
            # digest below is the end-to-end integrity confirmation.
            status, resp_headers, resp_body = unpack_response(first.payload)
            log_event(self.logger, logging.INFO, "http.request.complete",
                      session=session[:8], status=status,
                      response_bytes=len(resp_body),
                      elapsed_ms=int((time.monotonic() - started) * 1000))
            return HttpMessage(status, resp_headers, resp_body)

        # Multi-frame response: first is resp_meta, continue receiving.
        response_meta = parse_json_payload(
            reassemble(self._receive_set_from(first, "resp_meta", self.ack_timeout)))
        response_frames = self._receive_set("resp_data", session, timeout)
        end = self.endpoint.wait_frame(
            lambda item: item.session == session and item.kind in {"resp_end", "error"},
            timeout,
        )
        if end.kind == "error":
            self.endpoint.acknowledge(end)
            error = parse_json_payload(end.payload)
            raise RuntimeError(error.get("message", "remote tunnel error"))
        body_bytes = reassemble(response_frames)
        expected = end.meta.get("sha256") if end.meta else None
        if expected and digest(body_bytes) != expected:
            raise ProtocolError("response digest mismatch")
        response = HttpMessage(
            int(response_meta.get("status", 502)),
            [(str(k), str(v)) for k, v in response_meta.get("headers", [])],
            body_bytes,
        )
        log_event(self.logger, logging.INFO, "http.request.complete",
                  session=session[:8], status=response.status,
                  response_bytes=len(response.body),
                  elapsed_ms=int((time.monotonic() - started) * 1000))
        return response


class ClipboardGitServer:
    def __init__(self, endpoint: ClipboardEndpoint, *, chunk_bytes: int,
                 ack_timeout: float, retries: int, target_host: str,
                 target_port: int, upstream_timeout: float,
                 logger: logging.Logger | None = None) -> None:
        self.endpoint = endpoint
        self.chunk_bytes = chunk_bytes
        self.ack_timeout = ack_timeout
        self.retries = retries
        self.target_host = target_host
        self.target_port = target_port
        self.upstream_timeout = upstream_timeout
        self.logger = logger or logging.getLogger("clipboard_git_tunnel.server")

    def _receive_set(self, first: Frame, expected_kind: str, timeout: float) -> list[Frame]:
        if first.kind != expected_kind:
            raise ProtocolError(f"expected {expected_kind}, got {first.kind}")
        frames = [first]
        self.endpoint.acknowledge(first)
        while first.seq < first.total - 1:
            frame = self.endpoint.wait_frame(
                lambda item: item.session == first.session and item.kind == expected_kind,
                timeout,
            )
            if frame.total != first.total:
                raise ProtocolError("inconsistent clipboard frame total")
            if frame.seq == frames[-1].seq:
                # ACK loss can cause a retransmission of the last frame. It is
                # safe to acknowledge it again without appending duplicate data.
                self.endpoint.acknowledge(frame)
                continue
            if frame.seq != frames[-1].seq + 1:
                raise ProtocolError("clipboard frames out of order")
            self.endpoint.acknowledge(frame)
            frames.append(frame)
            first = frame
        return frames

    def _receive_request(self, first: Frame, timeout: float) -> tuple[str, str, list[tuple[str, str]], bytes]:
        session = first.session
        meta = parse_json_payload(reassemble(self._receive_set(first, "req_meta", timeout)))
        method = str(meta.get("method", "GET"))
        path = str(meta.get("path", "/"))
        raw_headers = meta.get("headers", [])
        headers = [
            (str(pair[0]), str(pair[1]))
            for pair in raw_headers
            if isinstance(pair, list) and len(pair) == 2
        ]
        data_first = self.endpoint.wait_frame(
            lambda item: item.session == session and item.kind == "req_data",
            timeout,
        )
        data = self._receive_set(data_first, "req_data", timeout)
        end = self.endpoint.wait_frame(
            lambda item: item.session == session and item.kind == "req_end",
            timeout,
        )
        self.endpoint.acknowledge(end)
        commit = self.endpoint.wait_frame(
            lambda item: item.session == session and item.kind == "req_commit",
            timeout,
        )
        self.endpoint.acknowledge(commit)
        # Wait for A to confirm it received req_commit's ACK before sending
        # anything. Deliberately no ACK for resp_begin: writing twice in a row
        # from B side (req_commit ACK, then response) within HSR's propagation
        # window silently drops the earlier write on the shared clipboard.
        self.endpoint.wait_frame(
            lambda item: item.session == session and item.kind == "resp_begin",
            timeout,
        )
        return method, path, headers, reassemble(data)

    def _forward(self, method: str, path: str, headers: list[tuple[str, str]], body: bytes) -> HttpMessage:
        started = time.monotonic()
        log_event(self.logger, logging.DEBUG, "upstream.request.begin",
                  method=method, path=safe_http_path(path), request_bytes=len(body),
                  target=f"{self.target_host}:{self.target_port}")
        from http.client import HTTPConnection
        connection = HTTPConnection(self.target_host, self.target_port, timeout=self.upstream_timeout)
        filtered = [
            (name, value) for name, value in headers
            if name.lower() not in {"host", "content-length", "connection", "transfer-encoding"}
        ]
        filtered.append(("Host", f"{self.target_host}:{self.target_port}"))
        if body:
            filtered.append(("Content-Length", str(len(body))))
        try:
            connection.request(method, path, body=body or None, headers=dict(filtered))
            response = connection.getresponse()
            result = HttpMessage(response.status, response.getheaders(), response.read())
            log_event(self.logger, logging.DEBUG, "upstream.response.complete",
                      status=result.status, response_bytes=len(result.body),
                      elapsed_ms=int((time.monotonic() - started) * 1000))
            return result
        finally:
            connection.close()

    def serve_one(self, timeout: float = 300.0) -> bool:
        try:
            first = self.endpoint.wait_frame(
                lambda item: item.kind in {"req_meta", "req_single"}, timeout)
        except TimeoutError:
            # Normal idle period: no Git request arrived within the poll
            # window. This is not a transport failure and must not produce a
            # traceback every --timeout seconds.
            return False
        session = first.session
        try:
            # ---- Request phase: single-frame vs multi-frame ----
            if first.kind == "req_single":
                method, path, headers, body = unpack_request(first.payload)
                self.endpoint.acknowledge(first)
                # resp_begin barrier: wait for A to signal readiness before
                # writing the response. This separates B's ACK of the request
                # from B's response write by a full propagation round-trip.
                self.endpoint.wait_frame(
                    lambda item: item.session == session and item.kind == "resp_begin",
                    timeout,
                )
            else:
                method, path, headers, body = self._receive_request(first, timeout)

            log_event(self.logger, logging.INFO, "http.request.received",
                      session=session[:8], method=method, path=safe_http_path(path),
                      request_bytes=len(body))
            response = self._forward(method, path, headers, body)

            # ---- Response phase: single-frame vs multi-frame ----
            packed_resp = pack_response(response.status, response.headers, response.body)
            if len(packed_resp) <= self.chunk_bytes:
                # Single-frame response: meta + body + SHA-256 in one frame
                # replaces resp_meta + resp_data + resp_end (3+ round-trips → 1).
                # Fire-and-forget: the client self-verifies the body SHA-256, so
                # NO ACK wait here. Waiting would trap serve_one in a
                # retries×ack_timeout busy window (5×5s=25s) when the ACK is
                # lost, during which A's next req_single goes unanswered → 504.
                # The response frame is the LAST B-side write of this request;
                # the next B-side write only happens after A's next
                # request + resp_begin, which is safely past the propagation
                # window, so no ACK pacing is required.
                self.endpoint.write_frame(
                    make_frame("resp_single", session, packed_resp))
            else:
                # Multi-frame response (large body): existing protocol.
                meta = json_payload({"status": response.status, "headers": response.headers})
                for frame in frame_chunks("resp_meta", session, meta, self.chunk_bytes):
                    # Meta ACK doubles as pacing: B must not write RESP_DATA
                    # until A confirms RESP_META arrived (single-slot clipboard).
                    self.endpoint.send_and_wait_ack(frame, self.ack_timeout, self.retries)
                for frame in frame_chunks("resp_data", session, response.body, self.chunk_bytes):
                    self.endpoint.send_and_wait_ack(frame, self.ack_timeout, self.retries)
                end = Frame("resp_end", session, 0, 1, b"", None,
                            {"sha256": digest(response.body)})
                # Fire-and-forget final frame (same rationale as resp_single):
                # a lost ACK must NOT strand serve_one for a full ACK window,
                # or the pipelined client retries would 504.
                self.endpoint.write_frame(end)

            log_event(self.logger, logging.INFO, "http.request.complete",
                      session=session[:8], status=response.status,
                      request_bytes=len(body), response_bytes=len(response.body))
            return True
        except Exception as exc:
            error = Frame("error", session, 0, 1,
                          json_payload({"message": str(exc)[:500]}), None)
            try:
                self.endpoint.send_and_wait_ack(error, self.ack_timeout, self.retries)
            except Exception:
                pass
            raise
