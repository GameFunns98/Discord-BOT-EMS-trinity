import asyncio
from datetime import date, datetime, timezone
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from ticket_renamer.bot import (
    FIVEROSTER_NAME_MARKER_VERSION,
    FIVEROSTER_NAME_PREFIX,
    PIN_MESSAGES_PERMISSION_BIT,
    TicketRenamerClient,
    _build_fiveroster_name_marker_embed,
    _parse_fiveroster_name_marker,
    _pin_messages_preflight,
)
from ticket_renamer.config import Settings
from ticket_renamer.fiveroster import (
    EnrollmentOutcome,
    EndedShift,
    FiveRosterError,
    FiveRosterRank,
    LoaRequest,
    QuotaProgress,
    ShiftHours,
    ShiftPeriodHours,
    ShiftStatus,
)
from ticket_renamer.onboarding import (
    ALL_ACTIONS,
    EnrollmentAction,
    OnboardingState,
    OnboardingView,
    actions_for_position,
    parse_onboarding_marker,
)
from ticket_renamer.parser import TicketForm
from ticket_renamer.shift_panel import ShiftAction, parse_shift_panel_marker


BOT_ID = 1444444444444444444
GUILD_ID = 999999999999999999
MEMBER_ID = 123456789012345678
OPERATOR_ROLE_ID = 1526254418784424168
REMOVE_ROLE_ID = 1480275608083632381
ADD_ROLE_IDS = frozenset(
    {
        1480275535002206411,
        1480681451870486651,
        1480680971660693677,
        1480680584450936873,
        1480680465957654770,
        1480353010273222807,
    }
)


class FakeRole:
    def __init__(self, role_id, name, position, *, managed=False):
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed


class DummyResponse:
    status = 403
    reason = "Forbidden"


class FakeMember:
    def __init__(
        self,
        member_id,
        roles,
        *,
        manage_roles=False,
        fail_edit=False,
    ):
        self.id = member_id
        self.roles = list(roles)
        self.top_role = max(self.roles, key=lambda role: role.position)
        self.guild_permissions = SimpleNamespace(manage_roles=manage_roles)
        self.fail_edit = fail_edit
        self.edits = []

    async def edit(self, *, roles, reason):
        if self.fail_edit:
            raise discord.Forbidden(DummyResponse(), "missing permissions")
        self.roles = list(roles)
        self.top_role = max(self.roles, key=lambda role: role.position)
        self.edits.append((list(roles), reason))
        return self


class FakeGuild:
    def __init__(self, *, target_fail_edit=False):
        self.id = GUILD_ID
        self.text_channels = []
        self.default_role = FakeRole(GUILD_ID, "@everyone", 0)
        self.roles = {
            role_id: FakeRole(role_id, f"role-{role_id}", 10 + index)
            for index, role_id in enumerate(sorted(ADD_ROLE_IDS | {REMOVE_ROLE_ID}))
        }
        self.bot_role = FakeRole(777777777777777777, "Ticket Renamer", 100)
        self.me = FakeMember(
            BOT_ID,
            [self.default_role, self.bot_role],
            manage_roles=True,
        )
        citizen_role = self.roles[REMOVE_ROLE_ID]
        unrelated_role = FakeRole(888888888888888888, "Unrelated", 5)
        self.target = FakeMember(
            MEMBER_ID,
            [self.default_role, unrelated_role, citizen_role],
            fail_edit=target_fail_edit,
        )
        self.target.guild = self
        self.members = {MEMBER_ID: self.target}

    def get_member(self, member_id):
        return self.members.get(member_id)

    async def fetch_member(self, member_id):
        member = self.get_member(member_id)
        if member is None:
            raise discord.NotFound(DummyResponse(), "not found")
        return member

    def get_role(self, role_id):
        return self.roles.get(role_id)


class FakeMessage:
    def __init__(
        self,
        *,
        message_id,
        channel,
        author_id,
        content="",
        embed=None,
        view=None,
        message_type=discord.MessageType.default,
        reference=None,
        delete_error=None,
    ):
        self.id = message_id
        self.channel = channel
        self.guild = channel.guild
        self.author = SimpleNamespace(id=author_id)
        self.content = content
        self.embeds = [embed] if embed is not None else []
        self.view = view
        self.edit_count = 0
        self.pinned = False
        self.pin_count = 0
        self.type = message_type
        self.reference = reference
        self.delete_error = delete_error
        self.delete_count = 0

    async def edit(
        self,
        *,
        embed=None,
        content=None,
        view=None,
        allowed_mentions=None,
    ):
        if embed is not None:
            self.embeds = [embed]
        if content is not None:
            self.content = content
        if view is not None:
            self.view = view
        self.edit_count += 1
        return self

    async def pin(self, *, reason):
        self.pinned = True
        self.pin_count += 1

    async def delete(self):
        if self.delete_error is not None:
            raise self.delete_error
        self.delete_count += 1


