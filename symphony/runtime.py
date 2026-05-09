from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from symphony.agents.base import AgentEvent, AgentEventCallback, TokenUsage
from symphony.config import WorkflowConfig
from symphony.orchestrator import (
    OrchestratorState,
    complete_worker_failure,
    complete_worker_success,
    dispatch_issue,
    reconcile_refreshed_issues,
    release_issue,
    select_dispatchable,
    should_dispatch,
)
from symphony.tracker.models import Issue
from symphony.workflow import WorkflowDefinition, render_prompt


StateCallback = Callable[[OrchestratorState], Any]


@dataclass(frozen=True)
class RuntimeTickResult:
    fetched: int
    dispatched: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    errors: dict[str, str] = field(default_factory=dict)


class SymphonyRuntime:
    """Offline-testable runtime coordinator for one Symphony poll tick."""

    def __init__(
        self,
        *,
        config: WorkflowConfig,
        workflow: WorkflowDefinition | None = None,
        prompt_template: str | None = None,
        tracker: Any,
        workspace_manager: Any,
        runner: Any,
        state: OrchestratorState | None = None,
        clock_ms: Callable[[], int] | None = None,
        on_event: AgentEventCallback | None = None,
        on_state_change: StateCallback | None = None,
    ) -> None:
        self.config = config
        self.workflow = workflow
        self.prompt_template = prompt_template if prompt_template is not None else (
            workflow.prompt_template if workflow is not None else ""
        )
        self.tracker = tracker
        self.workspace_manager = workspace_manager
        self.runner = runner
        self.state = state or OrchestratorState.from_config(config)
        self.clock_ms = clock_ms or _monotonic_epoch_ms
        self.on_event = on_event
        self.on_state_change = on_state_change

    async def run_tick(self) -> RuntimeTickResult:
        """Poll Linear once, dispatch eligible issues, and wait for started workers."""

        now_ms = self.clock_ms()
        await self.reconcile_running(now_ms=now_ms)
        candidates = list(await _maybe_await(self.tracker.fetch_candidate_issues()))

        released = self._release_due_retries_missing_from_candidates(candidates)
        dispatched_issues = self._dispatch_due_retries(candidates, now_ms=now_ms)

        remaining = [issue for issue in candidates if issue.id not in {item.id for item in dispatched_issues}]
        for issue in select_dispatchable(remaining, self.state):
            dispatch_issue(issue, self.state, now_ms=now_ms)
            dispatched_issues.append(issue)

        completed: list[str] = []
        failed: list[str] = []
        errors: dict[str, str] = {}
        worker_results = await asyncio.gather(
            *(self._run_dispatched_issue(issue) for issue in dispatched_issues),
        )
        for issue, result in zip(dispatched_issues, worker_results, strict=True):
            if result.success:
                completed.append(issue.identifier)
            else:
                failed.append(issue.identifier)
                errors[issue.identifier] = result.error or "worker_failed"

        self._notify_state_change()
        return RuntimeTickResult(
            fetched=len(candidates),
            dispatched=tuple(issue.identifier for issue in dispatched_issues),
            completed=tuple(completed),
            failed=tuple(failed),
            released=tuple(released),
            errors=errors,
        )

    async def run_issue(self, issue: Issue, *, attempt: int | None = None) -> "_WorkerResult":
        """Dispatch and run a single issue immediately."""

        dispatch_issue(issue, self.state, now_ms=self.clock_ms(), attempt=attempt)
        result = await self._run_dispatched_issue(issue)
        self._notify_state_change()
        return result

    async def reconcile_running(self, *, now_ms: int | None = None) -> None:
        if not self.state.running:
            return

        issue_ids = list(self.state.running)
        refreshed = list(await _maybe_await(self.tracker.fetch_issue_states_by_ids(issue_ids)))
        actions = reconcile_refreshed_issues(refreshed, self.state)
        for action in actions:
            if action.cleanup_workspace:
                await _maybe_await(self.workspace_manager.cleanup(action.identifier))
        self._notify_state_change()

    def snapshot(self) -> OrchestratorState:
        return self.state

    def _release_due_retries_missing_from_candidates(self, candidates: list[Issue]) -> list[str]:
        candidate_ids = {issue.id for issue in candidates}
        released: list[str] = []
        for retry in list(self.state.retry_attempts.values()):
            if retry.issue_id in candidate_ids:
                continue
            release_issue(retry.issue_id, self.state)
            released.append(retry.identifier)
        return released

    def _dispatch_due_retries(self, candidates: list[Issue], *, now_ms: int) -> list[Issue]:
        by_id = {issue.id: issue for issue in candidates}
        dispatched: list[Issue] = []
        due_retries = sorted(
            (
                retry
                for retry in self.state.retry_attempts.values()
                if retry.due_at_ms <= now_ms and retry.issue_id in by_id
            ),
            key=lambda retry: (retry.due_at_ms, retry.identifier),
        )

        for retry in due_retries:
            issue = by_id[retry.issue_id]
            if not should_dispatch(issue, self.state, allow_claimed_retry=True):
                continue
            dispatch_issue(issue, self.state, now_ms=now_ms, attempt=retry.attempt)
            dispatched.append(issue)

        return dispatched

    async def _run_dispatched_issue(self, issue: Issue) -> "_WorkerResult":
        entry = self.state.running[issue.id]
        workspace = None
        session = None

        try:
            workspace = await _maybe_await(self.workspace_manager.prepare_for_issue(issue))
            _attach_runtime_entry_metadata(entry, workspace_path=getattr(workspace, "path", None))
            await _maybe_await(self.workspace_manager.before_run(workspace))

            prompt = render_prompt(self.prompt_template, issue=issue, attempt=entry.retry_attempt)
            if _is_api_runner(self.runner):
                result = await self.runner.run_task(Path(workspace.path), prompt, issue, self._agent_event_handler)
            else:
                session = await self.runner.start_session(Path(workspace.path))
                entry.session_id = session.id
                result = await self.runner.run_turn(session, prompt, issue, self._agent_event_handler)

            _apply_usage(entry, getattr(result, "usage", None))
            await _maybe_await(self.workspace_manager.after_run(workspace))

            if result.success:
                complete_worker_success(issue.id, self.state, now_ms=self.clock_ms())
                return _WorkerResult(success=True)

            error = str(result.exit_reason or "worker_failed")
            complete_worker_failure(
                issue.id,
                self.state,
                now_ms=self.clock_ms(),
                max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                error=error,
            )
            return _WorkerResult(success=False, error=error)
        except Exception as exc:  # noqa: BLE001 - runtime must convert worker failures into retry state.
            error = str(exc) or exc.__class__.__name__
            if workspace is not None:
                await _best_effort_after_run(self.workspace_manager, workspace)
            if issue.id in self.state.running:
                complete_worker_failure(
                    issue.id,
                    self.state,
                    now_ms=self.clock_ms(),
                    max_retry_backoff_ms=self.config.agent.max_retry_backoff_ms,
                    error=error,
                )
            return _WorkerResult(success=False, error=error)
        finally:
            if session is not None and hasattr(self.runner, "stop_session"):
                await _maybe_await(self.runner.stop_session(session))
            self._notify_state_change()

    async def _agent_event_handler(self, event: AgentEvent) -> None:
        issue_id = event.issue_id
        if issue_id is not None and issue_id in self.state.running:
            entry = self.state.running[issue_id]
            entry.last_event_at_ms = self.clock_ms()
            entry.last_event = event.type.value
            entry.last_message = event.message
            if event.session_id is not None:
                entry.session_id = event.session_id
            if event.type.value in {"turn_completed", "turn_failed", "task_completed", "task_failed"}:
                entry.turn_count = getattr(entry, "turn_count", 0) + 1
            _append_recent_event(entry, event)

        _append_recent_event(self.state, event)
        if self.on_event is not None:
            await self.on_event(event)
        self._notify_state_change()

    def _notify_state_change(self) -> None:
        if self.on_state_change is not None:
            self.on_state_change(self.state)


