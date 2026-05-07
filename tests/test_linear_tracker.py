import unittest

from symphony.auth import TokenStore
from symphony.config import TrackerConfig
from symphony.tracker.linear import (
    CANDIDATE_ISSUES_QUERY,
    ISSUES_BY_ID_QUERY,
    GraphQLResponse,
    LinearAPIStatusError,
    LinearClient,
    LinearGraphQLError,
    LinearMissingEndCursorError,
    normalize_issue,
)


def issue_payload(issue_id="issue-1", identifier="IN-1", state="Todo", priority=1):
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": "Build the thing",
        "description": "Do useful work",
        "priority": priority,
        "state": {"name": state},
        "branchName": "feature/in-1",
        "url": "https://linear.app/example/issue/IN-1",
        "labels": {"nodes": [{"name": "Backend"}, {"name": "MVP"}]},
        "inverseRelations": {
            "nodes": [
                {
                    "type": "blocks",
                    "issue": {
                        "id": "blocker-1",
                        "identifier": "IN-0",
                        "state": {"name": "Done"},
                    },
                },
                {
                    "type": "relates",
                    "issue": {
                        "id": "related-1",
                        "identifier": "IN-9",
                        "state": {"name": "Todo"},
                    },
                },
            ]
        },
        "createdAt": "2026-05-07T01:02:03.000Z",
        "updatedAt": "2026-05-07T04:05:06.000Z",
    }


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


class LinearTrackerTests(unittest.TestCase):
    def client(self, transport):
        tracker = TrackerConfig.from_mapping(
            {
                "tracker": {
                    "kind": "linear",
                    "project_slug": "symphony-ai-agent-orchestration",
                    "active_states": ["Todo", "In Progress"],
                    "api_key": "$LINEAR_KEY",
                }
            }
        )
        return LinearClient(tracker, token_store=TokenStore(tracker, environ={"LINEAR_KEY": "lin_secret"}), transport=transport)

    def test_normalize_issue_payload(self):
        issue = normalize_issue(issue_payload(priority="not-an-int"))

        self.assertEqual("issue-1", issue.id)
        self.assertEqual("IN-1", issue.identifier)
        self.assertEqual("Todo", issue.state)
        self.assertIsNone(issue.priority)
        self.assertEqual(("backend", "mvp"), issue.labels)
        self.assertEqual(1, len(issue.blocked_by))
        self.assertEqual("IN-0", issue.blocked_by[0].identifier)
        self.assertEqual(2026, issue.created_at.year)

    def test_fetch_candidate_issues_paginates_and_preserves_order(self):
        transport = RecordingTransport(
            [
                GraphQLResponse(
                    200,
                    {
                        "data": {
                            "issues": {
                                "nodes": [issue_payload("issue-1", "IN-1")],
                                "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                            }
                        }
                    },
                ),
                GraphQLResponse(
                    200,
                    {
                        "data": {
                            "issues": {
                                "nodes": [issue_payload("issue-2", "IN-2")],
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                            }
                        }
                    },
                ),
            ]
        )

        issues = self.client(transport).fetch_candidate_issues()

        self.assertEqual(["IN-1", "IN-2"], [issue.identifier for issue in issues])
        self.assertIn("slugId", transport.calls[0]["payload"]["query"])
        self.assertEqual(CANDIDATE_ISSUES_QUERY, transport.calls[0]["payload"]["query"])
        self.assertEqual("symphony-ai-agent-orchestration", transport.calls[0]["payload"]["variables"]["projectSlug"])
        self.assertEqual(["Todo", "In Progress"], transport.calls[0]["payload"]["variables"]["stateNames"])
        self.assertIsNone(transport.calls[0]["payload"]["variables"]["after"])
        self.assertEqual("cursor-1", transport.calls[1]["payload"]["variables"]["after"])
        self.assertEqual("lin_secret", transport.calls[0]["headers"]["Authorization"])

    def test_fetch_issues_by_empty_states_skips_api_call(self):
        transport = RecordingTransport([])

        issues = self.client(transport).fetch_issues_by_states([])

        self.assertEqual([], issues)
        self.assertEqual([], transport.calls)

    def test_missing_end_cursor_is_pagination_error(self):
        transport = RecordingTransport(
            [
                GraphQLResponse(
                    200,
                    {
                        "data": {
                            "issues": {
                                "nodes": [],
                                "pageInfo": {"hasNextPage": True, "endCursor": None},
                            }
                        }
                    },
                )
            ]
        )

        with self.assertRaisesRegex(LinearMissingEndCursorError, "linear_missing_end_cursor"):
            self.client(transport).fetch_candidate_issues()

    def test_fetch_issue_states_by_ids_uses_graphql_id_query_and_requested_order(self):
        transport = RecordingTransport(
            [
                GraphQLResponse(
                    200,
                    {
                        "data": {
                            "issues": {
                                "nodes": [
                                    issue_payload("issue-2", "IN-2", "Done"),
                                    issue_payload("issue-1", "IN-1", "In Progress"),
                                ]
                            }
                        }
                    },
                )
            ]
        )

        issues = self.client(transport).fetch_issue_states_by_ids(["issue-1", "issue-2", "issue-1"])

        self.assertEqual(["issue-1", "issue-2"], [issue.id for issue in issues])
        self.assertEqual(ISSUES_BY_ID_QUERY, transport.calls[0]["payload"]["query"])
        self.assertIn("$ids: [ID!]!", transport.calls[0]["payload"]["query"])
        self.assertEqual(["issue-1", "issue-2"], transport.calls[0]["payload"]["variables"]["ids"])

    def test_graphql_errors_are_redacted(self):
        transport = RecordingTransport([GraphQLResponse(200, {"errors": [{"message": "bad lin_secret"}]})])

        with self.assertRaises(LinearGraphQLError) as raised:
            self.client(transport).fetch_candidate_issues()

        self.assertIn("[REDACTED]", str(raised.exception))
        self.assertNotIn("lin_secret", str(raised.exception))

    def test_status_errors_are_redacted(self):
        transport = RecordingTransport([GraphQLResponse(401, "token lin_secret rejected")])

        with self.assertRaises(LinearAPIStatusError) as raised:
            self.client(transport).fetch_candidate_issues()

        self.assertIn("[REDACTED]", str(raised.exception))
        self.assertNotIn("lin_secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
