from datetime import date, datetime, timezone
import unittest

from ticket_renamer.fiveroster import (
    LoaRequest,
    QuotaProgress,
    ShiftHours,
    ShiftPeriodHours,
    ShiftStatus,
)
from ticket_renamer.shift_panel import (
    ShiftPanelState,
    ShiftPanelSnapshot,
    ShiftPanelView,
    LoaCancelView,
    build_shift_panel_embed,
    loa_ranges_overlap,
    parse_loa_date,
    parse_shift_panel_marker,
)


MEMBER_ID = 123456789012345678
CREATOR_ID = 555555555555555555


async def unused_handler(interaction, action):
    return None


def snapshot(*, status=None, loas=(), quotas=()):
    period = ShiftPeriodHours(hours=5, formatted="5h", shift_count=3)
    return ShiftPanelSnapshot(
        status=status or ShiftStatus(on_shift=False),
        hours=ShiftHours(weekly=period, monthly=period, total=period),
        quotas=tuple(quotas),
        loas=tuple(loas),
        fetched_at=datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
    )


class ShiftPanelTests(unittest.TestCase):
    def test_date_parser_and_overlap(self) -> None:
        self.assertEqual(parse_loa_date("20.08.2026"), date(2026, 8, 20))
        self.assertEqual(parse_loa_date("2026-08-20"), date(2026, 8, 20))
        with self.assertRaises(ValueError):
            parse_loa_date("zítra")

        existing = LoaRequest(
            1,
            MEMBER_ID,
            date(2026, 8, 20),
            date(2026, 8, 25),
            "Dovolená",
            "pending",
        )
        self.assertIs(
            loa_ranges_overlap(date(2026, 8, 25), date(2026, 8, 26), (existing,)),
            existing,
        )
        self.assertIsNone(
            loa_ranges_overlap(date(2026, 8, 26), date(2026, 8, 27), (existing,))
        )

    def test_off_duty_embed_and_marker(self) -> None:
        quota = QuotaProgress("Týdenní minimum", "4h", "5h", 125, True)
        current = snapshot(quotas=(quota,))

        embed = build_shift_panel_embed(
            member_id=MEMBER_ID,
            snapshot=current,
            creator_id=CREATOR_ID,
        )
        marker = parse_shift_panel_marker(embed)

        self.assertEqual(current.state, ShiftPanelState.OFF_DUTY)
        self.assertEqual(marker.member_id, MEMBER_ID)
        self.assertEqual(marker.creator_id, CREATOR_ID)
        self.assertIn("mimo službu", embed.title)
        self.assertIn("125 %", next(field.value for field in embed.fields if field.name == "Kvóta"))

    def test_on_duty_and_loa_button_states(self) -> None:
        on_duty = snapshot(
            status=ShiftStatus(
                on_shift=True,
                shift_id=1,
                started_at=datetime(2026, 8, 15, 9, 0, tzinfo=timezone.utc),
                formatted_duration="1h",
            )
        )
        on_view = ShiftPanelView(unused_handler, on_duty)
        buttons = {item.label: item for item in on_view.children}
        self.assertTrue(buttons["Vstoupit do služby"].disabled)
        self.assertFalse(buttons["Ukončit službu"].disabled)
        self.assertTrue(buttons["Požádat o LOA"].disabled)

        active_loa = LoaRequest(
            2,
            MEMBER_ID,
            date(2026, 8, 15),
            date(2026, 8, 20),
            "Nemoc",
            "approved",
        )
        loa_snapshot = snapshot(loas=(active_loa,))
        loa_view = ShiftPanelView(unused_handler, loa_snapshot)
        buttons = {item.label: item for item in loa_view.children}
        self.assertEqual(loa_snapshot.state, ShiftPanelState.LOA)
        self.assertTrue(buttons["Vstoupit do služby"].disabled)
        self.assertFalse(buttons["Zrušit LOA"].disabled)

    def test_multiple_cancelable_loas_require_explicit_selection(self) -> None:
        requests = (
            LoaRequest(
                1,
                MEMBER_ID,
                date(2026, 8, 20),
                date(2026, 8, 21),
                "První",
                "pending",
            ),
            LoaRequest(
                2,
                MEMBER_ID,
                date(2026, 8, 25),
                date(2026, 8, 26),
                "Druhá",
                "approved",
            ),
        )

        async def cancel_handler(interaction, member_id, loa_id):
            return None

        view = LoaCancelView(
            requests=requests,
            member_id=MEMBER_ID,
            owner_id=MEMBER_ID,
            handler=cancel_handler,
        )
        self.assertIsNone(view.selected_loa_id)


if __name__ == "__main__":
    unittest.main()
