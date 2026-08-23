import logging
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ticket_renamer.bot import TicketRenamerClient
from ticket_renamer.config import Settings


TICKET_TOOL_BOT_ID = 1325579039888511056
RENAMER_BOT_ID = 1444444444444444444
FOLDER_CATEGORY_ID = 1511618288373858435
REQUEST_CHANNEL_ID = 1512345678901234567


class FakeMessage:
    def __init__(
        self,
        *,
        channel: "FakeTextChannel",
        embeds: list[object],
        author_id: int,
    ) -> None:
        self.author = SimpleNamespace(id=author_id)
        self.channel = channel
        self.guild = channel.guild
        self.embeds = embeds
        self.edit_count = 0

    async def edit(self, *, embed: object, allowed_mentions: object) -> None:
        self.embeds = [embed]
        self.edit_count += 1


class FakeTextChannel:
    def __init__(
        self,
        *,
        channel_id: int,
        name: str,
        category_id: int | None,
        guild: "FakeGuild",
        messages: list[FakeMessage] | None = None,
    ) -> None:
        self.id = channel_id
        self.name = name
        self.category_id = category_id
        self.guild = guild
        self._messages = messages or []
        self.edits: list[tuple[str, str | None]] = []
        self.sent_messages: list[FakeMessage] = []
        self.allow_send_messages = True
        self.allow_embed_links = True

    def history(self, *, limit: int, oldest_first: bool):
        async def iterator():
            messages = self._messages if oldest_first else list(reversed(self._messages))
            for history_message in messages[:limit]:
                yield history_message

        return iterator()

    def permissions_for(self, member: object) -> SimpleNamespace:
        return SimpleNamespace(
            manage_channels=True,
            send_messages=self.allow_send_messages,
            embed_links=self.allow_embed_links,
        )

    async def edit(self, *, name: str, reason: str | None = None) -> None:
        self.edits.append((name, reason))
        self.name = name

    async def send(self, *, embed: object, allowed_mentions: object) -> FakeMessage:
        sent_message = FakeMessage(
            channel=self,
            embeds=[embed],
            author_id=RENAMER_BOT_ID,
        )
        self._messages.append(sent_message)
        self.sent_messages.append(sent_message)
        return sent_message


class FakeGuild:
    def __init__(self) -> None:
        self.id = 999999999999999999
        self.me = object()
        self.text_channels: list[FakeTextChannel] = []

    def get_channel(self, channel_id: int) -> FakeTextChannel | None:
        return next(
            (channel for channel in self.text_channels if channel.id == channel_id),
            None,
        )


