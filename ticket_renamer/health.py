"""Small, secret-free runtime health marker used by systemd updates."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone

from . import __version__


HEALTH_FILE_NAME = "health.json"
HEALTH_DISABLE_ENV = "DTR_DISABLE_HEALTH"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _health_disabled(environment: Mapping[str, str]) -> bool:
    return environment.get(HEALTH_DISABLE_ENV, "").strip().casefold() in _TRUE_VALUES


def health_file_from_environment(
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    source = os.environ if environment is None else environment
    if _health_disabled(source):
        return None
    configured = source.get("DTR_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured) / HEALTH_FILE_NAME

    runtime = source.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        return Path(runtime) / "discord-ticket-renamer" / HEALTH_FILE_NAME
    return None


def write_health_marker(
    *,
    ready: bool,
    state: str,
    path: Path | None = None,
    version: str = __version__,
    pid: int | None = None,
    updated_at: float | None = None,
) -> Path | None:
    """Atomically write a minimal marker, or do nothing outside managed Linux."""

    if _health_disabled(os.environ):
        return None
    destination = path or health_file_from_environment()
    if destination is None:
        return None

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker_time = time.time() if updated_at is None else float(updated_at)
    payload = {
        "version": version,
        "pid": os.getpid() if pid is None else pid,
        "state": str(state),
        "ready": bool(ready),
        "updated_at": marker_time,
        "timestamp": datetime.fromtimestamp(marker_time, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".health-",
        suffix=".tmp",
        dir=destination.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name == "posix":
            destination.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination
