"""Global push-to-talk hotkey built on a low-level keyboard hook."""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes

from . import win32
from .win32 import user32

log = logging.getLogger(__name__)

# Each name matches any of its virtual keys (left and right variants).
KEY_GROUPS: dict[str, frozenset[int]] = {
    "ctrl": frozenset({0xA2, 0xA3}),
    "shift": frozenset({0xA0, 0xA1}),
    "alt": frozenset({0xA4, 0xA5}),
    "win": frozenset({0x5B, 0x5C}),
    **{f"f{n}": frozenset({0x6F + n}) for n in range(1, 25)},
}

# An unassigned virtual key. Tapping it while Win is held stops Windows from
# treating the Win press as a lone tap, which is what opens the Start menu.
MASK_VK = 0xE8

REHOOK_SECONDS = 120


def parse_hotkey(spec: str) -> list[frozenset[int]]:
    """Turn a spec such as "ctrl+win" into the key groups that must all be held."""
    names = [part.strip().lower() for part in spec.split("+") if part.strip()]
    if not names:
        raise ValueError("hotkey is empty")
    unknown = [name for name in names if name not in KEY_GROUPS]
    if unknown:
        raise ValueError(f"unknown hotkey key(s) {unknown}; choose from {sorted(KEY_GROUPS)}")
    return [KEY_GROUPS[name] for name in names]


class HotkeyListener:
    """Calls on_press when every key of the hotkey is held and on_release when
    one is let go. Pressing any other key during the hold calls on_cancel instead,
    so shortcuts that extend the hotkey (Ctrl+Win+D, ...) still work normally.

    Callbacks run on the hook thread and must return quickly: Windows drops
    hooks that stall.
    """

    def __init__(
        self,
        hotkey: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Callable[[], None],
    ) -> None:
        self._groups = parse_hotkey(hotkey)
        self._keys = frozenset().union(*self._groups)
        self._uses_win = bool(self._keys & KEY_GROUPS["win"])
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel
        self._down: set[int] = set()
        self._active = False
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: Exception | None = None
        # Keep a reference: the hook dies if the callback is garbage collected.
        self._proc = win32.HOOKPROC(self._hook_proc)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="turaco-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error:
            raise self._error

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            user32.PostThreadMessageW(self._thread_id, win32.WM_QUIT, 0, 0)
            self._thread.join(timeout=2)

    def _run(self) -> None:
        self._thread_id = win32.kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(
            win32.WH_KEYBOARD_LL, self._proc, win32.kernel32.GetModuleHandleW(None), 0
        )
        if not hook:
            self._error = ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            return
        self._ready.set()
        user32.SetTimer(None, 0, REHOOK_SECONDS * 1000, None)
        try:
            # The hook is delivered through this thread's message queue.
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == win32.WM_TIMER and not self._active:
                    # Windows silently removes a low-level hook whose callback
                    # was too slow to answer, and gives no way to find out.
                    # Installing a fresh one now and then means a lost hook
                    # costs a couple of minutes, not the rest of the session.
                    user32.UnhookWindowsHookEx(hook)
                    hook = user32.SetWindowsHookExW(
                        win32.WH_KEYBOARD_LL, self._proc, win32.kernel32.GetModuleHandleW(None), 0
                    )
                    if not hook:
                        log.error("Could not reinstall the keyboard hook")
        finally:
            user32.UnhookWindowsHookEx(hook)

    def is_held(self) -> bool:
        """True if Windows reports every key of the hotkey as physically down."""
        return all(any(win32.is_key_down(vk) for vk in group) for group in self._groups)

    def reset(self) -> None:
        """Forget the current hold, after a key release the hook never saw."""
        self._active = False
        self._down.clear()

    def _hook_proc(self, code: int, wparam: int, lparam: int) -> int:
        if code == 0:
            event = ctypes.cast(lparam, ctypes.POINTER(win32.KBDLLHOOKSTRUCT)).contents
            if event.dwExtraInfo != win32.TURACO_EXTRA_INFO:
                try:
                    self._handle(event.vkCode, wparam in (win32.WM_KEYDOWN, win32.WM_SYSKEYDOWN))
                except Exception:
                    log.exception("hotkey handler failed")
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _handle(self, vk: int, is_down: bool) -> None:
        if vk not in self._keys:
            if is_down and self._active:
                self._active = False
                self._on_cancel()
            return

        if not is_down:
            self._down.discard(vk)
            if self._active:
                self._active = False
                self._on_release()
            return

        if vk in self._down:
            return  # auto-repeat
        # A key-up can be missed (lock screen, elevated window), so re-check
        # the keys we believe are held before trusting them.
        self._down = {key for key in self._down if win32.is_key_down(key)}
        self._down.add(vk)
        if not self._active and all(group & self._down for group in self._groups):
            self._active = True
            if self._uses_win:
                win32.send_keys((MASK_VK, True), (MASK_VK, False))
            self._on_press()
