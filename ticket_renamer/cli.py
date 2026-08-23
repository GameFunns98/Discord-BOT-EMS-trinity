"""Command-line management interface for Discord Ticket Renamer."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import TextIO

from . import __version__
from .health import HEALTH_DISABLE_ENV
from .logging_config import configure_logging, secret_values_from_environment


BOT_SERVICE = "discord-ticket-renamer.service"
UPDATE_SERVICE = "discord-ticket-renamer-update.service"
UPDATE_TIMER = "discord-ticket-renamer-update.timer"
LEGACY_SOURCE_MARKER = "legacy-source"
HEALTH_MAX_AGE_SECONDS = 180
HEALTH_MAX_FUTURE_SKEW_SECONDS = 5

CommandExecutor = Callable[[Sequence[str]], int]
BotRunner = Callable[[], object]


@dataclass(frozen=True, slots=True)
class ManagedPaths:
    config_dir: Path
    env_file: Path
    data_dir: Path
    releases_dir: Path
    current: Path
    updater: Path
    health_file: Path

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
    ) -> "ManagedPaths":
        source = os.environ if environment is None else environment
        resolved_home = home or Path.home()
        # The installed user units use these literal per-user paths. Ambient
        # XDG_CONFIG_HOME/XDG_DATA_HOME values must not make doctor inspect a
        # different installation. DTR_* remains the explicit override surface.
        config_home = resolved_home / ".config"
        data_home = resolved_home / ".local/share"
        runtime_value = source.get("XDG_RUNTIME_DIR")
        runtime_home = (
            Path(runtime_value)
            if runtime_value
            else Path(tempfile.gettempdir())
            / f"discord-ticket-renamer-{os.getuid() if hasattr(os, 'getuid') else 'user'}"
        )
        config_dir = Path(
            source.get("DTR_CONFIG_DIR") or config_home / "discord-ticket-renamer"
        )
        data_dir = Path(
            source.get("DTR_DATA_DIR") or data_home / "discord-ticket-renamer"
        )
        lib_dir = Path(
            source.get("DTR_LIB_DIR")
            or resolved_home / ".local/lib/discord-ticket-renamer"
        )
        runtime_dir = Path(
            source.get("DTR_RUNTIME_DIR")
            or runtime_home / "discord-ticket-renamer"
        )
        return cls(
            config_dir=config_dir,
            env_file=config_dir / ".env",
            data_dir=data_dir,
            releases_dir=data_dir / "releases",
            current=data_dir / "current",
            updater=lib_dir / "updater.py",
            health_file=runtime_dir / "health.json",
        )


@dataclass(frozen=True, slots=True)
class Diagnostic:
    level: str
    message: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket-renamer",
        description="Správa Discord Ticket Renameru",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Spustí Discord bota v popředí")
    commands.add_parser("version", help="Zobrazí nainstalovanou verzi")
    commands.add_parser("self-test", help="Provede bezpečný offline self-test")
    commands.add_parser("doctor", help="Zkontroluje spravovanou Linux instalaci")
    commands.add_parser("status", help="Zobrazí stav služby")

    logs = commands.add_parser("logs", help="Sleduje bezpečný systemd journal")
    logs.add_argument("--lines", type=_positive_integer, default=100)
    logs.add_argument(
        "--no-follow",
        action="store_true",
        help="Vypíše pouze existující řádky a skončí",
    )

    commands.add_parser("restart", help="Restartuje službu bota")
    commands.add_parser("update-now", help="Okamžitě spustí kontrolu aktualizace")
    return parser


def _positive_integer(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 10000:
        raise argparse.ArgumentTypeError("hodnota musí být v rozsahu 1–10000")
    return number


def _default_executor(command: Sequence[str]) -> int:
    try:
        return subprocess.run(list(command), check=False).returncode
    except FileNotFoundError:
        print(f"Příkaz {command[0]} není dostupný.", file=sys.stderr)
        return 127


def _default_bot_runner() -> object:
    # Importing the Discord client is intentionally delayed.  Commands such as
    # doctor/version therefore work even when optional runtime dependencies are
    # not installed yet.
    from .bot import run

    return run()


def collect_diagnostics(
    paths: ManagedPaths,
    *,
    now: datetime | None = None,
    service_executor: CommandExecutor | None = None,
) -> list[Diagnostic]:
    results: list[Diagnostic] = []

    if not paths.env_file.is_file():
        results.append(Diagnostic("ERROR", f"Konfigurace chybí: {paths.env_file}"))
    else:
        results.append(Diagnostic("OK", f"Konfigurace existuje: {paths.env_file}"))
        if os.name == "posix":
            try:
                mode = stat.S_IMODE(paths.env_file.stat().st_mode)
            except OSError as exc:
                results.append(Diagnostic("ERROR", f"Oprávnění .env nelze přečíst: {exc}"))
            else:
                if mode & 0o077:
                    results.append(
                        Diagnostic(
                            "ERROR",
                            ".env je přístupný dalším uživatelům "
                            f"(režim {mode:04o}, požadováno 0600).",
                        )
                    )
                else:
                    results.append(Diagnostic("OK", "Oprávnění .env jsou bezpečná."))

    results.extend(_legacy_debug_log_diagnostics(paths))

    if not paths.releases_dir.is_dir():
        results.append(Diagnostic("ERROR", f"Adresář verzí chybí: {paths.releases_dir}"))
    else:
        results.append(Diagnostic("OK", f"Adresář verzí existuje: {paths.releases_dir}"))

    active_release: Path | None = None
    if not paths.current.exists():
        results.append(Diagnostic("ERROR", f"Aktivní verze chybí: {paths.current}"))
    elif not paths.current.is_dir():
        results.append(Diagnostic("ERROR", f"Aktivní verze není adresář: {paths.current}"))
    else:
        try:
            active_release = paths.current.resolve(strict=True)
        except OSError as exc:
            results.append(Diagnostic("ERROR", f"Aktivní verzi nelze otevřít: {exc}"))
        else:
            results.append(Diagnostic("OK", f"Aktivní verze: {active_release.name}"))

    if active_release is not None:
        python_candidates = (
            active_release / ".venv/bin/python",
            active_release / ".venv/Scripts/python.exe",
        )
        if any(candidate.is_file() for candidate in python_candidates):
            results.append(Diagnostic("OK", "Virtuální prostředí aktivní verze existuje."))
        else:
            results.append(Diagnostic("ERROR", "Aktivní verzi chybí .venv Python."))

    if paths.updater.is_file():
        results.append(Diagnostic("OK", f"Aktualizátor existuje: {paths.updater}"))
    else:
        results.append(Diagnostic("ERROR", f"Aktualizátor chybí: {paths.updater}"))

    expected_version = None
    if active_release is not None and re.fullmatch(r"\d+\.\d+\.\d+", active_release.name):
        expected_version = active_release.name
    results.extend(
        _health_diagnostics(
            paths.health_file,
            now=now,
            expected_version=expected_version,
        )
    )
    if service_executor is not None:
        results.extend(_systemd_diagnostics(service_executor))
    return results


def _legacy_debug_log_diagnostics(paths: ManagedPaths) -> list[Diagnostic]:
    marker = paths.config_dir / LEGACY_SOURCE_MARKER
    if not marker.exists():
        return []
    if not marker.is_file():
        return [Diagnostic("WARNING", "Marker původní instalace není běžný soubor.")]

    try:
        with marker.open("r", encoding="utf-8-sig") as handle:
            raw_source = handle.read(4097)
    except (OSError, UnicodeError) as exc:
        return [Diagnostic("WARNING", f"Marker původní instalace nelze přečíst: {exc}")]

    if len(raw_source) > 4096 or not raw_source.strip():
        return [Diagnostic("WARNING", "Marker původní instalace nemá platný obsah.")]

    source = Path(raw_source.strip()).expanduser()
    if not source.is_absolute() or not source.is_dir():
        return [Diagnostic("WARNING", "Původní instalační adresář z markeru není dostupný.")]

    try:
        old_logs = tuple(
            path for path in source.glob("TicketRenamer.log*") if path.is_file()
        )
    except OSError as exc:
        return [Diagnostic("WARNING", f"Staré logy nelze zkontrolovat: {exc}")]

    if old_logs:
        return [
            Diagnostic(
                "WARNING",
                f"Původní instalace obsahuje {len(old_logs)} staré logovací soubory; "
                "mohou obsahovat citlivá DEBUG data. Nebyly automaticky smazány.",
            )
        ]
    return [Diagnostic("OK", "Původní instalace neobsahuje staré TicketRenamer.log soubory.")]


def _systemd_diagnostics(executor: CommandExecutor) -> list[Diagnostic]:
    checks = (
        (
            ("systemctl", "--user", "is-active", "--quiet", BOT_SERVICE),
            "Služba bota je aktivní.",
            "Služba bota není aktivní.",
        ),
        (
            ("systemctl", "--user", "is-enabled", "--quiet", BOT_SERVICE),
            "Služba bota je povolená pro automatické spuštění.",
            "Služba bota není povolená pro automatické spuštění.",
        ),
        (
            ("systemctl", "--user", "is-active", "--quiet", UPDATE_TIMER),
            "Časovač automatických aktualizací je aktivní.",
            "Časovač automatických aktualizací není aktivní.",
        ),
        (
            ("systemctl", "--user", "is-enabled", "--quiet", UPDATE_TIMER),
            "Časovač automatických aktualizací je povolený.",
            "Časovač automatických aktualizací není povolený.",
        ),
    )
    results: list[Diagnostic] = []
    for command, success, failure in checks:
        try:
            return_code = executor(command)
        except OSError as exc:
            results.append(Diagnostic("ERROR", f"Stav systemd nelze ověřit: {exc}"))
            break
        results.append(
            Diagnostic(
                "OK" if return_code == 0 else "ERROR",
                success if return_code == 0 else failure,
            )
        )
    return results


def _health_diagnostics(
    health_file: Path,
    *,
    now: datetime | None = None,
    expected_version: str | None = None,
) -> list[Diagnostic]:
    if not health_file.is_file():
        return [Diagnostic("ERROR", f"Health marker chybí: {health_file}")]
    try:
        payload = json.loads(health_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [Diagnostic("ERROR", f"Health marker není čitelný: {exc}")]

    if not isinstance(payload, dict):
        return [Diagnostic("ERROR", "Health marker nemá platný formát.")]
    if payload.get("ready") is not True:
        return [Diagnostic("ERROR", "Bot podle health markeru není připravený.")]
    if payload.get("state") != "ready":
        return [Diagnostic("ERROR", "Health marker nemá stav ready.")]

    version = payload.get("version")
    if not isinstance(version, str) or not version.strip():
        return [Diagnostic("ERROR", "Health marker neobsahuje verzi.")]
    if expected_version is not None and version != expected_version:
        return [
            Diagnostic(
                "ERROR",
                f"Health marker patří verzi {version}, aktivní je {expected_version}.",
            )
        ]

    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return [Diagnostic("ERROR", "Health marker neobsahuje platné PID procesu.")]

    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp.strip():
        return [Diagnostic("ERROR", "Health marker neobsahuje timestamp.")]
    try:
        normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
        updated = datetime.fromisoformat(normalized)
        if updated.tzinfo is None:
            raise ValueError("timestamp has no timezone")
    except (OverflowError, TypeError, ValueError):
        return [Diagnostic("ERROR", f"Health marker verze {version} má neplatný timestamp.")]

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    age_seconds = (
        reference.astimezone(timezone.utc) - updated.astimezone(timezone.utc)
    ).total_seconds()
    if age_seconds < -HEALTH_MAX_FUTURE_SKEW_SECONDS:
        return [Diagnostic("ERROR", f"Health marker verze {version} má čas v budoucnosti.")]
    if age_seconds > HEALTH_MAX_AGE_SECONDS:
        return [
            Diagnostic(
                "ERROR",
                f"Health marker verze {version} je zastaralý ({int(age_seconds)} s).",
            )
        ]

    return [Diagnostic("OK", f"Bot verze {version} je podle health markeru připravený.")]


def run_doctor(
    paths: ManagedPaths,
    *,
    stdout: TextIO,
    now: datetime | None = None,
    executor: CommandExecutor | None = None,
) -> int:
    diagnostics = collect_diagnostics(
        paths,
        now=now,
        service_executor=executor or _default_executor,
    )
    symbols = {"OK": "[OK]", "WARNING": "[VAROVÁNÍ]", "ERROR": "[CHYBA]"}
    for item in diagnostics:
        print(f"{symbols[item.level]} {item.message}", file=stdout)
    errors = sum(item.level == "ERROR" for item in diagnostics)
    warnings = sum(item.level == "WARNING" for item in diagnostics)
    print(
        f"Výsledek: {len(diagnostics) - errors - warnings} OK, "
        f"{warnings} varování, {errors} chyb.",
        file=stdout,
    )
    return 1 if errors else 0


def run_self_test(*, stdout: TextIO) -> int:
    previous_health_override = os.environ.get(HEALTH_DISABLE_ENV)
    os.environ[HEALTH_DISABLE_ENV] = "1"
    client = None
    try:
        try:
            version_parts = __version__.split(".")
            if len(version_parts) != 3 or not all(part.isdecimal() for part in version_parts):
                raise ValueError("verze nemá formát X.Y.Z")

            # Import and construct the real Discord client without logging in. Tím
            # self-test zachytí chybějící runtime závislost, syntaktickou chybu i
            # nezabalený persistentní command ještě před přepnutím release.
            from .bot import TicketRenamerClient
            from .config import Settings
            from .parser import TicketForm, build_channel_name

            build_parser()
            if build_channel_name(TicketForm("Test User", "Záchranář")) is None:
                raise RuntimeError("parser názvu osobní složky není funkční")
            client = TicketRenamerClient(
                Settings(
                    discord_bot_token="offline-self-test",
                    ticket_tool_bot_ids=frozenset({1}),
                    ticket_category_ids=frozenset({1}),
                    request_channel_prefixes=("zadost-",),
                    request_history_limit=1,
                    channel_separator="・",
                    scan_existing_tickets=False,
                    scan_history_limit=1,
                    log_level=logging.INFO,
                )
            )
            for command_name in ("sluzebni-panel", "doplnit-zadost"):
                if client.tree.get_command(command_name) is None:
                    raise RuntimeError(f"slash command /{command_name} není zabalený")
            asyncio.run(client.close())
            client = None
        except Exception as exc:
            if client is not None:
                try:
                    asyncio.run(client.close())
                except Exception:
                    pass
            print(f"Self-test selhal: {exc}", file=stdout)
            return 1
        print(f"Self-test OK (verze {__version__}).", file=stdout)
        return 0
    finally:
        if previous_health_override is None:
            os.environ.pop(HEALTH_DISABLE_ENV, None)
        else:
            os.environ[HEALTH_DISABLE_ENV] = previous_health_override


def main(
    argv: Sequence[str] | None = None,
    *,
    executor: CommandExecutor | None = None,
    bot_runner: BotRunner | None = None,
    paths: ManagedPaths | None = None,
    environment: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    env = os.environ if environment is None else environment
    execute = executor or _default_executor

    if args.command == "version":
        print(__version__, file=out)
        return 0
    if args.command == "self-test":
        return run_self_test(stdout=out)
    if args.command == "doctor":
        return run_doctor(
            paths or ManagedPaths.from_environment(env),
            stdout=out,
            executor=execute,
        )
    if args.command == "run":
        raw_level = env.get("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, raw_level, logging.INFO)
        if not isinstance(level, int):
            level = logging.INFO
        configure_logging(
            level,
            stream=err,
            known_secrets=secret_values_from_environment(env),
        )
        runner = bot_runner or _default_bot_runner
        result = runner()
        return int(result) if isinstance(result, int) else 0

    if args.command == "status":
        return execute(("systemctl", "--user", "status", BOT_SERVICE, "--no-pager"))
    if args.command == "restart":
        return execute(("systemctl", "--user", "restart", BOT_SERVICE))
    if args.command == "update-now":
        return execute(("systemctl", "--user", "start", UPDATE_SERVICE))
    if args.command == "logs":
        command = [
            "journalctl",
            "--user",
            "--unit",
            BOT_SERVICE,
            "--lines",
            str(args.lines),
            "--output",
            "cat",
        ]
        if not args.no_follow:
            command.append("--follow")
        return execute(tuple(command))

    print(f"Neznámý příkaz: {args.command}", file=err)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
