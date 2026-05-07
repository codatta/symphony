import unittest

from symphony.auth import MissingLinearTokenError, TokenStore
from symphony.config import TrackerConfig
from symphony.workflow import WorkflowError, parse_workflow


class WorkflowConfigTests(unittest.TestCase):
    def test_parse_workflow_front_matter_and_prompt(self):
        workflow = parse_workflow(
            """---
tracker:
  kind: linear
  api_key: $WORKFLOW_LINEAR_KEY
  project_slug: symphony-ai-agent-orchestration
---

You are working on {{ issue.identifier }}.
"""
        )

        self.assertEqual("symphony-ai-agent-orchestration", workflow.config["tracker"]["project_slug"])
        self.assertEqual("You are working on {{ issue.identifier }}.", workflow.prompt_template)

    def test_non_map_front_matter_is_rejected(self):
        with self.assertRaisesRegex(WorkflowError, "workflow_front_matter_must_be_map"):
            parse_workflow("---\n- not\n- a\n- map\n---\nbody")

    def test_tracker_config_defaults_linear_fields(self):
        config = TrackerConfig.from_mapping({"tracker": {"kind": "linear"}})

        self.assertEqual("https://api.linear.app/graphql", config.endpoint)
        self.assertEqual(("Todo", "In Progress"), config.active_states)
        self.assertEqual(("Closed", "Cancelled", "Canceled", "Duplicate", "Done"), config.terminal_states)

    def test_token_resolution_env_wins_over_workflow(self):
        config = TrackerConfig.from_mapping({"tracker": {"api_key": "workflow-token"}})
        token = TokenStore(config, environ={"LINEAR_API_KEY": "env-token"}).resolve_linear_token()

        self.assertEqual("env-token", token)

    def test_token_resolution_supports_workflow_env_reference(self):
        config = TrackerConfig.from_mapping({"tracker": {"api_key": "$WORKFLOW_LINEAR_KEY"}})
        token = TokenStore(config, environ={"WORKFLOW_LINEAR_KEY": "referenced-token"}).resolve_linear_token()

        self.assertEqual("referenced-token", token)

    def test_empty_token_is_missing(self):
        config = TrackerConfig.from_mapping({"tracker": {"api_key": "$WORKFLOW_LINEAR_KEY"}})

        with self.assertRaisesRegex(MissingLinearTokenError, "missing_tracker_api_key"):
            TokenStore(config, environ={"WORKFLOW_LINEAR_KEY": "  "}).resolve_linear_token()


if __name__ == "__main__":
    unittest.main()
