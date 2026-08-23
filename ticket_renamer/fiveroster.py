from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
import logging
import re
from typing import Any, Mapping
import unicodedata

import aiohttp


LOGGER = logging.getLogger("ticket_renamer.fiveroster")
FIVEROSTER_BASE_URL = "https://fiveroster.com/api/v1"


class FiveRosterError(RuntimeError):
    """A safe, user-facing FiveRoster failure that never contains the API key."""


class FiveRosterConfigurationError(FiveRosterError):
    pass


class FiveRosterDifferentRankError(FiveRosterError):
    pass


@dataclass(frozen=True, slots=True)
class FiveRosterRank:
    key: str
    uuid: str
    name: str


@dataclass(frozen=True, slots=True)
class EnrollmentOutcome:
    rank: FiveRosterRank
    already_enrolled: bool = False
    callsign: str | None = None


@dataclass(frozen=True, slots=True)
class ShiftStatus:
    on_shift: bool
    shift_id: int | None = None
    started_at: datetime | None = None
    duration_seconds: int = 0
    formatted_duration: str = ""


@dataclass(frozen=True, slots=True)
class EndedShift:
    shift_id: int | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int
    formatted_duration: str


@dataclass(frozen=True, slots=True)
class ShiftPeriodHours:
    hours: float
    formatted: str
    shift_count: int


@dataclass(frozen=True, slots=True)
class ShiftHours:
    weekly: ShiftPeriodHours
    monthly: ShiftPeriodHours
    total: ShiftPeriodHours


@dataclass(frozen=True, slots=True)
class QuotaProgress:
    name: str
    required_formatted: str
    completed_formatted: str
    percentage: float
    is_met: bool
    time_remaining: str = ""


