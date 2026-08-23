from pathlib import Path
import re
import tomllib
import unittest

from ticket_renamer import __version__


ROOT = Path(__file__).resolve().parents[1]


class VersionConsistencyTests(unittest.TestCase):
    def test_package_metadata_and_readme_show_same_version(self) -> None:
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project_version = metadata["project"]["version"]
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        heading = re.search(
            r"^# Discord Ticket Renamer (?P<version>\d+\.\d+\.\d+)\s*$",
            readme,
            re.MULTILINE,
        )

        self.assertIsNotNone(heading)
        self.assertEqual(project_version, __version__)
        self.assertEqual(heading.group("version"), project_version)


if __name__ == "__main__":
    unittest.main()
