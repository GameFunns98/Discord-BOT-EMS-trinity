import logging
import os
import unittest
from unittest.mock import patch

from ticket_renamer.config import Settings


class SettingsTests(unittest.TestCase):
    def _base_environment(self) -> dict[str, str]:
        return {
            "DISCORD_BOT_TOKEN": "test-token",
            "TICKET_TOOL_BOT_IDS": "1325579039888511056",
            "TICKET_CATEGORY_IDS": "1511618288373858435",
        }

    def test_new_request_settings_have_safe_defaults(self) -> None:
        with (
            patch.dict(os.environ, self._base_environment(), clear=True),
            patch("ticket_renamer.config.load_dotenv"),
        ):
            settings = Settings.from_environment()

        self.assertEqual(settings.request_channel_prefixes, ("zadost-",))
        self.assertEqual(settings.request_history_limit, 100)
        self.assertEqual(settings.log_level, logging.INFO)
        self.assertFalse(settings.fiveroster_enabled)
        self.assertEqual(
            settings.onboarding_operator_role_ids,
            frozenset({1526254418784424168}),
        )
        self.assertIn(1480275535002206411, settings.onboarding_add_role_ids)
        self.assertEqual(
            settings.onboarding_remove_role_ids,
            frozenset({1480275608083632381}),
        )

    def test_request_prefixes_can_be_configured(self) -> None:
        environment = self._base_environment()
        environment["REQUEST_CHANNEL_PREFIXES"] = "zadost-,nabor-"

        with (
            patch.dict(os.environ, environment, clear=True),
            patch("ticket_renamer.config.load_dotenv"),
        ):
            settings = Settings.from_environment()

        self.assertEqual(settings.request_channel_prefixes, ("zadost-", "nabor-"))

    def test_fiveroster_requires_key_and_roster_together(self) -> None:
        environment = self._base_environment()
        environment["FIVEROSTER_API_KEY"] = "secret"
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("ticket_renamer.config.load_dotenv"),
        ):
            with self.assertRaisesRegex(RuntimeError, "musi byt vyplneny oba"):
                Settings.from_environment()

    def test_fiveroster_rank_names_and_roles_are_configurable(self) -> None:
        environment = self._base_environment()
        environment.update(
            {
                "FIVEROSTER_API_KEY": "secret",
                "FIVEROSTER_ROSTER_UUID": "roster-uuid",
                "FIVEROSTER_RANK_ACADEMY": "EMS Akademie",
                "ONBOARDING_OPERATOR_ROLE_IDS": "111111111111111111,222222222222222222",
            }
        )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("ticket_renamer.config.load_dotenv"),
        ):
            settings = Settings.from_environment()

        self.assertTrue(settings.fiveroster_enabled)
        self.assertEqual(settings.fiveroster_rank_names["academy"], "EMS Akademie")
        self.assertEqual(
            settings.onboarding_operator_role_ids,
            frozenset({111111111111111111, 222222222222222222}),
        )


if __name__ == "__main__":
    unittest.main()
