import logging
import os
from pathlib import Path
from threading import Lock
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ticket_renamer.events import AppEvent, EventLevel
from ticket_renamer.tray import TrayApplication, configure_file_logging, create_tray_image


class FakeIcon:
    def __init__(self) -> None:
        self.icon = None
        self.notifications: list[tuple[str, str]] = []
        self.menu_updates = 0

    def update_menu(self) -> None:
        self.menu_updates += 1

    def notify(self, message: str, title: str) -> None:
        self.notifications.append((title, message))


class TrayTests(unittest.TestCase):
    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in tuple(root.handlers):
            if getattr(handler, "ticket_renamer_handler", False):
                root.removeHandler(handler)
                handler.close()
        for name in ("discord", "discord.http", "discord.gateway", "aiohttp"):
            logging.getLogger(name).setLevel(logging.NOTSET)

    def _application(self) -> TrayApplication:
        application = TrayApplication.__new__(TrayApplication)
        application._state_lock = Lock()
        application._status = "Připojování…"
        application._last_event = "Start"
        application._notification_times = {}
        application._closing = False
        application.icon = FakeIcon()
        return application

    def test_creates_medical_tray_icon(self) -> None:
        image = create_tray_image("#22C55E")

        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")

    def test_success_event_updates_status_and_notifies(self) -> None:
        application = self._application()

        application.handle_event(
            AppEvent(
                EventLevel.SUCCESS,
                "Ticket přejmenován",
                "ticket-123 → 🚑・jackob-white",
                status="Připojeno",
            )
        )

        self.assertEqual(application._status, "Připojeno")
        self.assertEqual(application._last_event, "Ticket přejmenován")
        self.assertEqual(
            application.icon.notifications,
            [("Ticket přejmenován", "ticket-123 → 🚑・jackob-white")],
        )
        self.assertEqual(application.icon.menu_updates, 1)

    def test_repeated_identical_notification_is_throttled(self) -> None:
        application = self._application()
        event = AppEvent(
            EventLevel.ERROR,
            "Chybí oprávnění",
            "Bot nemůže spravovat kanál.",
            status="Chyba oprávnění",
        )

        application.handle_event(event)
        application.handle_event(event)

        self.assertEqual(len(application.icon.notifications), 1)

    def test_real_tray_file_log_excludes_event_detail_and_redacts_identifiers(self) -> None:
        employee_id = "123456789012345678"
        secret = "tray-secret-token"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with (
                patch("ticket_renamer.tray.application_directory", return_value=directory),
                patch.dict(os.environ, {"DISCORD_BOT_TOKEN": secret}, clear=False),
            ):
                log_path = configure_file_logging(
                    SimpleNamespace(log_level=logging.INFO)  # type: ignore[arg-type]
                )
                application = self._application()
                application.handle_event(
                    AppEvent(
                        EventLevel.SUCCESS,
                        "Směna zahájena",
                        f"Citlivé jméno zaměstnance {employee_id}",
                        status="Připojeno",
                        notify=False,
                    )
                )
                logging.getLogger("ticket_renamer.test").warning(
                    "Authorization: Bot %s member=%s",
                    secret,
                    employee_id,
                )
                for handler in logging.getLogger().handlers:
                    handler.flush()
                output = log_path.read_text(encoding="utf-8")
                for handler in tuple(logging.getLogger().handlers):
                    if getattr(handler, "ticket_renamer_handler", False):
                        logging.getLogger().removeHandler(handler)
                        handler.close()

        self.assertIn("Směna zahájena", output)
        self.assertNotIn("Citlivé jméno", output)
        self.assertNotIn(employee_id, output)
        self.assertNotIn(secret, output)
        self.assertIn("[SKRYTO]", output)


if __name__ == "__main__":
    unittest.main()
