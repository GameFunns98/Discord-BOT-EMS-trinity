from __future__ import annotations

import io
import logging
import os
import unittest
from unittest.mock import patch

from ticket_renamer.logging_config import (
    REDACTED,
    RAW_PAYLOAD_HIDDEN,
    configure_logging,
    redact_text,
)


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class LoggingConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in tuple(root.handlers):
            if getattr(handler, "ticket_renamer_console_handler", False):
                root.removeHandler(handler)
                handler.close()
        for name in ("discord", "discord.http", "discord.gateway", "aiohttp"):
            logging.getLogger(name).setLevel(logging.NOTSET)

    def test_redacts_auth_headers_api_keys_webhooks_and_known_values(self) -> None:
        original = (
            "Authorization: Bot abcdefghij x-api-key='key-12345' "
            "https://discord.com/api/webhooks/123456789/token-value "
            "known-super-secret"
        )

        result = redact_text(original, ("known-super-secret",))

        self.assertNotIn("abcdefghij", result)
        self.assertNotIn("key-12345", result)
        self.assertNotIn("token-value", result)
        self.assertNotIn("known-super-secret", result)
        self.assertGreaterEqual(result.count(REDACTED), 4)

    def test_redacts_interaction_callback_token_and_discord_snowflakes(self) -> None:
        interaction_id = "1234567890123456789"
        member_id = "987654321098765432"
        callback_token = "interaction-secret-token"
        original = (
            "POST https://discord.com/api/v10/interactions/"
            f"{interaction_id}/{callback_token}/callback for member {member_id}"
        )

        result = redact_text(original)

        self.assertNotIn(interaction_id, result)
        self.assertNotIn(member_id, result)
        self.assertNotIn(callback_token, result)
        self.assertGreaterEqual(result.count(REDACTED), 3)

    def test_hides_raw_mapping_and_payload_string(self) -> None:
        self.assertEqual(redact_text({"token": "secret"}), RAW_PAYLOAD_HIDDEN)
        self.assertEqual(redact_text('{"content": "private"}'), RAW_PAYLOAD_HIDDEN)
        self.assertNotIn("private", redact_text('payload={"content": "private"}'))
        self.assertNotIn("private", redact_text('HTTP 400: {"content": "private"}'))

    def test_redacts_generic_authorization_header_and_short_known_secret(self) -> None:
        numeric_secret = "prefix123456789012345678suffix"
        result = redact_text(
            f'Authorization: "Basic abc123"; value=s3 other={numeric_secret}',
            ("s3", numeric_secret),
        )

        self.assertNotIn("abc123", result)
        self.assertNotIn("s3", result)
        self.assertNotIn("prefix", result)
        self.assertNotIn("suffix", result)

    def test_configure_logging_clamps_chatty_libraries_even_in_debug(self) -> None:
        stream = io.StringIO()
        configure_logging(logging.DEBUG, stream=stream)

        for name in ("discord", "discord.http", "discord.gateway", "aiohttp"):
            self.assertGreaterEqual(logging.getLogger(name).level, logging.WARNING)

        logging.getLogger("discord.http").debug("GET payload=%s", {"secret": "value"})
        self.assertEqual(stream.getvalue(), "")

    def test_console_filter_redacts_arguments_and_exception_text(self) -> None:
        stream = io.StringIO()
        configure_logging(
            logging.INFO,
            stream=stream,
            known_secrets=("known-secret-value",),
        )
        logger = logging.getLogger("ticket_renamer.test")

        try:
            raise RuntimeError("Bearer exception-secret-token")
        except RuntimeError:
            logger.exception("Failure for %s", "known-secret-value")

        output = stream.getvalue()
        self.assertIn("Failure for [SKRYTO]", output)
        self.assertNotIn("known-secret-value", output)
        self.assertNotIn("exception-secret-token", output)

    def test_console_filter_never_serializes_structured_log_arguments(self) -> None:
        stream = io.StringIO()
        configure_logging(logging.INFO, stream=stream)

        logging.getLogger("ticket_renamer.test").warning(
            "API players: %s",
            ["111111111111111111", "222222222222222222"],
        )

        output = stream.getvalue()
        self.assertIn(RAW_PAYLOAD_HIDDEN, output)
        self.assertNotIn("111111111111111111", output)
        self.assertNotIn("222222222222222222", output)

    def test_tty_is_colored_but_plain_stream_is_not(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            colored = TtyBuffer()
            configure_logging(logging.INFO, stream=colored)
            logging.getLogger("ticket_renamer.test").info("hotovo")
            self.assertIn("\x1b[32m", colored.getvalue())

            plain = io.StringIO()
            configure_logging(logging.INFO, stream=plain)
            logging.getLogger("ticket_renamer.test").info("hotovo")
            self.assertNotIn("\x1b[", plain.getvalue())

    def test_no_color_environment_disables_tty_color(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            stream = TtyBuffer()
            configure_logging(logging.INFO, stream=stream)
            logging.getLogger("ticket_renamer.test").info("hotovo")

            self.assertNotIn("\x1b[", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
