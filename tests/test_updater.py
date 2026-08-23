from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

from linux import updater


def _release_payload(*, archive_size: int = 123) -> dict[str, object]:
    return {
        "tag_name": "v2.5.0",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "release-manifest.json",
                "browser_download_url": "https://github.com/example/project/releases/download/v2.5.0/manifest",
                "size": 300,
            },
            {
                "name": "discord-ticket-renamer-2.5.0-linux.tar.gz",
                "browser_download_url": "https://github.com/example/project/releases/download/v2.5.0/archive",
                "size": archive_size,
            },
        ],
    }


def _manifest_raw(*, archive_size: int = 123, digest: str = "a" * 64) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "version": "2.5.0",
            "tag_name": "v2.5.0",
            "repository": updater.DEFAULT_REPOSITORY,
            "minimum_python": ">=3.11",
            "assets": {
                "linux": {
                    "name": "discord-ticket-renamer-2.5.0-linux.tar.gz",
                    "sha256": digest,
                    "size": archive_size,
                }
            },
        }
    ).encode()


def _manifest(*, archive_size: int = 123, digest: str = "a" * 64) -> updater.ReleaseManifest:
    release = updater.parse_latest_release(_release_payload(archive_size=archive_size))
    return updater.parse_manifest(
        _manifest_raw(archive_size=archive_size, digest=digest),
        release,
        expected_repository=updater.DEFAULT_REPOSITORY,
    )


def _paths(tmp_path: Path) -> updater.InstallPaths:
    return updater.InstallPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        lib_dir=tmp_path / "lib",
        runtime_dir=tmp_path / "runtime",
    )


def _write_local_manifest(directory: Path, version: str) -> None:
    directory.mkdir(parents=True)
    (directory / updater.MANIFEST_ASSET_NAME).write_text(
        json.dumps({"version": version}),
        encoding="utf-8",
    )


def test_version_accepts_only_stable_semver() -> None:
    assert updater.Version.parse("v2.5.0") == updater.Version(2, 5, 0)
    assert str(updater.Version.parse("10.12.3")) == "10.12.3"
    for invalid in ("2.5", "v2.5.0-rc1", "v02.5.0", "latest", "2.5.0.1"):
        with pytest.raises(updater.UpdateError):
            updater.Version.parse(invalid)


def test_release_rejects_prerelease_and_duplicate_assets() -> None:
    prerelease = _release_payload()
    prerelease["prerelease"] = True
    with pytest.raises(updater.UpdateError, match="predbeznou"):
        updater.parse_latest_release(prerelease)

    duplicate = _release_payload()
    duplicate["assets"] = [*duplicate["assets"], duplicate["assets"][0]]  # type: ignore[index]
    with pytest.raises(updater.UpdateError, match="duplicitni"):
        updater.parse_latest_release(duplicate)

    unsafe_url = _release_payload()
    unsafe_url["assets"][0]["browser_download_url"] = "file:///etc/passwd"  # type: ignore[index]
    with pytest.raises(updater.UpdateError, match="nepovolenou"):
        updater.parse_latest_release(unsafe_url)


def test_manifest_must_match_tag_repository_asset_and_size() -> None:
    release = updater.parse_latest_release(_release_payload())
    parsed = updater.parse_manifest(
        _manifest_raw(),
        release,
        expected_repository=updater.DEFAULT_REPOSITORY,
    )
    assert parsed.version == updater.Version(2, 5, 0)
    assert parsed.linux_asset.size == 123

    wrong_repo = json.loads(_manifest_raw())
    wrong_repo["repository"] = "attacker/project"
    with pytest.raises(updater.UpdateError, match="jinemu"):
        updater.parse_manifest(
            json.dumps(wrong_repo).encode(),
            release,
            expected_repository=updater.DEFAULT_REPOSITORY,
        )

    wrong_size = json.loads(_manifest_raw())
    wrong_size["assets"]["linux"]["size"] = 122
    with pytest.raises(updater.UpdateError, match="Velikost"):
        updater.parse_manifest(
            json.dumps(wrong_size).encode(),
            release,
            expected_repository=updater.DEFAULT_REPOSITORY,
        )


