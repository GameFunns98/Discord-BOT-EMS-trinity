import asyncio
import copy
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from ticket_renamer.bot import TicketRenamerClient
from ticket_renamer.config import Settings
from ticket_renamer.manual_fallback import (
    ManualFallbackSource,
    ManualFallbackState,
    ManualRecoveryAction,
    ManualRecoveryView,
    ManualSubmission,
    ManualValidationError,
    build_manual_fallback_embed,
    is_recognizable_folder_embed,
    parse_manual_fallback_embed,
    validate_manual_ticket_form,
)
from ticket_renamer.parser import TicketForm


TICKET_TOOL_BOT_ID = 1325579039888511056
RENAMER_BOT_ID = 1444444444444444444
FOLDER_CATEGORY_ID = 1511618288373858435
OPERATOR_ROLE_ID = 1526254418784424168
MEMBER_ID = 123456789012345678
GUILD_ID = 999999999999999999


def folder_embed(fields):
    return SimpleNamespace(
        fields=[SimpleNamespace(name=name, value=value) for name, value in fields],
        description=None,
    )


class FakeMessage:
    next_id = 1

    def __init__(self, channel, embeds, author_id, *, content="", view=None):
        self.id = FakeMessage.next_id
        FakeMessage.next_id += 1
        self.channel = channel
        self.guild = channel.guild
        self.embeds = list(embeds)
        self.author = SimpleNamespace(id=author_id)
        self.content = content
        self.view = view
        self.edit_count = 0

    async def edit(
        self,
        *,
        embed=None,
        view=None,
        content=None,
        allowed_mentions=None,
    ):
        if embed is not None:
            self.embeds = [embed]
        if content is not None:
            self.content = content
        self.view = view
        self.edit_count += 1
        return self


class FakeMember:
    def __init__(self, member_id, guild, role_ids=(), administrator=False):
        self.id = member_id
        self.guild = guild
        self.roles = [SimpleNamespace(id=role_id) for role_id in role_ids]
        self.guild_permissions = SimpleNamespace(administrator=administrator)


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.me = object()
        self.text_channels = []
        self.members = {}

    def get_channel(self, channel_id):
        return next((channel for channel in self.text_channels if channel.id == channel_id), None)

    def get_member(self, member_id):
        return self.members.get(member_id)

    async def fetch_member(self, member_id):
        member = self.get_member(member_id)
        if member is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "member")
        return member


class FakeChannel:
    def __init__(
        self,
        guild,
        *,
        channel_id=333333333333333333,
        name="ticket-91",
        mutate_on_edit=True,
    ):
        self.id = channel_id
        self.name = name
        self.category_id = FOLDER_CATEGORY_ID
        self.guild = guild
        self._messages = []
        self.sent_messages = []
        self.edits = []
        self.mutate_on_edit = mutate_on_edit
        guild.text_channels.append(self)

    def permissions_for(self, member):
        return SimpleNamespace(
            manage_channels=True,
            send_messages=True,
            embed_links=True,
        )

    def history(self, *, limit, oldest_first):
        async def iterator():
            messages = self._messages if oldest_first else list(reversed(self._messages))
            for message in messages[:limit]:
                yield message

        return iterator()

    async def fetch_message(self, message_id):
        for message in self._messages:
            if message.id == message_id:
                return message
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "message")

    async def edit(self, *, name, reason=None):
        self.edits.append((name, reason))
        if self.mutate_on_edit:
            self.name = name
            return self
        edited_channel = copy.copy(self)
        edited_channel.name = name
        return edited_channel

    async def send(
        self,
        *,
        embed=None,
        view=None,
        content=None,
        allowed_mentions=None,
    ):
        message = FakeMessage(
            self,
            [embed] if embed is not None else [],
            RENAMER_BOT_ID,
            content=content or "",
            view=view,
        )
        self._messages.append(message)
        self.sent_messages.append(message)
        return message


class FakeResponse:
    def __init__(self):
        self.done = False
        self.messages = []

    def is_done(self):
        return self.done

    async def send_message(self, message, *, ephemeral, view=None):
        self.done = True
        self.messages.append((message, ephemeral, view))

    async def defer(self, *, ephemeral, thinking=False):
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, message, *, ephemeral, view=None):
        self.messages.append((message, ephemeral, view))


