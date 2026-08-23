from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import re
from typing import Awaitable, Callable, Iterable

import discord

from .fiveroster import LoaRequest, QuotaProgress, ShiftHours, ShiftStatus


SHIFT_PANEL_FOOTER_PREFIX = "Discord Ticket Renamer • Služební panel"
SHIFT_PANEL_SCHEMA_VERSION = 1
_SHIFT_PANEL_MARKER = re.compile(
    r"version:(?P<version>\d+)\s+•\s+member:(?P<member_id>\d{15,22})"
    r"\s+•\s+creator:(?P<creator>auto|\d{15,22})"
)


class ShiftPanelState(str, Enum):
    OFF_DUTY = "off_duty"
    ON_DUTY = "on_duty"
    LOA = "loa"


class ShiftAction(str, Enum):
    START = "start"
    END = "end"
    REQUEST_LOA = "request_loa"
    CANCEL_LOA = "cancel_loa"
    REFRESH = "refresh"


ALL_SHIFT_ACTIONS = tuple(ShiftAction)


@dataclass(frozen=True, slots=True)
class ShiftPanelMarker:
    member_id: int
    creator_id: int | None
    version: int = SHIFT_PANEL_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ShiftPanelSnapshot:
    status: ShiftStatus
    hours: ShiftHours
    quotas: tuple[QuotaProgress, ...]
    loas: tuple[LoaRequest, ...]
    fetched_at: datetime

    @property
    def today(self) -> date:
        return self.fetched_at.astimezone().date()

    @property
    def relevant_loas(self) -> tuple[LoaRequest, ...]:
        today = self.today
        requests = [
            request
            for request in self.loas
            if request.status in {"pending", "approved"} and request.end_date >= today
        ]
        return tuple(
            sorted(
                requests,
                key=lambda request: (
                    0
                    if request.status == "approved"
                    and request.start_date <= today <= request.end_date
                    else 1,
                    request.start_date,
                    request.id,
                ),
            )
        )

    @property
    def cancelable_loas(self) -> tuple[LoaRequest, ...]:
        return self.relevant_loas

    @property
    def active_loa(self) -> LoaRequest | None:
        today = self.today
        for request in self.relevant_loas:
            if (
                request.status == "approved"
                and request.start_date <= today <= request.end_date
            ):
                return request
        return None

    @property
    def state(self) -> ShiftPanelState:
        if self.status.on_shift:
            return ShiftPanelState.ON_DUTY
        if self.active_loa is not None:
            return ShiftPanelState.LOA
        return ShiftPanelState.OFF_DUTY


def parse_shift_panel_marker(embed: discord.Embed) -> ShiftPanelMarker | None:
    footer_text = str(getattr(getattr(embed, "footer", None), "text", "") or "")
    if not footer_text.startswith(SHIFT_PANEL_FOOTER_PREFIX):
        return None
    match = _SHIFT_PANEL_MARKER.search(footer_text)
    if match is None:
        return None
    creator_value = match.group("creator")
    return ShiftPanelMarker(
        member_id=int(match.group("member_id")),
        creator_id=None if creator_value == "auto" else int(creator_value),
        version=int(match.group("version")),
    )


def parse_loa_date(value: str) -> date:
    cleaned = re.sub(r"\s+", "", str(value or ""))
    for date_format in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            pass
    raise ValueError("Datum zadejte jako DD.MM.RRRR, například 20.08.2026.")


def loa_ranges_overlap(
    start_date: date,
    end_date: date,
    requests: Iterable[LoaRequest],
) -> LoaRequest | None:
    for request in requests:
        if request.status not in {"pending", "approved"}:
            continue
        if start_date <= request.end_date and request.start_date <= end_date:
            return request
    return None