def test_archive_checksum_and_size_are_verified(tmp_path: Path) -> None:
    archive = tmp_path / "release.tar.gz"
    archive.write_bytes(b"verified archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    updater.verify_archive(archive, _manifest(archive_size=archive.stat().st_size, digest=digest))

    with pytest.raises(updater.UpdateError, match="SHA-256"):
        updater.verify_archive(
            archive,
            _manifest(archive_size=archive.stat().st_size, digest="0" * 64),
        )


def _create_tar(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for info, content in members:
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def test_safe_extract_allows_regular_files(tmp_path: Path) -> None:
    archive = tmp_path / "safe.tar.gz"
    info = tarfile.TarInfo("discord-ticket-renamer-2.5.0/README.md")
    _create_tar(archive, [(info, b"safe")])
    destination = tmp_path / "out"
    updater.safe_extract_tar(archive, destination)
    assert (destination / "discord-ticket-renamer-2.5.0" / "README.md").read_bytes() == b"safe"


@pytest.mark.parametrize("member_name", ["../outside", "/absolute", "root/../../outside", "root\\file"])
def test_safe_extract_rejects_path_traversal(tmp_path: Path, member_name: str) -> None:
    archive = tmp_path / "bad.tar.gz"
    _create_tar(archive, [(tarfile.TarInfo(member_name), b"bad")])
    with pytest.raises(updater.UpdateError):
        updater.safe_extract_tar(archive, tmp_path / "out")
    assert not (tmp_path / "outside").exists()


def test_safe_extract_rejects_links(tmp_path: Path) -> None:
    archive = tmp_path / "link.tar.gz"
    info = tarfile.TarInfo("root/link")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    _create_tar(archive, [(info, b"")])
    with pytest.raises(updater.UpdateError, match="specialni"):
        updater.safe_extract_tar(archive, tmp_path / "out")


def test_env_migration_backs_up_and_changes_only_log_level(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "DISCORD_BOT_TOKEN=secret-value\nLOG_LEVEL=\"DEBUG\" # old\nOTHER=DEBUG\n"
    env_file.write_text(original, encoding="utf-8")
    moment = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

    backup = updater.migrate_env_log_level(env_file, now=moment)

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == original
    assert env_file.read_text(encoding="utf-8") == (
        "DISCORD_BOT_TOKEN=secret-value\nLOG_LEVEL=INFO # old\nOTHER=DEBUG\n"
    )
    assert updater.migrate_env_log_level(env_file, now=moment) is None


def test_health_requires_matching_fresh_ready_marker(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.create_directories()
    timestamp = datetime.fromtimestamp(1_000, timezone.utc).isoformat().replace("+00:00", "Z")
    paths.health_file.write_text(
        json.dumps(
            {
                "version": "2.5.0",
                "pid": 42,
                "state": "ready",
                "ready": True,
                "timestamp": timestamp,
            }
        ),
        encoding="utf-8",
    )

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "", "")

    manager = updater.UpdateManager(
        paths,
        object(),  # type: ignore[arg-type]
        command_runner=runner,
        clock=lambda: 1_000,
        sleep=lambda _seconds: None,
    )
    assert manager.wait_for_health(updater.Version(2, 5, 0), 999, timeout=1)


def test_activation_rolls_back_after_failed_health_check(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.create_directories()
    old = paths.releases_dir / "2.4.0"
    new = paths.releases_dir / "2.5.0"
    _write_local_manifest(old, "2.4.0")
    _write_local_manifest(new, "2.5.0")
    commands: list[list[str]] = []
    switches: list[tuple[Path, Path]] = []

    def runner(command: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0, "", "")

    manager = updater.UpdateManager(
        paths,
        object(),  # type: ignore[arg-type]
        command_runner=runner,
        link_switcher=lambda target, link: switches.append((target, link)),
        clock=lambda: 100.0,
    )
    results = iter([False, True])
    manager.wait_for_health = lambda *_args, **_kwargs: next(results)  # type: ignore[method-assign]

    with pytest.raises(updater.UpdateError, match="obnovena"):
        manager.activate_release(new, old, updater.Version(2, 5, 0))

    assert switches == [
        (old, paths.previous_link),
        (new, paths.current_link),
        (old, paths.current_link),
    ]
    actions = [command[2] for command in commands]
    assert actions == ["stop", "start", "stop", "start"]


def test_link_switch_failure_restarts_unchanged_old_release(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.create_directories()
    old = paths.releases_dir / "2.4.0"
    new = paths.releases_dir / "2.5.0"
    _write_local_manifest(old, "2.4.0")
    _write_local_manifest(new, "2.5.0")
    commands: list[list[str]] = []
    switches: list[tuple[Path, Path]] = []

    def runner(command: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0, "", "")

    def failing_switcher(target: Path, link: Path) -> None:
        switches.append((target, link))
        if link == paths.current_link:
            raise OSError("read-only filesystem")

    manager = updater.UpdateManager(
        paths,
        object(),  # type: ignore[arg-type]
        command_runner=runner,
        link_switcher=failing_switcher,
        clock=lambda: 100.0,
    )
    manager.wait_for_health = lambda *_args, **_kwargs: True  # type: ignore[method-assign]

    with pytest.raises(updater.UpdateError, match="znovu spustena"):
        manager.activate_release(new, old, updater.Version(2, 5, 0))

    assert switches == [(old, paths.previous_link), (new, paths.current_link)]
    assert [command[2] for command in commands] == ["stop", "start"]


def test_first_activation_failure_stops_bad_service_without_previous_release(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.create_directories()
    new = paths.releases_dir / "2.5.0"
    _write_local_manifest(new, "2.5.0")
    commands: list[list[str]] = []

    def runner(command: list[object], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append([str(part) for part in command])
        return subprocess.CompletedProcess(command, 0, "", "")

    manager = updater.UpdateManager(
        paths,
        object(),  # type: ignore[arg-type]
        command_runner=runner,
        link_switcher=lambda _target, _link: None,
        clock=lambda: 100.0,
    )
    manager.wait_for_health = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

    with pytest.raises(updater.UpdateError, match="neni k dispozici"):
        manager.activate_release(new, None, updater.Version(2, 5, 0))
    assert [command[2] for command in commands] == ["stop", "start", "stop"]


def test_updater_self_replacement_is_checked_and_keeps_backup(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.create_directories()
    release = paths.releases_dir / "2.5.0"
    candidate = release / "linux" / "updater.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("import sys\nprint('2')\n", encoding="utf-8")
    paths.updater_file.write_text("old updater\n", encoding="utf-8")
    manager = updater.UpdateManager(paths, object())  # type: ignore[arg-type]

    manager.replace_updater(release)

    assert paths.updater_file.read_text(encoding="utf-8") == "import sys\nprint('2')\n"
    assert paths.updater_file.with_name("updater.py.previous").read_text(encoding="utf-8") == "old updater\n"


def test_broken_updater_candidate_never_replaces_installed_copy(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.create_directories()
    release = paths.releases_dir / "2.5.0"
    candidate = release / "linux" / "updater.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("raise SystemExit(9)\n", encoding="utf-8")
    paths.updater_file.write_text("known good\n", encoding="utf-8")
    manager = updater.UpdateManager(paths, object())  # type: ignore[arg-type]

    with pytest.raises(updater.UpdateError):
        manager.replace_updater(release)
    assert paths.updater_file.read_text(encoding="utf-8") == "known good\n"


def test_failed_release_cleanup_is_bounded_and_preserves_unrelated_directories(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.create_directories()
    oldest = paths.releases_dir / "2.5.0.failed-20260823T100000Z"
    middle = paths.releases_dir / "2.5.0.failed-20260823T110000Z"
    newest = paths.releases_dir / "2.5.0.failed-20260823T120000Z"
    unrelated = paths.releases_dir / "manual-backup"
    for index, directory in enumerate((oldest, middle, newest), start=1):
        directory.mkdir()
        (directory / "large-placeholder").write_bytes(b"x")
        os.utime(directory, (index, index))
    unrelated.mkdir()

    manager = updater.UpdateManager(paths, object())  # type: ignore[arg-type]
    manager.prune_failed_releases(max_keep=1)

    assert not oldest.exists()
    assert not middle.exists()
    assert newest.is_dir()
    assert unrelated.is_dir()


def test_managed_install_paths_do_not_split_on_ambient_xdg_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/unexpected/config")
    monkeypatch.setenv("XDG_DATA_HOME", "/unexpected/data")
    monkeypatch.delenv("DTR_CONFIG_DIR", raising=False)
    monkeypatch.delenv("DTR_DATA_DIR", raising=False)

    paths = updater.InstallPaths.from_environment()

    assert paths.config_dir == Path.home() / ".config" / "discord-ticket-renamer"
    assert paths.data_dir == Path.home() / ".local" / "share" / "discord-ticket-renamer"


def test_download_failure_never_calls_systemctl(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.config_dir.mkdir(parents=True)
    paths.config_file.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")
    manifest = _manifest()

    class FailingClient:
        def latest(self) -> tuple[None, updater.ReleaseManifest]:
            return None, manifest

        def download(self, *_args: object) -> None:
            raise updater.UpdateError("network failed")

    commands: list[object] = []
    manager = updater.UpdateManager(
        paths,
        FailingClient(),  # type: ignore[arg-type]
        command_runner=lambda command, **_kwargs: commands.append(command),  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(updater.UpdateError, match="network"):
        manager.update()
    assert commands == []
