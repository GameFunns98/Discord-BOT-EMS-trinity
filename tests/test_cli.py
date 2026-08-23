from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ticket_renamer import __version__
from ticket_renamer.cli import ManagedPaths, collect_diagnostics, main


class CliTests(unittest.TestCase):
    def _paths(self, root: Path) -> ManagedPaths:
        config = root / "config"
        data = root / "data"
        return ManagedPaths(
            config_dir=config,
            env_file=config / ".env",
            data_dir=data,
            releases_dir=data / "releases",
            current=data / "current",
            updater=root / "updater.py",
            health_file=root / "runtime/health.json",
        )

    def test_version_and_self_test_do_not_call_services_or_bot(self) -> None:
        commands: list[tuple[str, ...]] = []
        bot_calls: list[bool] = []
        executor = lambda command: commands.append(tuple(command)) or 0
        runner = lambda: bot_calls.append(True)

        version_output = io.StringIO()
        self.assertEqual(
            main(["version"], executor=executor, bot_runner=runner, stdout=version_output),
            0,
        )
        self.assertEqual(version_output.getvalue().strip(), __version__)

        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            health = runtime / "discord-ticket-renamer/health.json"
            health.parent.mkdir(parents=True)
            original_marker = '{"version":"2.4.0","ready":true}\n'
            health.write_text(original_marker, encoding="utf-8")
            test_output = io.StringIO()

            with patch.dict(
                os.environ,
                {"XDG_RUNTIME_DIR": str(runtime)},
                clear=True,
            ):
                self.assertEqual(
                    main(
                        ["self-test"],
                        executor=executor,
                        bot_runner=runner,
                        stdout=test_output,
                    ),
                    0,
                )
                self.assertNotIn("DTR_DISABLE_HEALTH", os.environ)

            self.assertEqual(health.read_text(encoding="utf-8"), original_marker)
            self.assertIn("Self-test OK", test_output.getvalue())
        self.assertEqual(commands, [])
        self.assertEqual(bot_calls, [])

    def test_service_commands_are_exact_and_injectable(self) -> None:
        calls: list[tuple[str, ...]] = []

        def execute(command: object) -> int:
            calls.append(tuple(command))  # type: ignore[arg-type]
            return 23

        self.assertEqual(main(["status"], executor=execute), 23)
        self.assertEqual(main(["restart"], executor=execute), 23)
        self.assertEqual(main(["update-now"], executor=execute), 23)
        self.assertEqual(
            calls,
            [
                ("systemctl", "--user", "status", "discord-ticket-renamer.service", "--no-pager"),
                ("systemctl", "--user", "restart", "discord-ticket-renamer.service"),
                ("systemctl", "--user", "start", "discord-ticket-renamer-update.service"),
            ],
        )

    def test_logs_follow_by_default_and_can_be_bounded(self) -> None:
        calls: list[tuple[str, ...]] = []
        execute = lambda command: calls.append(tuple(command)) or 0

        main(["logs", "--lines", "25"], executor=execute)
        main(["logs", "--lines", "5", "--no-follow"], executor=execute)

        self.assertEqual(calls[0][-1], "--follow")
        self.assertNotIn("--follow", calls[1])
        self.assertIn("25", calls[0])
        self.assertIn("5", calls[1])

    def test_run_uses_injected_runner_and_redacts_environment_secret(self) -> None:
        calls: list[bool] = []
        stderr = io.StringIO()
        with patch("ticket_renamer.cli.configure_logging") as configure:
            result = main(
                ["run"],
                bot_runner=lambda: calls.append(True),
                environment={"LOG_LEVEL": "DEBUG", "DISCORD_BOT_TOKEN": "very-secret"},
                stderr=stderr,
            )

        self.assertEqual(result, 0)
        self.assertEqual(calls, [True])
        self.assertEqual(configure.call_args.args[0], 10)
        self.assertEqual(configure.call_args.kwargs["known_secrets"], ("very-secret",))

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits are Linux-only")
    def test_doctor_reports_insecure_env_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            paths.config_dir.mkdir()
            paths.env_file.write_text("SECRET=value", encoding="utf-8")
            paths.env_file.chmod(0o644)

            diagnostics = collect_diagnostics(paths)

        self.assertTrue(
            any(item.level == "ERROR" and "0600" in item.message for item in diagnostics)
        )

    def test_doctor_accepts_complete_managed_layout_without_reading_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            release = paths.releases_dir / "2.5.0"
            python = release / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            paths.config_dir.mkdir(parents=True)
            paths.env_file.write_text("DO_NOT_PRINT=top-secret", encoding="utf-8")
            if os.name == "posix":
                paths.env_file.chmod(0o600)
            if os.name == "posix":
                paths.current.symlink_to(release, target_is_directory=True)
            else:
                # Creating symlinks needs a special Windows privilege.  The
                # production layout is Linux-only, while this branch keeps the
                # test portable without changing what doctor validates.
                current_python = paths.current / ".venv/Scripts/python.exe"
                current_python.parent.mkdir(parents=True)
                current_python.touch()
            paths.updater.parent.mkdir(parents=True, exist_ok=True)
            paths.updater.touch()
            paths.health_file.parent.mkdir(parents=True)
            paths.health_file.write_text(
                json.dumps(
                    {
                        "ready": True,
                        "state": "ready",
                        "version": "2.5.0",
                        "pid": 1234,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            service_calls: list[tuple[str, ...]] = []

            result = main(
                ["doctor"],
                paths=paths,
                stdout=output,
                executor=lambda command: service_calls.append(tuple(command)) or 0,
            )

        self.assertEqual(result, 0)
        self.assertIn("2.5.0", output.getvalue())
        self.assertIn("Služba bota je aktivní", output.getvalue())
        self.assertIn("Časovač automatických aktualizací je povolený", output.getvalue())
        self.assertNotIn("top-secret", output.getvalue())
        self.assertEqual(
            service_calls,
            [
                (
                    "systemctl",
                    "--user",
                    "is-active",
                    "--quiet",
                    "discord-ticket-renamer.service",
                ),
                (
                    "systemctl",
                    "--user",
                    "is-enabled",
                    "--quiet",
                    "discord-ticket-renamer.service",
                ),
                (
                    "systemctl",
                    "--user",
                    "is-active",
                    "--quiet",
                    "discord-ticket-renamer-update.timer",
                ),
                (
                    "systemctl",
                    "--user",
                    "is-enabled",
                    "--quiet",
                    "discord-ticket-renamer-update.timer",
                ),
            ],
        )

    def test_old_readiness_marker_is_reported_as_stale_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            paths.health_file.parent.mkdir(parents=True)
            paths.health_file.write_text(
                json.dumps(
                    {
                        "ready": True,
                        "state": "ready",
                        "version": "2.5.0",
                        "pid": 1234,
                        "timestamp": "2026-08-23T17:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            diagnostics = collect_diagnostics(
                paths,
                now=datetime(2026, 8, 23, 18, 0, tzinfo=timezone.utc),
            )

        health = [item for item in diagnostics if "health marker" in item.message.casefold()]
        self.assertTrue(
            any(
                item.level == "ERROR" and "zastaralý" in item.message
                for item in health
            )
        )

    def test_health_marker_requires_ready_state_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            paths.health_file.parent.mkdir(parents=True)
            base = {"ready": True, "version": "2.5.0", "pid": 1234}

            paths.health_file.write_text(json.dumps(base), encoding="utf-8")
            missing_state = collect_diagnostics(paths)

            paths.health_file.write_text(
                json.dumps({**base, "state": "ready"}),
                encoding="utf-8",
            )
            missing_timestamp = collect_diagnostics(paths)

        self.assertTrue(
            any(item.level == "ERROR" and "stav ready" in item.message for item in missing_state)
        )
        self.assertTrue(
            any(item.level == "ERROR" and "timestamp" in item.message for item in missing_timestamp)
        )

    def test_doctor_reports_inactive_update_timer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))

            def execute(command: object) -> int:
                parts = tuple(command)  # type: ignore[arg-type]
                return 3 if "discord-ticket-renamer-update.timer" in parts else 0

            diagnostics = collect_diagnostics(paths, service_executor=execute)

        self.assertTrue(
            any(
                item.level == "ERROR"
                and "Časovač automatických aktualizací není" in item.message
                for item in diagnostics
            )
        )

    def test_doctor_warns_about_legacy_debug_logs_without_reading_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            legacy = root / "legacy"
            legacy.mkdir()
            sensitive = "Datum narození: never-read-by-doctor"
            (legacy / "TicketRenamer.log.1").write_text(sensitive, encoding="utf-8")
            paths.config_dir.mkdir()
            (paths.config_dir / "legacy-source").write_text(str(legacy), encoding="utf-8")

            diagnostics = collect_diagnostics(paths)

        warning = next(
            item
            for item in diagnostics
            if item.level == "WARNING" and "citlivá DEBUG data" in item.message
        )
        self.assertNotIn(sensitive, warning.message)

    def test_managed_paths_honor_updater_directory_overrides(self) -> None:
        environment = {
            "DTR_CONFIG_DIR": "/srv/dtr/config",
            "DTR_DATA_DIR": "/srv/dtr/data",
            "DTR_LIB_DIR": "/srv/dtr/lib",
            "DTR_RUNTIME_DIR": "/run/user/1000/dtr",
        }

        paths = ManagedPaths.from_environment(environment, home=Path("/home/bot"))

        self.assertEqual(paths.env_file, Path("/srv/dtr/config/.env"))
        self.assertEqual(paths.current, Path("/srv/dtr/data/current"))
        self.assertEqual(paths.updater, Path("/srv/dtr/lib/updater.py"))
        self.assertEqual(paths.health_file, Path("/run/user/1000/dtr/health.json"))

    def test_managed_paths_ignore_ambient_xdg_config_and_data_locations(self) -> None:
        environment = {
            "XDG_CONFIG_HOME": "/unexpected/config",
            "XDG_DATA_HOME": "/unexpected/data",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }

        paths = ManagedPaths.from_environment(environment, home=Path("/home/bot"))

        self.assertEqual(
            paths.env_file,
            Path("/home/bot/.config/discord-ticket-renamer/.env"),
        )
        self.assertEqual(
            paths.current,
            Path("/home/bot/.local/share/discord-ticket-renamer/current"),
        )
        self.assertEqual(
            paths.health_file,
            Path("/run/user/1000/discord-ticket-renamer/health.json"),
        )


if __name__ == "__main__":
    unittest.main()
