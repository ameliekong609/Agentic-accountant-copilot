"""Central engagement state.

EngagementState is the single coordination object the orchestrator and agents
reason over. It intentionally stores references to heavy artefacts rather than
embedding source documents or workbooks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._json import JsonModelMixin
from .decisions import AccountantDecision, DecisionStatus
from .evidence import EvidenceRef
from .exceptions import ExceptionItem, ExceptionStatus
from .preferences import PreferenceRule, PreferenceStatus


@dataclass
class EngagementState(JsonModelMixin):
    engagement_id: str
    entity_name: str
    fy_start: str
    fy_end: str
    entity_type: str | None = None
    documents_ref: str | None = None
    coa_ref: str | None = None
    bank_txns_ref: str | None = None
    events_ref: str | None = None
    matches_ref: str | None = None
    journals_ref: str | None = None
    statements_ref: str | None = None
    exceptions: list[ExceptionItem] = field(default_factory=list)
    decisions: list[AccountantDecision] = field(default_factory=list)
    preferences: list[PreferenceRule] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    agent_tasks: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EngagementState":
        return cls(
            engagement_id=data["engagement_id"],
            entity_name=data["entity_name"],
            entity_type=data.get("entity_type"),
            fy_start=data["fy_start"],
            fy_end=data["fy_end"],
            documents_ref=data.get("documents_ref"),
            coa_ref=data.get("coa_ref"),
            bank_txns_ref=data.get("bank_txns_ref"),
            events_ref=data.get("events_ref"),
            matches_ref=data.get("matches_ref"),
            journals_ref=data.get("journals_ref"),
            statements_ref=data.get("statements_ref"),
            exceptions=[ExceptionItem.from_dict(x) for x in data.get("exceptions", [])],
            decisions=[AccountantDecision.from_dict(x) for x in data.get("decisions", [])],
            preferences=[PreferenceRule.from_dict(x) for x in data.get("preferences", [])],
            evidence=[EvidenceRef.from_dict(x) for x in data.get("evidence", [])],
            agent_tasks=list(data.get("agent_tasks", [])),
        )

    def open_exceptions(self) -> list[ExceptionItem]:
        return [item for item in self.exceptions if item.is_open]

    def blocking_exceptions(self) -> list[ExceptionItem]:
        return [item for item in self.exceptions if item.is_blocking_by_default]

    def approved_preferences(self) -> list[PreferenceRule]:
        return [rule for rule in self.preferences if rule.status == PreferenceStatus.APPROVED]

    def approved_decision_ids(self) -> set[str]:
        return {
            decision.decision_id
            for decision in self.decisions
            if decision.status == DecisionStatus.APPROVED
        }

    def unresolved_human_approval_exceptions(self) -> list[ExceptionItem]:
        approved_ids = self.approved_decision_ids()
        unresolved: list[ExceptionItem] = []
        for item in self.exceptions:
            if item.status == ExceptionStatus.ACCEPTED_RISK:
                if item.requires_human_approval and item.decision_id not in approved_ids:
                    unresolved.append(item)
                continue
            if item.requires_human_approval and item.is_open:
                unresolved.append(item)
        return unresolved
