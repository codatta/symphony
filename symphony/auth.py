from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from .config import TrackerConfig


class MissingLinearTokenError(ValueError):
    """Raised when no Linear API token can be resolved."""


@dataclass(frozen=True)
class TokenStore:
    tracker: TrackerConfig
    environ: Mapping[str, str] | None = None

    def resolve_linear_token(self) -> str:
        env = self.environ if self.environ is not None else os.environ

        env_token = _non_empty(env.get("LINEAR_API_KEY"))
        if env_token is not None:
            return env_token

        configured = self.tracker.api_key
        if configured is None:
            raise MissingLinearTokenError("missing_tracker_api_key")

        if configured.startswith("$"):
            resolved = _non_empty(env.get(configured[1:]))
            if resolved is not None:
                return resolved
            raise MissingLinearTokenError("missing_tracker_api_key")

        return configured


def redact_secret(text: object, secrets: list[str | None]) -> str:
    redacted = str(text)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None

    trimmed = value.strip()
    return trimmed or None