def _single_line(value: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


def _multiline(value: str, limit: int) -> str:
    cleaned = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


def _date_text(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _quota_text(quotas: tuple[QuotaProgress, ...]) -> str:
    if not quotas:
        return "FiveRoster nemá pro tohoto zaměstnance aktivní kvótu."

    lines: list[str] = []
    for quota in quotas:
        icon = "✅" if quota.is_met else "⏳"
        progress = (
            f"{quota.completed_formatted} / {quota.required_formatted}"
            f" ({quota.percentage:.0f} %)"
        )
        line = f"{icon} **{_single_line(quota.name, 80)}:** {progress}"
        if quota.time_remaining and not quota.is_met:
            line += f" · zbývá {_single_line(quota.time_remaining, 60)}"
        lines.append(line)
    return _multiline("\n".join(lines), 1024)


def _loa_text(requests: tuple[LoaRequest, ...]) -> str:
    if not requests:
        return "Žádná čekající ani schválená LOA."

    status_labels = {"pending": "Čeká na schválení", "approved": "Schválená"}
    lines: list[str] = []
    for request in requests[:5]:
        status = status_labels.get(request.status, request.status)
        reason = _single_line(request.reason, 120) or "Bez uvedeného důvodu"
        lines.append(
            f"**{status}** · {_date_text(request.start_date)}–{_date_text(request.end_date)}"
            f"\n{reason}"
        )
    if len(requests) > 5:
        lines.append(f"… a dalších {len(requests) - 5}")
    return _multiline("\n".join(lines), 1024)


def build_shift_panel_embed(
    *,
    member_id: int,
    snapshot: ShiftPanelSnapshot,
    creator_id: int | None,
) -> discord.Embed:
    state = snapshot.state
    titles = {
        ShiftPanelState.OFF_DUTY: "⚪ Služební panel – mimo službu",
        ShiftPanelState.ON_DUTY: "🟢 Služební panel – ve službě",
        ShiftPanelState.LOA: "🌴 Služební panel – LOA",
    }
    colours = {
        ShiftPanelState.OFF_DUTY: discord.Colour.blurple(),
        ShiftPanelState.ON_DUTY: discord.Colour.green(),
        ShiftPanelState.LOA: discord.Colour.gold(),
    }
    embed = discord.Embed(title=titles[state], colour=colours[state])
    embed.add_field(name="Zaměstnanec", value=f"<@{member_id}>", inline=False)

    if state is ShiftPanelState.ON_DUTY:
        started_at = snapshot.status.started_at
        if started_at is not None:
            unix_timestamp = int(started_at.timestamp())
            started_text = f"<t:{unix_timestamp}:F>\n<t:{unix_timestamp}:R>"
        else:
            started_text = "FiveRoster neuvedl čas začátku."
        embed.add_field(name="Začátek služby", value=started_text, inline=True)
        embed.add_field(
            name="Aktuální délka",
            value=snapshot.status.formatted_duration or "Právě zahájena",
            inline=True,
        )
    elif state is ShiftPanelState.LOA and snapshot.active_loa is not None:
        active_loa = snapshot.active_loa
        embed.add_field(
            name="Aktivní LOA",
            value=(
                f"{_date_text(active_loa.start_date)}–{_date_text(active_loa.end_date)}"
                f"\n{_single_line(active_loa.reason, 900) or 'Bez uvedeného důvodu'}"
            ),
            inline=False,
        )

    weekly = snapshot.hours.weekly
    embed.add_field(
        name="Tento týden",
        value=f"**Čas:** {weekly.formatted}\n**Počet směn:** {weekly.shift_count}",
        inline=False,
    )
    embed.add_field(name="Kvóta", value=_quota_text(snapshot.quotas), inline=False)
    embed.add_field(
        name="LOA přehled",
        value=_loa_text(snapshot.relevant_loas),
        inline=False,
    )
    embed.add_field(
        name="Panel založil",
        value="Automaticky po nástupu" if creator_id is None else f"<@{creator_id}>",
        inline=False,
    )
    creator_value = "auto" if creator_id is None else str(creator_id)
    embed.set_footer(
        text=(
            f"{SHIFT_PANEL_FOOTER_PREFIX} • version:{SHIFT_PANEL_SCHEMA_VERSION} • "
            f"member:{member_id} • creator:{creator_value}"
        )
    )
    embed.timestamp = snapshot.fetched_at
    return embed


ShiftInteractionHandler = Callable[[discord.Interaction, ShiftAction], Awaitable[None]]
LoaSubmitHandler = Callable[[discord.Interaction, int, str, str, str], Awaitable[None]]
LoaCancelHandler = Callable[[discord.Interaction, int, int], Awaitable[None]]


class ShiftPanelButton(discord.ui.Button):
    def __init__(
        self,
        action: ShiftAction,
        handler: ShiftInteractionHandler,
        *,
        disabled: bool = False,
    ) -> None:
        labels = {
            ShiftAction.START: "Vstoupit do služby",
            ShiftAction.END: "Ukončit službu",
            ShiftAction.REQUEST_LOA: "Požádat o LOA",
            ShiftAction.CANCEL_LOA: "Zrušit LOA",
            ShiftAction.REFRESH: "Obnovit",
        }
        styles = {
            ShiftAction.START: discord.ButtonStyle.success,
            ShiftAction.END: discord.ButtonStyle.danger,
            ShiftAction.REQUEST_LOA: discord.ButtonStyle.primary,
            ShiftAction.CANCEL_LOA: discord.ButtonStyle.secondary,
            ShiftAction.REFRESH: discord.ButtonStyle.secondary,
        }
        super().__init__(
            label=labels[action],
            style=styles[action],
            custom_id=f"ticket-renamer:shift:{action.value}",
            disabled=disabled,
        )
        self.action = action
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._handler(interaction, self.action)


class ShiftPanelView(discord.ui.View):
    def __init__(
        self,
        handler: ShiftInteractionHandler,
        snapshot: ShiftPanelSnapshot | None = None,
    ) -> None:
        super().__init__(timeout=None)
        state = snapshot.state if snapshot is not None else None
        cancelable = bool(snapshot.cancelable_loas) if snapshot is not None else True
        disabled = {
            ShiftAction.START: state in {ShiftPanelState.ON_DUTY, ShiftPanelState.LOA},
            ShiftAction.END: state is not None and state is not ShiftPanelState.ON_DUTY,
            ShiftAction.REQUEST_LOA: state in {ShiftPanelState.ON_DUTY, ShiftPanelState.LOA},
            ShiftAction.CANCEL_LOA: not cancelable,
            ShiftAction.REFRESH: False,
        }
        for action in ALL_SHIFT_ACTIONS:
            self.add_item(ShiftPanelButton(action, handler, disabled=disabled[action]))


class LoaRequestModal(discord.ui.Modal, title="Žádost o LOA"):
    start_date = discord.ui.TextInput(
        label="Datum od",
        placeholder="20.08.2026",
        min_length=8,
        max_length=10,
    )
    end_date = discord.ui.TextInput(
        label="Datum do",
        placeholder="25.08.2026",
        min_length=8,
        max_length=10,
    )
    reason = discord.ui.TextInput(
        label="Důvod",
        style=discord.TextStyle.paragraph,
        min_length=2,
        max_length=1000,
    )

    def __init__(
        self,
        *,
        member_id: int,
        owner_id: int,
        handler: LoaSubmitHandler,
    ) -> None:
        super().__init__(timeout=300)
        self.member_id = member_id
        self.owner_id = owner_id
        self._handler = handler

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Tento formulář patří jinému uživateli.",
                ephemeral=True,
            )
            return
        await self._handler(
            interaction,
            self.member_id,
            str(self.start_date.value),
            str(self.end_date.value),
            str(self.reason.value),
        )


class _LoaSelect(discord.ui.Select):
    def __init__(self, requests: tuple[LoaRequest, ...]) -> None:
        options = [
            discord.SelectOption(
                label=f"{_date_text(request.start_date)}–{_date_text(request.end_date)}",
                value=str(request.id),
                description=_single_line(request.reason, 90) or "Bez důvodu",
            )
            for request in requests[:25]
        ]
        super().__init__(
            placeholder="Vyberte LOA ke zrušení",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, LoaCancelView):
            await interaction.response.send_message("Výběr LOA vypršel.", ephemeral=True)
            return
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "Tento výběr patří jinému uživateli.",
                ephemeral=True,
            )
            return
        view.selected_loa_id = int(self.values[0])
        await interaction.response.defer()


