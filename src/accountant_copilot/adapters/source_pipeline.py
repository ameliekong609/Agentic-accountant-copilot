"""Import exceptions and evidence from source pipeline control outputs.

This adapter translates deterministic matching and journal verifier control
signals into the Agentic Accountant Copilot exception queue and structured
evidence registry without exposing implementation-version language to users.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from accountant_copilot.state.evidence import EvidenceRef
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity


@dataclass
class SourcePipelineImport:
    exceptions: list[ExceptionItem]
    evidence: list[EvidenceRef]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _money(value: Any) -> str:
    if value is None:
        return "unknown amount"
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


def _verifier_severity(check: str) -> ExceptionSeverity:
    if check in {"per_entry_balanced", "overall_balanced", "matches_have_entries"}:
        return ExceptionSeverity.CRITICAL
    if "reconcile" in check or "reconciles" in check:
        return ExceptionSeverity.HIGH
    return ExceptionSeverity.MEDIUM


def _import_unmatched_bank(matching_payload: dict[str, Any]) -> SourcePipelineImport:
    exceptions: list[ExceptionItem] = []
    evidence: list[EvidenceRef] = []
    for idx, item in enumerate(matching_payload.get("unmatched_bank", []), start=1):
        evidence_id = f"ev_unmatched_bank_{idx:04d}"
        evidence.append(
            EvidenceRef(
                evidence_id=evidence_id,
                source_type="bank_statement",
                file_path=str(item.get("statement_id", "bank_statement")),
                row=str(item.get("row_index", "")) or None,
                quote=str(item.get("description", "")) or None,
                amount=str(item.get("amount", "")) or None,
                date=str(item.get("date", "")) or None,
            )
        )
        classification = item.get("user_classification")
        reason = item.get("classification_reason")
        severity = ExceptionSeverity.MEDIUM if classification else ExceptionSeverity.HIGH
        description_parts = [
            f"Unmatched bank transaction {item.get('statement_id', '?')} row {item.get('row_index', '?')}",
            f"{item.get('date', 'unknown date')} {item.get('description', '')}".strip(),
            f"amount {_money(item.get('amount'))} {item.get('direction', '')}".strip(),
        ]
        if classification:
            description_parts.append(f"Proposed classification: {classification}.")
        if reason:
            description_parts.append(str(reason))
        exceptions.append(
            ExceptionItem(
                exception_id=f"source_matching_unmatched_bank_{idx:04d}",
                source="source_pipeline.matching.unmatched_bank",
                severity=severity,
                category="unmatched_bank_transaction",
                description=" — ".join(part for part in description_parts if part),
                evidence_refs=[evidence_id],
                recommended_action=(
                    "Review the unmatched bank transaction, confirm classification, "
                    "or provide missing supporting evidence before release."
                ),
                requires_human_approval=True,
            )
        )
    return SourcePipelineImport(exceptions=exceptions, evidence=evidence)


def _import_unmatched_events(matching_payload: dict[str, Any]) -> SourcePipelineImport:
    exceptions: list[ExceptionItem] = []
    evidence: list[EvidenceRef] = []
    for idx, item in enumerate(matching_payload.get("unmatched_events", []), start=1):
        evidence_id = f"ev_unmatched_event_{idx:04d}"
        evidence.append(
            EvidenceRef(
                evidence_id=evidence_id,
                source_type="supporting_event",
                file_path=str(item.get("source_file") or item.get("event_id", "supporting_event")),
                quote=str(item.get("event_type", "")) or None,
                amount=str(item.get("net_cash_amount", "")) or None,
                date=str(item.get("date", "")) or None,
            )
        )
        classification = item.get("user_classification")
        reason = item.get("classification_reason")
        severity = ExceptionSeverity.MEDIUM if classification else ExceptionSeverity.HIGH
        description_parts = [
            f"Unmatched supporting event {item.get('event_id', '?')}",
            f"{item.get('event_type', 'unknown type')} from {item.get('counterparty', 'unknown counterparty')}",
            f"{item.get('date', 'unknown date')} net cash {_money(item.get('net_cash_amount'))}",
        ]
        if item.get("source_file"):
            description_parts.append(f"source file: {item['source_file']}.")
        if classification:
            description_parts.append(f"Proposed classification: {classification}.")
        if reason:
            description_parts.append(str(reason))
        exceptions.append(
            ExceptionItem(
                exception_id=f"source_matching_unmatched_event_{idx:04d}",
                source="source_pipeline.matching.unmatched_events",
                severity=severity,
                category="unmatched_event",
                description=" — ".join(part for part in description_parts if part),
                evidence_refs=[evidence_id],
                recommended_action=(
                    "Review whether this event is an accrual, out-of-period item, "
                    "wrong-entity document, or missing bank movement."
                ),
                requires_human_approval=True,
            )
        )
    return SourcePipelineImport(exceptions=exceptions, evidence=evidence)


def _import_verifier_findings(journal_payload: dict[str, Any]) -> SourcePipelineImport:
    exceptions: list[ExceptionItem] = []
    evidence: list[EvidenceRef] = []
    for idx, finding in enumerate(journal_payload.get("verifier_findings", []), start=1):
        evidence_id = f"ev_journal_finding_{idx:04d}"
        check = finding.get("check", "unknown")
        row_name = finding.get("row_name", "unknown row")
        detail = finding.get("detail", "")
        evidence.append(
            EvidenceRef(
                evidence_id=evidence_id,
                source_type="journal_control",
                file_path=str(finding.get("file", "journal")),
                quote=f"{row_name}: {detail}",
            )
        )
        exceptions.append(
            ExceptionItem(
                exception_id=f"source_journal_finding_{idx:04d}",
                source="source_pipeline.journal.verifier_findings",
                severity=_verifier_severity(check),
                category=f"journal_{check}",
                description=f"Journal verifier finding [{check}] {row_name}: {detail}",
                evidence_refs=[evidence_id],
                recommended_action=(
                    "Resolve this journal/control finding or record an explicit "
                    "accountant-approved accepted-risk decision before final release."
                ),
                requires_human_approval=True,
            )
        )
    return SourcePipelineImport(exceptions=exceptions, evidence=evidence)


def import_source_pipeline_controls(matching_path: Path, journal_path: Path) -> SourcePipelineImport:
    """Import source pipeline issues and structured evidence into engagement state."""
    matching_payload = _load_json(matching_path)
    journal_payload = _load_json(journal_path)
    result = SourcePipelineImport(exceptions=[], evidence=[])
    for partial in (
        _import_unmatched_bank(matching_payload),
        _import_unmatched_events(matching_payload),
        _import_verifier_findings(journal_payload),
    ):
        result.exceptions.extend(partial.exceptions)
        result.evidence.extend(partial.evidence)
    return result


def import_source_pipeline_exceptions(matching_path: Path, journal_path: Path) -> list[ExceptionItem]:
    """Import source pipeline issues into the exception queue."""
    return import_source_pipeline_controls(matching_path, journal_path).exceptions
