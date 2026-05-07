from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class WorkflowError(ValueError):
    """Raised when WORKFLOW.md cannot be parsed into the Symphony contract."""


@dataclass(frozen=True)
class WorkflowDefinition:
    config: dict[str, Any]
    prompt_template: str


def load_workflow(path: str | Path) -> WorkflowDefinition:
    workflow_path = Path(path)

    try:
        content = workflow_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorkflowError("missing_workflow_file") from exc

    return parse_workflow(content)


def parse_workflow(content: str) -> WorkflowDefinition:
    if content.startswith("---"):
        front_matter, body = _split_front_matter(content)
        parsed = yaml.safe_load(front_matter) if front_matter.strip() else {}

        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise WorkflowError("workflow_front_matter_must_be_map")

        return WorkflowDefinition(config=parsed, prompt_template=body.strip())

    return WorkflowDefinition(config={}, prompt_template=content.strip())


def _split_front_matter(content: str) -> tuple[str, str]:
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "", content

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])

    raise WorkflowError("unterminated_workflow_front_matter")
