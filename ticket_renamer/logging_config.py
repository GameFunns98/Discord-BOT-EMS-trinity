"""Safe, concise logging for the console and systemd journal.

The bot processes personal data and authentication credentials.  This module
therefore treats log redaction as a boundary: messages are rendered and
sanitised before a handler writes them anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import logging
import os
import re
import sys
from types import TracebackType
from typing import TextIO


REDACTED = "[SKRYTO]"
RAW_PAYLOAD_HIDDEN = "[RAW PAYLOAD SKRYT]"

_NOISY_LOGGERS = (
    "discord",
    "discord.http",
    "discord.gateway",
    "aiohttp",
)

_AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(?P<scheme>Bot|Bearer)\s+"
    r"(?P<credential>[A-Za-z0-9._~+/=-]{6,})"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b(?P<key>authorization)(?P<separator>\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|"
    r"(?:(?:Bot|Bearer|Basic)\s+)?[^\s,;]+)"
)
_SECRET_FIELD_RE = re.compile(
    r"(?ix)"
    r"(?P<key>"
    r"authorization|x-api-key|api[_-]?key|"
    r"discord_bot_token|fiveroster_api_key|"
    r"webhook[_-]?token|access[_-]?token"
    r")"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<quote>['\"]?)"
    r"""(?P<value>[^\s,;'\"]+)"""
    r"(?P=quote)"
)
_WEBHOOK_URL_RE = re.compile(
    r"(?i)(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api(?:/v\d+)?/"
    r"webhooks/\d+/)([^/?#\s]+)"
)
_INTERACTION_URL_RE = re.compile(
    r"(?i)(https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api(?:/v\d+)?/"
    r"interactions/\d+/)([^/?#\s]+)(/callback\b)"
)
_DISCORD_SNOWFLAKE_RE = re.compile(r"(?<!\d)\d{17,20}(?!\d)")
_RAW_PAYLOAD_RE = re.compile(
    r"(?is)\b(payload|request\s+body|response\s+body|gateway\s+event)"
    r"\s*[:=]\s*(\{.*|\[.*)"
)
_EMBEDDED_JSON_RE = re.compile(
    r"(?s)(?:\{\s*['\"][^'\"]+['\"]\s*:.*\}|"
    r"\[\s*(?:\{\s*['\"][^'\"]+['\"]\s*:|['\"]).*\])"
)

_LEVEL_COLORS = {
    logging.DEBUG: "\x1b[36m",
    logging.INFO: "\x1b[32m",
    logging.WARNING: "\x1b[33m",
    logging.ERROR: "\x1b[31m",
    logging.CRITICAL: "\x1b[1;31m",
}
_RESET_COLOR = "\x1b[0m"


def _looks_like_json_payload(value: str) -> bool:
    stripped = value.lstrip()
    if not stripped or stripped[0] not in "[{":
        return False
    # Raw Discord payloads nearly always contain JSON keys.  Avoid hiding an
    # ordinary human sentence merely because it starts with a bracket.
    if stripped[0] == "{":
        return bool(re.match(r"\{\s*['\"][^'\"]+['\"]\s*:", stripped))
    return bool(re.match(r"\[\s*(?:\{|['\"])", stripped))


def redact_text(value: object, known_secrets: Iterable[str] = ()) -> str:
    """Return a printable value with credentials and raw payloads removed."""

    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    ):
        return RAW_PAYLOAD_HIDDEN

    text = str(value)
    if _looks_like_json_payload(text):
        return RAW_PAYLOAD_HIDDEN

    text = _RAW_PAYLOAD_RE.sub(lambda match: f"{match.group(1)}={RAW_PAYLOAD_HIDDEN}", text)
    text = _EMBEDDED_JSON_RE.sub(RAW_PAYLOAD_HIDDEN, text)
    text = _WEBHOOK_URL_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _INTERACTION_URL_RE.sub(
        lambda match: f"{match.group(1)}{REDACTED}{match.group(3)}",
        text,
    )
    text = _AUTH_HEADER_RE.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}{REDACTED}",
        text,
    )
    text = _AUTH_SCHEME_RE.sub(
        lambda match: f"{match.group('scheme')} {REDACTED}",
        text,
    )
    text = _SECRET_FIELD_RE.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}{REDACTED}",
        text,
    )
    # Longest first prevents a shorter credential from partially exposing a
    # longer one.  Values supplied here are explicitly declared as secrets, so
    # even a short test/development credential must never be printed.
    secrets = sorted(
        {str(secret) for secret in known_secrets if secret},
        key=len,
        reverse=True,
    )
    for secret in secrets:
        text = text.replace(secret, REDACTED)
    # Discord snowflakes are stable identifiers and therefore personal data
    # when they identify an employee.  They are never needed in persistent
    # logs; short counters, HTTP statuses and ordinary timestamps remain intact.
    text = _DISCORD_SNOWFLAKE_RE.sub(REDACTED, text)
    return text


