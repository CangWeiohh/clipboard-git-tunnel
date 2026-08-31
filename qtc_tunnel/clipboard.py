"""Clipboard backends and reliable stop-and-wait framing."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from collections import deque
from dataclasses import replace
from ctypes import wintypes
from typing import Callable

from .logging_utils import log_event
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

    def __init__(self, retries: int = 750, delay: float = 0.02,
                 logger: logging.Logger | None = None) -> None:
        if os.name != "nt":
            raise ClipboardError("WindowsClipboard requires Windows")
        self.retries = retries
        self.delay = delay
        self.logger = logger or logging.getLogger("clipboard_git_tunnel.clipboard")
        # HSR holds the clipboard open while syncing large frames (256 KiB
        # payloads take seconds to move), so local OpenClipboard calls must be
        # willing to wait well beyond the old 200 ms budget or every request
        # that collides with an in-flight sync dies with OpenClipboard failed.
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
        warned = False
        for attempt in range(self.retries):
            if self.user32.OpenClipboard(None):
                return
            if not warned and attempt * self.delay >= 2.0:
                warned = True
                log_event(self.logger, logging.WARNING, "clipboard.busy",
                          elapsed_ms=int(attempt * self.delay * 1000),
                          hint="HSR syncing")
            time.sleep(self.delay)
        raise ClipboardError(
            f"OpenClipboard failed (busy > {self.retries * self.delay:.0f}s)")

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
                 focus: object | None = None, min_write_gap: float = 0.0,
                 logger: logging.Logger | None = None) -> None:
        self.backend = backend
        self.poll_interval = poll_interval
        self.focus = focus
        self.min_write_gap = float(min_write_gap)
        self.logger = logger or logging.getLogger("clipboard_git_tunnel.clipboard")
        # HSR may expose a value left by a previous tunnel run. Establish the
        # current clipboard as the receive baseline so B does not replay that
        # stale request immediately after startup.
        try:
            self._last_text = self.backend.read()
        except Exception:
            self._last_text = None
        self._last_write_time = 0.0
        # Frames that arrive while a different predicate is being waited on are
        # kept here for a later phase instead of being silently dropped (for
        # example, a new request held while B is waiting for resp_begin).
        self._pending_frames: deque[Frame] = deque(maxlen=32)

    def write_frame(self, frame: Frame) -> None:
        text = frame.to_text()
        if self.focus is not None:
            self.focus.before_clipboard_write()
        self.backend.write(text)
        self._last_text = text
        self._last_write_time = time.monotonic()
        log_event(self.logger, logging.INFO, "clipboard.write",
                  kind=frame.kind, session=frame.session[:8], seq=frame.seq,
                  total=frame.total, retry=frame.retry,
                  payload_bytes=len(frame.payload))

    def wait_write_gap(self) -> None:
        """Enforce a quiet period since this endpoint's previous clipboard write.

        HSR clipboard sync is a single-slot, event-driven channel: two
        consecutive writes from the same side inside one propagation window can
        silently drop one of them. stop-and-wait ACKs pace writes *within* a
        request, but the first frame of a new request can follow the previous
        request's final ACK within milliseconds (e.g. git clone's GET followed
        immediately by its POST). Callers must invoke this before starting a
        new exchange so consecutive same-side writes are at least one
        propagation round trip apart.
        """
        wait = self.min_write_gap - (time.monotonic() - self._last_write_time)
        if wait > 0:
            log_event(self.logger, logging.DEBUG, "clipboard.write_gap",
                      wait_ms=int(wait * 1000))
            time.sleep(wait)

    def _take_pending(self, predicate: Callable[[Frame], bool]) -> Frame | None:
        for index, frame in enumerate(self._pending_frames):
            if predicate(frame):
                del self._pending_frames[index]
                return frame
        return None

    def _stash_frame(self, frame: Frame) -> None:
        if len(self._pending_frames) == self._pending_frames.maxlen:
            self._pending_frames.popleft()
        self._pending_frames.append(frame)

    def wait_frame(self, predicate: Callable[[Frame], bool], timeout: float) -> Frame:
        pending = self._take_pending(predicate)
        if pending is not None:
            return pending
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.backend.read()
            if text != self._last_text:
                self._last_text = text
                try:
                    frame = Frame.from_text(text)
                except ProtocolError:
                    frame = None
                if frame is not None:
                    if predicate(frame):
                        log_event(self.logger, logging.INFO, "frame.receive",
                                  session=frame.session[:8], kind=frame.kind,
                                  seq=frame.seq, total=frame.total,
                                  payload_bytes=len(frame.payload), retry=frame.retry)
                        return frame
                    log_event(self.logger, logging.WARNING, "frame.unmatched",
                              session=frame.session[:8], kind=frame.kind,
                              seq=frame.seq, total=frame.total,
                              payload_bytes=len(frame.payload), retry=frame.retry,
                              hint="frame does not match current wait; stashed")
                    self._stash_frame(frame)
            pending = self._take_pending(predicate)
            if pending is not None:
                return pending
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
                log_event(self.logger, logging.INFO, "frame.ack",
                          session=frame.session[:8], kind=frame.kind,
                          seq=frame.seq, attempt=attempt)
                return
            except TimeoutError as exc:
                if attempt == retries:
                    log_event(self.logger, logging.WARNING, "clipboard.ack_timeout",
                              kind=frame.kind, seq=frame.seq, attempts=retries,
                              hint="peer never observed this frame; check HSRClient "
                                   "window has foreground (HSR skips clipboard sync "
                                   "while its render window is not active)")
                    raise TimeoutError(
                        f"clipboard ACK timeout kind={frame.kind} seq={frame.seq} "
                        f"attempts={retries}") from exc

    def acknowledge(self, frame: Frame) -> None:
        ack = Frame("ack", frame.session, frame.seq, frame.total, b"", None)
        self.write_frame(ack)
