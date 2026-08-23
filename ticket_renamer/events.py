from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable


class EventLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class AppEvent:
    level: EventLevel
    title: str
    message: str
    status: str | None = None
    notify: bool = True


EventReporter = Callable[[AppEvent], None]

