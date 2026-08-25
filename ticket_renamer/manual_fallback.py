from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import re
from typing import Awaitable, Callable, Iterable
import unicodedata

import discord

from .parser import TicketForm


MANUAL_FALLBACK_FOOTER_PREFIX = "Discord Ticket Renamer • Ruční žádost v1"
MANUAL_FALLBACK_HISTORY_LIMIT: int | None = None
MANUAL_POSITIONS = ("Záchranář", "Doktor", "Ochranka")

_MARKER = re.compile(
    r"state:(?P<state>[a-z]+)\s+•\s+source:(?P<source>[a-z]+)"
    r"\s+•\s+member:(?P<member_id>\d{1,22})"
    r"\s+•\s+creator:(?P<creator_id>\d{1,22})"
)
_DOB = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")
_PHONE_CHARACTERS = re.compile(r"^[+0-9() -]+$")


class ManualFallbackState(str, Enum):
    WAITING = "waiting"
    READY = "ready"
    DONE = "done"


class ManualFallbackSource(str, Enum):
    MANUAL = "manual"
    REQUEST = "request"


class ManualRecoveryAction(str, Enum):
    OPEN = "open"
    RETRY = "retry"


@dataclass(frozen=True, slots=True)
class ManualFallbackMarker:
    state: ManualFallbackState
    source: ManualFallbackSource
    member_id: int | None = None
    creator_id: int | None = None


@dataclass(frozen=True, slots=True)
class ManualFallbackRecord:
    marker: ManualFallbackMarker
    ticket_form: TicketForm | None = None
    reason: str = ""

    @property
    def is_authoritative(self) -> bool:
        return (
            self.marker.source is ManualFallbackSource.MANUAL
            and self.marker.state in {ManualFallbackState.READY, ManualFallbackState.DONE}
            and self.marker.member_id is not None
            and self.ticket_form is not None
        )


@dataclass(frozen=True, slots=True)
class ManualSubmission:
    source_message_id: int
    member_id: int
    member: object
    position: str
    full_name: str
    birth_date: str
    phone_number: str


class ManualValidationError(ValueError):
    pass


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(
        character for character in normalized if not unicodedata.combining(character)
    )


def _label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _fold(value)).strip()


def canonical_manual_position(value: str) -> str | None:
    normalized = _label(value).replace(" ", "")
    return {
        "zachranar": "Záchranář",
        "doktor": "Doktor",
        "ochranka": "Ochranka",
    }.get(normalized)


def validate_manual_ticket_form(
    full_name: str,
    birth_date: str,
    phone_number: str,
    position: str,
    *,
    today: date | None = None,
) -> TicketForm:
    cleaned_name = re.sub(r"[ \t]+", " ", str(full_name or "").strip())
    if not 2 <= len(cleaned_name) <= 100 or len(cleaned_name.split()) < 2:
        raise ManualValidationError("Zadejte jméno i příjmení (nejvýše 100 znaků).")
    if any(
        not (character.isalpha() or character in " -.'’")
        for character in cleaned_name
    ):
        raise ManualValidationError("Jméno obsahuje nepovolené znaky.")

    raw_birth_date = str(birth_date or "").strip()
    if not _DOB.fullmatch(raw_birth_date):
        raise ManualValidationError("Datum narození musí být ve formátu D.M.YYYY nebo DD.MM.YYYY.")
    try:
        parsed_birth_date = datetime.strptime(raw_birth_date, "%d.%m.%Y").date()
    except ValueError as exc:
        raise ManualValidationError("Datum narození není platné datum.") from exc
    upper_bound = today or date.today()
    if parsed_birth_date.year < 1900 or parsed_birth_date > upper_bound:
        raise ManualValidationError("Datum narození musí být mezi rokem 1900 a dneškem.")
    cleaned_birth_date = parsed_birth_date.strftime("%d.%m.%Y")

    cleaned_phone = re.sub(r"[ \t]+", " ", str(phone_number or "").strip())
    if not cleaned_phone or len(cleaned_phone) > 25 or not _PHONE_CHARACTERS.fullmatch(cleaned_phone):
        raise ManualValidationError("Telefonní číslo obsahuje nepovolené znaky.")
    if "+" in cleaned_phone and not cleaned_phone.startswith("+"):
        raise ManualValidationError("Znak + smí být pouze na začátku telefonního čísla.")
    if cleaned_phone.count("+") > 1:
        raise ManualValidationError("Telefonní číslo obsahuje více znaků +.")
    digit_count = sum(character.isdecimal() for character in cleaned_phone)
    if digit_count < 5 or digit_count > 15:
        raise ManualValidationError("Telefonní číslo musí obsahovat 5 až 15 číslic.")

    canonical_position = canonical_manual_position(position)
    if canonical_position is None:
        raise ManualValidationError("Vyberte podporovanou pozici.")

    return TicketForm(
        cleaned_name,
        canonical_position,
        birth_date=cleaned_birth_date,
        phone_number=cleaned_phone,
    )


