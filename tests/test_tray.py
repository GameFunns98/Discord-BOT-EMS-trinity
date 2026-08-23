from threading import Lock
import unittest

from ticket_renamer.events import AppEvent, EventLevel
from ticket_renamer.tray import TrayApplication, create_tray_image


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


if __name__ == "__main__":
    unittest.main()
