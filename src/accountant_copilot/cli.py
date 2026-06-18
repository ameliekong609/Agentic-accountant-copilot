"""Command-line interface for the Agentic Accountant Copilot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from accountant_copilot.adapters.source_pipeline import import_source_pipeline_controls
from accountant_copilot.orchestrator.planner import build_readiness_report, plan_next_tasks
from accountant_copilot.state.artifacts import AdjustmentProposal, ChartAccount, OutputArtifact, SourceDocument, StateTransition
from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.evidence import EvidenceRef
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity, ExceptionStatus
from accountant_copilot.state.preferences import PreferenceRule, PreferenceScope, PreferenceStatus


DEFAULT_STATE_PATH = Path("outputs/engagement_state.json")
DEFAULT_TURING_ENTITY_NAME = "XYZ Financial Pty Ltd ATF XYZ Australia Financial Trust"
DEFAULT_TURING_ENGAGEMENT_ID = "turing_financial_statements_fy2025"
_OUT_OF_SCOPE_VERSION = "".join(("v", "2"))
_OUT_OF_SCOPE_VERSION_UPPER = _OUT_OF_SCOPE_VERSION.upper()


def _neutralise_text(value: str) -> str:
    """Convert older internal source labels into product-scope language."""
    replacements = {
        f"{_OUT_OF_SCOPE_VERSION}.step5.unmatched_bank": "source_pipeline.matching.unmatched_bank",
        f"{_OUT_OF_SCOPE_VERSION}.step5.unmatched_events": "source_pipeline.matching.unmatched_events",
        f"{_OUT_OF_SCOPE_VERSION}.step6.verifier_findings": "source_pipeline.journal.verifier_findings",
        f"{_OUT_OF_SCOPE_VERSION}_step5_unmatched_bank_": "source_matching_unmatched_bank_",
        f"{_OUT_OF_SCOPE_VERSION}_step5_unmatched_event_": "source_matching_unmatched_event_",
        f"{_OUT_OF_SCOPE_VERSION}_step6_finding_": "source_journal_finding_",
        f"{_OUT_OF_SCOPE_VERSION}_unmatched_bank_transaction": "unmatched_bank_transaction",
        f"{_OUT_OF_SCOPE_VERSION}_unmatched_event": "unmatched_event",
        f"{_OUT_OF_SCOPE_VERSION}_step6_": "journal_",
        f"{_OUT_OF_SCOPE_VERSION_UPPER} classification:": "Proposed classification:",
        "Step 5": "matching",
        "Step 6": "journal",
        "step5": "matching",
        "step6": "journal",
        _OUT_OF_SCOPE_VERSION_UPPER: "source pipeline",
        _OUT_OF_SCOPE_VERSION: "source_pipeline",
    }
    neutral = value
    for old, new in replacements.items():
        neutral = neutral.replace(old, new)
    return neutral


def _neutralise_state_labels(state: EngagementState) -> EngagementState:
    """Keep loaded state aligned with current product terminology."""
    for item in state.exceptions:
        item.exception_id = _neutralise_text(item.exception_id)
        item.source = _neutralise_text(item.source)
        item.category = _neutralise_text(item.category)
        item.description = _neutralise_text(item.description)
        item.evidence_refs = [_neutralise_text(ref) for ref in item.evidence_refs]
        if item.decision_id:
            item.decision_id = _neutralise_text(item.decision_id)
    for decision in state.decisions:
        decision.decision_id = _neutralise_text(decision.decision_id)
        decision.question = _neutralise_text(decision.question)
        decision.evidence_refs = [_neutralise_text(ref) for ref in decision.evidence_refs]
    return state


def load_engagement_state(path: Path) -> EngagementState:
    """Load an engagement state JSON file."""
    try:
        return _neutralise_state_labels(EngagementState.model_validate_json(path.read_text()))
    except FileNotFoundError as exc:
        raise SystemExit(f"Engagement state not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Engagement state is not valid JSON: {path}: {exc}") from exc
    except KeyError as exc:
        raise SystemExit(f"Engagement state missing required field {exc!s}: {path}") from exc


def save_engagement_state(path: Path, state: EngagementState) -> None:
    """Persist engagement state JSON, creating the output directory if required."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.model_dump_json())


def state_hash(state: EngagementState) -> str:
    return hashlib.sha256(state.model_dump_json().encode()).hexdigest()


def _record_state_transition(
    state: EngagementState,
    *,
    command: str,
    before_hash: str,
    actor: str = "system",
    summary: str = "State updated.",
) -> None:
    transition = StateTransition(
        transition_id=f"transition_{len(state.state_transitions) + 1:04d}",
        command=command,
        before_hash=before_hash,
        after_hash="pending",
        actor=actor,
        timestamp=datetime.now(timezone.utc).isoformat(),
        summary=summary,
    )
    state.state_transitions.append(transition)
    transition.after_hash = state_hash(state)


def _final_signoff_decision(state: EngagementState) -> AccountantDecision | None:
    for decision in state.decisions:
        if decision.selected_option == "final_signoff" and decision.is_approved:
            return decision
    return None


def derive_lifecycle_status(state: EngagementState) -> str:
    if state.lifecycle_status == "released":
        return "released"
    if _final_signoff_decision(state):
        return "signed_off"
    if state.open_exceptions():
        return "exceptions_open"
    if state.evidence:
        return "evidence_imported"
    if state.documents_ref:
        return "intake"
    return state.lifecycle_status or "intake"


def inspect_engagement(state: EngagementState) -> dict:
    """Build a serialisable inspection payload for UI/CLI consumers."""
    readiness = build_readiness_report(state)
    tasks = plan_next_tasks(state)
    next_task = tasks[0] if tasks else None
    return {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "entity_type": state.entity_type,
        "fy_start": state.fy_start,
        "fy_end": state.fy_end,
        "lifecycle_status": derive_lifecycle_status(state),
        "coa_review_status": state.coa_review_status,
        "open_exception_count": len(state.open_exceptions()),
        "blocking_exception_count": readiness.blocking_exception_count,
        "human_approval_exception_count": readiness.human_approval_exception_count,
        "final_output_allowed": readiness.final_output_allowed,
        "readiness_summary": readiness.summary,
        "recommended_next_task": next_task.model_dump() if next_task else None,
    }


def format_inspection(payload: dict) -> str:
    """Render the inspection payload as human-readable text."""
    allowed = "YES" if payload["final_output_allowed"] else "NO"
    lines = [
        f"Engagement: {payload['entity_name']}",
        f"Engagement ID: {payload['engagement_id']}",
        f"Entity type: {payload['entity_type'] or 'unknown'}",
        f"FY: {payload['fy_start']} to {payload['fy_end']}",
        f"Lifecycle status: {payload['lifecycle_status']}",
        f"CoA review status: {payload['coa_review_status']}",
        "",
        f"Open exceptions: {payload['open_exception_count']}",
        f"Blocking exceptions: {payload['blocking_exception_count']}",
        f"Human approvals needed: {payload['human_approval_exception_count']}",
        f"Final output allowed: {allowed}",
        f"Readiness: {payload['readiness_summary']}",
    ]
    task = payload.get("recommended_next_task")
    if task:
        lines.extend(
            [
                "",
                f"Recommended next task: {task['agent_type']}",
                f"Goal: {task['goal']}",
            ]
        )
        criteria = task.get("acceptance_criteria", [])
        if criteria:
            lines.append("Acceptance criteria:")
            lines.extend(f"- {item}" for item in criteria)
    return "\n".join(lines) + "\n"


def _inspect_engagement_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    payload = inspect_engagement(state)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_inspection(payload), end="")
    return 0 if payload["final_output_allowed"] else 1


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _refs_text(refs: list[str]) -> str:
    return "; ".join(ref for ref in refs if ref) or "none recorded"


def _decision_by_id(state: EngagementState) -> dict[str, AccountantDecision]:
    return {decision.decision_id: decision for decision in state.decisions}


def format_audit_trail(state: EngagementState) -> str:
    """Render a markdown audit trail for an engagement."""
    payload = inspect_engagement(state)
    allowed = "YES" if payload["final_output_allowed"] else "NO"
    decisions = _decision_by_id(state)
    lines = [
        f"# Audit Trail — {state.entity_name}",
        "",
        "## Engagement",
        f"- Engagement ID: {state.engagement_id}",
        f"- Entity type: {state.entity_type or 'unknown'}",
        f"- FY: {state.fy_start} to {state.fy_end}",
        f"- Documents ref: {state.documents_ref or 'none recorded'}",
        f"- CoA ref: {state.coa_ref or 'none recorded'}",
        "",
        "## Release readiness",
        f"- Open exceptions: {payload['open_exception_count']}",
        f"- Blocking exceptions: {payload['blocking_exception_count']}",
        f"- Human approvals needed: {payload['human_approval_exception_count']}",
        f"- Final output allowed: {allowed}",
        f"- Readiness: {payload['readiness_summary']}",
        "",
        "## Exceptions",
    ]

    if not state.exceptions:
        lines.append("No exceptions recorded.")
    else:
        sorted_exceptions = sorted(
            state.exceptions,
            key=lambda item: (item.severity.value, item.status.value, item.category, item.exception_id),
        )
        for item in sorted_exceptions:
            decision = decisions.get(item.decision_id or "")
            lines.extend(
                [
                    "",
                    f"### {item.exception_id} — {item.severity.value} / {item.status.value}",
                    f"- Category: {item.category}",
                    f"- Source: {item.source}",
                    f"- Blocking: {_yes_no(item.is_blocking_by_default)}",
                    f"- Requires human approval: {_yes_no(item.requires_human_approval)}",
                    f"- Evidence: {_refs_text(item.evidence_refs)}",
                    f"- Description: {item.description}",
                    f"- Recommended action: {item.recommended_action}",
                    f"- Decision: {item.decision_id or 'none recorded'}",
                ]
            )
            if decision:
                lines.extend(
                    [
                        f"- Decision status: {decision.status.value}",
                        f"- Selected option: {decision.selected_option}",
                        f"- Approved by: {decision.approved_by or 'none recorded'}",
                        f"- Rationale: {decision.rationale}",
                    ]
                )

    lines.extend(["", "## Accountant decisions"])
    if not state.decisions:
        lines.append("No accountant decisions recorded.")
    else:
        for decision in sorted(state.decisions, key=lambda item: item.decision_id):
            lines.extend(
                [
                    "",
                    f"### {decision.decision_id}",
                    f"- Status: {decision.status.value}",
                    f"- Question: {decision.question}",
                    f"- Selected option: {decision.selected_option}",
                    f"- Approved by: {decision.approved_by or 'none recorded'}",
                    f"- Evidence: {_refs_text(decision.evidence_refs)}",
                    f"- Rationale: {decision.rationale}",
                ]
            )

    lines.extend(["", "## Evidence registry"])
    if not state.evidence:
        lines.append("No structured evidence recorded.")
    else:
        for evidence in sorted(state.evidence, key=lambda item: item.evidence_id):
            details = [f"source_type={evidence.source_type}", f"file={evidence.file_path}"]
            if evidence.page:
                details.append(f"page={evidence.page}")
            if evidence.row:
                details.append(f"row={evidence.row}")
            if evidence.date:
                details.append(f"date={evidence.date}")
            if evidence.amount:
                details.append(f"amount={evidence.amount}")
            if evidence.confidence:
                details.append(f"confidence={evidence.confidence}")
            lines.extend(
                [
                    "",
                    f"### {evidence.evidence_id}",
                    f"- {'; '.join(details)}",
                    f"- Quote: {evidence.quote or 'none recorded'}",
                ]
            )

    return "\n".join(lines).rstrip() + "\n"


def _export_audit_trail_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    markdown = format_audit_trail(state)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown)
        print(f"Exported audit trail → {output_path}")
    else:
        print(markdown, end="")
    return 0 if inspect_engagement(state)["final_output_allowed"] else 1


def _format_exception_item(item: ExceptionItem) -> list[str]:
    evidence = "; ".join(ref for ref in item.evidence_refs if ref) or "none recorded"
    return [
        f"- {item.exception_id} [{item.status.value}] {item.category}",
        f"  Source: {item.source}",
        f"  Description: {item.description}",
        f"  Evidence: {evidence}",
        f"  Recommended action: {item.recommended_action}",
    ]


def format_exception_review(state: EngagementState) -> str:
    """Render open exceptions grouped by severity for accountant review."""
    lines = [
        "Open exception review",
        f"Engagement: {state.entity_name}",
        f"Engagement ID: {state.engagement_id}",
        "",
    ]
    open_items = sorted(
        state.open_exceptions(),
        key=lambda item: (item.severity.value, item.category, item.exception_id),
    )
    if not open_items:
        lines.append("No open exceptions.")
    else:
        severity_order = ["critical", "high", "medium", "low"]
        for severity in severity_order:
            grouped = [item for item in open_items if item.severity.value == severity]
            if not grouped:
                continue
            lines.append(severity.upper())
            for item in grouped:
                lines.extend(_format_exception_item(item))
            lines.append("")
    lines.append(format_inspection(inspect_engagement(state)).rstrip())
    return "\n".join(lines) + "\n"


def _find_exception(state: EngagementState, exception_id: str) -> ExceptionItem:
    for item in state.exceptions:
        if item.exception_id == exception_id:
            return item
    raise SystemExit(f"Exception not found: {exception_id}")


def _record_review_decision(
    state: EngagementState,
    item: ExceptionItem,
    action: ExceptionStatus,
    rationale: str,
    approved_by: str,
) -> AccountantDecision:
    decision = AccountantDecision(
        decision_id=f"decision_{item.exception_id}_{len(state.decisions) + 1:04d}",
        question=f"How should exception {item.exception_id} be handled?",
        selected_option=action.value,
        rationale=rationale,
        status=DecisionStatus.APPROVED,
        approved_by=approved_by,
        evidence_refs=list(item.evidence_refs),
    )
    state.decisions.append(decision)
    item.status = action
    item.decision_id = decision.decision_id
    return decision


def _usage_error(message: str) -> None:
    print(f"accountant-copilot: error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _validate_review_decision_payload(payload: dict) -> tuple[str, ExceptionStatus, str, str]:
    exception_id = payload.get("exception_id")
    action_value = payload.get("action")
    rationale = payload.get("rationale")
    approved_by = payload.get("approved_by")
    if not exception_id:
        _usage_error("batch decision missing exception_id")
    if not rationale:
        _usage_error(f"batch decision for {exception_id} requires rationale")
    if not approved_by:
        _usage_error(f"batch decision for {exception_id} requires approved_by")
    if action_value not in {ExceptionStatus.RESOLVED.value, ExceptionStatus.ACCEPTED_RISK.value, ExceptionStatus.REJECTED.value}:
        _usage_error(f"invalid batch action for {exception_id}: {action_value}")
    return exception_id, ExceptionStatus(action_value), rationale, approved_by


def _apply_review_batch(state: EngagementState, decisions_path: Path) -> int:
    try:
        payload = json.loads(decisions_path.read_text())
    except FileNotFoundError:
        _usage_error(f"Batch decisions file not found: {decisions_path}")
    except json.JSONDecodeError as exc:
        _usage_error(f"Batch decisions file is not valid JSON: {exc}")
    entries = payload.get("decisions")
    if not isinstance(entries, list):
        _usage_error("Batch decisions file must contain a decisions list")
    parsed = [_validate_review_decision_payload(entry) for entry in entries]
    by_id = {item.exception_id: item for item in state.exceptions}
    for exception_id, _action, _rationale, _approved_by in parsed:
        if exception_id not in by_id:
            _usage_error(f"Unknown exception_id in batch: {exception_id}")
    for exception_id, action, rationale, approved_by in parsed:
        _record_review_decision(state, by_id[exception_id], action, rationale, approved_by)
    return len(parsed)


def _review_exceptions_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)

    if args.decisions:
        count = _apply_review_batch(state, Path(args.decisions))
        save_engagement_state(state_path, state)
        print(f"Applied {count} exception review decisions")
        print(format_inspection(inspect_engagement(state)), end="")
        return 0 if inspect_engagement(state)["final_output_allowed"] else 1

    if not args.exception_id:
        print(format_exception_review(state), end="")
        return 0 if inspect_engagement(state)["final_output_allowed"] else 1

    if not args.action:
        _usage_error("--action is required when --exception-id is provided")
    if args.action == ExceptionStatus.ACCEPTED_RISK.value and not args.rationale:
        _usage_error("accepted_risk requires --rationale")
    if args.action in {ExceptionStatus.RESOLVED.value, ExceptionStatus.REJECTED.value} and not args.rationale:
        _usage_error(f"{args.action} requires --rationale")
    if not args.approved_by:
        _usage_error("--approved-by is required when recording a review decision")

    item = _find_exception(state, args.exception_id)
    action = ExceptionStatus(args.action)
    _record_review_decision(
        state=state,
        item=item,
        action=action,
        rationale=args.rationale,
        approved_by=args.approved_by,
    )
    save_engagement_state(state_path, state)
    print(f"Updated exception {item.exception_id}: {item.status.value}")
    print(format_inspection(inspect_engagement(state)), end="")
    return 0 if inspect_engagement(state)["final_output_allowed"] else 1


def _sign_off_engagement_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    payload = inspect_engagement(state)
    if not payload["final_output_allowed"]:
        print(f"Cannot sign off engagement: {payload['readiness_summary']}", file=sys.stderr)
        return 1
    if not args.approved_by:
        _usage_error("--approved-by is required")
    if not args.rationale:
        _usage_error("--rationale is required")
    decision = AccountantDecision(
        decision_id=f"decision_final_signoff_{len(state.decisions) + 1:04d}",
        question="May the final financial statement workpaper pack be released?",
        selected_option="final_signoff",
        rationale=args.rationale,
        status=DecisionStatus.APPROVED,
        approved_by=args.approved_by,
        evidence_refs=[state.statements_ref] if state.statements_ref else [],
    )
    state.decisions.append(decision)
    save_engagement_state(state_path, state)
    print(f"Engagement signed off by {args.approved_by}")
    print(format_inspection(inspect_engagement(state)), end="")
    return 0


