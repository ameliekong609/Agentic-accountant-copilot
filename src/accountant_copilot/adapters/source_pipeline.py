"""Import exceptions from source pipeline control outputs.

This adapter is intentionally one-way: it translates deterministic matching and
journal verifier control signals into the Agentic Accountant Copilot exception
queue without exposing implementation-version language to users.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity


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


def _import_unmatched_bank(matching_payload: dict[str, Any]) -> list[ExceptionItem]:
    exceptions: list[ExceptionItem] = []
    for idx, item in enumerate(matching_payload.get("unmatched_bank", []), start=1):
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
                evidence_refs=[
                    f"matching.unmatched_bank[{idx - 1}]",
                    f"bank:{item.get('statement_id', '?')}:{item.get('row_index', '?')}",
                ],
                recommended_action=(
                    "Review the unmatched bank transaction, confirm classification, "
                    "or provide missing supporting evidence before release."
                ),
                requires_human_approval=True,
            )
        )
    return exceptions


def _import_unmatched_events(matching_payload: dict[str, Any]) -> list[ExceptionItem]:
    exceptions: list[ExceptionItem] = []
    for idx, item in enumerate(matching_payload.get("unmatched_events", []), start=1):
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
                evidence_refs=[f"matching.unmatched_events[{idx - 1}]", item.get("event_id", "")],
                recommended_action=(
                    "Review whether this event is an accrual, out-of-period item, "
                    "wrong-entity document, or missing bank movement."
                ),
                requires_human_approval=True,
            )
        )
    return exceptions


def _import_verifier_findings(journal_payload: dict[str, Any]) -> list[ExceptionItem]:
    exceptions: list[ExceptionItem] = []
    for idx, finding in enumerate(journal_payload.get("verifier_findings", []), start=1):
        check = finding.get("check", "unknown")
        row_name = finding.get("row_name", "unknown row")
        detail = finding.get("detail", "")
        exceptions.append(
            ExceptionItem(
                exception_id=f"source_journal_finding_{idx:04d}",
                source="source_pipeline.journal.verifier_findings",
                severity=_verifier_severity(check),
                category=f"journal_{check}",
                description=f"Journal verifier finding [{check}] {row_name}: {detail}",
                evidence_refs=[finding.get("file", "journal"), f"journal.verifier_findings[{idx - 1}]"],
                recommended_action=(
                    "Resolve this journal/control finding or record an explicit "
                    "accountant-approved accepted-risk decision before final release."
                ),
                requires_human_approval=True,
            )
        )
    return exceptions


def import_source_pipeline_exceptions(matching_path: Path, journal_path: Path) -> list[ExceptionItem]:
    """Import source pipeline issues into the exception queue."""
    matching_payload = _load_json(matching_path)
    journal_payload = _load_json(journal_path)
    exceptions: list[ExceptionItem] = []
    exceptions.extend(_import_unmatched_bank(matching_payload))
    exceptions.extend(_import_unmatched_events(matching_payload))
    exceptions.extend(_import_verifier_findings(journal_payload))
    return exceptions