def is_recognizable_folder_embed(embeds: Iterable[object]) -> bool:
    """Return true only for the Ticket Tool employee-folder form signature."""

    expected = {"uzivatel", "zamestnanec", "clen", "kanal zadosti", "kanal se zadosti"}
    for embed in embeds:
        for field in getattr(embed, "fields", ()) or ():
            if _label(str(getattr(field, "name", ""))) in expected:
                return True
        for line in str(getattr(embed, "description", "") or "").splitlines():
            candidate = line.split(":", 1)[0].strip(" *_`\t")
            if _label(candidate) in expected:
                return True
    return False


def build_manual_fallback_embed(
    *,
    state: ManualFallbackState,
    reason: str,
    source: ManualFallbackSource = ManualFallbackSource.MANUAL,
    member_id: int | None = None,
    creator_id: int | None = None,
    ticket_form: TicketForm | None = None,
) -> discord.Embed:
    colours = {
        ManualFallbackState.WAITING: discord.Colour.orange(),
        ManualFallbackState.READY: discord.Colour.gold(),
        ManualFallbackState.DONE: discord.Colour.green(),
    }
    titles = {
        ManualFallbackState.WAITING: "⚠️ Je potřeba doplnit údaje žádosti",
        ManualFallbackState.READY: "📝 Ručně doplněná žádost čeká na zpracování",
        ManualFallbackState.DONE: "✅ Ručně doplněná žádost je zpracovaná",
    }
    descriptions = {
        ManualFallbackState.WAITING: (
            "Zdrojovou žádost se nepodařilo bezpečně načíst. "
            "Nutné údaje: Jméno a příjmení, Datum narození, Telefonní číslo, Pozice. "
            "Tlačítko může použít pouze nakonfigurovaná role Vedení."
        ),
        ManualFallbackState.READY: (
            "Údaje jsou uložené přímo v této zprávě a mají přednost před "
            "později nalezenou žádostí. Zpracování lze bezpečně zopakovat."
        ),
        ManualFallbackState.DONE: (
            "Tyto údaje jsou autoritativním zdrojem osobní složky. "
            "Vedení je může tlačítkem znovu upravit."
        ),
    }
    if source is ManualFallbackSource.REQUEST:
        titles[ManualFallbackState.DONE] = "✅ Zdrojová žádost je znovu dostupná"
        descriptions[ManualFallbackState.DONE] = (
            "Aktuální údaje se načítají z propojeného kanálu žádosti. "
            "Tato zpráva neobsahuje kopii osobních údajů a není jejich autoritativním zdrojem."
        )
    embed = discord.Embed(
        title=titles[state],
        description=descriptions[state],
        colour=colours[state],
    )
    embed.add_field(name="Stav", value=state.value.upper(), inline=True)
    if reason:
        embed.add_field(name="Důvod", value=str(reason)[:1024], inline=False)
    if member_id is not None:
        embed.add_field(name="Uživatel", value=f"<@{member_id}>", inline=False)
    if ticket_form is not None:
        embed.add_field(name="Jméno a příjmení", value=ticket_form.full_name[:1024], inline=False)
        embed.add_field(name="Datum narození", value=ticket_form.birth_date[:1024], inline=True)
        embed.add_field(name="Telefonní číslo", value=ticket_form.phone_number[:1024], inline=True)
        embed.add_field(name="Pozice", value=ticket_form.position[:1024], inline=False)
    if creator_id:
        embed.add_field(name="Doplnil", value=f"<@{creator_id}>", inline=False)

    embed.set_footer(
        text=(
            f"{MANUAL_FALLBACK_FOOTER_PREFIX} • state:{state.value} • "
            f"source:{source.value} • member:{member_id or 0} • creator:{creator_id or 0}"
        )
    )
    return embed