def _export_workpaper_pack_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = inspect_engagement(state)
    (output_dir / "engagement_summary.md").write_text(format_inspection(payload))
    (output_dir / "exception_review.md").write_text(format_exception_review(state))
    (output_dir / "audit_trail.md").write_text(format_audit_trail(state))
    (output_dir / "readiness.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (output_dir / "decisions.json").write_text(json.dumps([d.model_dump() for d in state.decisions], indent=2, sort_keys=True))
    print(f"Exported workpaper pack → {output_dir}")
    return 0 if payload["final_output_allowed"] else 1


def _record_preference_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    status = PreferenceStatus.APPROVED if args.approved_by else PreferenceStatus.SUGGESTED
    preference = PreferenceRule(
        scope=PreferenceScope(args.scope),
        subject=args.subject,
        rule=args.rule,
        status=status,
        approved_by=args.approved_by,
        evidence_refs=list(args.evidence_ref or []),
    )
    state.preferences.append(preference)
    save_engagement_state(state_path, state)
    print(f"Recorded preference {preference.preference_id} ({preference.status.value})")
    return 0


def format_preferences(state: EngagementState) -> str:
    lines = ["Preferences", f"Engagement: {state.entity_name}", ""]
    if not state.preferences:
        lines.append("No preferences recorded.")
    else:
        for pref in sorted(state.preferences, key=lambda item: item.preference_id):
            lines.extend([
                f"- {pref.preference_id} [{pref.status.value}] {pref.scope.value}:{pref.subject}",
                f"  Rule: {pref.rule}",
                f"  Approved by: {pref.approved_by or 'none recorded'}",
                f"  Evidence: {_refs_text(pref.evidence_refs)}",
            ])
    return "\n".join(lines) + "\n"


def _list_preferences_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    print(format_preferences(state), end="")
    return 0


def _validate_state_command(args: argparse.Namespace) -> int:
    try:
        load_engagement_state(Path(args.state))
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("Engagement state is valid")
    return 0


_AMOUNT_RE = re.compile(r"(?:[$€£]\s?-?\d[\d,]*(?:\.\d{2})?|-?\d{1,3}(?:,\d{3})+(?:\.\d{2})?)")
_DATE_RE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b")
_CONTENT_KEYWORDS = {
    "bank": ["bank", "statement", "account", "closing balance", "opening balance"],
    "distribution": ["distribution", "dividend", "payment advice"],
    "tax": ["tax", "franking", "withholding", "capital gain"],
    "broker": ["confirmation", "sell", "buy", "settlement", "trade"],
    "interest": ["interest"],
    "balance": ["balance", "market value", "net asset"],
    "fees": ["fee", "management fee", "expense"],
}


def _unique_matches(pattern: re.Pattern[str], text: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for match in pattern.findall(text):
        value = match.strip()
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def _content_tags(text: str, document_type: str) -> list[str]:
    haystack = text.lower()
    tags = [document_type]
    for tag, needles in _CONTENT_KEYWORDS.items():
        if any(needle in haystack for needle in needles):
            tags.append(tag)
    return sorted(dict.fromkeys(tags))


def _build_document_inventory_payload(state: EngagementState) -> dict:
    evidence_by_document: dict[str, list[EvidenceRef]] = {}
    for evidence in state.evidence:
        key = evidence.document_id or evidence.file_path
        evidence_by_document.setdefault(key, []).append(evidence)

    documents = []
    for document in state.source_documents:
        evidence_items = evidence_by_document.get(document.document_id, [])
        combined = " ".join(item.quote or "" for item in evidence_items)
        pages = []
        for item in sorted(evidence_items, key=lambda ev: (int(ev.page or 0), ev.evidence_id)):
            quote = " ".join((item.quote or "").split())
            pages.append(
                {
                    "page": item.page,
                    "evidence_id": item.evidence_id,
                    "snippet": quote[:300],
                    "dates": _unique_matches(_DATE_RE, quote),
                    "amounts": _unique_matches(_AMOUNT_RE, quote),
                    "tags": _content_tags(quote, document.document_type),
                }
            )
        documents.append(
            {
                "document_id": document.document_id,
                "file_path": document.file_path,
                "document_type": document.document_type,
                "status": document.status,
                "evidence_count": len(evidence_items),
                "tags": _content_tags(combined, document.document_type),
                "dates": _unique_matches(_DATE_RE, combined, limit=12),
                "amounts": _unique_matches(_AMOUNT_RE, combined, limit=12),
                "pages": pages,
            }
        )
    return {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "documents": documents}


def _format_document_inventory(payload: dict) -> str:
    lines = [f"# Document Inventory — {payload['entity_name']}", ""]
    lines.append(f"Documents: {len(payload['documents'])}")
    lines.append("")
    for document in payload["documents"]:
        lines.extend(
            [
                f"## {document['document_id']} — {Path(document['file_path']).name}",
                f"- Path: `{document['file_path']}`",
                f"- Type: {document['document_type']}",
                f"- Evidence refs: {document['evidence_count']}",
                f"- Tags: {', '.join(document['tags']) if document['tags'] else 'none'}",
                f"- Dates found: {', '.join(document['dates']) if document['dates'] else 'none'}",
                f"- Amounts found: {', '.join(document['amounts']) if document['amounts'] else 'none'}",
                "",
            ]
        )
        if document["pages"]:
            lines.append("### Page evidence")
            for page in document["pages"]:
                lines.extend(
                    [
                        f"- Page {page['page'] or 'n/a'} — {page['evidence_id']}",
                        f"  - Tags: {', '.join(page['tags']) if page['tags'] else 'none'}",
                        f"  - Dates: {', '.join(page['dates']) if page['dates'] else 'none'}",
                        f"  - Amounts: {', '.join(page['amounts']) if page['amounts'] else 'none'}",
                        f"  - Snippet: {page['snippet']}",
                    ]
                )
            lines.append("")
        else:
            lines.extend(["No extracted page evidence yet.", ""])
    return "\n".join(lines).rstrip() + "\n"


def _export_document_inventory_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    payload = _build_document_inventory_payload(state)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_document_inventory(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported document inventory → {output}")
    print(f"Exported document inventory JSON → {json_output}")
    return 0


_PERIOD_RE = re.compile(
    r"Statement Period\s+(?P<start>\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\s*-\s*(?P<end>\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
    re.IGNORECASE,
)
_ALT_PERIOD_RE = re.compile(
    r"(?P<start>\d{1,2}/\d{1,2}/\d{2,4})\s+STATEMENT OPENING BALANCE.*?(?P<end>\d{1,2}/\d{1,2}/\d{2,4})\s+CLOSING BALANCE",
    re.IGNORECASE,
)
_OPENING_BALANCE_RE = re.compile(
    r"Opening Balance\s+(?P<amount>(?:[$€£]\s?)?[+-]?\s?\d[\d,]*(?:\.\d{2})?)\s*(?P<sign>CR|DR)?",
    re.IGNORECASE,
)
_CLOSING_BALANCE_RE = re.compile(
    r"Closing Balance\s+(?P<amount>(?:[$€£]\s?)?[+-]?\s?\d[\d,]*(?:\.\d{2})?)\s*(?P<sign>CR|DR)?",
    re.IGNORECASE,
)
_TOTAL_CREDITS_RE = re.compile(
    r"Total\s+(?:Credits|Deposits)\s*(?P<sign>[+-])?\s*(?P<amount>(?:[$€£]\s?)?\d[\d,]*(?:\.\d{2})?)",
    re.IGNORECASE,
)
_TOTAL_DEBITS_RE = re.compile(
    r"Total\s+(?:Debits|Withdrawals)\s*(?P<sign>[+-])?\s*(?P<amount>(?:[$€£]\s?)?\d[\d,]*(?:\.\d{2})?)",
    re.IGNORECASE,
)
_ACCOUNT_NUMBER_RE = re.compile(r"Account Number\s+(?P<account>[^\n]{0,80}?)(?:Statement Period|Opening Balance|Closing Balance|Business|$)", re.IGNORECASE)


def _clean_money_amount(amount: str | None) -> str | None:
    if amount is None:
        return None
    return amount.replace("$ ", "$").replace("€ ", "€").replace("£ ", "£").replace("+ ", "").replace("+", "").strip()


def _infer_bank_account_key(quote: str, account_number_raw: str | None) -> str:
    if account_number_raw:
        return f"account:{account_number_raw}"
    product_match = re.search(r"Business Transaction Account\s+\d+", quote, re.IGNORECASE)
    if product_match:
        return product_match.group(0).lower().replace(" ", "_")
    if re.search(r"Statement No\.\s+\d+", quote, re.IGNORECASE):
        return "statement_no_bank_account"
    return "unknown_bank_account"


def _extract_bank_fact_from_evidence(document: SourceDocument, evidence: EvidenceRef) -> dict | None:
    quote = " ".join((evidence.quote or "").split())
    period_match = _PERIOD_RE.search(quote) or _ALT_PERIOD_RE.search(quote)
    opening_match = _OPENING_BALANCE_RE.search(quote)
    closing_match = _CLOSING_BALANCE_RE.search(quote)
    total_credits_match = _TOTAL_CREDITS_RE.search(quote)
    total_debits_match = _TOTAL_DEBITS_RE.search(quote)
    account_match = _ACCOUNT_NUMBER_RE.search(quote)
    if not period_match or not closing_match:
        return None
    account_number_raw = account_match.group("account").strip() if account_match else None
    account_key_raw = _infer_bank_account_key(quote, account_number_raw)
    fact = {
        "document_id": document.document_id,
        "file_path": document.file_path,
        "page": evidence.page,
        "evidence_id": evidence.evidence_id,
        "account_number_raw": account_number_raw or None,
        "account_key_raw": account_key_raw,
        "statement_period_start": period_match.group("start"),
        "statement_period_end": period_match.group("end"),
        "opening_balance": _clean_money_amount(opening_match.group("amount")) if opening_match else None,
        "opening_balance_sign": (opening_match.group("sign") or "").upper() or None if opening_match else None,
        "closing_balance": _clean_money_amount(closing_match.group("amount")),
        "closing_balance_sign": (closing_match.group("sign") or "").upper() or None,
        "total_credits": _clean_money_amount(total_credits_match.group("amount")) if total_credits_match else None,
        "total_credits_sign": total_credits_match.group("sign") if total_credits_match else None,
        "total_debits": _clean_money_amount(total_debits_match.group("amount")) if total_debits_match else None,
        "total_debits_sign": total_debits_match.group("sign") if total_debits_match else None,
        "status": "extracted",
        "snippet": quote[:300],
    }
    return fact


def _build_bank_statement_facts_payload(state: EngagementState) -> dict:
    documents = {doc.document_id: doc for doc in state.source_documents if doc.document_type == "bank_statement"}
    evidence_by_document: dict[str, list[EvidenceRef]] = {doc_id: [] for doc_id in documents}
    for evidence in state.evidence:
        if evidence.source_type == "bank_statement" and evidence.document_id in documents:
            evidence_by_document.setdefault(evidence.document_id, []).append(evidence)

    facts: list[dict] = []
    findings: list[dict] = []
    extracted_document_ids: set[str] = set()
    for document_id, evidence_items in evidence_by_document.items():
        document = documents[document_id]
        for evidence in evidence_items:
            fact = _extract_bank_fact_from_evidence(document, evidence)
            if fact:
                facts.append(fact)
                extracted_document_ids.add(document_id)
                missing_fields = [field for field in ("opening_balance",) if fact.get(field) is None]
                if missing_fields:
                    findings.append(
                        {
                            "category": "bank_statement_fact_missing",
                            "document_id": document.document_id,
                            "file_path": document.file_path,
                            "evidence_id": evidence.evidence_id,
                            "page": evidence.page,
                            "missing_fields": missing_fields,
                            "recommended_action": "Review source document and improve bank fact parser or mark evidence out of scope.",
                        }
                    )
        if evidence_items and document_id not in extracted_document_ids:
            first = sorted(evidence_items, key=lambda ev: (int(ev.page or 0), ev.evidence_id))[0]
            findings.append(
                {
                    "category": "bank_statement_fact_missing",
                    "document_id": document.document_id,
                    "file_path": document.file_path,
                    "evidence_id": first.evidence_id,
                    "page": first.page,
                    "missing_fields": ["statement_period", "closing_balance"],
                    "recommended_action": "Review source document and improve bank fact parser or mark evidence out of scope.",
                }
            )
        elif not evidence_items:
            findings.append(
                {
                    "category": "bank_statement_evidence_missing",
                    "document_id": document.document_id,
                    "file_path": document.file_path,
                    "evidence_id": None,
                    "page": None,
                    "missing_fields": ["page_evidence"],
                    "recommended_action": "Extract bank statement source evidence before fact extraction.",
                }
            )
    return {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "fact_type": "bank_statement_facts",
        "facts": facts,
        "findings": findings,
        "summary": {
            "bank_documents": len(documents),
            "facts_extracted": len(facts),
            "findings": len(findings),
        },
    }


def _format_bank_statement_facts(payload: dict) -> str:
    lines = [f"# Bank Statement Facts — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend(
        [
            f"- Bank documents: {summary['bank_documents']}",
            f"- Facts extracted: {summary['facts_extracted']}",
            f"- Findings: {summary['findings']}",
            "",
        ]
    )
    if payload["facts"]:
        lines.append("## Extracted facts")
        for fact in payload["facts"]:
            lines.extend(
                [
                    f"- `{fact['evidence_id']}` — `{fact['file_path']}` page {fact['page']}",
                    f"  - Statement period: {fact['statement_period_start']} to {fact['statement_period_end']}",
                    f"  - Opening balance: {fact['opening_balance'] or 'not extracted'} {fact['opening_balance_sign'] or ''}".rstrip(),
                    f"  - Closing balance: {fact['closing_balance']} {fact['closing_balance_sign'] or ''}".rstrip(),
                    f"  - Total credits/deposits: {fact.get('total_credits') or 'not extracted'} {fact.get('total_credits_sign') or ''}".rstrip(),
                    f"  - Total debits/withdrawals: {fact.get('total_debits') or 'not extracted'} {fact.get('total_debits_sign') or ''}".rstrip(),
                    f"  - Account number/raw: {fact['account_number_raw'] or 'not extracted'}",
                    f"  - Account key/raw: {fact.get('account_key_raw') or 'unknown_bank_account'}",
                    f"  - Snippet: {fact['snippet']}",
                ]
            )
        lines.append("")
    if payload["findings"]:
        lines.append("## Findings needing review")
        for finding in payload["findings"]:
            lines.extend(
                [
                    f"- `{finding['evidence_id']}` — `{finding['file_path']}` page {finding['page']}",
                    f"  - Missing: {', '.join(finding['missing_fields'])}",
                    f"  - Action: {finding['recommended_action']}",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _export_bank_statement_facts_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    payload = _build_bank_statement_facts_payload(state)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_bank_statement_facts(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported bank statement facts → {output}")
    print(f"Exported bank statement facts JSON → {json_output}")
    return 0 if not payload["findings"] else 1


_TRANSACTION_DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{2,4}\b")
_MONEY_TOKEN_RE = re.compile(r"(?:[$€£]\s?)?[+-]?\s?\d[\d,]*(?:\.\d{2})")


def _extract_bank_transactions_from_evidence(document: SourceDocument, evidence: EvidenceRef) -> list[dict]:
    quote = " ".join((evidence.quote or "").split())
    matches = list(_TRANSACTION_DATE_RE.finditer(quote))
    transactions: list[dict] = []
    for index, match in enumerate(matches):
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(quote)
        segment = quote[match.start():segment_end].strip()
        if re.search(r"STATEMENT OPENING BALANCE|CLOSING BALANCE", segment, re.IGNORECASE):
            continue
        amounts = list(_MONEY_TOKEN_RE.finditer(segment))
        if len(amounts) < 2:
            continue
        transaction_amount = amounts[-2]
        balance_amount = amounts[-1]
        description = segment[match.end() - match.start():transaction_amount.start()].strip(" -")
        if not description:
            continue
        credit = None
        debit = None
        if re.search(r"deposit|credit|interest|dividend", description, re.IGNORECASE):
            credit = _clean_money_amount(transaction_amount.group(0))
        elif re.search(r"withdrawal|debit|payment|fee|charge", description, re.IGNORECASE):
            debit = _clean_money_amount(transaction_amount.group(0))
        else:
            credit = _clean_money_amount(transaction_amount.group(0))
        transactions.append(
            {
                "document_id": document.document_id,
                "file_path": document.file_path,
                "page": evidence.page,
                "evidence_id": evidence.evidence_id,
                "transaction_date": match.group(0),
                "description": description,
                "debit": debit,
                "credit": credit,
                "balance": _clean_money_amount(balance_amount.group(0)),
                "confidence": "text_pdf_pattern",
                "snippet": segment[:300],
            }
        )
    return transactions


def _build_bank_transactions_payload(state: EngagementState) -> dict:
    documents = {doc.document_id: doc for doc in state.source_documents if doc.document_type == "bank_statement"}
    transactions: list[dict] = []
    findings: list[dict] = []
    seen_documents: set[str] = set()
    for evidence in state.evidence:
        if evidence.source_type != "bank_statement" or evidence.document_id not in documents:
            continue
        document = documents[evidence.document_id]
        extracted = _extract_bank_transactions_from_evidence(document, evidence)
        if extracted:
            seen_documents.add(document.document_id)
            transactions.extend(extracted)
    for document_id, document in documents.items():
        if document_id not in seen_documents:
            findings.append(
                {
                    "category": "bank_transactions_not_extracted",
                    "document_id": document.document_id,
                    "file_path": document.file_path,
                    "recommended_action": "Review source document and improve transaction parser or mark transactions out of scope.",
                }
            )
    return {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "fact_type": "bank_transactions",
        "transactions": transactions,
        "findings": findings,
        "summary": {
            "bank_documents": len(documents),
            "transactions_extracted": len(transactions),
            "findings": len(findings),
        },
    }


def _format_bank_transactions(payload: dict) -> str:
    lines = [f"# Bank Transactions — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend(
        [
            f"- Bank documents: {summary['bank_documents']}",
            f"- Transactions extracted: {summary['transactions_extracted']}",
            f"- Findings: {summary['findings']}",
            "",
        ]
    )
    if payload["transactions"]:
        lines.append("## Extracted transactions")
        for transaction in payload["transactions"]:
            lines.extend(
                [
                    f"- `{transaction['evidence_id']}` — `{transaction['transaction_date']}` — {transaction['description']}",
                    f"  - Debit: {transaction['debit'] or 'n/a'}",
                    f"  - Credit: {transaction['credit'] or 'n/a'}",
                    f"  - Balance: {transaction['balance'] or 'n/a'}",
                    f"  - Source: `{transaction['file_path']}` page {transaction['page']}",
                ]
            )
        lines.append("")
    if payload["findings"]:
        lines.append("## Findings needing review")
        for finding in payload["findings"]:
            lines.extend(
                [
                    f"- {finding['category']}: `{finding['file_path']}`",
                    f"  - Action: {finding['recommended_action']}",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _export_bank_transactions_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    payload = _build_bank_transactions_payload(state)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_bank_transactions(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported bank transactions → {output}")
    print(f"Exported bank transactions JSON → {json_output}")
    return 0 if not payload["findings"] else 1


_INVOICE_NUMBER_RE = re.compile(r"\b(?P<invoice>INV-\d+)\b", re.IGNORECASE)
_INVOICE_DATE_RE = re.compile(r"TAX\s+INVOICE\s+(?P<date>\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", re.IGNORECASE)
_DUE_DATE_RE = re.compile(r"Due\s+Date:?\s*(?P<date>\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
_SUPPLIER_RE = re.compile(r"(?P<supplier>Emerald\s+Family\s+Enterprise\s+Group\s+Pty\s+Ltd)", re.IGNORECASE)
_DESCRIPTION_RE = re.compile(r"(?P<description>Portfolio\s+Management\s+Services)\s+From\s+(?:\S+\s+){0,2}?(?P<start>\d{1,2}/\d{1,2}/\d{4})\s+to\s+(?P<end>\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)
_SUBTOTAL_RE = re.compile(r"Subtotal\s+(?P<amount>(?:[$€£]\s?)?\d[\d,]*(?:\.\d{2})?)", re.IGNORECASE)
_GST_RE = re.compile(r"Total\s+GST(?:\s+\d+%)?\s+(?P<amount>(?:[$€£]\s?)?\d[\d,]*(?:\.\d{2})?)", re.IGNORECASE)
_AMOUNT_DUE_RE = re.compile(r"Amount\s+Due\s+(?:AUD\s+)?(?P<amount>(?:[$€£]\s?)?\d[\d,]*(?:\.\d{2})?)", re.IGNORECASE)


def _is_invoice_evidence(evidence: EvidenceRef) -> bool:
    quote = evidence.quote or ""
    return bool(re.search(r"tax\s+invoice|invoice\s+number|amount\s+due", quote, re.IGNORECASE))


def _extract_invoice_fact(document: SourceDocument, evidence: EvidenceRef) -> dict | None:
    quote = " ".join((evidence.quote or "").split())
    if not _is_invoice_evidence(evidence):
        return None
    invoice_match = _INVOICE_NUMBER_RE.search(quote)
    date_match = _INVOICE_DATE_RE.search(quote)
    due_match = _DUE_DATE_RE.search(quote)
    supplier_match = _SUPPLIER_RE.search(quote)
    description_match = _DESCRIPTION_RE.search(quote)
    subtotal_match = _SUBTOTAL_RE.search(quote)
    gst_match = _GST_RE.search(quote)
    amount_due_match = _AMOUNT_DUE_RE.search(quote)
    if not (
        invoice_match
        and date_match
        and due_match
        and supplier_match
        and description_match
        and subtotal_match
        and gst_match
        and amount_due_match
    ):
        return None
    return {
        "document_id": document.document_id,
        "file_path": document.file_path,
        "page": evidence.page,
        "evidence_id": evidence.evidence_id,
        "invoice_number": invoice_match.group("invoice"),
        "invoice_date": date_match.group("date"),
        "due_date": due_match.group("date"),
        "supplier": supplier_match.group("supplier"),
        "description": description_match.group("description"),
        "service_period_start": description_match.group("start"),
        "service_period_end": description_match.group("end"),
        "subtotal": _clean_money_amount(subtotal_match.group("amount")),
        "gst": _clean_money_amount(gst_match.group("amount")),
        "amount_due": _clean_money_amount(amount_due_match.group("amount")),
        "confidence": evidence.confidence,
        "snippet": quote[:300],
    }


def _build_invoice_facts_payload(state: EngagementState) -> dict:
    documents = {doc.document_id: doc for doc in state.source_documents}
    facts: list[dict] = []
    findings: list[dict] = []
    candidate_documents: set[str] = set()
    extracted_documents: set[str] = set()
    for evidence in state.evidence:
        if evidence.document_id not in documents or not _is_invoice_evidence(evidence):
            continue
        candidate_documents.add(evidence.document_id)
        fact = _extract_invoice_fact(documents[evidence.document_id], evidence)
        if fact:
            facts.append(fact)
            extracted_documents.add(evidence.document_id)
        else:
            findings.append(
                {
                    "category": "invoice_fact_extraction_incomplete",
                    "document_id": evidence.document_id,
                    "evidence_id": evidence.evidence_id,
                    "file_path": evidence.file_path,
                    "recommended_action": "Review invoice OCR/text and improve parser or record an accountant decision.",
                }
            )
    return {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "fact_type": "invoice_facts",
        "facts": facts,
        "findings": findings,
        "summary": {
            "invoice_documents": len(candidate_documents),
            "facts_extracted": len(facts),
            "findings": len(findings),
        },
    }


def _format_invoice_facts(payload: dict) -> str:
    lines = [f"# Invoice Facts — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([
        f"- Invoice documents: {summary['invoice_documents']}",
        f"- Facts extracted: {summary['facts_extracted']}",
        f"- Findings: {summary['findings']}",
        "",
    ])
    if payload["facts"]:
        lines.append("## Extracted invoice facts")
        for fact in payload["facts"]:
            lines.extend([
                f"- `{fact['invoice_number']}` — {fact['supplier']}",
                f"  - Invoice date: {fact['invoice_date']}",
                f"  - Due date: {fact['due_date']}",
                f"  - Description: {fact['description']}",
                f"  - Service period: {fact['service_period_start']} to {fact['service_period_end']}",
                f"  - Subtotal: {fact['subtotal']}",
                f"  - GST: {fact['gst']}",
                f"  - Amount due: {fact['amount_due']}",
                f"  - Evidence: `{fact['evidence_id']}` from `{fact['file_path']}` page {fact['page']}",
                f"  - Confidence: {fact['confidence']}",
            ])
        lines.append("")
    if payload["findings"]:
        lines.append("## Findings needing review")
        for finding in payload["findings"]:
            lines.extend([
                f"- {finding['category']}: `{finding['file_path']}`",
                f"  - Evidence: `{finding['evidence_id']}`",
                f"  - Action: {finding['recommended_action']}",
            ])
    return "\n".join(lines).rstrip() + "\n"


def _export_invoice_facts_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    payload = _build_invoice_facts_payload(state)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_invoice_facts(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported invoice facts → {output}")
    print(f"Exported invoice facts JSON → {json_output}")
    return 0 if not payload["findings"] else 1


def _candidate_invoice_treatment(fact: dict) -> str:
    description = (fact.get("description") or "").lower()
    if "portfolio" in description and "management" in description:
        return "portfolio_management_fee_or_service_expense"
    return "invoice_expense_or_accrual_review_required"


def _build_invoice_review_payload(facts_payload: dict) -> dict:
    review_findings: list[dict] = []
    for fact in facts_payload.get("facts", []):
        review_findings.append(
            {
                "category": "invoice_accounting_treatment_review_required",
                "invoice_number": fact.get("invoice_number"),
                "supplier": fact.get("supplier"),
                "amount_due": fact.get("amount_due"),
                "gst": fact.get("gst"),
                "candidate_treatment": _candidate_invoice_treatment(fact),
                "recommended_action": "Accountant to approve expense/accrual treatment, GST treatment, period allocation, and payment/matching handling.",
                "approved": False,
                "evidence_id": fact.get("evidence_id"),
                "file_path": fact.get("file_path"),
                "page": fact.get("page"),
            }
        )
        if fact.get("confidence") == "image_ocr":
            review_findings.append(
                {
                    "category": "invoice_ocr_evidence_review_required",
                    "invoice_number": fact.get("invoice_number"),
                    "supplier": fact.get("supplier"),
                    "amount_due": fact.get("amount_due"),
                    "candidate_treatment": "ocr_source_confirmation",
                    "recommended_action": "Accountant to confirm OCR fields against the source image before relying on extracted invoice facts.",
                    "approved": False,
                    "evidence_id": fact.get("evidence_id"),
                    "file_path": fact.get("file_path"),
                    "page": fact.get("page"),
                }
            )
    return {
        "engagement_id": facts_payload.get("engagement_id"),
        "entity_name": facts_payload.get("entity_name"),
        "review_type": "invoice_accounting_review",
        "review_findings": review_findings,
        "summary": {
            "invoices_reviewed": len(facts_payload.get("facts", [])),
            "review_findings": len(review_findings),
            "approved": 0,
        },
    }


def _format_invoice_review(payload: dict) -> str:
    lines = [f"# Invoice Accounting Review — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([
        f"- Invoices reviewed: {summary['invoices_reviewed']}",
        f"- Review findings: {summary['review_findings']}",
        f"- Approved automatically: {summary['approved']}",
        "",
    ])
    if payload["review_findings"]:
        lines.append("## Review findings")
        for finding in payload["review_findings"]:
            lines.extend([
                f"- {finding['category']}: `{finding.get('invoice_number')}` — {finding.get('supplier')}",
                f"  - Amount due: {finding.get('amount_due')}",
                f"  - Candidate treatment: {finding.get('candidate_treatment')}",
                f"  - Approved: {finding.get('approved')}",
                f"  - Evidence: `{finding.get('evidence_id')}` from `{finding.get('file_path')}` page {finding.get('page')}",
                f"  - Action: {finding.get('recommended_action')}",
            ])
    return "\n".join(lines).rstrip() + "\n"


def _export_invoice_review_command(args: argparse.Namespace) -> int:
    facts_payload = json.loads(Path(args.facts).read_text())
    payload = _build_invoice_review_payload(facts_payload)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_invoice_review(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported invoice accounting review → {output}")
    print(f"Exported invoice accounting review JSON → {json_output}")
    return 0 if not payload["review_findings"] else 1


_DISTRIBUTION_COMPONENT_LABELS = {
    "net_cash_distribution": ["Net cash distribution", "Net distribution", "Total cash distribution"],
    "cash_distribution": ["Cash Distribution", "Distribution Amount", "Amount paid", "Net amount payable"],
    "interest": ["Interest"],
    "franked_dividends": ["Dividends: franked", "Franked dividends", "Dividends - franked"],
    "unfranked_dividends": ["Dividends: unfranked", "Unfranked dividends", "Dividends - unfranked"],
    "foreign_income": ["Foreign income", "Assessable foreign source income"],
    "capital_gains": ["Capital gains", "Capital gain"],
    "foreign_income_tax_offset": ["Foreign income tax offset", "Foreign tax offset"],
    "franking_credit_tax_offset": ["Franking credit tax offset", "Franking Credits / Tax Offsets", "Franking credit"],
    "tfn_withholding": ["TFN amounts withheld", "TFN withholding tax", "Withholding tax"],
    "non_resident_withholding": ["Non Resident Withholding Amount", "Non-resident withholding", "Non Resident Withholding"],
}


def _is_distribution_tax_evidence(evidence: EvidenceRef, document: SourceDocument | None = None) -> bool:
    document_type = (document.document_type if document else evidence.source_type) or ""
    quote = evidence.quote or ""
    if document_type in {"investment_statement", "prior_year_financial_statements"}:
        return bool(re.search(r"distribution|tax statement|AMIT|payment advice|components of distribution|withholding|franking", quote, re.IGNORECASE))
    return bool(re.search(r"components of distribution|net cash distribution|payment advice|franking credit|foreign income tax offset", quote, re.IGNORECASE))


def _extract_label_amount(quote: str, labels: list[str]) -> str | None:
    money = r"(?P<amount>-?(?:[$€£]\s?)?\d[\d,]*(?:\.\d{2})|-)"
    for label in labels:
        pattern = re.compile(rf"{re.escape(label)}\s*(?:\([^)]*\))?\s*(?:[:\-])?\s*{money}", re.IGNORECASE)
        match = pattern.search(quote)
        if match:
            raw = match.group("amount")
            if raw == "-":
                return "0.00"
            return _clean_money_amount(raw)
    return None


def _extract_distribution_tax_fact(document: SourceDocument, evidence: EvidenceRef) -> dict | None:
    quote = " ".join((evidence.quote or "").split())
    if not _is_distribution_tax_evidence(evidence, document):
        return None
    components = {
        component: amount
        for component, labels in _DISTRIBUTION_COMPONENT_LABELS.items()
        if (amount := _extract_label_amount(quote, labels)) is not None
    }
    payment_date = None
    record_date = None
    payment_match = re.search(r"Payment\s+date:?\s*(?P<date>\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})", quote, re.IGNORECASE)
    record_match = re.search(r"Record\s+date:?\s*(?P<date>\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})", quote, re.IGNORECASE)
    if payment_match:
        payment_date = payment_match.group("date")
    if record_match:
        record_date = record_match.group("date")
    if not components and not payment_date and not record_date:
        return None
    return {
        "document_id": document.document_id,
        "file_path": document.file_path,
        "page": evidence.page,
        "evidence_id": evidence.evidence_id,
        "document_type": document.document_type,
        "payment_date": payment_date,
        "record_date": record_date,
        "components": components,
        "confidence": evidence.confidence,
        "snippet": quote[:300],
    }


def _build_distribution_tax_facts_payload(state: EngagementState) -> dict:
    documents = {doc.document_id: doc for doc in state.source_documents}
    facts: list[dict] = []
    findings: list[dict] = []
    candidate_documents: set[str] = set()
    extracted_documents: set[str] = set()
    for evidence in state.evidence:
        document = documents.get(evidence.document_id or "")
        if not document or not _is_distribution_tax_evidence(evidence, document):
            continue
        candidate_documents.add(document.document_id)
        fact = _extract_distribution_tax_fact(document, evidence)
        if fact:
            facts.append(fact)
            extracted_documents.add(document.document_id)
    for document_id in sorted(candidate_documents - extracted_documents):
        document = documents[document_id]
        findings.append(
            {
                "category": "distribution_tax_fact_extraction_incomplete",
                "document_id": document.document_id,
                "file_path": document.file_path,
                "recommended_action": "Review distribution/tax statement evidence and improve parser or record an accountant decision.",
            }
        )
    return {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "fact_type": "distribution_tax_facts",
        "facts": facts,
        "findings": findings,
        "summary": {
            "distribution_tax_documents": len(candidate_documents),
            "facts_extracted": len(facts),
            "findings": len(findings),
        },
    }


def _format_distribution_tax_facts(payload: dict) -> str:
    lines = [f"# Distribution and Tax Facts — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([
        f"- Distribution/tax documents: {summary['distribution_tax_documents']}",
        f"- Facts extracted: {summary['facts_extracted']}",
        f"- Findings: {summary['findings']}",
        "",
    ])
    if payload["facts"]:
        lines.append("## Extracted distribution/tax facts")
        for fact in payload["facts"]:
            lines.extend([
                f"- `{fact['evidence_id']}` — `{fact['file_path']}` page {fact['page']}",
                f"  - Payment date: {fact['payment_date'] or 'not extracted'}",
                f"  - Record date: {fact['record_date'] or 'not extracted'}",
                f"  - Confidence: {fact['confidence']}",
            ])
            if fact["components"]:
                lines.append("  - Components:")
                for component, amount in sorted(fact["components"].items()):
                    lines.append(f"    - {component}: {amount}")
            lines.append(f"  - Snippet: {fact['snippet']}")
        lines.append("")
    if payload["findings"]:
        lines.append("## Findings needing review")
        for finding in payload["findings"]:
            lines.extend([
                f"- {finding['category']}: `{finding['file_path']}`",
                f"  - Action: {finding['recommended_action']}",
            ])
    return "\n".join(lines).rstrip() + "\n"


def _export_distribution_tax_facts_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    payload = _build_distribution_tax_facts_payload(state)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_distribution_tax_facts(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported distribution/tax facts → {output}")
    print(f"Exported distribution/tax facts JSON → {json_output}")
    return 0 if not payload["findings"] else 1


def _distribution_review_actions(fact: dict) -> list[dict]:
    actions: list[dict] = []
    components = fact.get("components", {}) or {}
    income_components = {
        "cash_distribution",
        "net_cash_distribution",
        "interest",
        "franked_dividends",
        "unfranked_dividends",
        "foreign_income",
        "capital_gains",
    }
    tax_components = {"foreign_income_tax_offset", "franking_credit_tax_offset", "tfn_withholding", "non_resident_withholding"}
    if any(component in components for component in income_components):
        actions.append(
            {
                "category": "distribution_income_mapping_review_required",
                "candidate_treatment": "distribution_income_component_mapping",
                "recommended_action": "Accountant to map distribution income components to CoA/tax labels and confirm taxable/non-taxable presentation.",
            }
        )
    if any(component in components for component in tax_components):
        actions.append(
            {
                "category": "distribution_tax_component_review_required",
                "candidate_treatment": "tax_offset_or_withholding_review",
                "recommended_action": "Accountant to confirm tax offset, franking credit, and withholding treatment before statement or tax workpaper reliance.",
            }
        )
    if fact.get("payment_date") or components.get("net_cash_distribution") or components.get("cash_distribution"):
        actions.append(
            {
                "category": "distribution_bank_match_review_required",
                "candidate_treatment": "match_distribution_to_bank_receipt",
                "recommended_action": "Accountant to match the distribution/payment advice amount to bank receipt evidence or record why no bank match is expected.",
            }
        )
    if not actions:
        actions.append(
            {
                "category": "distribution_accounting_treatment_review_required",
                "candidate_treatment": "distribution_statement_review_required",
                "recommended_action": "Accountant to review distribution/tax source evidence and decide accounting treatment.",
            }
        )
    return actions


def _build_distribution_tax_review_payload(facts_payload: dict) -> dict:
    review_findings: list[dict] = []
    for fact in facts_payload.get("facts", []):
        for action in _distribution_review_actions(fact):
            review_findings.append(
                {
                    **action,
                    "document_id": fact.get("document_id"),
                    "file_path": fact.get("file_path"),
                    "page": fact.get("page"),
                    "evidence_id": fact.get("evidence_id"),
                    "payment_date": fact.get("payment_date"),
                    "record_date": fact.get("record_date"),
                    "components": fact.get("components", {}),
                    "approved": False,
                }
            )
    for finding in facts_payload.get("findings", []):
        review_findings.append(
            {
                "category": "distribution_source_extraction_review_required",
                "candidate_treatment": "source_fact_extraction_incomplete",
                "recommended_action": finding.get("recommended_action") or "Accountant to review source evidence before relying on distribution/tax facts.",
                "document_id": finding.get("document_id"),
                "file_path": finding.get("file_path"),
                "page": finding.get("page"),
                "evidence_id": finding.get("evidence_id"),
                "components": {},
                "approved": False,
            }
        )
    return {
        "engagement_id": facts_payload.get("engagement_id"),
        "entity_name": facts_payload.get("entity_name"),
        "review_type": "distribution_tax_accounting_review",
        "review_findings": review_findings,
        "summary": {
            "facts_reviewed": len(facts_payload.get("facts", [])),
            "source_findings_reviewed": len(facts_payload.get("findings", [])),
            "review_findings": len(review_findings),
            "approved": 0,
        },
    }


def _format_distribution_tax_review(payload: dict) -> str:
    lines = [f"# Distribution and Tax Accounting Review — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([
        f"- Facts reviewed: {summary['facts_reviewed']}",
        f"- Source findings reviewed: {summary['source_findings_reviewed']}",
        f"- Review findings: {summary['review_findings']}",
        f"- Approved automatically: {summary['approved']}",
        "",
    ])
    if payload["review_findings"]:
        lines.append("## Review findings")
        for finding in payload["review_findings"]:
            lines.extend([
                f"- {finding['category']}: `{finding.get('file_path')}`",
                f"  - Candidate treatment: {finding.get('candidate_treatment')}",
                f"  - Approved: {finding.get('approved')}",
                f"  - Evidence: `{finding.get('evidence_id')}` page {finding.get('page')}",
                f"  - Payment date: {finding.get('payment_date') or 'not extracted'}",
                f"  - Components: {json.dumps(finding.get('components', {}), sort_keys=True)}",
                f"  - Action: {finding.get('recommended_action')}",
            ])
    return "\n".join(lines).rstrip() + "\n"


def _export_distribution_tax_review_command(args: argparse.Namespace) -> int:
    facts_payload = json.loads(Path(args.facts).read_text())
    payload = _build_distribution_tax_review_payload(facts_payload)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_distribution_tax_review(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported distribution/tax accounting review → {output}")
    print(f"Exported distribution/tax accounting review JSON → {json_output}")
    return 0 if not payload["review_findings"] else 1


_BROKER_FIELD_LABELS = {
    "transaction_date": ["Transaction Date"],
    "settlement_date": ["Settlement Date"],
    "settlement_amount": ["Settlement Amount", "Total Amount Payable"],
    "consideration": ["Consideration"],
    "quantity": ["Quantity"],
    "price": ["Price"],
    "brokerage": ["Brokerage"],
    "fees": ["Misc Fees & Charges"],
    "gst": ["Total GST Payable"],
    "company": ["Company"],
    "security": ["Security"],
    "market": ["Market"],
    "isin": ["ISIN"],
    "transaction_number": ["Transaction No"],
}


def _extract_label_value(quote: str, labels: list[str]) -> str | None:
    for label in labels:
        pattern = re.compile(rf"{re.escape(label)}\s*:?\s*(?P<value>[^:]+?)(?=\s+[A-Z][A-Za-z /&]+\s*:|$)", re.IGNORECASE)
        match = pattern.search(quote)
        if match:
            value = " ".join(match.group("value").split()).strip(" -")
            return value or None
    return None


def _is_broker_confirmation_evidence(evidence: EvidenceRef, document: SourceDocument | None = None) -> bool:
    document_type = (document.document_type if document else evidence.source_type) or ""
    quote = evidence.quote or ""
    return document_type == "broker_confirmation" or bool(re.search(r"SELL CONFIRMATION|BUY CONFIRMATION|Settlement Amount|Transaction Date", quote, re.IGNORECASE))


def _extract_broker_trade_fact(document: SourceDocument, evidence: EvidenceRef) -> dict | None:
    quote = " ".join((evidence.quote or "").split())
    if not _is_broker_confirmation_evidence(evidence, document):
        return None
    fields = {field: value for field, labels in _BROKER_FIELD_LABELS.items() if (value := _extract_label_value(quote, labels))}
    side = "sell" if re.search(r"SELL CONFIRMATION", quote, re.IGNORECASE) else "buy" if re.search(r"BUY CONFIRMATION", quote, re.IGNORECASE) else None
    has_trade_fact = side or any(field in fields for field in ("transaction_date", "settlement_date", "settlement_amount", "consideration", "quantity"))
    if not has_trade_fact:
        return None
    return {
        "document_id": document.document_id,
        "file_path": document.file_path,
        "page": evidence.page,
        "evidence_id": evidence.evidence_id,
        "side": side,
        "fields": fields,
        "confidence": evidence.confidence,
        "snippet": quote[:300],
    }


def _build_broker_trade_facts_payload(state: EngagementState) -> dict:
    documents = {doc.document_id: doc for doc in state.source_documents}
    facts: list[dict] = []
    findings: list[dict] = []
    candidate_documents: set[str] = set()
    extracted_documents: set[str] = set()
    for evidence in state.evidence:
        document = documents.get(evidence.document_id or "")
        if not document or not _is_broker_confirmation_evidence(evidence, document):
            continue
        candidate_documents.add(document.document_id)
        fact = _extract_broker_trade_fact(document, evidence)
        if fact and len(fact.get("fields", {})) >= 2:
            facts.append(fact)
            extracted_documents.add(document.document_id)
    for document_id in sorted(candidate_documents - extracted_documents):
        document = documents[document_id]
        findings.append({
            "category": "broker_trade_fact_extraction_incomplete",
            "document_id": document.document_id,
            "file_path": document.file_path,
            "recommended_action": "Review broker confirmation evidence and improve parser or record an accountant decision.",
        })
    return {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "fact_type": "broker_trade_facts",
        "facts": facts,
        "findings": findings,
        "summary": {"broker_documents": len(candidate_documents), "facts_extracted": len(facts), "findings": len(findings)},
    }


def _format_broker_trade_facts(payload: dict) -> str:
    lines = [f"# Broker Trade Facts — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([f"- Broker documents: {summary['broker_documents']}", f"- Facts extracted: {summary['facts_extracted']}", f"- Findings: {summary['findings']}", ""])
    if payload["facts"]:
        lines.append("## Extracted broker trade facts")
        for fact in payload["facts"]:
            lines.extend([f"- `{fact['evidence_id']}` — `{fact['file_path']}` page {fact['page']}", f"  - Side: {fact['side'] or 'not extracted'}", f"  - Fields: {json.dumps(fact.get('fields', {}), sort_keys=True)}", f"  - Confidence: {fact['confidence']}"])
    if payload["findings"]:
        lines.extend(["", "## Findings needing review"])
        for finding in payload["findings"]:
            lines.extend([f"- {finding['category']}: `{finding['file_path']}`", f"  - Action: {finding['recommended_action']}"])
    return "\n".join(lines).rstrip() + "\n"


def _export_broker_trade_facts_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    payload = _build_broker_trade_facts_payload(state)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_broker_trade_facts(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported broker trade facts → {output}")
    print(f"Exported broker trade facts JSON → {json_output}")
    return 0 if not payload["findings"] else 1


def _build_broker_trade_review_payload(facts_payload: dict) -> dict:
    review_findings: list[dict] = []
    for fact in facts_payload.get("facts", []):
        for category, treatment, action in [
            ("broker_disposal_classification_review_required", "investment_disposal_or_acquisition_review", "Accountant to confirm buy/sell treatment, investment account mapping, and proceeds/cost handling."),
            ("broker_gain_loss_review_required", "realised_gain_loss_review", "Accountant to confirm cost base, realised gain/loss calculation, and tax/accounting presentation."),
            ("broker_bank_settlement_match_review_required", "match_broker_settlement_to_bank", "Accountant to match settlement amount/date to bank transaction evidence or record why no bank match is expected."),
        ]:
            review_findings.append({**fact, "category": category, "candidate_treatment": treatment, "recommended_action": action, "approved": False})
    for finding in facts_payload.get("findings", []):
        review_findings.append({**finding, "category": "broker_source_extraction_review_required", "candidate_treatment": "source_fact_extraction_incomplete", "approved": False})
    return {"engagement_id": facts_payload.get("engagement_id"), "entity_name": facts_payload.get("entity_name"), "review_type": "broker_trade_accounting_review", "review_findings": review_findings, "summary": {"facts_reviewed": len(facts_payload.get("facts", [])), "source_findings_reviewed": len(facts_payload.get("findings", [])), "review_findings": len(review_findings), "approved": 0}}


def _format_broker_trade_review(payload: dict) -> str:
    lines = [f"# Broker Trade Accounting Review — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([f"- Facts reviewed: {summary['facts_reviewed']}", f"- Source findings reviewed: {summary['source_findings_reviewed']}", f"- Review findings: {summary['review_findings']}", f"- Approved automatically: {summary['approved']}", ""])
    if payload["review_findings"]:
        lines.append("## Review findings")
        for finding in payload["review_findings"]:
            lines.extend([f"- {finding['category']}: `{finding.get('file_path')}`", f"  - Candidate treatment: {finding.get('candidate_treatment')}", f"  - Approved: {finding.get('approved')}", f"  - Evidence: `{finding.get('evidence_id')}` page {finding.get('page')}", f"  - Action: {finding.get('recommended_action')}"])
    return "\n".join(lines).rstrip() + "\n"


def _export_broker_trade_review_command(args: argparse.Namespace) -> int:
    facts_payload = json.loads(Path(args.facts).read_text())
    payload = _build_broker_trade_review_payload(facts_payload)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_broker_trade_review(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported broker trade accounting review → {output}")
    print(f"Exported broker trade accounting review JSON → {json_output}")
    return 0 if not payload["review_findings"] else 1


def _money_value(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.-]", "", str(value))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _date_value(value: str | None) -> str | None:
    parsed = _parse_bank_statement_date(value)
    return parsed.strftime("%Y-%m-%d") if parsed else None


def _bank_transaction_amount(transaction: dict) -> float | None:
    return _money_value(transaction.get("debit") or transaction.get("credit"))


def _source_fact_match_candidates(invoice_payload: dict | None, distribution_payload: dict | None, broker_payload: dict | None) -> list[dict]:
    candidates: list[dict] = []
    for fact in (invoice_payload or {}).get("facts", []):
        candidates.append({
            "source_fact_type": "invoice",
            "amount": _money_value(fact.get("amount_due")),
            "date": _date_value(fact.get("due_date") or fact.get("invoice_date")),
            "evidence_id": fact.get("evidence_id"),
            "file_path": fact.get("file_path"),
            "page": fact.get("page"),
            "label": fact.get("invoice_number") or "invoice",
        })
    for fact in (distribution_payload or {}).get("facts", []):
        components = fact.get("components", {}) or {}
        amount = components.get("net_cash_distribution") or components.get("cash_distribution")
        candidates.append({
            "source_fact_type": "distribution_tax",
            "amount": _money_value(amount),
            "date": _date_value(fact.get("payment_date") or fact.get("record_date")),
            "evidence_id": fact.get("evidence_id"),
            "file_path": fact.get("file_path"),
            "page": fact.get("page"),
            "label": "distribution_tax",
        })
    for fact in (broker_payload or {}).get("facts", []):
        fields = fact.get("fields", {}) or {}
        candidates.append({
            "source_fact_type": "broker_trade",
            "amount": _money_value(fields.get("settlement_amount") or fields.get("consideration")),
            "date": _date_value(fields.get("settlement_date") or fields.get("transaction_date")),
            "evidence_id": fact.get("evidence_id"),
            "file_path": fact.get("file_path"),
            "page": fact.get("page"),
            "label": fact.get("side") or "broker_trade",
        })
    return [candidate for candidate in candidates if candidate.get("amount") is not None]


def _build_source_fact_matches_payload(bank_payload: dict, invoice_payload: dict | None, distribution_payload: dict | None, broker_payload: dict | None) -> dict:
    transactions = bank_payload.get("transactions", [])
    source_facts = _source_fact_match_candidates(invoice_payload, distribution_payload, broker_payload)
    matches: list[dict] = []
    findings: list[dict] = []
    for fact in source_facts:
        candidates = []
        for transaction in transactions:
            amount = _bank_transaction_amount(transaction)
            if amount is None or abs(amount - fact["amount"]) > 0.005:
                continue
            if fact.get("date") and _date_value(transaction.get("transaction_date")) != fact["date"]:
                continue
            candidates.append(transaction)
        if len(candidates) == 1:
            transaction = candidates[0]
            matches.append({
                "source_fact_type": fact["source_fact_type"],
                "source_evidence_id": fact.get("evidence_id"),
                "bank_evidence_id": transaction.get("evidence_id"),
                "amount": f"{fact['amount']:.2f}",
                "date": fact.get("date"),
                "match_type": "exact_amount_date",
                "approved": False,
                "evidence_refs": [ref for ref in [fact.get("evidence_id"), transaction.get("evidence_id")] if ref],
            })
        elif len(candidates) > 1:
            findings.append({
                "category": "ambiguous_source_fact_bank_match",
                "source_fact_type": fact["source_fact_type"],
                "source_evidence_id": fact.get("evidence_id"),
                "candidate_bank_evidence_ids": [item.get("evidence_id") for item in candidates],
                "recommended_action": "Accountant to choose the correct bank transaction or mark the source fact unmatched.",
            })
        else:
            findings.append({
                "category": "source_fact_bank_match_missing",
                "source_fact_type": fact["source_fact_type"],
                "source_evidence_id": fact.get("evidence_id"),
                "amount": f"{fact['amount']:.2f}",
                "date": fact.get("date"),
                "recommended_action": "Accountant to locate bank evidence, adjust matching tolerance, or record why no bank match is expected.",
            })
    return {
        "engagement_id": bank_payload.get("engagement_id"),
        "entity_name": bank_payload.get("entity_name"),
        "match_type": "source_fact_to_bank_transaction",
        "matches": matches,
        "findings": findings,
        "summary": {"bank_transactions": len(transactions), "source_facts": len(source_facts), "matches": len(matches), "findings": len(findings)},
    }


def _format_source_fact_matches(payload: dict) -> str:
    lines = [f"# Source Fact Bank Matches — {payload.get('entity_name') or 'engagement'}", ""]
    summary = payload["summary"]
    lines.extend([f"- Bank transactions: {summary['bank_transactions']}", f"- Source facts: {summary['source_facts']}", f"- Matches: {summary['matches']}", f"- Findings: {summary['findings']}", ""])
    if payload["matches"]:
        lines.append("## Proposed matches")
        for match in payload["matches"]:
            lines.extend([f"- {match['source_fact_type']}: {match['amount']} on {match.get('date') or 'unknown date'}", f"  - Approved: {match['approved']}", f"  - Evidence: {', '.join(match.get('evidence_refs', []))}"])
    if payload["findings"]:
        lines.extend(["", "## Findings needing review"])
        for finding in payload["findings"]:
            lines.extend([f"- {finding['category']}: {finding.get('source_fact_type')}", f"  - Evidence: {finding.get('source_evidence_id')}", f"  - Action: {finding['recommended_action']}"])
    return "\n".join(lines).rstrip() + "\n"


def _load_optional_json(path: str | None) -> dict | None:
    return json.loads(Path(path).read_text()) if path else None


def _match_source_facts_command(args: argparse.Namespace) -> int:
    bank_payload = json.loads(Path(args.bank_transactions).read_text())
    payload = _build_source_fact_matches_payload(
        bank_payload,
        _load_optional_json(getattr(args, "invoice_facts", None)),
        _load_optional_json(getattr(args, "distribution_tax_facts", None)),
        _load_optional_json(getattr(args, "broker_trade_facts", None)),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_source_fact_matches(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported source fact matches → {output}")
    print(f"Exported source fact matches JSON → {json_output}")
    return 0 if not payload["findings"] else 1


def _account_keyword_score(account: ChartAccount, keywords: list[str]) -> int:
    haystack = f"{account.name} {account.type} {account.presentation_group}".lower()
    return sum(1 for keyword in keywords if keyword in haystack)


def _suggest_account_for_source_fact(fact: dict, accounts: list[ChartAccount]) -> ChartAccount | None:
    source_type = fact.get("source_fact_type")
    if source_type == "invoice":
        keywords = ["fee", "expense", "management", "accounting"]
    elif source_type == "distribution_tax":
        keywords = ["distribution", "income", "revenue", "dividend", "interest"]
    elif source_type == "broker_trade":
        keywords = ["investment", "portfolio", "asset", "security"]
    else:
        keywords = []
    scored = sorted(((_account_keyword_score(account, keywords), account) for account in accounts), key=lambda item: (-item[0], item[1].code))
    return scored[0][1] if scored and scored[0][0] > 0 else None


def _build_coa_mapping_payload(state: EngagementState, invoice_payload: dict | None, distribution_payload: dict | None, broker_payload: dict | None) -> dict:
    source_facts = _source_fact_match_candidates(invoice_payload, distribution_payload, broker_payload)
    suggestions: list[dict] = []
    findings: list[dict] = []
    for fact in source_facts:
        account = _suggest_account_for_source_fact(fact, state.chart_accounts)
        if account is None:
            findings.append({
                "category": "coa_mapping_account_missing",
                "source_fact_type": fact.get("source_fact_type"),
                "source_evidence_id": fact.get("evidence_id"),
                "recommended_action": "Accountant to add or select an appropriate CoA account before mapping this source fact.",
            })
            continue
        suggestion = {
            "source_fact_type": fact.get("source_fact_type"),
            "source_evidence_id": fact.get("evidence_id"),
            "candidate_account_id": account.account_id,
            "candidate_account_code": account.code,
            "candidate_account_name": account.name,
            "amount": f"{fact['amount']:.2f}",
            "candidate_treatment": "coa_mapping_suggestion",
            "approved": False,
            "evidence_refs": [fact.get("evidence_id"), account.account_id],
        }
        suggestions.append(suggestion)
        findings.append({
            "category": "coa_mapping_review_required",
            **suggestion,
            "recommended_action": "Accountant to approve, change, or reject this CoA mapping before journal/TB reliance.",
        })
    return {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "mapping_type": "source_fact_to_coa",
        "suggestions": suggestions,
        "findings": findings,
        "summary": {"source_facts": len(source_facts), "suggestions": len(suggestions), "findings": len(findings), "approved": 0},
    }


def _format_coa_mapping_suggestions(payload: dict) -> str:
    lines = [f"# CoA Mapping Suggestions — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([f"- Source facts: {summary['source_facts']}", f"- Suggestions: {summary['suggestions']}", f"- Findings: {summary['findings']}", f"- Approved automatically: {summary['approved']}", ""])
    if payload["suggestions"]:
        lines.append("## Suggested mappings")
        for item in payload["suggestions"]:
            lines.extend([f"- {item['source_fact_type']} {item['amount']} → {item['candidate_account_code']} {item['candidate_account_name']}", f"  - Approved: {item['approved']}", f"  - Evidence: {', '.join(ref for ref in item.get('evidence_refs', []) if ref)}"])
    if payload["findings"]:
        lines.extend(["", "## Findings needing review"])
        for item in payload["findings"]:
            lines.extend([f"- {item['category']}: {item.get('source_fact_type')}", f"  - Action: {item['recommended_action']}"])
    return "\n".join(lines).rstrip() + "\n"


def _suggest_coa_mappings_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    payload = _build_coa_mapping_payload(
        state,
        _load_optional_json(getattr(args, "invoice_facts", None)),
        _load_optional_json(getattr(args, "distribution_tax_facts", None)),
        _load_optional_json(getattr(args, "broker_trade_facts", None)),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_coa_mapping_suggestions(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported CoA mapping suggestions → {output}")
    print(f"Exported CoA mapping suggestions JSON → {json_output}")
    return 0 if not payload["findings"] else 1


def _mapping_id(item: dict) -> str:
    existing = item.get("mapping_id")
    if existing:
        return str(existing)
    return "map_" + hashlib.sha256(f"{item.get('source_evidence_id')}|{item.get('candidate_account_id')}|{item.get('source_fact_type')}".encode()).hexdigest()[:12]


def _export_coa_mapping_template_command(args: argparse.Namespace) -> int:
    mappings = json.loads(Path(args.mappings).read_text())
    decisions = []
    for item in mappings.get("suggestions", []):
        decisions.append({
            "mapping_id": _mapping_id(item),
            "source_fact_type": item.get("source_fact_type"),
            "source_evidence_id": item.get("source_evidence_id"),
            "candidate_account_id": item.get("candidate_account_id"),
            "candidate_account_code": item.get("candidate_account_code"),
            "candidate_account_name": item.get("candidate_account_name"),
            "amount": item.get("amount"),
            "action": "",
            "approved_by": "",
            "rationale": "",
        })
    payload = {"engagement_id": mappings.get("engagement_id"), "mapping_decisions": decisions}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported CoA mapping decision template → {output}")
    return 0


def _validate_mapping_decision(item: dict) -> tuple[str, str, str, str]:
    mapping_id = item.get("mapping_id")
    action = item.get("action")
    rationale = item.get("rationale")
    approved_by = item.get("approved_by")
    if not mapping_id:
        _usage_error("mapping decision missing mapping_id")
    if action not in {"approve", "reject"}:
        _usage_error(f"invalid mapping action for {mapping_id}: {action}")
    if not rationale:
        _usage_error(f"mapping decision for {mapping_id} requires rationale")
    if not approved_by:
        _usage_error(f"mapping decision for {mapping_id} requires approved_by")
    return str(mapping_id), str(action), str(rationale), str(approved_by)


def _apply_coa_mapping_decisions_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    mappings = json.loads(Path(args.mappings).read_text())
    decisions_payload = json.loads(Path(args.decisions).read_text())
    mapping_by_id = {_mapping_id(item): item for item in mappings.get("suggestions", [])}
    parsed = [_validate_mapping_decision(item) for item in decisions_payload.get("mapping_decisions", []) if item.get("action")]
    for mapping_id, _action, _rationale, _approved_by in parsed:
        if mapping_id not in mapping_by_id:
            _usage_error(f"Unknown mapping_id: {mapping_id}")
    applied_rows = []
    approved = 0
    rejected = 0
    for mapping_id, action, rationale, approved_by in parsed:
        mapping = mapping_by_id[mapping_id]
        selected = "approve_coa_mapping" if action == "approve" else "reject_coa_mapping"
        if action == "approve":
            approved += 1
        else:
            rejected += 1
        decision = AccountantDecision(
            decision_id=f"decision_{selected}_{len(state.decisions) + 1:04d}",
            question=f"{selected} {mapping_id}?",
            selected_option=selected,
            rationale=rationale,
            status=DecisionStatus.APPROVED,
            approved_by=approved_by,
            evidence_refs=[ref for ref in mapping.get("evidence_refs", []) if ref],
        )
        state.decisions.append(decision)
        applied_rows.append({
            "mapping_id": mapping_id,
            "action": action,
            "decision_id": decision.decision_id,
            "source_fact_type": mapping.get("source_fact_type"),
            "source_evidence_id": mapping.get("source_evidence_id"),
            "candidate_account_id": mapping.get("candidate_account_id"),
            "candidate_account_code": mapping.get("candidate_account_code"),
            "candidate_account_name": mapping.get("candidate_account_name"),
            "amount": mapping.get("amount"),
            "evidence_refs": mapping.get("evidence_refs", []),
        })
    save_engagement_state(state_path, state)
    payload = {"engagement_id": state.engagement_id, "applied_mappings": applied_rows, "summary": {"approved": approved, "rejected": rejected, "applied": len(applied_rows)}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Applied {len(applied_rows)} CoA mapping decisions → {output}")
    return 0


def _journal_accounts_for_mapping(account: ChartAccount) -> tuple[str, str]:
    if account.type in {"expense", "asset"}:
        return account.account_id, "pending_review_offset"
    if account.type in {"income", "revenue", "liability", "equity"}:
        return "pending_review_offset", account.account_id
    return account.account_id, "pending_review_offset"


def _format_journal_proposals(payload: dict) -> str:
    lines = [f"# Journal Proposals — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([f"- Proposals created: {summary['proposals_created']}", f"- Blocked mappings: {summary['blocked_mappings']}", f"- Approved automatically: {summary['approved']}", ""])
    if payload["proposals"]:
        lines.append("## Proposals pending accountant review")
        for item in payload["proposals"]:
            lines.extend([f"- {item['adjustment_id']} [{item['status']}] {item['description']}", f"  - DR {item['debit_account']} / CR {item['credit_account']}", f"  - Amount: {item['amount']}", f"  - Evidence: {', '.join(item.get('source_evidence_refs', []))}"])
    if payload["findings"]:
        lines.extend(["", "## Findings needing review"])
        for item in payload["findings"]:
            lines.extend([f"- {item['category']}: {item.get('mapping_id')}", f"  - Action: {item['recommended_action']}"])
    return "\n".join(lines).rstrip() + "\n"


def _propose_journals_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    applied_payload = json.loads(Path(args.applied_mappings).read_text())
    account_by_id = {account.account_id: account for account in state.chart_accounts}
    proposals: list[AdjustmentProposal] = []
    findings: list[dict] = []
    for item in applied_payload.get("applied_mappings", []):
        if item.get("action") != "approve":
            continue
        account_id = item.get("candidate_account_id")
        account = account_by_id.get(account_id)
        if account is None:
            findings.append({"category": "journal_proposal_account_missing", "mapping_id": item.get("mapping_id"), "candidate_account_id": account_id, "recommended_action": "Resolve or re-approve the CoA mapping before proposing a journal."})
            continue
        amount = _clean_money_amount(str(item.get("amount", ""))) or "0.00"
        debit_account, credit_account = _journal_accounts_for_mapping(account)
        evidence_refs = []
        for ref in list(item.get("evidence_refs", [])) + [item.get("source_evidence_id"), item.get("candidate_account_id"), item.get("decision_id")]:
            if ref and ref not in evidence_refs:
                evidence_refs.append(ref)
        adjustment_id = f"journal_{item.get('mapping_id', len(proposals) + 1)}"
        proposal = AdjustmentProposal(
            adjustment_id=adjustment_id,
            description=f"Proposed {item.get('source_fact_type')} journal from approved CoA mapping {item.get('mapping_id')}",
            debit_account=debit_account,
            credit_account=credit_account,
            amount=amount,
            date=getattr(args, "date", None) or state.fy_end,
            source_evidence_refs=evidence_refs,
            status="pending_review",
        )
        proposals.append(proposal)
    state.adjustment_proposals = [item for item in state.adjustment_proposals if not item.adjustment_id.startswith("journal_map_") and not item.adjustment_id.startswith("journal_map") and not item.adjustment_id.startswith("journal_")]
    state.adjustment_proposals.extend(proposals)
    if proposals:
        state.adjustment_review_status = "pending_review"
    save_engagement_state(state_path, state)
    payload = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "proposals": [proposal.model_dump() for proposal in proposals], "findings": findings, "summary": {"proposals_created": len(proposals), "blocked_mappings": len(findings), "approved": 0}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_journal_proposals(payload))
    output.with_suffix(".json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported journal proposals → {output}")
    return 1 if proposals or findings else 0


def _export_journal_decision_template_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    decisions = []
    for proposal in sorted(state.adjustment_proposals, key=lambda item: item.adjustment_id):
        decisions.append({
            "adjustment_id": proposal.adjustment_id,
            "description": proposal.description,
            "date": proposal.date,
            "debit_account": proposal.debit_account,
            "credit_account": proposal.credit_account,
            "amount": proposal.amount,
            "source_evidence_refs": proposal.source_evidence_refs,
            "action": "",
            "offset_account_id": "",
            "approved_by": "",
            "rationale": "",
        })
    payload = {"engagement_id": state.engagement_id, "journal_decisions": decisions}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported journal decision template → {output}")
    return 0


def _validate_journal_decision(item: dict) -> tuple[str, str, str, str, str | None]:
    adjustment_id = item.get("adjustment_id")
    action = item.get("action")
    rationale = item.get("rationale")
    approved_by = item.get("approved_by")
    offset_account_id = item.get("offset_account_id") or None
    if not adjustment_id:
        _usage_error("journal decision missing adjustment_id")
    if action not in {"approve", "reject"}:
        _usage_error(f"invalid journal action for {adjustment_id}: {action}")
    if not rationale:
        _usage_error(f"journal decision for {adjustment_id} requires rationale")
    if not approved_by:
        _usage_error(f"journal decision for {adjustment_id} requires approved_by")
    return str(adjustment_id), str(action), str(rationale), str(approved_by), offset_account_id


def _apply_journal_decisions_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    payload = json.loads(Path(args.decisions).read_text())
    proposal_by_id = {proposal.adjustment_id: proposal for proposal in state.adjustment_proposals}
    account_by_id = {account.account_id: account for account in state.chart_accounts}
    parsed = [_validate_journal_decision(item) for item in payload.get("journal_decisions", []) if item.get("action")]
    for adjustment_id, action, _rationale, _approved_by, offset_account_id in parsed:
        proposal = proposal_by_id.get(adjustment_id)
        if proposal is None:
            _usage_error(f"Unknown adjustment_id: {adjustment_id}")
        if action == "approve" and "pending_review_offset" in {proposal.debit_account, proposal.credit_account}:
            if not offset_account_id:
                _usage_error(f"journal decision for {adjustment_id} requires offset_account_id before approval")
            if offset_account_id not in account_by_id:
                _usage_error(f"Unknown offset_account_id: {offset_account_id}")
    applied_rows = []
    approved = 0
    rejected = 0
    for adjustment_id, action, rationale, approved_by, offset_account_id in parsed:
        proposal = proposal_by_id[adjustment_id]
        if action == "approve" and offset_account_id:
            if proposal.debit_account == "pending_review_offset":
                proposal.debit_account = offset_account_id
            if proposal.credit_account == "pending_review_offset":
                proposal.credit_account = offset_account_id
        proposal.status = "approved" if action == "approve" else "rejected"
        selected = "approve_journal" if action == "approve" else "reject_journal"
        decision = AccountantDecision(
            decision_id=f"decision_{selected}_{len(state.decisions) + 1:04d}",
            question=f"{selected} {adjustment_id}?",
            selected_option=selected,
            rationale=rationale,
            status=DecisionStatus.APPROVED,
            approved_by=approved_by,
            evidence_refs=proposal.source_evidence_refs,
        )
        state.decisions.append(decision)
        proposal.decision_id = decision.decision_id
        if action == "approve":
            approved += 1
        else:
            rejected += 1
        applied_rows.append({"adjustment_id": adjustment_id, "action": action, "decision_id": decision.decision_id, "debit_account": proposal.debit_account, "credit_account": proposal.credit_account, "amount": proposal.amount})
    if state.adjustment_proposals and not [proposal for proposal in state.adjustment_proposals if proposal.status != "approved"]:
        state.adjustment_review_status = "approved"
    elif applied_rows:
        state.adjustment_review_status = "pending_review"
    save_engagement_state(state_path, state)
    output_payload = {"engagement_id": state.engagement_id, "applied_journal_decisions": applied_rows, "summary": {"approved": approved, "rejected": rejected, "applied": len(applied_rows)}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(output_payload, indent=2, sort_keys=True))
    print(f"Applied {len(applied_rows)} journal decisions → {output}")
    return 0


def _money_decimal(value: str | int | float | None) -> Decimal:
    cleaned = _clean_money_amount(str(value or "0")) or "0.00"
    return Decimal(cleaned.replace(",", ""))


def _money_string(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}"


def _format_tb_impact_preview(payload: dict) -> str:
    lines = [f"# Trial Balance Impact Preview — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([f"- Approved journals included: {summary['approved_journals']}", f"- Excluded journals: {summary['excluded_journals']}", f"- Findings: {summary['findings']}", f"- Balanced: {summary['balanced']}", ""])
    lines.append("## Account impacts")
    if payload["account_impacts"]:
        for account_id, impact in sorted(payload["account_impacts"].items()):
            lines.extend([f"- {account_id}", f"  - Debits: {impact['debits']}", f"  - Credits: {impact['credits']}", f"  - Net debit/(credit): {impact['net_debit_credit']}", f"  - Journals: {', '.join(impact['journal_refs'])}"])
    else:
        lines.append("- No approved journal impacts.")
    if payload["findings"]:
        lines.extend(["", "## Findings"])
        for item in payload["findings"]:
            lines.extend([f"- {item['category']}: {item.get('adjustment_id')}", f"  - Action: {item['recommended_action']}"])
    return "\n".join(lines).rstrip() + "\n"


def _preview_tb_impact_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    account_ids = {account.account_id for account in state.chart_accounts}
    impacts: dict[str, dict[str, object]] = {}
    findings: list[dict] = []
    approved_journals = 0
    excluded_journals = 0
    total_debits = Decimal("0.00")
    total_credits = Decimal("0.00")

    def ensure_account(account_id: str) -> dict[str, object]:
        return impacts.setdefault(account_id, {"debits": Decimal("0.00"), "credits": Decimal("0.00"), "journal_refs": []})

    for proposal in state.adjustment_proposals:
        if proposal.status != "approved":
            excluded_journals += 1
            findings.append({"category": "tb_preview_unapproved_journal_excluded", "adjustment_id": proposal.adjustment_id, "recommended_action": "Approve or reject this journal before TB reliance."})
            continue
        if "pending_review_offset" in {proposal.debit_account, proposal.credit_account}:
            findings.append({"category": "tb_preview_placeholder_offset", "adjustment_id": proposal.adjustment_id, "recommended_action": "Resolve pending_review_offset before TB impact reliance."})
            continue
        missing = [account_id for account_id in [proposal.debit_account, proposal.credit_account] if account_id not in account_ids]
        if missing:
            findings.append({"category": "tb_preview_missing_account", "adjustment_id": proposal.adjustment_id, "missing_accounts": missing, "recommended_action": "Resolve missing CoA account IDs before TB impact reliance."})
            continue
        amount = _money_decimal(proposal.amount)
        debit_impact = ensure_account(proposal.debit_account)
        credit_impact = ensure_account(proposal.credit_account)
        debit_impact["debits"] = debit_impact["debits"] + amount
        credit_impact["credits"] = credit_impact["credits"] + amount
        debit_impact["journal_refs"].append(proposal.adjustment_id)
        credit_impact["journal_refs"].append(proposal.adjustment_id)
        total_debits += amount
        total_credits += amount
        approved_journals += 1
    account_impacts = {}
    for account_id, impact in impacts.items():
        debits = impact["debits"]
        credits = impact["credits"]
        account_impacts[account_id] = {"debits": _money_string(debits), "credits": _money_string(credits), "net_debit_credit": _money_string(debits - credits), "journal_refs": impact["journal_refs"]}
    balanced = total_debits == total_credits and not any(item["category"] in {"tb_preview_placeholder_offset", "tb_preview_missing_account"} for item in findings)
    payload = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "account_impacts": account_impacts, "findings": findings, "summary": {"approved_journals": approved_journals, "excluded_journals": excluded_journals, "findings": len(findings), "balanced": balanced}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_tb_impact_preview(payload))
    output.with_suffix(".json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported TB impact preview → {output}")
    return 0 if balanced and not findings else 1


def _format_reviewed_journals_markdown(payload: dict) -> str:
    lines = [f"# Reviewed Journals — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([f"- Exported: {summary['exported']}", f"- Excluded pending/rejected: {summary['excluded_pending_or_rejected']}", ""])
    if payload["journals"]:
        lines.append("## Approved journals")
        for item in payload["journals"]:
            lines.extend([f"- {item['adjustment_id']} {item['date']} {item['description']}", f"  - DR {item['debit_account']} / CR {item['credit_account']}", f"  - Amount: {item['amount']}", f"  - Decision: {item.get('decision_id') or ''}", f"  - Evidence: {', '.join(item.get('source_evidence_refs', []))}"])
    else:
        lines.append("No approved journals exported.")
    return "\n".join(lines).rstrip() + "\n"


def _export_reviewed_journals_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    approved = [proposal for proposal in state.adjustment_proposals if proposal.status == "approved"]
    for proposal in approved:
        if "pending_review_offset" in {proposal.debit_account, proposal.credit_account}:
            print(f"Cannot export approved journal {proposal.adjustment_id}: pending_review_offset remains", file=sys.stderr)
            return 1
    rows = [
        {
            "adjustment_id": proposal.adjustment_id,
            "date": proposal.date,
            "description": proposal.description,
            "debit_account": proposal.debit_account,
            "credit_account": proposal.credit_account,
            "amount": proposal.amount,
            "decision_id": proposal.decision_id,
            "source_evidence_refs": proposal.source_evidence_refs,
        }
        for proposal in approved
    ]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "journals": rows, "summary": {"exported": len(rows), "excluded_pending_or_rejected": len(state.adjustment_proposals) - len(rows)}}
    (output_dir / "reviewed_journals.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    fieldnames = ["adjustment_id", "date", "description", "debit_account", "credit_account", "amount", "decision_id", "source_evidence_refs"]
    with (output_dir / "reviewed_journals.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "source_evidence_refs": ";".join(row["source_evidence_refs"])})
    (output_dir / "reviewed_journals.md").write_text(_format_reviewed_journals_markdown(payload))
    print(f"Exported reviewed journals → {output_dir}")
    return 0


def _format_post_journal_tb(payload: dict) -> str:
    lines = [f"# Post-Journal Trial Balance — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([
        f"- Accounts: {summary['accounts']}",
        f"- Journals included: {summary['journals_included']}",
        f"- Excluded journals: {summary['excluded_journals']}",
        f"- Balanced movements: {summary['balanced_movements']}",
        f"- Findings: {summary['findings']}",
        "",
        "## Accounts",
    ])
    for row in payload["accounts"]:
        lines.append(f"- {row['account_id']} {row['code']} {row['name']} opening={row['opening_balance']} debits={row['debits']} credits={row['credits']} ending={row['ending_balance']}")
    if payload["findings"]:
        lines.extend(["", "## Findings"])
        for item in payload["findings"]:
            lines.append(f"- {item['category']}: {item.get('detail', item.get('adjustment_id', ''))}")
    return "\n".join(lines).rstrip() + "\n"


def _build_post_journal_tb_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    reviewed = json.loads(Path(args.reviewed_journals).read_text())
    reviewed_ids = {row.get("adjustment_id") for row in reviewed.get("journals", [])}
    journals = [proposal for proposal in state.adjustment_proposals if proposal.status == "approved" and proposal.adjustment_id in reviewed_ids]
    findings: list[dict] = []
    account_rows = []
    account_by_id = {account.account_id: account for account in state.chart_accounts}
    movement_by_account: dict[str, dict[str, Decimal]] = {account.account_id: {"debits": Decimal("0.00"), "credits": Decimal("0.00")} for account in state.chart_accounts}
    total_debits = Decimal("0.00")
    total_credits = Decimal("0.00")
    for proposal in journals:
        if "pending_review_offset" in {proposal.debit_account, proposal.credit_account}:
            findings.append({"category": "post_journal_tb_placeholder_offset", "adjustment_id": proposal.adjustment_id, "detail": "Approved journal still has pending_review_offset."})
            continue
        if proposal.debit_account not in account_by_id or proposal.credit_account not in account_by_id:
            findings.append({"category": "post_journal_tb_missing_account", "adjustment_id": proposal.adjustment_id, "detail": "Approved journal references a missing account."})
            continue
        amount = _money_decimal(proposal.amount)
        movement_by_account[proposal.debit_account]["debits"] += amount
        movement_by_account[proposal.credit_account]["credits"] += amount
        total_debits += amount
        total_credits += amount
    for account in sorted(state.chart_accounts, key=lambda item: item.account_id):
        opening = _money_decimal(account.opening_balance)
        debits = movement_by_account[account.account_id]["debits"]
        credits = movement_by_account[account.account_id]["credits"]
        ending = opening + debits - credits
        account_rows.append({
            "account_id": account.account_id,
            "code": account.code,
            "name": account.name,
            "type": account.type,
            "presentation_group": account.presentation_group,
            "opening_balance": _money_string(opening),
            "debits": _money_string(debits),
            "credits": _money_string(credits),
            "ending_balance": _money_string(ending),
        })
    excluded = len([p for p in state.adjustment_proposals if p.status != "approved"]) + len([p for p in state.adjustment_proposals if p.status == "approved" and p.adjustment_id not in reviewed_ids])
    balanced = total_debits == total_credits and not findings
    payload = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "accounts": account_rows, "findings": findings, "summary": {"accounts": len(account_rows), "journals_included": len(journals), "excluded_journals": excluded, "balanced_movements": balanced, "findings": len(findings)}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_post_journal_tb(payload))
    output.with_suffix(".json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported post-journal trial balance → {output}")
    return 0 if balanced else 1


def _statement_for_account_type(account_type: str) -> str | None:
    if account_type in {"asset", "liability", "equity"}:
        return "balance_sheet"
    if account_type in {"income", "revenue", "expense"}:
        return "profit_and_loss"
    return None


def _format_statement_mapping(payload: dict) -> str:
    lines = [f"# Statement Line Mapping Preview — {payload['entity_name']}", ""]
    lines.extend([f"- Mapped accounts: {payload['summary']['mapped_accounts']}", f"- Findings: {payload['summary']['findings']}", "", "## Mapped accounts"])
    for row in payload["mapped_accounts"]:
        lines.append(f"- {row['account_id']} → {row['statement']} / {row['line']} ending={row['ending_balance']}")
    if payload["findings"]:
        lines.extend(["", "## Findings"])
        for item in payload["findings"]:
            lines.append(f"- {item['category']}: {item.get('account_id', '')}")
    return "\n".join(lines).rstrip() + "\n"


def _preview_statement_line_mapping_command(args: argparse.Namespace) -> int:
    tb = json.loads(Path(args.post_journal_tb).read_text())
    mapped = []
    findings = []
    for account in tb.get("accounts", []):
        ending = _money_decimal(account.get("ending_balance"))
        if ending == Decimal("0.00"):
            continue
        statement = _statement_for_account_type(account.get("type", ""))
        if not statement or not account.get("presentation_group"):
            findings.append({"category": "statement_mapping_unmapped_account", "account_id": account.get("account_id"), "recommended_action": "Assign account type and presentation group before rendering draft statements."})
            continue
        mapped.append({"account_id": account["account_id"], "code": account.get("code"), "name": account.get("name"), "statement": statement, "line": account.get("presentation_group"), "ending_balance": account.get("ending_balance"), "type": account.get("type")})
    payload = {"engagement_id": tb.get("engagement_id"), "entity_name": tb.get("entity_name"), "mapped_accounts": mapped, "findings": findings, "summary": {"mapped_accounts": len(mapped), "findings": len(findings)}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_statement_mapping(payload))
    output.with_suffix(".json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported statement line mapping preview → {output}")
    return 0 if not findings else 1


def _format_draft_statements(payload: dict) -> str:
    lines = [f"# Draft Financial Statements — {payload['entity_name']}", "", "Status: internal_review_only", ""]
    for section in ["profit_and_loss", "balance_sheet"]:
        lines.extend([f"## {section.replace('_', ' ').title()}"])
        for line, amount in sorted(payload[section].items()):
            lines.append(f"- {line}: {amount}")
        lines.append("")
    lines.extend(["## Control references"] + [f"- {ref}" for ref in payload.get("control_refs", [])])
    return "\n".join(lines).rstrip() + "\n"


def _render_draft_statements_from_tb_command(args: argparse.Namespace) -> int:
    tb = json.loads(Path(args.post_journal_tb).read_text())
    mapping = json.loads(Path(args.mapping).read_text())
    findings = list(tb.get("findings", [])) + list(mapping.get("findings", []))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    pl: dict[str, Decimal] = {}
    bs: dict[str, Decimal] = {}
    for row in mapping.get("mapped_accounts", []):
        target = pl if row["statement"] == "profit_and_loss" else bs
        target[row["line"]] = target.get(row["line"], Decimal("0.00")) + _money_decimal(row.get("ending_balance"))
    payload = {"engagement_id": tb.get("engagement_id"), "entity_name": tb.get("entity_name"), "status": "internal_review_only", "profit_and_loss": {k: _money_string(v) for k, v in pl.items()}, "balance_sheet": {k: _money_string(v) for k, v in bs.items()}, "control_refs": [str(Path(args.post_journal_tb)), str(Path(args.mapping))], "findings": findings, "summary": {"mapping_findings": len(mapping.get("findings", [])), "tb_findings": len(tb.get("findings", []))}}
    (output_dir / "draft_statements.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    (output_dir / "draft_statements.md").write_text(_format_draft_statements(payload))
    print(f"Exported draft statements → {output_dir}")
    return 0 if not findings else 1


def _inspect_statement_chain_readiness_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    artifact_dir = Path(args.artifact_dir)
    required = ["post_journal_trial_balance.json", "statement_line_mapping.json", "draft_statements/draft_statements.json", "reviewed_journals/reviewed_journals.json"]
    missing = [name for name in required if not (artifact_dir / name).exists()]
    blockers = []
    if state.coa_review_status != "approved":
        blockers.append("CoA is not approved")
    if state.adjustment_proposals and any(p.status != "approved" for p in state.adjustment_proposals):
        blockers.append("Journal proposals remain pending/rejected")
    if not _final_signoff_decision(state):
        blockers.append("Final sign-off missing")
    ready = not missing and not blockers
    payload = {"engagement_id": state.engagement_id, "statement_chain_ready": ready, "missing_artifacts": missing, "blockers": blockers}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Statement chain ready: {'YES' if ready else 'NO'}")
        for item in missing:
            print(f"Missing: {item}")
        for item in blockers:
            print(f"Blocker: {item}")
    return 0 if ready else 1


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _draft_statement_review_decision(state: EngagementState) -> AccountantDecision | None:
    return next((decision for decision in reversed(state.decisions) if decision.selected_option == "approve_draft_statements" and decision.status == DecisionStatus.APPROVED), None)


def _export_draft_statement_review_template_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    draft_path = Path(args.draft)
    draft = json.loads(draft_path.read_text())
    payload = {
        "engagement_id": state.engagement_id,
        "draft_artifact": str(draft_path),
        "draft_sha256": _file_sha256(draft_path),
        "draft_status": draft.get("status"),
        "draft_findings": len(draft.get("findings", [])),
        "decision": {"action": "", "approved_by": "", "rationale": ""},
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported draft statement review template → {output}")
    return 0


def _apply_draft_statement_review_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    draft_path = Path(args.draft)
    draft = json.loads(draft_path.read_text())
    payload = json.loads(Path(args.decision).read_text())
    if payload.get("engagement_id") not in {None, state.engagement_id}:
        _usage_error("Draft statement review engagement_id does not match state")
    if payload.get("draft_sha256") and payload.get("draft_sha256") != _file_sha256(draft_path):
        _usage_error("Draft statement artifact hash does not match review template")
    decision_payload = payload.get("decision", {})
    action = decision_payload.get("action")
    if not action:
        applied = {"engagement_id": state.engagement_id, "draft_status": draft.get("status"), "summary": {"applied": 0}}
    else:
        if action not in {"approve", "reject"}:
            _usage_error(f"invalid draft statement review action: {action}")
        if not decision_payload.get("approved_by"):
            _usage_error("draft statement review requires approved_by")
        if not decision_payload.get("rationale"):
            _usage_error("draft statement review requires rationale")
        if action == "approve" and draft.get("findings"):
            _usage_error("Cannot approve draft statements while draft findings remain")
        selected = "approve_draft_statements" if action == "approve" else "reject_draft_statements"
        decision = AccountantDecision(
            decision_id=f"decision_{selected}_{len(state.decisions) + 1:04d}",
            question="Approve internal-review draft statements?",
            selected_option=selected,
            rationale=decision_payload["rationale"],
            status=DecisionStatus.APPROVED,
            approved_by=decision_payload["approved_by"],
            evidence_refs=[str(draft_path), payload.get("draft_sha256", _file_sha256(draft_path))],
        )
        state.decisions.append(decision)
        save_engagement_state(state_path, state)
        applied = {"engagement_id": state.engagement_id, "draft_status": "accountant_approved_draft" if action == "approve" else "draft_rejected", "decision_id": decision.decision_id, "summary": {"applied": 1}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(applied, indent=2, sort_keys=True))
    print(f"Applied draft statement review → {output}")
    return 0 if applied["summary"]["applied"] else 1


def _release_candidate_artifact_paths(artifact_dir: Path) -> list[Path]:
    return [
        artifact_dir / "reviewed_journals" / "reviewed_journals.json",
        artifact_dir / "reviewed_journals" / "reviewed_journals.md",
        artifact_dir / "post_journal_trial_balance.json",
        artifact_dir / "post_journal_trial_balance.md",
        artifact_dir / "statement_line_mapping.json",
        artifact_dir / "statement_line_mapping.md",
        artifact_dir / "draft_statements" / "draft_statements.json",
        artifact_dir / "draft_statements" / "draft_statements.md",
    ]


def _build_release_candidate_package_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    artifact_dir = Path(args.artifact_dir)
    readiness = {"missing_artifacts": [], "blockers": []}
    for path in _release_candidate_artifact_paths(artifact_dir):
        if not path.exists():
            readiness["missing_artifacts"].append(str(path.relative_to(artifact_dir)))
    if state.coa_review_status != "approved":
        readiness["blockers"].append("CoA is not approved")
    if state.adjustment_proposals and any(p.status != "approved" for p in state.adjustment_proposals):
        readiness["blockers"].append("Journal proposals remain pending/rejected")
    if not _draft_statement_review_decision(state):
        readiness["blockers"].append("Draft statements are not accountant-approved")
    if readiness["missing_artifacts"] or readiness["blockers"]:
        print(json.dumps(readiness, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    for path in _release_candidate_artifact_paths(artifact_dir):
        rel = str(path.relative_to(artifact_dir))
        artifacts[rel] = {"path": str(path), "sha256": _file_sha256(path)}
    draft_decision = _draft_statement_review_decision(state)
    manifest = {"engagement_id": state.engagement_id, "status": "release_candidate", "source_state_hash": state_hash(state), "created_at": datetime.now(timezone.utc).isoformat(), "artifacts": artifacts, "draft_decision_id": draft_decision.decision_id if draft_decision else None}
    (output_dir / "release_candidate_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (output_dir / "README.md").write_text("# Release Candidate Package\n\nStatus: release_candidate\n\n" + "\n".join(f"- {name}: {info['sha256']}" for name, info in artifacts.items()) + "\n")
    print(f"Built release candidate package → {output_dir}")
    return 0


def _verify_release_candidate_command(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text())
    findings = []
    for name, info in manifest.get("artifacts", {}).items():
        path = Path(info["path"])
        if not path.exists():
            findings.append({"category": "missing_artifact", "artifact": name})
            continue
        actual = _file_sha256(path)
        if actual != info.get("sha256"):
            findings.append({"category": "hash_mismatch", "artifact": name, "expected": info.get("sha256"), "actual": actual})
    payload = {"manifest": str(manifest_path), "verified": not findings, "findings": findings}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if not findings else 1


def _export_final_release_manifest_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    signoff = _final_signoff_decision(state)
    if signoff is None:
        print("Cannot export final release without final sign-off", file=sys.stderr)
        return 1
    manifest_path = Path(args.release_candidate)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("source_state_hash") != state_hash(state):
        print("Cannot export final release: release candidate state hash is stale", file=sys.stderr)
        return 1
    verify_payload = []
    for name, info in manifest.get("artifacts", {}).items():
        path = Path(info["path"])
        if not path.exists() or _file_sha256(path) != info.get("sha256"):
            verify_payload.append(name)
    if verify_payload:
        print(f"Cannot export final release: release candidate verification failed for {verify_payload}", file=sys.stderr)
        return 1
    payload = {"engagement_id": state.engagement_id, "status": "final_release_manifest", "release_candidate_manifest": str(manifest_path), "release_candidate_sha256": _file_sha256(manifest_path), "final_signoff_decision_id": signoff.decision_id, "source_state_hash": state_hash(state), "created_at": datetime.now(timezone.utc).isoformat()}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported final release manifest → {output}")
    return 0


def _build_accountant_review_workbench(state: EngagementState, artifact_dir: Path) -> dict:
    draft_path = artifact_dir / "draft_statements" / "draft_statements.json"
    draft = json.loads(draft_path.read_text()) if draft_path.exists() else {}
    return {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "artifact_dir": str(artifact_dir),
        "sections": {
            "coa_accounts": [
                {
                    "account_id": account.account_id,
                    "code": account.code,
                    "name": account.name,
                    "type": account.type,
                    "presentation_group": account.presentation_group,
                    "opening_balance": account.opening_balance,
                    "status": account.status,
                    "action": "",
                    "approved_by": "",
                    "rationale": "",
                }
                for account in sorted(state.chart_accounts, key=lambda item: item.account_id)
                if account.status != "approved"
            ],
            "journal_decisions": [
                {
                    "adjustment_id": proposal.adjustment_id,
                    "description": proposal.description,
                    "date": proposal.date,
                    "debit_account": proposal.debit_account,
                    "credit_account": proposal.credit_account,
                    "amount": proposal.amount,
                    "status": proposal.status,
                    "source_evidence_refs": proposal.source_evidence_refs,
                    "action": "",
                    "offset_account_id": "",
                    "approved_by": "",
                    "rationale": "",
                }
                for proposal in sorted(state.adjustment_proposals, key=lambda item: item.adjustment_id)
                if proposal.status != "approved"
            ],
            "draft_statement_review": {
                "draft_artifact": str(draft_path) if draft_path.exists() else "",
                "draft_sha256": _file_sha256(draft_path) if draft_path.exists() else "",
                "draft_status": draft.get("status", "missing"),
                "draft_findings": len(draft.get("findings", [])) if isinstance(draft.get("findings", []), list) else 0,
                "decision": {"action": "", "approved_by": "", "rationale": ""},
            },
            "final_signoff": {"action": "", "approved_by": "", "rationale": "", "release_candidate_manifest": str(artifact_dir / "release_candidate" / "release_candidate_manifest.json")},
        },
        "artifact_links": {
            "review_packet": str(artifact_dir / "review_packet"),
            "post_journal_trial_balance": str(artifact_dir / "post_journal_trial_balance.json"),
            "statement_line_mapping": str(artifact_dir / "statement_line_mapping.json"),
            "draft_statements": str(draft_path),
            "release_candidate": str(artifact_dir / "release_candidate" / "release_candidate_manifest.json"),
        },
    }


def _format_accountant_review_workbench(payload: dict) -> str:
    sections = payload["sections"]
    lines = [f"# Accountant Review Workbench — {payload['entity_name']}", ""]
    lines.extend(["## CoA accounts", f"- Pending accounts: {len(sections['coa_accounts'])}"])
    for item in sections["coa_accounts"]:
        lines.append(f"- {item['account_id']} {item['code']} {item['name']} status={item['status']}")
    lines.extend(["", "## Journal decisions", f"- Pending journals: {len(sections['journal_decisions'])}"])
    for item in sections["journal_decisions"]:
        lines.append(f"- {item['adjustment_id']} DR {item['debit_account']} / CR {item['credit_account']} amount={item['amount']}")
    draft = sections["draft_statement_review"]
    lines.extend(["", "## Draft statement review", f"- Draft status: {draft['draft_status']}", f"- Draft findings: {draft['draft_findings']}"])
    lines.extend(["", "## Required fields", "- action: approve/reject where applicable", "- approved_by", "- rationale", "- offset_account_id for journal approvals with pending_review_offset"])
    return "\n".join(lines).rstrip() + "\n"


def _export_accountant_review_workbench_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    artifact_dir = Path(args.artifact_dir)
    payload = _build_accountant_review_workbench(state, artifact_dir)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    output.with_suffix(".md").write_text(_format_accountant_review_workbench(payload))
    print(f"Exported accountant review workbench → {output}")
    return 0


def _require_review_fields(item: dict, label: str) -> tuple[str, str]:
    if not item.get("approved_by"):
        _usage_error(f"{label} requires approved_by")
    if not item.get("rationale"):
        _usage_error(f"{label} requires rationale")
    return str(item["approved_by"]), str(item["rationale"])


def _apply_accountant_review_workbench_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    artifact_dir = Path(args.artifact_dir)
    payload = json.loads(Path(args.workbench).read_text())
    if payload.get("engagement_id") not in {None, state.engagement_id}:
        _usage_error("workbench engagement_id does not match state")
    account_by_id = {account.account_id: account for account in state.chart_accounts}
    proposal_by_id = {proposal.adjustment_id: proposal for proposal in state.adjustment_proposals}
    applied: list[dict] = []
    sections = payload.get("sections", {})
    for item in sections.get("coa_accounts", []):
        action = item.get("action")
        if not action:
            continue
        if action not in {"approve", "reject"}:
            _usage_error(f"invalid CoA action for {item.get('account_id')}: {action}")
        account = account_by_id.get(item.get("account_id"))
        if account is None:
            _usage_error(f"Unknown account_id: {item.get('account_id')}")
        approved_by, rationale = _require_review_fields(item, f"CoA decision for {account.account_id}")
        account.status = "approved" if action == "approve" else "rejected"
        selected = "approve_coa" if action == "approve" else "reject_coa"
        decision = AccountantDecision(decision_id=f"decision_{selected}_{len(state.decisions) + 1:04d}", question=f"{selected} {account.account_id}?", selected_option=selected, rationale=rationale, status=DecisionStatus.APPROVED, approved_by=approved_by, evidence_refs=account.source_evidence_refs)
        state.decisions.append(decision)
        applied.append({"section": "coa_accounts", "id": account.account_id, "action": action, "decision_id": decision.decision_id})
    if state.chart_accounts and not [account for account in state.chart_accounts if account.status != "approved"]:
        state.coa_review_status = "approved"
    for item in sections.get("journal_decisions", []):
        action = item.get("action")
        if not action:
            continue
        if action not in {"approve", "reject"}:
            _usage_error(f"invalid journal action for {item.get('adjustment_id')}: {action}")
        proposal = proposal_by_id.get(item.get("adjustment_id"))
        if proposal is None:
            _usage_error(f"Unknown adjustment_id: {item.get('adjustment_id')}")
        approved_by, rationale = _require_review_fields(item, f"journal decision for {proposal.adjustment_id}")
        if action == "approve" and "pending_review_offset" in {proposal.debit_account, proposal.credit_account}:
            offset = item.get("offset_account_id")
            if not offset:
                _usage_error(f"journal decision for {proposal.adjustment_id} requires offset_account_id before approval")
            if offset not in account_by_id:
                _usage_error(f"Unknown offset_account_id: {offset}")
            if proposal.debit_account == "pending_review_offset":
                proposal.debit_account = offset
            if proposal.credit_account == "pending_review_offset":
                proposal.credit_account = offset
        proposal.status = "approved" if action == "approve" else "rejected"
        selected = "approve_journal" if action == "approve" else "reject_journal"
        decision = AccountantDecision(decision_id=f"decision_{selected}_{len(state.decisions) + 1:04d}", question=f"{selected} {proposal.adjustment_id}?", selected_option=selected, rationale=rationale, status=DecisionStatus.APPROVED, approved_by=approved_by, evidence_refs=proposal.source_evidence_refs)
        state.decisions.append(decision)
        proposal.decision_id = decision.decision_id
        applied.append({"section": "journal_decisions", "id": proposal.adjustment_id, "action": action, "decision_id": decision.decision_id})
    if state.adjustment_proposals and not [proposal for proposal in state.adjustment_proposals if proposal.status != "approved"]:
        state.adjustment_review_status = "approved"
    draft_section = sections.get("draft_statement_review", {})
    draft_decision = draft_section.get("decision", {}) if isinstance(draft_section, dict) else {}
    if draft_decision.get("action"):
        draft_path = Path(draft_section.get("draft_artifact") or artifact_dir / "draft_statements" / "draft_statements.json")
        draft = json.loads(draft_path.read_text())
        if draft_section.get("draft_sha256") and draft_section["draft_sha256"] != _file_sha256(draft_path):
            _usage_error("Draft statement artifact hash does not match workbench")
        if draft_decision["action"] not in {"approve", "reject"}:
            _usage_error(f"invalid draft statement action: {draft_decision['action']}")
        approved_by, rationale = _require_review_fields(draft_decision, "draft statement decision")
        if draft_decision["action"] == "approve" and draft.get("findings"):
            _usage_error("Cannot approve draft statements while draft findings remain")
        selected = "approve_draft_statements" if draft_decision["action"] == "approve" else "reject_draft_statements"
        decision = AccountantDecision(decision_id=f"decision_{selected}_{len(state.decisions) + 1:04d}", question="Approve internal-review draft statements?", selected_option=selected, rationale=rationale, status=DecisionStatus.APPROVED, approved_by=approved_by, evidence_refs=[str(draft_path), _file_sha256(draft_path)])
        state.decisions.append(decision)
        applied.append({"section": "draft_statement_review", "id": str(draft_path), "action": draft_decision["action"], "decision_id": decision.decision_id})
    final_section = sections.get("final_signoff", {})
    if isinstance(final_section, dict) and final_section.get("action"):
        if final_section["action"] != "approve":
            _usage_error(f"invalid final signoff action: {final_section['action']}")
        approved_by, rationale = _require_review_fields(final_section, "final signoff")
        decision = AccountantDecision(decision_id=f"decision_final_signoff_{len(state.decisions) + 1:04d}", question="Final release sign-off?", selected_option="final_signoff", rationale=rationale, status=DecisionStatus.APPROVED, approved_by=approved_by, evidence_refs=[final_section.get("release_candidate_manifest", "")])
        state.decisions.append(decision)
        applied.append({"section": "final_signoff", "id": "final_signoff", "action": "approve", "decision_id": decision.decision_id})
    save_engagement_state(state_path, state)
    output_payload = {"engagement_id": state.engagement_id, "summary": {"applied": len(applied)}, "applied_decisions": applied, "inspection": inspect_engagement(state)}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(output_payload, indent=2, sort_keys=True))
    print(f"Applied {len(applied)} accountant review workbench decisions → {output}")
    return 0


def _collect_release_blockers(state: EngagementState, artifact_dir: Path) -> list[dict]:
    blockers: list[dict] = []
    if state.coa_review_status != "approved" or [account for account in state.chart_accounts if account.status != "approved"]:
        blockers.append({"category": "coa", "artifact": "engagement_state.chart_accounts", "message": "Chart of accounts is not fully approved.", "required_action": "Approve or reject pending CoA accounts with reviewer and rationale."})
    if [proposal for proposal in state.adjustment_proposals if proposal.status != "approved"]:
        blockers.append({"category": "journal", "artifact": "engagement_state.adjustment_proposals", "message": "Journal proposals remain unresolved.", "required_action": "Approve/reject journals and resolve pending_review_offset accounts."})
    draft_path = artifact_dir / "draft_statements" / "draft_statements.json"
    if not draft_path.exists() or not _draft_statement_review_decision(state):
        blockers.append({"category": "statement", "artifact": str(draft_path), "message": "Draft statements are not accountant-approved.", "required_action": "Review and approve/reject draft statements."})
    rc_path = artifact_dir / "release_candidate" / "release_candidate_manifest.json"
    if not rc_path.exists():
        blockers.append({"category": "release_candidate", "artifact": str(rc_path), "message": "Release candidate package has not been built.", "required_action": "Build release candidate after approvals are complete."})
    else:
        manifest = json.loads(rc_path.read_text())
        for name, info in manifest.get("artifacts", {}).items():
            path = Path(info.get("path", ""))
            if not path.exists() or _file_sha256(path) != info.get("sha256"):
                blockers.append({"category": "release_candidate", "artifact": name, "message": "Release candidate artifact is missing or hash-mismatched.", "required_action": "Rebuild or verify release candidate before final release."})
                break
    if not _final_signoff_decision(state):
        blockers.append({"category": "final_signoff", "artifact": "engagement_state.decisions", "message": "Final sign-off is missing.", "required_action": "Record final sign-off after verified release candidate review."})
    open_blockers = [item for item in state.exceptions if getattr(item, "is_blocking", False)]
    if open_blockers:
        blockers.append({"category": "source_evidence", "artifact": "engagement_state.exceptions", "message": f"{len(open_blockers)} blocking source/control exceptions remain.", "required_action": "Resolve or accept-risk blocking exceptions before release."})
    return blockers


def _format_release_blockers(payload: dict) -> str:
    lines = [f"# Release Blockers — {payload['entity_name']}", "", f"- Blockers: {payload['summary']['blockers']}", ""]
    if payload["blockers"]:
        for item in payload["blockers"]:
            lines.extend([f"## {item['category']}", f"- Artifact: {item['artifact']}", f"- Issue: {item['message']}", f"- Required action: {item['required_action']}", ""])
    else:
        lines.append("No release blockers detected by this check.")
    return "\n".join(lines).rstrip() + "\n"


def _explain_release_blockers_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    artifact_dir = Path(args.artifact_dir)
    blockers = _collect_release_blockers(state, artifact_dir)
    payload = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "blockers": blockers, "summary": {"blockers": len(blockers)}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_release_blockers(payload))
    output.with_suffix(".json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported release blockers → {output}")
    return 0 if not blockers else 1


def _export_review_ui_bundle_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    workbench = _build_accountant_review_workbench(state, artifact_dir)
    blockers_payload = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "blockers": _collect_release_blockers(state, artifact_dir)}
    artifacts = {}
    for name, rel in {
        "post_journal_trial_balance": "post_journal_trial_balance.json",
        "statement_line_mapping": "statement_line_mapping.json",
        "draft_statements": "draft_statements/draft_statements.json",
        "release_candidate": "release_candidate/release_candidate_manifest.json",
    }.items():
        path = artifact_dir / rel
        artifacts[name] = json.loads(path.read_text()) if path.exists() and path.suffix == ".json" else None
    bundle = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "workbench": workbench, "release_blockers": blockers_payload, "artifacts": artifacts, "state_summary": inspect_engagement(state)}
    (output_dir / "review_ui_bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True))
    (output_dir / "README.md").write_text(f"# Review UI Bundle — {state.entity_name}\n\nThis bundle is read-only review data. Apply approvals with `apply-accountant-review-workbench`.\n")
    print(f"Exported review UI bundle → {output_dir}")
    return 0


def _accountant_review_ui_html(entity_name: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Accountant Review Workbench — {html.escape(entity_name)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #f6f7fb; color: #172033; }}
    header {{ background: #10223f; color: white; padding: 24px 32px; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
    section {{ background: white; border: 1px solid #d9deea; border-radius: 14px; padding: 18px; margin: 16px 0; box-shadow: 0 1px 3px rgba(16,34,63,.06); }}
    h1, h2 {{ margin-top: 0; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }}
    .card {{ border: 1px solid #e3e7ef; border-radius: 10px; padding: 12px; background: #fbfcff; }}
    label {{ display: block; font-size: 12px; font-weight: 700; margin-top: 8px; color: #46546b; }}
    select, input, textarea {{ width: 100%; box-sizing: border-box; margin-top: 4px; padding: 8px; border: 1px solid #cbd3df; border-radius: 8px; }}
    textarea {{ min-height: 60px; }}
    button {{ background: #2456d6; color: white; border: 0; border-radius: 10px; padding: 10px 14px; font-weight: 700; cursor: pointer; }}
    code {{ background: #eef2ff; padding: 2px 5px; border-radius: 5px; }}
    .danger {{ color: #a43b3b; }}
    .muted {{ color: #667085; }}
  </style>
</head>
<body>
  <header>
    <h1>Accountant Review Workbench</h1>
    <p>{html.escape(entity_name)} — local static review UI. This page edits a downloadable workbench JSON only; it does not mutate engagement state.</p>
  </header>
  <main>
    <section>
      <h2>How to use</h2>
      <ol>
        <li>Fill decisions below with reviewer and rationale.</li>
        <li>Click <strong>Download filled workbench JSON</strong>.</li>
        <li>Apply it with <code>PYTHONPATH=src python3.11 -m accountant_copilot.cli apply-accountant-review-workbench --state ... --workbench accountant_review_workbench_filled.json --artifact-dir ... --output applied_accountant_review_workbench.json</code>.</li>
      </ol>
      <button onclick="downloadWorkbench()">Download filled workbench JSON</button>
    </section>
    <section><h2>Release Blockers</h2><div id="blockers"></div></section>
    <section><h2>CoA Review</h2><div id="coa"></div></section>
    <section><h2>Journal Review</h2><div id="journals"></div></section>
    <section><h2>Draft Statement Review</h2><div id="draft"></div></section>
    <section><h2>Final Sign-off</h2><div id="final"></div></section>
  </main>
  <script src="app.js"></script>
</body>
</html>
"""


def _accountant_review_ui_js() -> str:
    return r"""
const state = window.REVIEW_UI_DATA;
const workbench = state.workbench;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
}

function setValue(path, value) {
  let target = workbench;
  for (let i = 0; i < path.length - 1; i++) target = target[path[i]];
  target[path[path.length - 1]] = value;
}

function decisionControls(path, includeOffset=false) {
  const pathText = JSON.stringify(path);
  const offset = includeOffset ? `<label>Offset account ID<input onchange='setValue(${pathText}.concat(["offset_account_id"]), this.value)' /></label>` : '';
  return `
    <label>Action<select onchange='setValue(${pathText}.concat(["action"]), this.value)'><option value=""></option><option value="approve">approve</option><option value="reject">reject</option></select></label>
    ${offset}
    <label>Reviewer<input onchange='setValue(${pathText}.concat(["approved_by"]), this.value)' /></label>
    <label>Rationale<textarea onchange='setValue(${pathText}.concat(["rationale"]), this.value)'></textarea></label>`;
}

function renderBlockers() {
  const blockers = state.release_blockers.blockers || [];
  document.getElementById('blockers').innerHTML = blockers.length ? blockers.map(b => `<div class="card"><strong class="danger">${escapeHtml(b.category)}</strong><p>${escapeHtml(b.message)}</p><p><strong>Required action:</strong> ${escapeHtml(b.required_action)}</p><p class="muted">${escapeHtml(b.artifact)}</p></div>`).join('') : '<p>No release blockers detected.</p>';
}

function renderCoa() {
  const rows = workbench.sections.coa_accounts || [];
  document.getElementById('coa').innerHTML = rows.length ? `<div class="grid">${rows.map((a, i) => `<div class="card"><strong>${escapeHtml(a.code)} ${escapeHtml(a.name)}</strong><p>${escapeHtml(a.type)} / ${escapeHtml(a.presentation_group)} / opening ${escapeHtml(a.opening_balance)}</p>${decisionControls(['sections','coa_accounts',i])}</div>`).join('')}</div>` : '<p>No CoA accounts pending review.</p>';
}

function renderJournals() {
  const rows = workbench.sections.journal_decisions || [];
  document.getElementById('journals').innerHTML = rows.length ? `<div class="grid">${rows.map((j, i) => `<div class="card"><strong>${escapeHtml(j.adjustment_id)}</strong><p>DR ${escapeHtml(j.debit_account)} / CR ${escapeHtml(j.credit_account)} / ${escapeHtml(j.amount)}</p>${decisionControls(['sections','journal_decisions',i], true)}</div>`).join('')}</div>` : '<p>No journals pending review.</p>';
}

function renderDraft() {
  const d = workbench.sections.draft_statement_review;
  document.getElementById('draft').innerHTML = `<div class="card"><p>Status: <strong>${escapeHtml(d.draft_status)}</strong></p><p>Findings: ${escapeHtml(d.draft_findings)}</p>${decisionControls(['sections','draft_statement_review','decision'])}</div>`;
}

function renderFinal() {
  document.getElementById('final').innerHTML = `<div class="card"><p>Final sign-off should only be completed after release candidate verification.</p>${decisionControls(['sections','final_signoff'])}</div>`;
}

function downloadWorkbench() {
  const blob = new Blob([JSON.stringify(workbench, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'accountant_review_workbench_filled.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

renderBlockers();
renderCoa();
renderJournals();
renderDraft();
renderFinal();
"""


def _export_accountant_review_ui_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    workbench = _build_accountant_review_workbench(state, artifact_dir)
    blockers_payload = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "blockers": _collect_release_blockers(state, artifact_dir)}
    artifacts = {}
    for name, rel in {
        "post_journal_trial_balance": "post_journal_trial_balance.json",
        "statement_line_mapping": "statement_line_mapping.json",
        "draft_statements": "draft_statements/draft_statements.json",
    }.items():
        path = artifact_dir / rel
        artifacts[name] = json.loads(path.read_text()) if path.exists() else None
    bundle = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "workbench": workbench, "release_blockers": blockers_payload, "artifacts": artifacts, "state_summary": inspect_engagement(state)}
    (output_dir / "accountant_review_workbench.json").write_text(json.dumps(workbench, indent=2, sort_keys=True))
    (output_dir / "review_ui_bundle.json").write_text(json.dumps(bundle, indent=2, sort_keys=True))
    data_script = "window.REVIEW_UI_DATA = " + json.dumps(bundle, indent=2, sort_keys=True) + ";\n\n"
    (output_dir / "app.js").write_text(data_script + _accountant_review_ui_js())
    (output_dir / "index.html").write_text(_accountant_review_ui_html(state.entity_name))
    (output_dir / "README.md").write_text(f"# Accountant Review UI — {state.entity_name}\n\nOpen `index.html` locally. The UI only downloads filled workbench JSON; apply it with `apply-accountant-review-workbench`.\n")
    print(f"Exported accountant review UI → {output_dir / 'index.html'}")
    return 0


def _parse_bank_statement_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _bank_balance_label(fact: dict, balance_field: str, sign_field: str) -> str | None:
    amount = fact.get(balance_field)
    if not amount:
        return None
    sign = fact.get(sign_field)
    return f"{amount} {sign}" if sign else str(amount)


def _normalise_balance_for_compare(fact: dict, balance_field: str, sign_field: str) -> tuple[str | None, str | None]:
    amount = fact.get(balance_field)
    if not amount:
        return (None, None)
    cleaned = _clean_money_amount(str(amount)) or ""
    comparable = re.sub(r"[^0-9.-]", "", cleaned)
    return (comparable, (fact.get(sign_field) or "").upper() or None)


def _balances_match(prior_close: tuple[str | None, str | None], current_open: tuple[str | None, str | None]) -> bool:
    if prior_close[0] is None or current_open[0] is None:
        return False
    if prior_close[0] != current_open[0]:
        return False
    if prior_close[1] and current_open[1] and prior_close[1] != current_open[1]:
        return False
    return True


def _build_bank_continuity_payload(facts_payload: dict) -> dict:
    grouped_facts: dict[str, list[dict]] = {}
    for fact in facts_payload.get("facts", []):
        grouped_facts.setdefault(fact.get("account_key_raw") or "unknown_bank_account", []).append(fact)
    comparisons: list[dict] = []
    findings: list[dict] = []
    for account_key, account_facts in sorted(grouped_facts.items()):
        facts = sorted(
            account_facts,
            key=lambda fact: (_parse_bank_statement_date(fact.get("statement_period_start")) or datetime.max, fact.get("evidence_id") or ""),
        )
        seen_periods: dict[tuple[str | None, str | None], dict] = {}
        for fact in facts:
            period_key = (fact.get("statement_period_start"), fact.get("statement_period_end"))
            if period_key in seen_periods:
                findings.append(
                    {
                        "category": "bank_duplicate_period",
                        "account_key_raw": account_key,
                        "prior_evidence_id": seen_periods[period_key].get("evidence_id"),
                        "current_evidence_id": fact.get("evidence_id"),
                        "statement_period_start": period_key[0],
                        "statement_period_end": period_key[1],
                        "recommended_action": "Review duplicate bank statement periods before relying on continuity checks.",
                    }
                )
            seen_periods[period_key] = fact

        for prior, current in zip(facts, facts[1:]):
            prior_close = _normalise_balance_for_compare(prior, "closing_balance", "closing_balance_sign")
            current_open = _normalise_balance_for_compare(current, "opening_balance", "opening_balance_sign")
            prior_end = _parse_bank_statement_date(prior.get("statement_period_end"))
            current_start = _parse_bank_statement_date(current.get("statement_period_start"))
            comparison = {
                "account_key_raw": account_key,
                "prior_evidence_id": prior.get("evidence_id"),
                "current_evidence_id": current.get("evidence_id"),
                "prior_period_end": prior.get("statement_period_end"),
                "current_period_start": current.get("statement_period_start"),
                "prior_closing_balance": _bank_balance_label(prior, "closing_balance", "closing_balance_sign"),
                "current_opening_balance": _bank_balance_label(current, "opening_balance", "opening_balance_sign"),
                "status": "matched" if _balances_match(prior_close, current_open) else "needs_review",
            }
            comparisons.append(comparison)
            if prior_end and current_start and current_start not in {prior_end, prior_end + timedelta(days=1)}:
                findings.append(
                    {
                        "category": "bank_period_gap_or_overlap",
                        "account_key_raw": account_key,
                        "prior_evidence_id": prior.get("evidence_id"),
                        "current_evidence_id": current.get("evidence_id"),
                        "prior_period_end": prior.get("statement_period_end"),
                        "current_period_start": current.get("statement_period_start"),
                        "recommended_action": "Review whether bank statement periods are missing, duplicated, or overlapping.",
                    }
                )
            if comparison["status"] != "matched":
                missing_fields = []
                if prior_close[0] is None:
                    missing_fields.append("prior_closing_balance")
                if current_open[0] is None:
                    missing_fields.append("current_opening_balance")
                finding = {
                    "category": "bank_continuity_missing_balance" if missing_fields else "bank_continuity_break",
                    "account_key_raw": account_key,
                    "prior_evidence_id": prior.get("evidence_id"),
                    "current_evidence_id": current.get("evidence_id"),
                    "prior_closing_balance": comparison["prior_closing_balance"],
                    "current_opening_balance": comparison["current_opening_balance"],
                    "missing_fields": missing_fields,
                    "recommended_action": "Review source statements before transaction extraction or bank-to-TB tie-out.",
                }
                findings.append(finding)
    return {
        "engagement_id": facts_payload.get("engagement_id"),
        "entity_name": facts_payload.get("entity_name"),
        "check_type": "bank_statement_continuity",
        "comparisons": comparisons,
        "findings": findings,
        "summary": {"comparisons": len(comparisons), "findings": len(findings)},
    }


def _format_bank_continuity(payload: dict) -> str:
    lines = [f"# Bank Continuity Check — {payload.get('entity_name')}", ""]
    summary = payload["summary"]
    lines.extend([f"- Comparisons: {summary['comparisons']}", f"- Findings: {summary['findings']}", ""])
    if payload["comparisons"]:
        lines.append("## Comparisons")
        for comparison in payload["comparisons"]:
            lines.extend(
                [
                    f"- `{comparison['prior_evidence_id']}` → `{comparison['current_evidence_id']}`: {comparison['status']}",
                    f"  - Account key/raw: {comparison.get('account_key_raw') or 'unknown_bank_account'}",
                    f"  - Prior closing: {comparison['prior_closing_balance'] or 'not extracted'}",
                    f"  - Current opening: {comparison['current_opening_balance'] or 'not extracted'}",
                    f"  - Period bridge: {comparison['prior_period_end']} → {comparison['current_period_start']}",
                ]
            )
        lines.append("")
    if payload["findings"]:
        lines.append("## Findings needing review")
        for finding in payload["findings"]:
            lines.extend(
                [
                    f"- {finding['category']}: `{finding.get('prior_evidence_id')}` → `{finding.get('current_evidence_id')}`",
                    f"  - Prior closing: {finding.get('prior_closing_balance') or 'n/a'}",
                    f"  - Current opening: {finding.get('current_opening_balance') or 'n/a'}",
                    f"  - Action: {finding['recommended_action']}",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _export_bank_continuity_command(args: argparse.Namespace) -> int:
    facts_payload = json.loads(Path(args.facts).read_text())
    payload = _build_bank_continuity_payload(facts_payload)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_bank_continuity(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported bank continuity check → {output}")
    print(f"Exported bank continuity check JSON → {json_output}")
    return 0 if not payload["findings"] else 1


def _record_evidence_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    evidence = EvidenceRef(
        evidence_id=args.evidence_id,
        source_type=args.source_type,
        file_path=args.file_path,
        page=args.page,
        row=args.row,
        quote=args.quote,
        amount=args.amount,
        date=args.date,
        confidence=args.confidence,
        document_id=args.document_id,
    )
    state.evidence.append(evidence)
    save_engagement_state(state_path, state)
    print(f"Recorded evidence {evidence.evidence_id}")
    return 0


def _export_review_template_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    template = {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "decisions": [
            {
                "exception_id": item.exception_id,
                "severity": item.severity.value,
                "category": item.category,
                "description": item.description,
                "evidence_refs": item.evidence_refs,
                "recommended_action": item.recommended_action,
                "action": "",
                "rationale": "",
                "approved_by": "",
            }
            for item in state.open_exceptions()
        ],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(template, indent=2, sort_keys=True))
    print(f"Exported review template → {output}")
    return 0


def _recommended_preferences(state: EngagementState) -> list[PreferenceRule]:
    candidates: list[PreferenceRule] = []
    subjects = {state.entity_name, state.entity_type, state.engagement_id, "*", "all", "global"}
    for pref in state.preferences:
        if not pref.is_approved:
            continue
        if pref.scope in {PreferenceScope.FIRM, PreferenceScope.ACCOUNTANT} or pref.subject in subjects:
            candidates.append(pref)
    return candidates


def _recommend_preferences_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    prefs = _recommended_preferences(state)
    print("Recommended preferences")
    if not prefs:
        print("No approved preferences match this engagement.")
    for pref in prefs:
        print(f"- {pref.preference_id}: {pref.rule}")
    return 0


def _apply_preferences_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    pref = next((item for item in state.preferences if item.preference_id == args.preference_id), None)
    if pref is None:
        _usage_error(f"Unknown preference_id: {args.preference_id}")
    if not pref.is_approved:
        _usage_error(f"Preference is not approved: {args.preference_id}")
    decision = AccountantDecision(
        decision_id=f"decision_apply_preference_{len(state.decisions) + 1:04d}",
        question=f"Apply preference {pref.preference_id} to this engagement?",
        selected_option="apply_preference",
        rationale=args.rationale,
        status=DecisionStatus.APPROVED,
        approved_by=args.approved_by,
        evidence_refs=[pref.preference_id],
    )
    state.decisions.append(decision)
    save_engagement_state(state_path, state)
    print(f"Applied preference {pref.preference_id}")
    return 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_document_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    document = SourceDocument(
        document_id=args.document_id,
        file_path=args.file_path,
        document_type=args.document_type,
        entity=args.entity,
        period_start=args.period_start,
        period_end=args.period_end,
        source_hash=_sha256_file(Path(args.file_path)),
        notes=args.notes,
    )
    state.source_documents = [d for d in state.source_documents if d.document_id != document.document_id]
    state.source_documents.append(document)
    save_engagement_state(state_path, state)
    print(f"Recorded document {document.document_id}")
    return 0


def format_documents(state: EngagementState) -> str:
    lines = ["Source documents", f"Engagement: {state.entity_name}", ""]
    if not state.source_documents:
        lines.append("No source documents recorded.")
    for doc in sorted(state.source_documents, key=lambda item: item.document_id):
        lines.append(f"- {doc.document_id}: {doc.document_type} {doc.file_path} hash={doc.source_hash}")
    return "\n".join(lines) + "\n"


def _list_documents_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    print(format_documents(state), end="")
    return 0


def _record_coa_account_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    account = ChartAccount(
        account_id=args.account_id,
        code=args.code,
        name=args.name,
        type=args.type,
        presentation_group=args.presentation_group,
        opening_balance=args.opening_balance,
        source_evidence_refs=list(args.evidence_ref or []),
    )
    state.chart_accounts = [item for item in state.chart_accounts if item.account_id != account.account_id]
    state.chart_accounts.append(account)
    state.coa_review_required = True
    if state.coa_review_status == "not_required":
        state.coa_review_status = "pending_review"
    save_engagement_state(state_path, state)
    print(f"Recorded CoA account {account.account_id}")
    return 0


def _record_adjustment_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    proposal = AdjustmentProposal(
        adjustment_id=args.adjustment_id,
        description=args.description,
        debit_account=args.debit_account,
        credit_account=args.credit_account,
        amount=args.amount,
        date=args.date,
        source_evidence_refs=list(args.evidence_ref or []),
    )
    state.adjustment_proposals = [item for item in state.adjustment_proposals if item.adjustment_id != proposal.adjustment_id]
    state.adjustment_proposals.append(proposal)
    state.adjustment_review_status = "pending_review"
    save_engagement_state(state_path, state)
    print(f"Recorded adjustment proposal {proposal.adjustment_id}")
    return 0


def _record_output_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    source_state_hash = hashlib.sha256(state.model_dump_json().encode()).hexdigest()
    artifact = OutputArtifact(
        output_id=args.output_id,
        file_path=args.file_path,
        artifact_type=args.artifact_type,
        verifier_status=args.verifier_status,
        created_at=datetime.now(timezone.utc).isoformat(),
        source_state_hash=source_state_hash,
    )
    state.output_artifacts = [item for item in state.output_artifacts if item.output_id != artifact.output_id]
    state.output_artifacts.append(artifact)
    save_engagement_state(state_path, state)
    print(f"Recorded output artifact {artifact.output_id} ({artifact.verifier_status})")
    return 0


def _review_coa_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    lines = [
        "CoA review",
        f"Engagement: {state.entity_name}",
        f"CoA ref: {state.coa_ref or 'none recorded'}",
        f"Status: {state.coa_review_status}",
        "Required decision: approve chart of accounts names, types, opening balances, and presentation grouping.",
    ]
    for account in sorted(state.chart_accounts, key=lambda item: item.account_id):
        lines.append(
            f"- {account.account_id} [{account.status}] {account.code} {account.name} "
            f"{account.type} {account.presentation_group} opening={account.opening_balance}"
        )
    print("\n".join(lines) + "\n")
    pending = [account for account in state.chart_accounts if account.status != "approved"]
    return 0 if state.coa_review_status == "approved" and not pending else 1


def _approve_coa_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    evidence_refs = [state.coa_ref] if state.coa_ref else []
    if getattr(args, "account_id", None):
        account = next((item for item in state.chart_accounts if item.account_id == args.account_id), None)
        if account is None:
            _usage_error(f"Unknown account_id: {args.account_id}")
        account.status = "approved"
        evidence_refs.extend(account.source_evidence_refs)
    decision = AccountantDecision(
        decision_id=f"decision_approve_coa_{len(state.decisions) + 1:04d}",
        question="Approve chart of accounts for this engagement?",
        selected_option="approve_coa",
        rationale=args.rationale,
        status=DecisionStatus.APPROVED,
        approved_by=args.approved_by,
        evidence_refs=evidence_refs,
    )
    state.decisions.append(decision)
    state.coa_review_required = True
    if not [account for account in state.chart_accounts if account.status != "approved"]:
        state.coa_review_status = "approved"
    save_engagement_state(state_path, state)
    print(f"CoA approved by {args.approved_by}")
    print(format_inspection(inspect_engagement(state)), end="")
    return 0 if inspect_engagement(state)["final_output_allowed"] else 1


def _adjustment_items(state: EngagementState) -> list[ExceptionItem]:
    return [item for item in state.exceptions if item.category.startswith("journal_") or "adjustment" in item.category]


def _review_adjustments_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    items = _adjustment_items(state)
    lines = ["Adjustment review", f"Engagement: {state.entity_name}", ""]
    for proposal in sorted(state.adjustment_proposals, key=lambda item: item.adjustment_id):
        lines.append(
            f"- {proposal.adjustment_id} [{proposal.status}] {proposal.date} "
            f"DR {proposal.debit_account} CR {proposal.credit_account} amount={proposal.amount}: {proposal.description}"
        )
    if not items and not state.adjustment_proposals:
        lines.append("No adjustment or journal review items recorded.")
    for item in items:
        lines.extend(_format_exception_item(item))
    print("\n".join(lines) + "\n")
    unresolved_proposals = [proposal for proposal in state.adjustment_proposals if proposal.status != "approved"]
    return 0 if not unresolved_proposals and not [item for item in items if item.is_open or item.status == ExceptionStatus.REJECTED] else 1


def _record_adjustment_decision(args: argparse.Namespace, selected_option: str, status: ExceptionStatus) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    item = None
    evidence_refs: list[str] = []
    target_id = getattr(args, "exception_id", None) or getattr(args, "adjustment_id", None)
    if getattr(args, "adjustment_id", None):
        proposal = next((p for p in state.adjustment_proposals if p.adjustment_id == args.adjustment_id), None)
        if proposal is None:
            _usage_error(f"Unknown adjustment_id: {args.adjustment_id}")
        proposal.status = "approved" if status == ExceptionStatus.RESOLVED else "rejected"
        evidence_refs = list(proposal.source_evidence_refs)
        target_id = proposal.adjustment_id
    else:
        item = _find_exception(state, args.exception_id)
        evidence_refs = list(item.evidence_refs)
    decision = AccountantDecision(
        decision_id=f"decision_{selected_option}_{len(state.decisions) + 1:04d}",
        question=f"{selected_option.replace('_', ' ').title()} {target_id}?",
        selected_option=selected_option,
        rationale=args.rationale,
        status=DecisionStatus.APPROVED,
        approved_by=args.approved_by,
        evidence_refs=evidence_refs,
    )
    state.decisions.append(decision)
    if getattr(args, "adjustment_id", None):
        proposal.decision_id = decision.decision_id
    if item is not None:
        item.status = status
        item.decision_id = decision.decision_id
    state.adjustment_review_status = "approved" if status == ExceptionStatus.RESOLVED else "rejected"
    save_engagement_state(state_path, state)
    print(f"Recorded {selected_option} for {target_id}")
    print(format_inspection(inspect_engagement(state)), end="")
    return 0 if inspect_engagement(state)["final_output_allowed"] else 1


def _approve_adjustment_command(args: argparse.Namespace) -> int:
    return _record_adjustment_decision(args, "approve_adjustment", ExceptionStatus.RESOLVED)


def _reject_adjustment_command(args: argparse.Namespace) -> int:
    return _record_adjustment_decision(args, "reject_adjustment", ExceptionStatus.REJECTED)


def _format_evidence_summary(state: EngagementState) -> str:
    lines = ["Evidence summary", f"Engagement: {state.entity_name}", ""]
    if not state.evidence:
        lines.append("No structured evidence recorded.")
    for evidence in sorted(state.evidence, key=lambda item: item.evidence_id):
        doc = f" document={evidence.document_id}" if evidence.document_id else ""
        lines.append(f"- {evidence.evidence_id}:{doc} {evidence.source_type} {evidence.file_path} {evidence.quote or ''}".rstrip())
    return "\n".join(lines) + "\n"


def _format_source_fact_layers(state_path: Path) -> str:
    base_dir = state_path.parent
    artifacts = [
        ("Bank transactions", "bank_transactions.md"),
        ("Invoice facts", "invoice_facts.md"),
        ("Invoice review", "invoice_review.md"),
        ("Distribution/tax facts", "distribution_tax_facts.md"),
        ("Distribution/tax review", "distribution_tax_review.md"),
        ("Broker trade facts", "broker_trade_facts.md"),
        ("Broker trade review", "broker_trade_review.md"),
        ("Source fact bank matches", "source_fact_matches.md"),
        ("CoA mapping suggestions", "coa_mapping_suggestions.md"),
    ]
    lines = ["# Source Fact and Review Layers", ""]
    found = False
    for title, filename in artifacts:
        path = base_dir / filename
        if not path.exists():
            continue
        found = True
        content = path.read_text(errors="ignore").strip()
        lines.extend([f"## {title}", "", f"Source artifact: `{path}`", "", content, ""])
    if not found:
        lines.append("No source fact layer artifacts found next to the engagement state.")
    return "\n".join(lines).rstrip() + "\n"


def _format_journal_tb_impact(state: EngagementState, state_path: Path) -> str:
    base_dir = state_path.parent
    lines = [f"# Journal / TB Impact Review — {state.entity_name}", ""]
    lines.extend([
        "## Control status",
        f"- CoA review status: {state.coa_review_status}",
        f"- Adjustment review status: {state.adjustment_review_status}",
        f"- CoA accounts: {len(state.chart_accounts)}",
        f"- Journal/adjustment proposals: {len(state.adjustment_proposals)}",
        f"- Approved journals: {len([proposal for proposal in state.adjustment_proposals if proposal.status == 'approved'])}",
        f"- Pending/rejected journals: {len([proposal for proposal in state.adjustment_proposals if proposal.status != 'approved'])}",
        "- Approved automatically: 0",
        "",
    ])
    linked_artifacts = [
        ("Prior statement CoA import", "prior_statement_coa_import.md"),
        ("CoA mapping suggestions", "coa_mapping_suggestions.md"),
        ("CoA mapping decision template", "coa_mapping_decisions_template.json"),
        ("Applied CoA mapping decisions", "applied_coa_mapping_decisions.json"),
        ("Journal proposals", "journal_proposals.md"),
        ("Journal decision template", "journal_decisions_template.json"),
        ("Applied journal decisions", "applied_journal_decisions.json"),
        ("TB impact preview", "tb_impact_preview.md"),
        ("Reviewed journals", "reviewed_journals/reviewed_journals.md"),
        ("Post-journal trial balance", "post_journal_trial_balance.md"),
        ("Statement line mapping", "statement_line_mapping.md"),
        ("Draft statements", "draft_statements/draft_statements.md"),
        ("Draft statement review template", "draft_statement_review_template.json"),
        ("Applied draft statement review", "applied_draft_statement_review.json"),
        ("Release candidate manifest", "release_candidate/release_candidate_manifest.json"),
        ("Final release manifest", "final_release_manifest.json"),
        ("Accountant review workbench", "accountant_review_workbench.md"),
        ("Release blockers", "release_blockers.md"),
        ("Review UI bundle", "review_ui_bundle/README.md"),
        ("Accountant review UI", "accountant_review_ui/index.html"),
    ]
    lines.append("## Linked artifacts")
    found = False
    for title, filename in linked_artifacts:
        path = base_dir / filename
        if path.exists():
            found = True
            lines.append(f"- {title}: `{path}`")
    if not found:
        lines.append("- No journal/TB layer artifacts found next to the engagement state.")
    lines.extend(["", "## CoA accounts pending/approved"])
    if state.chart_accounts:
        for account in sorted(state.chart_accounts, key=lambda item: item.account_id):
            lines.append(f"- {account.account_id} [{account.status}] {account.code} {account.name} — {account.type}/{account.presentation_group} opening={account.opening_balance}")
    else:
        lines.append("- No CoA accounts recorded.")
    lines.extend(["", "## Journal proposals"])
    if state.adjustment_proposals:
        for proposal in sorted(state.adjustment_proposals, key=lambda item: item.adjustment_id):
            lines.extend([
                f"- {proposal.adjustment_id} [{proposal.status}] {proposal.description}",
                f"  - Date: {proposal.date}",
                f"  - DR {proposal.debit_account} / CR {proposal.credit_account}",
                f"  - Amount: {proposal.amount}",
                f"  - Evidence: {', '.join(proposal.source_evidence_refs)}",
            ])
    else:
        lines.append("- No journal proposals recorded.")
    lines.extend(["", "## Accountant review required", "- Approve or reject each CoA account and journal proposal before TB/final statement reliance.", "- Resolve any `pending_review_offset` side before final journal posting."])
    lines.extend([
        "",
        "## Release workflow commands",
        "- `export-draft-statement-review-template` → create accountant approval template for draft statements.",
        "- `apply-draft-statement-review` → persist accountant draft approval/rejection with rationale.",
        "- `build-release-candidate-package` → package hashed reviewed artifacts after draft approval.",
        "- `verify-release-candidate` → detect missing or tampered release candidate artifacts.",
        "- `export-final-release-manifest` → create final manifest tied to verified release candidate and final sign-off.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _export_review_packet_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = inspect_engagement(state)
    readme = "\n".join([
        f"# Accountant Review Packet — {state.entity_name}",
        "",
        "## What the accountant needs to decide",
        "- Resolve, reject, or accept open exceptions with rationale.",
        "- Confirm CoA approval where required.",
        "- Review adjustment/journal items before final sign-off.",
        "- Review source fact layers in `source_fact_layers.md` for bank, invoice, distribution/tax, broker, and source-to-bank matching controls.",
        "- Review journal/TB impact in `journal_tb_impact.md` before relying on proposed journals or account mappings.",
        "",
        f"Readiness: {payload['readiness_summary']}",
    ]) + "\n"
    (output_dir / "README.md").write_text(readme)
    (output_dir / "open_exceptions.md").write_text(format_exception_review(state))
    (output_dir / "document_summary.md").write_text(format_documents(state))
    (output_dir / "evidence_summary.md").write_text(_format_evidence_summary(state))
    (output_dir / "source_fact_layers.md").write_text(_format_source_fact_layers(state_path))
    (output_dir / "journal_tb_impact.md").write_text(_format_journal_tb_impact(state, state_path))
    (output_dir / "preference_recommendations.md").write_text("Recommended preferences\n\n" + "\n".join(f"- {p.preference_id}: {p.rule}" for p in _recommended_preferences(state)) + "\n")
    template = {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "decisions": [
            {
                "exception_id": item.exception_id,
                "severity": item.severity.value,
                "category": item.category,
                "description": item.description,
                "evidence_refs": item.evidence_refs,
                "recommended_action": item.recommended_action,
                "action": "",
                "rationale": "",
                "approved_by": "",
            }
            for item in state.open_exceptions()
        ],
    }
    (output_dir / "review_decisions_template.json").write_text(json.dumps(template, indent=2, sort_keys=True))
    print(f"Exported review packet → {output_dir}")
    return 0 if payload["final_output_allowed"] else 1


def _export_release_manifest_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    signoff = _final_signoff_decision(state)
    if signoff is None:
        print("Cannot export release manifest before final sign-off", file=sys.stderr)
        return 1
    failed_outputs = [artifact for artifact in state.output_artifacts if artifact.verifier_status != "passed"]
    if failed_outputs:
        print("Cannot export release manifest: verifier status is not passing", file=sys.stderr)
        return 1
    state.lifecycle_status = "released"
    save_engagement_state(state_path, state)
    payload = inspect_engagement(state)
    manifest = {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "lifecycle_status": state.lifecycle_status,
        "final_output_allowed": payload["final_output_allowed"],
        "signoff_decision_id": signoff.decision_id,
        "output_artifact_ids": [artifact.output_id for artifact in state.output_artifacts],
        "workpaper_pack": args.workpaper_pack,
        "audit_trail": args.audit_trail,
        "created_outputs": [ref for ref in [state.statements_ref, args.workpaper_pack, args.audit_trail] if ref],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"Exported release manifest → {output}")
    return 0


def _read_csv_records(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _csv_quote(row: dict[str, str]) -> str:
    return ",".join(str(value) for value in row.values())


def _normalise_amount(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        amount = float(cleaned)
    except ValueError:
        return str(value)
    if negative:
        amount = -amount
    return f"{amount:.2f}"


def _normalise_date(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return text


def _row_date(row: dict[str, str]) -> str | None:
    return _normalise_date(row.get("date") or row.get("Date"))


def _row_amount(row: dict[str, str]) -> str | None:
    return _normalise_amount(row.get("amount") or row.get("Amount") or row.get("balance") or row.get("Balance"))


def _validate_csv_columns(rows: list[dict[str, str]], required: set[str]) -> str | None:
    if not rows:
        return "CSV contains no data rows"
    columns = {key.lower() for key in rows[0]}
    missing = sorted(required - columns)
    if missing:
        return f"CSV missing required columns: {', '.join(missing)}"
    return None


def _extract_pdf_page_quotes(path: Path) -> list[tuple[int, str]]:
    """Extract text quotes from a text-based PDF, one quote per page.

    PyMuPDF is preferred when installed. A `pdftotext` fallback keeps local
    development usable without making scanned/OCR documents appear verified.
    Empty pages intentionally return no evidence so the document remains gated.
    """
    try:
        import fitz  # type: ignore[import-not-found]

        pages: list[tuple[int, str]] = []
        with fitz.open(path) as doc:
            for index, page in enumerate(doc, start=1):
                text = " ".join(page.get_text("text").split())
                if text:
                    pages.append((index, text[:1000]))
        return pages
    except Exception:
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        pages = []
        for index, text in enumerate(result.stdout.split("\f"), start=1):
            quote = " ".join(text.split())
            if quote:
                pages.append((index, quote[:1000]))
        return pages


def _extract_image_ocr_quote(path: Path) -> str | None:
    """Extract text from an image using local Tesseract when available.

    OCR output is treated as evidence with OCR confidence, not as approved
    accounting treatment. If Tesseract is unavailable or no text is produced,
    return None so the source remains gated.
    """
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "6"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    quote = " ".join(result.stdout.split())
    return quote[:1000] if quote else None


def _classify_raw_document(path: Path) -> str:
    name = path.name.lower()
    if path.suffix.lower() == ".md":
        return "client_conventions"
    if path.suffix.lower() == ".csv":
        return "supporting_csv"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        return "image_support"
    if "estatement" in name or (path.suffix.lower() == ".pdf" and len(path.stem) == 36 and path.stem.count("-") == 4):
        return "bank_statement"
    if "financial statement" in name or "fy24" in name:
        return "prior_year_financial_statements"
    if any(token in name for token in ["distribution", "tax statement", "payment_advice", "annual statement"]):
        return "investment_statement"
    if "confirmation" in name or "sell" in name:
        return "broker_confirmation"
    return "source_document"


def _ingest_raw_inputs_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    before = state_hash(state)
    input_dir = Path(args.input_dir)
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Input directory not found: {input_dir}", file=sys.stderr)
        return 2
    files = sorted(path for path in input_dir.iterdir() if path.is_file() and path.name != ".DS_Store")
    state.source_documents = [doc for doc in state.source_documents if not doc.document_id.startswith("raw_")]
    state.evidence = [ev for ev in state.evidence if not ev.evidence_id.startswith("raw_")]
    state.exceptions = [item for item in state.exceptions if item.source != "raw_input_intake"]
    extraction_required = 0
    for idx, path in enumerate(files, start=1):
        document_id = f"raw_{idx:03d}"
        document_type = _classify_raw_document(path)
        document = SourceDocument(
            document_id=document_id,
            file_path=str(path),
            document_type=document_type,
            entity=state.entity_name,
            period_start=state.fy_start,
            period_end=state.fy_end,
            source_hash=_sha256_file(path),
            status="registered",
        )
        state.source_documents.append(document)
        if path.suffix.lower() == ".md":
            quote = path.read_text(errors="ignore")[:500]
            state.evidence.append(
                EvidenceRef(
                    evidence_id=f"raw_{idx:03d}_text_001",
                    source_type=document_type,
                    file_path=str(path),
                    quote=quote,
                    document_id=document_id,
                    confidence="1.0",
                )
            )
        elif path.suffix.lower() == ".pdf":
            page_quotes = _extract_pdf_page_quotes(path)
            if page_quotes:
                for page_number, quote in page_quotes:
                    state.evidence.append(
                        EvidenceRef(
                            evidence_id=f"raw_{idx:03d}_page_{page_number:03d}",
                            source_type=document_type,
                            file_path=str(path),
                            page=str(page_number),
                            quote=quote,
                            document_id=document_id,
                            confidence="text_pdf",
                        )
                    )
            else:
                extraction_required += 1
                state.exceptions.append(
                    ExceptionItem(
                        exception_id=f"raw_extraction_required_{idx:03d}",
                        source="raw_input_intake",
                        severity=ExceptionSeverity.HIGH,
                        category="source_extraction_required",
                        description=f"Raw source document requires extraction before final output: {path.name}",
                        evidence_refs=[document_id],
                        recommended_action="Extract page/cell-level source evidence or explicitly mark this document out of scope before release.",
                        requires_human_approval=True,
                    )
                )
        elif path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            quote = _extract_image_ocr_quote(path)
            if quote:
                state.evidence.append(
                    EvidenceRef(
                        evidence_id=f"raw_{idx:03d}_page_001",
                        source_type=document_type,
                        file_path=str(path),
                        page="1",
                        quote=quote,
                        document_id=document_id,
                        confidence="image_ocr",
                    )
                )
            else:
                extraction_required += 1
                state.exceptions.append(
                    ExceptionItem(
                        exception_id=f"raw_extraction_required_{idx:03d}",
                        source="raw_input_intake",
                        severity=ExceptionSeverity.HIGH,
                        category="source_extraction_required",
                        description=f"Raw source document requires extraction before final output: {path.name}",
                        evidence_refs=[document_id],
                        recommended_action="Extract page/cell-level source evidence or explicitly mark this document out of scope before release.",
                        requires_human_approval=True,
                    )
                )
    state.documents_ref = str(input_dir)
    _record_state_transition(state, command="ingest-raw-inputs", before_hash=before, summary=f"Registered {len(files)} raw input documents; extraction required for {extraction_required}.")
    save_engagement_state(state_path, state)
    print(f"Registered {len(files)} raw input documents; extraction-required: {extraction_required}")
    return 0 if extraction_required == 0 else 1


def _ingest_source_document_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    before = state_hash(state)
    source_path = Path(args.file_path)
    if not source_path.exists():
        print(f"Source document not found: {source_path}", file=sys.stderr)
        return 2
    if source_path.suffix.lower() != ".csv":
        print("Only CSV source-document intake is currently supported", file=sys.stderr)
        return 2
    document = SourceDocument(
        document_id=args.document_id,
        file_path=str(source_path),
        document_type=args.document_type,
        entity=args.entity,
        period_start=args.period_start,
        period_end=args.period_end,
        source_hash=_sha256_file(source_path),
        notes=args.notes,
    )
    rows = _read_csv_records(source_path)
    validation_error = _validate_csv_columns(rows, {"date", "description", "amount"})
    if validation_error:
        print(validation_error, file=sys.stderr)
        return 2
    state.source_documents = [doc for doc in state.source_documents if doc.document_id != document.document_id]
    state.source_documents.append(document)
    prefix = f"ev_{document.document_id}_row_"
    state.evidence = [ev for ev in state.evidence if not ev.evidence_id.startswith(prefix)]
    state.exceptions = [item for item in state.exceptions if not item.exception_id.startswith(f"source_duplicate_{document.document_id}_")]
    seen_rows: dict[tuple[str | None, str | None, str], int] = {}
    duplicates = 0
    for idx, row in enumerate(rows, start=2):
        quote = _csv_quote(row)
        amount = _row_amount(row)
        date = _row_date(row)
        key = (date, amount, quote)
        if key in seen_rows:
            duplicates += 1
            state.exceptions.append(
                ExceptionItem(
                    exception_id=f"source_duplicate_{document.document_id}_{idx:04d}",
                    source="source_intake",
                    severity=ExceptionSeverity.MEDIUM,
                    category="duplicate_source_row",
                    description=f"Duplicate source row {idx} matches row {seen_rows[key]} in {document.document_id}.",
                    evidence_refs=[f"{prefix}{idx}"],
                    recommended_action="Confirm whether duplicate source rows are valid before relying on this source document.",
                    requires_human_approval=True,
                )
            )
        else:
            seen_rows[key] = idx
        state.evidence.append(
            EvidenceRef(
                evidence_id=f"{prefix}{idx}",
                source_type=args.document_type,
                file_path=str(source_path),
                row=str(idx),
                quote=quote,
                amount=amount,
                date=date,
                document_id=document.document_id,
            )
        )
    _record_state_transition(state, command="ingest-source-document", before_hash=before, summary=f"Ingested {document.document_id}.")
    save_engagement_state(state_path, state)
    print(f"Ingested source document {document.document_id}")
    return 0 if duplicates == 0 else 1


def _match_transactions_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    before = state_hash(state)
    bank_rows = _read_csv_records(Path(args.bank_csv))
    event_rows = _read_csv_records(Path(args.events_csv))
    amount_tolerance = float(getattr(args, "amount_tolerance", 0) or 0)
    date_window_days = int(getattr(args, "date_window_days", 0) or 0)
    unused_events = set(range(len(event_rows)))
    matches: list[dict[str, object]] = []
    unmatched_bank: list[dict[str, str]] = []

    def close_enough(bank: dict[str, str], event: dict[str, str]) -> bool:
        b_amt = float(_row_amount(bank) or 0)
        e_amt = float(_row_amount(event) or 0)
        if abs(b_amt - e_amt) > amount_tolerance:
            return False
        b_date = datetime.strptime(_row_date(bank) or "1900-01-01", "%Y-%m-%d").date()
        e_date = datetime.strptime(_row_date(event) or "1900-01-01", "%Y-%m-%d").date()
        return abs((b_date - e_date).days) <= date_window_days

    for bank_idx, bank in enumerate(bank_rows):
        match_idx = None
        match_type = "exact_date_amount"
        for event_idx in list(unused_events):
            event = event_rows[event_idx]
            if _row_date(bank) == _row_date(event) and _row_amount(bank) == _row_amount(event):
                match_idx = event_idx
                break
            bank_desc = (bank.get("description") or bank.get("Description") or "").lower()
            event_desc = (event.get("description") or event.get("Description") or "").lower()
            refs = {part for part in bank_desc.replace("-", " ").split() if any(ch.isdigit() for ch in part)}
            if refs and refs.intersection(event_desc.replace("-", " ").split()) and close_enough(bank, event):
                match_idx = event_idx
                match_type = "reference_date_amount_tolerance"
                break
        if match_idx is None:
            # Composite exact amount on same date from multiple supporting events.
            b_amt = float(_row_amount(bank) or 0)
            candidates = [idx for idx in sorted(unused_events) if _row_date(event_rows[idx]) == _row_date(bank)]
            running: list[int] = []
            total = 0.0
            for idx in candidates:
                running.append(idx)
                total += float(_row_amount(event_rows[idx]) or 0)
                if abs(total - b_amt) <= amount_tolerance:
                    for used in running:
                        unused_events.remove(used)
                    matches.append({"bank_row": bank_idx + 2, "event_rows": [idx + 2 for idx in running], "match_type": "composite_amount", "amount": _row_amount(bank), "evidence_refs": [f"bank_row_{bank_idx + 2}"] + [f"event_row_{idx + 2}" for idx in running]})
                    match_idx = -1
                    break
        if match_idx is None:
            unmatched_bank.append(bank)
        elif match_idx >= 0:
            unused_events.remove(match_idx)
            matches.append({"bank_row": bank_idx + 2, "event_row": match_idx + 2, "match_type": match_type, "amount": _row_amount(bank), "evidence_refs": [f"bank_row_{bank_idx + 2}", f"event_row_{match_idx + 2}"]})
    unmatched_events = [event_rows[idx] for idx in sorted(unused_events)]
    payload = {"matches": matches, "unmatched_bank_transactions": unmatched_bank, "unmatched_events": unmatched_events}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    state.matches_ref = str(output)
    state.exceptions = [item for item in state.exceptions if item.source != "deterministic_matching"]
    for idx, row in enumerate(unmatched_bank, start=1):
        state.exceptions.append(
            ExceptionItem(
                exception_id=f"matching_unmatched_bank_{idx:04d}",
                source="deterministic_matching",
                severity=ExceptionSeverity.HIGH,
                category="unmatched_bank_transaction",
                description=f"Unmatched bank transaction: {_csv_quote(row)}",
                evidence_refs=[f"bank_row_{idx + 1}"],
                recommended_action="Classify or match this bank transaction before release.",
                requires_human_approval=True,
            )
        )
    for idx, row in enumerate(unmatched_events, start=1):
        state.exceptions.append(
            ExceptionItem(
                exception_id=f"matching_unmatched_event_{idx:04d}",
                source="deterministic_matching",
                severity=ExceptionSeverity.MEDIUM,
                category="unmatched_event",
                description=f"Unmatched supporting event: {_csv_quote(row)}",
                evidence_refs=[f"event_row_{idx + 1}"],
                recommended_action="Confirm whether this supporting event requires an adjustment.",
                requires_human_approval=True,
            )
        )
    _record_state_transition(state, command="match-transactions", before_hash=before, summary=f"Matched {len(matches)} transaction pairs.")
    save_engagement_state(state_path, state)
    print(f"Matched {len(matches)} transaction pairs; open exceptions: {len(unmatched_bank) + len(unmatched_events)}")
    return 0 if not unmatched_bank and not unmatched_events else 1


def _render_draft_statements_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    before = state_hash(state)
    payload = inspect_engagement(state)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Draft Financial Statements",
        "",
        f"Entity: {state.entity_name}",
        f"Financial year: {state.fy_start} to {state.fy_end}",
        f"Readiness: {payload['readiness_summary']}",
        "",
        "## Chart of accounts",
    ]
    if state.chart_accounts:
        for account in state.chart_accounts:
            lines.append(f"- {account.code} {account.name}: {account.opening_balance}")
    else:
        lines.append("- No structured CoA accounts recorded.")
    lines.extend(["", "## Adjustments"])
    if state.adjustment_proposals:
        for adjustment in state.adjustment_proposals:
            lines.append(f"- {adjustment.description}: {adjustment.amount}")
    else:
        lines.append("- No adjustment proposals recorded.")
    output.write_text("\n".join(lines) + "\n")
    status = "passed" if payload["final_output_allowed"] else "failed"
    verifier = {"output_id": "out_draft_statements", "file_path": str(output), "artifact_type": "draft_financial_statements", "status": status, "findings": [] if status == "passed" else [{"check": "readiness", "detail": payload["readiness_summary"]}]}
    verifier_path = Path(args.verifier_result)
    verifier_path.parent.mkdir(parents=True, exist_ok=True)
    verifier_path.write_text(json.dumps(verifier, indent=2, sort_keys=True))
    artifact = OutputArtifact(
        output_id="out_draft_statements",
        file_path=str(output),
        artifact_type="draft_financial_statements",
        verifier_status=status,
        created_at=datetime.now(timezone.utc).isoformat(),
        source_state_hash=before,
    )
    state.output_artifacts = [item for item in state.output_artifacts if item.output_id != artifact.output_id]
    state.output_artifacts.append(artifact)
    state.statements_ref = str(output)
    _record_state_transition(state, command="render-draft-statements", before_hash=before, summary=f"Rendered draft statements with verifier status {status}.")
    save_engagement_state(state_path, state)
    print(f"Rendered draft statements → {output}")
    return 0 if status == "passed" else 1


def _render_statement_package_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "balance_sheet.md").write_text(f"# Balance Sheet\n\nEntity: {state.entity_name}\n")
    (out / "income_statement.md").write_text(f"# Income and Distributions\n\nEntity: {state.entity_name}\n")
    verifier = {"output_id": "out_statement_package", "artifact_type": "statement_package", "status": "passed" if inspect_engagement(state)["final_output_allowed"] else "failed", "checks": [{"check": "package_files", "status": "passed"}]}
    (out / "verifier_result.json").write_text(json.dumps(verifier, indent=2, sort_keys=True))
    state.output_artifacts = [item for item in state.output_artifacts if item.output_id != "out_statement_package"]
    state.output_artifacts.append(OutputArtifact(output_id="out_statement_package", file_path=str(out), artifact_type="statement_package", verifier_status=verifier["status"], created_at=datetime.now(timezone.utc).isoformat(), source_state_hash=state_hash(state)))
    state.statements_ref = str(out)
    save_engagement_state(state_path, state)
    print(f"Rendered statement package → {out}")
    return 0 if verifier["status"] == "passed" else 1


def _import_trial_balance_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    rows = _read_csv_records(Path(args.trial_balance_csv))
    validation_error = _validate_csv_columns(rows, {"code", "name", "type", "presentation_group", "balance"})
    if validation_error:
        print(validation_error, file=sys.stderr)
        return 2
    state.chart_accounts = []
    state.exceptions = [item for item in state.exceptions if item.source != "trial_balance_import"]
    seen: set[str] = set()
    blockers = 0
    for row in rows:
        code = row.get("code") or row.get("Code") or ""
        name = row.get("name") or row.get("Name") or ""
        group = row.get("presentation_group") or row.get("Presentation_group") or ""
        state.chart_accounts.append(ChartAccount(account_id=f"acct_{code}", code=code, name=name, type=row.get("type") or row.get("Type") or "unknown", presentation_group=group, opening_balance=_normalise_amount(row.get("balance") or row.get("Balance")) or "0.00"))
        if code in seen or "suspense" in name.lower() or "suspense" in group.lower():
            blockers += 1
            category = "duplicate_account_code" if code in seen else "suspense_account"
            state.exceptions.append(ExceptionItem(exception_id=f"tb_{category}_{code}", source="trial_balance_import", severity=ExceptionSeverity.HIGH, category=category, description=f"Trial balance account needs review: {code} {name}", recommended_action="Resolve trial balance account mapping before CoA approval.", requires_human_approval=True))
        seen.add(code)
    state.coa_review_required = True
    state.coa_review_status = "pending_review"
    save_engagement_state(state_path, state)
    print(f"Imported {len(state.chart_accounts)} trial balance accounts")
    return 0 if blockers == 0 else 1


_PRIOR_STATEMENT_ACCOUNT_SPECS = [
    ("Distributions Received", "income", "Revenue"),
    ("Dividends Received", "income", "Revenue"),
    ("Interest Income", "income", "Revenue"),
    ("Accounting Fees", "expense", "Expenses"),
    ("Bank Fees", "expense", "Expenses"),
    ("Filing Fees", "expense", "Expenses"),
    ("Investment Expenses", "expense", "Expenses"),
    ("Cash at Bank CBA0700", "asset", "Cash and Cash Equivalents"),
    ("Cash at Bank WBC8243", "asset", "Cash and Cash Equivalents"),
    ("Hub24 Cash Account", "asset", "Cash and Cash Equivalents"),
    ("Investments ANZ Capital Notes", "asset", "Investments"),
    ("EVP Fund III", "asset", "Investments"),
    ("Newmark Bourke St Mall Trust", "asset", "Investments"),
    ("Spire Branford Castle US Private Equity Fund II", "asset", "Investments"),
    ("Accrued expenses", "liability", "Current Liabilities"),
    ("Unpaid Present Entitlement", "liability", "Beneficiary Accounts"),
    ("Unsecured Loan", "liability", "Non Current Liabilities"),
]


def _prior_statement_account_code(index: int, account_type: str) -> str:
    prefix = {"asset": "1", "liability": "2", "income": "4", "expense": "6"}.get(account_type, "9")
    return f"{prefix}{index:03d}"


def _extract_prior_statement_accounts(state: EngagementState) -> list[ChartAccount]:
    accounts: list[ChartAccount] = []
    seen_names: set[str] = set()
    evidence_items = [item for item in state.evidence if item.source_type == "prior_year_financial_statements"]
    for evidence in evidence_items:
        quote = " ".join((evidence.quote or "").split())
        for name, account_type, group in _PRIOR_STATEMENT_ACCOUNT_SPECS:
            if name in seen_names or not re.search(re.escape(name), quote, re.IGNORECASE):
                continue
            match = re.search(rf"{re.escape(name)}\s+(?P<amount>-?\d[\d,]*(?:\.\d{{2}})?|-)", quote, re.IGNORECASE)
            amount = _clean_money_amount(match.group("amount")) if match and match.group("amount") != "-" else "0.00"
            code = _prior_statement_account_code(len(accounts) + 1, account_type)
            accounts.append(ChartAccount(account_id=f"prior_acct_{code}", code=code, name=name, type=account_type, presentation_group=group, opening_balance=amount or "0.00", source_evidence_refs=[evidence.evidence_id]))
            seen_names.add(name)
    return accounts


def _format_prior_statement_coa_import(payload: dict) -> str:
    lines = [f"# Prior Statement CoA Import — {payload['entity_name']}", ""]
    summary = payload["summary"]
    lines.extend([f"- Accounts imported: {summary['accounts_imported']}", f"- Approved automatically: {summary['approved']}", ""])
    if payload["accounts"]:
        lines.append("## Imported accounts pending review")
        for account in payload["accounts"]:
            lines.extend([f"- {account['code']} {account['name']}", f"  - Type: {account['type']}", f"  - Group: {account['presentation_group']}", f"  - Opening balance: {account['opening_balance']}", f"  - Evidence: {', '.join(account.get('source_evidence_refs', []))}"])
    if payload["findings"]:
        lines.extend(["", "## Findings needing review"])
        for finding in payload["findings"]:
            lines.extend([f"- {finding['category']}", f"  - Action: {finding['recommended_action']}"])
    return "\n".join(lines).rstrip() + "\n"


def _import_coa_from_prior_statements_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    accounts = _extract_prior_statement_accounts(state)
    findings: list[dict] = []
    if not accounts:
        findings.append({"category": "prior_statement_coa_not_extracted", "recommended_action": "Review prior-year financial statement evidence or import a trial balance CSV before CoA mapping."})
    state.chart_accounts = [account for account in state.chart_accounts if not account.account_id.startswith("prior_acct_")]
    state.chart_accounts.extend(accounts)
    state.coa_review_required = True
    state.coa_review_status = "pending_review"
    save_engagement_state(state_path, state)
    payload = {"engagement_id": state.engagement_id, "entity_name": state.entity_name, "accounts": [account.model_dump() for account in accounts], "findings": findings, "summary": {"accounts_imported": len(accounts), "findings": len(findings), "approved": 0}}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_prior_statement_coa_import(payload))
    output.with_suffix(".json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Imported prior statement CoA accounts → {output}")
    return 0 if accounts and not findings else 1


def _render_xlsx_statements_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ("Draft Financial Statements", ""),
        ("Entity", state.entity_name),
        ("Financial year", f"{state.fy_start} to {state.fy_end}"),
        ("CoA accounts", str(len(state.chart_accounts))),
        ("Open exceptions", str(len(state.open_exceptions()))),
    ]
    sheet_rows = []
    for idx, (label, value) in enumerate(rows, start=1):
        sheet_rows.append(f'<row r="{idx}"><c r="A{idx}" t="inlineStr"><is><t>{_html_escape(label)}</t></is></c><c r="B{idx}" t="inlineStr"><is><t>{_html_escape(value)}</t></is></c></row>')
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Statements" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')
    status = "passed" if output.exists() else "failed"
    verifier = {"output_id": "out_xlsx_statements", "file_path": str(output), "artifact_type": "xlsx_financial_statements", "status": status, "checks": [{"check": "xlsx_zip_structure", "status": status}]}
    verifier_path = Path(args.verifier_result)
    verifier_path.parent.mkdir(parents=True, exist_ok=True)
    verifier_path.write_text(json.dumps(verifier, indent=2, sort_keys=True))
    state.output_artifacts = [item for item in state.output_artifacts if item.output_id != "out_xlsx_statements"]
    state.output_artifacts.append(OutputArtifact(output_id="out_xlsx_statements", file_path=str(output), artifact_type="xlsx_financial_statements", verifier_status=status, created_at=datetime.now(timezone.utc).isoformat(), source_state_hash=state_hash(state)))
    state.statements_ref = str(output)
    save_engagement_state(state_path, state)
    print(f"Rendered XLSX statements → {output}")
    return 0 if status == "passed" else 1


def _export_local_ui_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    review_href = Path(args.review_ui).name
    html = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Internal Accountant Copilot — {_html_escape(state.entity_name)}</title></head>
<body>
<h1>Internal Accountant Copilot — {_html_escape(state.entity_name)}</h1>
<p>This local wrapper links the safe review artifacts for internal use.</p>
<ul>
<li><a href=\"{_html_escape(review_href)}\">Open accountant review UI</a></li>
<li>State: <code>{_html_escape(str(Path(args.state)))}</code></li>
</ul>
</body></html>
"""
    output.write_text(html)
    print(f"Exported local UI wrapper → {output}")
    return 0


def _read_json_if_exists(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _setup_turing_workspace_summary(
    *,
    state: EngagementState,
    output_dir: Path,
    command_results: list[tuple[str, int]],
    readiness: dict,
) -> str:
    inventory = _read_json_if_exists(output_dir / "document_inventory.json")
    bank_facts = _read_json_if_exists(output_dir / "bank_statement_facts.json")
    bank_transactions = _read_json_if_exists(output_dir / "bank_transactions.json")
    bank_continuity = _read_json_if_exists(output_dir / "bank_continuity.json")
    invoice_facts = _read_json_if_exists(output_dir / "invoice_facts.json")
    invoice_review = _read_json_if_exists(output_dir / "invoice_review.json")
    distribution_tax = _read_json_if_exists(output_dir / "distribution_tax_facts.json")
    open_exceptions = state.open_exceptions()
    step_findings = [command for command, code in command_results if code != 0]
    lines = [
        f"# Turing Financial Statement Automation Setup — {state.entity_name}",
        "",
        f"Generated (UTC): {datetime.now(timezone.utc).date().isoformat()}",
        "",
        "## Inputs",
        f"- Input folder: `{state.documents_ref}`",
        f"- State: `{output_dir / 'engagement_state.json'}`",
        "",
        "## Command Results",
    ]
    for command, code in command_results:
        lines.append(f"- {command}: exit {code}")
    lines.extend(
        [
            "",
            "## Summary",
            f"- Source documents registered: {len(state.source_documents)}",
            f"- Evidence refs extracted: {len(state.evidence)}",
            f"- Open exceptions: {len(open_exceptions)}",
            f"- Blocking exceptions: {readiness.get('blocking_exception_count', 0)}",
            f"- Setup review steps with findings: {len(step_findings)}",
            f"- Final output allowed: {'YES' if readiness.get('final_output_allowed') else 'NO'}",
            f"- Readiness: {readiness.get('readiness_summary')}",
            "",
            "## Extracted Artifact Counts",
            f"- Document inventory documents: {len(inventory.get('documents', []))}",
            f"- Bank facts extracted: {bank_facts.get('summary', {}).get('facts_extracted', 0)}",
            f"- Bank fact findings: {bank_facts.get('summary', {}).get('findings', 0)}",
            f"- Bank transactions extracted: {bank_transactions.get('summary', {}).get('transactions_extracted', 0)}",
            f"- Bank transaction findings: {bank_transactions.get('summary', {}).get('findings', 0)}",
            f"- Bank continuity findings: {bank_continuity.get('summary', {}).get('findings', 0)}",
            f"- Invoice facts extracted: {invoice_facts.get('summary', {}).get('facts_extracted', 0)}",
            f"- Invoice review findings: {invoice_review.get('summary', {}).get('review_findings', 0)}",
            f"- Distribution/tax facts extracted: {distribution_tax.get('summary', {}).get('facts_extracted', 0)}",
            f"- Distribution/tax findings: {distribution_tax.get('summary', {}).get('findings', 0)}",
            "",
            "## Outputs",
            f"- Review packet: `{output_dir / 'review_packet'}`",
            f"- Review UI: `{output_dir / 'review.html'}`",
            f"- Local UI wrapper: `{output_dir / 'local_ui' / 'index.html'}`",
            f"- Statement package: `{output_dir / 'statement_package'}`",
            f"- Document inventory: `{output_dir / 'document_inventory.md'}`",
            f"- Bank facts: `{output_dir / 'bank_statement_facts.md'}`",
            f"- Bank continuity: `{output_dir / 'bank_continuity.md'}`",
            f"- Bank transactions: `{output_dir / 'bank_transactions.md'}`",
            f"- Invoice facts: `{output_dir / 'invoice_facts.md'}`",
            f"- Invoice review: `{output_dir / 'invoice_review.md'}`",
            f"- Distribution/tax facts: `{output_dir / 'distribution_tax_facts.md'}`",
            "",
            "## Control Note",
            "This setup registers and extracts source-linked evidence for review. It does not approve accounting treatment, release statements, or override open exceptions.",
        ]
    )
    if open_exceptions:
        lines.extend(["", "## Open Exceptions"])
        for item in open_exceptions:
            lines.append(f"- `{item.exception_id}` [{item.severity.value}] {item.category}: {item.description}")
    if step_findings:
        lines.extend(["", "## Review Steps With Findings"])
        for command in step_findings:
            lines.append(f"- {command}")
    return "\n".join(lines).rstrip() + "\n"


def _setup_turing_workspace_command(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    state_path = Path(args.state) if args.state else output_dir / "engagement_state.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    state = EngagementState(
        engagement_id=args.engagement_id,
        entity_name=args.entity_name,
        entity_type=args.entity_type,
        fy_start=args.fy_start,
        fy_end=args.fy_end,
        documents_ref=args.input_dir,
        coa_ref=args.input_dir,
    )
    save_engagement_state(state_path, state)

    command_results: list[tuple[str, int]] = []

    def run_step(name: str, func, namespace: argparse.Namespace) -> int:
        code = func(namespace)
        command_results.append((name, code))
        return code

    run_step("ingest-raw-inputs", _ingest_raw_inputs_command, argparse.Namespace(state=str(state_path), input_dir=args.input_dir))
    run_step("render-statement-package", _render_statement_package_command, argparse.Namespace(state=str(state_path), output_dir=str(output_dir / "statement_package")))
    run_step("export-review-packet", _export_review_packet_command, argparse.Namespace(state=str(state_path), output_dir=str(output_dir / "review_packet")))
    run_step("export-review-ui", _export_review_ui_command, argparse.Namespace(state=str(state_path), output=str(output_dir / "review.html")))
    run_step("export-local-ui", _export_local_ui_command, argparse.Namespace(state=str(state_path), review_ui=str(output_dir / "review.html"), output=str(output_dir / "local_ui" / "index.html")))
    run_step("export-document-inventory", _export_document_inventory_command, argparse.Namespace(state=str(state_path), output=str(output_dir / "document_inventory.md")))
    run_step("export-bank-statement-facts", _export_bank_statement_facts_command, argparse.Namespace(state=str(state_path), output=str(output_dir / "bank_statement_facts.md")))
    if (output_dir / "bank_statement_facts.json").exists():
        run_step("export-bank-continuity", _export_bank_continuity_command, argparse.Namespace(facts=str(output_dir / "bank_statement_facts.json"), output=str(output_dir / "bank_continuity.md")))
    run_step("export-bank-transactions", _export_bank_transactions_command, argparse.Namespace(state=str(state_path), output=str(output_dir / "bank_transactions.md")))
    run_step("export-invoice-facts", _export_invoice_facts_command, argparse.Namespace(state=str(state_path), output=str(output_dir / "invoice_facts.md")))
    if (output_dir / "invoice_facts.json").exists():
        run_step("export-invoice-review", _export_invoice_review_command, argparse.Namespace(facts=str(output_dir / "invoice_facts.json"), output=str(output_dir / "invoice_review.md")))
    run_step("export-distribution-tax-facts", _export_distribution_tax_facts_command, argparse.Namespace(state=str(state_path), output=str(output_dir / "distribution_tax_facts.md")))

    state = load_engagement_state(state_path)
    readiness = inspect_engagement(state)
    summary = _setup_turing_workspace_summary(state=state, output_dir=output_dir, command_results=command_results, readiness=readiness)
    summary_path = output_dir / "SETUP_RESULTS.md"
    summary_path.write_text(summary)
    print(f"Turing workspace setup summary → {summary_path}")
    has_step_findings = any(code != 0 for _, code in command_results)
    return 0 if readiness["final_output_allowed"] and not has_step_findings else 1


def _run_demo_command(args: argparse.Namespace) -> int:
    base = Path(args.output_dir)
    blocked = base / "blocked"
    clean = base / "clean"
    blocked.mkdir(parents=True, exist_ok=True)
    clean.mkdir(parents=True, exist_ok=True)
    blocked_state = EngagementState(
        engagement_id="demo_blocked",
        entity_name="Demo Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        exceptions=[ExceptionItem(source="demo", severity=ExceptionSeverity.HIGH, category="unmatched_bank_transaction", description="Demo item needs review.", recommended_action="Classify the demo item.", requires_human_approval=True)],
    )
    blocked_state_path = blocked / "state.json"
    save_engagement_state(blocked_state_path, blocked_state)
    _run_engagement_command(argparse.Namespace(state=str(blocked_state_path), review_packet_dir=str(blocked / "review_packet"), release_manifest=str(blocked / "release_manifest.json")))
    clean_state = EngagementState(
        engagement_id="demo_clean",
        entity_name="Demo Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        decisions=[AccountantDecision(decision_id="decision_final_signoff_0001", question="release?", selected_option="final_signoff", rationale="Approved demo.", status=DecisionStatus.APPROVED, approved_by="Demo Reviewer")],
    )
    clean_state_path = clean / "state.json"
    save_engagement_state(clean_state_path, clean_state)
    _run_engagement_command(argparse.Namespace(state=str(clean_state_path), review_packet_dir=str(clean / "review_packet"), release_manifest=str(clean / "release_manifest.json")))
    print(f"Demo outputs written → {base}")
    return 0


def _import_verifier_result_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    before = state_hash(state)
    payload = json.loads(Path(args.verifier_result).read_text())
    status = payload.get("status") or payload.get("verifier_status") or "not_run"
    artifact = OutputArtifact(
        output_id=payload.get("output_id", "out_verifier"),
        file_path=payload.get("file_path", "unknown output"),
        artifact_type=payload.get("artifact_type", "financial_statements"),
        verifier_status=status,
        created_at=datetime.now(timezone.utc).isoformat(),
        source_state_hash=before,
    )
    state.output_artifacts = [item for item in state.output_artifacts if item.output_id != artifact.output_id]
    state.output_artifacts.append(artifact)
    state.exceptions = [item for item in state.exceptions if not item.exception_id.startswith(f"output_verifier_{artifact.output_id}_")]
    if status != "passed":
        for idx, finding in enumerate(payload.get("findings", []) or [{"check": "verifier", "detail": "Verifier failed."}], start=1):
            check = finding.get("check", "verifier")
            state.exceptions.append(
                ExceptionItem(
                    exception_id=f"output_verifier_{artifact.output_id}_{idx:04d}",
                    source="output_verifier",
                    severity=ExceptionSeverity.CRITICAL,
                    category=f"output_{check}",
                    description=str(finding.get("detail", "Output verifier failed.")),
                    evidence_refs=[artifact.output_id],
                    recommended_action="Resolve failed output verifier finding before release.",
                    requires_human_approval=True,
                )
            )
    _record_state_transition(state, command="import-verifier-result", before_hash=before, summary=f"Imported verifier result {status}.")
    save_engagement_state(state_path, state)
    print(f"Imported verifier result for {artifact.output_id}: {status}")
    return 0 if status == "passed" else 1


_TEMPLATE_RULES = {
    "discretionary_trust": [
        "Review beneficiary distributions before final sign-off.",
        "Confirm trustee/accounting presentation for beneficiary distributions.",
        "Check retained earnings or settled sum presentation against firm preference.",
    ],
    "company": ["Review retained earnings and tax provision presentation."],
    "individual": ["Review proprietor drawings and tax-related classifications."],
    "partnership": ["Review partner capital and distribution allocations."],
}


def _recommend_templates_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    entity_type = state.entity_type or "unknown"
    rules = _TEMPLATE_RULES.get(entity_type, [])
    print(f"Entity template recommendations: {entity_type}")
    if not rules:
        print("No entity template rules recorded for this entity type.")
    for rule in rules:
        print(f"- {rule}")
    return 0


def _html_escape(value: str | None) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _export_review_ui_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    payload = inspect_engagement(state)
    exception_items = "\n".join(
        f"<li><strong>{_html_escape(item.exception_id)}</strong> [{item.severity.value}] {_html_escape(item.description)}</li>"
        for item in state.open_exceptions()
    ) or "<li>No open exceptions.</li>"
    evidence_items = "\n".join(
        f"<li>{_html_escape(ev.evidence_id)} — document={_html_escape(ev.document_id)} quote={_html_escape(ev.quote)}</li>"
        for ev in state.evidence
    ) or "<li>No structured evidence recorded.</li>"
    decision_template = json.dumps({
        "engagement_id": state.engagement_id,
        "decisions": [
            {
                "exception_id": item.exception_id,
                "action": "resolved",
                "approved_by": "Reviewer Name",
                "rationale": "Document accountant conclusion here.",
            }
            for item in state.open_exceptions()
        ],
        "coa_decisions": [{"account_id": account.account_id, "action": "approve", "rationale": "Approve CoA account."} for account in state.chart_accounts],
        "adjustment_decisions": [{"adjustment_id": adj.adjustment_id, "action": "approve", "rationale": "Approve adjustment."} for adj in state.adjustment_proposals],
        "preference_decisions": [],
        "output_verifier_decisions": [{"output_id": artifact.output_id, "action": "accept_verifier_status", "status": artifact.verifier_status} for artifact in state.output_artifacts],
    }, indent=2)
    html = f"""<!doctype html>
<html>
<head><meta charset=\"utf-8\"><title>Accountant Review — {_html_escape(state.entity_name)}</title></head>
<body>
<h1>Accountant Review — {_html_escape(state.entity_name)}</h1>
<p>Readiness: {_html_escape(payload['readiness_summary'])}</p>
<h2>Open exceptions</h2><ul>{exception_items}</ul>
<h2>Evidence</h2><ul>{evidence_items}</ul>
<h2>CoA accounts</h2><ul>{''.join(f'<li>{_html_escape(a.account_id)} [{_html_escape(a.status)}] {_html_escape(a.name)}</li>' for a in state.chart_accounts) or '<li>No CoA accounts recorded.</li>'}</ul>
<h2>Adjustments</h2><ul>{''.join(f'<li>{_html_escape(a.adjustment_id)} [{_html_escape(a.status)}] {_html_escape(a.description)}</li>' for a in state.adjustment_proposals) or '<li>No adjustments recorded.</li>'}</ul>
<h2>Copy decision JSON</h2>
<p>Save this as <code>review_decisions_template.json</code>, edit the rationale/actions, then apply with <code>review-exceptions --decisions</code>.</p>
<textarea rows=\"16\" cols=\"100\">{_html_escape(decision_template)}</textarea>
</body>
</html>
"""
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html)
    print(f"Exported review UI → {output}")
    return 0 if payload["final_output_allowed"] else 1


def _export_release_manifest_file(state_path: Path, output: Path, workpaper_pack: str | None = None, audit_trail: str | None = None) -> int:
    state = load_engagement_state(state_path)
    signoff = _final_signoff_decision(state)
    if signoff is None:
        print("Cannot export release manifest before final sign-off", file=sys.stderr)
        return 1
    failed_outputs = [artifact for artifact in state.output_artifacts if artifact.verifier_status != "passed"]
    if failed_outputs:
        print("Cannot export release manifest: verifier status is not passing", file=sys.stderr)
        return 1
    state.lifecycle_status = "released"
    final_hash = state_hash(state)
    save_engagement_state(state_path, state)
    payload = inspect_engagement(state)
    manifest = {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "lifecycle_status": state.lifecycle_status,
        "final_output_allowed": payload["final_output_allowed"],
        "signoff_decision_id": signoff.decision_id,
        "final_state_hash": final_hash,
        "output_artifact_ids": [artifact.output_id for artifact in state.output_artifacts],
        "workpaper_pack": workpaper_pack,
        "audit_trail": audit_trail,
        "created_outputs": [ref for ref in [state.statements_ref, workpaper_pack, audit_trail] if ref],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"Exported release manifest → {output}")
    return 0


def _apply_review_ui_decisions_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    payload = json.loads(Path(args.decisions).read_text())
    if payload.get("engagement_id") not in {None, state.engagement_id}:
        print("Decision file engagement_id does not match state", file=sys.stderr)
        return 2
    applied = 0
    for item in payload.get("decisions", []):
        if not item.get("action"):
            continue
        exception = _find_exception(state, item["exception_id"])
        action = ExceptionStatus(item["action"])
        _record_review_decision(state=state, item=exception, action=action, rationale=item.get("rationale", ""), approved_by=item.get("approved_by", ""))
        applied += 1
    for item in payload.get("coa_decisions", []):
        if item.get("action") != "approve":
            continue
        account = next((account for account in state.chart_accounts if account.account_id == item.get("account_id")), None)
        if account is None:
            _usage_error(f"Unknown account_id: {item.get('account_id')}")
        account.status = "approved"
        state.decisions.append(AccountantDecision(decision_id=f"decision_approve_coa_{len(state.decisions) + 1:04d}", question=f"Approve CoA account {account.account_id}?", selected_option="approve_coa", rationale=item.get("rationale", ""), status=DecisionStatus.APPROVED, approved_by=item.get("approved_by", ""), evidence_refs=account.source_evidence_refs))
        applied += 1
    if state.chart_accounts and not [account for account in state.chart_accounts if account.status != "approved"]:
        state.coa_review_required = True
        state.coa_review_status = "approved"
    for item in payload.get("adjustment_decisions", []):
        adjustment = next((adj for adj in state.adjustment_proposals if adj.adjustment_id == item.get("adjustment_id")), None)
        if adjustment is None:
            _usage_error(f"Unknown adjustment_id: {item.get('adjustment_id')}")
        action = item.get("action")
        adjustment.status = "approved" if action == "approve" else "rejected"
        decision = AccountantDecision(decision_id=f"decision_{action}_adjustment_{len(state.decisions) + 1:04d}", question=f"{action} adjustment {adjustment.adjustment_id}?", selected_option=f"{action}_adjustment", rationale=item.get("rationale", ""), status=DecisionStatus.APPROVED, approved_by=item.get("approved_by", ""), evidence_refs=adjustment.source_evidence_refs)
        state.decisions.append(decision)
        adjustment.decision_id = decision.decision_id
        applied += 1
    if state.adjustment_proposals and not [adj for adj in state.adjustment_proposals if adj.status != "approved"]:
        state.adjustment_review_status = "approved"
    for item in payload.get("preference_decisions", []):
        pref = next((pref for pref in state.preferences if pref.preference_id == item.get("preference_id")), None)
        if pref and item.get("action") == "apply":
            state.decisions.append(AccountantDecision(decision_id=f"decision_apply_preference_{len(state.decisions) + 1:04d}", question=f"Apply preference {pref.preference_id}?", selected_option="apply_preference", rationale=item.get("rationale", ""), status=DecisionStatus.APPROVED, approved_by=item.get("approved_by", ""), evidence_refs=[pref.preference_id]))
            applied += 1
    for item in payload.get("output_verifier_decisions", []):
        if item.get("action"):
            state.decisions.append(AccountantDecision(decision_id=f"decision_output_verifier_{len(state.decisions) + 1:04d}", question=f"Record output verifier decision {item.get('output_id')}?", selected_option=item.get("action", "output_verifier_decision"), rationale=item.get("rationale", "Verifier reviewed."), status=DecisionStatus.APPROVED, approved_by=item.get("approved_by", ""), evidence_refs=[item.get("output_id", "")]))
            applied += 1
    save_engagement_state(state_path, state)
    print(f"Applied {applied} review UI decisions")
    return 0


def _run_engagement_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    before = state_hash(state)

    if getattr(args, "input_dir", None):
        _ingest_raw_inputs_command(argparse.Namespace(state=str(state_path), input_dir=args.input_dir))
    if getattr(args, "bank_csv", None):
        _ingest_source_document_command(argparse.Namespace(state=str(state_path), document_id="doc_bank", file_path=args.bank_csv, document_type="bank_statement", entity=state.entity_name, period_start=state.fy_start, period_end=state.fy_end, notes="Internal run source intake."))
    if getattr(args, "events_csv", None):
        _ingest_source_document_command(argparse.Namespace(state=str(state_path), document_id="doc_events", file_path=args.events_csv, document_type="supporting_events", entity=state.entity_name, period_start=state.fy_start, period_end=state.fy_end, notes="Internal run source intake."))
    if getattr(args, "trial_balance_csv", None):
        _import_trial_balance_command(argparse.Namespace(state=str(state_path), trial_balance_csv=args.trial_balance_csv))
    if getattr(args, "bank_csv", None) and getattr(args, "events_csv", None):
        matches_path = getattr(args, "matches_output", None) or str(Path(args.review_packet_dir).parent / "matches.json")
        _match_transactions_command(argparse.Namespace(state=str(state_path), bank_csv=args.bank_csv, events_csv=args.events_csv, output=matches_path, amount_tolerance=getattr(args, "amount_tolerance", "0"), date_window_days=getattr(args, "date_window_days", "0")))
    if getattr(args, "statement_package_dir", None):
        _render_statement_package_command(argparse.Namespace(state=str(state_path), output_dir=args.statement_package_dir))

    state = load_engagement_state(state_path)
    payload = inspect_engagement(state)
    if not payload["final_output_allowed"]:
        packet_dir = Path(args.review_packet_dir)
        _export_review_packet_command(argparse.Namespace(state=str(state_path), output_dir=str(packet_dir)))
        if getattr(args, "review_ui", None):
            _export_review_ui_command(argparse.Namespace(state=str(state_path), output=args.review_ui))
        state = load_engagement_state(state_path)
        _record_state_transition(state, command="run-engagement", before_hash=before, summary="Engagement blocked; review packet and UI exported.")
        save_engagement_state(state_path, state)
        print(f"Engagement blocked; review packet exported → {packet_dir}")
        return 1
    _record_state_transition(state, command="run-engagement", before_hash=before, summary="Engagement ready for release manifest.")
    save_engagement_state(state_path, state)
    result = _export_release_manifest_file(state_path, Path(args.release_manifest))
    print("Engagement ready")
    return result


def _import_source_exceptions_command(args: argparse.Namespace) -> int:
    imported = import_source_pipeline_controls(
        matching_path=Path(args.matching),
        journal_path=Path(args.journal),
    )
    state = EngagementState(
        engagement_id=args.engagement_id,
        entity_name=args.entity_name,
        entity_type=args.entity_type,
        fy_start=args.fy_start,
        fy_end=args.fy_end,
        documents_ref=args.documents_ref,
        coa_ref=args.coa_ref,
        bank_txns_ref=args.bank_txns_ref,
        events_ref=args.events_ref,
        matches_ref=str(Path(args.matching)),
        journals_ref=str(Path(args.journal)),
        exceptions=imported.exceptions,
        evidence=imported.evidence,
        lifecycle_status="evidence_imported" if imported.evidence else "intake",
    )
    output_path = Path(args.output)
    save_engagement_state(output_path, state)
    noun = "exception" if len(imported.exceptions) == 1 else "exceptions"
    print(f"Imported {len(imported.exceptions)} source pipeline {noun} and {len(imported.evidence)} evidence refs → {output_path}")
    payload = inspect_engagement(state)
    print(format_inspection(payload), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accountant-copilot",
        description="Agentic Accountant Copilot command-line tools.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect-engagement",
        help="Inspect engagement state, readiness, exceptions, and next task.",
    )
    inspect_parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE_PATH),
        help=f"Path to engagement_state.json (default: {DEFAULT_STATE_PATH})",
    )
    inspect_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of text.",
    )
    inspect_parser.set_defaults(func=_inspect_engagement_command)

    run_parser = subparsers.add_parser(
        "run-engagement",
        help="Run deterministic engagement orchestration and stop at review gates.",
    )
    run_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    run_parser.add_argument("--review-packet-dir", default="outputs/review_packet")
    run_parser.add_argument("--release-manifest", default="outputs/release_manifest.json")
    run_parser.add_argument("--bank-csv", default=None)
    run_parser.add_argument("--input-dir", default=None)
    run_parser.add_argument("--events-csv", default=None)
    run_parser.add_argument("--trial-balance-csv", default=None)
    run_parser.add_argument("--matches-output", default=None)
    run_parser.add_argument("--statement-package-dir", default=None)
    run_parser.add_argument("--review-ui", default=None)
    run_parser.add_argument("--amount-tolerance", default="0")
    run_parser.add_argument("--date-window-days", default="0")
    run_parser.set_defaults(func=_run_engagement_command)

    apply_review_ui_parser = subparsers.add_parser(
        "apply-review-ui-decisions",
        help="Apply accountant decisions copied from the review UI JSON.",
    )
    apply_review_ui_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    apply_review_ui_parser.add_argument("--decisions", required=True)
    apply_review_ui_parser.set_defaults(func=_apply_review_ui_decisions_command)

    ingest_parser = subparsers.add_parser(
        "ingest-raw-inputs",
        help="Register raw input files and create extraction-required review exceptions.",
    )
    ingest_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    ingest_parser.add_argument("--input-dir", default="inputs")
    ingest_parser.set_defaults(func=_ingest_raw_inputs_command)

    ingest_parser = subparsers.add_parser(
        "ingest-source-document",
        help="Ingest a CSV source document into the document and evidence registers.",
    )
    ingest_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    ingest_parser.add_argument("--document-id", required=True)
    ingest_parser.add_argument("--file-path", required=True)
    ingest_parser.add_argument("--document-type", required=True)
    ingest_parser.add_argument("--entity", required=True)
    ingest_parser.add_argument("--period-start", required=True)
    ingest_parser.add_argument("--period-end", required=True)
    ingest_parser.add_argument("--notes", default=None)
    ingest_parser.set_defaults(func=_ingest_source_document_command)

    match_parser = subparsers.add_parser(
        "match-transactions",
        help="Run deterministic date/amount matching and create review exceptions.",
    )
    match_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    match_parser.add_argument("--bank-csv", required=True)
    match_parser.add_argument("--events-csv", required=True)
    match_parser.add_argument("--output", required=True)
    match_parser.add_argument("--amount-tolerance", default="0")
    match_parser.add_argument("--date-window-days", default="0")
    match_parser.set_defaults(func=_match_transactions_command)

    render_parser = subparsers.add_parser(
        "render-draft-statements",
        help="Render a draft financial statement artifact and verifier result.",
    )
    render_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    render_parser.add_argument("--output", required=True)
    render_parser.add_argument("--verifier-result", required=True)
    render_parser.set_defaults(func=_render_draft_statements_command)

    package_parser = subparsers.add_parser(
        "render-statement-package",
        help="Render a structured draft statement package with verifier detail.",
    )
    package_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    package_parser.add_argument("--output-dir", required=True)
    package_parser.set_defaults(func=_render_statement_package_command)

    tb_parser = subparsers.add_parser(
        "import-trial-balance",
        help="Import trial balance CSV into structured CoA accounts and review exceptions.",
    )
    tb_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    tb_parser.add_argument("--trial-balance-csv", required=True)
    tb_parser.set_defaults(func=_import_trial_balance_command)

    prior_coa_parser = subparsers.add_parser(
        "import-coa-from-prior-statements",
        help="Import candidate CoA accounts from prior-year financial statement evidence for accountant review.",
    )
    prior_coa_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    prior_coa_parser.add_argument("--output", default="outputs/prior_statement_coa_import.md")
    prior_coa_parser.set_defaults(func=_import_coa_from_prior_statements_command)

    xlsx_parser = subparsers.add_parser(
        "render-xlsx-statements",
        help="Render XLSX financial statements with verifier detail.",
    )
    xlsx_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    xlsx_parser.add_argument("--output", required=True)
    xlsx_parser.add_argument("--verifier-result", required=True)
    xlsx_parser.set_defaults(func=_render_xlsx_statements_command)

    local_ui_parser = subparsers.add_parser(
        "export-local-ui",
        help="Export a local internal UI wrapper linking review artifacts.",
    )
    local_ui_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    local_ui_parser.add_argument("--review-ui", required=True)
    local_ui_parser.add_argument("--output", required=True)
    local_ui_parser.set_defaults(func=_export_local_ui_command)

    setup_turing_parser = subparsers.add_parser(
        "setup-turing-workspace",
        help="Rebuild the local Turing financial statement automation review workspace from raw inputs.",
    )
    setup_turing_parser.add_argument("--input-dir", default="inputs")
    setup_turing_parser.add_argument("--output-dir", default="outputs/turing_financial_statement_setup")
    setup_turing_parser.add_argument("--state", default=None)
    setup_turing_parser.add_argument("--engagement-id", default=DEFAULT_TURING_ENGAGEMENT_ID)
    setup_turing_parser.add_argument("--entity-name", default=DEFAULT_TURING_ENTITY_NAME)
    setup_turing_parser.add_argument("--entity-type", default="discretionary_trust")
    setup_turing_parser.add_argument("--fy-start", default="2024-07-01")
    setup_turing_parser.add_argument("--fy-end", default="2025-06-30")
    setup_turing_parser.set_defaults(func=_setup_turing_workspace_command)

    demo_parser = subparsers.add_parser(
        "run-demo",
        help="Create a safe sample engagement demo with blocked and clean paths.",
    )
    demo_parser.add_argument("--output-dir", required=True)
    demo_parser.set_defaults(func=_run_demo_command)

    validate_parser = subparsers.add_parser(
        "validate-state",
        help="Validate engagement state JSON before running state-changing commands.",
    )
    validate_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    validate_parser.set_defaults(func=_validate_state_command)

    inventory_parser = subparsers.add_parser(
        "export-document-inventory",
        help="Export high-level source document and page evidence inventory.",
    )
    inventory_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    inventory_parser.add_argument("--output", default="outputs/document_inventory.md")
    inventory_parser.set_defaults(func=_export_document_inventory_command)

    bank_facts_parser = subparsers.add_parser(
        "export-bank-statement-facts",
        help="Extract bank statement periods, opening balances, closing balances, and summary totals from page evidence.",
    )
    bank_facts_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    bank_facts_parser.add_argument("--output", default="outputs/bank_statement_facts.md")
    bank_facts_parser.set_defaults(func=_export_bank_statement_facts_command)

    bank_transactions_parser = subparsers.add_parser(
        "export-bank-transactions",
        help="Extract evidence-linked bank transaction rows from page evidence.",
    )
    bank_transactions_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    bank_transactions_parser.add_argument("--output", default="outputs/bank_transactions.md")
    bank_transactions_parser.set_defaults(func=_export_bank_transactions_command)

    invoice_facts_parser = subparsers.add_parser(
        "export-invoice-facts",
        help="Extract evidence-linked invoice facts from source evidence.",
    )
    invoice_facts_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    invoice_facts_parser.add_argument("--output", default="outputs/invoice_facts.md")
    invoice_facts_parser.set_defaults(func=_export_invoice_facts_command)

    invoice_review_parser = subparsers.add_parser(
        "export-invoice-review",
        help="Create accountant review findings from extracted invoice facts without auto-approval.",
    )
    invoice_review_parser.add_argument("--facts", default="outputs/invoice_facts.json")
    invoice_review_parser.add_argument("--output", default="outputs/invoice_review.md")
    invoice_review_parser.set_defaults(func=_export_invoice_review_command)

    distribution_tax_parser = subparsers.add_parser(
        "export-distribution-tax-facts",
        help="Extract evidence-linked distribution and tax statement facts from source evidence.",
    )
    distribution_tax_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    distribution_tax_parser.add_argument("--output", default="outputs/distribution_tax_facts.md")
    distribution_tax_parser.set_defaults(func=_export_distribution_tax_facts_command)

    distribution_tax_review_parser = subparsers.add_parser(
        "export-distribution-tax-review",
        help="Create accountant review findings from extracted distribution/tax facts without auto-approval.",
    )
    distribution_tax_review_parser.add_argument("--facts", default="outputs/distribution_tax_facts.json")
    distribution_tax_review_parser.add_argument("--output", default="outputs/distribution_tax_review.md")
    distribution_tax_review_parser.set_defaults(func=_export_distribution_tax_review_command)

    broker_trade_facts_parser = subparsers.add_parser(
        "export-broker-trade-facts",
        help="Extract evidence-linked broker trade facts from confirmation evidence.",
    )
    broker_trade_facts_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    broker_trade_facts_parser.add_argument("--output", default="outputs/broker_trade_facts.md")
    broker_trade_facts_parser.set_defaults(func=_export_broker_trade_facts_command)

    broker_trade_review_parser = subparsers.add_parser(
        "export-broker-trade-review",
        help="Create accountant review findings from extracted broker trade facts without auto-approval.",
    )
    broker_trade_review_parser.add_argument("--facts", default="outputs/broker_trade_facts.json")
    broker_trade_review_parser.add_argument("--output", default="outputs/broker_trade_review.md")
    broker_trade_review_parser.set_defaults(func=_export_broker_trade_review_command)

    source_fact_match_parser = subparsers.add_parser(
        "match-source-facts",
        help="Match extracted invoice, distribution/tax, and broker source facts to bank transaction evidence.",
    )
    source_fact_match_parser.add_argument("--bank-transactions", required=True)
    source_fact_match_parser.add_argument("--invoice-facts", default=None)
    source_fact_match_parser.add_argument("--distribution-tax-facts", default=None)
    source_fact_match_parser.add_argument("--broker-trade-facts", default=None)
    source_fact_match_parser.add_argument("--output", default="outputs/source_fact_matches.md")
    source_fact_match_parser.set_defaults(func=_match_source_facts_command)

    coa_mapping_parser = subparsers.add_parser(
        "suggest-coa-mappings",
        help="Suggest unapproved CoA mappings for extracted source facts and emit accountant review findings.",
    )
    coa_mapping_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    coa_mapping_parser.add_argument("--invoice-facts", default=None)
    coa_mapping_parser.add_argument("--distribution-tax-facts", default=None)
    coa_mapping_parser.add_argument("--broker-trade-facts", default=None)
    coa_mapping_parser.add_argument("--output", default="outputs/coa_mapping_suggestions.md")
    coa_mapping_parser.set_defaults(func=_suggest_coa_mappings_command)

    coa_mapping_template_parser = subparsers.add_parser(
        "export-coa-mapping-template",
        help="Export a JSON decision template for CoA mapping suggestions.",
    )
    coa_mapping_template_parser.add_argument("--mappings", required=True)
    coa_mapping_template_parser.add_argument("--output", required=True)
    coa_mapping_template_parser.set_defaults(func=_export_coa_mapping_template_command)

    coa_mapping_apply_parser = subparsers.add_parser(
        "apply-coa-mapping-decisions",
        help="Apply accountant decisions for CoA mapping suggestions.",
    )
    coa_mapping_apply_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    coa_mapping_apply_parser.add_argument("--mappings", required=True)
    coa_mapping_apply_parser.add_argument("--decisions", required=True)
    coa_mapping_apply_parser.add_argument("--output", required=True)
    coa_mapping_apply_parser.set_defaults(func=_apply_coa_mapping_decisions_command)

    journal_proposal_parser = subparsers.add_parser(
        "propose-journals",
        help="Create pending-review journal proposals from approved CoA mapping decisions.",
    )
    journal_proposal_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    journal_proposal_parser.add_argument("--applied-mappings", required=True)
    journal_proposal_parser.add_argument("--output", default="outputs/journal_proposals.md")
    journal_proposal_parser.add_argument("--date", default=None)
    journal_proposal_parser.set_defaults(func=_propose_journals_command)

    journal_template_parser = subparsers.add_parser(
        "export-journal-decision-template",
        help="Export a JSON decision template for pending journal proposals.",
    )
    journal_template_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    journal_template_parser.add_argument("--output", required=True)
    journal_template_parser.set_defaults(func=_export_journal_decision_template_command)

    journal_apply_parser = subparsers.add_parser(
        "apply-journal-decisions",
        help="Apply accountant approvals/rejections for journal proposals and resolve offset placeholders.",
    )
    journal_apply_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    journal_apply_parser.add_argument("--decisions", required=True)
    journal_apply_parser.add_argument("--output", required=True)
    journal_apply_parser.set_defaults(func=_apply_journal_decisions_command)

    tb_preview_parser = subparsers.add_parser(
        "preview-tb-impact",
        help="Preview trial balance impact from approved journal proposals.",
    )
    tb_preview_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    tb_preview_parser.add_argument("--output", default="outputs/tb_impact_preview.md")
    tb_preview_parser.set_defaults(func=_preview_tb_impact_command)

    reviewed_journals_parser = subparsers.add_parser(
        "export-reviewed-journals",
        help="Export approved reviewed journals to JSON, CSV, and markdown.",
    )
    reviewed_journals_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    reviewed_journals_parser.add_argument("--output-dir", default="outputs/reviewed_journals")
    reviewed_journals_parser.set_defaults(func=_export_reviewed_journals_command)

    post_journal_tb_parser = subparsers.add_parser(
        "build-post-journal-tb",
        help="Build a post-journal trial balance from CoA opening balances and reviewed journals.",
    )
    post_journal_tb_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    post_journal_tb_parser.add_argument("--reviewed-journals", required=True)
    post_journal_tb_parser.add_argument("--output", default="outputs/post_journal_trial_balance.md")
    post_journal_tb_parser.set_defaults(func=_build_post_journal_tb_command)

    statement_mapping_parser = subparsers.add_parser(
        "preview-statement-line-mapping",
        help="Preview statement line mapping from a post-journal trial balance.",
    )
    statement_mapping_parser.add_argument("--post-journal-tb", required=True)
    statement_mapping_parser.add_argument("--output", default="outputs/statement_line_mapping.md")
    statement_mapping_parser.set_defaults(func=_preview_statement_line_mapping_command)

    draft_from_tb_parser = subparsers.add_parser(
        "render-draft-statements-from-tb",
        help="Render internal-review-only draft statements from post-journal TB and mapping preview.",
    )
    draft_from_tb_parser.add_argument("--post-journal-tb", required=True)
    draft_from_tb_parser.add_argument("--mapping", required=True)
    draft_from_tb_parser.add_argument("--output-dir", default="outputs/draft_statements")
    draft_from_tb_parser.set_defaults(func=_render_draft_statements_from_tb_command)

    statement_chain_parser = subparsers.add_parser(
        "inspect-statement-chain-readiness",
        help="Inspect readiness of reviewed-journal to draft-statement artifact chain.",
    )
    statement_chain_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    statement_chain_parser.add_argument("--artifact-dir", default="outputs")
    statement_chain_parser.add_argument("--json", action="store_true")
    statement_chain_parser.set_defaults(func=_inspect_statement_chain_readiness_command)

    draft_review_template_parser = subparsers.add_parser(
        "export-draft-statement-review-template",
        help="Export accountant review template for internal draft statements.",
    )
    draft_review_template_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    draft_review_template_parser.add_argument("--draft", required=True)
    draft_review_template_parser.add_argument("--output", required=True)
    draft_review_template_parser.set_defaults(func=_export_draft_statement_review_template_command)

    draft_review_apply_parser = subparsers.add_parser(
        "apply-draft-statement-review",
        help="Apply accountant approval/rejection for draft statements.",
    )
    draft_review_apply_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    draft_review_apply_parser.add_argument("--decision", required=True)
    draft_review_apply_parser.add_argument("--draft", required=True)
    draft_review_apply_parser.add_argument("--output", required=True)
    draft_review_apply_parser.set_defaults(func=_apply_draft_statement_review_command)

    release_candidate_parser = subparsers.add_parser(
        "build-release-candidate-package",
        help="Build release candidate package with hashed reviewed artifacts.",
    )
    release_candidate_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    release_candidate_parser.add_argument("--artifact-dir", default="outputs")
    release_candidate_parser.add_argument("--output-dir", default="outputs/release_candidate")
    release_candidate_parser.set_defaults(func=_build_release_candidate_package_command)

    verify_release_candidate_parser = subparsers.add_parser(
        "verify-release-candidate",
        help="Verify release candidate artifact hashes.",
    )
    verify_release_candidate_parser.add_argument("--manifest", required=True)
    verify_release_candidate_parser.set_defaults(func=_verify_release_candidate_command)

    final_release_parser = subparsers.add_parser(
        "export-final-release-manifest",
        help="Export final release manifest tied to a verified release candidate.",
    )
    final_release_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    final_release_parser.add_argument("--release-candidate", required=True)
    final_release_parser.add_argument("--output", required=True)
    final_release_parser.set_defaults(func=_export_final_release_manifest_command)

    accountant_workbench_parser = subparsers.add_parser(
        "export-accountant-review-workbench",
        help="Export unified accountant review workbench for CoA, journals, draft statements, and final sign-off.",
    )
    accountant_workbench_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    accountant_workbench_parser.add_argument("--artifact-dir", default="outputs")
    accountant_workbench_parser.add_argument("--output", required=True)
    accountant_workbench_parser.set_defaults(func=_export_accountant_review_workbench_command)

    apply_accountant_workbench_parser = subparsers.add_parser(
        "apply-accountant-review-workbench",
        help="Apply decisions from unified accountant review workbench.",
    )
    apply_accountant_workbench_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    apply_accountant_workbench_parser.add_argument("--workbench", required=True)
    apply_accountant_workbench_parser.add_argument("--artifact-dir", default="outputs")
    apply_accountant_workbench_parser.add_argument("--output", required=True)
    apply_accountant_workbench_parser.set_defaults(func=_apply_accountant_review_workbench_command)

    release_blockers_parser = subparsers.add_parser(
        "explain-release-blockers",
        help="Export plain-English release blockers grouped by control layer.",
    )
    release_blockers_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    release_blockers_parser.add_argument("--artifact-dir", default="outputs")
    release_blockers_parser.add_argument("--output", required=True)
    release_blockers_parser.set_defaults(func=_explain_release_blockers_command)

    review_ui_bundle_parser = subparsers.add_parser(
        "export-review-ui-bundle",
        help="Export read-only review UI data bundle for the accountant workbench.",
    )
    review_ui_bundle_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    review_ui_bundle_parser.add_argument("--artifact-dir", default="outputs")
    review_ui_bundle_parser.add_argument("--output-dir", required=True)
    review_ui_bundle_parser.set_defaults(func=_export_review_ui_bundle_command)

    accountant_review_ui_parser = subparsers.add_parser(
        "export-accountant-review-ui",
        help="Export a local static accountant review UI for filling workbench decisions.",
    )
    accountant_review_ui_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    accountant_review_ui_parser.add_argument("--artifact-dir", default="outputs")
    accountant_review_ui_parser.add_argument("--output-dir", required=True)
    accountant_review_ui_parser.set_defaults(func=_export_accountant_review_ui_command)

    bank_continuity_parser = subparsers.add_parser(
        "export-bank-continuity",
        help="Compare sequential bank statement closing and opening balances.",
    )
    bank_continuity_parser.add_argument("--facts", default="outputs/bank_statement_facts.json")
    bank_continuity_parser.add_argument("--output", default="outputs/bank_continuity.md")
    bank_continuity_parser.set_defaults(func=_export_bank_continuity_command)

    evidence_parser = subparsers.add_parser(
        "record-evidence",
        help="Record a structured source evidence reference in engagement state.",
    )
    evidence_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    evidence_parser.add_argument("--evidence-id", required=True)
    evidence_parser.add_argument("--source-type", required=True)
    evidence_parser.add_argument("--file-path", required=True)
    evidence_parser.add_argument("--page", default=None)
    evidence_parser.add_argument("--row", default=None)
    evidence_parser.add_argument("--quote", default=None)
    evidence_parser.add_argument("--amount", default=None)
    evidence_parser.add_argument("--date", default=None)
    evidence_parser.add_argument("--confidence", default=None)
    evidence_parser.add_argument("--document-id", default=None)
    evidence_parser.set_defaults(func=_record_evidence_command)

    document_parser = subparsers.add_parser(
        "record-document",
        help="Record a source document with a stable hash in the engagement manifest.",
    )
    document_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    document_parser.add_argument("--document-id", required=True)
    document_parser.add_argument("--file-path", required=True)
    document_parser.add_argument("--document-type", required=True)
    document_parser.add_argument("--entity", required=True)
    document_parser.add_argument("--period-start", required=True)
    document_parser.add_argument("--period-end", required=True)
    document_parser.add_argument("--notes", default=None)
    document_parser.set_defaults(func=_record_document_command)

    list_documents_parser = subparsers.add_parser(
        "list-documents",
        help="List source documents recorded in engagement state.",
    )
    list_documents_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    list_documents_parser.set_defaults(func=_list_documents_command)

    template_parser = subparsers.add_parser(
        "export-review-template",
        help="Export a JSON template for batch accountant exception review.",
    )
    template_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    template_parser.add_argument("--output", required=True)
    template_parser.set_defaults(func=_export_review_template_command)

    audit_parser = subparsers.add_parser(
        "export-audit-trail",
        help="Export engagement readiness, exceptions, evidence, and accountant decisions as markdown.",
    )
    audit_parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE_PATH),
        help=f"Path to engagement_state.json (default: {DEFAULT_STATE_PATH})",
    )
    audit_parser.add_argument(
        "--output",
        default=None,
        help="Where to write audit_trail.md. If omitted, markdown is printed to stdout.",
    )
    audit_parser.set_defaults(func=_export_audit_trail_command)

    review_parser = subparsers.add_parser(
        "review-exceptions",
        help="List and record accountant decisions for engagement exceptions.",
    )
    review_parser.add_argument(
        "--state",
        default=str(DEFAULT_STATE_PATH),
        help=f"Path to engagement_state.json (default: {DEFAULT_STATE_PATH})",
    )
    review_parser.add_argument("--exception-id", default=None, help="Exception to update.")
    review_parser.add_argument(
        "--action",
        choices=[
            ExceptionStatus.RESOLVED.value,
            ExceptionStatus.ACCEPTED_RISK.value,
            ExceptionStatus.REJECTED.value,
        ],
        default=None,
        help="Review outcome to record for --exception-id.",
    )
    review_parser.add_argument("--rationale", default=None, help="Accountant rationale for the decision.")
    review_parser.add_argument("--approved-by", default=None, help="Reviewer name for the approved decision.")
    review_parser.add_argument("--decisions", default=None, help="JSON batch file of exception review decisions.")
    review_parser.set_defaults(func=_review_exceptions_command)

    coa_review_parser = subparsers.add_parser(
        "review-coa",
        help="Show chart of accounts approval status and required review decision.",
    )
    coa_review_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    coa_review_parser.set_defaults(func=_review_coa_command)

    coa_record_parser = subparsers.add_parser(
        "record-coa-account",
        help="Record a structured chart-of-accounts account for review.",
    )
    coa_record_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    coa_record_parser.add_argument("--account-id", required=True)
    coa_record_parser.add_argument("--code", required=True)
    coa_record_parser.add_argument("--name", required=True)
    coa_record_parser.add_argument("--type", required=True)
    coa_record_parser.add_argument("--presentation-group", required=True)
    coa_record_parser.add_argument("--opening-balance", required=True)
    coa_record_parser.add_argument("--evidence-ref", action="append", default=[])
    coa_record_parser.set_defaults(func=_record_coa_account_command)

    coa_approve_parser = subparsers.add_parser(
        "approve-coa",
        help="Record accountant approval for chart of accounts.",
    )
    coa_approve_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    coa_approve_parser.add_argument("--account-id", default=None)
    coa_approve_parser.add_argument("--approved-by", required=True)
    coa_approve_parser.add_argument("--rationale", required=True)
    coa_approve_parser.set_defaults(func=_approve_coa_command)

    adjustment_review_parser = subparsers.add_parser(
        "review-adjustments",
        help="List adjustment and journal review items.",
    )
    adjustment_review_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    adjustment_review_parser.set_defaults(func=_review_adjustments_command)

    record_adjustment_parser = subparsers.add_parser(
        "record-adjustment",
        help="Record a structured adjustment proposal for accountant review.",
    )
    record_adjustment_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    record_adjustment_parser.add_argument("--adjustment-id", required=True)
    record_adjustment_parser.add_argument("--description", required=True)
    record_adjustment_parser.add_argument("--debit-account", required=True)
    record_adjustment_parser.add_argument("--credit-account", required=True)
    record_adjustment_parser.add_argument("--amount", required=True)
    record_adjustment_parser.add_argument("--date", required=True)
    record_adjustment_parser.add_argument("--evidence-ref", action="append", default=[])
    record_adjustment_parser.set_defaults(func=_record_adjustment_command)

    approve_adjustment_parser = subparsers.add_parser(
        "approve-adjustment",
        help="Approve a proposed adjustment or journal review item.",
    )
    approve_adjustment_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    approve_adjustment_parser.add_argument("--exception-id", default=None)
    approve_adjustment_parser.add_argument("--adjustment-id", default=None)
    approve_adjustment_parser.add_argument("--approved-by", required=True)
    approve_adjustment_parser.add_argument("--rationale", required=True)
    approve_adjustment_parser.set_defaults(func=_approve_adjustment_command)

    reject_adjustment_parser = subparsers.add_parser(
        "reject-adjustment",
        help="Reject a proposed adjustment or journal review item.",
    )
    reject_adjustment_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    reject_adjustment_parser.add_argument("--exception-id", default=None)
    reject_adjustment_parser.add_argument("--adjustment-id", default=None)
    reject_adjustment_parser.add_argument("--approved-by", required=True)
    reject_adjustment_parser.add_argument("--rationale", required=True)
    reject_adjustment_parser.set_defaults(func=_reject_adjustment_command)

    packet_parser = subparsers.add_parser(
        "export-review-packet",
        help="Export an accountant-facing review packet folder.",
    )
    packet_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    packet_parser.add_argument("--output-dir", required=True)
    packet_parser.set_defaults(func=_export_review_packet_command)

    signoff_parser = subparsers.add_parser(
        "sign-off-engagement",
        help="Record final accountant sign-off when readiness allows release.",
    )
    signoff_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    signoff_parser.add_argument("--approved-by", required=True)
    signoff_parser.add_argument("--rationale", required=True)
    signoff_parser.set_defaults(func=_sign_off_engagement_command)

    pack_parser = subparsers.add_parser(
        "export-workpaper-pack",
        help="Export a review-ready markdown/JSON workpaper pack folder.",
    )
    pack_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    pack_parser.add_argument("--output-dir", required=True)
    pack_parser.set_defaults(func=_export_workpaper_pack_command)

    record_pref_parser = subparsers.add_parser(
        "record-preference",
        help="Record an engagement/client/accountant/firm preference rule.",
    )
    record_pref_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    record_pref_parser.add_argument("--scope", required=True, choices=[scope.value for scope in PreferenceScope])
    record_pref_parser.add_argument("--subject", required=True)
    record_pref_parser.add_argument("--rule", required=True)
    record_pref_parser.add_argument("--approved-by", default=None)
    record_pref_parser.add_argument("--evidence-ref", action="append", default=[])
    record_pref_parser.set_defaults(func=_record_preference_command)

    list_pref_parser = subparsers.add_parser(
        "list-preferences",
        help="List preference rules recorded in engagement state.",
    )
    list_pref_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    list_pref_parser.set_defaults(func=_list_preferences_command)

    recommend_pref_parser = subparsers.add_parser(
        "recommend-preferences",
        help="Recommend approved preference rules that match this engagement.",
    )
    recommend_pref_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    recommend_pref_parser.set_defaults(func=_recommend_preferences_command)

    apply_pref_parser = subparsers.add_parser(
        "apply-preferences",
        help="Record an approved decision applying a preference to this engagement.",
    )
    apply_pref_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    apply_pref_parser.add_argument("--preference-id", required=True)
    apply_pref_parser.add_argument("--approved-by", required=True)
    apply_pref_parser.add_argument("--rationale", required=True)
    apply_pref_parser.set_defaults(func=_apply_preferences_command)

    output_parser = subparsers.add_parser(
        "record-output",
        help="Record a generated output artifact and verifier status.",
    )
    output_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    output_parser.add_argument("--output-id", required=True)
    output_parser.add_argument("--file-path", required=True)
    output_parser.add_argument("--artifact-type", required=True)
    output_parser.add_argument("--verifier-status", required=True, choices=["passed", "failed", "not_run"])
    output_parser.set_defaults(func=_record_output_command)

    verifier_parser = subparsers.add_parser(
        "import-verifier-result",
        help="Import verifier JSON into output artifacts and blocking exceptions.",
    )
    verifier_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    verifier_parser.add_argument("--verifier-result", required=True)
    verifier_parser.set_defaults(func=_import_verifier_result_command)

    template_recommend_parser = subparsers.add_parser(
        "recommend-templates",
        help="Recommend entity-type accounting template rules.",
    )
    template_recommend_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    template_recommend_parser.set_defaults(func=_recommend_templates_command)

    review_ui_parser = subparsers.add_parser(
        "export-review-ui",
        help="Export a static HTML accountant review page.",
    )
    review_ui_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    review_ui_parser.add_argument("--output", required=True)
    review_ui_parser.set_defaults(func=_export_review_ui_command)

    manifest_parser = subparsers.add_parser(
        "export-release-manifest",
        help="Export a final release manifest after accountant sign-off.",
    )
    manifest_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    manifest_parser.add_argument("--output", required=True)
    manifest_parser.add_argument("--workpaper-pack", default=None)
    manifest_parser.add_argument("--audit-trail", default=None)
    manifest_parser.set_defaults(func=_export_release_manifest_command)

    import_parser = subparsers.add_parser(
        "import-source-exceptions",
        help="Import source pipeline control issues into a new engagement state exception queue.",
    )
    import_parser.add_argument("--matching", required=True, help="Path to matching control output JSON")
    import_parser.add_argument("--journal", required=True, help="Path to journal/control output JSON")
    import_parser.add_argument("--output", required=True, help="Where to write engagement_state.json")
    import_parser.add_argument("--engagement-id", required=True)
    import_parser.add_argument("--entity-name", required=True)
    import_parser.add_argument("--fy-start", required=True)
    import_parser.add_argument("--fy-end", required=True)
    import_parser.add_argument("--entity-type", default=None)
    import_parser.add_argument("--documents-ref", default=None)
    import_parser.add_argument("--coa-ref", default=None)
    import_parser.add_argument("--bank-txns-ref", default=None)
    import_parser.add_argument("--events-ref", default=None)
    import_parser.set_defaults(func=_import_source_exceptions_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
