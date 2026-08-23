#!/usr/bin/env python3
"""Build the verified Linux GitHub Release payload without third-party tools."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tomllib


REPOSITORY = "GameFunns98/Discord-BOT-EMS-trinity"


def project_version(root: Path) -> str:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    configured = str(pyproject["project"]["version"])

    init_text = (root / "ticket_renamer" / "__init__.py").read_text(encoding="utf-8")
    init_match = re.search(r'^__version__\s*=\s*["\'](\d+\.\d+\.\d+)["\']', init_text, re.M)
    if init_match is None or init_match.group(1) != configured:
        raise SystemExit("Verze v pyproject.toml a ticket_renamer/__init__.py se neshoduji.")

    readme = (root / "README.md").read_text(encoding="utf-8")
    readme_match = re.search(
        r"^# Discord Ticket Renamer\s+(\d+\.\d+\.\d+)\s*$",
        readme,
        re.M,
    )
    if readme_match is None or readme_match.group(1) != configured:
        raise SystemExit("Verze v README neodpovida verzi projektu.")
    return configured


def tracked_release_files(root: Path, lock_file: Path) -> list[tuple[Path, PurePosixPath]]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    selected: list[tuple[Path, PurePosixPath]] = []
    for raw_name in result.stdout.split(b"\0"):
        if not raw_name:
            continue
        relative = PurePosixPath(raw_name.decode("utf-8"))
        include = (
            (relative.parts[0] == "ticket_renamer" and relative.suffix == ".py")
            or relative.parts[0] == "linux"
            or relative.as_posix()
            in {
                "README.md",
                ".env.example",
                "pyproject.toml",
                "requirements.txt",
                "launcher.py",
            }
        )
        if include:
            source = root.joinpath(*relative.parts)
            if source.is_file():
                selected.append((source, relative))
    selected.append((lock_file, PurePosixPath("requirements-linux.lock")))

    forbidden = {".env", "TicketRenamerTray.exe"}
    if any(path.name in forbidden or path.suffix == ".log" for _, path in selected):
        raise SystemExit("Release vyber obsahuje zakazany konfiguracni nebo logovaci soubor.")
    selected_names = {path.as_posix() for _, path in selected}
    required = {
        ".env.example",
        "linux/install.sh",
        "linux/ticket-renamer",
        "linux/updater.py",
        "linux/systemd/discord-ticket-renamer.service",
        "linux/systemd/discord-ticket-renamer-update.service",
        "linux/systemd/discord-ticket-renamer-update.timer",
        "pyproject.toml",
        "requirements-linux.lock",
        "ticket_renamer/__init__.py",
        "ticket_renamer/__main__.py",
    }
    missing = sorted(required - selected_names)
    if missing:
        raise SystemExit(f"Release vyber neobsahuje povinne soubory: {', '.join(missing)}")
    return sorted(selected, key=lambda item: item[1].as_posix())


def build_archive(
    output: Path,
    root_name: str,
    files: list[tuple[Path, PurePosixPath]],
    *,
    epoch: int,
) -> None:
    with output.open("xb") as raw_output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_output, mtime=epoch) as gzip_output:
            with tarfile.open(mode="w", fileobj=gzip_output, format=tarfile.PAX_FORMAT) as archive:
                for source, relative in files:
                    content = source.read_bytes()
                    target = PurePosixPath(root_name) / relative
                    info = tarfile.TarInfo(target.as_posix())
                    info.size = len(content)
                    info.mtime = epoch
                    info.uid = 0
                    info.gid = 0
                    info.uname = "root"
                    info.gname = "root"
                    info.mode = 0o755 if source.suffix == ".sh" or source.name == "ticket-renamer" else 0o644
                    import io

                    archive.addfile(info, io.BytesIO(content))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("release-assets"))
    parser.add_argument("--lock-file", type=Path, default=Path("requirements-linux.lock"))
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    version = project_version(root)
    if args.tag != f"v{version}" or not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
        raise SystemExit(f"Tag {args.tag!r} neodpovida stabilni verzi v{version}.")

    lock_file = args.lock_file if args.lock_file.is_absolute() else root / args.lock_file
    if not lock_file.is_file() or not lock_file.read_text(encoding="utf-8").strip():
        raise SystemExit("Chybi neprazdny requirements-linux.lock.")
    output_dir = args.output_dir if args.output_dir.is_absolute() else root / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    asset_name = f"discord-ticket-renamer-{version}-linux.tar.gz"
    archive_path = output_dir / asset_name
    archive_path.unlink(missing_ok=True)
    epoch = int(os.getenv("SOURCE_DATE_EPOCH", "0"))
    build_archive(
        archive_path,
        f"discord-ticket-renamer-{version}",
        tracked_release_files(root, lock_file),
        epoch=epoch,
    )
    digest = sha256(archive_path)
    manifest = {
        "schema_version": 1,
        "version": version,
        "tag_name": args.tag,
        "repository": REPOSITORY,
        "minimum_python": ">=3.11",
        "assets": {
            "linux": {
                "name": asset_name,
                "sha256": digest,
                "size": archive_path.stat().st_size,
            }
        },
    }
    (output_dir / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / f"{asset_name}.sha256").write_text(
        f"{digest}  {asset_name}\n",
        encoding="ascii",
    )
    print(archive_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
