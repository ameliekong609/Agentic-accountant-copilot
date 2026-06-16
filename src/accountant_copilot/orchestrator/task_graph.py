"""Agent task graph primitives."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

from accountant_copilot.state._json import JsonModelMixin


class TaskStatus(str, Enum):
    TODO = "todo"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass
class AgentTask(JsonModelMixin):
    agent_type: str
    goal: str
    acceptance_criteria: list[str]
    task_id: str = field(default_factory=lambda: f"task_{uuid4().hex[:12]}")
    status: TaskStatus = TaskStatus.TODO
    input_refs: list[str] = field(default_factory=list)
    output_refs: list[str] = field(default_factory=list)
    result_summary: str | None = None
    finding_refs: list[str] = field(default_factory=list)
    requires_human_approval: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentTask":
        return cls(
            task_id=data.get("task_id") or f"task_{uuid4().hex[:12]}",
            agent_type=data["agent_type"],
            goal=data["goal"],
            acceptance_criteria=list(data.get("acceptance_criteria", [])),
            status=TaskStatus(data.get("status", TaskStatus.TODO.value)),
            input_refs=list(data.get("input_refs", [])),
            output_refs=list(data.get("output_refs", [])),
            result_summary=data.get("result_summary"),
            finding_refs=list(data.get("finding_refs", [])),
            requires_human_approval=bool(data.get("requires_human_approval", False)),
        )
