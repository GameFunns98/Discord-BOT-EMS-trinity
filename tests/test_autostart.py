import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from ticket_renamer import autostart


class AutostartTests(unittest.TestCase):
    def test_module_import_without_winreg_is_safe(self) -> None:
        module_path = Path(autostart.__file__)
        specification = importlib.util.spec_from_file_location(
            "ticket_renamer_test_autostart_without_winreg",
            module_path,
        )
        self.assertIsNotNone(specification)
        self.assertIsNotNone(specification.loader)
        module = importlib.util.module_from_spec(specification)

        with patch.dict(sys.modules, {"winreg": None}):
            specification.loader.exec_module(module)  # type: ignore[union-attr]

        self.assertFalse(module.is_enabled())
        with self.assertRaisesRegex(OSError, "jen ve Windows"):
            module.enable()
        with self.assertRaisesRegex(OSError, "jen ve Windows"):
            module.disable()


if __name__ == "__main__":
    unittest.main()