def parse_manual_fallback_embed(embed: object) -> ManualFallbackRecord | None:
    footer_text = str(getattr(getattr(embed, "footer", None), "text", "") or "")
    if not footer_text.startswith(MANUAL_FALLBACK_FOOTER_PREFIX):
        return None
    marker_match = _MARKER.search(footer_text)
    if marker_match is None:
        return None
    try:
        state = ManualFallbackState(marker_match.group("state"))
        source = ManualFallbackSource(marker_match.group("source"))
    except ValueError:
        return None

    raw_member_id = int(marker_match.group("member_id"))
    raw_creator_id = int(marker_match.group("creator_id"))
    marker = ManualFallbackMarker(
        state=state,
        source=source,
        member_id=raw_member_id or None,
        creator_id=raw_creator_id or None,
    )
    values = {
        _label(str(getattr(field, "name", ""))): str(getattr(field, "value", "") or "").strip()
        for field in getattr(embed, "fields", ()) or ()
    }
    ticket_form: TicketForm | None = None
    full_name = values.get("jmeno a prijmeni", "")
    raw_position = values.get("pozice", "")
    position = canonical_manual_position(raw_position) or raw_position
    if full_name and position:
        ticket_form = TicketForm(
            full_name,
            position,
            birth_date=values.get("datum narozeni", ""),
            phone_number=values.get("telefonni cislo", ""),
        )
    return ManualFallbackRecord(
        marker=marker,
        ticket_form=ticket_form,
        reason=values.get("duvod", ""),
    )


RecoveryHandler = Callable[[discord.Interaction, ManualRecoveryAction], Awaitable[None]]
InteractionGuard = Callable[[discord.Interaction], Awaitable[bool]]
SubmissionHandler = Callable[[discord.Interaction, ManualSubmission], Awaitable[None]]


class _RecoveryButton(discord.ui.Button):
    def __init__(self, action: ManualRecoveryAction, handler: RecoveryHandler, label: str) -> None:
        super().__init__(
            label=label,
            style=(
                discord.ButtonStyle.primary
                if action is ManualRecoveryAction.OPEN
                else discord.ButtonStyle.secondary
            ),
            custom_id=f"ticket-renamer:manual:{action.value}",
        )
        self._action = action
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._handler(interaction, self._action)