def embed(fields: list[tuple[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        fields=[SimpleNamespace(name=name, value=value) for name, value in fields],
        description=None,
    )


def message(
    channel: FakeTextChannel,
    embeds: list[object],
    *,
    author_id: int = TICKET_TOOL_BOT_ID,
) -> FakeMessage:
    return FakeMessage(
        channel=channel,
        embeds=embeds,
        author_id=author_id,
    )


def embed_field_values(employee_embed: object) -> dict[str, str]:
    return {field.name: field.value for field in employee_embed.fields}


class LinkedRequestFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
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
        )
        self.client = TicketRenamerClient(self.settings, reporter=self.events.append)
        self.client._connection.user = SimpleNamespace(id=RENAMER_BOT_ID)
        self.guild = FakeGuild()

    async def asyncTearDown(self) -> None:
        await self.client.close()

    def _linked_scenario(
        self,
        *,
        phone_number: str = "5207126224",
    ) -> tuple[FakeTextChannel, FakeTextChannel, FakeMessage, FakeMessage]:
        request_channel = FakeTextChannel(
            channel_id=REQUEST_CHANNEL_ID,
            name="zadost-67",
            category_id=222222222222222222,
            guild=self.guild,
        )
        request_message = message(
            request_channel,
            [
                embed(
                    [
                        ("Jméno a příjmení:", "Luis Diaz"),
                        ("Datum narození:", "10.09.1989"),
                        ("Telefonní číslo:", phone_number),
                        ("Pozice o kterou si žádáte", "Záchranář"),
                    ]
                )
            ],
        )
        request_channel._messages.append(request_message)
        personal_folder = FakeTextChannel(
            channel_id=333333333333333333,
            name="ticket-91",
            category_id=FOLDER_CATEGORY_ID,
            guild=self.guild,
        )
        self.guild.text_channels.extend([request_channel, personal_folder])
        folder_message = message(
            personal_folder,
            [embed([("Kanál žádosti", f"<#{REQUEST_CHANNEL_ID}>")])],
        )
        return request_channel, personal_folder, request_message, folder_message

    async def test_renames_folder_and_sends_basic_information_embed(self) -> None:
        _, personal_folder, _, folder_message = self._linked_scenario()

        with patch("ticket_renamer.bot.discord.TextChannel", FakeTextChannel):
            changed = await self.client._maybe_rename_from_message(folder_message)

        self.assertTrue(changed)
        self.assertEqual(personal_folder.name, "🚑・luis-diaz")
        self.assertEqual(len(personal_folder.edits), 1)
        self.assertEqual(len(personal_folder.sent_messages), 1)

        employee_embed = personal_folder.sent_messages[0].embeds[0]
        self.assertEqual(
            embed_field_values(employee_embed),
            {
                "Jméno a příjmení": "Luis Diaz",
                "Datum narození": "10.09.1989",
                "Telefonní číslo": "5207126224",
                "Pozice": "Záchranář",
            },
        )
        self.assertIn("Zdroj: #zadost-67", employee_embed.footer.text)
        event_titles = [event.title for event in self.events]
        self.assertIn("Osobní složka přejmenována", event_titles)
        self.assertIn("Základní informace vloženy", event_titles)

    async def test_repeated_processing_does_not_duplicate_embed(self) -> None:
        _, personal_folder, _, folder_message = self._linked_scenario()

        with patch("ticket_renamer.bot.discord.TextChannel", FakeTextChannel):
            first_changed = await self.client._maybe_rename_from_message(folder_message)
            second_changed = await self.client._maybe_rename_from_message(folder_message)

        self.assertTrue(first_changed)
        self.assertFalse(second_changed)
        self.assertEqual(len(personal_folder.sent_messages), 1)
        self.assertEqual(personal_folder.sent_messages[0].edit_count, 0)

    async def test_changed_application_updates_existing_embed(self) -> None:
        _, personal_folder, request_message, folder_message = self._linked_scenario()

        with patch("ticket_renamer.bot.discord.TextChannel", FakeTextChannel):
            await self.client._maybe_rename_from_message(folder_message)
            request_message.embeds = [
                embed(
                    [
                        ("Jméno a příjmení:", "Luis Diaz"),
                        ("Datum narození:", "10.09.1989"),
                        ("Telefonní číslo:", "999888777"),
                        ("Pozice o kterou si žádáte", "Záchranář"),
                    ]
                )
            ]
            changed = await self.client._maybe_rename_from_message(folder_message)

        self.assertTrue(changed)
        self.assertEqual(len(personal_folder.sent_messages), 1)
        info_message = personal_folder.sent_messages[0]
        self.assertEqual(info_message.edit_count, 1)
        self.assertEqual(
            embed_field_values(info_message.embeds[0])["Telefonní číslo"],
            "999888777",
        )
        self.assertEqual(self.events[-1].title, "Základní informace aktualizovány")

    async def test_missing_embed_permission_is_reported_without_sending(self) -> None:
        _, personal_folder, _, folder_message = self._linked_scenario()
        personal_folder.allow_embed_links = False

        with patch("ticket_renamer.bot.discord.TextChannel", FakeTextChannel):
            changed = await self.client._maybe_rename_from_message(folder_message)

        self.assertTrue(changed)
        self.assertEqual(personal_folder.name, "🚑・luis-diaz")
        self.assertEqual(personal_folder.sent_messages, [])
        self.assertEqual(self.events[-1].title, "Chybí oprávnění pro embed")

    async def test_never_renames_or_writes_into_request_channel_itself(self) -> None:
        request_channel = FakeTextChannel(
            channel_id=REQUEST_CHANNEL_ID,
            name="zadost-67",
            category_id=FOLDER_CATEGORY_ID,
            guild=self.guild,
        )
        self.guild.text_channels.append(request_channel)
        request_message = message(
            request_channel,
            [
                embed(
                    [
                        ("Jméno a příjmení:", "Luis Diaz"),
                        ("Pozice o kterou si žádáte", "Záchranář"),
                    ]
                )
            ],
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeTextChannel):
            changed = await self.client._maybe_rename_from_message(request_message)

        self.assertFalse(changed)
        self.assertEqual(request_channel.name, "zadost-67")
        self.assertEqual(request_channel.edits, [])
        self.assertEqual(request_channel.sent_messages, [])

    async def test_rejects_selected_channel_without_request_prefix(self) -> None:
        unrelated_channel = FakeTextChannel(
            channel_id=REQUEST_CHANNEL_ID,
            name="obecny-chat",
            category_id=222222222222222222,
            guild=self.guild,
        )
        personal_folder = FakeTextChannel(
            channel_id=333333333333333333,
            name="ticket-91",
            category_id=FOLDER_CATEGORY_ID,
            guild=self.guild,
        )
        self.guild.text_channels.extend([unrelated_channel, personal_folder])
        folder_message = message(
            personal_folder,
            [embed([("Kanál žádosti", f"<#{REQUEST_CHANNEL_ID}>")])],
        )

        with patch("ticket_renamer.bot.discord.TextChannel", FakeTextChannel):
            changed = await self.client._maybe_rename_from_message(folder_message)

        self.assertFalse(changed)
        self.assertEqual(personal_folder.name, "ticket-91")
        self.assertEqual(personal_folder.sent_messages, [])
        self.assertEqual(self.events[-1].title, "Neplatný kanál žádosti")


if __name__ == "__main__":
    unittest.main()
