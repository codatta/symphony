import asyncio
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from symphony.cli import StartupError, create_status_api, load_startup_context, main, run_once
from symphony.orchestrator import OrchestratorState


class CLITests(unittest.TestCase):
    def test_help_exits_successfully_without_workflow_file(self):
        with redirect_stdout(StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["--help"])

        self.assertEqual(0, raised.exception.code)

    def test_load_startup_context_validates_workflow_and_resolves_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: $LINEAR_KEY
  project_slug: symphony-ai-agent-orchestration
codex:
  command: codex app-server
---
Work on {{ issue.identifier }}.
""",
                encoding="utf-8",
            )

            context = load_startup_context(
                workflow_path,
                logs_root="runtime-logs",
                port=7337,
                environ={"LINEAR_KEY": "lin_secret"},
            )

            self.assertEqual(workflow_path.resolve(), context.workflow_path)
            self.assertEqual((root / "runtime-logs").resolve(), context.logs_root)
            self.assertEqual(7337, context.port)
            self.assertEqual("Work on {{ issue.identifier }}.", context.workflow.prompt_template)
            self.assertEqual("symphony-ai-agent-orchestration", context.config.tracker.project_slug)

    def test_load_startup_context_rejects_missing_linear_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: $LINEAR_KEY
  project_slug: symphony-ai-agent-orchestration
---
Body
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StartupError, "missing_tracker_api_key"):
                load_startup_context(workflow_path, logs_root="log", port=7337, environ={})

    def test_check_mode_reports_valid_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
---
Body
""",
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                result = main([str(workflow_path), "--check", "--log-level", "WARNING"])

            self.assertEqual(0, result)

    def test_status_api_uses_runtime_snapshot_and_refresh_callback(self):
        class FakeRuntime:
            def __init__(self):
                self.config = None
                self.state = OrchestratorState(
                    poll_interval_ms=30_000,
                    max_concurrent_agents=1,
                    active_states=("Todo",),
                    terminal_states=("Done",),
                )
                self.ticks = 0

            def snapshot(self):
                return self.state

            async def run_tick(self):
                self.ticks += 1
                return {"queued": True, "operations": ["poll"]}

        runtime = FakeRuntime()
        api = create_status_api(runtime)

        health = api.handle_request("GET", "/api/v1/health")
        refresh = asyncio.run(api.async_handle_request("POST", "/api/v1/refresh"))

        self.assertEqual(200, health.status_code)
        self.assertEqual(202, refresh.status_code)
        self.assertEqual(1, runtime.ticks)

    def test_run_once_delegates_to_runtime_tick(self):
        class FakeRuntime:
            def __init__(self):
                self.ticks = 0

            async def run_tick(self):
                self.ticks += 1
                return "tick-result"

        runtime = FakeRuntime()

        result = asyncio.run(run_once(runtime))

        self.assertEqual("tick-result", result)
        self.assertEqual(1, runtime.ticks)


if __name__ == "__main__":
    unittest.main()
