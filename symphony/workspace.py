from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from symphony.config import HooksConfig, WorkspaceConfig
from symphony.tracker.models import Issue


LOGGER = logging.getLogger(__name__)
HOOK_OUTPUT_LIMIT = 4_000
WORKSPACE_MODE = stat.S_IRWXU
_UNSAFE_WORKSPACE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceError(ValueError):
    """Raised when a workspace path or lifecycle operation is invalid."""


class WorkspaceHookError(RuntimeError):
    """Raised when a blocking workspace lifecycle hook fails."""


@dataclass(frozen=True)
class Workspace:
    path: Path
    workspace_key: str
    created_now: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).expanduser().resolve())


@dataclass(frozen=True)
class WorkspaceHookResult:
    name: str
    command: str
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class WorkspaceManager:
    workspace: WorkspaceConfig
    hooks: HooksConfig = HooksConfig()
    environ: Mapping[str, str] | None = None

    async def prepare_for_issue(self, issue: Issue) -> Workspace:
        return await self.prepare(issue.identifier)

    async def prepare(self, issue_identifier: str) -> Workspace:
        workspace_key = sanitize_workspace_key(issue_identifier)
        root = self._ensure_root()
        workspace_path = self.workspace_path(workspace_key)

        if not is_path_within_root(workspace_path, root):
            raise WorkspaceError("workspace_path_outside_root")
        if workspace_path.exists() and not workspace_path.is_dir():
            raise WorkspaceError("workspace_path_exists_not_directory")

        created_now = not workspace_path.exists()
        if created_now:
            workspace_path.mkdir(mode=WORKSPACE_MODE)
            _restrict_owner_permissions(workspace_path)

        handle = Workspace(path=workspace_path, workspace_key=workspace_key, created_now=created_now)
        if created_now:
            try:
                await self.run_hook("after_create", handle)
            except Exception:
                shutil.rmtree(workspace_path, ignore_errors=True)
                raise

        return handle

    async def before_run(self, workspace: Workspace) -> WorkspaceHookResult | None:
        self.validate_workspace(workspace.path)
        return await self.run_hook("before_run", workspace)

    async def after_run(self, workspace: Workspace) -> WorkspaceHookResult | None:
        self.validate_workspace(workspace.path)
        return await self.run_hook("after_run", workspace, best_effort=True)

    async def cleanup_for_issue(
        self,
        issue: Issue,
        *,
        failed: bool = False,
        keep_on_failure: bool = False,
    ) -> bool:
        return await self.cleanup(issue.identifier, failed=failed, keep_on_failure=keep_on_failure)

    async def cleanup(
        self,
        issue_identifier: str,
        *,
        failed: bool = False,
        keep_on_failure: bool = False,
    ) -> bool:
        if failed and keep_on_failure:
            return False

        workspace_key = sanitize_workspace_key(issue_identifier)
        workspace_path = self.workspace_path(workspace_key)
        root = self.workspace.root.expanduser().resolve()
        if not is_path_within_root(workspace_path, root):
            raise WorkspaceError("workspace_path_outside_root")
        if not workspace_path.exists():
            return False
        if not workspace_path.is_dir():
            raise WorkspaceError("workspace_path_exists_not_directory")

        handle = Workspace(path=workspace_path, workspace_key=workspace_key, created_now=False)
        await self.run_hook("before_remove", handle, best_effort=True)
        shutil.rmtree(workspace_path)
        return True

    async def run_hook(
        self,
        hook_name: str,
        workspace: Workspace,
        *,
        best_effort: bool = False,
    ) -> WorkspaceHookResult | None:
        command = getattr(self.hooks, hook_name)
        if command is None:
            return None

        try:
            return await _run_shell_hook(
                hook_name,
                command,
                workspace.path,
                timeout_ms=self.hooks.timeout_ms,
                environ=self.environ,
            )
        except WorkspaceHookError:
            if not best_effort:
                raise
            LOGGER.warning("Workspace hook %s failed; continuing", hook_name, exc_info=True)
            return None

    def workspace_path(self, workspace_key: str) -> Path:
        path = (self.workspace.root.expanduser().resolve() / workspace_key).resolve()
        if not is_path_within_root(path, self.workspace.root.expanduser().resolve()):
            raise WorkspaceError("workspace_path_outside_root")
        return path

    def validate_workspace(self, path: str | Path) -> Path:
        root = self.workspace.root.expanduser().resolve()
        workspace_path = Path(path).expanduser().resolve()
        if not is_path_within_root(workspace_path, root):
            raise WorkspaceError("workspace_path_outside_root")
        if not workspace_path.is_dir():
            raise WorkspaceError("workspace_path_missing")
        return workspace_path

    def _ensure_root(self) -> Path:
        root = self.workspace.root.expanduser().resolve()
        if root.exists() and not root.is_dir():
            raise WorkspaceError("workspace_root_exists_not_directory")
        root.mkdir(mode=WORKSPACE_MODE, parents=True, exist_ok=True)
        _restrict_owner_permissions(root)
        return root


def sanitize_workspace_key(issue_identifier: str) -> str:
    candidate = _UNSAFE_WORKSPACE_CHARS.sub("_", issue_identifier.strip()).strip("_")
    if candidate in {"", ".", ".."}:
        raise WorkspaceError("workspace_identifier_required")
    return candidate


def is_path_within_root(path: str | Path, root: str | Path) -> bool:
    workspace_path = Path(path).expanduser().resolve()
    workspace_root = Path(root).expanduser().resolve()
    return workspace_path == workspace_root or workspace_root in workspace_path.parents


async def _run_shell_hook(
    hook_name: str,
    command: str,
    cwd: Path,
    *,
    timeout_ms: int,
    environ: Mapping[str, str] | None,
) -> WorkspaceHookResult:
    env = os.environ.copy()
    if environ is not None:
        env.update(environ)

    process = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise WorkspaceHookError(f"workspace_hook_timeout:{hook_name}") from exc

    stdout = _decode_and_truncate(stdout_bytes)
    stderr = _decode_and_truncate(stderr_bytes)
    exit_code = process.returncode if process.returncode is not None else -1
    result = WorkspaceHookResult(
        name=hook_name,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )
    if exit_code != 0:
        raise WorkspaceHookError(f"workspace_hook_failed:{hook_name}:{exit_code}")
    return result


def _decode_and_truncate(value: bytes) -> str:
    decoded = value.decode("utf-8", errors="replace")
    if len(decoded) <= HOOK_OUTPUT_LIMIT:
        return decoded
    return decoded[:HOOK_OUTPUT_LIMIT] + "\n[truncated]"


def _restrict_owner_permissions(path: Path) -> None:
    path.chmod(WORKSPACE_MODE)
