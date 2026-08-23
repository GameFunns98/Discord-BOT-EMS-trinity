from types import SimpleNamespace
import unittest

from ticket_renamer.parser import (
    RequestChannelReference,
    TicketForm,
    abbreviate_person_name,
    build_channel_name,
    emoji_for_position,
    parse_request_channel_reference,
    parse_selected_member_id,
    parse_ticket_form,
    slugify_person_name,
)


class TicketFormParserTests(unittest.TestCase):
    def test_reads_ticket_tool_embed_fields(self) -> None:
        embed = SimpleNamespace(
            fields=[
                SimpleNamespace(name="Jméno a Příjmení:", value="Jackob White"),
                SimpleNamespace(name="Datum narození:", value="1.1.2000"),
                SimpleNamespace(name="Telefonní číslo:", value="4806193882"),
                SimpleNamespace(name="Pozice:", value="Záchranář"),
            ],
            description=None,
        )

        parsed = parse_ticket_form([embed])

        self.assertEqual(
            parsed,
            TicketForm(
                "Jackob White",
                "Záchranář",
                birth_date="1.1.2000",
                phone_number="4806193882",
            ),
        )

    def test_reads_markdown_description_with_czech_diacritics(self) -> None:
        embed = SimpleNamespace(
            fields=[],
            description=(
                "**Jméno a Příjmení:**\n"
                "Jackob White\n"
                "**Datum narození:**\n"
                "1.1.2000\n"
                "**Telefonní číslo:**\n"
                "4806193882\n"
                "**Pozice:**\n"
                "Záchranář"
            ),
        )

        parsed = parse_ticket_form([embed])

        self.assertEqual(
            parsed,
            TicketForm(
                "Jackob White",
                "Záchranář",
                birth_date="1.1.2000",
                phone_number="4806193882",
            ),
        )

    def test_reads_values_across_multiple_embeds(self) -> None:
        name_embed = SimpleNamespace(
            fields=[SimpleNamespace(name="**Jméno a Příjmení:**", value="Jan Novák")],
            description=None,
        )
        position_embed = SimpleNamespace(
            fields=[SimpleNamespace(name="**Pozice:**", value="Doktor")],
            description=None,
        )

        parsed = parse_ticket_form([name_embed, position_embed])

        self.assertEqual(parsed, TicketForm("Jan Novák", "Doktor"))

    def test_reads_new_application_position_label(self) -> None:
        embed = SimpleNamespace(
            fields=[
                SimpleNamespace(name="Jméno a příjmení:", value="Luis Diaz"),
                SimpleNamespace(
                    name="Pozice o kterou si žádáte",
                    value="Záchranář",
                ),
            ],
            description=None,
        )

        parsed = parse_ticket_form([embed])

        self.assertEqual(parsed, TicketForm("Luis Diaz", "Záchranář"))

    def test_reads_basic_information_from_current_application_embed(self) -> None:
        embed = SimpleNamespace(
            fields=[
                SimpleNamespace(name="Jméno a příjmení:", value="Michael Jesus"),
                SimpleNamespace(name="Datum narození:", value="3.3.1980"),
                SimpleNamespace(name="Telefonní číslo:", value="5201307837"),
                SimpleNamespace(
                    name="Pozice o kterou si žádáte",
                    value="Záchranář",
                ),
            ],
            description=None,
        )

        parsed = parse_ticket_form([embed])

        self.assertEqual(
            parsed,
            TicketForm(
                "Michael Jesus",
                "Záchranář",
                birth_date="3.3.1980",
                phone_number="5201307837",
            ),
        )

    def test_reads_selected_request_channel_mention(self) -> None:
        embed = SimpleNamespace(
            fields=[
                SimpleNamespace(name="Uživatel", value="<@123456789012345678>"),
                SimpleNamespace(
                    name="Kanál žádosti",
                    value="<#1512345678901234567>",
                ),
            ],
            description=None,
        )

        parsed = parse_request_channel_reference([embed])

        self.assertEqual(
            parsed,
            RequestChannelReference(channel_id=1512345678901234567),
        )
        self.assertEqual(
            parse_selected_member_id([embed]),
            123456789012345678,
        )

    def test_reads_nickname_member_mention_and_raw_id(self) -> None:
        mention_embed = SimpleNamespace(
            fields=[SimpleNamespace(name="Uživatel", value="<@!123456789012345678>")],
            description=None,
        )
        raw_embed = SimpleNamespace(
            fields=[SimpleNamespace(name="Zaměstnanec", value="123456789012345679")],
            description=None,
        )

        self.assertEqual(parse_selected_member_id([mention_embed]), 123456789012345678)
        self.assertEqual(parse_selected_member_id([raw_embed]), 123456789012345679)

    def test_rejects_invalid_selected_member(self) -> None:
        invalid_embed = SimpleNamespace(
            fields=[SimpleNamespace(name="Uživatel", value="někdo")],
            description=None,
        )
        self.assertIsNone(parse_selected_member_id([invalid_embed]))

    def test_reads_request_channel_name_from_description(self) -> None:
        embed = SimpleNamespace(
            fields=[],
            description="**Kanál žádosti:**\n#zadost-67",
        )

        parsed = parse_request_channel_reference([embed])

        self.assertEqual(
            parsed,
            RequestChannelReference(channel_name="zadost-67"),
        )

    def test_maps_ticket_display_name_to_request_channel_prefix(self) -> None:
        embed = SimpleNamespace(
            fields=[
                SimpleNamespace(
                    name="Kanál žádosti",
                    value="Ticket-67 six seven",
                )
            ],
            description=None,
        )

        parsed = parse_request_channel_reference([embed], ("zadost-",))

        self.assertEqual(
            parsed,
            RequestChannelReference(channel_name="zadost-67"),
        )

    def test_creates_expected_channel_names(self) -> None:
        cases = [
            ("Jackob White", "Záchranář", "🚑・jackob-white"),
            ("Marie Nováková", "doktor", "🩺・marie-novakova"),
            ("Viktor Cobbet", "Ochranka", "🛡️・viktor-cobbet"),
        ]

        for full_name, position, expected in cases:
            with self.subTest(position=position):
                self.assertEqual(
                    build_channel_name(TicketForm(full_name, position)),
                    expected,
                )

    def test_normalizes_person_name(self) -> None:
        self.assertEqual(slugify_person_name("  MUDr. Žaneta Černá  "), "mudr-zaneta-cerna")

    def test_builds_fiveroster_name_suggestion(self) -> None:
        self.assertEqual(abbreviate_person_name("Fero Lakatoš"), "F. Lakatoš")
        self.assertEqual(abbreviate_person_name("MUDr. Fero Lakatoš"), "F. Lakatoš")
        self.assertEqual(abbreviate_person_name("Jan Pavel Novák"), "J. Novák")
        self.assertEqual(abbreviate_person_name("Cher"), "Cher")

    def test_rejects_unknown_position(self) -> None:
        form = TicketForm("Jackob White", "Pilot")
        self.assertIsNone(emoji_for_position(form.position))
        self.assertIsNone(build_channel_name(form))

    def test_channel_name_never_exceeds_discord_limit(self) -> None:
        form = TicketForm("A" * 150, "Ochranka")
        channel_name = build_channel_name(form)

        self.assertIsNotNone(channel_name)
        self.assertLessEqual(len(channel_name or ""), 100)


if __name__ == "__main__":
    unittest.main()