class SensitiveDataFilter(logging.Filter):
    """Render log arguments once and replace sensitive material in-place."""

    def __init__(self, known_secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.known_secrets = tuple(secret for secret in known_secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = self._secrets()
        if isinstance(record.msg, Mapping) or (
            isinstance(record.msg, Sequence)
            and not isinstance(record.msg, (str, bytes, bytearray))
        ):
            rendered = RAW_PAYLOAD_HIDDEN
        elif isinstance(record.args, Mapping):
            # Named logging arguments are themselves a mapping.  Keeping the
            # format string is useful context, but the supplied payload must
            # not be serialised into the log.
            rendered = f"{record.msg} {RAW_PAYLOAD_HIDDEN}"
        else:
            try:
                safe_args = tuple(
                    RAW_PAYLOAD_HIDDEN
                    if isinstance(argument, Mapping)
                    or (
                        isinstance(argument, Sequence)
                        and not isinstance(argument, (str, bytes, bytearray))
                    )
                    else argument
                    for argument in record.args
                )
                rendered = str(record.msg) % safe_args if record.args else str(record.msg)
            except Exception:
                rendered = "[NEPLATNA LOGOVACI ZPRAVA]"
        record.msg = redact_text(rendered, secrets)
        record.args = ()
        return True

    def _secrets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.known_secrets, *secret_values_from_environment())))


class ConciseFormatter(logging.Formatter):
    """Small terminal-friendly formatter with safe exception rendering."""

    def __init__(
        self,
        *,
        use_color: bool = False,
        known_secrets: Iterable[str] = (),
    ) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self.use_color = use_color
        self.known_secrets = tuple(known_secrets)

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        level = record.levelname[:5].ljust(5)
        area = record.name.removeprefix("ticket_renamer.") or "app"
        secrets = self._secrets()
        message = redact_text(record.getMessage(), secrets)
        line = f"{timestamp} {level} {area} | {message}"

        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            line = f"{line}\n{redact_text(record.stack_info, secrets)}"

        if self.use_color:
            color = _LEVEL_COLORS.get(record.levelno, "")
            if color:
                return f"{color}{line}{_RESET_COLOR}"
        return line

    def formatException(
        self,
        ei: tuple[type[BaseException], BaseException, TracebackType | None],
    ) -> str:
        return redact_text(super().formatException(ei), self._secrets())

    def _secrets(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.known_secrets, *secret_values_from_environment())))


def _stream_supports_color(stream: TextIO) -> bool:
    if os.getenv("NO_COLOR") is not None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def clamp_third_party_loggers() -> None:
    """Prevent protocol-level Discord/aiohttp traffic from reaching handlers."""

    for name in _NOISY_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET or logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)


def configure_logging(
    level: int = logging.INFO,
    *,
    stream: TextIO | None = None,
    known_secrets: Iterable[str] = (),
) -> logging.Handler:
    """Configure one safe console handler and return it.

    Calling the function repeatedly replaces only our own console handler and
    leaves the Windows rotating-file handler (and test capture handlers) alone.
    """

    target_stream = stream or sys.stderr
    secrets = tuple(secret for secret in known_secrets if secret)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in tuple(root.handlers):
        if getattr(handler, "ticket_renamer_console_handler", False):
            root.removeHandler(handler)
            handler.close()

    handler = logging.StreamHandler(target_stream)
    handler.ticket_renamer_console_handler = True  # type: ignore[attr-defined]
    handler.setLevel(level)
    handler.addFilter(SensitiveDataFilter(secrets))
    handler.setFormatter(
        ConciseFormatter(
            use_color=_stream_supports_color(target_stream),
            known_secrets=secrets,
        )
    )
    root.addHandler(handler)
    clamp_third_party_loggers()
    return handler


def secret_values_from_environment(
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Collect only known credential values without ever displaying them."""

    source = os.environ if environment is None else environment
    names = (
        "DISCORD_BOT_TOKEN",
        "FIVEROSTER_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    )
    return tuple(value for name in names if (value := source.get(name, "").strip()))