class FakeInteraction:
    def __init__(
        self,
        guild,
        channel,
        user,
        *,
        message=None,
    ):
        self.guild = guild
        self.channel = channel
        self.user = user
        self.message = message
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class ManualFallbackUnitTests(unittest.TestCase):
    def test_recognizes_only_folder_creation_signature(self):
        self.assertTrue(is_recognizable_folder_embed([folder_embed([("Uživatel", "x")])]))
        self.assertTrue(
            is_recognizable_folder_embed([folder_embed([("Kanál žádosti", "x")])])
        )
        self.assertFalse(
            is_recognizable_folder_embed(
                [folder_embed([("Jméno a příjmení", "Luis Diaz"), ("Pozice", "Doktor")])]
            )
        )

    def test_validates_and_normalizes_manual_fields(self):
        form = validate_manual_ticket_form(
            "  MUDr. Jan  Novák ",
            "01.02.1990",
            "+420 777 111 222",
            "doktor",
        )
        self.assertEqual(
            form,
            TicketForm(
                "MUDr. Jan Novák",
                "Doktor",
                birth_date="01.02.1990",
                phone_number="+420 777 111 222",
            ),
        )

    def test_normalizes_single_digit_day_and_month(self):
        form = validate_manual_ticket_form(
            "Jan Novák",
            "1.1.2000",
            "777111222",
            "Doktor",
        )
        self.assertEqual(form.birth_date, "01.01.2000")

    def test_rejects_invalid_date_phone_name_and_position(self):
        invalid_cases = [
            ("Jan Novák", "1990-02-01", "777111222", "Doktor"),
            ("Jan Novák", "31.02.1990", "777111222", "Doktor"),
            ("Jan", "01.02.1990", "777111222", "Doktor"),
            ("Jan Novák", "01.02.1990", "12", "Doktor"),
            ("Jan Novák", "01.02.1990", "777111222", "Pilot"),
        ]
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ManualValidationError):
                validate_manual_ticket_form(*values)

    def test_marker_round_trip_and_authority(self):
        form = TicketForm(
            "Jan Novák",
            "Doktor",
            birth_date="01.02.1990",
            phone_number="777111222",
        )
        embed = build_manual_fallback_embed(
            state=ManualFallbackState.DONE,
            reason="Ručně doplněno",
            source=ManualFallbackSource.MANUAL,
            member_id=MEMBER_ID,
            creator_id=222222222222222222,
            ticket_form=form,
        )
        parsed = parse_manual_fallback_embed(embed)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.ticket_form, form)
        self.assertTrue(parsed.is_authoritative)

    def test_persistent_view_has_stable_custom_ids(self):
        async def handler(interaction, action):
            return None

        view = ManualRecoveryView(handler)
        self.assertIsNone(view.timeout)
        self.assertEqual(
            {item.custom_id for item in view.children},
            {"ticket-renamer:manual:open", "ticket-renamer:manual:retry"},
        )


class ManualFallbackFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeMessage.next_id = 1
        self.events = []
        self.settings = Settings(
            discord_bot_token="test-token",
            ticket_tool_bot_ids=frozenset({TICKET_TOOL_BOT_ID}),
            ticket_category_ids=frozenset({FOLDER_CATEGORY_ID}),
            request_channel_prefixes=("zadost-",),
            request_history_limit=100,
            channel_separator="・",
            scan_existing_tickets=False,
            scan_history_limit=25,
            log_level=logging.INFO,
            onboarding_operator_role_ids=frozenset({OPERATOR_ROLE_ID}),
        )
        self.client = TicketRenamerClient(self.settings, reporter=self.events.append)
        self.client._connection.user = SimpleNamespace(id=RENAMER_BOT_ID)
        self.guild = FakeGuild()
        self.member = FakeMember(MEMBER_ID, self.guild)
        self.guild.members[MEMBER_ID] = self.member
        self.operator = FakeMember(
            555555555555555555,
            self.guild,
            role_ids=(OPERATOR_ROLE_ID,),
        )
        self.channel = FakeChannel(self.guild)

    async def asyncTearDown(self):
        await self.client.close()

    def ticket_tool_message(self, fields):
        message = FakeMessage(
            self.channel,
            [folder_embed(fields)],
            TICKET_TOOL_BOT_ID,
        )
        self.channel._messages.append(message)
        return message

    async def test_missing_request_creates_exactly_one_waiting_panel(self):
        source = self.ticket_tool_message([("Uživatel", f"<@{MEMBER_ID}>")])
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            first = await self.client._maybe_rename_from_message(source)
            second = await self.client._maybe_rename_from_message(source)

        panels = [
            message
            for message in self.channel._messages
            if message.embeds and parse_manual_fallback_embed(message.embeds[0]) is not None
        ]
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(panels), 1)
        self.assertEqual(
            parse_manual_fallback_embed(panels[0].embeds[0]).marker.state,
            ManualFallbackState.WAITING,
        )

    async def test_concurrent_missing_request_events_create_one_panel(self):
        source = self.ticket_tool_message([("Uživatel", f"<@{MEMBER_ID}>")])
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            results = await asyncio.gather(
                self.client._maybe_rename_from_message(source),
                self.client._maybe_rename_from_message(source),
            )
        panels = [
            message
            for message in self.channel._messages
            if message.embeds and parse_manual_fallback_embed(message.embeds[0]) is not None
        ]
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(panels), 1)

    async def test_unavailable_named_request_creates_recovery_panel(self):
        source = self.ticket_tool_message(
            [
                ("Uživatel", f"<@{MEMBER_ID}>"),
                ("Kanál žádosti", "#zadost-404"),
            ]
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            changed = await self.client._maybe_rename_from_message(source)
        self.assertTrue(changed)
        self.assertTrue(
            any(
                message.embeds and parse_manual_fallback_embed(message.embeds[0])
                for message in self.channel.sent_messages
            )
        )

    async def test_request_without_application_form_creates_recovery_panel(self):
        request_channel = FakeChannel(
            self.guild,
            channel_id=444444444444444444,
            name="zadost-12",
        )
        request_channel.category_id = 777777777777777777
        request_channel._messages.append(
            FakeMessage(
                request_channel,
                [folder_embed([("Nesouvisející pole", "hodnota")])],
                TICKET_TOOL_BOT_ID,
            )
        )
        source = self.ticket_tool_message(
            [
                ("Uživatel", f"<@{MEMBER_ID}>"),
                ("Kanál žádosti", f"<#{request_channel.id}>"),
            ]
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            changed = await self.client._maybe_rename_from_message(source)
        self.assertTrue(changed)
        panel = self.channel.sent_messages[-1]
        self.assertIn("nebyl nalezen", parse_manual_fallback_embed(panel.embeds[0]).reason)

    async def test_valid_request_with_missing_member_creates_prefilled_panel(self):
        source = self.ticket_tool_message(
            [
                ("Uživatel", "neplatný účet"),
                ("Jméno a příjmení", "Jan Novák"),
                ("Datum narození", "1.1.2000"),
                ("Telefonní číslo", "777111222"),
                ("Pozice", "Doktor"),
            ]
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            changed = await self.client._maybe_rename_from_message(source)
        self.assertTrue(changed)
        record = parse_manual_fallback_embed(self.channel.sent_messages[-1].embeds[0])
        self.assertEqual(record.marker.state, ManualFallbackState.WAITING)
        self.assertIsNone(record.marker.member_id)
        self.assertEqual(record.ticket_form.full_name, "Jan Novák")

    async def test_unsupported_position_creates_prefilled_panel(self):
        source = self.ticket_tool_message(
            [
                ("Uživatel", f"<@{MEMBER_ID}>"),
                ("Jméno a příjmení", "Jan Novák"),
                ("Datum narození", "1.1.2000"),
                ("Telefonní číslo", "777111222"),
                ("Pozice", "Pilot"),
            ]
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            changed = await self.client._maybe_rename_from_message(source)
        self.assertTrue(changed)
        record = parse_manual_fallback_embed(self.channel.sent_messages[-1].embeds[0])
        self.assertEqual(record.ticket_form.position, "Pilot")

    async def test_fiveroster_failure_does_not_create_recovery_panel(self):
        source = self.ticket_tool_message(
            [
                ("Uživatel", f"<@{MEMBER_ID}>"),
                ("Jméno a příjmení", "Jan Novák"),
                ("Datum narození", "1.1.2000"),
                ("Telefonní číslo", "777111222"),
                ("Pozice", "Doktor"),
            ]
        )
        self.client._upsert_onboarding = AsyncMock(return_value=False)
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._maybe_rename_from_message(source)
        self.assertFalse(
            any(
                message.embeds and parse_manual_fallback_embed(message.embeds[0])
                for message in self.channel.sent_messages
            )
        )

    async def test_historical_processing_never_creates_recovery_panel(self):
        source = self.ticket_tool_message([("Uživatel", f"<@{MEMBER_ID}>")])
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            changed = await self.client._maybe_rename_from_message(
                source,
                allow_onboarding=False,
                allow_manual_fallback=False,
            )
        self.assertFalse(changed)
        self.assertEqual(self.channel.sent_messages, [])

    async def test_unrecognized_embed_does_not_scan_whole_channel_for_marker(self):
        source = self.ticket_tool_message(
            [("Jméno a příjmení", "Jan Novák"), ("Pozice", "Doktor")]
        )
        self.client._find_manual_fallback_message = AsyncMock()
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._maybe_rename_from_message(source)
        self.client._find_manual_fallback_message.assert_not_awaited()

    async def test_manual_record_is_authoritative_over_later_ticket_tool_data(self):
        manual_form = TicketForm(
            "Jan Novák",
            "Doktor",
            birth_date="01.02.1990",
            phone_number="777111222",
        )
        manual_message = FakeMessage(
            self.channel,
            [
                build_manual_fallback_embed(
                    state=ManualFallbackState.DONE,
                    reason="Ručně doplněno",
                    source=ManualFallbackSource.MANUAL,
                    member_id=MEMBER_ID,
                    creator_id=self.operator.id,
                    ticket_form=manual_form,
                )
            ],
            RENAMER_BOT_ID,
        )
        self.channel._messages.append(manual_message)
        source = self.ticket_tool_message(
            [
                ("Uživatel", f"<@{MEMBER_ID}>"),
                ("Jméno a příjmení", "Cizí Jméno"),
                ("Datum narození", "03.03.1993"),
                ("Telefonní číslo", "999999999"),
                ("Pozice", "Ochranka"),
            ]
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._maybe_rename_from_message(source)

        self.assertEqual(self.channel.name, "🩺・jan-novak")
        info_embed = next(
            embed
            for message in self.channel.sent_messages
            for embed in message.embeds
            if str(getattr(embed.footer, "text", "")).startswith(
                "Discord Ticket Renamer • Základní informace"
            )
        )
        values = {field.name: field.value for field in info_embed.fields}
        self.assertEqual(values["Jméno a příjmení"], "Jan Novák")
        self.assertEqual(values["Pozice"], "Doktor")

    async def test_submission_processes_and_marks_done(self):
        panel, _ = await self.client._ensure_manual_fallback(
            self.channel,
            reason="Chybí žádost",
            selected_member_id=MEMBER_ID,
        )
        interaction = FakeInteraction(self.guild, self.channel, self.operator)
        submission = ManualSubmission(
            source_message_id=panel.id,
            member_id=MEMBER_ID,
            member=self.member,
            position="Záchranář",
            full_name="Luis Diaz",
            birth_date="10.09.1989",
            phone_number="5207126224",
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_manual_submission(interaction, submission)

        record = parse_manual_fallback_embed(panel.embeds[0])
        self.assertEqual(record.marker.state, ManualFallbackState.DONE)
        self.assertEqual(record.marker.source, ManualFallbackSource.MANUAL)
        self.assertEqual(record.marker.creator_id, self.operator.id)
        self.assertEqual(self.channel.name, "🚑・luis-diaz")
        self.assertIn("zpracována", interaction.followup.messages[-1][0])

    async def test_submission_uses_channel_returned_by_non_mutating_edit(self):
        channel = FakeChannel(
            self.guild,
            channel_id=333333333333333334,
            name="ticket-immutable",
            mutate_on_edit=False,
        )
        panel, _ = await self.client._ensure_manual_fallback(
            channel,
            reason="Chybí žádost",
            selected_member_id=MEMBER_ID,
        )
        interaction = FakeInteraction(self.guild, channel, self.operator)
        submission = ManualSubmission(
            source_message_id=panel.id,
            member_id=MEMBER_ID,
            member=self.member,
            position="Doktor",
            full_name="Jan Novák",
            birth_date="1.1.2000",
            phone_number="777111222",
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_manual_submission(interaction, submission)

        # discord.py vrací z TextChannel.edit nový objekt a původní nemění.
        self.assertEqual(channel.name, "ticket-immutable")
        self.assertEqual(channel.edits[0][0], "🩺・jan-novak")
        self.assertEqual(
            parse_manual_fallback_embed(panel.embeds[0]).marker.state,
            ManualFallbackState.DONE,
        )
        self.assertIn("zpracována", interaction.followup.messages[-1][0])

    async def test_ready_manual_record_recovers_to_done_on_later_event(self):
        form = TicketForm(
            "Jan Novák",
            "Doktor",
            birth_date="01.02.1990",
            phone_number="777111222",
        )
        panel = FakeMessage(
            self.channel,
            [
                build_manual_fallback_embed(
                    state=ManualFallbackState.READY,
                    reason="Předchozí zpracování bylo přerušeno",
                    source=ManualFallbackSource.MANUAL,
                    member_id=MEMBER_ID,
                    creator_id=self.operator.id,
                    ticket_form=form,
                )
            ],
            RENAMER_BOT_ID,
        )
        self.channel._messages.append(panel)
        source = self.ticket_tool_message(
            [
                ("Uživatel", f"<@{MEMBER_ID}>"),
                ("Kanál žádosti", "zadost-999"),
            ]
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            completed = await self.client._maybe_rename_from_message(source)

        self.assertTrue(completed)
        self.assertEqual(
            parse_manual_fallback_embed(panel.embeds[0]).marker.state,
            ManualFallbackState.DONE,
        )
        self.assertEqual(self.channel.name, "🩺・jan-novak")

    async def test_ready_manual_record_honors_historical_no_onboarding(self):
        form = TicketForm(
            "Jan Novák",
            "Doktor",
            birth_date="01.02.1990",
            phone_number="777111222",
        )
        panel = FakeMessage(
            self.channel,
            [
                build_manual_fallback_embed(
                    state=ManualFallbackState.READY,
                    reason="Předchozí zpracování bylo přerušeno",
                    source=ManualFallbackSource.MANUAL,
                    member_id=MEMBER_ID,
                    creator_id=self.operator.id,
                    ticket_form=form,
                )
            ],
            RENAMER_BOT_ID,
        )
        self.channel._messages.append(panel)
        source = self.ticket_tool_message([("Uživatel", f"<@{MEMBER_ID}>")])
        self.client._upsert_onboarding = AsyncMock(return_value=False)

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            completed = await self.client._maybe_rename_from_message(
                source,
                allow_onboarding=False,
                allow_manual_fallback=False,
            )

        self.assertTrue(completed)
        self.client._upsert_onboarding.assert_not_awaited()
        self.assertEqual(
            parse_manual_fallback_embed(panel.embeds[0]).marker.state,
            ManualFallbackState.DONE,
        )

    async def test_concurrent_ready_retries_process_only_once(self):
        form = TicketForm(
            "Jan Novák",
            "Doktor",
            birth_date="01.02.1990",
            phone_number="777111222",
        )
        panel = FakeMessage(
            self.channel,
            [
                build_manual_fallback_embed(
                    state=ManualFallbackState.READY,
                    reason="Předchozí zpracování bylo přerušeno",
                    source=ManualFallbackSource.MANUAL,
                    member_id=MEMBER_ID,
                    creator_id=self.operator.id,
                    ticket_form=form,
                )
            ],
            RENAMER_BOT_ID,
        )
        self.channel._messages.append(panel)
        first = FakeInteraction(
            self.guild,
            self.channel,
            self.operator,
            message=panel,
        )
        second = FakeInteraction(
            self.guild,
            self.channel,
            self.operator,
            message=panel,
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        call_count = 0
        original = self.client._process_manual_record_locked

        async def delayed_processing(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        with (
            patch("ticket_renamer.bot.discord.TextChannel", FakeChannel),
            patch.object(
                self.client,
                "_process_manual_record_locked",
                new=delayed_processing,
            ),
        ):
            first_task = asyncio.create_task(
                self.client._handle_manual_recovery_interaction(
                    first,
                    ManualRecoveryAction.RETRY,
                )
            )
            await entered.wait()
            second_task = asyncio.create_task(
                self.client._handle_manual_recovery_interaction(
                    second,
                    ManualRecoveryAction.RETRY,
                )
            )
            for _ in range(20):
                if second.response.done:
                    break
                await asyncio.sleep(0)
            self.assertTrue(second.response.done)
            release.set()
            await asyncio.gather(first_task, second_task)

        self.assertEqual(call_count, 1)
        self.assertEqual(
            parse_manual_fallback_embed(panel.embeds[0]).marker.state,
            ManualFallbackState.DONE,
        )
        followups = [
            message
            for interaction in (first, second)
            for message, _, _ in interaction.followup.messages
        ]
        self.assertTrue(any("zpracována" in message for message in followups))
        self.assertTrue(any("už byla zpracována" in message for message in followups))

    async def test_request_source_panel_drops_manual_pii_and_tracks_live_request(self):
        self.channel = FakeChannel(
            self.guild,
            channel_id=333333333333333335,
            name="ticket-request-source",
            mutate_on_edit=False,
        )
        stale_form = TicketForm(
            "Staré Jméno",
            "Ochranka",
            birth_date="02.02.1992",
            phone_number="999888777",
        )
        panel, _ = await self.client._ensure_manual_fallback(
            self.channel,
            reason="Zdrojová žádost není dostupná",
            selected_member_id=MEMBER_ID,
            prefill_form=stale_form,
        )
        request_channel = FakeChannel(
            self.guild,
            channel_id=444444444444444444,
            name="zadost-67",
        )
        request_channel.category_id = 777777777777777777
        request_message = FakeMessage(
            request_channel,
            [
                folder_embed(
                    [
                        ("Jméno a příjmení:", "Luis Diaz"),
                        ("Datum narození:", "10.09.1989"),
                        ("Telefonní číslo:", "5207126224"),
                        ("Pozice o kterou si žádáte", "Záchranář"),
                    ]
                )
            ],
            TICKET_TOOL_BOT_ID,
        )
        request_channel._messages.append(request_message)
        source = self.ticket_tool_message(
            [
                ("Uživatel", f"<@{MEMBER_ID}>"),
                ("Kanál žádosti", f"<#{request_channel.id}>"),
            ]
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._maybe_rename_from_message(source)

        record = parse_manual_fallback_embed(panel.embeds[0])
        self.assertEqual(self.channel.name, "ticket-request-source")
        self.assertEqual(record.marker.state, ManualFallbackState.DONE)
        self.assertEqual(record.marker.source, ManualFallbackSource.REQUEST)
        self.assertIsNone(record.marker.member_id)
        self.assertIsNone(record.marker.creator_id)
        self.assertIsNone(record.ticket_form)
        self.assertIn("Zdrojová žádost", panel.embeds[0].title)
        self.assertIn("není jejich autoritativním zdrojem", panel.embeds[0].description)
        self.assertTrue(
            {
                "Uživatel",
                "Jméno a příjmení",
                "Datum narození",
                "Telefonní číslo",
                "Pozice",
                "Doplnil",
            }.isdisjoint(field.name for field in panel.embeds[0].fields)
        )
        request_panel_snapshot = panel.embeds[0].to_dict()

        request_message.embeds = [
            folder_embed(
                [
                    ("Jméno a příjmení:", "Marie Svobodová"),
                    ("Datum narození:", "03.03.1993"),
                    ("Telefonní číslo:", "777666555"),
                    ("Pozice o kterou si žádáte", "Doktor"),
                ]
            )
        ]
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._maybe_rename_from_message(source)

        self.assertEqual(panel.embeds[0].to_dict(), request_panel_snapshot)
        info_embed = next(
            embed
            for message in self.channel.sent_messages
            for embed in message.embeds
            if str(getattr(embed.footer, "text", "")).startswith(
                "Discord Ticket Renamer • Základní informace"
            )
        )
        values = {field.name: field.value for field in info_embed.fields}
        self.assertEqual(values["Jméno a příjmení"], "Marie Svobodová")
        self.assertEqual(values["Telefonní číslo"], "777666555")

    async def test_administrator_without_operator_role_is_denied(self):
        panel, _ = await self.client._ensure_manual_fallback(
            self.channel,
            reason="Chybí žádost",
        )
        administrator = FakeMember(
            666666666666666666,
            self.guild,
            administrator=True,
        )
        interaction = FakeInteraction(
            self.guild,
            self.channel,
            administrator,
            message=panel,
        )
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_manual_recovery_interaction(
                interaction,
                ManualRecoveryAction.OPEN,
            )

        self.assertIn("Nemáte oprávnění", interaction.response.messages[-1][0])

    async def test_slash_command_creates_no_duplicate(self):
        first = FakeInteraction(self.guild, self.channel, self.operator)
        second = FakeInteraction(self.guild, self.channel, self.operator)
        with patch("ticket_renamer.bot.discord.TextChannel", FakeChannel):
            await self.client._handle_manual_request_command(first)
            await self.client._handle_manual_request_command(second)

        panels = [
            message
            for message in self.channel._messages
            if message.embeds and parse_manual_fallback_embed(message.embeds[0]) is not None
        ]
        self.assertEqual(len(panels), 1)
        self.assertIn("už v tomto kanálu existuje", second.followup.messages[-1][0])


if __name__ == "__main__":
    unittest.main()