class _LoaConfirm(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Potvrdit zrušení", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, LoaCancelView):
            await interaction.response.send_message("Potvrzení LOA vypršelo.", ephemeral=True)
            return
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "Toto potvrzení patří jinému uživateli.",
                ephemeral=True,
            )
            return
        if view.selected_loa_id is None:
            await interaction.response.send_message(
                "Nejdříve vyberte LOA, kterou chcete zrušit.",
                ephemeral=True,
            )
            return
        await view.handler(interaction, view.member_id, view.selected_loa_id)


class _LoaDismiss(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Ponechat LOA", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, LoaCancelView) or interaction.user.id != view.owner_id:
            await interaction.response.send_message(
                "Toto potvrzení patří jinému uživateli.",
                ephemeral=True,
            )
            return
        view.stop()
        await interaction.response.edit_message(content="LOA zůstala beze změny.", view=None)


class LoaCancelView(discord.ui.View):
    def __init__(
        self,
        *,
        requests: tuple[LoaRequest, ...],
        member_id: int,
        owner_id: int,
        handler: LoaCancelHandler,
    ) -> None:
        if not requests:
            raise ValueError("LoaCancelView vyžaduje alespoň jednu LOA.")
        super().__init__(timeout=120)
        self.member_id = member_id
        self.owner_id = owner_id
        self.handler = handler
        self.selected_loa_id: int | None = requests[0].id if len(requests) == 1 else None
        if len(requests) > 1:
            self.add_item(_LoaSelect(requests))
        self.add_item(_LoaConfirm())
        self.add_item(_LoaDismiss())
