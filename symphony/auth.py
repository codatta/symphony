from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .config import TrackerConfig


DEFAULT_CREDENTIALS_DIR = ".config/symphony"
DEFAULT_CREDENTIALS_FILE = "credentials.json"


class MissingLinearTokenError(ValueError):
    """Raised when no Linear API token can be resolved."""


@dataclass(frozen=True)
class TokenStore:
    tracker: TrackerConfig
    environ: Mapping[str, str] | None = None
    credentials_path: Path | None = None

    def resolve_linear_token(self) -> str:
        env = self.environ if self.environ is not None else os.environ

        env_token = _non_empty(env.get("LINEAR_API_KEY"))
        if env_token is not None:
            return env_token

        configured = self.tracker.api_key
        if configured is not None:
            if configured.startswith("$"):
                resolved = _non_empty(env.get(configured[1:]))
                if resolved is not None:
                    return resolved
            else:
                resolved = _non_empty(configured)
                if resolved is not None:
                    return resolved

        stored = load_local_linear_token(path=self.credentials_path, environ=env)
        if stored is not None:
            return stored

        raise MissingLinearTokenError("missing_tracker_api_key")


def default_credentials_path(environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    configured_home = _non_empty(env.get("XDG_CONFIG_HOME"))
    if configured_home is not None:
        return Path(configured_home).expanduser() / "symphony" / DEFAULT_CREDENTIALS_FILE
    return Path.home() / DEFAULT_CREDENTIALS_DIR / DEFAULT_CREDENTIALS_FILE


def load_local_linear_token(
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    credentials_path = Path(path).expanduser() if path is not None else default_credentials_path(environ)
    try:
        payload = json.loads(credentials_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(payload, dict):
        return None
    linear = payload.get("linear")
    if not isinstance(linear, dict):
        return None
    token = linear.get("api_key")
    return _non_empty(token) if isinstance(token, str) else None


def save_local_linear_token(
    token: str,
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    resolved = _non_empty(token)
    if resolved is None:
        raise MissingLinearTokenError("missing_tracker_api_key")
    return _save_credentials({"linear": {"api_key": resolved}}, path=path, environ=environ)


def load_local_github_token(
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    credentials_path = Path(path).expanduser() if path is not None else default_credentials_path(environ)
    try:
        payload = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    github = payload.get("github")
    if not isinstance(github, dict):
        return None
    token = github.get("token")
    return _non_empty(token) if isinstance(token, str) else None


def save_local_github_token(
    token: str,
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    resolved = _non_empty(token)
    if resolved is None:
        raise ValueError("empty_github_token")
    return _save_credentials({"github": {"token": resolved}}, path=path, environ=environ)


def _save_credentials(
    updates: dict[str, object],
    *,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    credentials_path = Path(path).expanduser() if path is not None else default_credentials_path(environ)
    credentials_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        credentials_path.parent.chmod(0o700)
    except OSError:
        pass

    try:
        existing: dict[str, object] = json.loads(credentials_path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing = {}

    existing.update(updates)
    credentials_path.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        credentials_path.chmod(0o600)
    except OSError:
        pass
    return credentials_path


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
