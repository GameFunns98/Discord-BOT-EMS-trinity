from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Awaitable, Callable, Iterable
import unicodedata

import discord


ONBOARDING_FOOTER_PREFIX = "Discord Ticket Renamer • FiveRoster nástup"
_ONBOARDING_MARKER = re.compile(
    r"member:(?P<member_id>\d{15,22})\s+•\s+state:(?P<state>[a-z_]+)"
    r"\s+•\s+allowed:(?P<allowed>[a-z_,]+)"
    r"(?:\s+•\s+selected:(?P<selected>[a-z_]+))?"
)


class EnrollmentAction(str, Enum):
    PARAMEDIC = "paramedic"
    ACADEMY = "academy"
    DOCTOR = "doctor"
    DOCTOR_TRAINING = "doctor_training"
    SECURITY = "security"

    @property
    def label(self) -> str:
        return {
            EnrollmentAction.PARAMEDIC: "Paramedic",
            EnrollmentAction.ACADEMY: "Akademie",
            EnrollmentAction.DOCTOR: "Doktor",
            EnrollmentAction.DOCTOR_TRAINING: "Doktor v zácviku",
            EnrollmentAction.SECURITY: "Security",
        }[self]


ALL_ACTIONS = tuple(EnrollmentAction)


class OnboardingState(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class OnboardingMarker:
    member_id: int
    state: OnboardingState
    allowed_actions: tuple[EnrollmentAction, ...]
    selected_action: EnrollmentAction | None = None


InteractionHandler = Callable[[discord.Interaction, EnrollmentAction], Awaitable[None]]


def actions_for_position(position: str) -> tuple[EnrollmentAction, ...]:
    folded = unicodedata.normalize("NFKD", position.casefold())
    normalized = re.sub(
        r"[^a-z0-9]+",
        "",
        "".join(character for character in folded if not unicodedata.combining(character)),
    )
    if normalized == "zachranar":
        return (EnrollmentAction.PARAMEDIC, EnrollmentAction.ACADEMY)
    if normalized == "doktor":
        return (EnrollmentAction.DOCTOR, EnrollmentAction.DOCTOR_TRAINING)
    if normalized == "ochranka":
        return (EnrollmentAction.SECURITY,)
    return ()


def parse_onboarding_marker(embed: discord.Embed) -> OnboardingMarker | None:
    footer_text = str(getattr(getattr(embed, "footer", None), "text", "") or "")
    if not footer_text.startswith(ONBOARDING_FOOTER_PREFIX):
        return None
    match = _ONBOARDING_MARKER.search(footer_text)
    if match is None:
        return None
    try:
        state = OnboardingState(match.group("state"))
        actions = tuple(
            EnrollmentAction(item)
            for item in match.group("allowed").split(",")
            if item
        )
        selected_value = match.group("selected")
        selected_action = EnrollmentAction(selected_value) if selected_value else None
    except ValueError:
        return None
    return OnboardingMarker(
        member_id=int(match.group("member_id")),
        state=state,
        allowed_actions=actions,
        selected_action=selected_action,
    )


def build_onboarding_embed(
    *,
    full_name: str,
    member_id: int,
    requested_position: str,
    allowed_actions: Iterable[EnrollmentAction],
    state: OnboardingState,
    selected_action: EnrollmentAction | None = None,
    actor_id: int | None = None,
    detail: str | None = None,
) -> discord.Embed:
    actions = tuple(allowed_actions)
    colours = {
        OnboardingState.PENDING: discord.Colour.blurple(),
        OnboardingState.PROCESSING: discord.Colour.gold(),
        OnboardingState.COMPLETED: discord.Colour.green(),
        OnboardingState.PARTIAL: discord.Colour.orange(),
        OnboardingState.ERROR: discord.Colour.red(),
    }
    state_labels = {
        OnboardingState.PENDING: "Čeká na výběr hodnosti",
        OnboardingState.PROCESSING: "Probíhá zápis",
        OnboardingState.COMPLETED: "Dokončeno",
        OnboardingState.PARTIAL: "FiveRoster hotový, Discord role vyžadují kontrolu",
        OnboardingState.ERROR: "Zápis se nezdařil",
    }
    embed = discord.Embed(
        title="📋 Nástup do FiveRosteru",
        description=(
            "Vyberte cílovou hodnost. Ovládání je dostupné pouze vedení."
            if state is OnboardingState.PENDING and len(actions) > 1
            else None
        ),
        colour=colours[state],
    )
    embed.add_field(name="Zaměstnanec", value=f"{full_name}\n<@{member_id}>", inline=False)
    embed.add_field(name="Požadovaná pozice", value=requested_position, inline=True)
    embed.add_field(name="Stav", value=state_labels[state], inline=True)
    if selected_action is not None:
        embed.add_field(name="FiveRoster hodnost", value=selected_action.label, inline=True)
    if actor_id is not None:
        embed.add_field(name="Provedl", value=f"<@{actor_id}>", inline=True)
    if detail:
        embed.add_field(name="Podrobnosti", value=detail[:1024], inline=False)

    allowed_value = ",".join(action.value for action in actions)
    footer = (
        f"{ONBOARDING_FOOTER_PREFIX} • member:{member_id} • "
        f"state:{state.value} • allowed:{allowed_value}"
    )
    if selected_action is not None:
        footer += f" • selected:{selected_action.value}"
    embed.set_footer(text=footer)
    return embed


class EnrollmentButton(discord.ui.Button):
    def __init__(
        self,
        action: EnrollmentAction,
        handler: InteractionHandler,
        *,
        disabled: bool = False,
        retry_security: bool = False,
    ) -> None:
        label = action.label
        if retry_security and action is EnrollmentAction.SECURITY:
            label = "Opakovat Security"
        super().__init__(
            label=label,
            style=(
                discord.ButtonStyle.secondary
                if retry_security or disabled
                else discord.ButtonStyle.primary
            ),
            custom_id=f"ticket-renamer:onboarding:{action.value}",
            disabled=disabled,
        )
        self.action = action
        self._handler = handler

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._handler(interaction, self.action)


class OnboardingView(discord.ui.View):
    def __init__(
        self,
        handler: InteractionHandler,
        actions: Iterable[EnrollmentAction] = ALL_ACTIONS,
        *,
        disabled: bool = False,
        retry_security: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        for action in actions:
            self.add_item(
                EnrollmentButton(
                    action,
                    handler,
                    disabled=disabled,
                    retry_security=retry_security,
                )
            )
