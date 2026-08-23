from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re

from dotenv import load_dotenv

from .paths import application_directory


def _environment_file() -> Path:
    """Return the .env next to the EXE, or in the source project root."""

    return application_directory() / ".env"


def _required_text(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"V souboru .env chybi hodnota {name}.")
    return value


def _snowflake_ids(name: str, default: str | None = None) -> frozenset[int]:
    raw_value = os.getenv(name, default or "").strip()
    if not raw_value:
        raise RuntimeError(f"V souboru .env chybi hodnota {name}.")
    result: set[int] = set()

    for raw_item in raw_value.replace(";", ",").split(","):
        item = raw_item.strip()
        if not item:
            continue
        if not item.isdecimal() or int(item) <= 0:
            raise RuntimeError(
                f"{name} musi obsahovat pouze Discord ID oddelena carkou."
            )
        result.add(int(item))

    if not result:
        raise RuntimeError(f"V souboru .env chybi platne Discord ID v {name}.")
    return frozenset(result)


def _optional_text(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _text(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise RuntimeError(f"V souboru .env chybi hodnota {name}.")
    if "\n" in value or "\r" in value or len(value) > 100:
        raise RuntimeError(f"{name} musi byt jeden radek o delce nejvyse 100 znaku.")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    normalized = raw_value.strip().casefold()
    if normalized in {"1", "true", "yes", "ano", "on"}:
        return True
    if normalized in {"0", "false", "no", "ne", "off"}:
        return False
    raise RuntimeError(f"{name} musi byt true nebo false.")


def _bounded_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} musi byt cele cislo.") from exc

    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} musi byt v rozsahu {minimum}-{maximum}.")
    return value


def _channel_prefixes(name: str, default: str) -> tuple[str, ...]:
    raw_value = os.getenv(name, default)
    prefixes: list[str] = []

    for raw_item in raw_value.replace(";", ",").split(","):
        item = raw_item.strip().casefold()
        if not item:
            continue
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", item):
            raise RuntimeError(
                f"{name} musi obsahovat prefixy kanalu oddelene carkou, napriklad zadost-."
            )
        prefixes.append(item)

    if not prefixes:
        raise RuntimeError(f"V souboru .env chybi platny prefix v {name}.")
    return tuple(dict.fromkeys(prefixes))


@dataclass(frozen=True, slots=True)
class Settings:
    discord_bot_token: str
    ticket_tool_bot_ids: frozenset[int]
    ticket_category_ids: frozenset[int]
    request_channel_prefixes: tuple[str, ...]
    request_history_limit: int
    channel_separator: str
    scan_existing_tickets: bool
    scan_history_limit: int
    log_level: int
    fiveroster_api_key: str | None = None
    fiveroster_roster_uuid: str | None = None
    fiveroster_rank_paramedic: str = "Paramedic"
    fiveroster_rank_academy: str = "Akademie"
    fiveroster_rank_doctor: str = "Doktor"
    fiveroster_rank_doctor_training: str = "Doktor v zácviku"
    fiveroster_rank_security: str = "Security"
    onboarding_operator_role_ids: frozenset[int] = frozenset({1526254418784424168})
    onboarding_add_role_ids: frozenset[int] = frozenset(
        {
            1480275535002206411,
            1480681451870486651,
            1480680971660693677,
            1480680584450936873,
            1480680465957654770,
            1480353010273222807,
        }
    )
    onboarding_remove_role_ids: frozenset[int] = frozenset({1480275608083632381})

    @property
    def fiveroster_enabled(self) -> bool:
        return bool(self.fiveroster_api_key and self.fiveroster_roster_uuid)

    @property
    def fiveroster_rank_names(self) -> dict[str, str]:
        return {
            "paramedic": self.fiveroster_rank_paramedic,
            "academy": self.fiveroster_rank_academy,
            "doctor": self.fiveroster_rank_doctor,
            "doctor_training": self.fiveroster_rank_doctor_training,
            "security": self.fiveroster_rank_security,
        }

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(_environment_file())

        fiveroster_api_key = _optional_text("FIVEROSTER_API_KEY")
        fiveroster_roster_uuid = _optional_text("FIVEROSTER_ROSTER_UUID")
        if bool(fiveroster_api_key) != bool(fiveroster_roster_uuid):
            raise RuntimeError(
                "FIVEROSTER_API_KEY a FIVEROSTER_ROSTER_UUID musi byt vyplneny oba, nebo ani jeden."
            )

        separator = os.getenv("CHANNEL_SEPARATOR", "・").strip()
        if not separator or len(separator) > 4 or "\n" in separator or "\r" in separator:
            raise RuntimeError("CHANNEL_SEPARATOR musi mit 1 az 4 znaky a nesmi obsahovat novy radek.")

        log_level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        log_level = getattr(logging, log_level_name, None)
        if not isinstance(log_level, int):
            raise RuntimeError("LOG_LEVEL musi byt napriklad DEBUG, INFO, WARNING nebo ERROR.")

        return cls(
            discord_bot_token=_required_text("DISCORD_BOT_TOKEN"),
            ticket_tool_bot_ids=_snowflake_ids("TICKET_TOOL_BOT_IDS"),
            ticket_category_ids=_snowflake_ids("TICKET_CATEGORY_IDS"),
            request_channel_prefixes=_channel_prefixes(
                "REQUEST_CHANNEL_PREFIXES",
                "zadost-",
            ),
            request_history_limit=_bounded_integer(
                "REQUEST_HISTORY_LIMIT",
                100,
                1,
                1000,
            ),
            channel_separator=separator,
            scan_existing_tickets=_boolean("SCAN_EXISTING_TICKETS", False),
            scan_history_limit=_bounded_integer("SCAN_HISTORY_LIMIT", 25, 1, 100),
            log_level=log_level,
            fiveroster_api_key=fiveroster_api_key,
            fiveroster_roster_uuid=fiveroster_roster_uuid,
            fiveroster_rank_paramedic=_text("FIVEROSTER_RANK_PARAMEDIC", "Paramedic"),
            fiveroster_rank_academy=_text("FIVEROSTER_RANK_ACADEMY", "Akademie"),
            fiveroster_rank_doctor=_text("FIVEROSTER_RANK_DOCTOR", "Doktor"),
            fiveroster_rank_doctor_training=_text(
                "FIVEROSTER_RANK_DOCTOR_TRAINING",
                "Doktor v zácviku",
            ),
            fiveroster_rank_security=_text("FIVEROSTER_RANK_SECURITY", "Security"),
            onboarding_operator_role_ids=_snowflake_ids(
                "ONBOARDING_OPERATOR_ROLE_IDS",
                "1526254418784424168",
            ),
            onboarding_add_role_ids=_snowflake_ids(
                "ONBOARDING_ADD_ROLE_IDS",
                (
                    "1480275535002206411,1480681451870486651,"
                    "1480680971660693677,1480680584450936873,"
                    "1480680465957654770,1480353010273222807"
                ),
            ),
            onboarding_remove_role_ids=_snowflake_ids(
                "ONBOARDING_REMOVE_ROLE_IDS",
                "1480275608083632381",
            ),
        )
