from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from threading import Lock, Thread
import time

from PIL import Image, ImageDraw
import pystray

from . import __version__
from . import autostart
from .config import Settings
from .controller import BotController
from .events import AppEvent, EventLevel
from .paths import application_directory


LOGGER = logging.getLogger("ticket_renamer.tray")


STATUS_COLORS = {
    "Připojeno": "#22C55E",
    "Připojování…": "#F59E0B",
    "Obnovování spojení…": "#F59E0B",
    "Zastaveno": "#64748B",
}


def configure_file_logging(settings: Settings) -> Path:
    log_path = application_directory() / "TicketRenamer.log"
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    if not any(getattr(handler, "ticket_renamer_handler", False) for handler in root_logger.handlers):
        handler = RotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.ticket_renamer_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
            )
        )
        root_logger.addHandler(handler)

    return log_path


def create_tray_image(color: str = "#F59E0B") -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    drawing = ImageDraw.Draw(image)
    drawing.rounded_rectangle((4, 4, 60, 60), radius=15, fill="#111827")
    drawing.ellipse((10, 10, 54, 54), fill=color)
    drawing.rounded_rectangle((27, 17, 37, 47), radius=2, fill="white")
    drawing.rounded_rectangle((17, 27, 47, 37), radius=2, fill="white")
    return image


class TrayApplication:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log_path = configure_file_logging(settings)
        self._state_lock = Lock()
        self._status = "Připojování…"
        self._last_event = "Aplikace se spouští"
        self._notification_times: dict[tuple[str, str], float] = {}
        self._closing = False

        self.controller = BotController(settings, self.handle_event)
        self.icon = pystray.Icon(
            "discord-ticket-renamer",
            create_tray_image(),
            f"Discord Ticket Renamer {__version__}",
            menu=pystray.Menu(
                pystray.MenuItem(self._status_text, None, enabled=False),
                pystray.MenuItem(self._last_event_text, None, enabled=False),
                pystray.MenuItem(f"Verze: {__version__}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Test oznámení", self._test_notification),
                pystray.MenuItem("Restartovat bota", self._restart_bot),
                pystray.MenuItem("Otevřít log", self._open_log),
                pystray.MenuItem("Otevřít složku aplikace", self._open_directory),
                pystray.MenuItem(
                    "Spouštět při přihlášení",
                    self._toggle_autostart,
                    checked=lambda item: autostart.is_enabled(),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Ukončit", self._quit),
            ),
        )

    def run(self) -> None:
        LOGGER.info("Tray aplikace se spouští.")
        self.icon.run(setup=self._setup)
        LOGGER.info("Tray aplikace byla ukončena.")

    def _setup(self, icon: pystray.Icon) -> None:
        icon.visible = True
        self.controller.start()

    def _status_text(self, item: pystray.MenuItem) -> str:
        with self._state_lock:
            return f"Stav: {self._status}"

    def _last_event_text(self, item: pystray.MenuItem) -> str:
        with self._state_lock:
            return f"Poslední událost: {self._last_event[:60]}"

    def handle_event(self, event: AppEvent) -> None:
        level_to_log = {
            EventLevel.INFO: logging.INFO,
            EventLevel.SUCCESS: logging.INFO,
            EventLevel.WARNING: logging.WARNING,
            EventLevel.ERROR: logging.ERROR,
        }
        LOGGER.log(level_to_log[event.level], "%s: %s", event.title, event.message)

        with self._state_lock:
            if event.status:
                self._status = event.status
            self._last_event = event.title
            status = self._status

        self.icon.icon = create_tray_image(self._color_for_status(status))
        try:
            self.icon.update_menu()
        except Exception:
            LOGGER.exception("Nepodařilo se aktualizovat tray menu.")

        if event.notify and not self._closing and self._should_notify(event):
            try:
                self.icon.notify(event.message, event.title)
            except Exception:
                LOGGER.exception("Windows oznámení se nepodařilo zobrazit.")

    def _color_for_status(self, status: str) -> str:
        if "Chyba" in status or "chyba" in status:
            return "#EF4444"
        return STATUS_COLORS.get(status, "#F59E0B")

    def _should_notify(self, event: AppEvent) -> bool:
        key = (event.title, event.message)
        now = time.monotonic()
        previous = self._notification_times.get(key)
        self._notification_times[key] = now

        stale_keys = [
            notification_key
            for notification_key, timestamp in self._notification_times.items()
            if now - timestamp > 300
        ]
        for stale_key in stale_keys:
            self._notification_times.pop(stale_key, None)

        return previous is None or now - previous >= 30

    def _test_notification(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.handle_event(
            AppEvent(
                EventLevel.SUCCESS,
                "Test oznámení",
                "Windows oznámení Discord Ticket Renamer fungují.",
                status=self._status,
            )
        )

    def _restart_bot(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        if self._closing:
            return
        self.handle_event(
            AppEvent(
                EventLevel.INFO,
                "Restartování",
                "Discord bot se restartuje.",
                status="Připojování…",
                notify=False,
            )
        )
        Thread(target=self.controller.restart, name="BotRestart", daemon=True).start()

    def _open_log(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        try:
            self.log_path.touch(exist_ok=True)
            os.startfile(self.log_path)  # type: ignore[attr-defined]
        except Exception:
            LOGGER.exception("Log se nepodařilo otevřít.")
            self.icon.notify("Log se nepodařilo otevřít.", "Chyba")

    def _open_directory(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        try:
            os.startfile(application_directory())  # type: ignore[attr-defined]
        except Exception:
            LOGGER.exception("Složku aplikace se nepodařilo otevřít.")
            self.icon.notify("Složku aplikace se nepodařilo otevřít.", "Chyba")

    def _toggle_autostart(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        try:
            if autostart.is_enabled():
                autostart.disable()
                message = "Automatické spuštění bylo vypnuto."
            else:
                autostart.enable()
                message = "Aplikace se bude spouštět po přihlášení do Windows."
            self.icon.update_menu()
            self.icon.notify(message, "Automatické spuštění")
        except Exception:
            LOGGER.exception("Nastavení automatického spuštění selhalo.")
            self.icon.notify("Nastavení automatického spuštění selhalo.", "Chyba")

    def _quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        if self._closing:
            return
        self._closing = True

        def shutdown() -> None:
            self.controller.stop()
            self.icon.stop()

        Thread(target=shutdown, name="ApplicationShutdown", daemon=False).start()
