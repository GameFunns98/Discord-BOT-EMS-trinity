from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging

import discord
from discord import app_commands

from .config import Settings
from .events import AppEvent, EventLevel, EventReporter
from .fiveroster import (
    EnrollmentOutcome,
    FiveRosterClient,
    FiveRosterConfigurationError,
    FiveRosterDifferentRankError,
    FiveRosterError,
    LoaRequest,
)
from .onboarding import (
    ALL_ACTIONS,
    ONBOARDING_FOOTER_PREFIX,
    EnrollmentAction,
    OnboardingMarker,
    OnboardingState,
    OnboardingView,
    actions_for_position,
    build_onboarding_embed,
    parse_onboarding_marker,
)
from .parser import (
    RequestChannelReference,
    TicketForm,
    abbreviate_person_name,
    build_channel_name,
    emoji_for_position,
    parse_request_channel_reference,
    parse_selected_member_id,
    parse_ticket_form,
)
from .shift_panel import (
    ALL_SHIFT_ACTIONS,
    LoaCancelView,
    LoaRequestModal,
    ShiftAction,
    ShiftPanelMarker,
    ShiftPanelSnapshot,
    ShiftPanelView,
    build_shift_panel_embed,
    loa_ranges_overlap,
    parse_loa_date,
    parse_shift_panel_marker,
)


LOGGER = logging.getLogger("ticket_renamer")
EMPLOYEE_INFO_FOOTER_PREFIX = "Discord Ticket Renamer • Základní informace"
EMPLOYEE_INFO_HISTORY_LIMIT = 100
FIVEROSTER_NAME_PREFIX = "**Nastavte jméno ve FiveRosteru na:**"
# Pokud panel není připnutý, musí se projít celá historie. Omezené hledání by
# po delší době mohlo založit druhý panel a porušit idempotenci.
SHIFT_PANEL_HISTORY_LIMIT: int | None = None
SHIFT_SNAPSHOT_TTL_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class OnboardingExecutionResult:
    state: OnboardingState
    action: EnrollmentAction
    detail: str
    actor_id: int | None = None


@dataclass(frozen=True, slots=True)
class ShiftPanelUpsertResult:
    message: discord.Message | None = None
    created: bool = False
    updated: bool = False
    pinned: bool = False
    error: str | None = None
    conflict: bool = False

    @property
    def changed(self) -> bool:
        return self.created or self.updated or self.pinned


class ShiftPanelLookupError(RuntimeError):
    pass


def _embed_field_value(value: str) -> str:
    cleaned = str(value or "").strip()
    return (cleaned or "Neuvedeno")[:1024]


def build_employee_info_embed(
    ticket_form: TicketForm,
    request_channel_name: str | None = None,
) -> discord.Embed:
    position_emoji = emoji_for_position(ticket_form.position) or "📁"
    employee_embed = discord.Embed(
        title=f"{position_emoji} Základní informace zaměstnance",
        colour=discord.Colour.from_rgb(34, 197, 94),
    )
    employee_embed.add_field(
        name="Jméno a příjmení",
        value=_embed_field_value(ticket_form.full_name),
        inline=False,
    )
    employee_embed.add_field(
        name="Datum narození",
        value=_embed_field_value(ticket_form.birth_date),
        inline=False,
    )
    employee_embed.add_field(
        name="Telefonní číslo",
        value=_embed_field_value(ticket_form.phone_number),
        inline=False,
    )
    employee_embed.add_field(
        name="Pozice",
        value=_embed_field_value(ticket_form.position),
        inline=False,
    )

    footer = EMPLOYEE_INFO_FOOTER_PREFIX
    if request_channel_name:
        footer = f"{footer} • Zdroj: #{request_channel_name}"
    employee_embed.set_footer(text=footer)
    return employee_embed


def _is_employee_info_embed(embed: discord.Embed) -> bool:
    footer_text = str(getattr(getattr(embed, "footer", None), "text", "") or "")
    return footer_text.startswith(EMPLOYEE_INFO_FOOTER_PREFIX)


