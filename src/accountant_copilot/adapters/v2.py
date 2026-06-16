"""Import exceptions from the legacy V2 pipeline outputs.

This adapter is intentionally one-way: it translates useful V2 control signals
into the new Agentic Accountant Copilot exception queue. It does not preserve the
old step-by-step workflow as the product architecture.
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


def _step6_severity(check: str) -> ExceptionSeverity:
    if check in {"per_entry_balanced", "overall_balanced", "matches_have_entries"}:
        return ExceptionSeverity.CRITICAL
    if "reconcile" in check or "reconciles" in check:
        return ExceptionSeverity.HIGH
    return ExceptionSeverity.MEDIUM


def _import_unmatched_bank(step5: dict[str, Any]) -> list[ExceptionItem]:
    exceptions: list[ExceptionItem] = []
    for idx, item in enumerate(step5.get("unmatched_bank", []), start=1):
        classification = item.get("user_classification")
        reason = item.get("classification_reason")
        severity = ExceptionSeverity.MEDIUM if classification else ExceptionSeverity.HIGH
        description_parts = [
            f"Unmatched bank transaction {item.get('statement_id', '?')} row {item.get('row_index', '?')}",
            f"{item.get('date', 'unknown date')} {item.get('description', '')}".strip(),
            f"amount {_money(item.get('amount'))} {item.get('direction', '')}".strip(),
        ]
        if classification:
            description_parts.append(f"V2 classification: {classification}.")
        if reason:
            description_parts.append(str(reason))
        exceptions.append(
            ExceptionItem(
                exception_id=f"v2_step5_unmatched_bank_{idx:04d}",
                source="v2.step5.unmatched_bank",
                severity=severity,
                category="v2_unmatched_bank_transaction",
                description=" — ".join(part for part in description_parts if part),
                evidence_refs=[
                    f"step5.unmatched_bank[{idx - 1}]",
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


def _import_unmatched_events(step5: dict[str, Any]) -> list[ExceptionItem]:
    exceptions: list[ExceptionItem] = []
    for idx, item in enumerate(step5.get("unmatched_events", []), start=1):
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
            description_parts.append(f"V2 classification: {classification}.")
        if reason:
            description_parts.append(str(reason))
        exceptions.append(
            ExceptionItem(
                exception_id=f"v2_step5_unmatched_event_{idx:04d}",
                source="v2.step5.unmatched_events",
                severity=severity,
                category="v2_unmatched_event",
                description=" — ".join(part for part in description_parts if part),
                evidence_refs=[f"step5.unmatched_events[{idx - 1}]", item.get("event_id", "")],
                recommended_action=(
                    "Review whether this event is an accrual, out-of-period item, "
                    "wrong-entity document, or missing bank movement."
                ),
                requires_human_approval=True,
            )
        )
    return exceptions


def _import_step6_findings(step6: dict[str, Any]) -> list[ExceptionItem]:
    exceptions: list[ExceptionItem] = []
    for idx, finding in enumerate(step6.get("verifier_findings", []), start=1):
        check = finding.get("check", "unknown")
        row_name = finding.get("row_name", "unknown row")
        detail = finding.get("detail", "")
        exceptions.append(
            ExceptionItem(
                exception_id=f"v2_step6_finding_{idx:04d}",
                source="v2.step6.verifier_findings",
                severity=_step6_severity(check),
                category=f"v2_step6_{check}",
                description=f"Step 6 verifier finding [{check}] {row_name}: {detail}",
                evidence_refs=[finding.get("file", "step6"), f"step6.verifier_findings[{idx - 1}]"],
                recommended_action=(
                    "Resolve this journal/control finding or record an explicit "
                    "accountant-approved accepted-risk decision before final release."
                ),
                requires_human_approval=True,
            )
        )
    return exceptions


def import_v2_exceptions(step5_path: Path, step6_path: Path) -> list[ExceptionItem]:
    """Import Step 5/Step 6 V2 issues into the new exception queue."""
    step5 = _load_json(step5_path)
    step6 = _load_json(step6_path)
    exceptions: list[ExceptionItem] = []
    exceptions.extend(_import_unmatched_bank(step5))
    exceptions.extend(_import_unmatched_events(step5))
    exceptions.extend(_import_step6_findings(step6))
    return exceptions
