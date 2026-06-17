"""Central engagement state.

EngagementState is the single coordination object the orchestrator and agents
reason over. It intentionally stores references to heavy artefacts rather than
embedding source documents or workbooks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._json import JsonModelMixin
from .artifacts import AdjustmentProposal, ChartAccount, OutputArtifact, SourceDocument, StateTransition
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
    source_documents: list[SourceDocument] = field(default_factory=list)
    chart_accounts: list[ChartAccount] = field(default_factory=list)
    adjustment_proposals: list[AdjustmentProposal] = field(default_factory=list)
    output_artifacts: list[OutputArtifact] = field(default_factory=list)
    state_transitions: list[StateTransition] = field(default_factory=list)
    agent_tasks: list[dict[str, Any]] = field(default_factory=list)
    coa_review_required: bool = False
    coa_review_status: str = "not_required"
    adjustment_review_status: str = "not_started"
    lifecycle_status: str = "intake"

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
            source_documents=[SourceDocument.from_dict(x) for x in data.get("source_documents", [])],
            chart_accounts=[ChartAccount.from_dict(x) for x in data.get("chart_accounts", [])],
            adjustment_proposals=[AdjustmentProposal.from_dict(x) for x in data.get("adjustment_proposals", [])],
            output_artifacts=[OutputArtifact.from_dict(x) for x in data.get("output_artifacts", [])],
            state_transitions=[StateTransition.from_dict(x) for x in data.get("state_transitions", [])],
            agent_tasks=list(data.get("agent_tasks", [])),
            coa_review_required=bool(data.get("coa_review_required", False)),
            coa_review_status=data.get("coa_review_status", "not_required"),
            adjustment_review_status=data.get("adjustment_review_status", "not_started"),
            lifecycle_status=data.get("lifecycle_status", "intake"),
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
