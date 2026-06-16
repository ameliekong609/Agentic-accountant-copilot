"""Accountant decision records.

Decisions are the human-control layer: agents may recommend, but accountant
approval is recorded explicitly and referenced by exceptions/preferences.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ._json import JsonModelMixin


class DecisionStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass
class AccountantDecision(JsonModelMixin):
    decision_id: str
    question: str
    selected_option: str
    rationale: str
    status: DecisionStatus = DecisionStatus.PROPOSED
    approved_by: str | None = None
    evidence_refs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountantDecision":
        return cls(
            decision_id=data["decision_id"],
            question=data["question"],
            selected_option=data["selected_option"],
            rationale=data["rationale"],
            status=DecisionStatus(data.get("status", DecisionStatus.PROPOSED.value)),
            approved_by=data.get("approved_by"),
            evidence_refs=list(data.get("evidence_refs", [])),
        )

    @property
    def is_approved(self) -> bool:
        return self.status == DecisionStatus.APPROVED