@dataclass(frozen=True)
class _WorkerResult:
    success: bool
    error: str | None = None


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _best_effort_after_run(workspace_manager: Any, workspace: Any) -> None:
    try:
        await _maybe_await(workspace_manager.after_run(workspace))
    except Exception:
        return


def _is_api_runner(runner: Any) -> bool:
    return hasattr(runner, "run_task") and not hasattr(runner, "start_session")


def _attach_runtime_entry_metadata(entry: Any, *, workspace_path: Any) -> None:
    if workspace_path is not None:
        entry.workspace_path = Path(workspace_path)
    if not hasattr(entry, "turn_count"):
        entry.turn_count = 0
    if not hasattr(entry, "recent_events"):
        entry.recent_events = []


def _append_recent_event(target: Any, event: AgentEvent) -> None:
    if not hasattr(target, "recent_events"):
        target.recent_events = []
    target.recent_events.append(
        {
            "event": event.type.value,
            "message": event.message,
            "issue_identifier": event.issue_identifier,
            "session_id": event.session_id,
        }
    )
    if len(target.recent_events) > 50:
        del target.recent_events[:-50]


def _apply_usage(entry: Any, usage: TokenUsage | None) -> None:
    if usage is None:
        return
    entry.input_tokens += usage.input_tokens
    entry.output_tokens += usage.output_tokens
    entry.total_tokens += usage.total_tokens


def _monotonic_epoch_ms() -> int:
    return int(time.time() * 1000)
