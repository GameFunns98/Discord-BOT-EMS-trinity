#!/usr/bin/env python3
"""Atomic GitHub Releases updater for the Linux installation.

This file intentionally uses only Python's standard library.  It is installed
outside versioned application directories, so it remains available while the
bot service is stopped and the ``current`` symlink is switched.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


UPDATER_VERSION = "1"
DEFAULT_REPOSITORY = "GameFunns98/Discord-BOT-EMS-trinity"
MANIFEST_ASSET_NAME = "release-manifest.json"
SERVICE_NAME = "discord-ticket-renamer.service"
MAX_MANIFEST_BYTES = 1_000_000
MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 5_000

LOGGER = logging.getLogger("ticket_renamer.updater")


class UpdateError(RuntimeError):
    """A safe, user-presentable updater failure."""


class UpdateLocked(UpdateError):
    """Another updater process already owns the update lock."""


@dataclass(frozen=True, order=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value.strip())
        if not match:
            raise UpdateError(f"Neplatna stabilni verze: {value!r}.")
        return cls(*(int(part) for part in match.groups()))

    @property
    def tag(self) -> str:
        return f"v{self}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    url: str
    size: int


@dataclass(frozen=True, slots=True)
class LatestRelease:
    tag: str
    version: Version
    assets: Mapping[str, ReleaseAsset]


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    version: Version
    tag: str
    repository: str
    linux_asset: ReleaseAsset
    sha256: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class InstallPaths:
    config_dir: Path
    data_dir: Path
    lib_dir: Path
    runtime_dir: Path

    @classmethod
    def from_environment(cls) -> "InstallPaths":
        home = Path.home()
        # The installed systemd units intentionally use these stable per-user
        # paths.  DTR_* remains available for isolated tests and deliberate
        # custom unit overrides, but ambient XDG settings must not split the
        # installer, service and updater across different directories.
        config_home = home / ".config"
        data_home = home / ".local" / "share"
        user_id = getattr(os, "getuid", lambda: 0)()
        runtime_home = Path(os.getenv("XDG_RUNTIME_DIR", f"/tmp/discord-ticket-renamer-{user_id}"))
        return cls(
            config_dir=Path(os.getenv("DTR_CONFIG_DIR", config_home / "discord-ticket-renamer")),
            data_dir=Path(os.getenv("DTR_DATA_DIR", data_home / "discord-ticket-renamer")),
            lib_dir=Path(
                os.getenv(
                    "DTR_LIB_DIR",
                    home / ".local" / "lib" / "discord-ticket-renamer",
                )
            ),
            runtime_dir=Path(
                os.getenv(
                    "DTR_RUNTIME_DIR",
                    runtime_home / "discord-ticket-renamer",
                )
            ),
        )

    @property
    def releases_dir(self) -> Path:
        return self.data_dir / "releases"

    @property
    def current_link(self) -> Path:
        return self.data_dir / "current"

    @property
    def previous_link(self) -> Path:
        return self.data_dir / "previous"

    @property
    def config_file(self) -> Path:
        return self.config_dir / ".env"

    @property
    def health_file(self) -> Path:
        return self.runtime_dir / "health.json"

    @property
    def lock_file(self) -> Path:
        return self.runtime_dir / "update.lock"

    @property
    def updater_file(self) -> Path:
        return self.lib_dir / "updater.py"

    def create_directories(self) -> None:
        for directory in (
            self.config_dir,
            self.data_dir,
            self.releases_dir,
            self.lib_dir,
            self.runtime_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


class FileLock:
    """Small non-blocking cross-platform lock; production uses ``flock``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any | None = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.path.stat().st_size == 0:
                    lock_file.write(b"0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            lock_file.close()
            raise UpdateLocked("Aktualizace uz probiha v jinem procesu.") from exc
        self._file = lock_file
        return self

    def __exit__(self, *_: object) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise UpdateError(f"{label} nema ocekavany objektovy format.")
    return value


def _require_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise UpdateError(f"V datech chybi textova hodnota {key}.")
    return value.strip()


def _require_size(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise UpdateError(f"{label} musi byt kladne cele cislo.")
    if value > MAX_ARCHIVE_BYTES:
        raise UpdateError(f"{label} prekrocil bezpecnostni limit aktualizatoru.")
    return value


def _require_github_download_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or not parsed.path:
        raise UpdateError("GitHub Release obsahuje nepovolenou adresu souboru.")
    if parsed.username is not None or parsed.password is not None:
        raise UpdateError("GitHub Release adresa nesmi obsahovat prihlasovaci udaje.")
    return value


def parse_latest_release(payload: object) -> LatestRelease:
    data = _require_mapping(payload, "GitHub release")
    if data.get("draft") is True or data.get("prerelease") is True:
        raise UpdateError("GitHub vratil koncept nebo predbeznou verzi misto stabilniho vydani.")
    tag = _require_string(data, "tag_name")
    version = Version.parse(tag)
    assets_data = data.get("assets")
    if not isinstance(assets_data, list):
        raise UpdateError("GitHub release neobsahuje seznam souboru.")

    assets: dict[str, ReleaseAsset] = {}
    for raw_asset in assets_data:
        asset_data = _require_mapping(raw_asset, "GitHub release asset")
        name = _require_string(asset_data, "name")
        url = _require_github_download_url(_require_string(asset_data, "browser_download_url"))
        size = _require_size(asset_data.get("size"), f"Velikost souboru {name}")
        if name in assets:
            raise UpdateError(f"Release obsahuje duplicitni soubor {name}.")
        assets[name] = ReleaseAsset(name=name, url=url, size=size)

    if MANIFEST_ASSET_NAME not in assets:
        raise UpdateError(f"Release neobsahuje {MANIFEST_ASSET_NAME}.")
    return LatestRelease(tag=tag, version=version, assets=assets)


def parse_manifest(
    raw: bytes,
    release: LatestRelease,
    *,
    expected_repository: str,
) -> ReleaseManifest:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("Manifest je prilis velky.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("Release manifest neni platny UTF-8 JSON.") from exc
    data = _require_mapping(payload, "Release manifest")
    if data.get("schema_version") != 1:
        raise UpdateError("Release manifest pouziva nepodporovanou verzi schematu.")

    repository = _require_string(data, "repository")
    if repository.casefold() != expected_repository.casefold():
        raise UpdateError("Release manifest patri jinemu GitHub repozitari.")
    tag = _require_string(data, "tag_name")
    version = Version.parse(_require_string(data, "version"))
    if tag != version.tag or tag != release.tag or version != release.version:
        raise UpdateError("Verze v manifestu neodpovida GitHub Release tagu.")
    if _require_string(data, "minimum_python") != ">=3.11":
        raise UpdateError("Release manifest obsahuje nepodporovanou verzi Pythonu.")

    assets = _require_mapping(data.get("assets"), "Manifest assets")
    linux_data = _require_mapping(assets.get("linux"), "Linux asset")
    asset_name = _require_string(linux_data, "name")
    if "/" in asset_name or "\\" in asset_name:
        raise UpdateError("Nazev Linux archivu nesmi obsahovat cestu.")
    digest = _require_string(linux_data, "sha256").casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError("SHA-256 v manifestu nema platny format.")
    declared_size = _require_size(linux_data.get("size"), "Velikost Linux archivu")
    release_asset = release.assets.get(asset_name)
    if release_asset is None:
        raise UpdateError("Linux archiv z manifestu chybi mezi GitHub Release soubory.")
    if release_asset.size != declared_size:
        raise UpdateError("Velikost Linux archivu nesouhlasi mezi GitHubem a manifestem.")

    return ReleaseManifest(
        version=version,
        tag=tag,
        repository=repository,
        linux_asset=release_asset,
        sha256=digest,
        raw=raw,
    )


class GitHubClient:
    def __init__(self, repository: str, *, timeout: float = 30.0) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise UpdateError("Neplatny nazev GitHub repozitare.")
        self.repository = repository
        self.timeout = timeout
        self.endpoint = f"https://api.github.com/repos/{repository}/releases/latest"

    @staticmethod
    def _request(url: str) -> Request:
        return Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"DiscordTicketRenamer-Updater/{UPDATER_VERSION}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _read(self, url: str, *, maximum: int) -> bytes:
        try:
            with urlopen(self._request(url), timeout=self.timeout) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > maximum:
                    raise UpdateError("Stahovany soubor prekrocil bezpecnostni limit.")
                data = response.read(maximum + 1)
        except HTTPError as exc:
            if exc.code == 429 or exc.code == 403:
                raise UpdateError("GitHub docasne omezil pocet pozadavku; bot zustava beze zmeny.") from exc
            raise UpdateError(f"GitHub vratil HTTP {exc.code}; bot zustava beze zmeny.") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise UpdateError("GitHub neni dostupny; bot zustava beze zmeny.") from exc
        if len(data) > maximum:
            raise UpdateError("Stahovany soubor prekrocil bezpecnostni limit.")
        return data

    def latest(self) -> tuple[LatestRelease, ReleaseManifest]:
        raw_release = self._read(self.endpoint, maximum=MAX_MANIFEST_BYTES)
        try:
            payload = json.loads(raw_release.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError("GitHub odpoved neni platny JSON.") from exc
        release = parse_latest_release(payload)
        manifest_asset = release.assets[MANIFEST_ASSET_NAME]
        raw_manifest = self._read(manifest_asset.url, maximum=MAX_MANIFEST_BYTES)
        manifest = parse_manifest(
            raw_manifest,
            release,
            expected_repository=self.repository,
        )
        return release, manifest

    def download(self, asset: ReleaseAsset, destination: Path) -> None:
        if asset.size > MAX_ARCHIVE_BYTES:
            raise UpdateError("Linux archiv je prilis velky.")
        request = self._request(asset.url)
        written = 0
        try:
            with urlopen(request, timeout=self.timeout) as response, destination.open("xb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > asset.size or written > MAX_ARCHIVE_BYTES:
                        raise UpdateError("Stazeny archiv je vetsi, nez uvadi GitHub.")
                    output.write(block)
        except HTTPError as exc:
            raise UpdateError(f"Linux archiv nelze stahnout (HTTP {exc.code}).") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise UpdateError("Linux archiv nelze stahnout; bot zustava beze zmeny.") from exc
        if written != asset.size:
            raise UpdateError("Stazeny archiv nema velikost uvedenou na GitHubu.")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_archive(path: Path, manifest: ReleaseManifest) -> None:
    if path.stat().st_size != manifest.linux_asset.size:
        raise UpdateError("Velikost stazeneho archivu nesouhlasi s manifestem.")
    if sha256_file(path) != manifest.sha256:
        raise UpdateError("SHA-256 stazeneho archivu nesouhlasi s manifestem.")


def _safe_member_path(name: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise UpdateError("Archiv obsahuje neplatnou cestu.")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UpdateError("Archiv se pokusil zapisovat mimo cilovy adresar.")
    return path


def safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve()
    total_size = 0
    seen: set[PurePosixPath] = set()
    try:
        source = tarfile.open(archive, mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError("Linux archiv neni platny tar.gz soubor.") from exc

    with source:
        members = source.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise UpdateError("Linux archiv obsahuje prilis mnoho souboru.")
        for member in members:
            relative = _safe_member_path(member.name)
            if relative in seen:
                raise UpdateError("Linux archiv obsahuje duplicitni cestu.")
            seen.add(relative)
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise UpdateError("Linux archiv obsahuje nepovoleny specialni soubor nebo odkaz.")
            if not member.isdir() and not member.isfile():
                raise UpdateError("Linux archiv obsahuje nepodporovany typ souboru.")
            total_size += member.size
            if total_size > MAX_EXTRACTED_BYTES:
                raise UpdateError("Rozbaleny archiv by prekrocil bezpecnostni limit.")

            target = destination.joinpath(*relative.parts)
            try:
                target.resolve().relative_to(destination_root)
            except ValueError as exc:
                raise UpdateError("Archiv se pokusil zapisovat mimo cilovy adresar.") from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = source.extractfile(member)
            if extracted is None:
                raise UpdateError("Z archivu nelze precist bezny soubor.")
            with extracted, target.open("xb") as output:
                shutil.copyfileobj(extracted, output, length=1024 * 1024)
            target.chmod(0o755 if member.mode & stat.S_IXUSR else 0o644)


def find_release_root(extracted: Path) -> Path:
    children = list(extracted.iterdir())
    if len(children) != 1 or not children[0].is_dir():
        raise UpdateError("Linux archiv musi obsahovat prave jeden korenovy adresar.")
    root = children[0]
    required = (
        root / "pyproject.toml",
        root / "ticket_renamer" / "__init__.py",
        root / "requirements-linux.lock",
        root / "linux" / "updater.py",
    )
    if any(not item.is_file() for item in required):
        raise UpdateError("Linux archiv neobsahuje vsechny povinne soubory.")
    return root


def atomic_symlink(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(f".{link.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def symlink_target(link: Path) -> Path | None:
    if not link.is_symlink():
        return None
    raw_target = Path(os.readlink(link))
    if not raw_target.is_absolute():
        raw_target = link.parent / raw_target
    return raw_target.resolve()


def read_installed_version(release_dir: Path | None) -> Version | None:
    if release_dir is None:
        return None
    manifest_file = release_dir / MANIFEST_ASSET_NAME
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
        return Version.parse(str(data["version"]))
    except (OSError, KeyError, TypeError, json.JSONDecodeError, UpdateError):
        return None


def _venv_python(release_dir: Path) -> Path:
    if os.name == "nt":
        return release_dir / ".venv" / "Scripts" / "python.exe"
    return release_dir / ".venv" / "bin" / "python"


def run_command(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    check: bool = True,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    rendered = [os.fspath(part) for part in command]
    result = subprocess.run(
        rendered,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.STDOUT if quiet else None,
    )
    if check and result.returncode != 0:
        raise UpdateError(f"Prikaz {Path(rendered[0]).name} skoncil chybou {result.returncode}.")
    return result


def link_environment_file(release_dir: Path, config_file: Path) -> None:
    if not config_file.is_file():
        raise UpdateError(f"Konfigurace chybi: {config_file}")
    target = release_dir / ".env"
    if target.is_symlink() and target.resolve() == config_file.resolve():
        return
    if target.exists() or target.is_symlink():
        raise UpdateError("Release neocekavane obsahuje vlastni .env; aktualizace byla zastavena.")
    target.symlink_to(config_file)


def _unique_failed_path(release_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = release_dir.with_name(f"{release_dir.name}.failed-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = release_dir.with_name(f"{release_dir.name}.failed-{stamp}-{suffix}")
        suffix += 1
    return candidate


class UpdateManager:
    def __init__(
        self,
        paths: InstallPaths,
        client: GitHubClient,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = run_command,
        link_switcher: Callable[[Path, Path], None] = atomic_symlink,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.paths = paths
        self.client = client
        self.command_runner = command_runner
        self.link_switcher = link_switcher
        self.sleep = sleep
        self.clock = clock

    def _run(self, command: Sequence[str | os.PathLike[str]], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return self.command_runner(command, **kwargs)

    def _self_test(self, release_dir: Path) -> None:
        python = _venv_python(release_dir)
        if not python.is_file():
            raise UpdateError("Virtualni prostredi nove verze nema Python interpreter.")
        self._run(
            [python, "-m", "ticket_renamer", "self-test"],
            cwd=release_dir,
            quiet=True,
        )

    def prepare_release(self, archive: Path, manifest: ReleaseManifest, work_dir: Path) -> Path:
        self.prune_failed_releases(max_keep=1)
        release_dir = self.paths.releases_dir / str(manifest.version)
        if release_dir.exists():
            release_dir.rename(_unique_failed_path(release_dir))
            self.prune_failed_releases(max_keep=1)

        extracted = work_dir / "extracted"
        safe_extract_tar(archive, extracted)
        archive_root = find_release_root(extracted)
        shutil.move(os.fspath(archive_root), os.fspath(release_dir))
        try:
            (release_dir / MANIFEST_ASSET_NAME).write_bytes(manifest.raw)
            link_environment_file(release_dir, self.paths.config_file)
            self._run([sys.executable, "-m", "venv", release_dir / ".venv"])
            python = _venv_python(release_dir)
            self._run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "-r",
                    release_dir / "requirements-linux.lock",
                ],
                cwd=release_dir,
            )
            self._self_test(release_dir)
        except Exception:
            if release_dir.exists():
                release_dir.rename(_unique_failed_path(release_dir))
            self.prune_failed_releases(max_keep=1)
            raise
        LOGGER.info("Nova verze %s je pripravena a self-test uspel.", manifest.version)
        return release_dir

    def _systemctl(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._run(
            ["systemctl", "--user", *arguments, SERVICE_NAME],
            check=check,
            quiet=True,
        )

    def wait_for_health(self, version: Version, started_after: float, *, timeout: float = 60.0) -> bool:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            active = self._systemctl("is-active", "--quiet", check=False).returncode == 0
            if active:
                try:
                    payload = json.loads(self.paths.health_file.read_text(encoding="utf-8"))
                    raw_timestamp = payload.get("timestamp")
                    if not isinstance(raw_timestamp, str):
                        raise ValueError("missing health timestamp")
                    normalized_timestamp = raw_timestamp[:-1] + "+00:00" if raw_timestamp.endswith("Z") else raw_timestamp
                    updated_at = datetime.fromisoformat(normalized_timestamp).timestamp()
                    if (
                        payload.get("ready") is True
                        and payload.get("state") == "ready"
                        and payload.get("version") == str(version)
                        and isinstance(payload.get("pid"), int)
                        and payload["pid"] > 0
                        and updated_at >= started_after - 1
                        and updated_at <= self.clock() + 5
                    ):
                        return True
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            self.sleep(1.0)
        return False

    def activate_release(
        self,
        release_dir: Path,
        old_target: Path | None,
        version: Version,
    ) -> None:
        if old_target is not None:
            self.link_switcher(old_target, self.paths.previous_link)
        self._systemctl("stop")
        switched = False
        try:
            self.link_switcher(release_dir, self.paths.current_link)
            switched = True
            try:
                self.paths.health_file.unlink(missing_ok=True)
            except OSError:
                pass
            started_at = self.clock()
            self._systemctl("start")
            if not self.wait_for_health(version, started_at):
                raise UpdateError("Nova verze se do 60 sekund nepripojila k Discordu.")
        except Exception as update_error:
            if not switched:
                if old_target is None:
                    raise
                LOGGER.error(
                    "Prepnuti odkazu na novou verzi selhalo; znovu spoustim puvodni verzi."
                )
                try:
                    self.paths.health_file.unlink(missing_ok=True)
                except OSError:
                    pass
                restore_started = self.clock()
                self._systemctl("start")
                old_version = read_installed_version(old_target)
                if old_version is None or not self.wait_for_health(old_version, restore_started):
                    raise UpdateError(
                        "Prepnuti verze selhalo a nepodarilo se potvrdit opetovne spusteni puvodni verze."
                    ) from update_error
                raise UpdateError(
                    "Prepnuti verze selhalo; puvodni funkcni verze byla znovu spustena."
                ) from update_error
            LOGGER.error("Aktivace nove verze selhala; probiha automaticky navrat.")
            self._systemctl("stop", check=False)
            if old_target is None:
                if self.paths.current_link.is_symlink():
                    self.paths.current_link.unlink(missing_ok=True)
                raise UpdateError(
                    "Nova verze selhala a neni k dispozici predchozi verze pro obnoveni."
                ) from update_error
            self.link_switcher(old_target, self.paths.current_link)
            try:
                self.paths.health_file.unlink(missing_ok=True)
            except OSError:
                pass
            rollback_started = self.clock()
            self._systemctl("start")
            old_version = read_installed_version(old_target)
            if old_version is None or not self.wait_for_health(old_version, rollback_started):
                raise UpdateError(
                    "Nova verze selhala a nepodarilo se potvrdit ani obnoveni predchozi verze."
                ) from update_error
            raise UpdateError("Nova verze selhala; predchozi funkcni verze byla obnovena.") from update_error

    def replace_updater(self, release_dir: Path) -> None:
        source = release_dir / "linux" / "updater.py"
        if not source.is_file():
            raise UpdateError("Nova verze neobsahuje samostatny aktualizator.")
        check_result = self._run([sys.executable, source, "version"], quiet=True)
        if not re.fullmatch(r"\d+", (check_result.stdout or "").strip()):
            raise UpdateError("Nova kopie aktualizatoru neprosla vlastnim testem spusteni.")
        destination = self.paths.updater_file
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.new-{os.getpid()}")
        backup = destination.with_name(f"{destination.name}.previous")
        backup_temp = destination.with_name(f".{destination.name}.previous-{os.getpid()}")
        try:
            shutil.copy2(source, temporary)
            temporary.chmod(0o755)
            if destination.is_file():
                shutil.copy2(destination, backup_temp)
                os.replace(backup_temp, backup)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
            backup_temp.unlink(missing_ok=True)

    def prune_releases(self) -> None:
        protected = {
            target
            for target in (
                symlink_target(self.paths.current_link),
                symlink_target(self.paths.previous_link),
            )
            if target is not None
        }
        versioned: list[tuple[Version, Path]] = []
        for child in self.paths.releases_dir.iterdir():
            if child.is_symlink() or not child.is_dir() or ".failed-" in child.name:
                continue
            try:
                versioned.append((Version.parse(child.name), child.resolve()))
            except UpdateError:
                continue
        versioned.sort(reverse=True)
        protected.update(path for _, path in versioned[:2])
        releases_root = self.paths.releases_dir.resolve()
        for _, release_dir in versioned:
            if release_dir in protected:
                continue
            try:
                release_dir.relative_to(releases_root)
            except ValueError:
                continue
            shutil.rmtree(release_dir)
        self.prune_failed_releases(max_keep=1)

    def prune_failed_releases(self, *, max_keep: int) -> None:
        if max_keep < 0:
            raise ValueError("max_keep nesmi byt zaporne")
        releases_root = self.paths.releases_dir.resolve()
        protected = {
            target
            for target in (
                symlink_target(self.paths.current_link),
                symlink_target(self.paths.previous_link),
            )
            if target is not None
        }
        failed_pattern = re.compile(
            r"^\d+\.\d+\.\d+\.failed-\d{8}T\d{6}Z(?:-\d+)?$"
        )
        failed: list[Path] = []
        for child in self.paths.releases_dir.iterdir():
            if child.is_symlink() or not child.is_dir() or not failed_pattern.fullmatch(child.name):
                continue
            resolved = child.resolve()
            try:
                resolved.relative_to(releases_root)
            except ValueError:
                continue
            if resolved not in protected:
                failed.append(resolved)
        failed.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for failed_release in failed[max_keep:]:
            shutil.rmtree(failed_release)

    def update(self) -> bool:
        self.paths.create_directories()
        with FileLock(self.paths.lock_file):
            _, manifest = self.client.latest()
            old_target = symlink_target(self.paths.current_link)
            current_version = read_installed_version(old_target)
            if current_version is not None and manifest.version <= current_version:
                LOGGER.info("Aktualni verze %s je nejnovejsi.", current_version)
                return False

            LOGGER.info("Byla nalezena nova stabilni verze %s.", manifest.version)
            expected_release_dir = (self.paths.releases_dir / str(manifest.version)).resolve()
            if old_target is not None and old_target.resolve() == expected_release_dir:
                raise UpdateError(
                    "Aktivni release ma poskozeny manifest; automaticka aktualizace jej nebude menit."
                )
            with tempfile.TemporaryDirectory(prefix="update-", dir=self.paths.data_dir) as raw_work:
                work_dir = Path(raw_work)
                archive = work_dir / manifest.linux_asset.name
                self.client.download(manifest.linux_asset, archive)
                verify_archive(archive, manifest)
                release_dir = self.prepare_release(archive, manifest, work_dir)

            self.activate_release(release_dir, old_target, manifest.version)
            try:
                self.replace_updater(release_dir)
            except UpdateError as exc:
                LOGGER.warning("Bot byl aktualizovan, ale kopii aktualizatoru nelze nahradit: %s", exc)
            self.prune_releases()
            LOGGER.info("Aktualizace na verzi %s byla uspesne dokoncena.", manifest.version)
            return True


def migrate_env_log_level(path: Path, *, now: datetime | None = None) -> Path | None:
    """Change only active ``LOG_LEVEL=DEBUG`` values and retain a mode-0600 backup."""

    if not path.is_file():
        raise UpdateError(f"Konfigurace chybi: {path}")
    original = path.read_text(encoding="utf-8-sig")
    pattern = re.compile(
        r"^(?P<prefix>\s*LOG_LEVEL\s*=\s*)(?P<quote>['\"]?)DEBUG(?P=quote)(?P<suffix>\s*(?:#.*)?(?:\r?\n|$))",
        re.IGNORECASE | re.MULTILINE,
    )
    updated, count = pattern.subn(r"\g<prefix>INFO\g<suffix>", original)
    if count == 0:
        return None

    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.backup-{timestamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{timestamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    backup.chmod(0o600)

    temporary = path.with_name(f".{path.name}.new-{os.getpid()}")
    try:
        temporary.write_text(updated, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | updater | %(message)s",
        datefmt="%H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discord Ticket Renamer Linux updater")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    update_parser = subparsers.add_parser("update", help="zkontrolovat a nainstalovat novy release")
    update_parser.add_argument(
        "--repository",
        default=os.getenv("DTR_GITHUB_REPOSITORY", DEFAULT_REPOSITORY),
    )
    migrate_parser = subparsers.add_parser("migrate-env", help="bezpecne zmenit DEBUG na INFO")
    migrate_parser.add_argument("path", type=Path)
    subparsers.add_parser("version", help="vypsat verzi aktualizatoru")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    command = args.command or "update"
    try:
        if command == "version":
            print(UPDATER_VERSION)
            return 0
        if command == "migrate-env":
            backup = migrate_env_log_level(args.path)
            if backup is None:
                LOGGER.info("LOG_LEVEL nebylo nastaveno na DEBUG; konfigurace zustala beze zmeny.")
            else:
                LOGGER.info("LOG_LEVEL bylo zmeneno na INFO; zaloha: %s", backup)
            return 0
        paths = InstallPaths.from_environment()
        client = GitHubClient(args.repository)
        UpdateManager(paths, client).update()
        return 0
    except UpdateLocked as exc:
        LOGGER.info("%s", exc)
        return 75
    except UpdateError as exc:
        LOGGER.error("%s", exc)
        return 1
    except Exception:
        LOGGER.exception("Neocekavana chyba aktualizatoru; bezici verze nebyla zamerne zastavena.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
