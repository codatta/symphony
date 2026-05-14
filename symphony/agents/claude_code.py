from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from symphony.agents.base import (
    AgentEvent,
    AgentEventCallback,
    AgentEventType,
    AgentRunnerError,
    AgentSession,
    CLIAgentRunner,
    TokenUsage,
    TurnResult,
)
from symphony.tracker.models import Issue


DEFAULT_CLAUDE_COMMAND = "claude"
DEFAULT_TURN_TIMEOUT_MS = 3_600_000
DEFAULT_PERMISSION_MODE = "bypassPermissions"

_LINEAR_SYSTEM_PROMPT = """
You have access to Linear via the LINEAR_API_KEY environment variable.
To post comments, update issue state, or attach PR links, use Bash:

  curl -s -X POST https://api.linear.app/graphql \\
    -H "Authorization: $LINEAR_API_KEY" \\
    -H "Content-Type: application/json" \\
    -d '{"query": "YOUR_GRAPHQL_QUERY"}'

Only call Linear when you have meaningful progress to report or need to update state.
""".strip()


@dataclass
class _ClaudeSessionState:
    session_id: str | None = None


class ClaudeCodeRunner(CLIAgentRunner):
    """Claude Code CLI runner using --print --output-format stream-json."""

    name = "claude_code"

    def __init__(
        self,
        command: str = DEFAULT_CLAUDE_COMMAND,
        *,
        model: str | None = None,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        turn_timeout_ms: int = DEFAULT_TURN_TIMEOUT_MS,
        linear_api_key: str | None = None,
    ) -> None:
        super().__init__(command)
        self.model = model
        self.permission_mode = permission_mode
        self.turn_timeout_ms = turn_timeout_ms
        self.linear_api_key = linear_api_key

    async def start_session(
        self,
        workspace: Path,
        *,
        worker_host: str | None = None,
    ) -> AgentSession:
        if worker_host is not None:
            raise AgentRunnerError("remote_claude_worker_not_supported")

        workspace = Path(workspace).expanduser().resolve()
        if not workspace.exists() or not workspace.is_dir():
            raise AgentRunnerError("invalid_workspace_cwd")

        state = _ClaudeSessionState()
        return AgentSession(
            id=f"claude-{workspace.name}",
            workspace=workspace,
            metadata={"claude_state": state},
        )

    async def run_turn(
        self,
        session: AgentSession,
        prompt: str,
        issue: Issue,
        on_event: AgentEventCallback,
    ) -> TurnResult:
        state: _ClaudeSessionState = session.metadata["claude_state"]
        cmd = self._build_command(session.workspace, session_id=state.session_id)
        env = self._build_env()

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(session.workspace),
                env=env,
            )
        except FileNotFoundError as exc:
            raise AgentRunnerError("claude_not_found") from exc
        except OSError as exc:
            raise AgentRunnerError(f"claude_launch_failed:{exc}") from exc

        if process.stdin is None:
            raise AgentRunnerError("claude_stdin_unavailable")

        process.stdin.write(prompt.encode())
        await process.stdin.drain()
        process.stdin.close()

        stderr_task = asyncio.create_task(_drain_stderr(process))
        try:
            result = await asyncio.wait_for(
                self._read_events(process, issue, session, on_event, state),
                timeout=self.turn_timeout_ms / 1000,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return TurnResult(success=False, exit_reason="turn_timeout")
        finally:
            await asyncio.gather(stderr_task, return_exceptions=True)

        return result

    async def stop_session(self, session: AgentSession) -> None:
        pass  # process exits after each --print invocation

    def _build_command(self, workspace: Path, *, session_id: str | None) -> tuple[str, ...]:
        cmd: list[str] = list(self.command)
        cmd += [
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--permission-mode", self.permission_mode,
            "--add-dir", str(workspace),
        ]
        if self.model:
            cmd += ["--model", self.model]
        if session_id:
            cmd += ["--resume", session_id]
        if self.linear_api_key:
            cmd += ["--append-system-prompt", _LINEAR_SYSTEM_PROMPT]
        return tuple(cmd)

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.linear_api_key:
            env["LINEAR_API_KEY"] = self.linear_api_key
        return env

    async def _read_events(
        self,
        process: asyncio.subprocess.Process,
        issue: Issue,
        session: AgentSession,
        on_event: AgentEventCallback,
        state: _ClaudeSessionState,
    ) -> TurnResult:
        if process.stdout is None:
            raise AgentRunnerError("claude_stdout_unavailable")

        usage: TokenUsage | None = None
        session_started = False

        while True:
            line = await process.stdout.readline()
            if not line:
                break

            try:
                event = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            event_type = event.get("type")

            if event_type == "system" and event.get("subtype") == "init":
                sid = event.get("session_id")
                if sid:
                    state.session_id = sid
                if not session_started:
                    session_started = True
                    await on_event(AgentEvent(
                        type=AgentEventType.SESSION_STARTED,
                        issue_id=issue.id,
                        issue_identifier=issue.identifier,
                        session_id=sid or session.id,
                        message="Claude Code session started.",
                        data={"event": "session_started", "session_id": sid},
                    ))

            elif event_type == "assistant":
                text = _extract_text(event.get("message", {}).get("content", []))
                if text:
                    await on_event(AgentEvent(
                        type=AgentEventType.NOTIFICATION,
                        issue_id=issue.id,
                        issue_identifier=issue.identifier,
                        session_id=state.session_id or session.id,
                        message=text[:500],
                        data={"event": "assistant_message"},
                    ))

            elif event_type == "result":
                sid = event.get("session_id")
                if sid:
                    state.session_id = sid
                raw_usage = event.get("usage") or {}
                if raw_usage:
                    usage = TokenUsage.from_input_output(
                        raw_usage.get("input_tokens", 0),
                        raw_usage.get("output_tokens", 0),
                    )
                subtype = event.get("subtype", "")
                is_error = event.get("is_error", False)
                result_text = event.get("result", "")

                if is_error or subtype.startswith("error"):
                    await on_event(AgentEvent(
                        type=AgentEventType.TURN_FAILED,
                        issue_id=issue.id,
                        issue_identifier=issue.identifier,
                        session_id=state.session_id or session.id,
                        message=result_text or subtype or "claude_error",
                        data={"event": "turn_failed", "subtype": subtype},
                    ))
                    return TurnResult(
                        success=False,
                        exit_reason=subtype or "turn_failed",
                        usage=usage,
                    )

                await on_event(AgentEvent(
                    type=AgentEventType.TURN_COMPLETED,
                    issue_id=issue.id,
                    issue_identifier=issue.identifier,
                    session_id=state.session_id or session.id,
                    message=(result_text[:500] if result_text else "Turn completed."),
                    data={"event": "turn_completed", "subtype": subtype},
                ))
                return TurnResult(success=True, exit_reason="turn_completed", usage=usage)

        returncode = await process.wait()
        return TurnResult(
            success=returncode == 0,
            exit_reason=f"claude_exited:{returncode}",
            usage=usage,
        )


def _extract_text(content: list[Any]) -> str:
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    ]
    return " ".join(parts)


async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
    if process.stderr is None:
        return
    try:
        data = await process.stderr.read()
        if data:
            import logging
            logging.getLogger(__name__).debug("claude stderr: %s", data.decode(errors="replace").strip())
    except Exception:
        pass
