"""Bidirectional clipboard transport for Git Smart HTTP request/response bytes."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .clipboard import ClipboardEndpoint
from .protocol import Frame, ProtocolError, digest, json_payload, parse_json_payload
from .transfer import frame_chunks, reassemble


@dataclass
class HttpMessage:
    status: int
    headers: list[tuple[str, str]]
    body: bytes


class ClipboardGitClient:
    def __init__(self, endpoint: ClipboardEndpoint, *, chunk_bytes: int,
                 ack_timeout: float, retries: int) -> None:
        self.endpoint = endpoint
        self.chunk_bytes = chunk_bytes
        self.ack_timeout = ack_timeout
        self.retries = retries

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
            frames.append(frame)
            if frame.seq == frame.total - 1:
                return frames

    def request(self, session: str, method: str, path: str,
                headers: list[tuple[str, str]], body: bytes,
                timeout: float) -> HttpMessage:
        meta = json_payload({
            "method": method,
            "path": path,
            "headers": [[name, value] for name, value in headers],
        })
        self._send_frames("req_meta", session, meta)
        self._send_frames("req_data", session, body)
        end = Frame("req_end", session, 0, 1, b"", None)
        self.endpoint.send_and_wait_ack(end, self.ack_timeout, self.retries)
        # Barrier for the final request ACK. HSR clipboard propagation can be
        # slower than the local B-end code path; without this confirmation B
        # could overwrite req_end's ACK with the response before A observes it.
        commit = Frame("req_commit", session, 0, 1, b"", None)
        self.endpoint.send_and_wait_ack(commit, self.ack_timeout, self.retries)

        response_meta = parse_json_payload(
            reassemble(self._receive_set("resp_meta", session, timeout)))
        response_frames = self._receive_set("resp_data", session, timeout)
        end = self.endpoint.wait_frame(
            lambda item: item.session == session and item.kind in {"resp_end", "error"},
            timeout,
        )
        self.endpoint.acknowledge(end)
        if end.kind == "error":
            error = parse_json_payload(end.payload)
            raise RuntimeError(error.get("message", "remote tunnel error"))
        body_bytes = reassemble(response_frames)
        expected = end.meta.get("sha256") if end.meta else None
        if expected and digest(body_bytes) != expected:
            raise ProtocolError("response digest mismatch")
        return HttpMessage(
            int(response_meta.get("status", 502)),
            [(str(k), str(v)) for k, v in response_meta.get("headers", [])],
            body_bytes,
        )


class ClipboardGitServer:
    def __init__(self, endpoint: ClipboardEndpoint, *, chunk_bytes: int,
                 ack_timeout: float, retries: int, target_host: str,
                 target_port: int, upstream_timeout: float) -> None:
        self.endpoint = endpoint
        self.chunk_bytes = chunk_bytes
        self.ack_timeout = ack_timeout
        self.retries = retries
        self.target_host = target_host
        self.target_port = target_port
        self.upstream_timeout = upstream_timeout

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
        return method, path, headers, reassemble(data)

    def _forward(self, method: str, path: str, headers: list[tuple[str, str]], body: bytes) -> HttpMessage:
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
            return HttpMessage(response.status, response.getheaders(), response.read())
        finally:
            connection.close()

    def serve_one(self, timeout: float = 300.0) -> None:
        first = self.endpoint.wait_frame(lambda item: item.kind == "req_meta", timeout)
        session = first.session
        try:
            method, path, headers, body = self._receive_request(first, timeout)
            response = self._forward(method, path, headers, body)
            meta = json_payload({"status": response.status, "headers": response.headers})
            for frame in frame_chunks("resp_meta", session, meta, self.chunk_bytes):
                self.endpoint.send_and_wait_ack(frame, self.ack_timeout, self.retries)
            for frame in frame_chunks("resp_data", session, response.body, self.chunk_bytes):
                self.endpoint.send_and_wait_ack(frame, self.ack_timeout, self.retries)
            end = Frame("resp_end", session, 0, 1, b"", None,
                        {"sha256": digest(response.body)})
            self.endpoint.send_and_wait_ack(end, self.ack_timeout, self.retries)
        except Exception as exc:
            error = Frame("error", session, 0, 1,
                          json_payload({"message": str(exc)[:500]}), None)
            try:
                self.endpoint.send_and_wait_ack(error, self.ack_timeout, self.retries)
            except Exception:
                pass
            raise
