import os
import shlex
import stat
import tempfile
import unittest
from pathlib import Path

from symphony.config import HooksConfig, WorkspaceConfig
from symphony.tracker.models import Issue
from symphony.workspace import (
    WorkspaceError,
    WorkspaceHookError,
    WorkspaceManager,
    is_path_within_root,
    sanitize_workspace_key,
)


def make_issue(identifier: str = "IN-42") -> Issue:
    return Issue(
        id="issue-id",
        identifier=identifier,
        title="Test issue",
        description=None,
        priority=None,
        state="In Progress",
        branch_name=None,
        url=None,
    )


class WorkspaceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_created_with_sanitized_path_and_owner_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "workspaces"))

            workspace = await manager.prepare_for_issue(make_issue("IN-42 add-retry"))

            self.assertEqual("IN-42_add-retry", workspace.workspace_key)
            self.assertEqual((Path(temp_dir) / "workspaces" / "IN-42_add-retry").resolve(), workspace.path)
            self.assertTrue(workspace.created_now)
            self.assertTrue(workspace.path.is_dir())
            self.assertEqual(stat.S_IRWXU, stat.S_IMODE(workspace.path.stat().st_mode))
            self.assertEqual(stat.S_IRWXU, stat.S_IMODE(workspace.path.parent.stat().st_mode))

    async def test_existing_workspace_is_reused_without_after_create(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "events.log"
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(after_create=f"printf after_create >> {shlex.quote(str(log_path))}"),
            )

            first = await manager.prepare("IN-42")
            second = await manager.prepare("IN-42")

            self.assertTrue(first.created_now)
            self.assertFalse(second.created_now)
            self.assertEqual(first.path, second.path)
            self.assertEqual("after_create", log_path.read_text(encoding="utf-8"))

    async def test_hooks_run_in_lifecycle_order_and_cleanup_removes_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "events.log"
            log = shlex.quote(str(log_path))
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(
                    after_create=f"printf 'after_create\\n' >> {log}",
                    before_run=f"printf 'before_run\\n' >> {log}",
                    after_run=f"printf 'after_run\\n' >> {log}",
                    before_remove=f"printf 'before_remove\\n' >> {log}",
                ),
            )

            workspace = await manager.prepare("IN-42")
            await manager.before_run(workspace)
            await manager.after_run(workspace)
            removed = await manager.cleanup("IN-42")

            self.assertTrue(removed)
            self.assertFalse(workspace.path.exists())
            self.assertEqual(
                ["after_create", "before_run", "after_run", "before_remove"],
                log_path.read_text(encoding="utf-8").splitlines(),
            )

    async def test_blocking_hook_failure_aborts_progression(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(after_create="exit 7"),
            )

            with self.assertRaisesRegex(WorkspaceHookError, "workspace_hook_failed:after_create:7"):
                await manager.prepare("IN-42")

            self.assertFalse((Path(temp_dir) / "workspaces" / "IN-42").exists())

    async def test_nonblocking_hooks_are_best_effort(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(after_run="exit 2", before_remove="exit 3"),
            )

            workspace = await manager.prepare("IN-42")

            with self.assertLogs("symphony.workspace", level="WARNING") as logs:
                self.assertIsNone(await manager.after_run(workspace))
                self.assertTrue(await manager.cleanup("IN-42"))

            self.assertFalse(workspace.path.exists())
            self.assertIn("Workspace hook after_run failed", logs.output[0])
            self.assertIn("Workspace hook before_remove failed", logs.output[1])

    async def test_hook_timeout_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(before_run="sleep 2", timeout_ms=50),
            )

            workspace = await manager.prepare("IN-42")

            with self.assertRaisesRegex(WorkspaceHookError, "workspace_hook_timeout:before_run"):
                await manager.before_run(workspace)

    async def test_cleanup_can_keep_failed_workspace_for_debugging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(WorkspaceConfig(Path(temp_dir) / "workspaces"))
            workspace = await manager.prepare("IN-42")

            removed = await manager.cleanup("IN-42", failed=True, keep_on_failure=True)

            self.assertFalse(removed)
            self.assertTrue(workspace.path.exists())

    async def test_non_directory_at_workspace_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspaces"
            root.mkdir()
            (root / "IN-42").write_text("not a directory", encoding="utf-8")
            manager = WorkspaceManager(WorkspaceConfig(root))

            with self.assertRaisesRegex(WorkspaceError, "workspace_path_exists_not_directory"):
                await manager.prepare("IN-42")

    async def test_workspace_path_validation_rejects_out_of_root_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspaces"
            outside = Path(temp_dir) / "outside"
            outside.mkdir()
            manager = WorkspaceManager(WorkspaceConfig(root))

            with self.assertRaisesRegex(WorkspaceError, "workspace_path_outside_root"):
                manager.validate_workspace(outside)

    def test_identifier_sanitization_and_root_containment_helpers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "workspaces"
            sanitized = sanitize_workspace_key("../../IN-42 add/retry")

            self.assertEqual(".._.._IN-42_add_retry", sanitized)
            self.assertTrue(is_path_within_root(root / sanitized, root))
            self.assertFalse(is_path_within_root(Path(temp_dir) / "elsewhere", root))

            with self.assertRaisesRegex(WorkspaceError, "workspace_identifier_required"):
                sanitize_workspace_key("  ")


class WorkspaceHookEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_hooks_run_in_workspace_with_configured_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = WorkspaceManager(
                WorkspaceConfig(Path(temp_dir) / "workspaces"),
                HooksConfig(before_run="printf \"$SYMPHONY_MARKER\" > marker.txt"),
                environ={"SYMPHONY_MARKER": "from-hook"},
            )

            workspace = await manager.prepare("IN-42")
            await manager.before_run(workspace)

            self.assertEqual("from-hook", (workspace.path / "marker.txt").read_text(encoding="utf-8"))
            self.assertNotIn("SYMPHONY_MARKER", os.environ)
