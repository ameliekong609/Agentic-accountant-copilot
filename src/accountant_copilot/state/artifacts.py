"""Structured engagement artifact models."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._json import JsonModelMixin


@dataclass
class SourceDocument(JsonModelMixin):
    document_id: str
    file_path: str
    document_type: str
    entity: str
    period_start: str
    period_end: str
    source_hash: str
    status: str = "recorded"
    notes: str | None = None
    original_file_name: str | None = None
    display_name: str | None = None
    naming_confidence: str | None = None
    naming_status: str = "not_suggested"
    naming_method: str | None = None
    naming_evidence_refs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceDocument":
        return cls(
            document_id=data["document_id"],
            file_path=data["file_path"],
            document_type=data["document_type"],
            entity=data["entity"],
            period_start=data["period_start"],
            period_end=data["period_end"],
            source_hash=data["source_hash"],
            status=data.get("status", "recorded"),
            notes=data.get("notes"),
            original_file_name=data.get("original_file_name"),
            display_name=data.get("display_name") or data.get("suggested_name"),
            naming_confidence=data.get("naming_confidence"),
            naming_status=data.get("naming_status", "not_suggested"),
            naming_method=data.get("naming_method"),
            naming_evidence_refs=list(data.get("naming_evidence_refs", [])),
        )


@dataclass
class ChartAccount(JsonModelMixin):
    account_id: str
    code: str
    name: str
    type: str
    presentation_group: str
    opening_balance: str
    source_evidence_refs: list[str] = field(default_factory=list)
    status: str = "pending_review"
    decision_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChartAccount":
        return cls(
            account_id=data["account_id"],
            code=data["code"],
            name=data["name"],
            type=data["type"],
            presentation_group=data["presentation_group"],
            opening_balance=str(data["opening_balance"]),
            source_evidence_refs=list(data.get("source_evidence_refs", [])),
            status=data.get("status", "pending_review"),
            decision_id=data.get("decision_id"),
        )


@dataclass
class AdjustmentProposal(JsonModelMixin):
    adjustment_id: str
    description: str
    debit_account: str
    credit_account: str
    amount: str
    date: str
    source_evidence_refs: list[str] = field(default_factory=list)
    status: str = "pending_review"
    decision_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdjustmentProposal":
        return cls(
            adjustment_id=data["adjustment_id"],
            description=data["description"],
            debit_account=data["debit_account"],
            credit_account=data["credit_account"],
            amount=str(data["amount"]),
            date=data["date"],
            source_evidence_refs=list(data.get("source_evidence_refs", [])),
            status=data.get("status", "pending_review"),
            decision_id=data.get("decision_id"),
        )


@dataclass
class OutputArtifact(JsonModelMixin):
    output_id: str
    file_path: str
    artifact_type: str
    verifier_status: str
    created_at: str
    source_state_hash: str | None = None
    release_manifest_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutputArtifact":
        return cls(
            output_id=data["output_id"],
            file_path=data["file_path"],
            artifact_type=data["artifact_type"],
            verifier_status=data["verifier_status"],
            created_at=data["created_at"],
            source_state_hash=data.get("source_state_hash"),
            release_manifest_id=data.get("release_manifest_id"),
        )


@dataclass
class StateTransition(JsonModelMixin):
    transition_id: str
    command: str
    before_hash: str
    after_hash: str
    actor: str
    timestamp: str
    summary: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateTransition":
        return cls(
            transition_id=data["transition_id"],
            command=data["command"],
            before_hash=data["before_hash"],
            after_hash=data["after_hash"],
            actor=data.get("actor", "system"),
            timestamp=data["timestamp"],
            summary=data["summary"],
        )
