from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable
import unicodedata


POSITION_EMOJIS = {
    "zachranar": "🚑",
    "doktor": "🩺",
    "ochranka": "🛡️",
}

_NAME_LABELS = {
    "jmeno a prijmeni",
    "jmeno prijmeni",
}
_POSITION_LABELS = {
    "pozice",
    "pozice o kterou si zadate",
    "pozice o kterou zadate",
    "pozice o kterou se uchazite",
}
_BIRTH_DATE_LABELS = {
    "datum narozeni",
}
_PHONE_NUMBER_LABELS = {
    "telefonni cislo",
    "telefon",
}
_REQUEST_CHANNEL_LABELS = {
    "kanal zadosti",
    "kanal se zadosti",
    "zadost",
}
_USER_LABELS = {
    "uzivatel",
    "zamestnanec",
    "clen",
}
_DESCRIPTION_LABEL = re.compile(
    r"^\s*(?:\*\*|__)?\s*(?P<label>.+?)\s*:\s*(?:\*\*|__)?\s*(?P<value>.*?)\s*$"
)
_CHANNEL_MENTION = re.compile(r"<#(?P<channel_id>\d{15,22})>")
_CHANNEL_LINK = re.compile(
    r"https?://(?:www\.)?(?:discord(?:app)?\.com)/channels/\d{15,22}/(?P<channel_id>\d{15,22})",
    re.IGNORECASE,
)
_RAW_CHANNEL_ID = re.compile(r"^\s*(?P<channel_id>\d{15,22})\s*$")
_MEMBER_MENTION = re.compile(r"<@!?(?P<member_id>\d{15,22})>")
_RAW_MEMBER_ID = re.compile(r"^\s*(?P<member_id>\d{15,22})\s*$")
_TICKET_NUMBER = re.compile(r"\b(?:ticket|zadost)\s*[-_#]?\s*(?P<number>\d+)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TicketForm:
    full_name: str
    position: str
    birth_date: str = ""
    phone_number: str = ""


@dataclass(frozen=True, slots=True)
class RequestChannelReference:
    channel_id: int | None = None
    channel_name: str | None = None


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _fold(value)).strip()


def _clean_value(value: Any) -> str:
    text = str(value or "").replace("\u200b", " ").strip()
    text = text.strip("` ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _canonical_label(value: str) -> str | None:
    label = _normalized_key(value)
    if label in _NAME_LABELS:
        return "full_name"
    if label in _POSITION_LABELS:
        return "position"
    if label in _BIRTH_DATE_LABELS:
        return "birth_date"
    if label in _PHONE_NUMBER_LABELS:
        return "phone_number"
    if label in _REQUEST_CHANNEL_LABELS:
        return "request_channel"
    if label in _USER_LABELS:
        return "user"
    return None


def _description_values(description: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    waiting_for: str | None = None

    for raw_line in (description or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        label_match = _DESCRIPTION_LABEL.match(line)
        if label_match:
            canonical = _canonical_label(label_match.group("label"))
            if canonical:
                inline_value = _clean_value(label_match.group("value"))
                if inline_value:
                    values.setdefault(canonical, inline_value)
                    waiting_for = None
                else:
                    waiting_for = canonical
                continue
            waiting_for = None

        if waiting_for:
            values.setdefault(waiting_for, _clean_value(line))
            waiting_for = None

    return values


def _embed_values(embeds: Iterable[Any]) -> dict[str, str]:
    values: dict[str, str] = {}

    for embed in embeds:
        for field in getattr(embed, "fields", ()) or ():
            canonical = _canonical_label(str(getattr(field, "name", "")))
            field_value = _clean_value(getattr(field, "value", ""))
            if canonical and field_value:
                values.setdefault(canonical, field_value)

        for key, value in _description_values(getattr(embed, "description", None)).items():
            values.setdefault(key, value)

    return values


def parse_ticket_form(embeds: Iterable[Any]) -> TicketForm | None:
    """Find the applicant's basic information across one or more Discord embeds."""

    values = _embed_values(embeds)

    full_name = values.get("full_name", "").strip()
    position = values.get("position", "").strip()
    if not full_name or not position:
        return None
    return TicketForm(
        full_name=full_name,
        position=position,
        birth_date=values.get("birth_date", "").strip(),
        phone_number=values.get("phone_number", "").strip(),
    )


def parse_request_channel_reference(
    embeds: Iterable[Any],
    request_channel_prefixes: Iterable[str] = ("zadost-",),
) -> RequestChannelReference | None:
    """Read the selected application channel from an employee-folder embed."""

    raw_value = _embed_values(embeds).get("request_channel", "").strip()
    if not raw_value:
        return None

    channel_id: int | None = None
    for pattern in (_CHANNEL_MENTION, _CHANNEL_LINK, _RAW_CHANNEL_ID):
        match = pattern.search(raw_value)
        if match:
            channel_id = int(match.group("channel_id"))
            break

    channel_name: str | None = None
    prefixes = tuple(
        prefix.strip().casefold()
        for prefix in request_channel_prefixes
        if prefix and prefix.strip()
    )

    folded_value = _fold(raw_value)
    for prefix in prefixes:
        name_match = re.search(
            rf"(?<![a-z0-9_-])#?(?P<name>{re.escape(_fold(prefix))}[a-z0-9_-]+)(?![a-z0-9_-])",
            folded_value,
        )
        if name_match:
            channel_name = name_match.group("name")
            break

    if channel_name is None:
        ticket_match = _TICKET_NUMBER.search(folded_value)
        if ticket_match and prefixes:
            channel_name = f"{prefixes[0]}{ticket_match.group('number')}"

    if channel_id is None and channel_name is None:
        return None
    return RequestChannelReference(channel_id=channel_id, channel_name=channel_name)


def parse_selected_member_id(embeds: Iterable[Any]) -> int | None:
    """Read the Discord member selected in the employee-folder form."""

    raw_value = _embed_values(embeds).get("user", "").strip()
    if not raw_value:
        return None

    for pattern in (_MEMBER_MENTION, _RAW_MEMBER_ID):
        match = pattern.search(raw_value)
        if match:
            return int(match.group("member_id"))
    return None


def slugify_person_name(full_name: str) -> str:
    folded = _fold(_clean_value(full_name))
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")


_NAME_TITLES = {
    "bc",
    "dr",
    "ing",
    "judr",
    "md",
    "mgr",
    "mudr",
    "phdr",
    "phd",
}


def abbreviate_person_name(full_name: str) -> str:
    """Return a FiveRoster suggestion such as ``F. Lakatoš``."""

    parts = [part.strip() for part in _clean_value(full_name).split() if part.strip()]
    while len(parts) > 1 and _normalized_key(parts[0]) in _NAME_TITLES:
        parts.pop(0)

    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][:100]

    first_character = parts[0][0].upper()
    return f"{first_character}. {parts[-1]}"[:100]


def emoji_for_position(position: str) -> str | None:
    return POSITION_EMOJIS.get(_normalized_key(position))


def build_channel_name(
    ticket_form: TicketForm,
    separator: str = "・",
    limit: int = 100,
) -> str | None:
    emoji = emoji_for_position(ticket_form.position)
    slug = slugify_person_name(ticket_form.full_name)
    if not emoji or not slug:
        return None

    prefix = f"{emoji}{separator}"
    maximum_slug_length = limit - len(prefix)
    if maximum_slug_length < 1:
        return None

    shortened_slug = slug[:maximum_slug_length].rstrip("-")
    if not shortened_slug:
        return None
    return f"{prefix}{shortened_slug}"
