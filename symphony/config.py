from __future__ import annotations

from dataclasses import dataclass, field
from os import PathLike
from typing import Any, Mapping


DEFAULT_LINEAR_ENDPOINT = "https://api.linear.app/graphql"
DEFAULT_ACTIVE_STATES = ("Todo", "In Progress")
DEFAULT_TERMINAL_STATES = ("Closed", "Cancelled", "Canceled", "Duplicate", "Done")


class ConfigError(ValueError):
    """Raised when runtime config is missing or unsupported."""


@dataclass(frozen=True)
class TrackerConfig:
    kind: str
    endpoint: str = DEFAULT_LINEAR_ENDPOINT
    api_key: str | None = None
    project_slug: str | None = None
    active_states: tuple[str, ...] = field(default_factory=lambda: DEFAULT_ACTIVE_STATES)
    terminal_states: tuple[str, ...] = field(default_factory=lambda: DEFAULT_TERMINAL_STATES)

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "TrackerConfig":
        tracker = config.get("tracker", {})
        if tracker is None:
            tracker = {}
        if not isinstance(tracker, Mapping):
            raise ConfigError("tracker_config_must_be_map")

        kind = _string_value(tracker.get("kind")) or "linear"
        if kind != "linear":
            raise ConfigError("unsupported_tracker_kind")

        return cls(
            kind=kind,
            endpoint=_string_value(tracker.get("endpoint")) or DEFAULT_LINEAR_ENDPOINT,
            api_key=_string_value(tracker.get("api_key")),
            project_slug=_string_value(tracker.get("project_slug")),
            active_states=_string_tuple(tracker.get("active_states"), DEFAULT_ACTIVE_STATES),
            terminal_states=_string_tuple(tracker.get("terminal_states"), DEFAULT_TERMINAL_STATES),
        )


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    if isinstance(value, PathLike):
        return str(value)
    return None


def _string_tuple(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if not isinstance(value, list):
        raise ConfigError("tracker_states_must_be_list")

    states = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return states if states else default