class FakeChannel:
    def __init__(
        self,
        guild,
        *,
        can_pin=True,
        can_manage_messages=True,
        send_delay=0.0,
    ):
        self.id = 333333333333333333
        self.name = "🚑・fero-lakatos"
        self.category_id = 1511618288373858435
        self.guild = guild
        self._messages = []
        self._next_message_id = 1
        self.can_pin = can_pin
        self.can_manage_messages = can_manage_messages
        self.send_delay = send_delay
        guild.text_channels.append(self)

    def permissions_for(self, member):
        return SimpleNamespace(
            send_messages=True,
            embed_links=True,
            pin_messages=self.can_pin,
            manage_messages=self.can_manage_messages,
            manage_channels=True,
        )

    def history(self, *, limit, oldest_first):
        async def iterator():
            messages = self._messages if oldest_first else list(reversed(self._messages))
            selected = messages if limit is None else messages[:limit]
            for message in selected:
                yield message

        return iterator()

    def pins(self, *, limit, oldest_first):
        async def iterator():
            messages = [message for message in self._messages if message.pinned]
            if not oldest_first:
                messages.reverse()
            for message in messages[:limit]:
                yield message

        return iterator()

    async def send(
        self,
        *,
        embed=None,
        content=None,
        view=None,
        allowed_mentions=None,
    ):
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        message = FakeMessage(
            message_id=self._next_message_id,
            channel=self,
            author_id=BOT_ID,
            content=content or "",
            embed=embed,
            view=view,
        )
        self._next_message_id += 1
        self._messages.append(message)
        return message

    async def fetch_message(self, message_id):
        for message in self._messages:
            if message.id == message_id:
                return message
        raise discord.NotFound(DummyResponse(), "not found")


class FakeFiveRoster:
    def __init__(self, *, error=None, already_enrolled=False, delay=0, callsign="A-01"):
        self.error = error
        self.already_enrolled = already_enrolled
        self.delay = delay
        self.callsign = callsign
        self.calls = []
        self.closed = False
        self.shift_status = ShiftStatus(on_shift=False)
        period = ShiftPeriodHours(hours=0, formatted="0h", shift_count=0)
        self.shift_hours = ShiftHours(weekly=period, monthly=period, total=period)
        self.quotas = ()
        self.loas = []
        self.shift_start_calls = []
        self.shift_end_calls = []

    async def start(self):
        return None

    async def close(self):
        self.closed = True

    async def enroll(self, member_id, rank_key):
        self.calls.append((member_id, rank_key))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return EnrollmentOutcome(
            rank=FiveRosterRank(rank_key, f"uuid-{rank_key}", EnrollmentAction(rank_key).label),
            already_enrolled=self.already_enrolled,
            callsign=self.callsign,
        )

    async def get_player_callsign(self, member_id):
        return self.callsign

    async def is_player_enrolled(self, member_id):
        return True

    async def get_shift_status(self, member_id):
        return self.shift_status

    async def get_shift_hours(self, member_id):
        return self.shift_hours

    async def get_quota_progress(self, member_id):
        return self.quotas

    async def list_loa(self):
        return tuple(self.loas)

    async def start_shift(self, member_id):
        self.shift_start_calls.append(member_id)
        self.shift_status = ShiftStatus(
            on_shift=True,
            shift_id=1,
            started_at=datetime.now(timezone.utc),
            formatted_duration="0h",
        )
        return self.shift_status

    async def end_shift(self, member_id):
        self.shift_end_calls.append(member_id)
        self.shift_status = ShiftStatus(on_shift=False)
        return EndedShift(1, None, datetime.now(timezone.utc), 60, "1m")

    async def create_loa(self, member_id, start_date, end_date, reason):
        request = LoaRequest(1, member_id, start_date, end_date, reason, "pending")
        self.loas.append(request)
        return request

    async def cancel_loa(self, loa_id):
        self.loas = [request for request in self.loas if request.id != loa_id]


class FakeResponseState:
    def __init__(self):
        self.messages = []
        self.deferred = False

    def is_done(self):
        return self.deferred or bool(self.messages)

    async def send_message(self, message, *, ephemeral):
        self.messages.append((message, ephemeral))

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = True

    async def send_modal(self, modal):
        self.messages.append((modal, True))


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, message, *, ephemeral, view=None):
        self.messages.append((message, ephemeral, view))


class FakeInteraction:
    def __init__(
        self,
        *,
        guild,
        channel,
        message=None,
        role_ids=(),
        user_id=555555555555555555,
        administrator=False,
    ):
        self.guild = guild
        self.channel = channel
        self.message = message
        self.user = SimpleNamespace(
            id=user_id,
            roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
            guild_permissions=SimpleNamespace(administrator=administrator),
        )
        self.response = FakeResponseState()
        self.followup = FakeFollowup()
        self.original_edits = []

    async def edit_original_response(self, *, content, view):
        self.original_edits.append((content, view))


def settings():
    return Settings(
        discord_bot_token="test-token",
        ticket_tool_bot_ids=frozenset({1325579039888511056}),
        ticket_category_ids=frozenset({1511618288373858435}),
        request_channel_prefixes=("zadost-",),
        request_history_limit=100,
        channel_separator="・",
        scan_existing_tickets=False,
        scan_history_limit=25,
        log_level=logging.INFO,
        fiveroster_api_key="secret",
        fiveroster_roster_uuid="roster",
        onboarding_operator_role_ids=frozenset({OPERATOR_ROLE_ID}),
        onboarding_add_role_ids=ADD_ROLE_IDS,
        onboarding_remove_role_ids=frozenset({REMOVE_ROLE_ID}),
    )


def marker_from(message):
    return parse_onboarding_marker(message.embeds[0])


def shift_marker_from(message):
    return parse_shift_panel_marker(message.embeds[0]) if message.embeds else None


def name_command_messages(channel):
    return [
        message
        for message in channel._messages
        if message.content.startswith(FIVEROSTER_NAME_PREFIX)
    ]


def name_marker_from(message):
    for embed in message.embeds:
        marker = _parse_fiveroster_name_marker(embed)
        if marker is not None:
            return marker
    return None


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.guild = FakeGuild()
        self.channel = FakeChannel(self.guild)
        self.fiveroster = FakeFiveRoster()
        self.client = TicketRenamerClient(
            settings(),
            reporter=self.events.append,
            fiveroster_client=self.fiveroster,
        )
        self.client._connection.user = SimpleNamespace(id=BOT_ID)

    async def asyncTearDown(self):
        await self.client.close()

    async def _complete_security_onboarding(self):
        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Ochranka"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_onboarding_interaction(
                interaction,
                EnrollmentAction.SECURITY,
            )
        return onboarding_message

    def test_position_actions_and_persistent_view(self):
        self.assertEqual(
            actions_for_position("Záchranář"),
            (EnrollmentAction.PARAMEDIC, EnrollmentAction.ACADEMY),
        )
        self.assertEqual(
            actions_for_position("Doktor"),
            (EnrollmentAction.DOCTOR, EnrollmentAction.DOCTOR_TRAINING),
        )
        self.assertEqual(actions_for_position("Ochranka"), (EnrollmentAction.SECURITY,))
        view = OnboardingView(self.client._handle_onboarding_interaction, ALL_ACTIONS)
        self.assertIsNone(view.timeout)
        self.assertEqual(len({item.custom_id for item in view.children}), 5)
        self.assertIsNotNone(self.client.tree.get_command("sluzebni-panel"))

    def test_pin_messages_preflight_uses_discord_py_27_attribute(self):
        granted = SimpleNamespace(
            administrator=False,
            pin_messages=True,
            value=0,
        )
        denied = SimpleNamespace(
            administrator=False,
            pin_messages=False,
            value=PIN_MESSAGES_PERMISSION_BIT,
        )

        self.assertIs(_pin_messages_preflight(granted), True)
        self.assertIs(_pin_messages_preflight(denied), False)

    def test_pin_messages_preflight_reads_raw_bit_51(self):
        permissions = SimpleNamespace(
            administrator=False,
            value=PIN_MESSAGES_PERMISSION_BIT,
        )

        self.assertIs(_pin_messages_preflight(permissions), True)

    def test_pin_messages_preflight_rejects_manage_messages_only(self):
        permissions = SimpleNamespace(
            administrator=False,
            manage_messages=True,
            value=1 << 13,
        )

        self.assertIs(_pin_messages_preflight(permissions), False)

    def test_pin_messages_preflight_accepts_administrator(self):
        permissions = SimpleNamespace(
            administrator=True,
            pin_messages=False,
            value=0,
        )

        self.assertIs(_pin_messages_preflight(permissions), True)

    def test_pin_messages_preflight_returns_unknown_without_permission_data(self):
        self.assertIsNone(_pin_messages_preflight(None))
        self.assertIsNone(_pin_messages_preflight(object()))

    async def test_pending_buttons_are_idempotent_without_name_command(self):
        form = TicketForm("Fero Lakatoš", "Záchranář")

        first = await self.client._upsert_onboarding(self.channel, form, MEMBER_ID)
        second = await self.client._upsert_onboarding(self.channel, form, MEMBER_ID)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(self.channel._messages), 1)
        onboarding_message = self.channel._messages[0]
        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.PENDING)
        self.assertEqual(
            [item.label for item in onboarding_message.view.children],
            ["Paramedic", "Akademie"],
        )
        self.assertEqual(name_command_messages(self.channel), [])

    async def test_security_requires_operator_confirmation(self):
        changed = await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Ochranka"),
            MEMBER_ID,
        )

        self.assertTrue(changed)
        onboarding_message = self.channel._messages[0]
        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.PENDING)
        self.assertFalse(onboarding_message.view.children[0].disabled)
        self.assertEqual(self.fiveroster.calls, [])

    async def test_security_failure_exposes_retry_button_without_role_change(self):
        self.fiveroster.error = FiveRosterError("API je nedostupné")

        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Ochranka"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_onboarding_interaction(
                interaction,
                EnrollmentAction.SECURITY,
            )

        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.ERROR)
        self.assertEqual(onboarding_message.view.children[0].label, "Opakovat Security")
        self.assertFalse(onboarding_message.view.children[0].disabled)
        self.assertEqual(self.guild.target.edits, [])

    async def test_security_startup_error_is_recovered_on_confirmation(self):
        self.client._fiveroster_error = "Hodnost Security nebyla načtena"
        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Ochranka"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.PENDING)
        self.assertEqual(self.fiveroster.calls, [])

        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_onboarding_interaction(
                interaction,
                EnrollmentAction.SECURITY,
            )

        self.assertEqual(self.fiveroster.calls, [(MEMBER_ID, "security")])
        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.COMPLETED)

    async def test_unauthorized_click_is_rejected(self):
        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Záchranář"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_onboarding_interaction(
                interaction,
                EnrollmentAction.PARAMEDIC,
            )

        self.assertIn("Nemáte oprávnění", interaction.response.messages[0][0])
        self.assertEqual(self.fiveroster.calls, [])
        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.PENDING)

    async def test_authorized_click_enrolls_and_disables_all_buttons(self):
        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Záchranář"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_onboarding_interaction(
                interaction,
                EnrollmentAction.PARAMEDIC,
            )

        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.COMPLETED)
        self.assertTrue(all(item.disabled for item in onboarding_message.view.children))
        self.assertEqual(self.fiveroster.calls, [(MEMBER_ID, "paramedic")])
        self.assertIn("Hotovo", interaction.followup.messages[-1][0])
        commands = name_command_messages(self.channel)
        self.assertEqual(len(commands), 1)
        self.assertIn("/nameinradio A-01 F. Lakatoš", commands[0].content)
        self.assertTrue(commands[0].pinned)
        self.assertEqual(commands[0].pin_count, 1)
        radio_marker = name_marker_from(commands[0])
        self.assertIsNotNone(radio_marker)
        self.assertEqual(radio_marker.member_id, MEMBER_ID)
        self.assertEqual(radio_marker.version, FIVEROSTER_NAME_MARKER_VERSION)

    async def test_name_command_uses_literal_placeholder_only_when_callsign_missing(self):
        self.fiveroster.callsign = None
        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Ochranka"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_onboarding_interaction(
                interaction,
                EnrollmentAction.SECURITY,
            )

        command = name_command_messages(self.channel)[0]
        self.assertIn("/nameinradio [volačka] F. Lakatoš", command.content)
        self.assertIn("nevrátil konkrétní volačku", command.content)
        self.assertTrue(command.pinned)

    async def test_name_command_survives_failed_final_onboarding_embed_edit(self):
        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Ochranka"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )

        with (
            patch("ticket_renamer.bot.discord.TextChannel", FakeChannel),
            patch.object(
                self.client,
                "_edit_onboarding_message",
                AsyncMock(side_effect=[True, False]),
            ),
        ):
            await self.client._handle_onboarding_interaction(
                interaction,
                EnrollmentAction.SECURITY,
            )

        command = name_command_messages(self.channel)[0]
        self.assertIn("/nameinradio A-01 F. Lakatoš", command.content)
        self.assertTrue(command.pinned)
        self.assertIn("Stav tlačítek se ale nepodařilo uložit", interaction.followup.messages[-1][0])

    async def test_name_command_is_found_beyond_100_messages_after_restart(self):
        await self._complete_security_onboarding()
        command = name_command_messages(self.channel)[0]
        command.pinned = False
        for index in range(110):
            await self.channel.send(content=f"provozní zpráva {index}")

        restarted = TicketRenamerClient(
            settings(),
            reporter=self.events.append,
            fiveroster_client=self.fiveroster,
        )
        restarted._connection.user = SimpleNamespace(id=BOT_ID)
        try:
            changed = await restarted._upsert_fiveroster_name_message(
                self.channel,
                "Fero Lakatoš",
                MEMBER_ID,
                callsign="A-01",
            )
        finally:
            await restarted.close()

        self.assertTrue(changed)
        self.assertEqual(len(name_command_messages(self.channel)), 1)
        self.assertTrue(command.pinned)
        self.assertEqual(command.pin_count, 2)

    async def test_name_command_migrates_single_legacy_prefix_message(self):
        legacy = await self.channel.send(
            content=f"{FIVEROSTER_NAME_PREFIX}\n```\nF. Lakatoš\n```"
        )

        changed = await self.client._upsert_fiveroster_name_message(
            self.channel,
            "Fero Lakatoš",
            MEMBER_ID,
            callsign="A-01",
        )

        commands = name_command_messages(self.channel)
        self.assertTrue(changed)
        self.assertEqual(commands, [legacy])
        self.assertEqual(legacy.edit_count, 1)
        self.assertIn("/nameinradio A-01 F. Lakatoš", legacy.content)
        self.assertTrue(legacy.pinned)
        marker = name_marker_from(legacy)
        self.assertIsNotNone(marker)
        self.assertEqual(marker.member_id, MEMBER_ID)
        self.assertEqual(marker.version, FIVEROSTER_NAME_MARKER_VERSION)

    async def test_name_command_foreign_member_marker_stops_update(self):
        foreign_member_id = 987654321098765432
        foreign = await self.channel.send(
            content=f"{FIVEROSTER_NAME_PREFIX}\n```\nCizí jméno\n```",
            embed=_build_fiveroster_name_marker_embed(foreign_member_id),
        )
        original_content = foreign.content

        changed = await self.client._upsert_fiveroster_name_message(
            self.channel,
            "Fero Lakatoš",
            MEMBER_ID,
            callsign="A-01",
        )

        self.assertFalse(changed)
        self.assertEqual(name_command_messages(self.channel), [foreign])
        self.assertEqual(foreign.content, original_content)
        self.assertEqual(foreign.edit_count, 0)
        self.assertFalse(foreign.pinned)
        self.assertEqual(self.events[-1].title, "Příkaz /nameinradio vyžaduje kontrolu")
        self.assertIn("jiným Discord uživatelem", self.events[-1].message)

    async def test_multiple_legacy_name_messages_stop_without_creating_another(self):
        first = await self.channel.send(
            content=f"{FIVEROSTER_NAME_PREFIX}\n```\nF. První\n```"
        )
        second = await self.channel.send(
            content=f"{FIVEROSTER_NAME_PREFIX}\n```\nF. Druhý\n```"
        )

        changed = await self.client._upsert_fiveroster_name_message(
            self.channel,
            "Fero Lakatoš",
            MEMBER_ID,
            callsign="A-01",
        )

        self.assertFalse(changed)
        self.assertEqual(name_command_messages(self.channel), [first, second])
        self.assertEqual(first.edit_count, 0)
        self.assertEqual(second.edit_count, 0)
        self.assertIsNone(name_marker_from(first))
        self.assertIsNone(name_marker_from(second))
        self.assertEqual(self.events[-1].title, "Příkaz /nameinradio vyžaduje kontrolu")
        self.assertIn("více zpráv", self.events[-1].message)

    async def test_concurrent_name_message_upserts_create_only_one_message(self):
        self.channel.send_delay = 0.02

        results = await asyncio.gather(
            self.client._upsert_fiveroster_name_message(
                self.channel,
                "Fero Lakatoš",
                MEMBER_ID,
                callsign="A-01",
            ),
            self.client._upsert_fiveroster_name_message(
                self.channel,
                "Fero Lakatoš",
                MEMBER_ID,
                callsign="A-01",
            ),
        )

        commands = name_command_messages(self.channel)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len(commands), 1)
        self.assertTrue(commands[0].pinned)
        self.assertEqual(commands[0].pin_count, 1)
        self.assertEqual(name_marker_from(commands[0]).member_id, MEMBER_ID)

    async def test_api_failure_keeps_rank_buttons_enabled(self):
        self.fiveroster.error = FiveRosterError("API je nedostupné")
        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Doktor"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_onboarding_interaction(
                interaction,
                EnrollmentAction.DOCTOR_TRAINING,
            )

        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.ERROR)
        self.assertTrue(all(not item.disabled for item in onboarding_message.view.children))
        self.assertEqual(self.guild.target.edits, [])

    async def test_concurrent_clicks_only_enroll_once(self):
        self.fiveroster.delay = 0.02
        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Záchranář"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        first = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        second = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await asyncio.gather(
                self.client._handle_onboarding_interaction(first, EnrollmentAction.PARAMEDIC),
                self.client._handle_onboarding_interaction(second, EnrollmentAction.PARAMEDIC),
            )

        self.assertEqual(self.fiveroster.calls, [(MEMBER_ID, "paramedic")])
        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.COMPLETED)

    async def test_role_failure_marks_completed_enrollment_as_partial(self):
        self.guild.target.fail_edit = True

        result = await self.client._execute_onboarding(
            guild=self.guild,
            channel=self.channel,
            member_id=MEMBER_ID,
            action=EnrollmentAction.PARAMEDIC,
            actor_id=555555555555555555,
        )

        self.assertEqual(result.state, OnboardingState.PARTIAL)
        self.assertEqual(result.callsign, "A-01")
        self.assertEqual(self.fiveroster.calls, [(MEMBER_ID, "paramedic")])
        self.assertIn("opravte ručně", result.detail)

    async def test_partial_interactive_onboarding_still_creates_shift_panel(self):
        self.guild.target.fail_edit = True
        await self.client._upsert_onboarding(
            self.channel,
            TicketForm("Fero Lakatoš", "Záchranář"),
            MEMBER_ID,
        )
        onboarding_message = self.channel._messages[0]
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=onboarding_message,
            role_ids=(OPERATOR_ROLE_ID,),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_onboarding_interaction(
                interaction,
                EnrollmentAction.PARAMEDIC,
            )

        self.assertEqual(marker_from(onboarding_message).state, OnboardingState.PARTIAL)
        panel = next(message for message in self.channel._messages if shift_marker_from(message))
        self.assertTrue(panel.pinned)
        command = name_command_messages(self.channel)[0]
        self.assertIn("/nameinradio A-01 F. Lakatoš", command.content)
        self.assertTrue(command.pinned)

    async def test_existing_same_rank_still_synchronizes_roles(self):
        self.fiveroster.already_enrolled = True

        result = await self.client._execute_onboarding(
            guild=self.guild,
            channel=self.channel,
            member_id=MEMBER_ID,
            action=EnrollmentAction.PARAMEDIC,
            actor_id=555555555555555555,
        )

        self.assertEqual(result.state, OnboardingState.COMPLETED)
        self.assertEqual(result.callsign, "A-01")
        self.assertIn("už byl", result.detail)
        self.assertEqual(len(self.guild.target.edits), 1)

    async def test_completed_onboarding_creates_one_pinned_shift_panel(self):
        form = TicketForm("Fero Lakatoš", "Ochranka")
        await self._complete_security_onboarding()
        await self.client._upsert_onboarding(self.channel, form, MEMBER_ID)

        panels = [message for message in self.channel._messages if shift_marker_from(message)]
        self.assertEqual(len(panels), 1)
        self.assertTrue(panels[0].pinned)
        self.assertEqual(panels[0].pin_count, 1)
        self.assertEqual(shift_marker_from(panels[0]).member_id, MEMBER_ID)

    async def test_new_manual_pin_notice_for_bot_message_deletes_only_notice(self):
        target = await self.channel.send(content="bot panel")
        target.pinned = True
        notice = FakeMessage(
            message_id=900000000000000001,
            channel=self.channel,
            author_id=555555555555555555,
            message_type=discord.MessageType.pins_add,
            reference=SimpleNamespace(
                message_id=target.id,
                channel_id=self.channel.id,
            ),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client.on_message(notice)

        self.assertEqual(notice.delete_count, 1)
        self.assertEqual(target.delete_count, 0)
        self.assertTrue(target.pinned)

    async def test_pin_notice_for_non_bot_message_is_preserved(self):
        target = FakeMessage(
            message_id=900000000000000002,
            channel=self.channel,
            author_id=MEMBER_ID,
            content="běžná zpráva uživatele",
        )
        target.pinned = True
        self.channel._messages.append(target)
        notice = FakeMessage(
            message_id=900000000000000003,
            channel=self.channel,
            author_id=555555555555555555,
            message_type=discord.MessageType.pins_add,
            reference=SimpleNamespace(
                message_id=target.id,
                channel_id=self.channel.id,
            ),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client.on_message(notice)

        self.assertEqual(notice.delete_count, 0)
        self.assertEqual(target.delete_count, 0)

    async def test_only_exact_pins_add_in_personal_folder_is_cleaned(self):
        scenarios = []
        regular_target = await self.channel.send(content="bot panel")
        scenarios.append(
            FakeMessage(
                message_id=900000000000000004,
                channel=self.channel,
                author_id=BOT_ID,
                message_type=discord.MessageType.default,
                reference=SimpleNamespace(
                    message_id=regular_target.id,
                    channel_id=self.channel.id,
                ),
            )
        )

        request_channel = FakeChannel(self.guild)
        request_channel.name = "zadost-123"
        request_target = await request_channel.send(content="bot panel")
        scenarios.append(
            FakeMessage(
                message_id=900000000000000005,
                channel=request_channel,
                author_id=BOT_ID,
                message_type=discord.MessageType.pins_add,
                reference=SimpleNamespace(
                    message_id=request_target.id,
                    channel_id=request_channel.id,
                ),
            )
        )

        unrelated_channel = FakeChannel(self.guild)
        unrelated_channel.category_id = 777777777777777777
        unrelated_target = await unrelated_channel.send(content="bot panel")
        scenarios.append(
            FakeMessage(
                message_id=900000000000000006,
                channel=unrelated_channel,
                author_id=BOT_ID,
                message_type=discord.MessageType.pins_add,
                reference=SimpleNamespace(
                    message_id=unrelated_target.id,
                    channel_id=unrelated_channel.id,
                ),
            )
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            for notice in scenarios:
                await self.client.on_message(notice)

        self.assertTrue(all(notice.delete_count == 0 for notice in scenarios))

    async def test_pin_notice_cleanup_requires_manage_messages(self):
        channel = FakeChannel(self.guild, can_manage_messages=False)
        target = await channel.send(content="bot panel")
        notice = FakeMessage(
            message_id=900000000000000007,
            channel=channel,
            author_id=555555555555555555,
            message_type=discord.MessageType.pins_add,
            reference=SimpleNamespace(
                message_id=target.id,
                channel_id=channel.id,
            ),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client.on_message(notice)

        self.assertEqual(notice.delete_count, 0)
        self.assertEqual(target.delete_count, 0)
        self.assertEqual(self.events[-1].title, "Hlášku o připnutí nelze odstranit")
        self.assertIn("Spravovat zprávy", self.events[-1].message)

    async def test_pin_notice_cleanup_accepts_administrator_without_manage_messages(self):
        channel = FakeChannel(self.guild, can_manage_messages=False)
        channel.permissions_for = lambda member: SimpleNamespace(
            administrator=True,
            manage_messages=False,
        )
        target = await channel.send(content="bot panel")
        notice = FakeMessage(
            message_id=900000000000000008,
            channel=channel,
            author_id=555555555555555555,
            message_type=discord.MessageType.pins_add,
            reference=SimpleNamespace(
                message_id=target.id,
                channel_id=channel.id,
            ),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client.on_message(notice)

        self.assertEqual(notice.delete_count, 1)
        self.assertEqual(target.delete_count, 0)
        self.assertEqual(self.events, [])

    async def test_pin_notice_delete_errors_are_safe_and_reported(self):
        target = await self.channel.send(content="bot panel")
        errors = (
            discord.Forbidden(DummyResponse(), "missing permissions"),
            discord.HTTPException(DummyResponse(), "temporary failure"),
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            for index, error in enumerate(errors, start=1):
                with self.subTest(error=type(error).__name__):
                    notice = FakeMessage(
                        message_id=900000000000000010 + index,
                        channel=self.channel,
                        author_id=BOT_ID,
                        message_type=discord.MessageType.pins_add,
                        reference=SimpleNamespace(
                            message_id=target.id,
                            channel_id=self.channel.id,
                        ),
                        delete_error=error,
                    )
                    await self.client.on_message(notice)
                    self.assertEqual(notice.delete_count, 0)
                    self.assertEqual(
                        self.events[-1].title,
                        "Hlášku o připnutí nelze odstranit",
                    )

        self.assertEqual(target.delete_count, 0)

    async def test_historical_scan_does_not_delete_old_pin_notice(self):
        target = await self.channel.send(content="starší bot panel")
        old_notice = FakeMessage(
            message_id=900000000000000020,
            channel=self.channel,
            author_id=BOT_ID,
            message_type=discord.MessageType.pins_add,
            reference=SimpleNamespace(
                message_id=target.id,
                channel_id=self.channel.id,
            ),
        )
        self.channel._messages.append(old_notice)
        self.client._connection._guilds[self.guild.id] = self.guild

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            updated = await self.client._scan_existing_channels()

        self.assertEqual(updated, 0)
        self.assertEqual(old_notice.delete_count, 0)
        self.assertEqual(target.delete_count, 0)

    async def test_historical_scan_finds_onboarding_marker_beyond_100_messages(self):
        onboarding_message = await self._complete_security_onboarding()
        command = name_command_messages(self.channel)[0]
        self.channel._messages.remove(command)
        for index in range(110):
            await self.channel.send(content=f"historická provozní zpráva {index}")
        onboarding_depth = (
            len(self.channel._messages)
            - 1
            - self.channel._messages.index(onboarding_message)
        )
        self.assertGreater(onboarding_depth, 100)
        self.client._connection._guilds[self.guild.id] = self.guild

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            updated = await self.client._scan_existing_channels()

        commands = name_command_messages(self.channel)
        self.assertGreaterEqual(updated, 1)
        self.assertEqual(len(commands), 1)
        self.assertIn("/nameinradio A-01 F. Lakatoš", commands[0].content)
        self.assertTrue(commands[0].pinned)

    async def test_only_panel_owner_can_start_and_end_shift(self):
        await self._complete_security_onboarding()
        panel = next(message for message in self.channel._messages if shift_marker_from(message))
        denied = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=panel,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_interaction(denied, ShiftAction.START)
        self.assertIn("pouze zaměstnanec", denied.response.messages[0][0])
        self.assertEqual(self.fiveroster.shift_start_calls, [])

        owner = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=panel,
            user_id=MEMBER_ID,
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_interaction(owner, ShiftAction.START)
        self.assertEqual(self.fiveroster.shift_start_calls, [MEMBER_ID])
        self.assertIn("ve službě", panel.embeds[0].title)

        ending = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=panel,
            user_id=MEMBER_ID,
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_interaction(ending, ShiftAction.END)
        self.assertEqual(self.fiveroster.shift_end_calls, [MEMBER_ID])
        self.assertIn("mimo službu", panel.embeds[0].title)

    async def test_concurrent_shift_start_is_posted_once(self):
        await self._complete_security_onboarding()
        panel = next(message for message in self.channel._messages if shift_marker_from(message))
        first = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=panel,
            user_id=MEMBER_ID,
        )
        second = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=panel,
            user_id=MEMBER_ID,
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await asyncio.gather(
                self.client._handle_shift_panel_interaction(first, ShiftAction.START),
                self.client._handle_shift_panel_interaction(second, ShiftAction.START),
            )
        self.assertEqual(self.fiveroster.shift_start_calls, [MEMBER_ID])

    async def test_manual_refresh_bypasses_snapshot_cache(self):
        await self._complete_security_onboarding()
        panel = next(message for message in self.channel._messages if shift_marker_from(message))
        original_get_status = self.fiveroster.get_shift_status
        self.fiveroster.get_shift_status = AsyncMock(wraps=original_get_status)
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=panel,
            user_id=MEMBER_ID,
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_interaction(interaction, ShiftAction.REFRESH)

        self.assertEqual(self.fiveroster.get_shift_status.await_count, 1)
        self.assertIn("aktuální", interaction.followup.messages[-1][0])

    async def test_shift_api_failure_is_reported_without_starting(self):
        await self._complete_security_onboarding()
        panel = next(message for message in self.channel._messages if shift_marker_from(message))
        self.fiveroster.get_shift_status = AsyncMock(
            side_effect=FiveRosterError("API je dočasně nedostupné")
        )
        interaction = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            message=panel,
            user_id=MEMBER_ID,
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_interaction(interaction, ShiftAction.START)

        self.assertEqual(self.fiveroster.shift_start_calls, [])
        self.assertIn("selhal", interaction.followup.messages[-1][0])

    async def test_manual_command_requires_leadership_and_records_creator(self):
        denied = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            administrator=True,
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_command(denied, self.guild.target)
        self.assertIn("Nemáte oprávnění", denied.response.messages[0][0])

        allowed = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_command(allowed, self.guild.target)

        panel = next(message for message in self.channel._messages if shift_marker_from(message))
        self.assertEqual(shift_marker_from(panel).creator_id, allowed.user.id)
        self.assertTrue(panel.pinned)
        self.assertIn("vytvořen a připnut", allowed.followup.messages[-1][0])

    async def test_manual_command_rejects_request_channel_and_unenrolled_member(self):
        request_channel = FakeChannel(self.guild)
        request_channel.name = "zadost-123"
        denied_channel = FakeInteraction(
            guild=self.guild,
            channel=request_channel,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_command(denied_channel, self.guild.target)
        self.assertIn("pouze přímo", denied_channel.response.messages[0][0])
        self.assertEqual(request_channel._messages, [])

        self.fiveroster.is_player_enrolled = AsyncMock(return_value=False)
        unenrolled = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_command(unenrolled, self.guild.target)
        self.assertIn("není zapsaný", unenrolled.followup.messages[-1][0])
        self.assertFalse(any(shift_marker_from(message) for message in self.channel._messages))

    async def test_unpinned_old_panel_is_found_without_creating_duplicate(self):
        first = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_command(first, self.guild.target)
        panel = next(message for message in self.channel._messages if shift_marker_from(message))
        panel.pinned = False
        for _ in range(110):
            await self.channel.send(content="provozní zpráva")

        second = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_command(second, self.guild.target)

        panels = [message for message in self.channel._messages if shift_marker_from(message)]
        self.assertEqual(panels, [panel])
        self.assertTrue(panel.pinned)

    async def test_missing_pin_permission_keeps_panel_and_reports_error(self):
        channel = FakeChannel(self.guild, can_pin=False)
        interaction = FakeInteraction(
            guild=self.guild,
            channel=channel,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_command(interaction, self.guild.target)

        panel = next(message for message in channel._messages if shift_marker_from(message))
        self.assertFalse(panel.pinned)
        self.assertIn("vyžaduje kontrolu", interaction.followup.messages[-1][0])
        self.assertTrue(any(event.title == "Služební panel není připnutý" for event in self.events))

    async def test_manual_command_refuses_panel_for_different_member(self):
        first = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        other_member = SimpleNamespace(
            id=223456789012345678,
            guild=self.guild,
        )
        second = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_shift_panel_command(first, self.guild.target)
            await self.client._handle_shift_panel_command(second, other_member)

        panels = [message for message in self.channel._messages if shift_marker_from(message)]
        self.assertEqual(len(panels), 1)
        self.assertIn("jiným Discord uživatelem", second.followup.messages[-1][0])

    async def test_loa_submission_overlap_and_cancel(self):
        await self._complete_security_onboarding()
        submission = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            user_id=MEMBER_ID,
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_loa_submission(
                submission,
                MEMBER_ID,
                "20.08.2026",
                "22.08.2026",
                "Dovolená",
            )
        self.assertEqual(len(self.fiveroster.loas), 1)
        self.assertIn("odeslána", submission.followup.messages[-1][0])

        overlap = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            user_id=MEMBER_ID,
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_loa_submission(
                overlap,
                MEMBER_ID,
                "22.08.2026",
                "23.08.2026",
                "Další žádost",
            )
        self.assertEqual(len(self.fiveroster.loas), 1)
        self.assertIn("překrývá", overlap.followup.messages[-1][0])

        cancellation = FakeInteraction(
            guild=self.guild,
            channel=self.channel,
            user_id=MEMBER_ID,
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_loa_cancel(cancellation, MEMBER_ID, 1)
        self.assertEqual(self.fiveroster.loas, [])
        self.assertIn("zrušena", cancellation.original_edits[-1][0])


if __name__ == "__main__":
    unittest.main()