class ManualRecoveryView(discord.ui.View):
    def __init__(
        self,
        handler: RecoveryHandler,
        *,
        state: ManualFallbackState | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.add_item(
            _RecoveryButton(
                ManualRecoveryAction.OPEN,
                handler,
                "Doplnit údaje" if state is ManualFallbackState.WAITING else "Upravit údaje",
            )
        )
        if state is None or state is ManualFallbackState.READY:
            self.add_item(
                _RecoveryButton(
                    ManualRecoveryAction.RETRY,
                    handler,
                    "Zopakovat zpracování",
                )
            )


class ManualDetailsModal(discord.ui.Modal):
    def __init__(
        self,
        handler: SubmissionHandler,
        *,
        source_message_id: int,
        member: object,
        position: str,
        ticket_form: TicketForm | None = None,
    ) -> None:
        super().__init__(
            title="Základní údaje zaměstnance",
            custom_id="ticket-renamer:manual:details",
        )
        self._handler = handler
        self._source_message_id = source_message_id
        self._member = member
        self._position = position
        self.full_name = discord.ui.TextInput(
            label="Jméno a příjmení",
            custom_id="full_name",
            default=ticket_form.full_name if ticket_form is not None else None,
            max_length=100,
        )
        self.birth_date = discord.ui.TextInput(
            label="Datum narození (D.M.YYYY)",
            custom_id="birth_date",
            default=ticket_form.birth_date if ticket_form is not None else None,
            max_length=10,
        )
        self.phone_number = discord.ui.TextInput(
            label="Telefonní číslo",
            custom_id="phone_number",
            default=ticket_form.phone_number if ticket_form is not None else None,
            max_length=25,
        )
        self.add_item(self.full_name)
        self.add_item(self.birth_date)
        self.add_item(self.phone_number)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._handler(
            interaction,
            ManualSubmission(
                source_message_id=self._source_message_id,
                member_id=int(getattr(self._member, "id", 0)),
                member=self._member,
                position=self._position,
                full_name=str(self.full_name.value),
                birth_date=str(self.birth_date.value),
                phone_number=str(self.phone_number.value),
            ),
        )


class _ManualMemberSelect(discord.ui.UserSelect):
    def __init__(self, owner: "ManualSelectionView", initial_member: object | None) -> None:
        kwargs: dict[str, object] = {}
        if initial_member is not None:
            kwargs["default_values"] = [initial_member]
        super().__init__(
            placeholder="Vyberte zaměstnance",
            custom_id="ticket-renamer:manual:member",
            min_values=1,
            max_values=1,
            **kwargs,
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._owner.guard(interaction):
            return
        self._owner.member = self.values[0]
        await interaction.response.defer()


class _ManualPositionSelect(discord.ui.Select):
    def __init__(self, owner: "ManualSelectionView", initial_position: str | None) -> None:
        super().__init__(
            placeholder="Vyberte pozici",
            custom_id="ticket-renamer:manual:position",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=position,
                    value=position,
                    default=position == initial_position,
                )
                for position in MANUAL_POSITIONS
            ],
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._owner.guard(interaction):
            return
        self._owner.position = self.values[0]
        await interaction.response.defer()


class _ManualContinueButton(discord.ui.Button):
    def __init__(self, owner: "ManualSelectionView") -> None:
        super().__init__(
            label="Pokračovat",
            style=discord.ButtonStyle.success,
            custom_id="ticket-renamer:manual:continue",
        )
        self._owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self._owner.guard(interaction):
            return
        if self._owner.member is None or self._owner.position is None:
            await interaction.response.send_message(
                "Nejprve vyberte zaměstnance i pozici.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ManualDetailsModal(
                self._owner.submission_handler,
                source_message_id=self._owner.source_message_id,
                member=self._owner.member,
                position=self._owner.position,
                ticket_form=self._owner.ticket_form,
            )
        )


class ManualSelectionView(discord.ui.View):
    def __init__(
        self,
        guard: InteractionGuard,
        submission_handler: SubmissionHandler,
        *,
        source_message_id: int,
        initial_member: object | None = None,
        initial_position: str | None = None,
        ticket_form: TicketForm | None = None,
    ) -> None:
        super().__init__(timeout=15 * 60)
        self.guard = guard
        self.submission_handler = submission_handler
        self.source_message_id = source_message_id
        self.member = initial_member
        self.position = canonical_manual_position(initial_position or "")
        self.ticket_form = ticket_form
        self.add_item(_ManualMemberSelect(self, initial_member))
        self.add_item(_ManualPositionSelect(self, self.position))
        self.add_item(_ManualContinueButton(self))
