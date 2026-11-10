"""Opt-in learning of daily routines: which app is in front, and when.

Privacy by design: only the program's file name (for example "chrome.exe") is recorded, never
window titles (which contain page names and document names), never keystrokes or screen
contents. It is off unless R2_LEARN=1, it lives only in the local database, and `r2 forget`
wipes it. R2 uses it for one thing: knowing which apps you usually use at this time of day."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("r2.activity")

_IGNORE = {
    "python.exe", "pythonw.exe",  # R2 itself
    "lockapp.exe", "searchhost.exe", "shellexperiencehost.exe", "textinputhost.exe",
    "startmenuexperiencehost.exe", "applicationframehost.exe",
}


def current_app() -> str | None:
    """File name of the program that owns the foreground window (Windows), or None."""
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value.rsplit("\\", 1)[-1] or None
    finally:
        kernel32.CloseHandle(handle)


class ActivityLogger:
    def __init__(self, memory, sampler=current_app, interval: float = 60.0):
        self._memory = memory
        self._sampler = sampler
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample_once(self) -> None:
        try:
            name = self._sampler()
        except Exception:
            return
        if name and name.lower() not in _IGNORE:
            self._memory.log_activity(name)

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                self.sample_once()
                self._stop.wait(self._interval)

        self._thread = threading.Thread(target=loop, name="r2-activity", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
