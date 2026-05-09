from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from symphony.agents.base import AgentEvent, AgentEventType, AgentSession, TaskResult, TokenUsage, TurnResult
from symphony.config import WorkflowConfig
from symphony.http_server import build_state_snapshot
from symphony.orchestrator import RetryEntry
from symphony.runtime import SymphonyRuntime
from symphony.tracker.models import Issue


def make_config(workspace_root: Path) -> WorkflowConfig:
    return WorkflowConfig.from_mapping(
        {
            "tracker": {
                "kind": "linear",
                "active_states": ["Todo", "In Progress"],
                "terminal_states": ["Done", "Canceled"],
            },
            "workspace": {"root": str(workspace_root)},
            "agent": {"max_concurrent_agents": 2, "max_retry_backoff_ms": 300_000},
            "polling": {"interval_ms": 5_000},
        }
    )


def issue(
    issue_id: str = "issue-1",
    identifier: str = "IN-200",
    *,
    state: str = "Todo",
    priority: int | None = 1,
) -> Issue:
    return Issue(
        id=issue_id,
        identifier=identifier,
        title=f"{identifier} title",
        description="Runtime glue",
        priority=priority,
        state=state,
        branch_name=None,
        url=f"https://linear.app/example/issue/{identifier}",
    )


class ManualClock:
    def __init__(self, now_ms: int = 1_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class FakeTracker:
    def __init__(self, candidates: list[Issue]) -> None:
        self.candidates = candidates
        self.fetch_calls = 0
        self.refresh_calls: list[list[str]] = []

    async def fetch_candidate_issues(self) -> list[Issue]:
        self.fetch_calls += 1
        return list(self.candidates)

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        self.refresh_calls.append(issue_ids)
        by_id = {item.id: item for item in self.candidates}
        return [by_id[issue_id] for issue_id in issue_ids if issue_id in by_id]


@dataclass(frozen=True)
class FakeWorkspace:
    path: Path
    workspace_key: str
    created_now: bool = True


class FakeWorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str]] = []

    async def prepare_for_issue(self, target: Issue) -> FakeWorkspace:
        self.calls.append(("prepare", target.identifier))
        path = self.root / target.identifier
        path.mkdir(parents=True, exist_ok=True)
        return FakeWorkspace(path=path, workspace_key=target.identifier)

    async def before_run(self, workspace: FakeWorkspace) -> None:
        self.calls.append(("before_run", workspace.workspace_key))

    async def after_run(self, workspace: FakeWorkspace) -> None:
        self.calls.append(("after_run", workspace.workspace_key))

    async def cleanup(self, identifier: str) -> bool:
        self.calls.append(("cleanup", identifier))
        return True


class FakeSessionRunner:
    def __init__(self, *, success: bool = True, exit_reason: str = "turn_completed") -> None:
        self.success = success
        self.exit_reason = exit_reason
        self.prompts: list[str] = []
        self.sessions_stopped: list[str] = []
        self.snapshots_during_turn: list[dict] = []
        self.runtime: SymphonyRuntime | None = None

    async def start_session(self, workspace: Path) -> AgentSession:
        return AgentSession(id="session-1", workspace=workspace)

    async def run_turn(self, session: AgentSession, prompt: str, target: Issue, on_event) -> TurnResult:
        self.prompts.append(prompt)
        await on_event(
            AgentEvent(
                type=AgentEventType.SESSION_STARTED,
                message="started",
                issue_id=target.id,
                issue_identifier=target.identifier,
                session_id=session.id,
            )
        )
        if self.runtime is not None:
            self.snapshots_during_turn.append(build_state_snapshot(self.runtime.snapshot()))
        await on_event(
            AgentEvent(
                type=AgentEventType.TURN_COMPLETED if self.success else AgentEventType.TURN_FAILED,
                message=self.exit_reason,
                issue_id=target.id,
                issue_identifier=target.identifier,
                session_id=session.id,
            )
        )
        return TurnResult(
            success=self.success,
            exit_reason=self.exit_reason,
            usage=TokenUsage.from_input_output(10, 5),
        )

    async def stop_session(self, session: AgentSession) -> None:
        self.sessions_stopped.append(session.id)


class RaisingSessionRunner(FakeSessionRunner):
    async def run_turn(self, session: AgentSession, prompt: str, target: Issue, on_event) -> TurnResult:
        self.prompts.append(prompt)
        raise RuntimeError("agent exploded")


