from __future__ import annotations

import asyncio
import ctypes
import sys

from ticket_renamer.bot import TicketRenamerClient
from ticket_renamer.config import Settings
from ticket_renamer.controller import BotController
from ticket_renamer.tray import TrayApplication, create_tray_image


ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\DiscordTicketRenamerTray"


def show_message(title: str, message: str, *, error: bool = False) -> None:
    flags = 0x00000000 | (0x00000010 if error else 0x00000040)
    ctypes.windll.user32.MessageBoxW(None, message, title, flags)


class SingleInstance:
    def __init__(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        self._kernel32 = kernel32
        self._handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def self_test() -> None:
    settings = Settings.from_environment()
    image = create_tray_image()
    if image.size != (64, 64):
        raise RuntimeError("Tray ikona nebyla vytvořena správně.")
    BotController(settings, lambda event: None)

    async def validate_discord_client() -> None:
        client = TicketRenamerClient(settings)
        try:
            for command_name in ("sluzebni-panel", "doplnit-zadost"):
                if client.tree.get_command(command_name) is None:
                    raise RuntimeError(
                        f"Příkaz /{command_name} nebyl zaregistrován."
                    )
        finally:
            await client.close()

    asyncio.run(validate_discord_client())


def main() -> None:
    if "--self-test" in sys.argv[1:] or "--check-config" in sys.argv[1:]:
        self_test()
        return

    settings = Settings.from_environment()
    instance = SingleInstance()
    if instance.already_running:
        instance.close()
        show_message(
            "Discord Ticket Renamer",
            "Aplikace už běží v oznamovací oblasti u hodin.",
        )
        return

    try:
        TrayApplication(settings).run()
    finally:
        instance.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        show_message(
            "Discord Ticket Renamer – chyba",
            f"Aplikaci nelze spustit:\n\n{exc}",
            error=True,
        )
        raise