class TicketRenamerClient(discord.Client):
    def __init__(
        self,
        settings: Settings,
        reporter: EventReporter | None = None,
        fiveroster_client: FiveRosterClient | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)

        self.settings = settings
        self._reporter = reporter
        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._shift_locks: dict[int, asyncio.Lock] = {}
        self._shift_snapshot_cache: dict[int, tuple[float, ShiftPanelSnapshot]] = {}
        self._roster_loa_cache: tuple[float, tuple[LoaRequest, ...]] | None = None
        self._roster_loa_lock = asyncio.Lock()
        self._initial_scan_finished = False
        self._synced_command_guild_ids: set[int] = set()
        self._closing_requested = False
        self._fiveroster = fiveroster_client
        self._fiveroster_error: str | None = None
        self._fiveroster_notice_reported = False
        self.tree = app_commands.CommandTree(self)

        @self.tree.command(
            name="sluzebni-panel",
            description="Vytvoří nebo obnoví připnutý služební panel v této osobní složce.",
        )
        @app_commands.guild_only()
        @app_commands.describe(uzivatel="Zaměstnanec, kterému osobní složka patří")
        async def service_panel_command(
            interaction: discord.Interaction,
            uzivatel: discord.Member,
        ) -> None:
            await self._handle_shift_panel_command(interaction, uzivatel)

    async def setup_hook(self) -> None:
        self.add_view(OnboardingView(self._handle_onboarding_interaction, ALL_ACTIONS))
        self.add_view(ShiftPanelView(self._handle_shift_panel_interaction))

        if not self.settings.fiveroster_enabled:
            return
        if self._fiveroster is None:
            self._fiveroster = FiveRosterClient(
                self.settings.fiveroster_api_key or "",
                self.settings.fiveroster_roster_uuid or "",
                self.settings.fiveroster_rank_names,
            )

        try:
            await self._fiveroster.start()
        except FiveRosterError as exc:
            self._fiveroster_error = str(exc)
            LOGGER.error("Inicializace FiveRoster API selhala: %s", exc)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "FiveRoster není připravený",
                    str(exc),
                    status="Připojování – chyba FiveRosteru",
                )
            )
        else:
            self._fiveroster_error = None
            self._report(
                AppEvent(
                    EventLevel.SUCCESS,
                    "FiveRoster je připravený",
                    "API klíč, roster a pět cílových hodností byly ověřeny.",
                    status="Připojování…",
                )
            )

    def _report(self, event: AppEvent) -> None:
        if self._reporter is None:
            return
        try:
            self._reporter(event)
        except Exception:
            LOGGER.exception("Nepodařilo se předat událost uživatelskému rozhraní.")

    async def _sync_shift_panel_command(self) -> None:
        for guild in self.guilds:
            if guild.id in self._synced_command_guild_ids:
                continue
            if not any(guild.get_channel(category_id) is not None for category_id in self.settings.ticket_category_ids):
                continue
            guild_object = discord.Object(id=guild.id)
            try:
                self.tree.copy_global_to(guild=guild_object)
                await self.tree.sync(guild=guild_object)
            except discord.HTTPException:
                LOGGER.exception("Synchronizace slash commandu selhala na serveru %s.", guild.id)
                self._report(
                    AppEvent(
                        EventLevel.ERROR,
                        "Slash command se nepodařilo zapnout",
                        "Discord nezaregistroval /sluzebni-panel. Zkontrolujte instalaci aplikace.",
                        status="Připojeno – chyba commandu",
                    )
                )
                continue
            self._synced_command_guild_ids.add(guild.id)
            LOGGER.info("Slash command /sluzebni-panel byl synchronizovan na serveru %s.", guild.id)

    def _is_configured_channel(self, channel: object) -> bool:
        return (
            isinstance(channel, discord.TextChannel)
            and channel.category_id in self.settings.ticket_category_ids
        )

    def _is_request_channel(self, channel: object) -> bool:
        return (
            isinstance(channel, discord.TextChannel)
            and any(
                channel.name.casefold().startswith(prefix)
                for prefix in self.settings.request_channel_prefixes
            )
        )

    def _is_rename_target_channel(self, channel: object) -> bool:
        return self._is_configured_channel(channel) and not self._is_request_channel(channel)

    def _is_ticket_tool_message(self, message: discord.Message) -> bool:
        return message.author.id in self.settings.ticket_tool_bot_ids

    async def on_ready(self) -> None:
        LOGGER.info("Bot je pripojen jako %s.", self.user)
        self._report(
            AppEvent(
                EventLevel.SUCCESS,
                "Bot je připojen",
                f"Discord Ticket Renamer je aktivní jako {self.user}.",
                status="Připojeno",
            )
        )

        await self._sync_shift_panel_command()

        if not self.settings.fiveroster_enabled and not self._fiveroster_notice_reported:
            self._fiveroster_notice_reported = True
            self._report(
                AppEvent(
                    EventLevel.WARNING,
                    "FiveRoster není nakonfigurován",
                    (
                        "Přejmenování a základní informace fungují, ale nástupová tlačítka "
                        "se nevytvoří, dokud nebude v .env API klíč a UUID rosteru."
                    ),
                    status="Připojeno – FiveRoster vypnutý",
                )
            )

        if self.settings.scan_existing_tickets and not self._initial_scan_finished:
            self._initial_scan_finished = True
            updated = await self._scan_existing_channels()
            LOGGER.info("Kontrola existujicich osobnich slozek dokoncena; aktualizovano: %d.", updated)
            self._report(
                AppEvent(
                    EventLevel.INFO,
                    "Kontrola osobních složek dokončena",
                    f"Aktualizované existující osobní složky: {updated}.",
                    status="Připojeno",
                    notify=updated > 0,
                )
            )

    async def on_disconnect(self) -> None:
        if self._closing_requested:
            return
        LOGGER.warning("Spojení s Discordem bylo přerušeno; klient se pokusí znovu připojit.")
        self._report(
            AppEvent(
                EventLevel.WARNING,
                "Spojení přerušeno",
                "Bot se automaticky pokouší znovu připojit k Discordu.",
                status="Obnovování spojení…",
            )
        )

    async def on_resumed(self) -> None:
        LOGGER.info("Spojení s Discordem bylo obnoveno.")
        self._report(
            AppEvent(
                EventLevel.SUCCESS,
                "Spojení obnoveno",
                "Bot je znovu připojený a sleduje osobní složky.",
                status="Připojeno",
            )
        )

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        LOGGER.exception("Neočekávaná chyba v Discord události %s.", event_method)
        self._report(
            AppEvent(
                EventLevel.ERROR,
                "Neočekávaná chyba",
                "Při zpracování Discord události nastala chyba. Podrobnosti jsou v logu.",
                status="Připojeno – chyba události",
            )
        )

    async def close(self) -> None:
        self._closing_requested = True
        if self._fiveroster is not None:
            try:
                await self._fiveroster.close()
            except Exception:
                LOGGER.exception("FiveRoster klient se nepodařilo korektně ukončit.")
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        await self._maybe_rename_from_message(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id is None:
            return

        channel = self.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(payload.channel_id)
            except discord.NotFound:
                return
            except discord.Forbidden:
                LOGGER.error("Bot nemůže načíst upravenou osobní složku %s.", payload.channel_id)
                self._report(
                    AppEvent(
                        EventLevel.ERROR,
                        "Kanál není dostupný",
                        "Bot nemůže načíst osobní složku. Zkontrolujte oprávnění View Channels.",
                        status="Připojeno – chyba oprávnění",
                    )
                )
                return
            except discord.HTTPException:
                LOGGER.exception("Discord nevrátil osobní složku %s.", payload.channel_id)
                return

        if not self._is_rename_target_channel(channel):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        except discord.Forbidden:
            LOGGER.error("Bot nemůže načíst zprávu %s.", payload.message_id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Zpráva není dostupná",
                    "Bot nemůže číst zprávy osobní složky. Zkontrolujte Read Message History.",
                    status="Připojeno – chyba oprávnění",
                )
            )
            return
        except discord.HTTPException:
            LOGGER.exception("Discord nevrátil zprávu %s.", payload.message_id)
            return

        await self._maybe_rename_from_message(message)

    async def _maybe_rename_from_message(
        self,
        message: discord.Message,
        *,
        allow_onboarding: bool = True,
    ) -> bool:
        if message.guild is None or not self._is_rename_target_channel(message.channel):
            return False
        if not self._is_ticket_tool_message(message) or not message.embeds:
            return False

        request_channel: discord.TextChannel | None = None
        selected_member_id = parse_selected_member_id(message.embeds)
        ticket_form = parse_ticket_form(message.embeds)
        if ticket_form is None:
            reference = parse_request_channel_reference(
                message.embeds,
                self.settings.request_channel_prefixes,
            )
            if reference is None:
                return False

            request_channel = await self._resolve_request_channel(message.guild, reference)
            if request_channel is None:
                return False

            ticket_form = await self._read_ticket_form(request_channel)
            if ticket_form is None:
                return False

        desired_name = build_channel_name(
            ticket_form,
            separator=self.settings.channel_separator,
        )
        if desired_name is None:
            if emoji_for_position(ticket_form.position) is None:
                LOGGER.warning(
                    "Osobni slozka %s odkazuje na nepodporovanou pozici; kanal zustal beze zmeny.",
                    message.channel.id,
                )
                self._report(
                    AppEvent(
                        EventLevel.WARNING,
                        "Neznámá pozice",
                        (
                            f"Kanál {message.channel.name} nebyl přejmenován: "
                            f"pozice „{ticket_form.position}“ není podporovaná."
                        ),
                        status="Připojeno – upozornění",
                    )
                )
            return False

        lock = self._channel_locks.setdefault(message.channel.id, asyncio.Lock())
        async with lock:
            renamed = False
            if message.channel.name != desired_name:
                previous_name = message.channel.name
                bot_member = message.guild.me
                permissions = (
                    message.channel.permissions_for(bot_member)
                    if bot_member is not None
                    else None
                )

                if permissions is not None and not permissions.manage_channels:
                    LOGGER.error(
                        "Bot nema opravneni Spravovat kanaly v osobni slozce %s.",
                        message.channel.id,
                    )
                    self._report(
                        AppEvent(
                            EventLevel.ERROR,
                            "Chybí oprávnění",
                            f"Bot nemůže přejmenovat kanál {message.channel.name}: chybí Spravovat kanály.",
                            status="Připojeno – chyba oprávnění",
                        )
                    )
                else:
                    try:
                        await message.channel.edit(
                            name=desired_name,
                            reason="Automaticke prejmenovani osobni slozky podle formulare zadosti",
                        )
                        renamed = True
                    except discord.Forbidden:
                        LOGGER.error(
                            "Discord zakazal prejmenovani kanalu %s. Zkontrolujte opravneni bota.",
                            message.channel.id,
                        )
                        self._report(
                            AppEvent(
                                EventLevel.ERROR,
                                "Discord zakázal změnu",
                                f"Kanál {message.channel.name} nebyl přejmenován. Zkontrolujte oprávnění bota.",
                                status="Připojeno – chyba oprávnění",
                            )
                        )
                    except discord.HTTPException:
                        LOGGER.exception("Discord neprejmenoval kanal %s.", message.channel.id)
                        self._report(
                            AppEvent(
                                EventLevel.ERROR,
                                "Přejmenování selhalo",
                                f"Discord nepřejmenoval kanál {message.channel.name}. Podrobnosti jsou v logu.",
                                status="Připojeno – chyba Discordu",
                            )
                        )

                if renamed:
                    LOGGER.info(
                        "Osobni slozka %s byla prejmenovana podle pozice %s.",
                        message.channel.id,
                        ticket_form.position,
                    )
                    self._report(
                        AppEvent(
                            EventLevel.SUCCESS,
                            "Osobní složka přejmenována",
                            f"{previous_name} → {desired_name}",
                            status="Připojeno",
                        )
                    )

            info_changed = await self._upsert_employee_info(
                message.channel,
                ticket_form,
                request_channel,
            )

            onboarding_changed = False
            if allow_onboarding and selected_member_id is not None:
                onboarding_changed = await self._upsert_onboarding(
                    message.channel,
                    ticket_form,
                    selected_member_id,
                )
            elif (
                allow_onboarding
                and self.settings.fiveroster_enabled
                and request_channel is not None
            ):
                LOGGER.warning(
                    "Osobni slozka %s nema platne pole Uzivatel; FiveRoster workflow nevznikl.",
                    message.channel.id,
                )
                self._report(
                    AppEvent(
                        EventLevel.ERROR,
                        "Ve složce chybí uživatel",
                        (
                            f"Kanál {message.channel.name} byl přejmenován, ale pole Uživatel "
                            "neobsahuje platný Discord účet. FiveRoster nástup nebyl vytvořen."
                        ),
                        status="Připojeno – chyba formuláře",
                    )
                )

        return renamed or info_changed or onboarding_changed

    async def _upsert_employee_info(
        self,
        channel: discord.TextChannel,
        ticket_form: TicketForm,
        request_channel: discord.TextChannel | None,
    ) -> bool:
        bot_member = channel.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member is not None else None
        missing_permissions: list[str] = []
        if permissions is not None:
            if not permissions.send_messages:
                missing_permissions.append("Posílat zprávy")
            if not permissions.embed_links:
                missing_permissions.append("Vkládat odkazy")

        if missing_permissions:
            LOGGER.error(
                "Bot nema opravneni odeslat embed do osobni slozky %s: %s.",
                channel.id,
                ", ".join(missing_permissions),
            )
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Chybí oprávnění pro embed",
                    (
                        f"Bot nemůže vložit základní informace do {channel.name}. "
                        f"Chybí: {', '.join(missing_permissions)}."
                    ),
                    status="Připojeno – chyba oprávnění",
                )
            )
            return False

        employee_embed = build_employee_info_embed(
            ticket_form,
            request_channel.name if request_channel is not None else None,
        )
        existing_message: discord.Message | None = None
        existing_embed: discord.Embed | None = None
        bot_user_id = getattr(self.user, "id", None)

        try:
            async for candidate in channel.history(
                limit=EMPLOYEE_INFO_HISTORY_LIMIT,
                oldest_first=False,
            ):
                if bot_user_id is None or candidate.author.id != bot_user_id:
                    continue
                for candidate_embed in candidate.embeds:
                    if _is_employee_info_embed(candidate_embed):
                        existing_message = candidate
                        existing_embed = candidate_embed
                        break
                if existing_message is not None:
                    break
        except discord.Forbidden:
            LOGGER.error("Bot nema pristup k historii osobni slozky %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Historie osobní složky není dostupná",
                    f"Bot nemůže ověřit existující embed v kanálu {channel.name}.",
                    status="Připojeno – chyba oprávnění",
                )
            )
            return False
        except discord.HTTPException:
            LOGGER.exception("Nepodarilo se nacist historii osobni slozky %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Kontrola embedu selhala",
                    f"Nepodařilo se načíst historii kanálu {channel.name}.",
                    status="Připojeno – chyba Discordu",
                )
            )
            return False

        if existing_message is not None and existing_embed is not None:
            if existing_embed.to_dict() == employee_embed.to_dict():
                return False
            try:
                await existing_message.edit(
                    embed=employee_embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                LOGGER.error("Discord zakazal aktualizaci embedu v kanalu %s.", channel.id)
                self._report(
                    AppEvent(
                        EventLevel.ERROR,
                        "Aktualizace embedu byla zakázána",
                        f"Bot nemůže upravit základní informace v kanálu {channel.name}.",
                        status="Připojeno – chyba oprávnění",
                    )
                )
                return False
            except discord.HTTPException:
                LOGGER.exception("Discord neaktualizoval embed v kanalu %s.", channel.id)
                self._report(
                    AppEvent(
                        EventLevel.ERROR,
                        "Aktualizace embedu selhala",
                        f"Discord neupravil základní informace v kanálu {channel.name}.",
                        status="Připojeno – chyba Discordu",
                    )
                )
                return False

            LOGGER.info("Zakladni informace byly aktualizovany v osobni slozce %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.SUCCESS,
                    "Základní informace aktualizovány",
                    f"Embed v kanálu {channel.name} byl aktualizován.",
                    status="Připojeno",
                )
            )
            return True

        try:
            await channel.send(
                embed=employee_embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            LOGGER.error("Discord zakazal odeslani embedu do kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Odeslání embedu bylo zakázáno",
                    f"Bot nemůže poslat základní informace do kanálu {channel.name}.",
                    status="Připojeno – chyba oprávnění",
                )
            )
            return False
        except discord.HTTPException:
            LOGGER.exception("Discord neodeslal embed do kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Odeslání embedu selhalo",
                    f"Discord neposlal základní informace do kanálu {channel.name}.",
                    status="Připojeno – chyba Discordu",
                )
            )
            return False

        LOGGER.info("Zakladni informace byly vlozeny do osobni slozky %s.", channel.id)
        self._report(
            AppEvent(
                EventLevel.SUCCESS,
                "Základní informace vloženy",
                f"Embed byl vložen do kanálu {channel.name}.",
                status="Připojeno",
            )
        )
        return True

    async def _find_onboarding_message(
        self,
        channel: discord.TextChannel,
    ) -> tuple[discord.Message | None, OnboardingMarker | None]:
        bot_user_id = getattr(self.user, "id", None)
        try:
            async for candidate in channel.history(
                limit=EMPLOYEE_INFO_HISTORY_LIMIT,
                oldest_first=False,
            ):
                if bot_user_id is None or candidate.author.id != bot_user_id:
                    continue
                for candidate_embed in candidate.embeds:
                    marker = parse_onboarding_marker(candidate_embed)
                    if marker is not None:
                        return candidate, marker
        except discord.Forbidden:
            LOGGER.error("Bot nema pristup k historii onboardingu v kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Historie nástupu není dostupná",
                    f"Bot nemůže zkontrolovat nástupovou zprávu v kanálu {channel.name}.",
                    status="Připojeno – chyba oprávnění",
                )
            )
        except discord.HTTPException:
            LOGGER.exception("Nacteni onboarding zpravy v kanalu %s selhalo.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Kontrola nástupu selhala",
                    f"Discord nevrátil historii kanálu {channel.name}.",
                    status="Připojeno – chyba Discordu",
                )
            )
        return None, None

    async def _send_onboarding_message(
        self,
        channel: discord.TextChannel,
        *,
        embed: discord.Embed,
        view: OnboardingView,
    ) -> discord.Message | None:
        try:
            return await channel.send(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            LOGGER.error("Discord zakazal odeslani onboarding zpravy do kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Nástupová zpráva byla zakázána",
                    f"Bot nemůže poslat tlačítka do kanálu {channel.name}.",
                    status="Připojeno – chyba oprávnění",
                )
            )
        except discord.HTTPException:
            LOGGER.exception("Discord neodeslal onboarding zpravu do kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Odeslání nástupu selhalo",
                    f"Discord neposlal tlačítka do kanálu {channel.name}.",
                    status="Připojeno – chyba Discordu",
                )
            )
        return None

    async def _edit_onboarding_message(
        self,
        message: discord.Message,
        *,
        embed: discord.Embed,
        view: OnboardingView,
    ) -> bool:
        try:
            await message.edit(
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except discord.Forbidden:
            LOGGER.error("Discord zakazal upravu onboarding zpravy %s.", message.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Nástupovou zprávu nelze upravit",
                    "Bot nemá oprávnění aktualizovat stav tlačítek.",
                    status="Připojeno – chyba oprávnění",
                )
            )
        except discord.HTTPException:
            LOGGER.exception("Discord neupravil onboarding zpravu %s.", message.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Aktualizace nástupu selhala",
                    "Discord neuložil nový stav nástupové zprávy.",
                    status="Připojeno – chyba Discordu",
                )
            )
        return False

    async def _upsert_fiveroster_name_message(
        self,
        channel: discord.TextChannel,
        full_name: str,
    ) -> bool:
        suggestion = abbreviate_person_name(full_name)
        if not suggestion:
            return False
        safe_suggestion = suggestion.replace("`", "'").replace("\r", " ").replace("\n", " ")
        content = f"{FIVEROSTER_NAME_PREFIX}\n```\n{safe_suggestion}\n```"
        existing_message: discord.Message | None = None
        bot_user_id = getattr(self.user, "id", None)

        try:
            async for candidate in channel.history(
                limit=EMPLOYEE_INFO_HISTORY_LIMIT,
                oldest_first=False,
            ):
                if bot_user_id is None or candidate.author.id != bot_user_id:
                    continue
                if str(getattr(candidate, "content", "") or "").startswith(
                    FIVEROSTER_NAME_PREFIX
                ):
                    existing_message = candidate
                    break
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Nepodarilo se najit zpravu s FiveRoster jmenem v kanalu %s.", channel.id)
            return False

        if existing_message is not None:
            if existing_message.content == content:
                return False
            try:
                await existing_message.edit(
                    content=content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.exception("FiveRoster jmeno se nepodarilo aktualizovat v kanalu %s.", channel.id)
                return False

        try:
            await channel.send(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except discord.Forbidden:
            LOGGER.error("Discord zakazal odeslani navrhu jmena do kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Návrh jména nelze odeslat",
                    f"Bot nemůže poslat kopírovatelné jméno do kanálu {channel.name}.",
                    status="Připojeno – chyba oprávnění",
                )
            )
        except discord.HTTPException:
            LOGGER.exception("Discord neodeslal navrh jmena do kanalu %s.", channel.id)
        return False

    def _result_embed(
        self,
        *,
        ticket_form: TicketForm,
        member_id: int,
        actions: tuple[EnrollmentAction, ...],
        result: OnboardingExecutionResult,
    ) -> discord.Embed:
        return build_onboarding_embed(
            full_name=ticket_form.full_name,
            member_id=member_id,
            requested_position=ticket_form.position,
            allowed_actions=actions,
            state=result.state,
            selected_action=result.action,
            actor_id=result.actor_id,
            detail=result.detail,
        )

    async def _ensure_fiveroster_ready(self) -> str | None:
        if self._fiveroster is None:
            return "FiveRoster klient není připravený."
        if self._fiveroster_error is None:
            return None

        try:
            await self._fiveroster.start()
        except FiveRosterError as exc:
            self._fiveroster_error = str(exc)
            return self._fiveroster_error

        self._fiveroster_error = None
        self._report(
            AppEvent(
                EventLevel.SUCCESS,
                "FiveRoster spojení obnoveno",
                "API a cílové hodnosti byly znovu ověřeny.",
                status="Připojeno",
            )
        )
        return None

    async def _load_roster_loa(self, *, force: bool = False) -> tuple[LoaRequest, ...]:
        if self._fiveroster is None:
            raise FiveRosterError("FiveRoster klient není připravený.")
        loop = asyncio.get_running_loop()
        cached = self._roster_loa_cache
        if not force and cached is not None and loop.time() - cached[0] < SHIFT_SNAPSHOT_TTL_SECONDS:
            return cached[1]

        async with self._roster_loa_lock:
            cached = self._roster_loa_cache
            if (
                not force
                and cached is not None
                and loop.time() - cached[0] < SHIFT_SNAPSHOT_TTL_SECONDS
            ):
                return cached[1]
            requests = await self._fiveroster.list_loa()
            self._roster_loa_cache = (loop.time(), tuple(requests))
            return tuple(requests)

    def _invalidate_shift_snapshot(self, member_id: int, *, loa_changed: bool = False) -> None:
        self._shift_snapshot_cache.pop(member_id, None)
        if loa_changed:
            self._roster_loa_cache = None

    def _cache_shift_snapshot(self, member_id: int, snapshot: ShiftPanelSnapshot) -> None:
        self._shift_snapshot_cache[member_id] = (
            asyncio.get_running_loop().time(),
            snapshot,
        )

    async def _load_shift_snapshot(
        self,
        member_id: int,
        *,
        force: bool = False,
    ) -> ShiftPanelSnapshot:
        readiness_error = await self._ensure_fiveroster_ready()
        if readiness_error is not None:
            raise FiveRosterError(readiness_error)
        if self._fiveroster is None:
            raise FiveRosterError("FiveRoster klient není připravený.")

        loop = asyncio.get_running_loop()
        cached = self._shift_snapshot_cache.get(member_id)
        if not force and cached is not None and loop.time() - cached[0] < SHIFT_SNAPSHOT_TTL_SECONDS:
            return cached[1]

        status, hours, quotas, all_loas = await asyncio.gather(
            self._fiveroster.get_shift_status(member_id),
            self._fiveroster.get_shift_hours(member_id),
            self._fiveroster.get_quota_progress(member_id),
            self._load_roster_loa(force=force),
        )
        snapshot = ShiftPanelSnapshot(
            status=status,
            hours=hours,
            quotas=tuple(quotas),
            loas=tuple(request for request in all_loas if request.player_id == member_id),
            fetched_at=datetime.now(timezone.utc),
        )
        self._shift_snapshot_cache[member_id] = (loop.time(), snapshot)
        return snapshot

    async def _find_shift_panel_message(
        self,
        channel: discord.TextChannel,
    ) -> tuple[discord.Message | None, ShiftPanelMarker | None, discord.Embed | None]:
        bot_user_id = getattr(self.user, "id", None)

        def panel_from_message(
            candidate: discord.Message,
        ) -> tuple[discord.Message, ShiftPanelMarker, discord.Embed] | None:
            if bot_user_id is None or candidate.author.id != bot_user_id:
                return None
            for candidate_embed in candidate.embeds:
                marker = parse_shift_panel_marker(candidate_embed)
                if marker is not None:
                    return candidate, marker, candidate_embed
            return None

        pins_method = getattr(channel, "pins", None)
        if callable(pins_method):
            try:
                async for candidate in pins_method(limit=250, oldest_first=False):
                    found = panel_from_message(candidate)
                    if found is not None:
                        return found
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.warning(
                    "Pripnute zpravy neslo nacist v kanalu %s; zkousim historii.",
                    channel.id,
                )
        try:
            async for candidate in channel.history(
                limit=SHIFT_PANEL_HISTORY_LIMIT,
                oldest_first=False,
            ):
                found = panel_from_message(candidate)
                if found is not None:
                    return found
        except discord.Forbidden:
            detail = "Bot nemůže číst historii osobní složky."
            LOGGER.error("Bot nema historii pro sluzebni panel v kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Služební panel nelze ověřit",
                    f"{channel.name}: {detail}",
                    status="Připojeno – chyba oprávnění",
                )
            )
            raise ShiftPanelLookupError(detail)
        except discord.HTTPException:
            LOGGER.exception("Historie sluzebniho panelu selhala v kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Kontrola služebního panelu selhala",
                    f"Discord nevrátil historii kanálu {channel.name}.",
                    status="Připojeno – chyba Discordu",
                )
            )
            raise ShiftPanelLookupError("Discord nevrátil historii osobní složky.")
        return None, None, None

    async def _pin_shift_panel_message(
        self,
        channel: discord.TextChannel,
        message: discord.Message,
    ) -> tuple[bool, str | None]:
        if bool(getattr(message, "pinned", False)):
            return False, None

        bot_member = channel.guild.me
        permissions = channel.permissions_for(bot_member) if bot_member is not None else None
        if permissions is not None:
            can_pin = bool(
                getattr(
                    permissions,
                    "pin_messages",
                    getattr(permissions, "manage_messages", False),
                )
            )
            if not can_pin:
                return False, "Bot nemá v osobní složce oprávnění Připínat zprávy."

        try:
            await message.pin(reason="Pripnuti sluzebniho panelu zamestnance")
            return True, None
        except discord.Forbidden:
            return False, "Discord zakázal připnutí. Zkontrolujte oprávnění Připínat zprávy."
        except discord.HTTPException:
            LOGGER.exception("Pripnuti sluzebniho panelu %s selhalo.", message.id)
            return False, "Discord služební panel nepřipnul."

    def _report_shift_panel_failure(
        self,
        channel: discord.TextChannel,
        detail: str,
        *,
        title: str = "Služební panel selhal",
    ) -> None:
        LOGGER.error("Sluzebni panel v kanalu %s selhal: %s", channel.id, detail)
        self._report(
            AppEvent(
                EventLevel.ERROR,
                title,
                f"{channel.name}: {detail}",
                status="Připojeno – chyba služebního panelu",
            )
        )

    async def _upsert_shift_panel(
        self,
        channel: discord.TextChannel,
        member_id: int,
        *,
        creator_id: int | None,
        force_refresh: bool = False,
        snapshot: ShiftPanelSnapshot | None = None,
    ) -> ShiftPanelUpsertResult:
        try:
            existing_message, marker, existing_embed = await self._find_shift_panel_message(channel)
        except ShiftPanelLookupError as exc:
            return ShiftPanelUpsertResult(error=str(exc))
        if marker is not None and marker.member_id != member_id:
            detail = (
                "Kanál už obsahuje služební panel svázaný s jiným Discord uživatelem."
            )
            self._report_shift_panel_failure(channel, detail, title="Konflikt služebního panelu")
            return ShiftPanelUpsertResult(error=detail, conflict=True)

        if snapshot is None:
            try:
                snapshot = await self._load_shift_snapshot(member_id, force=force_refresh)
            except FiveRosterError as exc:
                detail = str(exc)
                self._report_shift_panel_failure(channel, detail)
                return ShiftPanelUpsertResult(message=existing_message, error=detail)

        effective_creator_id = marker.creator_id if marker is not None else creator_id
        embed = build_shift_panel_embed(
            member_id=member_id,
            snapshot=snapshot,
            creator_id=effective_creator_id,
        )
        view = ShiftPanelView(self._handle_shift_panel_interaction, snapshot)
        created = False
        updated = False

        if existing_message is not None:
            if existing_embed is None or existing_embed.to_dict() != embed.to_dict():
                try:
                    await existing_message.edit(
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    updated = True
                except discord.Forbidden:
                    detail = "Bot nemůže upravit existující služební panel."
                    self._report_shift_panel_failure(channel, detail)
                    return ShiftPanelUpsertResult(message=existing_message, error=detail)
                except discord.HTTPException:
                    LOGGER.exception("Discord neupravil sluzebni panel %s.", existing_message.id)
                    detail = "Discord neuložil aktualizovaný služební panel."
                    self._report_shift_panel_failure(channel, detail)
                    return ShiftPanelUpsertResult(message=existing_message, error=detail)
            message = existing_message
        else:
            bot_member = channel.guild.me
            permissions = channel.permissions_for(bot_member) if bot_member is not None else None
            missing_permissions: list[str] = []
            if permissions is not None:
                if not permissions.send_messages:
                    missing_permissions.append("Posílat zprávy")
                if not permissions.embed_links:
                    missing_permissions.append("Vkládat odkazy")
            if missing_permissions:
                detail = "Chybí oprávnění: " + ", ".join(missing_permissions) + "."
                self._report_shift_panel_failure(channel, detail)
                return ShiftPanelUpsertResult(error=detail)
            try:
                message = await channel.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                created = True
            except discord.Forbidden:
                detail = "Bot nemůže odeslat služební panel do osobní složky."
                self._report_shift_panel_failure(channel, detail)
                return ShiftPanelUpsertResult(error=detail)
            except discord.HTTPException:
                LOGGER.exception("Discord neodeslal sluzebni panel do kanalu %s.", channel.id)
                detail = "Discord služební panel neodeslal."
                self._report_shift_panel_failure(channel, detail)
                return ShiftPanelUpsertResult(error=detail)

        pinned, pin_error = await self._pin_shift_panel_message(channel, message)
        if pin_error is not None:
            self._report_shift_panel_failure(
                channel,
                pin_error,
                title="Služební panel není připnutý",
            )
        if created:
            LOGGER.info(
                "Sluzebni panel clena %s byl vytvoren v kanalu %s.",
                member_id,
                channel.id,
            )
            self._report(
                AppEvent(
                    EventLevel.SUCCESS,
                    "Služební panel vytvořen",
                    f"Panel zaměstnance {member_id} byl vložen do {channel.name}.",
                    status="Připojeno",
                )
            )
        return ShiftPanelUpsertResult(
            message=message,
            created=created,
            updated=updated,
            pinned=pinned,
            error=pin_error,
        )

    async def _upsert_onboarding(
        self,
        channel: discord.TextChannel,
        ticket_form: TicketForm,
        member_id: int,
    ) -> bool:
        if not self.settings.fiveroster_enabled:
            return False
        if self._fiveroster is None:
            if not self._fiveroster_notice_reported:
                self._fiveroster_notice_reported = True
                self._report(
                    AppEvent(
                        EventLevel.ERROR,
                        "FiveRoster nástup není dostupný",
                        "FiveRoster klient není připravený.",
                        status="Připojeno – chyba FiveRosteru",
                    )
                )
            return False

        actions = actions_for_position(ticket_form.position)
        if not actions:
            return False

        existing_message, marker = await self._find_onboarding_message(channel)
        name_changed = False

        if existing_message is not None and marker is not None:
            name_changed = await self._upsert_fiveroster_name_message(
                channel,
                ticket_form.full_name,
            )
            if marker.state in {OnboardingState.COMPLETED, OnboardingState.PARTIAL}:
                if marker.member_id != member_id:
                    self._report(
                        AppEvent(
                            EventLevel.ERROR,
                            "Dokončený nástup má jiného uživatele",
                            (
                                f"Kanál {channel.name} už obsahuje dokončený nástup pro jiné ID. "
                                "Automatická změna byla zastavena."
                            ),
                            status="Připojeno – vyžaduje kontrolu",
                        )
                    )
                    return name_changed
                panel_result = await self._upsert_shift_panel(
                    channel,
                    member_id,
                    creator_id=None,
                )
                return name_changed or panel_result.changed

            if marker.state is OnboardingState.ERROR:
                return name_changed

            if marker.state is OnboardingState.PROCESSING:
                interrupted = OnboardingExecutionResult(
                    state=OnboardingState.ERROR,
                    action=marker.selected_action or marker.allowed_actions[0],
                    detail="Předchozí zpracování nebylo dokončeno. Volbu bezpečně zopakujte.",
                )
                edited = await self._edit_onboarding_message(
                    existing_message,
                    embed=self._result_embed(
                        ticket_form=ticket_form,
                        member_id=marker.member_id,
                        actions=marker.allowed_actions,
                        result=interrupted,
                    ),
                    view=OnboardingView(
                        self._handle_onboarding_interaction,
                        marker.allowed_actions,
                        retry_security=(marker.allowed_actions == (EnrollmentAction.SECURITY,)),
                    ),
                )
                return edited or name_changed

            if marker.member_id == member_id and marker.allowed_actions == actions:
                return name_changed

            pending_embed = build_onboarding_embed(
                full_name=ticket_form.full_name,
                member_id=member_id,
                requested_position=ticket_form.position,
                allowed_actions=actions,
                state=OnboardingState.PENDING,
            )
            edited = await self._edit_onboarding_message(
                existing_message,
                embed=pending_embed,
                view=OnboardingView(self._handle_onboarding_interaction, actions),
            )
            return edited or name_changed

        pending_embed = build_onboarding_embed(
            full_name=ticket_form.full_name,
            member_id=member_id,
            requested_position=ticket_form.position,
            allowed_actions=actions,
            state=OnboardingState.PENDING,
        )
        sent = await self._send_onboarding_message(
            channel,
            embed=pending_embed,
            view=OnboardingView(self._handle_onboarding_interaction, actions),
        )
        name_changed = await self._upsert_fiveroster_name_message(
            channel,
            ticket_form.full_name,
        )
        return sent is not None or name_changed

    def _onboarding_failure(
        self,
        *,
        channel: discord.TextChannel,
        action: EnrollmentAction,
        detail: str,
        actor_id: int | None,
        title: str = "FiveRoster zápis selhal",
    ) -> OnboardingExecutionResult:
        LOGGER.error(
            "Onboarding v kanalu %s pro hodnost %s selhal: %s",
            channel.id,
            action.value,
            detail,
        )
        self._report(
            AppEvent(
                EventLevel.ERROR,
                title,
                f"{channel.name}: {detail}",
                status="Připojeno – chyba nástupu",
            )
        )
        return OnboardingExecutionResult(
            state=OnboardingState.ERROR,
            action=action,
            detail=detail,
            actor_id=actor_id,
        )

    async def _execute_onboarding(
        self,
        *,
        guild: discord.Guild,
        channel: discord.TextChannel,
        member_id: int,
        action: EnrollmentAction,
        actor_id: int | None,
    ) -> OnboardingExecutionResult:
        target_member = guild.get_member(member_id)
        if target_member is None:
            try:
                target_member = await guild.fetch_member(member_id)
            except discord.NotFound:
                return self._onboarding_failure(
                    channel=channel,
                    action=action,
                    detail="Vybraný Discord uživatel už na serveru není.",
                    actor_id=actor_id,
                )
            except discord.Forbidden:
                return self._onboarding_failure(
                    channel=channel,
                    action=action,
                    detail="Bot nemůže načíst vybraného člena serveru.",
                    actor_id=actor_id,
                    title="Chybí oprávnění ke členovi",
                )
            except discord.HTTPException:
                return self._onboarding_failure(
                    channel=channel,
                    action=action,
                    detail="Discord nevrátil vybraného člena serveru.",
                    actor_id=actor_id,
                    title="Načtení člena selhalo",
                )

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return self._onboarding_failure(
                channel=channel,
                action=action,
                detail="Bot nemá oprávnění Spravovat role.",
                actor_id=actor_id,
                title="Chybí Spravovat role",
            )

        required_role_ids = (
            self.settings.onboarding_add_role_ids
            | self.settings.onboarding_remove_role_ids
        )
        roles_by_id: dict[int, discord.Role] = {}
        missing_role_ids: list[int] = []
        for role_id in sorted(required_role_ids):
            role = guild.get_role(role_id)
            if role is None:
                missing_role_ids.append(role_id)
            else:
                roles_by_id[role_id] = role

        if missing_role_ids:
            return self._onboarding_failure(
                channel=channel,
                action=action,
                detail=(
                    "Na serveru chybí nakonfigurované role: "
                    + ", ".join(str(role_id) for role_id in missing_role_ids)
                ),
                actor_id=actor_id,
                title="Chybí Discord role",
            )

        managed_roles = [
            role.name
            for role in roles_by_id.values()
            if bool(getattr(role, "managed", False))
        ]
        if managed_roles:
            return self._onboarding_failure(
                channel=channel,
                action=action,
                detail="Tyto role spravuje integrace a bot je nemůže měnit: " + ", ".join(managed_roles),
                actor_id=actor_id,
                title="Role nelze spravovat",
            )

        bot_top_position = int(getattr(bot_member.top_role, "position", 0))
        target_top_position = int(getattr(target_member.top_role, "position", 0))
        blocked_roles = [
            role.name
            for role in roles_by_id.values()
            if int(getattr(role, "position", 0)) >= bot_top_position
        ]
        if target_top_position >= bot_top_position or blocked_roles:
            detail = "Role bota musí být v seznamu rolí výše než zaměstnanec i všechny měněné role."
            if blocked_roles:
                detail += " Blokované role: " + ", ".join(blocked_roles)
            return self._onboarding_failure(
                channel=channel,
                action=action,
                detail=detail,
                actor_id=actor_id,
                title="Nesprávné pořadí rolí",
            )

        readiness_error = await self._ensure_fiveroster_ready()
        if readiness_error is not None:
            return self._onboarding_failure(
                channel=channel,
                action=action,
                detail=readiness_error,
                actor_id=actor_id,
            )

        try:
            enrollment: EnrollmentOutcome = await self._fiveroster.enroll(
                member_id,
                action.value,
            )
        except FiveRosterDifferentRankError as exc:
            return self._onboarding_failure(
                channel=channel,
                action=action,
                detail=str(exc),
                actor_id=actor_id,
                title="Uživatel už má jinou hodnost",
            )
        except (FiveRosterConfigurationError, FiveRosterError) as exc:
            return self._onboarding_failure(
                channel=channel,
                action=action,
                detail=str(exc),
                actor_id=actor_id,
            )

        preserved_roles = {
            role.id: role
            for role in target_member.roles
            if role.id != guild.id
            and role.id not in self.settings.onboarding_remove_role_ids
        }
        for role_id in self.settings.onboarding_add_role_ids:
            preserved_roles[role_id] = roles_by_id[role_id]
        new_roles = sorted(
            preserved_roles.values(),
            key=lambda role: (int(getattr(role, "position", 0)), role.id),
        )

        try:
            await target_member.edit(
                roles=new_roles,
                reason=(
                    f"FiveRoster nastup na hodnost {enrollment.rank.name} "
                    f"v osobni slozce {channel.name}"
                ),
            )
        except discord.Forbidden:
            detail = (
                f"FiveRoster hodnost {enrollment.rank.name} byla nastavena, ale Discord "
                "zakázal změnu rolí. Role opravte ručně."
            )
            LOGGER.error("Discord role po FiveRoster zapisu selhaly v kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Nástup je dokončen jen částečně",
                    f"{channel.name}: {detail}",
                    status="Připojeno – nutná ruční oprava rolí",
                )
            )
            return OnboardingExecutionResult(
                state=OnboardingState.PARTIAL,
                action=action,
                detail=detail,
                actor_id=actor_id,
            )
        except discord.HTTPException:
            detail = (
                f"FiveRoster hodnost {enrollment.rank.name} byla nastavena, ale Discord "
                "neuložil role. Role opravte ručně."
            )
            LOGGER.exception("Discord neulozil role po FiveRoster zapisu v kanalu %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Nástup je dokončen jen částečně",
                    f"{channel.name}: {detail}",
                    status="Připojeno – nutná ruční oprava rolí",
                )
            )
            return OnboardingExecutionResult(
                state=OnboardingState.PARTIAL,
                action=action,
                detail=detail,
                actor_id=actor_id,
            )

        if enrollment.already_enrolled:
            detail = (
                f"Uživatel už byl ve FiveRosteru na hodnosti {enrollment.rank.name}; "
                "Discord role byly synchronizovány."
            )
        else:
            detail = f"Uživatel byl zapsán na hodnost {enrollment.rank.name}."
        if enrollment.callsign:
            detail += f" Volací znak: {enrollment.callsign}."

        LOGGER.info(
            "Onboarding clena %s na hodnost %s byl dokoncen v kanalu %s.",
            member_id,
            enrollment.rank.name,
            channel.id,
        )
        self._report(
            AppEvent(
                EventLevel.SUCCESS,
                "FiveRoster nástup dokončen",
                f"{channel.name}: {enrollment.rank.name} pro člena {member_id}.",
                status="Připojeno",
            )
        )
        return OnboardingExecutionResult(
            state=OnboardingState.COMPLETED,
            action=action,
            detail=detail,
            actor_id=actor_id,
        )

    @staticmethod
    def _onboarding_data_from_message(
        message: discord.Message,
    ) -> tuple[OnboardingMarker, TicketForm] | None:
        for embed in message.embeds:
            marker = parse_onboarding_marker(embed)
            if marker is None:
                continue
            full_name = ""
            requested_position = ""
            for field in embed.fields:
                if field.name == "Zaměstnanec":
                    full_name = str(field.value).splitlines()[0].strip()
                elif field.name == "Požadovaná pozice":
                    requested_position = str(field.value).strip()
            if full_name and requested_position:
                return marker, TicketForm(full_name, requested_position)
        return None

    async def _interaction_reply(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _handle_onboarding_interaction(
        self,
        interaction: discord.Interaction,
        action: EnrollmentAction,
    ) -> None:
        guild = interaction.guild
        channel = interaction.channel
        source_message = interaction.message
        if (
            guild is None
            or not isinstance(channel, discord.TextChannel)
            or not self._is_rename_target_channel(channel)
            or source_message is None
        ):
            await self._interaction_reply(
                interaction,
                "Toto tlačítko lze použít pouze v osobní složce na serveru.",
            )
            return

        operator_role_ids = {
            int(getattr(role, "id", 0))
            for role in getattr(interaction.user, "roles", ())
        }
        if not operator_role_ids.intersection(self.settings.onboarding_operator_role_ids):
            LOGGER.warning(
                "Uzivatel %s se pokusil pouzit onboarding bez role vedeni v kanalu %s.",
                interaction.user.id,
                channel.id,
            )
            await self._interaction_reply(
                interaction,
                "Nemáte oprávnění. Toto rozhodnutí může provést pouze role vedení.",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self._channel_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            try:
                current_message = await channel.fetch_message(source_message.id)
            except discord.NotFound:
                await interaction.followup.send(
                    "Nástupová zpráva už neexistuje.",
                    ephemeral=True,
                )
                return
            except (discord.Forbidden, discord.HTTPException):
                LOGGER.exception("Nastupovou zpravu %s se nepodarilo znovu nacist.", source_message.id)
                await interaction.followup.send(
                    "Discord nedovolil ověřit aktuální stav zprávy.",
                    ephemeral=True,
                )
                return

            onboarding_data = self._onboarding_data_from_message(current_message)
            if onboarding_data is None:
                await interaction.followup.send(
                    "Nástupová zpráva nemá platný stav.",
                    ephemeral=True,
                )
                return
            marker, ticket_form = onboarding_data
            if marker.state in {OnboardingState.COMPLETED, OnboardingState.PARTIAL}:
                await interaction.followup.send(
                    "Tento nástup už byl zpracován.",
                    ephemeral=True,
                )
                return
            if marker.state is OnboardingState.PROCESSING:
                await interaction.followup.send(
                    "Nástup právě zpracovává jiný požadavek.",
                    ephemeral=True,
                )
                return
            if action not in marker.allowed_actions:
                await interaction.followup.send(
                    "Tato hodnost neodpovídá pozici v žádosti.",
                    ephemeral=True,
                )
                return

            processing_embed = build_onboarding_embed(
                full_name=ticket_form.full_name,
                member_id=marker.member_id,
                requested_position=ticket_form.position,
                allowed_actions=marker.allowed_actions,
                state=OnboardingState.PROCESSING,
                selected_action=action,
                actor_id=interaction.user.id,
            )
            processing_saved = await self._edit_onboarding_message(
                current_message,
                embed=processing_embed,
                view=OnboardingView(
                    self._handle_onboarding_interaction,
                    marker.allowed_actions,
                    disabled=True,
                ),
            )
            if not processing_saved:
                await interaction.followup.send(
                    "Nepodařilo se uzamknout tlačítka, proto nebyl proveden žádný zápis.",
                    ephemeral=True,
                )
                return

            result = await self._execute_onboarding(
                guild=guild,
                channel=channel,
                member_id=marker.member_id,
                action=action,
                actor_id=interaction.user.id,
            )
            result_view = OnboardingView(
                self._handle_onboarding_interaction,
                marker.allowed_actions,
                disabled=result.state in {OnboardingState.COMPLETED, OnboardingState.PARTIAL},
                retry_security=(
                    result.state is OnboardingState.ERROR
                    and marker.allowed_actions == (EnrollmentAction.SECURITY,)
                ),
            )
            final_saved = await self._edit_onboarding_message(
                current_message,
                embed=self._result_embed(
                    ticket_form=ticket_form,
                    member_id=marker.member_id,
                    actions=marker.allowed_actions,
                    result=result,
                ),
                view=result_view,
            )
            panel_result: ShiftPanelUpsertResult | None = None
            if result.state in {OnboardingState.COMPLETED, OnboardingState.PARTIAL}:
                panel_result = await self._upsert_shift_panel(
                    channel,
                    marker.member_id,
                    creator_id=None,
                    force_refresh=True,
                )
            if result.state is OnboardingState.COMPLETED:
                reply = f"Hotovo: {result.detail}"
            elif result.state is OnboardingState.PARTIAL:
                reply = f"Částečně dokončeno: {result.detail}"
            else:
                reply = f"Zápis se nezdařil: {result.detail}"
            if not final_saved:
                reply += " Stav tlačítek se ale nepodařilo uložit; zkontrolujte kanál a log."
            if panel_result is not None and panel_result.error is not None:
                reply += f" Služební panel vyžaduje kontrolu: {panel_result.error}"
            await interaction.followup.send(reply, ephemeral=True)

    @staticmethod
    def _shift_marker_from_message(message: discord.Message) -> ShiftPanelMarker | None:
        for embed in message.embeds:
            marker = parse_shift_panel_marker(embed)
            if marker is not None:
                return marker
        return None

    def _has_onboarding_operator_role(self, member: object) -> bool:
        role_ids = {
            int(getattr(role, "id", 0))
            for role in getattr(member, "roles", ())
        }
        return bool(role_ids.intersection(self.settings.onboarding_operator_role_ids))

    async def _handle_shift_panel_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if (
            guild is None
            or not isinstance(channel, discord.TextChannel)
            or not self._is_rename_target_channel(channel)
        ):
            await self._interaction_reply(
                interaction,
                "Příkaz lze použít pouze přímo v nakonfigurované osobní složce.",
            )
            return
        if not self._has_onboarding_operator_role(interaction.user):
            LOGGER.warning(
                "Uzivatel %s pouzil /sluzebni-panel bez role vedeni v kanalu %s.",
                interaction.user.id,
                channel.id,
            )
            await self._interaction_reply(
                interaction,
                "Nemáte oprávnění. Příkaz může použít pouze nakonfigurovaná role vedení.",
            )
            return
        if member.guild.id != guild.id:
            await self._interaction_reply(interaction, "Vybraný uživatel není členem tohoto serveru.")
            return
        if not self.settings.fiveroster_enabled:
            await self._interaction_reply(interaction, "FiveRoster není v aplikaci nakonfigurovaný.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self._shift_locks.setdefault(member.id, asyncio.Lock())
        async with lock:
            readiness_error = await self._ensure_fiveroster_ready()
            if readiness_error is not None or self._fiveroster is None:
                await interaction.followup.send(
                    f"FiveRoster není připravený: {readiness_error or 'chybí klient'}",
                    ephemeral=True,
                )
                return
            try:
                enrolled = await self._fiveroster.is_player_enrolled(member.id)
            except FiveRosterError as exc:
                self._report_shift_panel_failure(channel, str(exc))
                await interaction.followup.send(
                    f"Nelze ověřit zaměstnance ve FiveRosteru: {exc}",
                    ephemeral=True,
                )
                return
            if not enrolled:
                await interaction.followup.send(
                    "Označený uživatel zatím není zapsaný v nakonfigurovaném EMS rosteru.",
                    ephemeral=True,
                )
                return

            result = await self._upsert_shift_panel(
                channel,
                member.id,
                creator_id=interaction.user.id,
                force_refresh=True,
            )

        if result.message is not None and not result.conflict:
            operation = "zalozil" if result.created else "obnovil"
            LOGGER.info(
                "Vedouci %s rucne %s sluzebni panel clena %s v kanalu %s.",
                interaction.user.id,
                operation,
                member.id,
                channel.id,
            )

        if result.conflict:
            reply = f"Panel nebyl změněn: {result.error}"
        elif result.error is not None:
            reply = f"Panel byl zpracován, ale vyžaduje kontrolu: {result.error}"
        elif result.created:
            reply = "Služební panel byl vytvořen a připnut."
        elif result.pinned:
            reply = "Existující služební panel byl obnoven a připnut."
        else:
            reply = "Existující služební panel byl obnoven; už byl připnutý."
        await interaction.followup.send(reply, ephemeral=True)

    async def _reload_interaction_panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        source_message: discord.Message,
    ) -> tuple[discord.Message | None, ShiftPanelMarker | None]:
        try:
            current_message = await channel.fetch_message(source_message.id)
        except discord.NotFound:
            await interaction.followup.send("Služební panel už neexistuje.", ephemeral=True)
            return None, None
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Sluzebni panel %s se nepodarilo znovu nacist.", source_message.id)
            await interaction.followup.send(
                "Discord nedovolil ověřit aktuální stav služebního panelu.",
                ephemeral=True,
            )
            return None, None
        marker = self._shift_marker_from_message(current_message)
        if marker is None:
            await interaction.followup.send("Služební panel nemá platný marker.", ephemeral=True)
            return None, None
        return current_message, marker

    async def _handle_shift_panel_interaction(
        self,
        interaction: discord.Interaction,
        action: ShiftAction,
    ) -> None:
        guild = interaction.guild
        channel = interaction.channel
        source_message = interaction.message
        if (
            guild is None
            or not isinstance(channel, discord.TextChannel)
            or not self._is_rename_target_channel(channel)
            or source_message is None
        ):
            await self._interaction_reply(
                interaction,
                "Toto tlačítko lze použít pouze v osobní složce na serveru.",
            )
            return

        source_marker = self._shift_marker_from_message(source_message)
        if source_marker is None:
            await self._interaction_reply(interaction, "Služební panel nemá platný stav.")
            return
        if interaction.user.id != source_marker.member_id:
            LOGGER.warning(
                "Uzivatel %s se pokusil ovladat panel clena %s v kanalu %s.",
                interaction.user.id,
                source_marker.member_id,
                channel.id,
            )
            await self._interaction_reply(
                interaction,
                "Tento služební panel může ovládat pouze zaměstnanec, kterému patří.",
            )
            return

        if action is ShiftAction.REQUEST_LOA:
            await interaction.response.send_modal(
                LoaRequestModal(
                    member_id=source_marker.member_id,
                    owner_id=interaction.user.id,
                    handler=self._handle_loa_submission,
                )
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self._shift_locks.setdefault(source_marker.member_id, asyncio.Lock())
        async with lock:
            current_message, marker = await self._reload_interaction_panel(
                interaction,
                channel,
                source_message,
            )
            if current_message is None or marker is None:
                return
            if marker.member_id != interaction.user.id:
                await interaction.followup.send(
                    "Panel byl mezitím svázán s jiným uživatelem; akce byla zastavena.",
                    ephemeral=True,
                )
                return

            if action is ShiftAction.REFRESH:
                result = await self._upsert_shift_panel(
                    channel,
                    marker.member_id,
                    creator_id=marker.creator_id,
                    force_refresh=True,
                )
                reply = (
                    f"Panel se nepodařilo obnovit: {result.error}"
                    if result.error
                    else "Služební panel je aktuální."
                )
                await interaction.followup.send(reply, ephemeral=True)
                return

            if action is ShiftAction.CANCEL_LOA:
                try:
                    snapshot = await self._load_shift_snapshot(marker.member_id)
                except FiveRosterError as exc:
                    self._report_shift_panel_failure(channel, str(exc))
                    await interaction.followup.send(
                        f"LOA nelze načíst: {exc}",
                        ephemeral=True,
                    )
                    return
                if not snapshot.cancelable_loas:
                    await interaction.followup.send(
                        "Nemáte žádnou čekající ani schválenou LOA ke zrušení.",
                        ephemeral=True,
                    )
                    return
                await interaction.followup.send(
                    "Vyberte LOA a potvrďte její zrušení:",
                    view=LoaCancelView(
                        requests=snapshot.cancelable_loas,
                        member_id=marker.member_id,
                        owner_id=interaction.user.id,
                        handler=self._handle_loa_cancel,
                    ),
                    ephemeral=True,
                )
                return

            if action is ShiftAction.START:
                await self._start_member_shift(interaction, channel, marker)
                return
            if action is ShiftAction.END:
                await self._end_member_shift(interaction, channel, marker)
                return

    async def _start_member_shift(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        marker: ShiftPanelMarker,
    ) -> None:
        if self._fiveroster is None:
            await interaction.followup.send("FiveRoster klient není připravený.", ephemeral=True)
            return
        try:
            snapshot = await self._load_shift_snapshot(marker.member_id, force=True)
        except FiveRosterError as exc:
            self._report_shift_panel_failure(channel, str(exc), title="Vstup do služby selhal")
            await interaction.followup.send(f"Vstup do služby selhal: {exc}", ephemeral=True)
            return
        if snapshot.active_loa is not None:
            await self._upsert_shift_panel(
                channel,
                marker.member_id,
                creator_id=marker.creator_id,
                snapshot=snapshot,
            )
            await interaction.followup.send(
                "Během právě platné schválené LOA nelze vstoupit do služby.",
                ephemeral=True,
            )
            return
        if snapshot.status.on_shift:
            await self._upsert_shift_panel(
                channel,
                marker.member_id,
                creator_id=marker.creator_id,
                snapshot=snapshot,
            )
            await interaction.followup.send("Už jste ve službě; panel byl obnoven.", ephemeral=True)
            return

        started_status = None
        try:
            started_status = await self._fiveroster.start_shift(marker.member_id)
        except FiveRosterError as original_error:
            try:
                confirmed = await self._fiveroster.get_shift_status(marker.member_id)
                if confirmed.on_shift:
                    started_status = confirmed
            except FiveRosterError:
                pass
            if started_status is None:
                self._report_shift_panel_failure(
                    channel,
                    str(original_error),
                    title="Vstup do služby selhal",
                )
                await interaction.followup.send(
                    f"Vstup do služby selhal: {original_error}",
                    ephemeral=True,
                )
                return

        updated_snapshot = ShiftPanelSnapshot(
            status=started_status,
            hours=snapshot.hours,
            quotas=snapshot.quotas,
            loas=snapshot.loas,
            fetched_at=datetime.now(timezone.utc),
        )
        self._cache_shift_snapshot(marker.member_id, updated_snapshot)
        result = await self._upsert_shift_panel(
            channel,
            marker.member_id,
            creator_id=marker.creator_id,
            snapshot=updated_snapshot,
        )
        LOGGER.info("Clen %s vstoupil do sluzby v kanalu %s.", marker.member_id, channel.id)
        self._report(
            AppEvent(
                EventLevel.SUCCESS,
                "Směna zahájena",
                f"{channel.name}: člen {marker.member_id} vstoupil do služby.",
                status="Připojeno",
            )
        )
        reply = "Směna byla zahájena."
        if result.error:
            reply += f" FiveRoster je zapsaný, ale panel vyžaduje kontrolu: {result.error}"
        await interaction.followup.send(reply, ephemeral=True)

    async def _end_member_shift(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        marker: ShiftPanelMarker,
    ) -> None:
        readiness_error = await self._ensure_fiveroster_ready()
        if readiness_error is not None or self._fiveroster is None:
            await interaction.followup.send(
                f"Ukončení služby selhalo: {readiness_error or 'FiveRoster není připravený.'}",
                ephemeral=True,
            )
            return
        try:
            status = await self._fiveroster.get_shift_status(marker.member_id)
        except FiveRosterError as exc:
            self._report_shift_panel_failure(channel, str(exc), title="Ukončení služby selhalo")
            await interaction.followup.send(f"Ukončení služby selhalo: {exc}", ephemeral=True)
            return
        if not status.on_shift:
            self._invalidate_shift_snapshot(marker.member_id)
            result = await self._upsert_shift_panel(
                channel,
                marker.member_id,
                creator_id=marker.creator_id,
                force_refresh=True,
            )
            reply = "Už jste mimo službu; panel byl obnoven."
            if result.error:
                reply += f" Obnovení selhalo: {result.error}"
            await interaction.followup.send(reply, ephemeral=True)
            return

        ended_shift = None
        try:
            ended_shift = await self._fiveroster.end_shift(marker.member_id)
        except FiveRosterError as original_error:
            try:
                confirmed = await self._fiveroster.get_shift_status(marker.member_id)
                if not confirmed.on_shift:
                    ended_shift = True
            except FiveRosterError:
                pass
            if ended_shift is None:
                self._report_shift_panel_failure(
                    channel,
                    str(original_error),
                    title="Ukončení služby selhalo",
                )
                await interaction.followup.send(
                    f"Ukončení služby selhalo: {original_error}",
                    ephemeral=True,
                )
                return

        self._invalidate_shift_snapshot(marker.member_id)
        result = await self._upsert_shift_panel(
            channel,
            marker.member_id,
            creator_id=marker.creator_id,
            force_refresh=True,
        )
        duration = getattr(ended_shift, "formatted_duration", "")
        LOGGER.info("Clen %s ukoncil sluzbu v kanalu %s.", marker.member_id, channel.id)
        self._report(
            AppEvent(
                EventLevel.SUCCESS,
                "Směna ukončena",
                f"{channel.name}: člen {marker.member_id} ukončil službu{f' ({duration})' if duration else ''}.",
                status="Připojeno",
            )
        )
        reply = "Směna byla ukončena."
        if duration:
            reply += f" Délka: {duration}."
        if result.error:
            reply += f" FiveRoster je zapsaný, ale panel vyžaduje kontrolu: {result.error}"
        await interaction.followup.send(reply, ephemeral=True)

    async def _handle_loa_submission(
        self,
        interaction: discord.Interaction,
        member_id: int,
        start_value: str,
        end_value: str,
        reason_value: str,
    ) -> None:
        if interaction.user.id != member_id:
            await interaction.response.send_message(
                "Tento LOA formulář patří jinému uživateli.",
                ephemeral=True,
            )
            return
        try:
            start_date = parse_loa_date(start_value)
            end_date = parse_loa_date(end_value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if start_date > end_date:
            await interaction.response.send_message(
                "Datum začátku LOA nesmí být později než datum konce.",
                ephemeral=True,
            )
            return
        reason = " ".join(str(reason_value or "").split())
        if len(reason) < 2:
            await interaction.response.send_message("Uveďte důvod LOA.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not self._is_rename_target_channel(channel):
            await interaction.response.send_message(
                "LOA lze odeslat pouze z osobní složky.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self._shift_locks.setdefault(member_id, asyncio.Lock())
        async with lock:
            try:
                existing_message, marker, _ = await self._find_shift_panel_message(channel)
            except ShiftPanelLookupError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            if existing_message is None or marker is None or marker.member_id != member_id:
                await interaction.followup.send(
                    "Služební panel už není svázaný s vaším účtem.",
                    ephemeral=True,
                )
                return
            if self._fiveroster is None:
                await interaction.followup.send("FiveRoster klient není připravený.", ephemeral=True)
                return
            try:
                snapshot = await self._load_shift_snapshot(member_id, force=True)
            except FiveRosterError as exc:
                self._report_shift_panel_failure(channel, str(exc), title="LOA žádost selhala")
                await interaction.followup.send(f"LOA žádost selhala: {exc}", ephemeral=True)
                return
            if snapshot.status.on_shift:
                await interaction.followup.send(
                    "LOA nelze založit během aktivní směny. Nejdříve ukončete službu.",
                    ephemeral=True,
                )
                return
            overlap = loa_ranges_overlap(start_date, end_date, snapshot.loas)
            if overlap is not None:
                await interaction.followup.send(
                    (
                        "Termín se překrývá s čekající nebo schválenou LOA "
                        f"{overlap.start_date.strftime('%d.%m.%Y')}–"
                        f"{overlap.end_date.strftime('%d.%m.%Y')}."
                    ),
                    ephemeral=True,
                )
                return
            try:
                created = await self._fiveroster.create_loa(
                    member_id,
                    start_date,
                    end_date,
                    reason,
                )
            except FiveRosterError as original_error:
                created = None
                try:
                    confirmed_loas = await self._load_roster_loa(force=True)
                    created = next(
                        (
                            request
                            for request in confirmed_loas
                            if request.player_id == member_id
                            and request.start_date == start_date
                            and request.end_date == end_date
                            and request.reason == reason
                            and request.status in {"pending", "approved"}
                        ),
                        None,
                    )
                except FiveRosterError:
                    pass
                if created is None:
                    self._report_shift_panel_failure(
                        channel,
                        str(original_error),
                        title="LOA žádost selhala",
                    )
                    await interaction.followup.send(
                        f"LOA žádost selhala: {original_error}",
                        ephemeral=True,
                    )
                    return

            updated_snapshot = ShiftPanelSnapshot(
                status=snapshot.status,
                hours=snapshot.hours,
                quotas=snapshot.quotas,
                loas=tuple((*snapshot.loas, created)),
                fetched_at=datetime.now(timezone.utc),
            )
            self._roster_loa_cache = None
            self._cache_shift_snapshot(member_id, updated_snapshot)
            result = await self._upsert_shift_panel(
                channel,
                member_id,
                creator_id=marker.creator_id,
                snapshot=updated_snapshot,
            )

        LOGGER.info("Clen %s vytvoril LOA %s v kanalu %s.", member_id, created.id, channel.id)
        self._report(
            AppEvent(
                EventLevel.SUCCESS,
                "LOA žádost vytvořena",
                f"Člen {member_id} založil LOA {created.id} v kanálu {channel.id}.",
                status="Připojeno",
            )
        )
        reply = "LOA žádost byla odeslána ke schválení."
        if result.error:
            reply += f" FiveRoster je zapsaný, ale panel vyžaduje kontrolu: {result.error}"
        await interaction.followup.send(reply, ephemeral=True)

    async def _handle_loa_cancel(
        self,
        interaction: discord.Interaction,
        member_id: int,
        loa_id: int,
    ) -> None:
        if interaction.user.id != member_id:
            await interaction.response.send_message(
                "Toto potvrzení patří jinému uživateli.",
                ephemeral=True,
            )
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not self._is_rename_target_channel(channel):
            await interaction.response.send_message(
                "LOA lze zrušit pouze v osobní složce.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        lock = self._shift_locks.setdefault(member_id, asyncio.Lock())
        async with lock:
            try:
                existing_message, marker, _ = await self._find_shift_panel_message(channel)
            except ShiftPanelLookupError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            if existing_message is None or marker is None or marker.member_id != member_id:
                await interaction.followup.send(
                    "Služební panel už není svázaný s vaším účtem.",
                    ephemeral=True,
                )
                return
            if self._fiveroster is None:
                await interaction.followup.send("FiveRoster klient není připravený.", ephemeral=True)
                return
            try:
                all_loas = await self._load_roster_loa(force=True)
                base_snapshot = await self._load_shift_snapshot(member_id)
            except FiveRosterError as exc:
                self._report_shift_panel_failure(channel, str(exc), title="Zrušení LOA selhalo")
                await interaction.followup.send(f"Zrušení LOA selhalo: {exc}", ephemeral=True)
                return

            target = next(
                (
                    request
                    for request in all_loas
                    if request.id == loa_id
                    and request.player_id == member_id
                    and request.status in {"pending", "approved"}
                ),
                None,
            )
            cancelled_now = target is not None
            if target is not None:
                try:
                    await self._fiveroster.cancel_loa(loa_id)
                except FiveRosterError as original_error:
                    try:
                        confirmed_loas = await self._load_roster_loa(force=True)
                        still_cancelable = any(
                            request.id == loa_id
                            and request.player_id == member_id
                            and request.status in {"pending", "approved"}
                            for request in confirmed_loas
                        )
                    except FiveRosterError:
                        still_cancelable = True
                    if still_cancelable:
                        self._report_shift_panel_failure(
                            channel,
                            str(original_error),
                            title="Zrušení LOA selhalo",
                        )
                        await interaction.followup.send(
                            f"Zrušení LOA selhalo: {original_error}",
                            ephemeral=True,
                        )
                        return
                    all_loas = confirmed_loas

            remaining_all = tuple(request for request in all_loas if request.id != loa_id)
            self._roster_loa_cache = (asyncio.get_running_loop().time(), remaining_all)
            updated_snapshot = ShiftPanelSnapshot(
                status=base_snapshot.status,
                hours=base_snapshot.hours,
                quotas=base_snapshot.quotas,
                loas=tuple(
                    request for request in remaining_all if request.player_id == member_id
                ),
                fetched_at=datetime.now(timezone.utc),
            )
            self._cache_shift_snapshot(member_id, updated_snapshot)
            result = await self._upsert_shift_panel(
                channel,
                member_id,
                creator_id=marker.creator_id,
                snapshot=updated_snapshot,
            )

        if cancelled_now:
            LOGGER.info("Clen %s zrusil LOA %s v kanalu %s.", member_id, loa_id, channel.id)
            self._report(
                AppEvent(
                    EventLevel.SUCCESS,
                    "LOA zrušena",
                    f"{channel.name}: člen {member_id} zrušil LOA {loa_id}.",
                    status="Připojeno",
                )
            )
            reply = "LOA byla zrušena."
        else:
            reply = "LOA už nebyla aktivní; panel byl obnoven."
        if result.error:
            reply += f" FiveRoster je zapsaný, ale panel vyžaduje kontrolu: {result.error}"
        await interaction.edit_original_response(content=reply, view=None)

    async def _resolve_request_channel(
        self,
        guild: discord.Guild,
        reference: RequestChannelReference,
    ) -> discord.TextChannel | None:
        channel: object | None = None

        if reference.channel_id is not None:
            channel = guild.get_channel(reference.channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(reference.channel_id)
                except discord.NotFound:
                    channel = None
                except discord.Forbidden:
                    LOGGER.error(
                        "Bot nema pristup k vybranemu kanalu zadosti %s.",
                        reference.channel_id,
                    )
                    self._report(
                        AppEvent(
                            EventLevel.ERROR,
                            "Kanál žádosti není dostupný",
                            "Bot nemůže otevřít vybraný kanál žádosti. Zkontrolujte View Channels.",
                            status="Připojeno – chyba oprávnění",
                        )
                    )
                    return None
                except discord.HTTPException:
                    LOGGER.exception(
                        "Discord nevratil vybrany kanal zadosti %s.",
                        reference.channel_id,
                    )
                    self._report(
                        AppEvent(
                            EventLevel.ERROR,
                            "Kanál žádosti se nepodařilo načíst",
                            "Discord nevrátil vybraný kanál žádosti. Podrobnosti jsou v logu.",
                            status="Připojeno – chyba Discordu",
                        )
                    )
                    return None

        if channel is None and reference.channel_name:
            matches = [
                candidate
                for candidate in guild.text_channels
                if candidate.name.casefold() == reference.channel_name.casefold()
            ]
            if len(matches) == 1:
                channel = matches[0]
            elif len(matches) > 1:
                LOGGER.warning(
                    "Na serveru je vice kanalu zadosti s nazvem %s.",
                    reference.channel_name,
                )
                self._report(
                    AppEvent(
                        EventLevel.WARNING,
                        "Kanál žádosti není jednoznačný",
                        f"Na serveru je více kanálů s názvem {reference.channel_name}.",
                        status="Připojeno – upozornění",
                    )
                )
                return None

        if not isinstance(channel, discord.TextChannel):
            LOGGER.warning("Vybrany kanal zadosti nebyl nalezen nebo neni textovy kanal.")
            self._report(
                AppEvent(
                    EventLevel.WARNING,
                    "Kanál žádosti nebyl nalezen",
                    "Vybraný kanál žádosti neexistuje nebo není textový kanál.",
                    status="Připojeno – upozornění",
                )
            )
            return None

        if channel.guild.id != guild.id or not self._is_request_channel(channel):
            LOGGER.warning(
                "Kanal %s byl odmitnut jako zdroj zadosti; neodpovida povolenemu prefixu.",
                channel.id,
            )
            self._report(
                AppEvent(
                    EventLevel.WARNING,
                    "Neplatný kanál žádosti",
                    (
                        f"Kanál {channel.name} není platná žádost. "
                        "Název musí začínat povoleným prefixem."
                    ),
                    status="Připojeno – upozornění",
                )
            )
            return None

        return channel

    async def _read_ticket_form(
        self,
        channel: discord.TextChannel,
    ) -> TicketForm | None:
        try:
            async for source_message in channel.history(
                limit=self.settings.request_history_limit,
                oldest_first=False,
            ):
                if not self._is_ticket_tool_message(source_message) or not source_message.embeds:
                    continue
                ticket_form = parse_ticket_form(source_message.embeds)
                if ticket_form is not None:
                    return ticket_form
        except discord.Forbidden:
            LOGGER.error("Bot nema pristup k historii zadosti %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Historie žádosti není dostupná",
                    f"Bot nemůže číst historii kanálu {channel.name}.",
                    status="Připojeno – chyba oprávnění",
                )
            )
            return None
        except discord.HTTPException:
            LOGGER.exception("Nepodarilo se nacist historii zadosti %s.", channel.id)
            self._report(
                AppEvent(
                    EventLevel.ERROR,
                    "Načtení žádosti selhalo",
                    f"Nepodařilo se načíst historii kanálu {channel.name}.",
                    status="Připojeno – chyba Discordu",
                )
            )
            return None

        LOGGER.warning(
            "V kanalu zadosti %s nebyl nalezen podporovany formular.",
            channel.id,
        )
        self._report(
            AppEvent(
                EventLevel.WARNING,
                "Ve vybrané žádosti chybí formulář",
                (
                    f"V kanálu {channel.name} nebylo nalezeno jméno a pozice. "
                    "Osobní složka zůstala beze změny."
                ),
                status="Připojeno – upozornění",
            )
        )

        return None

    async def _scan_existing_channels(self) -> int:
        updated = 0

        for guild in self.guilds:
            for channel in guild.text_channels:
                if not self._is_rename_target_channel(channel):
                    continue

                channel_changed = False
                try:
                    async for message in channel.history(
                        limit=self.settings.scan_history_limit,
                        oldest_first=False,
                    ):
                        if not self._is_ticket_tool_message(message) or not message.embeds:
                            continue
                        if (
                            parse_ticket_form(message.embeds) is None
                            and parse_request_channel_reference(
                                message.embeds,
                                self.settings.request_channel_prefixes,
                            )
                            is None
                        ):
                            continue
                        if await self._maybe_rename_from_message(
                            message,
                            allow_onboarding=False,
                        ):
                            channel_changed = True
                        break

                    if self.settings.fiveroster_enabled:
                        _, onboarding_marker = await self._find_onboarding_message(channel)
                        if (
                            onboarding_marker is not None
                            and onboarding_marker.state
                            in {OnboardingState.COMPLETED, OnboardingState.PARTIAL}
                        ):
                            panel_result = await self._upsert_shift_panel(
                                channel,
                                onboarding_marker.member_id,
                                creator_id=None,
                            )
                            channel_changed = channel_changed or panel_result.changed
                except discord.Forbidden:
                    LOGGER.error(
                        "Bot nema pristup k historii kanalu %s.",
                        channel.id,
                    )
                    self._report(
                        AppEvent(
                            EventLevel.ERROR,
                            "Historie není dostupná",
                            f"Bot nemůže číst historii kanálu {channel.name}.",
                            status="Připojeno – chyba oprávnění",
                        )
                    )
                except discord.HTTPException:
                    LOGGER.exception("Nepodarilo se nacist historii kanalu %s.", channel.id)
                    self._report(
                        AppEvent(
                            EventLevel.ERROR,
                            "Kontrola osobní složky selhala",
                            f"Nepodařilo se načíst historii kanálu {channel.name}.",
                            status="Připojeno – chyba Discordu",
                        )
                    )

                if channel_changed:
                    updated += 1

        return updated


def run() -> None:
    settings = Settings.from_environment()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    TicketRenamerClient(settings).run(settings.discord_bot_token, log_handler=None)
