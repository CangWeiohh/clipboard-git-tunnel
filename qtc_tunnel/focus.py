"""Windows HSRClient foreground controller.

HSR clipboard redirection in the qr-git-tunnel deployment only propagates
updates reliably while the HSRClient render window owns the foreground. This module is
kept separate from the transport protocol so the transport remains testable
with MemoryClipboard on non-Windows systems.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import threading
import time
from ctypes import wintypes

from .logging_utils import log_event


def hwnd_value(hwnd) -> int:
    """Normalize Win32 HWND values for reliable comparisons.

    ``wintypes.HWND`` is a ``ctypes.c_void_p`` wrapper. Two wrappers carrying
    the same numeric handle compare unequal by object identity, while Win32 API
    calls may return a plain int. Always compare the underlying integer value.
    """
    value = getattr(hwnd, "value", hwnd)
    return int(value or 0)


def same_hwnd(left, right) -> bool:
    return hwnd_value(left) == hwnd_value(right)


class FocusController:
    def before_clipboard_write(self) -> None:
        """Give the transport's clipboard write a chance to cross HSR."""


class NullFocusController(FocusController):
    pass


class WindowsHSRFocus(FocusController):
    _SYSTEM_PROCESSES = {
        "textinputhost.exe", "searchapp.exe", "searchhost.exe",
        "startmenuexperiencehost.exe", "shellexperiencehost.exe",
        "applicationframehost.exe", "sihost.exe", "dwm.exe",
    }

    def __init__(self, keywords: list[str] | None = None, rescan_interval: float = 3.0,
                 logger: logging.Logger | None = None):
        if os.name != "nt":
            raise RuntimeError("WindowsHSRFocus requires Windows")
        self.keywords = [item.lower() for item in (keywords or []) if item.strip()]
        self.rescan_interval = rescan_interval
        self.logger = logger or logging.getLogger("clipboard_git_tunnel.focus")
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self._lock = threading.RLock()
        self._hwnd = None
        self._last_scan = 0.0
        self._last_alt = 0.0
        self._last_warning = 0.0

        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.GetWindowTextW.restype = ctypes.c_int
        self.user32.GetClassNameW.restype = ctypes.c_int
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.SetFocus.argtypes = [wintypes.HWND]
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        self.user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p]
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self._scan(force=True)

    def _process_name(self, pid: int) -> str:
        # QueryFullProcessImageNameW avoids the psapi dependency and works for
        # the normal HSRClient process under the same interactive user.
        handle = self.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(len(buf))
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
        finally:
            self.kernel32.CloseHandle(handle)
        return ""

    def _window_process_base(self, hwnd) -> str:
        """Return the executable basename owning *hwnd*, or an empty string."""
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        if not self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return ""
        path = self._process_name(pid.value)
        return path.rsplit("\\", 1)[-1].lower()

    def _is_trusted_window(self, hwnd) -> bool:
        """Whether an HWND belongs to the HSR client process family."""
        base = self._window_process_base(hwnd)
        return bool(base) and (
            "hsrclient" in base or "cmss" in base or "receiver" in base
        )

    def _scan(self, force: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_scan < self.rescan_interval:
                return
            self._last_scan = now
            candidates: list[tuple[int, int, str]] = []
            foreground = self.user32.GetForegroundWindow()

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def enum_proc(hwnd, _lparam):
                if not self.user32.IsWindowVisible(hwnd):
                    return True
                title_buf = ctypes.create_unicode_buffer(256)
                class_buf = ctypes.create_unicode_buffer(256)
                self.user32.GetWindowTextW(hwnd, title_buf, 256)
                self.user32.GetClassNameW(hwnd, class_buf, 256)
                pid = wintypes.DWORD()
                self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                proc = self._process_name(pid.value)
                base = proc.rsplit("\\", 1)[-1].lower()
                if base in self._SYSTEM_PROCESSES:
                    return True
                haystack = f"{title_buf.value} {class_buf.value} {proc}".lower()
                trusted = "hsrclient" in base or "cmss" in base or "receiver" in base
                if self.keywords:
                    trusted = any(key in haystack for key in self.keywords)
                if trusted:
                    # Prefer the window that is already foreground. HSRClient
                    # may expose multiple top-level windows with the same title;
                    # pinning a different same-titled HWND makes the old equality
                    # check report a false focus failure and can stop HSR sync.
                    score = (1000 if same_hwnd(hwnd, foreground) else 0)
                    score += (100 if "hsrclient" in base else 50) + len(haystack)
                    candidates.append((score, int(hwnd), haystack))
                return True

            self.user32.EnumWindows(enum_proc, 0)
            if candidates:
                candidates.sort(reverse=True)
                self._hwnd = wintypes.HWND(candidates[0][1])

    def before_clipboard_write(self) -> None:
        with self._lock:
            if not self._hwnd or not self.user32.IsWindow(self._hwnd):
                self._scan(force=True)
            if not self._hwnd:
                return
            target = self._hwnd
            foreground = self.user32.GetForegroundWindow()
            if same_hwnd(foreground, target):
                return
            # HSRClient can recreate or switch its top-level render HWND while
            # keeping the same foreground process/window title. In that case the
            # foreground is already a valid HSR surface; treating HWND inequality
            # as focus failure causes needless rewrites and, more importantly,
            # hides the fact that the clipboard sync path is actually active.
            if self._is_trusted_window(foreground):
                self._hwnd = wintypes.HWND(hwnd_value(foreground))
                return
            self.user32.ShowWindow(target, 9)
            fg_tid = wintypes.DWORD()
            target_tid = wintypes.DWORD()
            if foreground:
                self.user32.GetWindowThreadProcessId(foreground, ctypes.byref(fg_tid))
            self.user32.GetWindowThreadProcessId(target, ctypes.byref(target_tid))
            attached = bool(foreground and fg_tid.value and target_tid.value and
                            self.user32.AttachThreadInput(fg_tid.value, target_tid.value, True))
            try:
                self.user32.BringWindowToTop(target)
                self.user32.SetForegroundWindow(target)
                self.user32.SetFocus(target)
            finally:
                if attached:
                    self.user32.AttachThreadInput(fg_tid.value, target_tid.value, False)
            if not same_hwnd(self.user32.GetForegroundWindow(), target):
                now = time.monotonic()
                if now - self._last_alt >= 2.0:
                    self._last_alt = now
                    self.user32.keybd_event(0x12, 0, 0, None)
                    self.user32.keybd_event(0x12, 0, 0x0002, None)
                self.user32.BringWindowToTop(target)
                self.user32.SetForegroundWindow(target)
                if not same_hwnd(self.user32.GetForegroundWindow(), target):
                    if now - self._last_warning >= 5.0:
                        self._last_warning = now
                        fg = self.user32.GetForegroundWindow()
                        fg_title = ctypes.create_unicode_buffer(256)
                        tgt_title = ctypes.create_unicode_buffer(256)
                        self.user32.GetWindowTextW(fg, fg_title, 256)
                        self.user32.GetWindowTextW(target, tgt_title, 256)
                        log_event(self.logger, logging.WARNING, "focus.foreground_failed",
                                  foreground=fg_title.value, target=tgt_title.value)
