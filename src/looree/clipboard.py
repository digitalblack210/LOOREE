"""Insert text into the focused app by pasting it through the clipboard."""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

from . import win32
from .win32 import kernel32, user32

log = logging.getLogger(__name__)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

VK_CONTROL = 0x11
VK_V = 0x56
# Held modifiers would turn the injected Ctrl+V into another shortcut
# (Win+V opens clipboard history), so paste waits for these to be released.
MODIFIER_VKS = (0x5B, 0x5C, 0xA0, 0xA1, 0xA4, 0xA5)  # Win, Shift, Alt

# Formats whose handles are GDI objects rather than global memory. They cannot
# be copied byte for byte, and Windows rebuilds the common ones from the
# memory-backed formats (CF_BITMAP from CF_DIB, for example).
_HANDLE_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}

# How long the target app gets to read the clipboard before it is restored.
RESTORE_DELAY_SECONDS = 0.3

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t


class _Clipboard:
    """Context manager holding the clipboard open; other apps may hold it briefly."""

    def __enter__(self) -> _Clipboard:
        for _ in range(20):
            if user32.OpenClipboard(None):
                return self
            time.sleep(0.02)
        raise OSError("could not open the clipboard")

    def __exit__(self, *exc) -> None:
        user32.CloseClipboard()


def _put(fmt: int, data: bytes) -> None:
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
    if not handle:
        raise MemoryError("GlobalAlloc failed")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise MemoryError("GlobalLock failed")
    ctypes.memmove(pointer, data, len(data))
    kernel32.GlobalUnlock(handle)
    # On success the clipboard owns the memory.
    if not user32.SetClipboardData(fmt, handle):
        kernel32.GlobalFree(handle)


def _snapshot() -> list[tuple[int, bytes]]:
    """Copy every memory-backed format currently on the clipboard."""
    saved = []
    with _Clipboard():
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            if fmt not in _HANDLE_FORMATS and not 0x300 <= fmt <= 0x3FF:
                handle = user32.GetClipboardData(fmt)
                pointer = kernel32.GlobalLock(handle) if handle else None
                if pointer:
                    saved.append((fmt, ctypes.string_at(pointer, kernel32.GlobalSize(handle))))
                    kernel32.GlobalUnlock(handle)
            fmt = user32.EnumClipboardFormats(fmt)
    return saved


def _restore(saved: list[tuple[int, bytes]]) -> None:
    with _Clipboard():
        user32.EmptyClipboard()
        for fmt, data in saved:
            _put(fmt, data)


def _set_text(text: str) -> None:
    with _Clipboard():
        user32.EmptyClipboard()
        _put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
        # Keep the transcript out of clipboard history (Win+V) and cloud sync.
        no = (0).to_bytes(4, "little")
        _put(user32.RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing"), no)
        _put(user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory"), no)
        _put(user32.RegisterClipboardFormatW("CanUploadToCloudClipboard"), no)


def _wait_for_modifiers_released(timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while any(win32.is_key_down(vk) for vk in MODIFIER_VKS):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def paste_text(text: str, restore_delay: float = RESTORE_DELAY_SECONDS) -> bool:
    """Paste text into the focused window, then put the old clipboard back.

    Returns False if the paste was skipped because a modifier key stayed held.
    """
    if not _wait_for_modifiers_released():
        log.warning("Paste skipped: a modifier key is still held")
        return False
    saved = _snapshot()
    try:
        _set_text(text)
        win32.send_keys((VK_CONTROL, True), (VK_V, True), (VK_V, False), (VK_CONTROL, False))
        time.sleep(restore_delay)
    finally:
        _restore(saved)
    return True
