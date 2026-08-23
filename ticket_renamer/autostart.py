from __future__ import annotations

import sys

try:
    import winreg as _winreg
except ImportError:  # Windows registry is intentionally unavailable on Linux.
    _winreg = None


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DiscordTicketRenamer"


def _command() -> str:
    return f'"{sys.executable}"'


def _registry():
    if _winreg is None:
        raise OSError("Automatické spuštění v registru je dostupné jen ve Windows.")
    return _winreg


def is_enabled() -> bool:
    if _winreg is None:
        return False
    registry = _registry()
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = registry.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return False
    return str(value).strip().casefold() == _command().casefold()


def enable() -> None:
    registry = _registry()
    with registry.CreateKeyEx(
        registry.HKEY_CURRENT_USER,
        RUN_KEY,
        access=registry.KEY_SET_VALUE,
    ) as key:
        registry.SetValueEx(key, VALUE_NAME, 0, registry.REG_SZ, _command())


def disable() -> None:
    registry = _registry()
    try:
        with registry.OpenKey(
            registry.HKEY_CURRENT_USER,
            RUN_KEY,
            access=registry.KEY_SET_VALUE,
        ) as key:
            registry.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return
