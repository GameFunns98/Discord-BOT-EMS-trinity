from datetime import date
import unittest

from ticket_renamer.fiveroster import (
    FiveRosterClient,
    FiveRosterConfigurationError,
    FiveRosterDifferentRankError,
    FiveRosterError,
)


RANK_NAMES = {
    "paramedic": "Paramedic",
    "academy": "Akademie",
    "doctor": "Doktor",
    "doctor_training": "Doktor v zácviku",
    "security": "Security",
}


def ranks_payload():
    return {
        "ranks": [
            {"rank_uuid": "p", "name": "Paramedic", "is_section": False},
            {"rank_uuid": "a", "name": "Akademie", "is_section": False},
            {"rank_uuid": "d", "name": "Doktor", "is_section": False},
            {"rank_uuid": "dt", "name": "Doktor v zácviku", "is_section": False},
            {"rank_uuid": "s", "name": "Security", "is_section": False},
        ]
    }


class StubFiveRosterClient(FiveRosterClient):
    def __init__(self, responses):
        super().__init__("secret", "roster", RANK_NAMES)
        self.responses = list(responses)
        self.requests = []

    async def _request_json(self, method, path, *, json_body=None, params=None):
        self.requests.append((method, path, json_body, params))
        return self.responses.pop(0)


class FiveRosterClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_all_unique_non_section_ranks(self) -> None:
        client = StubFiveRosterClient([(200, ranks_payload())])

        ranks = await client.refresh_ranks()

        self.assertEqual(ranks["doctor_training"].uuid, "dt")
        self.assertEqual(len(ranks), 5)

    async def test_rejects_missing_rank(self) -> None:
        payload = ranks_payload()
        payload["ranks"] = payload["ranks"][:-1]
        client = StubFiveRosterClient([(200, payload)])

        with self.assertRaisesRegex(FiveRosterConfigurationError, "Security"):
            await client.refresh_ranks()

    async def test_enroll_posts_member_id_after_roster_check(self) -> None:
        client = StubFiveRosterClient(
            [
                (200, ranks_payload()),
                (200, {"data": []}),
                (200, {"callsign": "A-01"}),
            ]
        )
        await client.refresh_ranks()

        result = await client.enroll(123456789012345678, "paramedic")

        self.assertFalse(result.already_enrolled)
        self.assertEqual(result.callsign, "A-01")
        self.assertEqual(
            client.requests[-1],
            (
                "POST",
                "/rosters/roster/ranks/p/enroll",
                {"member_id": "123456789012345678"},
                None,
            ),
        )

    async def test_same_rank_is_idempotent_and_different_rank_stops(self) -> None:
        same = StubFiveRosterClient(
            [
                (200, ranks_payload()),
                (200, {"data": [{"id": "123", "rank_uuid": "p"}]}),
            ]
        )
        await same.refresh_ranks()
        outcome = await same.enroll(123, "paramedic")
        self.assertTrue(outcome.already_enrolled)
        self.assertEqual([request[0] for request in same.requests].count("POST"), 0)

        different = StubFiveRosterClient(
            [
                (200, ranks_payload()),
                (200, {"data": [{"id": "123", "rank_uuid": "a"}]}),
            ]
        )
        await different.refresh_ranks()
        with self.assertRaises(FiveRosterDifferentRankError):
            await different.enroll(123, "paramedic")

    async def test_reads_shift_status_hours_quota_and_loa(self) -> None:
        client = StubFiveRosterClient(
            [
                (
                    200,
                    {
                        "on_shift": True,
                        "shift": {
                            "id": 42,
                            "started_at": "2026-08-15T08:00:00Z",
                            "duration_seconds": 3600,
                            "formatted_duration": "1h 0m",
                        },
                    },
                ),
                (
                    200,
                    {
                        "weekly": {"hours": 5, "formatted": "5h", "shift_count": 3},
                        "monthly": {"hours": 20, "formatted": "20h", "shift_count": 12},
                        "total": {"hours": 100, "formatted": "100h", "shift_count": 50},
                    },
                ),
                (
                    200,
                    {
                        "has_active_quotas": True,
                        "quotas": [
                            {
                                "quota_name": "Týdenní minimum",
                                "required_formatted": "4h",
                                "completed_formatted": "3h",
                                "percentage": 75,
                                "is_met": False,
                                "time_remaining": "2 dny",
                            }
                        ],
                    },
                ),
                (
                    200,
                    {
                        "data": [
                            {
                                "id": 7,
                                "player_id": "123",
                                "start_date": "2026-08-20",
                                "end_date": "2026-08-22",
                                "reason": "Dovolená",
                                "status": "pending",
                            }
                        ]
                    },
                ),
            ]
        )

        status = await client.get_shift_status(123)
        hours = await client.get_shift_hours(123)
        quotas = await client.get_quota_progress(123)
        loas = await client.list_loa()

        self.assertTrue(status.on_shift)
        self.assertEqual(status.shift_id, 42)
        self.assertEqual(hours.weekly.shift_count, 3)
        self.assertEqual(quotas[0].percentage, 75)
        self.assertEqual(loas[0].start_date, date(2026, 8, 20))
        self.assertEqual(
            client.requests[0],
            ("GET", "/shifts/status", None, {"player_id": "123"}),
        )

    async def test_shift_and_loa_writes_use_expected_payloads(self) -> None:
        client = StubFiveRosterClient(
            [
                (
                    201,
                    {
                        "shift": {
                            "id": 1,
                            "started_at": "2026-08-15T08:00:00Z",
                        }
                    },
                ),
                (
                    200,
                    {
                        "shift": {
                            "id": 1,
                            "started_at": "2026-08-15T08:00:00Z",
                            "ended_at": "2026-08-15T09:00:00Z",
                            "duration_seconds": 3600,
                            "formatted_duration": "1h 0m",
                        }
                    },
                ),
                (201, {"data": {"id": 8, "status": "pending"}}),
                (200, {"success": True}),
            ]
        )

        await client.start_shift(123)
        ended = await client.end_shift(123)
        loa = await client.create_loa(
            123,
            date(2026, 8, 20),
            date(2026, 8, 22),
            "Dovolená",
        )
        await client.cancel_loa(loa.id)

        self.assertEqual(ended.formatted_duration, "1h 0m")
        self.assertEqual(
            client.requests[0],
            (
                "POST",
                "/shifts/start",
                {"roster_uuid": "roster", "player_id": "123"},
                None,
            ),
        )
        self.assertEqual(client.requests[-1][1], "/rosters/roster/loa/8/cancel")

    async def test_http_429_is_reported_without_exposing_key(self) -> None:
        client = StubFiveRosterClient(
            [(429, {"error": {"message": "Too many requests"}})]
        )

        with self.assertRaisesRegex(FiveRosterError, "HTTP 429") as context:
            await client.get_shift_status(123)

        self.assertNotIn("secret", str(context.exception))
