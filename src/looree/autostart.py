"""Start Looree when the user signs in, via the per-user Run registry key."""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE = "Looree"


def _command() -> str:
    # pythonw runs without a console window.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" -m looree'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _VALUE)
            return True
    except FileNotFoundError:
        return False


def set_enabled(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, _VALUE, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(key, _VALUE)
            except FileNotFoundError:
                pass
