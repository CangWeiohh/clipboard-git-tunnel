"""Clipboard backends and reliable stop-and-wait framing."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import replace
from ctypes import wintypes
from typing import Callable

from .protocol import Frame, ProtocolError, parse_json_payload


class ClipboardError(RuntimeError):
    pass


class ClipboardBackend:
    def read(self) -> str:
        raise NotImplementedError

    def write(self, text: str) -> None:
        raise NotImplementedError


class WindowsClipboard(ClipboardBackend):
    """CF_UNICODETEXT clipboard backend; intended for Windows A/B endpoints."""

    CF_UNICODETEXT = 13

    def __init__(self, retries: int = 20, delay: float = 0.01) -> None:
        if os.name != "nt":
            raise ClipboardError("WindowsClipboard requires Windows")
        self.retries = retries
        self.delay = delay
        self._lock = threading.RLock()
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.user32.OpenClipboard.argtypes = [wintypes.HWND]
        self.user32.OpenClipboard.restype = wintypes.BOOL
        self.user32.CloseClipboard.argtypes = []
        self.user32.EmptyClipboard.argtypes = []
        self.user32.GetClipboardData.argtypes = [wintypes.UINT]
        self.user32.GetClipboardData.restype = wintypes.HANDLE
        self.user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self.user32.SetClipboardData.restype = wintypes.HANDLE
        self.kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        self.kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        self.kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self.kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        self.kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        self.GMEM_MOVEABLE = 0x0002

    def _open(self) -> None:
        for _ in range(self.retries):
            if self.user32.OpenClipboard(None):
                return
            time.sleep(self.delay)
        raise ClipboardError("OpenClipboard failed")

    def read(self) -> str:
        with self._lock:
            self._open()
            try:
                handle = self.user32.GetClipboardData(self.CF_UNICODETEXT)
                if not handle:
                    return ""
                ptr = self.kernel32.GlobalLock(handle)
                if not ptr:
                    return ""
                try:
                    return ctypes.wstring_at(ptr)
                finally:
                    self.kernel32.GlobalUnlock(handle)
            finally:
                self.user32.CloseClipboard()

    def write(self, text: str) -> None:
        encoded = (text + "\0").encode("utf-16-le")
        with self._lock:
            self._open()
            handle = None
            transferred = False
            try:
                handle = self.kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(encoded))
                if not handle:
                    raise ClipboardError("GlobalAlloc failed")
                ptr = self.kernel32.GlobalLock(handle)
                if not ptr:
                    raise ClipboardError("GlobalLock failed")
                try:
                    ctypes.memmove(ptr, encoded, len(encoded))
                finally:
                    self.kernel32.GlobalUnlock(handle)
                self.user32.EmptyClipboard()
                if not self.user32.SetClipboardData(self.CF_UNICODETEXT, handle):
                    raise ClipboardError("SetClipboardData failed")
                transferred = True
            finally:
                self.user32.CloseClipboard()
                if handle and not transferred:
                    self.kernel32.GlobalFree(handle)


class MemoryClipboard(ClipboardBackend):
    """Thread-safe backend used by protocol tests and local simulations."""

    def __init__(self, initial: str = "") -> None:
        self._text = initial
        self._revision = 0
        self._condition = threading.Condition()

    def read(self) -> str:
        with self._condition:
            return self._text

    def write(self, text: str) -> None:
        with self._condition:
            self._text = text
            self._revision += 1
            self._condition.notify_all()

    def wait_change(self, revision: int, timeout: float | None = None) -> tuple[int, str]:
        with self._condition:
            self._condition.wait_for(lambda: self._revision > revision, timeout)
            return self._revision, self._text

    @property
    def revision(self) -> int:
        with self._condition:
            return self._revision


class ClipboardEndpoint:
    """Polling endpoint that filters malformed, stale, or unrelated frames."""

    def __init__(self, backend: ClipboardBackend, poll_interval: float = 0.05,
                 focus: object | None = None) -> None:
        self.backend = backend
        self.poll_interval = poll_interval
        self.focus = focus
        self._last_text = None

    def write_frame(self, frame: Frame) -> None:
        text = frame.to_text()
        if self.focus is not None:
            self.focus.before_clipboard_write()
        self.backend.write(text)
        self._last_text = text

    def wait_frame(self, predicate: Callable[[Frame], bool], timeout: float) -> Frame:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.backend.read()
            if text != self._last_text:
                self._last_text = text
                try:
                    frame = Frame.from_text(text)
                except ProtocolError:
                    frame = None
                if frame is not None and predicate(frame):
                    return frame
            time.sleep(self.poll_interval)
        raise TimeoutError("clipboard frame timeout")

    def send_and_wait_ack(self, frame: Frame, timeout: float, retries: int = 5) -> None:
        for attempt in range(1, retries + 1):
            outbound = replace(frame, retry=attempt - 1)
            self.write_frame(outbound)
            try:
                reply = self.wait_frame(
                    lambda candidate: candidate.session == frame.session
                    and candidate.seq == frame.seq
                    and candidate.kind in {"ack", "error"},
                    timeout,
                )
                if reply.kind == "error":
                    try:
                        details = parse_json_payload(reply.payload)
                    except ProtocolError:
                        details = {"message": "remote returned malformed error"}
                    self.acknowledge(reply)
                    raise ClipboardError(details.get("message", "remote clipboard error"))
                return
            except TimeoutError as exc:
                if attempt == retries:
                    raise TimeoutError(
                        f"clipboard ACK timeout kind={frame.kind} seq={frame.seq} "
                        f"attempts={retries}") from exc

    def acknowledge(self, frame: Frame) -> None:
        ack = Frame("ack", frame.session, frame.seq, frame.total, b"", None)
        self.write_frame(ack)