class FakeAPIRunner:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_task(self, workspace: Path, prompt: str, target: Issue, on_event) -> TaskResult:
        self.prompts.append(prompt)
        await on_event(
            AgentEvent(
                type=AgentEventType.TASK_COMPLETED,
                message="task completed",
                issue_id=target.id,
                issue_identifier=target.identifier,
            )
        )
        return TaskResult(success=True, exit_reason="task_completed", output_paths=(workspace / "artifact.txt",))


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_tick_dispatches_issue_runs_workspace_and_schedules_continuation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            runner = FakeSessionRunner()
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work on {{ issue.identifier }} attempt={{ attempt }}",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )
            runner.runtime = runtime

            result = await runtime.run_tick()

        self.assertEqual(1, result.fetched)
        self.assertEqual(("IN-200",), result.dispatched)
        self.assertEqual(("IN-200",), result.completed)
        self.assertEqual((), result.failed)
        self.assertEqual(["Work on IN-200 attempt=None"], runner.prompts)
        self.assertEqual(["session-1"], runner.sessions_stopped)
        self.assertNotIn(target.id, runtime.state.running)
        self.assertIn(target.id, runtime.state.retry_attempts)
        retry = runtime.state.retry_attempts[target.id]
        self.assertEqual(1, retry.attempt)
        self.assertIsNone(retry.error)
        self.assertEqual("IN-200", retry.identifier)
        self.assertEqual(1, runner.snapshots_during_turn[0]["counts"]["running"])
        self.assertEqual("session-1", runner.snapshots_during_turn[0]["running"][0]["session_id"])

    async def test_failure_schedules_retry_and_still_stops_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            runner = FakeSessionRunner(success=False, exit_reason="turn_failed")
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work on {{ issue.identifier }}",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(20_000),
            )

            result = await runtime.run_tick()

        self.assertEqual(("IN-200",), result.failed)
        self.assertEqual({"IN-200": "turn_failed"}, result.errors)
        self.assertEqual(["session-1"], runner.sessions_stopped)
        retry = runtime.state.retry_attempts[target.id]
        self.assertEqual(1, retry.attempt)
        self.assertEqual("turn_failed", retry.error)
        self.assertEqual(30_000, retry.due_at_ms)

    async def test_exception_schedules_retry_and_runs_after_run_best_effort(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            workspace_manager = FakeWorkspaceManager(Path(temp_dir) / "workspaces")
            runner = RaisingSessionRunner()
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work on {{ issue.identifier }}",
                tracker=FakeTracker([target]),
                workspace_manager=workspace_manager,
                runner=runner,
                clock_ms=ManualClock(40_000),
            )

            result = await runtime.run_tick()

        self.assertEqual(("IN-200",), result.failed)
        self.assertEqual("agent exploded", result.errors["IN-200"])
        self.assertIn(("after_run", "IN-200"), workspace_manager.calls)
        self.assertEqual(["session-1"], runner.sessions_stopped)
        self.assertEqual("agent exploded", runtime.state.retry_attempts[target.id].error)

    async def test_due_retry_is_redispatched_with_attempt_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue()
            clock = ManualClock(50_000)
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Retry attempt {{ attempt }} for {{ issue.identifier }}",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=clock,
            )
            runtime.state.retry_attempts[target.id] = RetryEntry(
                issue_id=target.id,
                identifier=target.identifier,
                attempt=3,
                due_at_ms=clock.now_ms,
                error="previous failure",
            )

            result = await runtime.run_tick()

        self.assertEqual(("IN-200",), result.dispatched)
        self.assertEqual(["Retry attempt 3 for IN-200"], runtime.runner.prompts)
        self.assertEqual(1, runtime.state.retry_attempts[target.id].attempt)
        self.assertIsNone(runtime.state.retry_attempts[target.id].error)

    async def test_retry_missing_from_candidate_poll_is_released(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Work",
                tracker=FakeTracker([]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=FakeSessionRunner(),
                clock_ms=ManualClock(10_000),
            )
            runtime.state.retry_attempts["issue-1"] = RetryEntry(
                issue_id="issue-1",
                identifier="IN-200",
                attempt=1,
                due_at_ms=10_000,
                error=None,
            )
            runtime.state.claimed.add("issue-1")

            result = await runtime.run_tick()

        self.assertEqual(("IN-200",), result.released)
        self.assertNotIn("issue-1", runtime.state.retry_attempts)
        self.assertNotIn("issue-1", runtime.state.claimed)

    async def test_api_runner_path_uses_run_task_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = issue(identifier="IN-201")
            runner = FakeAPIRunner()
            runtime = SymphonyRuntime(
                config=make_config(Path(temp_dir) / "workspaces"),
                prompt_template="Generate {{ issue.identifier }}",
                tracker=FakeTracker([target]),
                workspace_manager=FakeWorkspaceManager(Path(temp_dir) / "workspaces"),
                runner=runner,
                clock_ms=ManualClock(10_000),
            )

            result = await runtime.run_tick()

        self.assertEqual(("IN-201",), result.completed)
        self.assertEqual(["Generate IN-201"], runner.prompts)
        self.assertEqual(1, len(runtime.state.recent_events))
        self.assertEqual("task_completed", runtime.state.recent_events[0]["event"])


if __name__ == "__main__":
    unittest.main()