@dataclass(frozen=True, slots=True)
class LoaRequest:
    id: int
    player_id: int
    start_date: date
    end_date: date
    reason: str
    status: str


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _error_message(payload: Any, status: int) -> str:
    # API messages can echo a player's name, LOA reason, request body or another
    # personal value.  Keep the actionable HTTP status, but never persist the
    # remote response text in Discord replies, tray notifications or logs.
    del payload
    return f"FiveRoster vrátil HTTP {status}."


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FiveRosterError("FiveRoster vrátil neplatný čas směny.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value or "").strip()

    if not text:
        raise FiveRosterError(f"FiveRoster vrátil prázdné datum LOA: {value!r}")

    try:
        return date.fromisoformat(text)
    except ValueError:
        pass

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date()
    except ValueError:
        pass

    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass

    for date_format in (
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            pass

    raise FiveRosterError(f"FiveRoster vrátil neplatné datum LOA: {value!r}")


def _integer(value: Any, *, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FiveRosterError("FiveRoster vrátil neočekávanou číselnou hodnotu.") from exc


def _number(value: Any, *, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise FiveRosterError("FiveRoster vrátil neočekávanou číselnou hodnotu.") from exc


def _formatted_hours(hours: float) -> str:
    total_minutes = max(0, round(hours * 60))
    whole_hours, minutes = divmod(total_minutes, 60)
    if minutes:
        return f"{whole_hours}h {minutes}m"
    return f"{whole_hours}h"


def _shift_status_from_mapping(item: Mapping[str, Any]) -> ShiftStatus:
    duration_seconds = _integer(item.get("duration_seconds"))
    return ShiftStatus(
        on_shift=True,
        shift_id=_integer(item.get("id"), default=0) or None,
        started_at=_parse_datetime(item.get("started_at")),
        duration_seconds=duration_seconds,
        formatted_duration=(
            str(item.get("formatted_duration") or "").strip()
            or _formatted_hours(duration_seconds / 3600)
        ),
    )


def _period_hours(payload: Any) -> ShiftPeriodHours:
    if not isinstance(payload, Mapping):
        raise FiveRosterError("FiveRoster nevrátil očekávané statistiky směn.")
    hours = _number(payload.get("hours"))
    return ShiftPeriodHours(
        hours=hours,
        formatted=str(payload.get("formatted") or "").strip() or _formatted_hours(hours),
        shift_count=_integer(payload.get("shift_count")),
    )


class FiveRosterClient:
    def __init__(
        self,
        api_key: str,
        roster_uuid: str,
        rank_names: Mapping[str, str],
        *,
        timeout_seconds: float = 12.0,
        rate_limit_per_minute: int = 45,
    ) -> None:
        self._api_key = api_key
        self.roster_uuid = roster_uuid
        self.rank_names = dict(rank_names)
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._ranks: dict[str, FiveRosterRank] = {}
        self._rate_limit_per_minute = max(1, min(int(rate_limit_per_minute), 49))
        self._request_times: deque[float] = deque()
        self._rate_limit_lock = asyncio.Lock()

    @property
    def ranks(self) -> Mapping[str, FiveRosterRank]:
        return dict(self._ranks)

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"X-API-KEY": self._api_key},
            )
        await self.refresh_ranks()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def rank_for(self, key: str) -> FiveRosterRank:
        try:
            return self._ranks[key]
        except KeyError as exc:
            raise FiveRosterConfigurationError(
                f"Hodnost FiveRoster pro akci {key} není načtená."
            ) from exc

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> tuple[int, Any]:
        session = self._session
        if session is None or session.closed:
            raise FiveRosterError("Připojení k FiveRoster API není připravené.")

        await self._wait_for_rate_slot()
        try:
            async with session.request(
                method,
                f"{FIVEROSTER_BASE_URL}{path}",
                json=json_body,
                params=params,
            ) as response:
                try:
                    payload = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    payload = None
                return response.status, payload
        except TimeoutError as exc:
            raise FiveRosterError("FiveRoster API neodpovědělo v časovém limitu.") from exc
        except aiohttp.ClientError as exc:
            raise FiveRosterError("K FiveRoster API se nepodařilo připojit.") from exc

    async def _wait_for_rate_slot(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            wait_seconds = 0.0
            async with self._rate_limit_lock:
                now = loop.time()
                while self._request_times and self._request_times[0] <= now - 60:
                    self._request_times.popleft()
                if len(self._request_times) < self._rate_limit_per_minute:
                    self._request_times.append(now)
                    return
                wait_seconds = max(0.01, 60 - (now - self._request_times[0]))
            await asyncio.sleep(wait_seconds)

    async def refresh_ranks(self) -> Mapping[str, FiveRosterRank]:
        status, payload = await self._request_json(
            "GET",
            f"/rosters/{self.roster_uuid}/ranks",
        )
        if status < 200 or status >= 300:
            raise FiveRosterConfigurationError(_error_message(payload, status))

        raw_ranks: Any = payload.get("ranks") if isinstance(payload, Mapping) else None
        if raw_ranks is None and isinstance(payload, Mapping):
            raw_ranks = payload.get("data")
        if not isinstance(raw_ranks, list):
            raise FiveRosterConfigurationError(
                "FiveRoster nevrátil očekávaný seznam hodností."
            )

        resolved: dict[str, FiveRosterRank] = {}
        for key, configured_name in self.rank_names.items():
            target = _normalized_name(configured_name)
            matches: list[Mapping[str, Any]] = []
            for item in raw_ranks:
                if not isinstance(item, Mapping) or bool(item.get("is_section")):
                    continue
                if _normalized_name(str(item.get("name") or "")) == target:
                    matches.append(item)

            if len(matches) != 1:
                if not matches:
                    detail = f"Hodnost „{configured_name}“ nebyla nalezena."
                else:
                    detail = f"Hodnost „{configured_name}“ není v rosteru jednoznačná."
                raise FiveRosterConfigurationError(detail)

            item = matches[0]
            rank_uuid = str(
                item.get("rank_uuid")
                or item.get("id")
                or ""
            ).strip()
            rank_name = str(item.get("name") or configured_name).strip()
            if not rank_uuid:
                raise FiveRosterConfigurationError(
                    f"Hodnost „{configured_name}“ nemá rank_uuid."
                )
            resolved[key] = FiveRosterRank(key=key, uuid=rank_uuid, name=rank_name)

        self._ranks = resolved
        LOGGER.info("FiveRoster hodnosti byly ověřeny; načteno: %d.", len(resolved))
        return self.ranks

    async def get_player(self, member_id: int) -> Mapping[str, Any] | None:
        status, payload = await self._request_json(
            "GET",
            f"/rosters/{self.roster_uuid}/players",
        )

        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))

        players: Any = payload.get("data") if isinstance(payload, Mapping) else None

        if not isinstance(players, list):
            raise FiveRosterError(
                "FiveRoster nevratil ocekavany seznam clenu."
            )

        expected_id = str(member_id).strip()

        LOGGER.info(
            "FiveRoster lookup byl zahájen; API vrátilo %d hráčů.",
            len(players),
        )

        for player in players:
            if not isinstance(player, Mapping):
                continue

            player_id = str(
                player.get("id")
                or player.get("member_id")
                or player.get("player_id")
                or ""
            ).strip()

            if player_id != expected_id:
                continue

            LOGGER.info("FiveRoster lookup našel odpovídajícího hráče.")

            return player

        LOGGER.warning(
            "FiveRoster lookup nenašel odpovídajícího hráče mezi %d záznamy.",
            len(players),
        )

        return None

    async def get_player_rank_uuid(self, member_id: int) -> str | None:
        player = await self.get_player(member_id)

        if player is None:
            return None

        rank_uuid = str(player.get("rank_uuid") or "").strip()

        if rank_uuid:
            return rank_uuid

        rank_name = str(
            player.get("rank")
            or player.get("rank_name")
            or ""
        ).strip()

        if rank_name:
            normalized_rank = _normalized_name(rank_name)

            for rank in self._ranks.values():
                if _normalized_name(rank.name) == normalized_rank:
                    return rank.uuid

        LOGGER.warning(
            "FiveRoster hráče našel, ale z odpovědi nelze bezpečně určit cílovou hodnost."
        )

        return None

    async def is_player_enrolled(self, member_id: int) -> bool:
        player = await self.get_player(member_id)
        return player is not None

    async def enroll(self, member_id: int, rank_key: str) -> EnrollmentOutcome:
        rank = self.rank_for(rank_key)
        current_rank_uuid = await self.get_player_rank_uuid(member_id)
        if current_rank_uuid == rank.uuid:
            return EnrollmentOutcome(rank=rank, already_enrolled=True)
        if current_rank_uuid:
            raise FiveRosterDifferentRankError(
                "Uživatel už je ve FiveRosteru na jiné hodnosti; automatická změna byla zastavena."
            )

        try:
            status, payload = await self._request_json(
                "POST",
                f"/rosters/{self.roster_uuid}/ranks/{rank.uuid}/enroll",
                json_body={"member_id": str(member_id)},
            )
        except FiveRosterError as original_error:
            try:
                if await self.get_player_rank_uuid(member_id) == rank.uuid:
                    return EnrollmentOutcome(rank=rank)
            except FiveRosterError:
                pass
            raise original_error

        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))

        callsign: str | None = None
        if isinstance(payload, Mapping):
            raw_callsign = str(payload.get("callsign") or "").strip()
            callsign = raw_callsign or None
        return EnrollmentOutcome(rank=rank, callsign=callsign)

    async def get_shift_status(self, member_id: int) -> ShiftStatus:
        status, payload = await self._request_json(
            "GET",
            "/shifts/status",
            params={"player_id": str(member_id)},
        )
        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))
        if not isinstance(payload, Mapping):
            raise FiveRosterError("FiveRoster nevrátil očekávaný stav směny.")
        if not bool(payload.get("on_shift")):
            return ShiftStatus(on_shift=False)
        shift = payload.get("shift")
        if not isinstance(shift, Mapping):
            raise FiveRosterError("FiveRoster nevrátil podrobnosti aktivní směny.")
        return _shift_status_from_mapping(shift)

    async def start_shift(self, member_id: int) -> ShiftStatus:
        status, payload = await self._request_json(
            "POST",
            "/shifts/start",
            json_body={
                "roster_uuid": self.roster_uuid,
                "player_id": str(member_id),
            },
        )
        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))
        shift = payload.get("shift") if isinstance(payload, Mapping) else None
        if not isinstance(shift, Mapping):
            raise FiveRosterError("FiveRoster nepotvrdil zahájenou směnu.")
        return _shift_status_from_mapping(shift)

    async def end_shift(self, member_id: int) -> EndedShift:
        status, payload = await self._request_json(
            "POST",
            "/shifts/end",
            json_body={"player_id": str(member_id)},
        )
        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))
        shift = payload.get("shift") if isinstance(payload, Mapping) else None
        if not isinstance(shift, Mapping):
            raise FiveRosterError("FiveRoster nepotvrdil ukončenou směnu.")
        duration_seconds = _integer(shift.get("duration_seconds"))
        return EndedShift(
            shift_id=_integer(shift.get("id"), default=0) or None,
            started_at=_parse_datetime(shift.get("started_at")),
            ended_at=_parse_datetime(shift.get("ended_at")),
            duration_seconds=duration_seconds,
            formatted_duration=(
                str(shift.get("formatted_duration") or "").strip()
                or _formatted_hours(duration_seconds / 3600)
            ),
        )

    async def get_shift_hours(self, member_id: int) -> ShiftHours:
        status, payload = await self._request_json(
            "GET",
            "/shifts/hours",
            params={
                "player_id": str(member_id),
                "roster_uuid": self.roster_uuid,
            },
        )
        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))
        if not isinstance(payload, Mapping):
            raise FiveRosterError("FiveRoster nevrátil očekávané statistiky směn.")
        return ShiftHours(
            weekly=_period_hours(payload.get("weekly")),
            monthly=_period_hours(payload.get("monthly")),
            total=_period_hours(payload.get("total")),
        )

    async def get_quota_progress(self, member_id: int) -> tuple[QuotaProgress, ...]:
        status, payload = await self._request_json(
            "GET",
            "/shifts/quota-progress",
            params={
                "player_id": str(member_id),
                "roster_uuid": self.roster_uuid,
            },
        )
        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))
        raw_quotas = payload.get("quotas") if isinstance(payload, Mapping) else None
        if raw_quotas is None and isinstance(payload, Mapping) and not bool(
            payload.get("has_active_quotas")
        ):
            return ()
        if not isinstance(raw_quotas, list):
            raise FiveRosterError("FiveRoster nevrátil očekávaný stav kvót.")
        quotas: list[QuotaProgress] = []
        for item in raw_quotas:
            if not isinstance(item, Mapping):
                raise FiveRosterError("FiveRoster vrátil neplatnou položku kvóty.")
            quotas.append(
                QuotaProgress(
                    name=str(item.get("quota_name") or "Kvóta").strip() or "Kvóta",
                    required_formatted=str(item.get("required_formatted") or "0h").strip(),
                    completed_formatted=str(item.get("completed_formatted") or "0h").strip(),
                    percentage=_number(item.get("percentage")),
                    is_met=bool(item.get("is_met")),
                    time_remaining=str(item.get("time_remaining") or "").strip(),
                )
            )
        return tuple(quotas)

    async def list_loa(self) -> tuple[LoaRequest, ...]:
        status, payload = await self._request_json(
            "GET",
            f"/rosters/{self.roster_uuid}/loa",
        )

        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))

        raw_requests = payload.get("data") if isinstance(payload, Mapping) else None

        if not isinstance(raw_requests, list):
            raise FiveRosterError("FiveRoster nevrátil očekávaný seznam LOA.")

        requests: list[LoaRequest] = []

        for item in raw_requests:
            if not isinstance(item, Mapping):
                LOGGER.warning("FiveRoster LOA: přeskočena neplatná položka.")
                continue

            loa_id = _integer(item.get("id"))
            player_id = _integer(item.get("player_id"))
            raw_start = item.get("start_date")
            raw_end = item.get("end_date")
            loa_status = str(item.get("status") or "").strip().casefold()

            try:
                start_date = _parse_date(raw_start)
                end_date = _parse_date(raw_end)
            except FiveRosterError:
                LOGGER.warning("FiveRoster LOA má neplatné datum a bude přeskočena.")
                continue

            requests.append(
                LoaRequest(
                    id=loa_id,
                    player_id=player_id,
                    start_date=start_date,
                    end_date=end_date,
                    reason=str(item.get("reason") or "").strip(),
                    status=loa_status,
                )
            )

        LOGGER.info(
            "FiveRoster LOA: z API načteno %d položek, úspěšně zpracováno %d.",
            len(raw_requests),
            len(requests),
        )

        return tuple(requests)

    async def create_loa(
        self,
        member_id: int,
        start_date: date,
        end_date: date,
        reason: str,
    ) -> LoaRequest:
        status, payload = await self._request_json(
            "POST",
            f"/rosters/{self.roster_uuid}/loa",
            json_body={
                "player_id": str(member_id),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "reason": reason,
            },
        )
        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping) or _integer(data.get("id")) <= 0:
            raise FiveRosterError("FiveRoster nepotvrdil vytvořenou LOA.")
        return LoaRequest(
            id=_integer(data.get("id")),
            player_id=member_id,
            start_date=start_date,
            end_date=end_date,
            reason=reason,
            status=str(data.get("status") or "pending").strip().casefold(),
        )

    async def cancel_loa(self, loa_id: int) -> None:
        status, payload = await self._request_json(
            "POST",
            f"/rosters/{self.roster_uuid}/loa/{loa_id}/cancel",
        )
        if status < 200 or status >= 300:
            raise FiveRosterError(_error_message(payload, status))
