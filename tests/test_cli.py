import asyncio
import urllib.request
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from symphony.cli import (
    RuntimeWorkflowReloader,
    StartupError,
    create_runtime,
    create_status_api,
    create_status_http_server,
    load_startup_context,
    main,
    run_once,
)
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

    def test_status_http_server_serves_health_endpoint(self):
        async def exercise():
            state = OrchestratorState(
                poll_interval_ms=30_000,
                max_concurrent_agents=1,
                active_states=("Todo",),
                terminal_states=("Done",),
            )
            api = create_status_api(type("Runtime", (), {"snapshot": lambda _self: state, "run_tick": lambda _self: None})())
            server = create_status_http_server(api, 0, loop=asyncio.get_running_loop())
            task = asyncio.create_task(asyncio.to_thread(server.serve_forever, 0.05))
            try:
                port = server.server_address[1]

                def fetch():
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2) as response:
                        return response.status, response.read()

                return await asyncio.to_thread(fetch)
            finally:
                server.shutdown()
                server.server_close()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        status, body = asyncio.run(exercise())

        self.assertEqual(200, status)
        self.assertIn(b'"status":"ok"', body)

    def test_runtime_workflow_reloader_applies_changed_config_and_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workflow_path = root / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
polling:
  interval_ms: 5000
workspace:
  root: workspaces-a
---
First prompt {{ issue.identifier }}.
""",
                encoding="utf-8",
            )
            context = load_startup_context(workflow_path, logs_root="log", port=7337)
            runtime = create_runtime(context)
            reloader = RuntimeWorkflowReloader.from_context(runtime, context)

            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
  active_states: [Reviewing]
polling:
  interval_ms: 1234
workspace:
  root: workspaces-b
codex:
  command: codex app-server --profile changed
---
Changed prompt {{ issue.identifier }}.
""",
                encoding="utf-8",
            )

            changed = reloader.reload_now()

            self.assertTrue(changed)
            self.assertEqual("Changed prompt {{ issue.identifier }}.", runtime.prompt_template)
            self.assertEqual(1234, runtime.state.poll_interval_ms)
            self.assertEqual(("Reviewing",), runtime.state.active_states)
            self.assertEqual((root / "workspaces-b").resolve(), runtime.config.workspace.root)

    def test_runtime_workflow_reloader_keeps_last_good_config_on_invalid_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: literal-token
  project_slug: symphony-ai-agent-orchestration
polling:
  interval_ms: 5000
---
First prompt.
""",
                encoding="utf-8",
            )
            context = load_startup_context(workflow_path, logs_root="log", port=7337)
            runtime = create_runtime(context)
            reloader = RuntimeWorkflowReloader.from_context(runtime, context, environ={})

            workflow_path.write_text(
                """---
tracker:
  kind: linear
  api_key: $MISSING_LINEAR_KEY
  project_slug: symphony-ai-agent-orchestration
polling:
  interval_ms: 1234
---
Invalid prompt.
""",
                encoding="utf-8",
            )

            changed = reloader.reload_now()

            self.assertFalse(changed)
            self.assertEqual("First prompt.", runtime.prompt_template)
            self.assertEqual(5000, runtime.state.poll_interval_ms)
            self.assertIsNotNone(reloader.reloader.last_error)

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
