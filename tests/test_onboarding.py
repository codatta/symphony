import os
import stat
import tempfile
import unittest
from pathlib import Path

from symphony.auth import load_local_linear_token, save_local_linear_token
from symphony.onboarding import InitConfig, default_workspace_root, generate_workflow, write_workflow
from symphony.workflow import parse_workflow


class OnboardingTests(unittest.TestCase):
    def test_generate_workflow_uses_preset_and_parseable_front_matter(self):
        content = generate_workflow(
            InitConfig(
                project_slug="symphony-ai-agent-orchestration",
                preset="codex-safe",
                workspace_root="~/.symphony/workspaces/symphony",
            )
        )

        workflow = parse_workflow(content)

        self.assertEqual("symphony-ai-agent-orchestration", workflow.config["tracker"]["project_slug"])
        self.assertEqual(1, workflow.config["agent"]["max_concurrent_agents"])
        self.assertEqual("never", workflow.config["codex"]["approval_policy"])
        self.assertIn("{{ issue.identifier }}", workflow.prompt_template)

    def test_default_workspace_root_sanitizes_project_slug(self):
        self.assertEqual("~/.symphony/workspaces/A-B-C.1", default_workspace_root(" A/B C.1 "))

    def test_write_workflow_refuses_to_overwrite_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workflow_path = Path(temp_dir) / "WORKFLOW.md"
            workflow_path.write_text("existing", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "workflow_file_exists"):
                write_workflow(workflow_path, "new")

    def test_local_linear_credentials_round_trip_with_private_file_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "credentials.json"

            saved = save_local_linear_token("lin_secret", path=path)

            self.assertEqual(path, saved)
            self.assertEqual("lin_secret", load_local_linear_token(path=path))
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
