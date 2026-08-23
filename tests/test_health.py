import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ticket_renamer.health import health_file_from_environment, write_health_marker


class HealthMarkerTests(unittest.TestCase):
    def test_managed_runtime_path_and_atomic_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            path = health_file_from_environment({"DTR_RUNTIME_DIR": str(runtime)})

            self.assertEqual(path, runtime / "health.json")
            result = write_health_marker(
                ready=True,
                state="ready",
                path=path,
                version="2.5.0",
                pid=1234,
                updated_at=123.5,
            )

            self.assertEqual(result, path)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {
                    "version": "2.5.0",
                    "pid": 1234,
                    "state": "ready",
                    "ready": True,
                    "updated_at": 123.5,
                    "timestamp": "1970-01-01T00:02:03.500000Z",
                },
            )
            self.assertEqual(list(runtime.glob(".health-*.tmp")), [])

    def test_unmanaged_windows_style_environment_is_noop(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(health_file_from_environment())
            self.assertIsNone(write_health_marker(ready=False, state="stopped"))

    def test_disable_override_blocks_implicit_and_explicit_health_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            explicit = runtime / "explicit.json"
            environment = {
                "DTR_DISABLE_HEALTH": "true",
                "DTR_RUNTIME_DIR": str(runtime),
            }

            self.assertIsNone(health_file_from_environment(environment))
            with patch.dict(os.environ, environment, clear=True):
                self.assertIsNone(
                    write_health_marker(
                        ready=False,
                        state="stopping",
                        path=explicit,
                    )
                )

            self.assertFalse(explicit.exists())


if __name__ == "__main__":
    unittest.main()
