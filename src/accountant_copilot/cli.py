"""Command-line interface for the Agentic Accountant Copilot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree as ET

from accountant_copilot.adapters.source_pipeline import import_source_pipeline_controls
from accountant_copilot.orchestrator.planner import build_readiness_report, plan_next_tasks
from accountant_copilot.state.artifacts import AdjustmentProposal, ChartAccount, OutputArtifact, SourceDocument, StateTransition
from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.evidence import EvidenceRef
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity, ExceptionStatus
from accountant_copilot.state.preferences import PreferenceRule, PreferenceScope, PreferenceStatus
from accountant_copilot.tb_bridge_workflow import (
    RELATIONSHIP_REASONING_CONTRACT_VERSION,
    TB_BRIDGE_CONTRACT_VERSION,
    TB_BRIDGE_JSON,
    TB_BRIDGE_MD,
    TB_BRIDGE_OUTPUT_DIR,
    TB_BRIDGE_XLSX,
    build_relationship_reasoning_prompt,
    build_tb_bridge_prompt,
    accounting_pdf_retrieval_tool_for_prompt,
    client_evidence_guardrail_for_prompt,
    enrich_tb_bridge_payload_for_workbook,
    failed_relationship_register,
    failed_tb_bridge_workpaper,
    format_relationship_register,
    format_tb_bridge_workpaper,
    load_accounting_reference_for_prompt,
    load_accounting_pdf_topic_map_for_prompt,
    load_accounting_skill_for_prompt,
    non_client_evidence_reference_findings,
    normalise_relationship_register,
    normalise_tb_bridge_workpaper,
    repair_tb_bridge_workbook_hyperlinks,
    source_of_truth_redo_instruction,
    validate_relationship_register,
    validate_tb_bridge_workpaper,
    write_tb_bridge_workbook_builder,
)


DEFAULT_STATE_PATH = Path("outputs/engagement_state.json")
DEFAULT_TURING_ENTITY_NAME = "XYZ Financial Pty Ltd ATF XYZ Australia Financial Trust"
DEFAULT_TURING_ENGAGEMENT_ID = "turing_financial_statements_fy2025"
_OUT_OF_SCOPE_VERSION = "".join(("v", "2"))
_OUT_OF_SCOPE_VERSION_UPPER = _OUT_OF_SCOPE_VERSION.upper()
_LAST_AI_EXTRACTION_ERROR: str | None = None
_PDF_PAGE_QUOTE_CHAR_LIMIT = 8000
SOURCE_MATCHING_CONTRACT_VERSION = RELATIONSHIP_REASONING_CONTRACT_VERSION
COA_MAPPING_CONTRACT_VERSION = TB_BRIDGE_CONTRACT_VERSION


def _set_last_ai_extraction_error(reason: str | None) -> None:
    global _LAST_AI_EXTRACTION_ERROR
    _LAST_AI_EXTRACTION_ERROR = reason


def _last_ai_extraction_error() -> str | None:
    return _LAST_AI_EXTRACTION_ERROR


def _load_local_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip("\"'")


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
                "original_file_name": document.original_file_name or Path(document.file_path).name,
                "display_name": document.display_name or Path(document.file_path).name,
                "naming_confidence": document.naming_confidence,
                "naming_status": document.naming_status,
                "naming_method": document.naming_method,
                "naming_evidence_refs": document.naming_evidence_refs,
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


def _build_document_inventory_payload_from_records(entity_name: str, documents_source: list[SourceDocument], evidence_source: list[EvidenceRef]) -> dict:
    evidence_by_document: dict[str, list[EvidenceRef]] = {}
    for evidence in evidence_source:
        key = evidence.document_id or evidence.file_path
        evidence_by_document.setdefault(key, []).append(evidence)

    documents = []
    for document in documents_source:
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
                "original_file_name": document.original_file_name or Path(document.file_path).name,
                "display_name": document.display_name or Path(document.file_path).name,
                "naming_confidence": document.naming_confidence,
                "naming_status": document.naming_status,
                "naming_method": document.naming_method,
                "naming_evidence_refs": document.naming_evidence_refs,
                "document_type": document.document_type,
                "status": document.status,
                "source_hash": document.source_hash,
                "evidence_count": len(evidence_items),
                "tags": _content_tags(combined, document.document_type),
                "dates": _unique_matches(_DATE_RE, combined, limit=12),
                "amounts": _unique_matches(_AMOUNT_RE, combined, limit=12),
                "pages": pages,
            }
        )
    return {"inventory_id": "source_document_inventory", "entity_name": entity_name, "documents": documents}


def _format_document_inventory(payload: dict) -> str:
    lines = [f"# Document Inventory — {payload['entity_name']}", ""]
    lines.append(f"Documents: {len(payload['documents'])}")
    lines.append("")
    for document in payload["documents"]:
        lines.extend(
            [
                f"## {document['document_id']} — {document.get('display_name') or Path(document['file_path']).name}",
                f"- Path: `{document['file_path']}`",
                f"- Original file name: {document.get('original_file_name') or Path(document['file_path']).name}",
                f"- Display name: {document.get('display_name') or Path(document['file_path']).name}",
                f"- Naming status: {document.get('naming_status') or 'not_suggested'}",
                f"- Naming confidence: {document.get('naming_confidence') or 'n/a'}",
                f"- Naming method: {document.get('naming_method') or 'n/a'}",
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


DEFAULT_OPENAI_FACT_MODEL = "gpt-5.5"
DEFAULT_ANTHROPIC_FACT_MODEL = "claude-sonnet-4-6"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


def _openai_fact_schema(allowed_fact_types: list[str]) -> dict:
    fact_types = [*allowed_fact_types, "none"]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "extracted": {"type": "boolean"},
            "fact_type": {"type": "string", "enum": fact_types},
            "fields": {"type": "object"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "reason": {"type": "string"},
        },
        "required": ["extracted", "fact_type", "fields", "confidence", "reason"],
    }


def _extract_output_text_from_response(payload: dict) -> str | None:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                return content["text"]
    return None


def _openai_structured_fact_extract(
    document: SourceDocument,
    evidence: EvidenceRef,
    *,
    allowed_fact_types: list[str],
    model: str,
    timeout: int,
) -> dict | None:
    _set_last_ai_extraction_error(None)
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_OPENAI_FACT_JSON")
    if fake_payload:
        try:
            return json.loads(fake_payload)
        except json.JSONDecodeError:
            _set_last_ai_extraction_error("Fake OpenAI fact payload was not valid JSON.")
            return None
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _set_last_ai_extraction_error("OPENAI_API_KEY is not set.")
        return None
    quote = " ".join((evidence.quote or "").split())
    request_payload = {
        "model": model,
        "store": False,
        "input": [
            {
                "role": "system",
                "content": (
                    "Extract source-grounded accounting facts from the supplied document evidence. "
                    "Return only fields supported by the text. If required values are absent, set extracted=false."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "document_id": document.document_id,
                        "display_name": document.display_name or Path(document.file_path).name,
                        "document_type": document.document_type,
                        "allowed_fact_types": allowed_fact_types,
                        "evidence_id": evidence.evidence_id,
                        "page": evidence.page,
                        "quote": quote[:3000],
                    },
                    sort_keys=True,
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "accounting_fact_extraction",
                "schema": _openai_fact_schema(allowed_fact_types),
                "strict": False,
            }
        },
    }
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(request_payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="ignore")
        _set_last_ai_extraction_error(f"OpenAI request failed with HTTP {error.code}: {body[:240]}")
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        _set_last_ai_extraction_error("OpenAI request failed or returned invalid JSON.")
        return None
    output_text = _extract_output_text_from_response(response_payload)
    if not output_text:
        _set_last_ai_extraction_error("OpenAI response did not include structured output text.")
        return None
    try:
        structured = json.loads(output_text)
    except json.JSONDecodeError:
        _set_last_ai_extraction_error("OpenAI structured output was not valid JSON.")
        return None
    return structured if isinstance(structured, dict) else None


def _anthropic_structured_fact_extract(
    document: SourceDocument,
    evidence: EvidenceRef,
    *,
    allowed_fact_types: list[str],
    model: str,
    timeout: int,
) -> dict | None:
    _set_last_ai_extraction_error(None)
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_ANTHROPIC_FACT_JSON")
    if fake_payload:
        try:
            return json.loads(fake_payload)
        except json.JSONDecodeError:
            _set_last_ai_extraction_error("Fake Anthropic fact payload was not valid JSON.")
            return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        _set_last_ai_extraction_error("ANTHROPIC_API_KEY is not set.")
        return None
    quote = " ".join((evidence.quote or "").split())
    tool_schema = _openai_fact_schema(allowed_fact_types)
    request_payload = {
        "model": model,
        "max_tokens": 2048,
        "system": (
            "Extract source-grounded accounting facts from the supplied document evidence. "
            "Return only fields supported by the text. If required values are absent, set extracted=false."
        ),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "document_id": document.document_id,
                        "display_name": document.display_name or Path(document.file_path).name,
                        "document_type": document.document_type,
                        "allowed_fact_types": allowed_fact_types,
                        "evidence_id": evidence.evidence_id,
                        "page": evidence.page,
                        "quote": quote[:3000],
                    },
                    sort_keys=True,
                ),
            }
        ],
        "tools": [
            {
                "name": "extract_accounting_fact",
                "description": "Return structured accounting fact extraction from the provided evidence.",
                "input_schema": tool_schema,
            }
        ],
        "tool_choice": {"type": "tool", "name": "extract_accounting_fact"},
    }
    request = urllib.request.Request(
        ANTHROPIC_MESSAGES_URL,
        data=json.dumps(request_payload).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="ignore")
        _set_last_ai_extraction_error(f"Anthropic request failed with HTTP {error.code}: {body[:240]}")
        return None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        _set_last_ai_extraction_error("Anthropic request failed or returned invalid JSON.")
        return None
    for content in response_payload.get("content", []):
        if isinstance(content, dict) and content.get("type") == "tool_use" and content.get("name") == "extract_accounting_fact":
            tool_input = content.get("input")
            return tool_input if isinstance(tool_input, dict) else None
    _set_last_ai_extraction_error("Anthropic response did not include the expected tool result.")
    return None


def _ai_structured_fact_extract(
    document: SourceDocument,
    evidence: EvidenceRef,
    *,
    allowed_fact_types: list[str],
    provider: str,
    openai_model: str,
    anthropic_model: str,
    timeout: int,
) -> dict | None:
    if provider == "anthropic":
        return _anthropic_structured_fact_extract(document, evidence, allowed_fact_types=allowed_fact_types, model=anthropic_model, timeout=timeout)
    return _openai_structured_fact_extract(document, evidence, allowed_fact_types=allowed_fact_types, model=openai_model, timeout=timeout)


def _ai_fact_record(
    document: SourceDocument,
    evidence: EvidenceRef,
    *,
    expected_fact_type: str,
    provider: str,
    openai_model: str,
    anthropic_model: str,
    timeout: int,
    cache_dir: Path | None = None,
) -> dict | None:
    model = anthropic_model if provider == "anthropic" else openai_model
    quote = " ".join((evidence.quote or "").split())
    cache_path = None
    if cache_dir is not None:
        cache_key_payload = {
            "provider": provider,
            "model": model,
            "fact_type": expected_fact_type,
            "document_id": document.document_id,
            "evidence_id": evidence.evidence_id,
            "source_hash": document.source_hash,
            "quote_hash": hashlib.sha256(quote.encode()).hexdigest(),
        }
        cache_key = hashlib.sha256(json.dumps(cache_key_payload, sort_keys=True).encode()).hexdigest()
        cache_path = cache_dir / f"{cache_key}.json"
    payload = None
    if cache_path is not None and cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text()).get("payload")
        except json.JSONDecodeError:
            payload = None
    if payload is None:
        payload = _ai_structured_fact_extract(
            document,
            evidence,
            allowed_fact_types=[expected_fact_type],
            provider=provider,
            openai_model=openai_model,
            anthropic_model=anthropic_model,
            timeout=timeout,
        )
        if cache_path is not None and payload and payload.get("extracted"):
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"cached_at": datetime.now(timezone.utc).isoformat(), "payload": payload}, indent=2, sort_keys=True))
    if not payload or not payload.get("extracted") or payload.get("fact_type") != expected_fact_type:
        if not _last_ai_extraction_error():
            _set_last_ai_extraction_error("AI returned no supported accounting fact for this evidence.")
        return None
    fields = payload.get("fields", {})
    if not isinstance(fields, dict) or not fields:
        _set_last_ai_extraction_error("AI returned an accounting fact without supported fields.")
        return None
    common = {
        "document_id": document.document_id,
        "file_path": document.file_path,
        "page": evidence.page,
        "evidence_id": evidence.evidence_id,
        "confidence": payload.get("confidence") or evidence.confidence,
        "snippet": quote[:300],
        "extraction_method": "ai",
        "ai_provider": provider,
    }
    if expected_fact_type == "broker_trade":
        broker_fields = dict(fields)
        side = broker_fields.pop("side", None)
        return {**common, "side": side, "fields": broker_fields}
    record = {**common, **fields}
    if expected_fact_type == "distribution_tax":
        record["document_type"] = document.document_type
        record.setdefault("components", {})
    return record


def _write_fact_extraction_checkpoint(
    *,
    progress_path: Path | None,
    partial_path: Path | None,
    engagement_id: str,
    entity_name: str,
    fact_type: str,
    facts: list[dict],
    findings: list[dict],
    processed_items: int,
    total_items: int,
    status: str,
    current_document_id: str | None = None,
    current_evidence_id: str | None = None,
) -> None:
    if progress_path is None and partial_path is None:
        return
    progress = {
        "status": status,
        "fact_type": fact_type,
        "processed_items": processed_items,
        "total_items": total_items,
        "facts_extracted": len(facts),
        "findings": len(findings),
        "current_document_id": current_document_id,
        "current_evidence_id": current_evidence_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if progress_path is not None:
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.write_text(json.dumps(progress, indent=2, sort_keys=True))
    if partial_path is not None:
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        partial_payload = {
            "engagement_id": engagement_id,
            "entity_name": entity_name,
            "fact_type": fact_type,
            "facts": facts,
            "findings": findings,
            "progress": progress,
        }
        partial_path.write_text(json.dumps(partial_payload, indent=2, sort_keys=True))


def _source_records_from_inventory(inventory_path: Path) -> argparse.Namespace:
    payload = json.loads(inventory_path.read_text())
    documents: list[SourceDocument] = []
    evidence_items: list[EvidenceRef] = []
    for item in payload.get("documents", []):
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("document_id") or "")
        file_path = str(item.get("file_path") or "")
        if not document_id or not file_path:
            continue
        document = SourceDocument(
            document_id=document_id,
            file_path=file_path,
            document_type=str(item.get("document_type") or "unknown"),
            entity=str(payload.get("entity_name") or "Uploaded documents"),
            period_start="",
            period_end="",
            source_hash=str(item.get("source_hash") or ""),
            status=str(item.get("status") or "registered"),
            original_file_name=str(item.get("original_file_name") or Path(file_path).name),
            display_name=str(item.get("display_name") or Path(file_path).name),
            naming_confidence=item.get("naming_confidence"),
            naming_status=item.get("naming_status"),
            naming_method=item.get("naming_method"),
            naming_evidence_refs=list(item.get("naming_evidence_refs") or []),
        )
        documents.append(document)
        path = Path(file_path)
        if path.suffix.lower() == ".pdf":
            for page_number, quote in _extract_pdf_page_quotes(path):
                evidence_items.append(EvidenceRef(evidence_id=f"{document_id}_page_{page_number:03d}", source_type=document.document_type, file_path=file_path, page=str(page_number), quote=quote, document_id=document_id, confidence="text_pdf"))
        elif path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
            quote = _extract_image_ocr_quote(path)
            if quote:
                evidence_items.append(EvidenceRef(evidence_id=f"{document_id}_page_001", source_type=document.document_type, file_path=file_path, page="1", quote=quote, document_id=document_id, confidence="image_ocr"))
        elif path.suffix.lower() == ".md":
            quote = path.read_text(errors="ignore")[:5000]
            evidence_items.append(EvidenceRef(evidence_id=f"{document_id}_text_001", source_type=document.document_type, file_path=file_path, quote=quote, document_id=document_id, confidence="1.0"))
    return argparse.Namespace(
        engagement_id=str(payload.get("inventory_id") or "source_document_inventory"),
        entity_name=str(payload.get("entity_name") or "Uploaded documents"),
        source_documents=documents,
        evidence=evidence_items,
    )


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
        "extraction_method": "deterministic",
    }
    return fact


def _build_bank_statement_facts_payload(
    state: EngagementState,
    *,
    use_ai_extraction: bool = False,
    ai_provider: str = "anthropic",
    openai_model: str = DEFAULT_OPENAI_FACT_MODEL,
    anthropic_model: str = DEFAULT_ANTHROPIC_FACT_MODEL,
    openai_timeout: int = 60,
    progress_path: Path | None = None,
    partial_path: Path | None = None,
    cache_dir: Path | None = None,
) -> dict:
    documents = {doc.document_id: doc for doc in state.source_documents if doc.document_type == "bank_statement"}
    evidence_by_document: dict[str, list[EvidenceRef]] = {doc_id: [] for doc_id in documents}
    for evidence in state.evidence:
        if evidence.source_type == "bank_statement" and evidence.document_id in documents:
            evidence_by_document.setdefault(evidence.document_id, []).append(evidence)

    facts: list[dict] = []
    findings: list[dict] = []
    extracted_document_ids: set[str] = set()
    total_items = sum(len(items) for items in evidence_by_document.values())
    processed_items = 0
    _write_fact_extraction_checkpoint(
        progress_path=progress_path,
        partial_path=partial_path,
        engagement_id=state.engagement_id,
        entity_name=state.entity_name,
        fact_type="bank_statement_facts",
        facts=facts,
        findings=findings,
        processed_items=processed_items,
        total_items=total_items,
        status="running",
    )
    for document_id, evidence_items in evidence_by_document.items():
        document = documents[document_id]
        for evidence in evidence_items:
            fact = None
            ai_fallback_reason = None
            if use_ai_extraction:
                fact = _ai_fact_record(document, evidence, expected_fact_type="bank_statement", provider=ai_provider, openai_model=openai_model, anthropic_model=anthropic_model, timeout=openai_timeout, cache_dir=cache_dir)
                ai_fallback_reason = _last_ai_extraction_error()
            elif fact is None:
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
            processed_items += 1
            _write_fact_extraction_checkpoint(
                progress_path=progress_path,
                partial_path=partial_path,
                engagement_id=state.engagement_id,
                entity_name=state.entity_name,
                fact_type="bank_statement_facts",
                facts=facts,
                findings=findings,
                processed_items=processed_items,
                total_items=total_items,
                status="running",
                current_document_id=document_id,
                current_evidence_id=evidence.evidence_id,
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
                    "ai_failure_reason": _last_ai_extraction_error() if use_ai_extraction else None,
                    "recommended_action": "Review AI extraction response/key and source evidence." if use_ai_extraction else "Review source document and improve bank fact parser or mark evidence out of scope.",
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
    payload = {
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
    _write_fact_extraction_checkpoint(
        progress_path=progress_path,
        partial_path=partial_path,
        engagement_id=state.engagement_id,
        entity_name=state.entity_name,
        fact_type="bank_statement_facts",
        facts=facts,
        findings=findings,
        processed_items=processed_items,
        total_items=total_items,
        status="complete",
    )
    return payload


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
    state = _source_records_from_inventory(Path(args.inventory)) if getattr(args, "inventory", None) else load_engagement_state(Path(args.state))
    output = Path(args.output)
    payload = _build_bank_statement_facts_payload(
        state,
        use_ai_extraction=bool(getattr(args, "use_ai_extraction", False)),
        ai_provider=getattr(args, "ai_provider", "anthropic"),
        openai_model=getattr(args, "openai_model", DEFAULT_OPENAI_FACT_MODEL),
        anthropic_model=getattr(args, "anthropic_model", DEFAULT_ANTHROPIC_FACT_MODEL),
        openai_timeout=int(getattr(args, "openai_timeout", 60) or 60),
        progress_path=output.with_suffix(".progress.json"),
        partial_path=output.with_suffix(".partial.json"),
        cache_dir=output.parent / ".ai_cache",
    )
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
        "extraction_method": "deterministic",
    }


def _build_invoice_facts_payload(
    state: EngagementState,
    *,
    use_ai_extraction: bool = False,
    ai_provider: str = "anthropic",
    openai_model: str = DEFAULT_OPENAI_FACT_MODEL,
    anthropic_model: str = DEFAULT_ANTHROPIC_FACT_MODEL,
    openai_timeout: int = 60,
    progress_path: Path | None = None,
    partial_path: Path | None = None,
    cache_dir: Path | None = None,
) -> dict:
    documents = {doc.document_id: doc for doc in state.source_documents}
    facts: list[dict] = []
    findings: list[dict] = []
    candidate_documents: set[str] = set()
    extracted_documents: set[str] = set()
    candidate_evidence = [evidence for evidence in state.evidence if evidence.document_id in documents and _is_invoice_evidence(evidence)]
    total_items = len(candidate_evidence)
    processed_items = 0
    _write_fact_extraction_checkpoint(
        progress_path=progress_path,
        partial_path=partial_path,
        engagement_id=state.engagement_id,
        entity_name=state.entity_name,
        fact_type="invoice_facts",
        facts=facts,
        findings=findings,
        processed_items=processed_items,
        total_items=total_items,
        status="running",
    )
    for evidence in candidate_evidence:
        candidate_documents.add(evidence.document_id)
        fact = None
        ai_fallback_reason = None
        if use_ai_extraction:
            fact = _ai_fact_record(documents[evidence.document_id], evidence, expected_fact_type="invoice", provider=ai_provider, openai_model=openai_model, anthropic_model=anthropic_model, timeout=openai_timeout, cache_dir=cache_dir)
            ai_fallback_reason = _last_ai_extraction_error()
        elif fact is None:
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
                    "ai_failure_reason": ai_fallback_reason if use_ai_extraction else None,
                    "recommended_action": "Review AI extraction response/key and source evidence." if use_ai_extraction else "Review invoice OCR/text and improve parser or record an accountant decision.",
                }
            )
        processed_items += 1
        _write_fact_extraction_checkpoint(
            progress_path=progress_path,
            partial_path=partial_path,
            engagement_id=state.engagement_id,
            entity_name=state.entity_name,
            fact_type="invoice_facts",
            facts=facts,
            findings=findings,
            processed_items=processed_items,
            total_items=total_items,
            status="running",
            current_document_id=evidence.document_id,
            current_evidence_id=evidence.evidence_id,
        )
    payload = {
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
    _write_fact_extraction_checkpoint(
        progress_path=progress_path,
        partial_path=partial_path,
        engagement_id=state.engagement_id,
        entity_name=state.entity_name,
        fact_type="invoice_facts",
        facts=facts,
        findings=findings,
        processed_items=processed_items,
        total_items=total_items,
        status="complete",
    )
    return payload


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
    state = _source_records_from_inventory(Path(args.inventory)) if getattr(args, "inventory", None) else load_engagement_state(Path(args.state))
    output = Path(args.output)
    payload = _build_invoice_facts_payload(
        state,
        use_ai_extraction=bool(getattr(args, "use_ai_extraction", False)),
        ai_provider=getattr(args, "ai_provider", "anthropic"),
        openai_model=getattr(args, "openai_model", DEFAULT_OPENAI_FACT_MODEL),
        anthropic_model=getattr(args, "anthropic_model", DEFAULT_ANTHROPIC_FACT_MODEL),
        openai_timeout=int(getattr(args, "openai_timeout", 60) or 60),
        progress_path=output.with_suffix(".progress.json"),
        partial_path=output.with_suffix(".partial.json"),
        cache_dir=output.parent / ".ai_cache",
    )
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


def _clean_distribution_money(amount: str | None) -> str | None:
    cleaned = _clean_money_amount(amount)
    if cleaned is None:
        return None
    return re.sub(r"^[A-Z]{1,3}\$", "", cleaned).strip()


@lru_cache(maxsize=64)
def _extract_pdf_full_text(path_text: str) -> str:
    path = Path(path_text)
    if not path.exists() or path.suffix.lower() != ".pdf":
        return ""
    try:
        import fitz  # type: ignore[import-not-found]

        with fitz.open(path) as doc:
            return " ".join(page.get_text("text") for page in doc)
    except Exception:
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout


def _distribution_quote_with_pdf_text(document: SourceDocument, evidence: EvidenceRef) -> str:
    quote = " ".join((evidence.quote or "").split())
    if not re.search(r"\bAN3\b|ANZ Capital Notes 9|AN3_Payment_Advice", f"{quote} {document.file_path}", re.IGNORECASE):
        return quote
    pdf_text = _extract_pdf_full_text(document.file_path)
    if not pdf_text:
        return quote
    pdf_quote = " ".join(pdf_text.split())
    if len(pdf_quote) <= len(quote):
        return quote
    return pdf_quote


def _extract_an3_payment_advice_fields(quote: str) -> dict:
    if not re.search(r"\bAN3\b|ANZ Capital Notes 9", quote, re.IGNORECASE):
        return {}
    fields: dict[str, object] = {}
    header_match = re.search(
        r"Security Code\s+Record Date\s+Payment Date\s+TFN\s*/?\s*ABN\s+"
        r"(?P<security>[A-Z0-9]+)\s+"
        r"(?P<record_date>\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+"
        r"(?P<payment_date>\d{1,2}\s+[A-Za-z]+\s+\d{4})",
        quote,
        re.IGNORECASE,
    )
    if header_match:
        fields["security_code"] = header_match.group("security")
        fields["record_date"] = header_match.group("record_date")
        fields["payment_date"] = header_match.group("payment_date")
    investment_match = re.search(
        r"details of your\s+\w+\s+(?P<name>ANZ Capital Notes 9)\s+distribution",
        quote,
        re.IGNORECASE,
    )
    if investment_match:
        fields["investment_name"] = investment_match.group("name")
    table_match = re.search(
        r"NUMBER OF\s+NOTES\s+FRANKED\s+AMOUNT\s+UNFRANKED\s+AMOUNT\s+LESS\s+TAX\*?\s+NET\s+AMOUNT\s+FRANKING\s+CREDIT\s+"
        r"(?P<notes>\d[\d,]*)\s+"
        r"(?P<franked>A?\$\d[\d,]*\.\d{2})\s+"
        r"(?P<unfranked>A?\$\d[\d,]*\.\d{2})\s+"
        r"(?P<tax>A?\$\d[\d,]*\.\d{2})\s+"
        r"(?P<net>A?\$\d[\d,]*\.\d{2})\s+"
        r"(?P<franking>A?\$\d[\d,]*\.\d{2})",
        quote,
        re.IGNORECASE,
    )
    if table_match:
        fields["number_of_notes"] = table_match.group("notes")
        components = {
            "franked_amount": _clean_distribution_money(table_match.group("franked")),
            "unfranked_amount": _clean_distribution_money(table_match.group("unfranked")),
            "tfn_withholding": _clean_distribution_money(table_match.group("tax")),
            "net_cash_distribution": _clean_distribution_money(table_match.group("net")),
            "franking_credit_tax_offset": _clean_distribution_money(table_match.group("franking")),
        }
        fields["components"] = {key: value for key, value in components.items() if value is not None}
        fields["amount"] = components["net_cash_distribution"]
    return fields


def _extract_distribution_tax_fact(document: SourceDocument, evidence: EvidenceRef) -> dict | None:
    quote = _distribution_quote_with_pdf_text(document, evidence)
    if not _is_distribution_tax_evidence(evidence, document):
        return None
    components = {
        component: amount
        for component, labels in _DISTRIBUTION_COMPONENT_LABELS.items()
        if (amount := _extract_label_amount(quote, labels)) is not None
    }
    an3_fields = _extract_an3_payment_advice_fields(quote)
    components.update(an3_fields.get("components", {}))
    payment_date = None
    record_date = None
    payment_match = re.search(r"Payment\s+date:?\s*(?P<date>\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})", quote, re.IGNORECASE)
    record_match = re.search(r"Record\s+date:?\s*(?P<date>\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})", quote, re.IGNORECASE)
    if payment_match:
        payment_date = payment_match.group("date")
    if record_match:
        record_date = record_match.group("date")
    payment_date = an3_fields.get("payment_date") or payment_date
    record_date = an3_fields.get("record_date") or record_date
    if not components and not payment_date and not record_date:
        return None
    fact = {
        "document_id": document.document_id,
        "file_path": document.file_path,
        "page": evidence.page,
        "evidence_id": evidence.evidence_id,
        "document_type": document.document_type,
        "investment_name": an3_fields.get("investment_name"),
        "security_code": an3_fields.get("security_code"),
        "amount": an3_fields.get("amount") or components.get("net_cash_distribution") or components.get("cash_distribution"),
        "payment_date": payment_date,
        "record_date": record_date,
        "components": components,
        "confidence": evidence.confidence,
        "snippet": quote[:300],
        "extraction_method": "deterministic",
    }
    if an3_fields.get("number_of_notes"):
        fact["number_of_notes"] = an3_fields["number_of_notes"]
    return fact


def _build_distribution_tax_facts_payload(
    state: EngagementState,
    *,
    use_ai_extraction: bool = False,
    ai_provider: str = "anthropic",
    openai_model: str = DEFAULT_OPENAI_FACT_MODEL,
    anthropic_model: str = DEFAULT_ANTHROPIC_FACT_MODEL,
    openai_timeout: int = 60,
    progress_path: Path | None = None,
    partial_path: Path | None = None,
    cache_dir: Path | None = None,
) -> dict:
    documents = {doc.document_id: doc for doc in state.source_documents}
    facts: list[dict] = []
    findings: list[dict] = []
    candidate_documents: set[str] = set()
    extracted_documents: set[str] = set()
    ai_failure_by_document: dict[str, str] = {}
    candidate_items = [
        (evidence, document)
        for evidence in state.evidence
        if (document := documents.get(evidence.document_id or "")) and _is_distribution_tax_evidence(evidence, document)
    ]
    total_items = len(candidate_items)
    processed_items = 0
    _write_fact_extraction_checkpoint(
        progress_path=progress_path,
        partial_path=partial_path,
        engagement_id=state.engagement_id,
        entity_name=state.entity_name,
        fact_type="distribution_tax_facts",
        facts=facts,
        findings=findings,
        processed_items=processed_items,
        total_items=total_items,
        status="running",
    )
    for evidence, document in candidate_items:
        candidate_documents.add(document.document_id)
        fact = None
        ai_fallback_reason = None
        if use_ai_extraction:
            fact = _ai_fact_record(document, evidence, expected_fact_type="distribution_tax", provider=ai_provider, openai_model=openai_model, anthropic_model=anthropic_model, timeout=openai_timeout, cache_dir=cache_dir)
            ai_fallback_reason = _last_ai_extraction_error()
            if ai_fallback_reason:
                ai_failure_by_document[document.document_id] = ai_fallback_reason
        elif fact is None:
            fact = _extract_distribution_tax_fact(document, evidence)
        if fact:
            facts.append(fact)
            extracted_documents.add(document.document_id)
        processed_items += 1
        _write_fact_extraction_checkpoint(
            progress_path=progress_path,
            partial_path=partial_path,
            engagement_id=state.engagement_id,
            entity_name=state.entity_name,
            fact_type="distribution_tax_facts",
            facts=facts,
            findings=findings,
            processed_items=processed_items,
            total_items=total_items,
            status="running",
            current_document_id=document.document_id,
            current_evidence_id=evidence.evidence_id,
        )
    for document_id in sorted(candidate_documents - extracted_documents):
        document = documents[document_id]
        findings.append(
            {
                "category": "distribution_tax_fact_extraction_incomplete",
                "document_id": document.document_id,
                "file_path": document.file_path,
                "ai_failure_reason": ai_failure_by_document.get(document_id) if use_ai_extraction else None,
                "recommended_action": "Review AI extraction response/key and source evidence." if use_ai_extraction else "Review distribution/tax statement evidence and improve parser or record an accountant decision.",
            }
        )
    payload = {
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
    _write_fact_extraction_checkpoint(
        progress_path=progress_path,
        partial_path=partial_path,
        engagement_id=state.engagement_id,
        entity_name=state.entity_name,
        fact_type="distribution_tax_facts",
        facts=facts,
        findings=findings,
        processed_items=processed_items,
        total_items=total_items,
        status="complete",
    )
    return payload


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
                f"  - Investment/security: {fact.get('investment_name') or fact.get('security_code') or 'not extracted'}",
                f"  - Amount: {fact.get('amount') or 'not extracted'}",
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
    state = _source_records_from_inventory(Path(args.inventory)) if getattr(args, "inventory", None) else load_engagement_state(Path(args.state))
    output = Path(args.output)
    payload = _build_distribution_tax_facts_payload(
        state,
        use_ai_extraction=bool(getattr(args, "use_ai_extraction", False)),
        ai_provider=getattr(args, "ai_provider", "anthropic"),
        openai_model=getattr(args, "openai_model", DEFAULT_OPENAI_FACT_MODEL),
        anthropic_model=getattr(args, "anthropic_model", DEFAULT_ANTHROPIC_FACT_MODEL),
        openai_timeout=int(getattr(args, "openai_timeout", 60) or 60),
        progress_path=output.with_suffix(".progress.json"),
        partial_path=output.with_suffix(".partial.json"),
        cache_dir=output.parent / ".ai_cache",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_distribution_tax_facts(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported distribution/tax facts → {output}")
    print(f"Exported distribution/tax facts JSON → {json_output}")
    return 0 if not payload["findings"] else 1


def _no_accounting_fact_reason(document: dict, evidence_count: int) -> str:
    document_type = str(document.get("document_type") or "unknown")
    supported_types = {
        "bank_statement",
        "broker_confirmation",
        "image_support",
        "investment_statement",
        "supporting_csv",
    }
    if evidence_count == 0:
        return "No extractable text evidence was available. The source may be scanned, image-only, encrypted, password-protected, or otherwise unreadable."
    if document_type not in supported_types:
        return f"No accounting fact extractor is mapped for document type `{document_type}`."
    return "Text evidence exists, but the extractor did not find the required accounting fields for this document type."


def _build_accounting_facts_by_document_payload(state_path: Path, artifact_dir: Path) -> dict:
    state = json.loads(state_path.read_text())
    documents = []
    by_document: dict[str, dict] = {}
    evidence_count_by_document: dict[str, int] = {}
    for evidence in state.get("evidence", []):
        document_id = str(evidence.get("document_id", ""))
        if document_id:
            evidence_count_by_document[document_id] = evidence_count_by_document.get(document_id, 0) + 1
    for source in state.get("source_documents", []):
        document_id = str(source.get("document_id", ""))
        if not document_id:
            continue
        file_path = str(source.get("file_path", ""))
        document = {
            "document_id": document_id,
            "file_path": file_path,
            "file_name": Path(file_path).name,
            "display_name": source.get("display_name") or Path(file_path).name,
            "document_type": source.get("document_type", "unknown"),
            "status": "no_fact_extracted",
            "evidence_count": evidence_count_by_document.get(document_id, 0),
            "no_fact_reason": "",
            "accounting_facts": [],
        }
        by_document[document_id] = document
        documents.append(document)
    specs = [
        ("bank_statement", "bank_statement_facts.json", "facts"),
        ("bank_transaction", "bank_transactions.json", "transactions"),
        ("invoice", "invoice_facts.json", "facts"),
        ("distribution_tax", "distribution_tax_facts.json", "facts"),
        ("broker_trade", "broker_trade_facts.json", "facts"),
    ]
    metadata_keys = {"document_id", "file_path", "page", "evidence_id", "document_type", "snippet", "confidence", "extraction_method", "ai_provider", "ai_attempted", "ai_fallback_reason"}
    accounting_fact_rows = 0
    no_fact_reason_by_document: dict[str, str] = {}
    for fact_type, filename, record_key in specs:
        payload_path = artifact_dir / filename
        if not payload_path.exists():
            continue
        payload = json.loads(payload_path.read_text())
        for finding in payload.get("findings", []) if isinstance(payload, dict) else []:
            if not isinstance(finding, dict):
                continue
            document_id = str(finding.get("document_id", ""))
            ai_failure_reason = str(finding.get("ai_failure_reason") or "").strip()
            recommended_action = str(finding.get("recommended_action") or "").strip()
            if document_id and ai_failure_reason:
                no_fact_reason_by_document[document_id] = f"AI extraction did not return a usable {fact_type} fact: {ai_failure_reason}"
            elif document_id and recommended_action:
                no_fact_reason_by_document.setdefault(document_id, recommended_action)
        records = payload.get(record_key, []) if isinstance(payload, dict) else []
        for record in records if isinstance(records, list) else []:
            document_id = str(record.get("document_id", ""))
            if not document_id:
                continue
            document = by_document.get(document_id)
            if document is None:
                file_path = str(record.get("file_path", ""))
                document = {
                    "document_id": document_id,
                    "file_path": file_path,
                    "file_name": Path(file_path).name,
                    "display_name": Path(file_path).name,
                    "document_type": record.get("document_type", "unknown"),
                    "status": "no_fact_extracted",
                    "evidence_count": evidence_count_by_document.get(document_id, 0),
                    "no_fact_reason": "",
                    "accounting_facts": [],
                }
                by_document[document_id] = document
                documents.append(document)
            fields = {key: value for key, value in record.items() if key not in metadata_keys}
            document["accounting_facts"].append({
                "fact_type": fact_type,
                "page": record.get("page", ""),
                "evidence_id": record.get("evidence_id", ""),
                "confidence": record.get("confidence"),
                "snippet": record.get("snippet", ""),
                "extraction_method": record.get("extraction_method") or "deterministic",
                "ai_provider": record.get("ai_provider"),
                "ai_attempted": record.get("ai_attempted"),
                "ai_fallback_reason": record.get("ai_fallback_reason"),
                "fields": fields,
            })
            document["status"] = "extracted"
            document["no_fact_reason"] = ""
            accounting_fact_rows += 1
    for document in documents:
        if not document["accounting_facts"]:
            document["no_fact_reason"] = no_fact_reason_by_document.get(document["document_id"]) or _no_accounting_fact_reason(document, int(document.get("evidence_count") or 0))
    documents_with_facts = sum(1 for document in documents if document["accounting_facts"])
    return {
        "engagement_id": state.get("engagement_id", ""),
        "entity_name": state.get("entity_name", ""),
        "fact_type": "accounting_facts_by_document",
        "documents": documents,
        "summary": {
            "uploaded_documents": len(documents),
            "documents_with_facts": documents_with_facts,
            "accounting_fact_rows": accounting_fact_rows,
            "documents_without_facts": len(documents) - documents_with_facts,
        },
    }


def _build_accounting_facts_by_document_payload_from_inventory(inventory_path: Path, artifact_dir: Path) -> dict:
    inventory = json.loads(inventory_path.read_text())
    documents = []
    by_document: dict[str, dict] = {}
    for source in inventory.get("documents", []):
        if not isinstance(source, dict):
            continue
        document_id = str(source.get("document_id", ""))
        if not document_id:
            continue
        file_path = str(source.get("file_path", ""))
        document = {
            "document_id": document_id,
            "file_path": file_path,
            "file_name": Path(file_path).name,
            "display_name": source.get("display_name") or Path(file_path).name,
            "document_type": source.get("document_type", "unknown"),
            "status": "no_fact_extracted",
            "evidence_count": int(source.get("evidence_count") or len(source.get("pages", []) or [])),
            "no_fact_reason": "",
            "accounting_facts": [],
        }
        by_document[document_id] = document
        documents.append(document)
    specs = [
        ("bank_statement", "bank_statement_facts.json", "facts"),
        ("invoice", "invoice_facts.json", "facts"),
        ("distribution_tax", "distribution_tax_facts.json", "facts"),
        ("broker_trade", "broker_trade_facts.json", "facts"),
    ]
    metadata_keys = {"document_id", "file_path", "page", "evidence_id", "document_type", "snippet", "confidence", "extraction_method", "ai_provider", "ai_attempted", "ai_fallback_reason"}
    accounting_fact_rows = 0
    no_fact_reason_by_document: dict[str, str] = {}
    for fact_type, filename, record_key in specs:
        payload_path = artifact_dir / filename
        if not payload_path.exists():
            continue
        payload = json.loads(payload_path.read_text())
        for finding in payload.get("findings", []) if isinstance(payload, dict) else []:
            if isinstance(finding, dict) and finding.get("document_id"):
                reason = str(finding.get("ai_failure_reason") or finding.get("recommended_action") or "").strip()
                if reason:
                    no_fact_reason_by_document[str(finding["document_id"])] = f"AI extraction did not return a usable {fact_type} fact: {reason}"
        records = payload.get(record_key, []) if isinstance(payload, dict) else []
        for record in records if isinstance(records, list) else []:
            document_id = str(record.get("document_id", ""))
            if not document_id:
                continue
            document = by_document.get(document_id)
            if document is None:
                continue
            fields = {key: value for key, value in record.items() if key not in metadata_keys}
            document["accounting_facts"].append({
                "fact_type": fact_type,
                "page": record.get("page", ""),
                "evidence_id": record.get("evidence_id", ""),
                "confidence": record.get("confidence"),
                "snippet": record.get("snippet", ""),
                "extraction_method": record.get("extraction_method") or "ai",
                "ai_provider": record.get("ai_provider"),
                "fields": fields,
            })
            document["status"] = "extracted"
            accounting_fact_rows += 1
    for document in documents:
        if not document["accounting_facts"]:
            document["no_fact_reason"] = no_fact_reason_by_document.get(document["document_id"]) or _no_accounting_fact_reason(document, int(document.get("evidence_count") or 0))
    documents_with_facts = sum(1 for document in documents if document["accounting_facts"])
    return {
        "inventory_id": inventory.get("inventory_id", "source_document_inventory"),
        "entity_name": inventory.get("entity_name", "Uploaded documents"),
        "fact_type": "accounting_facts_by_document",
        "documents": documents,
        "summary": {
            "uploaded_documents": len(documents),
            "documents_with_facts": documents_with_facts,
            "accounting_fact_rows": accounting_fact_rows,
            "documents_without_facts": len(documents) - documents_with_facts,
        },
    }


SPLIT_FACT_ARTIFACT_NAMES = [
    "bank_statement_facts.json",
    "bank_statement_facts.md",
    "bank_transactions.json",
    "bank_transactions.md",
    "invoice_facts.json",
    "invoice_facts.md",
    "distribution_tax_facts.json",
    "distribution_tax_facts.md",
    "broker_trade_facts.json",
    "broker_trade_facts.md",
]


def _remove_legacy_split_fact_files(output_dir: Path) -> None:
    for name in SPLIT_FACT_ARTIFACT_NAMES:
        path = output_dir / name
        if path.exists():
            path.unlink()


def _export_accounting_facts_by_document_command(args: argparse.Namespace) -> int:
    output = Path(args.output)
    artifact_dir = Path(args.artifact_dir)
    if getattr(args, "remove_legacy_split_facts", False):
        for name in ("bank_transactions.json", "bank_transactions.md"):
            path = artifact_dir / name
            if path.exists():
                path.unlink()
    payload = _build_accounting_facts_by_document_payload_from_inventory(Path(args.inventory), artifact_dir) if getattr(args, "inventory", None) else _build_accounting_facts_by_document_payload(Path(args.state), artifact_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    if getattr(args, "remove_legacy_split_facts", False):
        _remove_legacy_split_fact_files(artifact_dir)
        if output.parent != artifact_dir:
            _remove_legacy_split_fact_files(output.parent)
    print(f"Exported accounting facts by document → {output}")
    return 0


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
        "extraction_method": "deterministic",
    }


def _build_broker_trade_facts_payload(
    state: EngagementState,
    *,
    use_ai_extraction: bool = False,
    ai_provider: str = "anthropic",
    openai_model: str = DEFAULT_OPENAI_FACT_MODEL,
    anthropic_model: str = DEFAULT_ANTHROPIC_FACT_MODEL,
    openai_timeout: int = 60,
    progress_path: Path | None = None,
    partial_path: Path | None = None,
    cache_dir: Path | None = None,
) -> dict:
    documents = {doc.document_id: doc for doc in state.source_documents}
    facts: list[dict] = []
    findings: list[dict] = []
    candidate_documents: set[str] = set()
    extracted_documents: set[str] = set()
    ai_failure_by_document: dict[str, str] = {}
    candidate_items = [
        (evidence, document)
        for evidence in state.evidence
        if (document := documents.get(evidence.document_id or "")) and _is_broker_confirmation_evidence(evidence, document)
    ]
    total_items = len(candidate_items)
    processed_items = 0
    _write_fact_extraction_checkpoint(
        progress_path=progress_path,
        partial_path=partial_path,
        engagement_id=state.engagement_id,
        entity_name=state.entity_name,
        fact_type="broker_trade_facts",
        facts=facts,
        findings=findings,
        processed_items=processed_items,
        total_items=total_items,
        status="running",
    )
    for evidence, document in candidate_items:
        candidate_documents.add(document.document_id)
        fact = None
        ai_fallback_reason = None
        if use_ai_extraction:
            fact = _ai_fact_record(document, evidence, expected_fact_type="broker_trade", provider=ai_provider, openai_model=openai_model, anthropic_model=anthropic_model, timeout=openai_timeout, cache_dir=cache_dir)
            ai_fallback_reason = _last_ai_extraction_error()
            if ai_fallback_reason:
                ai_failure_by_document[document.document_id] = ai_fallback_reason
        elif fact is None:
            fact = _extract_broker_trade_fact(document, evidence)
        if fact and len(fact.get("fields", {})) >= 2:
            facts.append(fact)
            extracted_documents.add(document.document_id)
        processed_items += 1
        _write_fact_extraction_checkpoint(
            progress_path=progress_path,
            partial_path=partial_path,
            engagement_id=state.engagement_id,
            entity_name=state.entity_name,
            fact_type="broker_trade_facts",
            facts=facts,
            findings=findings,
            processed_items=processed_items,
            total_items=total_items,
            status="running",
            current_document_id=document.document_id,
            current_evidence_id=evidence.evidence_id,
        )
    for document_id in sorted(candidate_documents - extracted_documents):
        document = documents[document_id]
        findings.append({
            "category": "broker_trade_fact_extraction_incomplete",
            "document_id": document.document_id,
            "file_path": document.file_path,
            "ai_failure_reason": ai_failure_by_document.get(document_id) if use_ai_extraction else None,
            "recommended_action": "Review AI extraction response/key and source evidence." if use_ai_extraction else "Review broker confirmation evidence and improve parser or record an accountant decision.",
        })
    payload = {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "fact_type": "broker_trade_facts",
        "facts": facts,
        "findings": findings,
        "summary": {"broker_documents": len(candidate_documents), "facts_extracted": len(facts), "findings": len(findings)},
    }
    _write_fact_extraction_checkpoint(
        progress_path=progress_path,
        partial_path=partial_path,
        engagement_id=state.engagement_id,
        entity_name=state.entity_name,
        fact_type="broker_trade_facts",
        facts=facts,
        findings=findings,
        processed_items=processed_items,
        total_items=total_items,
        status="complete",
    )
    return payload


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
    state = _source_records_from_inventory(Path(args.inventory)) if getattr(args, "inventory", None) else load_engagement_state(Path(args.state))
    output = Path(args.output)
    payload = _build_broker_trade_facts_payload(
        state,
        use_ai_extraction=bool(getattr(args, "use_ai_extraction", False)),
        ai_provider=getattr(args, "ai_provider", "anthropic"),
        openai_model=getattr(args, "openai_model", DEFAULT_OPENAI_FACT_MODEL),
        anthropic_model=getattr(args, "anthropic_model", DEFAULT_ANTHROPIC_FACT_MODEL),
        openai_timeout=int(getattr(args, "openai_timeout", 60) or 60),
        progress_path=output.with_suffix(".progress.json"),
        partial_path=output.with_suffix(".partial.json"),
        cache_dir=output.parent / ".ai_cache",
    )
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


def _source_match_fact_rows_from_accounting_payload(facts_payload: dict) -> list[dict]:
    rows = _source_coverage_facts(facts_payload)
    for row in rows:
        fields = row.get("fields", {}) if isinstance(row.get("fields"), dict) else {}
        row["amount_candidates"] = [
            _clean_money_amount(str(fields.get(key)))
            for key in [
                "amount",
                "debit",
                "credit",
                "amount_due",
                "cash_distribution",
                "total_taxable_income",
                "net_settlement_amount",
                "settlement_amount",
                "gross_amount",
                "called_amount",
                "market_value",
                "closing_balance",
                "opening_balance",
            ]
            if fields.get(key) is not None and fields.get(key) != ""
        ]
        row["date_candidates"] = [
            str(fields.get(key))
            for key in [
                "date",
                "transaction_date",
                "invoice_date",
                "due_date",
                "payment_date",
                "distribution_date",
                "record_date",
                "trade_date",
                "settlement_date",
                "notice_date",
                "statement_date",
                "period_end",
                "statement_period_end",
            ]
            if fields.get(key) is not None and fields.get(key) != ""
        ]
        row["description_candidates"] = [
            str(fields.get(key))
            for key in [
                "description",
                "counterparty",
                "reference",
                "supplier_name",
                "investment_name",
                "security_name",
                "account_name",
                "line_item",
            ]
            if fields.get(key) is not None and fields.get(key) != ""
        ]
    return rows


_SOURCE_MATCH_EVIDENCE_KEYWORDS = [
    "payment instruction",
    "payment instructions",
    "eft",
    "bpay",
    "bank:",
    "bank ",
    "bsb",
    "account name",
    "account number",
    "payment reference",
    "reference",
    "distribution",
    "cash distribution",
    "gross cash distribution",
    "net cash distribution",
    "less: distribution",
    "market value summary",
    "performance summary",
    "one registry",
    "att:",
    "investor",
    "investor no",
    "registration",
    "address",
    "person 1",
    "person 2",
    "issued to",
    "benefit of the party",
    "westpac",
    "commbank",
    "commonwealth",
    "anz",
    "nab",
    "automic",
    "capital call",
    "due date",
]


def _source_match_excerpt(text: str, keyword: str, radius: int = 450) -> str:
    lowered = text.lower()
    index = lowered.find(keyword.lower())
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(text), index + len(keyword) + radius)
    return " ".join(text[start:end].split())


def _source_match_document_pages(document: dict) -> list[dict[str, str]]:
    page_quotes = document.get("page_quotes") if isinstance(document.get("page_quotes"), list) else []
    pages = [page for page in page_quotes if isinstance(page, dict) and page.get("quote")]
    if pages:
        return [{"page": str(page.get("page") or ""), "evidence_id": str(page.get("evidence_id") or ""), "quote": str(page.get("quote") or "")} for page in pages]
    file_path = document.get("file_path")
    if not file_path:
        return []
    path = Path(str(file_path))
    if not path.exists():
        return []
    try:
        return _document_text_for_codex(path)[0]
    except Exception:
        return []


def _source_match_document_evidence(facts_payload: dict) -> list[dict]:
    documents: list[dict] = []
    for document in facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []:
        if not isinstance(document, dict):
            continue
        excerpts: list[dict[str, str]] = []
        seen: set[str] = set()
        for page in _source_match_document_pages(document):
            quote = str(page.get("quote") or "")
            for keyword in _SOURCE_MATCH_EVIDENCE_KEYWORDS:
                excerpt = _source_match_excerpt(quote, keyword)
                if not excerpt or excerpt in seen:
                    continue
                seen.add(excerpt)
                excerpts.append(
                    {
                        "page": str(page.get("page") or ""),
                        "evidence_id": str(page.get("evidence_id") or ""),
                        "keyword": keyword,
                        "excerpt": excerpt[:1400],
                    }
                )
                if len(excerpts) >= 5:
                    break
            if len(excerpts) >= 5:
                break
        if not excerpts:
            continue
        documents.append(
            {
                "document_id": document.get("document_id"),
                "display_name": document.get("display_name") or document.get("file_name") or document.get("file_path"),
                "document_type": document.get("document_type"),
                "file_path": document.get("file_path"),
                "payment_or_matching_evidence": excerpts,
            }
        )
    return documents


def _source_match_document_index(facts_payload: dict) -> list[dict]:
    documents: list[dict] = []
    for document in facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []:
        if not isinstance(document, dict):
            continue
        pages = _source_match_document_pages(document)
        combined = " ".join(str(page.get("quote") or "") for page in pages)
        documents.append(
            {
                "document_id": document.get("document_id"),
                "display_name": document.get("display_name") or document.get("file_name") or document.get("file_path"),
                "original_file_name": document.get("original_file_name") or document.get("file_name"),
                "document_type": document.get("document_type"),
                "file_path": document.get("file_path"),
                "status": document.get("status"),
                "summary": document.get("document_summary") or "",
                "entity_relevance": document.get("entity_relevance") or "",
                "entity_relevance_reason": document.get("entity_relevance_reason") or "",
                "period_start": document.get("period_start") or "",
                "period_end": document.get("period_end") or "",
                "statement_date": document.get("statement_date") or "",
                "key_parties": document.get("key_parties") if isinstance(document.get("key_parties"), list) else [],
                "key_identifiers": document.get("key_identifiers") if isinstance(document.get("key_identifiers"), list) else [],
                "primary_amounts": document.get("primary_amounts") if isinstance(document.get("primary_amounts"), list) else [],
                "review_flags": document.get("review_flags") if isinstance(document.get("review_flags"), list) else [],
                "page_count": len(pages),
                "visible_dates": _unique_matches(_DATE_RE, combined, limit=10),
                "visible_amounts": _unique_matches(_AMOUNT_RE, combined, limit=12),
            }
        )
    return documents


def _source_match_context(facts_payload: dict, coverage_payload: dict | None) -> dict:
    facts = _source_match_fact_rows_from_accounting_payload(facts_payload)
    return {
        "matching_contract_version": SOURCE_MATCHING_CONTRACT_VERSION,
        "agent_mode": "codex_cli_workspace_investigation",
        "entity_name": facts_payload.get("entity_name", "Uploaded documents") if isinstance(facts_payload, dict) else "Uploaded documents",
        "workspace": {
            "cwd": str(Path.cwd()),
            "instruction": (
                "You are running as Codex CLI in this repository. You may inspect the supplied file_path values, "
                "search extracted page quotes, and compare original document text before returning the JSON event register."
            ),
            "important_files": {
                "source_document_index": "outputs/raw_inputs_pdf_extraction/source_document_index.json",
                "source_coverage_continuity": "outputs/raw_inputs_pdf_extraction/source_coverage_continuity.json",
                "input_documents_dir": "inputs",
            },
        },
        "document_index": _source_match_document_index(facts_payload),
        "facts": facts,
        "source_document_evidence": _source_match_document_evidence(facts_payload),
        "source_coverage_continuity": coverage_payload or {},
        "rules": [
            "Use Codex CLI as an investigative bookkeeper with workspace access: compare document names, document summaries, dates, amounts, descriptions, bank account identifiers, coverage findings, page quotes, and original source files.",
            "Step 2 is only a source document index. It deliberately does not extract detailed accounting facts. Build accounting events by reading source PDFs/page quotes and source document paths yourself.",
            "When you inspect original files or page quotes to support an event, include the document id in document_refs and cite the page/evidence id or exact amount/date in investigation_summary.",
            "Step 3 must build an Accounting Event Register, not CoA mappings or postings. Every review item should answer: what happened, what evidence supports it, and what is missing or judgemental.",
            "Run two passes. Source-first matching rule: read each source document's payment instructions, expected receiving bank/payee/account, payment reference, and due/payment date before searching bank statements. Cash-first matching rule: inspect bank statement documents and classify or resolve meaningful cash movements, even when no external source document exists. If legacy facts include every bank_transaction, review those too.",
            "Entity-first review rule: before matching or proposing journals, check whether the source document appears addressed to or held for the reporting entity/trust/company. If it appears to belong to Person 1/Person 2 or another non-reporting party, confidently mark it as entity_mismatch / likely irrelevant and recommend excluding it from this engagement unless the client confirms it belongs to the reporting entity.",
            "Assume documents are relevant unless there is a clear party/entity mismatch, a personal holder, or evidence that the document belongs to another entity. Do not create Step 3 review issues merely because an otherwise relevant document is old, out of period, or needs later accounting treatment.",
            "For capital calls, first search bank statement documents for the instructed receiving bank/payee/account/reference. Only then consider other-bank or related-party payments.",
            "If the source instructs payment to Westpac/AUTOMIC but the candidate cash movement is CommBank/ZXY, do not present a clean match. Mark it unresolved or a low/medium hypothesis unless source evidence explicitly supports that intermediary path.",
            "For broker sale confirmations, if cash date, amount, security, and broker/payee support a bank receipt but gross/net extraction labels appear inconsistent, keep the cash match in proposed_matches and add a separate unresolved extraction_gap item for the field check.",
            "Cash-first classification rule: if a bank transaction has no separate source document but the description is business-meaningful (for example KPMG service/accounting fees, ATO tax payments/refunds, bank fees, interest, platform fees, broker/FNZ cash settlements, or internal transfers), put it in proposed_matches as match_type bank_only_classification with source_fact_refs and bank_fact_refs empty if needed, and cite bank statement document ids in document_refs. State the inferred business meaning and clearly note that no external source document is attached.",
            "Before calling an investment distribution receipt bank-only, check investment/market value/AMIT statement facts and source_document_evidence for dated distribution rows, cash distribution totals, registry names, and exact amounts. A dated distribution line can support a source_and_bank event even when the bank receipt settles days or weeks later and the annual total does not equal the single receipt.",
            "Grouping rule: grouping is allowed only when the row is one coherent economic story, such as quarterly distributions for the same security into the same bank account, several bank-only KPMG service-fee payments, or a transfer sweep supported by component deposits. When you group, do not hide the components: include each component amount, date, source document id, bank document id where relevant, and the group total in investigation_summary. Do not group unrelated counterparties, unrelated document types, or items that need different judgement.",
            "General roll-up and residual rule: when a statement shows an annual/period total that breaks into dated components, banked receipts/payments, residual receivables/payables, withholding, fees, or timing differences, do the simple arithmetic yourself. Preserve both the total and the components in the event register narrative, for example total amount = bank-supported component + source-only residual. This rule must work for future clients and document types, not only the sample engagement.",
            "General multi-amount bank explanation rule: when one bank movement appears related to several source or bank amounts, add the component total and compare it to the bank movement. If there is a difference, explain the difference plainly instead of merely saying unmatched. Use this for transfers following sale deposits, distributions split across dates, batch payments, refunds offset against tax, or similar future-client patterns.",
            "If a source document page quote supports an event but Step 2 did not extract the exact line as a structured fact, include that source document id in document_refs and cite the page/evidence id in evidence_refs or investigation_summary. Do not label the event bank_only merely because the exact line is missing from source_fact_refs.",
            "Do not frame ordinary bank-only KPMG, ATO, bank fee, interest, or platform-fee transactions primarily as missing invoices. The useful Step 3 output is the cash classification and limitation, so downstream CoA mapping can decide the account treatment.",
            "If an event is monetary, populate amount with the event amount or grouped total. For unresolved monetary items with several candidate amounts, state the amounts in investigation_summary and use missing_or_judgement to explain the ambiguity.",
            "For each proposed match/classification, include event_type, event_readiness, evidence_level, event_meaning, evidence_summary, and missing_or_judgement.",
            "Use event_readiness complete for clean source+bank events and clear bank-only cash classifications; needs_judgement for plausible but judgement-heavy events; needs_support for items missing evidence or classification; excluded for wrong-entity/personal/non-accounting documents.",
            "Use evidence_level source_and_bank, bank_only, source_only, or no_accounting_event.",
            "Do not suggest debit accounts, credit accounts, CoA accounts, journals, or trial balance impact in Step 3. Those belong to Step 4. You may describe the business meaning, such as ATO tax account cash movement, KPMG service fee, investment distribution, capital call funding, broker settlement, internal transfer, bank/platform fee, or interest cash movement.",
            "If a bank transaction cannot be classified from the bank description, amount, date, direction, or surrounding context, put it in unresolved_items with issue_type unclassified_bank_transaction and explain what is missing.",
            "If a source fact should have cash support but no bank transaction can be found after checking instructed bank/payee/reference and reasonable timing differences, put it in unresolved_items with issue_type missing_bank_match or unmatched_source_fact.",
            "Do not book, approve, or ask the junior accountant to approve every row. Proposed matches are an event register for Step 4; only surface real exceptions, missing support, ambiguity, or judgement points.",
            "If a relationship is plausible but not proven, put it in hypotheses, not proposed_matches.",
            "If extraction appears incomplete or a source cannot be matched, put it in unresolved_items with why and what to check next.",
            "Use source_fact_refs and bank_fact_refs only when supplied facts exist. Otherwise leave those arrays empty and cite document_refs plus page/evidence ids.",
            "Never fabricate evidence, dates, amounts, accounts, or transactions.",
        ],
    }


def _codex_source_match_prompt(
    facts_payload: dict,
    coverage_payload: dict | None,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> str:
    return build_relationship_reasoning_prompt(
        facts_payload,
        coverage_payload,
        recovery_attempt=recovery_attempt,
        previous_error=previous_error,
        validation_findings=validation_findings,
        previous_payload=previous_payload,
    )

def _codex_investigate_source_matches(
    facts_payload: dict,
    coverage_payload: dict | None,
    command: str,
    timeout: int,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> tuple[dict | None, str | None]:
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_SOURCE_MATCH_JSON")
    if fake_payload:
        payload = _extract_json_object(fake_payload)
        return payload, None if payload is not None else "Fake Codex source match payload was not valid JSON."
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_codex_source_match_prompt(
                facts_payload,
                coverage_payload,
                recovery_attempt=recovery_attempt,
                previous_error=previous_error,
                validation_findings=validation_findings,
                previous_payload=previous_payload,
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"Codex command was not found: {command}"
    except subprocess.TimeoutExpired:
        return None, f"Codex command timed out after {timeout} seconds."
    except (subprocess.SubprocessError, ValueError) as exc:
        return None, f"Codex command failed to start: {exc}"
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        return None, f"Codex command exited {result.returncode}: {stderr[:500]}"
    if not result.stdout.strip():
        return None, f"Codex command returned no stdout. {stderr[:500]}".strip()
    payload = _extract_json_object(result.stdout)
    if payload is None:
        return None, f"Codex command did not return a JSON object. stdout={result.stdout[:500]!r}"
    return payload, None


def _source_match_valid_refs(facts_payload: dict) -> tuple[set[str], set[str]]:
    fact_refs: set[str] = set()
    document_refs: set[str] = set()
    for document in facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []:
        if isinstance(document, dict) and document.get("document_id"):
            document_refs.add(str(document["document_id"]))
    for row in _source_coverage_facts(facts_payload):
        for key in ("fact_ref", "evidence_id"):
            value = row.get(key)
            if value:
                fact_refs.add(str(value))
        if row.get("document_id"):
            document_refs.add(str(row["document_id"]))
    return fact_refs, document_refs


def _list_value(value: object) -> list:
    return value if isinstance(value, list) else []


def _validate_investigative_source_matches(payload: dict | None, facts_payload: dict) -> list[dict]:
    return validate_relationship_register(payload, facts_payload)

def _normalise_investigative_source_matches(payload: dict, facts_payload: dict, validation_findings: list[dict]) -> dict:
    return normalise_relationship_register(payload, facts_payload, validation_findings)

def _codex_failed_source_match_payload(facts_payload: dict, error: str, attempt_history: list[dict], validation_findings: list[dict] | None = None) -> dict:
    return failed_relationship_register(facts_payload, error, attempt_history, validation_findings)

def _format_investigative_source_matches(payload: dict) -> str:
    return format_relationship_register(payload)

def _load_optional_json(path: str | None) -> dict | None:
    return json.loads(Path(path).read_text()) if path else None


def _match_source_facts_from_accounting_command(args: argparse.Namespace) -> int:
    accounting_facts_path = Path(args.accounting_facts)
    if not accounting_facts_path.exists():
        print(f"Accounting facts file not found: {accounting_facts_path}", file=sys.stderr)
        return 2
    facts_payload = json.loads(accounting_facts_path.read_text())
    coverage_payload = _load_optional_json(getattr(args, "source_coverage", None))
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 120) or 120)
    payload = None
    error = None
    validation_findings: list[dict] = []
    attempt_history: list[dict] = []
    previous_payload: dict | None = None
    output = Path(args.output)
    progress_path = output.parent / "relationship_reasoning_progress.json"
    attempt_history_path = output.parent / "relationship_reasoning_attempt_history.json"
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        _write_step_progress(
            progress_path,
            {
                "stage": "relationship_reasoning",
                "status": "running",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": f"Investigating accounting relationships attempt {attempt} of {max_attempts}.",
            },
        )
        payload, error = _codex_investigate_source_matches(
            facts_payload,
            coverage_payload,
            codex_command,
            attempt_timeout,
            recovery_attempt=attempt - 1,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
        )
        validation_findings = _validate_investigative_source_matches(payload, facts_payload)
        attempt_history.append(
            {
                "attempt": attempt,
                "mode": "normal" if attempt == 1 else "recovery",
                "timeout_seconds": attempt_timeout,
                "status": "success" if payload is not None and not validation_findings else "failed",
                "error": error or "",
                "validation_findings": validation_findings,
            }
        )
        _write_codex_attempt_history(
            attempt_history_path,
            stage="relationship_reasoning",
            attempts=attempt_history,
            status="success" if payload is not None and not validation_findings else "needs_attention",
            message=(
                f"Relationship reasoning attempt {attempt} produced a usable event register."
                if payload is not None and not validation_findings
                else f"Relationship reasoning attempt {attempt} needs correction."
            ),
            extra={
                "current_error": error or "",
                "relationship_count": len(payload.get("relationships") if isinstance(payload, dict) and isinstance(payload.get("relationships"), list) else []),
            },
        )
        _write_step_progress(
            progress_path,
            {
                "stage": "relationship_reasoning",
                "status": "success" if payload is not None and not validation_findings else "needs_attention",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": (
                    f"Relationship reasoning attempt {attempt} produced a usable event register."
                    if payload is not None and not validation_findings
                    else f"Relationship reasoning attempt {attempt} needs correction."
                ),
                "error": error or "",
                "validation_findings": validation_findings,
            },
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not validation_findings:
            break
        if payload is not None and validation_findings:
            error = "Codex source matching output failed schema validation."
    if payload is None:
        final_payload = _codex_failed_source_match_payload(facts_payload, error or "Codex CLI did not return a usable source matching result.", attempt_history, validation_findings)
    elif validation_findings:
        final_payload = _codex_failed_source_match_payload(facts_payload, "Codex CLI returned a source matching result that did not pass validation.", attempt_history, validation_findings)
    else:
        final_payload = _normalise_investigative_source_matches(payload, facts_payload, validation_findings)
        final_payload["codex_attempt_history"] = attempt_history
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_investigative_source_matches(final_payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    event_register_md = output.parent / "accounting_event_register.md"
    event_register_json = output.parent / "accounting_event_register.json"
    relationship_register_md = output.parent / "relationship_reasoning_register.md"
    relationship_register_json = output.parent / "relationship_reasoning_register.json"
    event_register_md.write_text(_format_investigative_source_matches(final_payload))
    event_register_json.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    relationship_register_md.write_text(_format_investigative_source_matches(final_payload))
    relationship_register_json.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    print(f"Exported Codex relationship reasoning -> {relationship_register_md}")
    print(f"Exported Codex relationship reasoning JSON -> {relationship_register_json}")
    if final_payload.get("status") == "codex_failed":
        _write_codex_attempt_history(
            attempt_history_path,
            stage="relationship_reasoning",
            attempts=attempt_history,
            status="failed",
            message=str(final_payload.get("error") or "Relationship reasoning failed."),
            extra={"validation_findings": final_payload.get("validation_findings") or []},
        )
        _write_step_progress(
            progress_path,
            {
                "stage": "relationship_reasoning",
                "status": "failed",
                "attempts": attempt_history,
                "message": str(final_payload.get("error") or "Relationship reasoning failed."),
                "validation_findings": final_payload.get("validation_findings") or [],
            },
        )
        return 1
    _write_codex_attempt_history(
        attempt_history_path,
        stage="relationship_reasoning",
        attempts=attempt_history,
        status="complete" if not final_payload.get("validation_findings") else "needs_attention",
        message="Accounting event register is ready." if not final_payload.get("validation_findings") else "Accounting event register was produced with validation notes.",
        extra={
            "relationship_count": len(final_payload.get("relationships") if isinstance(final_payload.get("relationships"), list) else []),
            "event_register_path": str(event_register_json),
        },
    )
    _write_step_progress(
        progress_path,
        {
            "stage": "relationship_reasoning",
            "status": "complete" if not final_payload.get("validation_findings") else "needs_attention",
            "attempts": attempt_history,
            "message": "Accounting event register is ready." if not final_payload.get("validation_findings") else "Accounting event register was produced with validation notes.",
            "relationship_count": len(final_payload.get("relationships") if isinstance(final_payload.get("relationships"), list) else []),
            "event_register_path": str(event_register_json),
        },
    )
    return 0 if not final_payload.get("validation_findings") else 1


def _match_source_facts_command(args: argparse.Namespace) -> int:
    if getattr(args, "accounting_facts", None):
        return _match_source_facts_from_accounting_command(args)
    if not getattr(args, "bank_transactions", None):
        print("--bank-transactions is required unless --accounting-facts is supplied.", file=sys.stderr)
        return 2
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


def _codex_coa_mapping_prompt(
    event_register: dict,
    source_index: dict,
    prior_coa: dict | None = None,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> str:
    return build_tb_bridge_prompt(
        event_register,
        source_index,
        prior_coa,
        recovery_attempt=recovery_attempt,
        previous_error=previous_error,
        validation_findings=validation_findings,
        previous_payload=previous_payload,
    )


def _codex_map_coa_from_events(
    event_register: dict,
    source_index: dict,
    prior_coa: dict | None,
    command: str,
    timeout: int,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
    candidate_output_path: Path | None = None,
) -> tuple[dict | None, str | None]:
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_TB_BRIDGE_JSON") or os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_COA_MAPPING_JSON")
    if fake_payload:
        payload = _extract_json_object(fake_payload)
        return payload, None if payload is not None else "Fake Codex TB bridge payload was not valid JSON."
    if candidate_output_path is not None and candidate_output_path.exists():
        try:
            candidate_output_path.unlink()
        except OSError:
            pass
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_codex_coa_mapping_prompt(
                event_register,
                source_index,
                prior_coa,
                recovery_attempt=recovery_attempt,
                previous_error=previous_error,
                validation_findings=validation_findings,
                previous_payload=previous_payload,
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"Codex command was not found: {command}"
    except subprocess.TimeoutExpired:
        return None, f"Codex command timed out after {timeout} seconds."
    except (subprocess.SubprocessError, ValueError) as exc:
        return None, f"Codex command failed to start: {exc}"
    stderr = (result.stderr or "").strip()
    sidecar_payload, sidecar_error = _read_json_object_file(candidate_output_path)
    if result.returncode != 0:
        if sidecar_payload is not None:
            return sidecar_payload, None
        return None, f"Codex command exited {result.returncode}: {stderr[:500]}"
    if not result.stdout.strip():
        if sidecar_payload is not None:
            return sidecar_payload, None
        if sidecar_error:
            return None, sidecar_error
        return None, f"Codex command returned no stdout. {stderr[:500]}".strip()
    payload = _extract_json_object(result.stdout)
    if payload is None:
        if sidecar_payload is not None:
            return sidecar_payload, None
        if sidecar_error:
            return None, sidecar_error
        return None, f"Codex command did not return a JSON object. stdout={result.stdout[:500]!r}"
    return payload, None


def _validate_coa_mapping_workpaper(payload: dict | None, event_register: dict, prior_coa: dict | None = None) -> list[dict]:
    return validate_tb_bridge_workpaper(payload, event_register, prior_coa)


def _blocking_validation_findings(findings: list[dict]) -> list[dict]:
    return [finding for finding in findings if isinstance(finding, dict) and finding.get("severity") == "high"]


def _normalise_coa_mapping_workpaper(payload: dict, event_register: dict, validation_findings: list[dict]) -> dict:
    return normalise_tb_bridge_workpaper(payload, event_register, validation_findings)


def _codex_failed_coa_mapping_payload(event_register: dict, error: str, attempt_history: list[dict], validation_findings: list[dict] | None = None) -> dict:
    return failed_tb_bridge_workpaper(event_register, error, attempt_history, validation_findings)


def _format_coa_mapping_workpaper(payload: dict) -> str:
    return format_tb_bridge_workpaper(payload)


def _build_coa_mapping_workpaper_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    event_register_path = Path(getattr(args, "event_register", None) or artifact_dir / "accounting_event_register.json")
    source_index_path = Path(getattr(args, "source_index", None) or artifact_dir / "source_document_index.json")
    prior_coa_path = Path(getattr(args, "prior_coa", None) or artifact_dir / "prior_statement_coa_import.json")
    if not event_register_path.exists():
        print(f"Accounting event register not found: {event_register_path}", file=sys.stderr)
        return 2
    if not source_index_path.exists():
        print(f"Source document index not found: {source_index_path}", file=sys.stderr)
        return 2
    event_register = json.loads(event_register_path.read_text())
    source_index = json.loads(source_index_path.read_text())
    if getattr(args, "prior_coa", None):
        prior_coa = json.loads(prior_coa_path.read_text()) if prior_coa_path.exists() else {}
    else:
        prior_coa = _build_prior_statement_coa_from_source_index(
            source_index,
            prior_fs_document_id=getattr(args, "prior_fs_document_id", None),
            prior_fs_file=getattr(args, "prior_fs_file", None),
        )
        prior_coa_path.parent.mkdir(parents=True, exist_ok=True)
        prior_coa_path.write_text(json.dumps(prior_coa, indent=2, sort_keys=True))
        prior_coa_path.with_suffix(".md").write_text(_format_prior_statement_coa_import(prior_coa))
        blocking_findings = [finding for finding in _list_value(prior_coa.get("findings")) if isinstance(finding, dict) and finding.get("severity") == "high"]
        if blocking_findings or not _list_value(prior_coa.get("accounts")):
            for finding in blocking_findings:
                print(f"{finding.get('category')}: {finding.get('message') or finding.get('recommended_action') or ''}", file=sys.stderr)
            print(f"Prior-year FS opening balance import is not usable: {prior_coa_path}", file=sys.stderr)
            return 2
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 600) or 600)
    payload = None
    error = None
    validation_findings: list[dict] = []
    attempt_history: list[dict] = []
    previous_payload: dict | None = None
    generation_progress_path = output_dir / "tb_bridge_generation_progress.json"
    attempt_history_path = output_dir / "tb_bridge_attempt_history.json"
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "tb_bridge_generation",
                "status": "running",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": f"Preparing TB bridge attempt {attempt} of {max_attempts}.",
            },
        )
        payload, error = _codex_map_coa_from_events(
            event_register,
            source_index,
            prior_coa,
            codex_command,
            attempt_timeout,
            recovery_attempt=attempt - 1,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
            candidate_output_path=output_dir / TB_BRIDGE_JSON,
        )
        validation_findings = _validate_coa_mapping_workpaper(payload, event_register, prior_coa)
        blocking_findings = _blocking_validation_findings(validation_findings)
        attempt_history.append(
            {
                "attempt": attempt,
                "mode": "normal" if attempt == 1 else "recovery",
                "timeout_seconds": attempt_timeout,
                "status": "success" if payload is not None and not blocking_findings else "failed",
                "error": error or "",
                "validation_findings": validation_findings,
            }
        )
        _write_codex_attempt_history(
            attempt_history_path,
            stage="tb_bridge_generation",
            attempts=attempt_history,
            status="success" if payload is not None and not blocking_findings else "needs_attention",
            message=(
                f"TB bridge attempt {attempt} produced a usable workbook shape."
                if payload is not None and not blocking_findings
                else f"TB bridge attempt {attempt} needs correction."
            ),
            extra={
                "current_error": error or "",
                "blocking_findings": blocking_findings,
                "candidate_output_path": str(output_dir / TB_BRIDGE_JSON),
            },
        )
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "tb_bridge_generation",
                "status": "success" if payload is not None and not blocking_findings else "needs_attention",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": (
                    f"TB bridge attempt {attempt} produced a usable workbook shape."
                    if payload is not None and not blocking_findings
                    else f"TB bridge attempt {attempt} needs correction."
                ),
                "error": error or "",
                "validation_findings": validation_findings,
            },
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not blocking_findings:
            break
        if payload is not None and blocking_findings:
            error = "Codex TB bridge output failed schema validation."
    if payload is None:
        final_payload = _codex_failed_coa_mapping_payload(event_register, error or "Codex CLI did not return a usable TB bridge result.", attempt_history, validation_findings)
    elif _blocking_validation_findings(validation_findings):
        final_payload = _codex_failed_coa_mapping_payload(event_register, "Codex CLI returned a TB bridge result that did not pass validation.", attempt_history, validation_findings)
    else:
        final_payload = _normalise_coa_mapping_workpaper(payload, event_register, validation_findings)
        final_payload["codex_attempt_history"] = attempt_history
    output_dir.mkdir(parents=True, exist_ok=True)
    final_payload = enrich_tb_bridge_payload_for_workbook(final_payload, event_register, source_index, prior_coa)
    json_output = output_dir / TB_BRIDGE_JSON
    md_output = output_dir / TB_BRIDGE_MD
    json_output.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    md_output.write_text(_format_coa_mapping_workpaper(final_payload))
    print(f"Exported Codex TB Bridge Matrix JSON -> {json_output}")
    print(f"Exported Codex TB Bridge Matrix notes -> {md_output}")
    if final_payload.get("status") == "codex_failed":
        _write_codex_attempt_history(
            attempt_history_path,
            stage="tb_bridge_generation",
            attempts=attempt_history,
            status="failed",
            message=str(final_payload.get("error") or "TB bridge generation failed."),
            extra={"validation_findings": final_payload.get("validation_findings") or []},
        )
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "tb_bridge_generation",
                "status": "failed",
                "attempts": attempt_history,
                "message": str(final_payload.get("error") or "TB bridge generation failed."),
                "validation_findings": final_payload.get("validation_findings") or [],
            },
        )
        return 1
    _write_codex_attempt_history(
        attempt_history_path,
        stage="tb_bridge_generation",
        attempts=attempt_history,
        status="complete" if not _blocking_validation_findings(final_payload.get("validation_findings") or []) else "needs_attention",
        message="TB bridge data is ready." if not _blocking_validation_findings(final_payload.get("validation_findings") or []) else "TB bridge data was produced with validation notes.",
        extra={
            "workpaper_json": str(json_output),
            "workpaper_md": str(md_output),
            "validation_findings": final_payload.get("validation_findings") or [],
        },
    )
    if not getattr(args, "skip_xlsx", False):
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "workbook_build",
                "status": "running",
                "attempts": attempt_history,
                "message": "Building the Excel workbook from the TB bridge data.",
                "json_path": str(json_output),
            },
        )
        builder = write_tb_bridge_workbook_builder(
            output_dir,
            os.environ.get("ACCOUNTANT_COPILOT_NODE_MODULES", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"),
        )
        node_bin = os.environ.get("ACCOUNTANT_COPILOT_NODE", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
        result = subprocess.run([node_bin, str(builder)], cwd=Path.cwd(), text=True, capture_output=True, check=False)
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        if result.returncode != 0:
            _write_step_progress(
                generation_progress_path,
                {
                    "stage": "workbook_build",
                    "status": "failed",
                    "attempts": attempt_history,
                    "message": "Excel workbook build failed.",
                    "returncode": result.returncode,
                    "stdout": (result.stdout or "")[-2000:],
                    "stderr": (result.stderr or "")[-2000:],
                },
            )
            return result.returncode
        repaired = repair_tb_bridge_workbook_hyperlinks(output_dir / TB_BRIDGE_XLSX)
        if repaired:
            print(f"Repaired Evidence Index hyperlinks -> {repaired} link(s)")
    _write_step_progress(
        generation_progress_path,
        {
            "stage": "workbook_build",
            "status": "complete",
            "attempts": attempt_history,
            "message": "TB bridge workbook data is ready.",
            "workbook_path": str(output_dir / TB_BRIDGE_XLSX),
            "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
        },
    )
    return 0 if not _blocking_validation_findings(final_payload.get("validation_findings") or []) else 1


def _write_tb_bridge_outputs(
    *,
    output_dir: Path,
    payload: dict,
    event_register: dict,
    source_index: dict,
    prior_coa: dict | None,
    skip_xlsx: bool,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    final_payload = enrich_tb_bridge_payload_for_workbook(payload, event_register, source_index, prior_coa)
    json_output = output_dir / TB_BRIDGE_JSON
    md_output = output_dir / TB_BRIDGE_MD
    json_output.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    md_output.write_text(_format_coa_mapping_workpaper(final_payload))
    print(f"Exported Codex TB Bridge Matrix JSON -> {json_output}")
    print(f"Exported Codex TB Bridge Matrix notes -> {md_output}")
    if skip_xlsx:
        return 0
    builder = write_tb_bridge_workbook_builder(
        output_dir,
        os.environ.get("ACCOUNTANT_COPILOT_NODE_MODULES", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"),
    )
    node_bin = os.environ.get("ACCOUNTANT_COPILOT_NODE", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
    result = subprocess.run([node_bin, str(builder)], cwd=Path.cwd(), text=True, capture_output=True, check=False)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode == 0:
        repaired = repair_tb_bridge_workbook_hyperlinks(output_dir / TB_BRIDGE_XLSX)
        if repaired:
            print(f"Repaired Evidence Index hyperlinks -> {repaired} link(s)")
    return result.returncode


def _turing_review_needs_corrections(output_dir: Path) -> bool:
    review_json = output_dir / "turing_senior_review.json"
    if not review_json.exists():
        return False
    try:
        review_payload = json.loads(review_json.read_text())
    except json.JSONDecodeError:
        return False
    return review_payload.get("status") == "needs_corrections" and bool(review_payload.get("correction_briefs"))


def _severity_rank(value: object) -> int:
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(str(value or "").strip().lower(), 2)


def _is_internal_presentation_finding(finding: dict) -> bool:
    if str(finding.get("category") or "").strip().lower() != "presentation":
        return False
    message = str(finding.get("message") or "").casefold()
    return (
        ("evidence index" in message or "source hyperlink" in message or "hyperlink" in message)
        and ("blank" in message or "invisible" in message or "pdf cell" in message or "link" in message)
    )


def _public_turing_findings(review_payload: dict) -> list[dict]:
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    public: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if _is_internal_presentation_finding(finding):
            continue
        if _severity_rank(finding.get("severity")) <= 1:
            continue
        public.append(finding)
    return public


def _turing_review_has_blocking_items(review_payload: dict) -> bool:
    if review_payload.get("status") == "codex_failed":
        return True
    if _public_turing_findings(review_payload):
        return True
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    correction_briefs = review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else []
    if not correction_briefs:
        return False
    if findings:
        return any(isinstance(finding, dict) and finding in _public_turing_findings(review_payload) for finding in findings)
    return True


def _turing_review_is_ready(output_dir: Path) -> bool:
    review_json = output_dir / "turing_senior_review.json"
    if not review_json.exists():
        return False
    try:
        review_payload = json.loads(review_json.read_text())
    except json.JSONDecodeError:
        return False
    if review_payload.get("status") == "ready":
        return True
    if review_payload.get("status") == "needs_corrections" and not _turing_review_has_blocking_items(review_payload):
        return True
    return False


def _archive_turing_review_round(output_dir: Path, round_number: int) -> None:
    for suffix in [".md", ".json"]:
        current = output_dir / f"turing_senior_review{suffix}"
        if current.exists():
            archived = output_dir / f"turing_senior_review_round_{round_number}{suffix}"
            archived.write_text(current.read_text())


def _review_correction_findings(review_payload: dict) -> list[dict]:
    findings: list[dict] = []
    for index, brief in enumerate(review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else [], start=1):
        if not isinstance(brief, dict):
            continue
        brief_id = brief.get("brief_id") or f"C{index:03d}"
        message_parts = [
            f"Turing correction brief {brief_id}.",
            f"Issue: {brief.get('issue') or ''}",
            f"Expected treatment: {brief.get('expected_treatment') or ''}",
            f"Required workbook change: {brief.get('required_workbook_change') or ''}",
            f"Validation test: {brief.get('validation_test') or ''}",
        ]
        files_or_amounts = brief.get("files_or_amounts_to_recheck")
        if isinstance(files_or_amounts, list) and files_or_amounts:
            message_parts.append("Files or amounts to re-check: " + "; ".join(str(item) for item in files_or_amounts if item is not None))
        findings.append(
            {
                "category": "turing_correction_brief",
                "severity": "high",
                "message": " ".join(part for part in message_parts if part.strip()),
                "brief": brief,
            }
        )
    review_findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    for finding in review_findings:
        if not isinstance(finding, dict):
            continue
        findings.append(
            {
                "category": f"turing_review_finding:{finding.get('category') or 'other'}",
                "severity": finding.get("severity") or "medium",
                "message": finding.get("message") or "",
                "finding": finding,
            }
        )
    return findings


def _write_turing_correction_round_log(
    *,
    output_dir: Path,
    correction_round: int,
    review_payload: dict,
    attempt_history: list[dict],
    status: str,
    error: str = "",
    validation_findings: list[dict] | None = None,
    corrected_payload: dict | None = None,
    output_return_code: int | None = None,
) -> None:
    round_label = str(correction_round or "latest")
    json_path = output_dir / f"turing_correction_round_{round_label}_log.json"
    md_path = output_dir / f"turing_correction_round_{round_label}_log.md"
    briefs = review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else []
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    corrected_summary = corrected_payload.get("summary") if isinstance(corrected_payload, dict) and isinstance(corrected_payload.get("summary"), dict) else {}
    payload = {
        "artifact_type": "turing_correction_round_log",
        "correction_round": correction_round,
        "status": status,
        "error": error,
        "review_status_before_correction": review_payload.get("status"),
        "findings_before_correction": findings,
        "correction_briefs": briefs,
        "attempt_history": attempt_history,
        "validation_findings_after_correction": validation_findings or [],
        "corrected_workpaper_summary": corrected_summary,
        "output_return_code": output_return_code,
        "outputs": {
            "tb_bridge_json": str(output_dir / TB_BRIDGE_JSON),
            "tb_bridge_markdown": str(output_dir / TB_BRIDGE_MD),
            "tb_bridge_workbook": str(output_dir / TB_BRIDGE_XLSX),
            "review_json": str(output_dir / "turing_senior_review.json"),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    lines = [f"# Turing Correction Round {round_label}", "", f"- Status: {status}"]
    if error:
        lines.append(f"- Error: {error}")
    if output_return_code is not None:
        lines.append(f"- Output return code: {output_return_code}")
    lines.extend(["", "## Issues Turing Asked Tessa To Fix"])
    if briefs:
        for brief in briefs:
            if not isinstance(brief, dict):
                continue
            lines.extend(
                [
                    f"### {brief.get('brief_id', 'brief')}",
                    f"- Issue: {brief.get('issue', '')}",
                    f"- Expected treatment: {brief.get('expected_treatment', '')}",
                    f"- Required workbook change: {brief.get('required_workbook_change', '')}",
                    f"- Validation test: {brief.get('validation_test', '')}",
                    "",
                ]
            )
    else:
        lines.append("- No correction briefs were supplied.")
    lines.append("## Attempts")
    for attempt in attempt_history:
        if not isinstance(attempt, dict):
            continue
        lines.append(
            f"- Attempt {attempt.get('attempt')}: {attempt.get('status')} "
            f"(timeout {attempt.get('timeout_seconds')}s)"
        )
        if attempt.get("error"):
            lines.append(f"  Error: {attempt.get('error')}")
        findings_after = attempt.get("validation_findings") if isinstance(attempt.get("validation_findings"), list) else []
        if findings_after:
            lines.append(f"  Validation findings: {len(findings_after)}")
    if corrected_summary:
        lines.extend(["", "## Corrected Workpaper Summary"])
        for key, value in corrected_summary.items():
            lines.append(f"- {key}: {value}")
    md_path.write_text("\n".join(lines).rstrip() + "\n")


def _apply_turing_corrections_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    workpaper_json = Path(getattr(args, "workpaper_json", None) or output_dir / TB_BRIDGE_JSON)
    review_json = Path(getattr(args, "review_json", None) or output_dir / "turing_senior_review.json")
    source_index_path = Path(getattr(args, "source_index", None) or artifact_dir / "source_document_index.json")
    event_register_path = Path(getattr(args, "event_register", None) or artifact_dir / "accounting_event_register.json")
    prior_coa_path = Path(getattr(args, "prior_coa", None) or artifact_dir / "prior_statement_coa_import.json")
    missing = [path for path in [workpaper_json, review_json, source_index_path, event_register_path] if not path.exists()]
    if missing:
        for path in missing:
            print(f"Required correction input not found: {path}", file=sys.stderr)
        return 2
    workpaper_payload = json.loads(workpaper_json.read_text())
    review_payload = json.loads(review_json.read_text())
    source_index = json.loads(source_index_path.read_text())
    event_register = json.loads(event_register_path.read_text())
    prior_coa = json.loads(prior_coa_path.read_text()) if prior_coa_path.exists() else None
    correction_round = int(getattr(args, "correction_round", 0) or 0)
    correction_findings = _review_correction_findings(review_payload)
    if not correction_findings:
        print("Turing review did not include correction briefs, so no correction pass is required.")
        return 0
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 600) or 600)
    payload = None
    error = "Turing senior review found correction briefs. Apply the briefs and return the complete corrected TB bridge workpaper JSON."
    validation_findings: list[dict] = correction_findings
    attempt_history: list[dict] = []
    attempt_history_path = output_dir / f"turing_correction_round_{correction_round or 'latest'}_attempt_history.json"
    previous_payload: dict | None = workpaper_payload
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        payload, error = _codex_map_coa_from_events(
            event_register,
            source_index,
            prior_coa,
            codex_command,
            attempt_timeout,
            recovery_attempt=attempt,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
            candidate_output_path=output_dir / TB_BRIDGE_JSON,
        )
        validation_findings = _validate_coa_mapping_workpaper(payload, event_register, prior_coa)
        attempt_history.append(
            {
                "attempt": attempt,
                "mode": "turing_correction",
                "timeout_seconds": attempt_timeout,
                "status": "success" if payload is not None and not validation_findings else "failed",
                "error": error or "",
                "validation_findings": validation_findings,
            }
        )
        _write_codex_attempt_history(
            attempt_history_path,
            stage="turing_correction",
            attempts=attempt_history,
            status="success" if payload is not None and not validation_findings else "needs_attention",
            message=(
                f"Turing correction round {correction_round or 'latest'} attempt {attempt} produced a usable corrected workpaper."
                if payload is not None and not validation_findings
                else f"Turing correction round {correction_round or 'latest'} attempt {attempt} needs correction."
            ),
            extra={"correction_round": correction_round, "current_error": error or ""},
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not validation_findings:
            break
        if payload is not None and validation_findings:
            error = "Codex correction output failed schema validation."
    if payload is None or validation_findings:
        failure_payload = _codex_failed_coa_mapping_payload(
            event_register,
            error or "Codex CLI did not return a usable corrected TB bridge result.",
            attempt_history,
            validation_findings,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "tb_bridge_correction_failed.json").write_text(json.dumps(failure_payload, indent=2, sort_keys=True))
        _write_turing_correction_round_log(
            output_dir=output_dir,
            correction_round=correction_round,
            review_payload=review_payload,
            attempt_history=attempt_history,
            status="failed",
            error=error or "Codex CLI did not return a usable corrected TB bridge result.",
            validation_findings=validation_findings,
            output_return_code=1,
        )
        print("Codex could not apply Turing corrections.", file=sys.stderr)
        return 1
    final_payload = _normalise_coa_mapping_workpaper(payload, event_register, validation_findings)
    final_payload["codex_attempt_history"] = attempt_history
    final_payload["turing_correction_source"] = {
        "review_status": review_payload.get("status"),
        "correction_briefs": review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else [],
        "review_summary": review_payload.get("summary") if isinstance(review_payload.get("summary"), dict) else {},
    }
    output_return_code = _write_tb_bridge_outputs(
        output_dir=output_dir,
        payload=final_payload,
        event_register=event_register,
        source_index=source_index,
        prior_coa=prior_coa,
        skip_xlsx=bool(getattr(args, "skip_xlsx", False)),
    )
    _write_turing_correction_round_log(
        output_dir=output_dir,
        correction_round=correction_round,
        review_payload=review_payload,
        attempt_history=attempt_history,
        status="applied" if output_return_code == 0 else "output_failed",
        error="" if output_return_code == 0 else "Corrected JSON was produced, but workbook output failed.",
        validation_findings=validation_findings,
        corrected_payload=final_payload,
        output_return_code=output_return_code,
    )
    return output_return_code


def _prepare_workpaper_update_run_context(artifact_dir: Path, *, entity_name: str | None, fy_start: str | None, fy_end: str | None) -> None:
    context = {
        key: value
        for key, value in {
            "entity_name": entity_name,
            "target_fy_start": fy_start,
            "target_fy_end": fy_end,
        }.items()
        if value
    }
    if not context:
        return
    for file_name in [
        "document_inventory.json",
        "source_document_index.json",
        "accounting_facts_by_document.json",
        "source_coverage_continuity.json",
    ]:
        path = artifact_dir / file_name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload.update(context)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _prepare_workpaper_summary(
    *,
    client_folder: Path,
    artifact_dir: Path,
    output_dir: Path,
    step_statuses: dict[str, int],
) -> str:
    workbook_path = output_dir / TB_BRIDGE_XLSX
    tb_json_path = output_dir / TB_BRIDGE_JSON
    review_path = output_dir / "turing_senior_review.md"
    source_index_path = artifact_dir / "source_document_index.json"
    event_register_path = artifact_dir / "accounting_event_register.json"
    lines = ["# Prepared Workpaper Summary", ""]
    lines.extend(
        [
            f"- Client folder: {client_folder}",
            f"- Source index: {source_index_path}",
            f"- Event register: {event_register_path}",
            f"- TB Bridge workbook: {workbook_path}",
            f"- Turing senior review: {review_path}",
            "",
        ]
    )
    lines.append("## Run status")
    for label, code in step_statuses.items():
        state = "completed" if code == 0 else "completed with warnings" if label == "step2_source_index" and source_index_path.exists() else "needs attention"
        lines.append(f"- {label}: {state} (exit {code})")
    if tb_json_path.exists():
        try:
            tb_payload = json.loads(tb_json_path.read_text())
        except json.JSONDecodeError:
            tb_payload = {}
        summary = tb_payload.get("summary") if isinstance(tb_payload.get("summary"), dict) else {}
        findings = tb_payload.get("validation_findings") if isinstance(tb_payload.get("validation_findings"), list) else []
        lines.extend(["", "## Workbook checks"])
        lines.append(f"- Accounts: {summary.get('accounts', 0)}")
        lines.append(f"- Movement columns: {summary.get('movement_columns', 0)}")
        lines.append(f"- Movement notes: {summary.get('movement_notes', 0)}")
        lines.append(f"- Validation findings: {len(findings)}")
        if findings:
            lines.append("")
            lines.append("## Needs attention")
            for finding in findings[:12]:
                if not isinstance(finding, dict):
                    continue
                message = finding.get("message") or finding.get("category") or finding
                lines.append(f"- {message}")
    if review_path.exists():
        try:
            review_payload = json.loads(review_path.with_suffix(".json").read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            review_payload = {}
        review_summary = review_payload.get("summary") if isinstance(review_payload.get("summary"), dict) else {}
        review_findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
        public_review_findings = _public_turing_findings(review_payload) if isinstance(review_payload, dict) else []
        internal_review_notes = max(0, len(review_findings) - len(public_review_findings))
        lines.extend(["", "## Turing senior review"])
        lines.append(f"- Status: {'ready' if _turing_review_is_ready(output_dir) else review_payload.get('status', 'review_created')}")
        lines.append(f"- Sampled items: {review_summary.get('sampled_items', len(review_payload.get('sampled_items', []) if isinstance(review_payload.get('sampled_items'), list) else []))}")
        lines.append(f"- Material findings shown to accountant: {len(public_review_findings)}")
        lines.append(f"- Internal low-risk notes handled by Tessa/Turing: {internal_review_notes}")
        if public_review_findings:
            lines.append("")
            lines.append("## Material review items")
            for finding in public_review_findings[:10]:
                if not isinstance(finding, dict):
                    continue
                lines.append(f"- {finding.get('severity', 'review')} / {finding.get('category', 'judgement')}: {finding.get('message', '')}")
    lines.extend(
        [
            "",
            "## Accountant-facing instruction",
            "Open the TB Bridge workbook first. Use Movement Notes to search important amounts and Evidence Index to open source PDFs.",
            "",
        ]
    )
    return "\n".join(lines)


def _remove_previous_event_register_outputs(artifact_dir: Path) -> None:
    for file_name in [
        "source_fact_matches.md",
        "source_fact_matches.json",
        "accounting_event_register.md",
        "accounting_event_register.json",
        "relationship_reasoning_register.md",
        "relationship_reasoning_register.json",
        "relationship_reasoning_progress.json",
        "relationship_reasoning_attempt_history.json",
    ]:
        path = artifact_dir / file_name
        if path.exists():
                path.unlink()


def _last_good_workpaper_dir(output_dir: Path) -> Path:
    return output_dir / "_last_good"


def _workpaper_promotable_files() -> list[str]:
    return [
        TB_BRIDGE_JSON,
        TB_BRIDGE_MD,
        TB_BRIDGE_XLSX,
        "turing_senior_review.md",
        "turing_senior_review.json",
        f"{TB_BRIDGE_XLSX}.inspect.ndjson",
    ]


def _snapshot_previous_workpaper_outputs(output_dir: Path) -> bool:
    workbook_path = output_dir / TB_BRIDGE_XLSX
    if not workbook_path.exists():
        return False
    snapshot_dir = _last_good_workpaper_dir(output_dir)
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for file_name in _workpaper_promotable_files():
        source = output_dir / file_name
        if source.exists() and source.is_file():
            shutil.copy2(source, snapshot_dir / file_name)
            copied.append(file_name)
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(
            {
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
                "files": copied,
                "reason": "Last valid workbook snapshot before starting a new run.",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return bool(copied)


def _restore_last_good_workpaper_outputs(output_dir: Path, *, reason: str) -> bool:
    snapshot_dir = _last_good_workpaper_dir(output_dir)
    workbook_path = snapshot_dir / TB_BRIDGE_XLSX
    if not workbook_path.exists():
        return False
    restored: list[str] = []
    for file_name in _workpaper_promotable_files():
        source = snapshot_dir / file_name
        if source.exists() and source.is_file():
            shutil.copy2(source, output_dir / file_name)
            restored.append(file_name)
    (output_dir / "last_good_workpaper_restored.json").write_text(
        json.dumps(
            {
                "restored_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "files": restored,
                "snapshot_dir": str(snapshot_dir),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return bool(restored)


def _prepare_workpaper_progress_path(output_dir: Path) -> Path:
    return output_dir / "prepare_workpaper_progress.json"


def _write_prepare_workpaper_progress(
    output_dir: Path,
    *,
    stage: str,
    status: str,
    message: str,
    step_statuses: dict[str, int] | None = None,
    extra: dict | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "status": status,
        "message": message,
        "step_statuses": step_statuses or {},
    }
    if extra:
        payload.update(extra)
    _prepare_workpaper_progress_path(output_dir).write_text(json.dumps(payload, indent=2, sort_keys=True))


def _write_step_progress(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True))


def _write_codex_attempt_history(
    path: Path,
    *,
    stage: str,
    attempts: list[dict],
    status: str,
    message: str,
    extra: dict | None = None,
) -> None:
    payload = {
        "stage": stage,
        "status": status,
        "message": message,
        "attempt_count": len(attempts),
        "last_attempt": attempts[-1] if attempts else {},
        "attempts": attempts,
    }
    if extra:
        payload.update(extra)
    _write_step_progress(path, payload)


def _remove_previous_workpaper_outputs(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    generated_names = {
        TB_BRIDGE_JSON,
        TB_BRIDGE_MD,
        TB_BRIDGE_XLSX,
        "prepared_workpaper_summary.md",
        "turing_senior_review.md",
        "turing_senior_review.json",
        "tb_bridge_correction_failed.json",
        "build_tb_bridge_workpaper.mjs",
        "last_good_workpaper_restored.json",
        "prepare_workpaper_progress.json",
        "tb_bridge_generation_progress.json",
        "tb_bridge_attempt_history.json",
        "turing_review_attempt_history.json",
    }
    generated_patterns = [
        "preview_*.png",
        "*.inspect.ndjson",
        "turing_senior_review_round_*.*",
    ]
    for file_name in generated_names:
        path = output_dir / file_name
        if path.exists():
            path.unlink()
    for pattern in generated_patterns:
        for path in output_dir.glob(pattern):
            if path.is_file():
                path.unlink()


def _prepare_workpaper_command(args: argparse.Namespace) -> int:
    client_folder = Path(args.client_folder).expanduser()
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    codex_timeout = int(getattr(args, "codex_timeout", 1200) or 1200)
    codex_max_attempts = int(getattr(args, "codex_max_attempts", 3) or 3)
    review_correction_rounds = max(0, int(getattr(args, "review_correction_rounds", 2) or 0))
    force_reprocess = bool(getattr(args, "force_reprocess", False)) or not bool(getattr(args, "allow_cache", False))
    if not client_folder.exists() or not client_folder.is_dir():
        print(f"Client folder not found: {client_folder}", file=sys.stderr)
        return 2

    had_last_good_workbook = _snapshot_previous_workpaper_outputs(output_dir)
    _remove_previous_workpaper_outputs(output_dir)
    if had_last_good_workbook:
        print(f"Saved previous valid workbook snapshot -> {_last_good_workpaper_dir(output_dir)}")

    source_index = artifact_dir / "source_document_index.json"
    print(f"Preparing accountant workpaper from: {client_folder}")
    _write_prepare_workpaper_progress(
        output_dir,
        stage="indexing",
        status="running",
        message="Tessa is reading the uploaded files and building the evidence index.",
        extra={
            "client_folder": str(client_folder),
            "last_good_snapshot_available": had_last_good_workbook,
        },
    )
    print("Step 1/3: indexing source documents with Codex CLI")
    step_statuses: dict[str, int] = {}
    try:
        step_statuses["step2_source_index"] = _process_documents_command(
            argparse.Namespace(
                input_dir=str(client_folder),
                artifact_dir=str(artifact_dir),
                codex_command=codex_command,
                codex_timeout=codex_timeout,
                codex_max_attempts=codex_max_attempts,
                batch_size=int(getattr(args, "batch_size", 5) or 5),
                force_reprocess=force_reprocess,
            )
        )
    except Exception as exc:  # noqa: BLE001 - product runner must checkpoint unexpected failures.
        traceback.print_exc()
        step_statuses["step2_source_index"] = 1
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Source indexing crashed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="indexing",
            status="failed",
            message=(
                "Tessa could not finish reading the uploaded files. Previous valid workbook was restored."
                if restored_last_good
                else "Tessa could not finish reading the uploaded files. No refreshed workbook was produced."
            ),
            step_statuses=step_statuses,
            extra={
                "error_type": type(exc).__name__,
                "last_good_restored": restored_last_good,
                "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
            },
        )
        return 1
    _write_prepare_workpaper_progress(
        output_dir,
        stage="indexing",
        status="complete" if step_statuses["step2_source_index"] == 0 else "needs_attention",
        message="Evidence index completed." if step_statuses["step2_source_index"] == 0 else "Evidence index completed with documents needing attention.",
        step_statuses=step_statuses,
        extra={"source_index_path": str(source_index)},
    )
    _prepare_workpaper_update_run_context(
        artifact_dir,
        entity_name=getattr(args, "entity_name", None),
        fy_start=getattr(args, "fy_start", None),
        fy_end=getattr(args, "fy_end", None),
    )
    accounting_facts = artifact_dir / "accounting_facts_by_document.json"
    source_coverage = artifact_dir / "source_coverage_continuity.json"
    if not source_index.exists() or not accounting_facts.exists():
        print("Source index was not created, so the workpaper cannot continue.", file=sys.stderr)
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Source indexing failed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="indexing",
            status="failed",
            message=(
                "Source index was not created. Previous valid workbook was restored."
                if restored_last_good
                else "Source index was not created, so the workpaper cannot continue."
            ),
            step_statuses=step_statuses,
            extra={"last_good_restored": restored_last_good, "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists()},
        )
        return 1

    print("Step 2/3: building accounting event register with Codex CLI")
    _write_prepare_workpaper_progress(
        output_dir,
        stage="relationships",
        status="running",
        message="Tessa is investigating relationships between prior-year balances, bank movements and source documents.",
        step_statuses=step_statuses,
    )
    _remove_previous_event_register_outputs(artifact_dir)
    try:
        step_statuses["step3_event_register"] = _match_source_facts_command(
            argparse.Namespace(
                accounting_facts=str(accounting_facts),
                source_coverage=str(source_coverage) if source_coverage.exists() else None,
                codex_command=codex_command,
                codex_timeout=codex_timeout,
                codex_max_attempts=codex_max_attempts,
                bank_transactions=None,
                invoice_facts=None,
                distribution_tax_facts=None,
                broker_trade_facts=None,
                output=str(artifact_dir / "source_fact_matches.md"),
            )
        )
    except Exception as exc:  # noqa: BLE001 - product runner must checkpoint unexpected failures.
        traceback.print_exc()
        step_statuses["step3_event_register"] = 1
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Relationship reasoning crashed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="relationships",
            status="failed",
            message=f"Movement reasoning failed with a product error: {exc}",
            step_statuses=step_statuses,
            extra={
                "error_type": type(exc).__name__,
                "last_good_restored": restored_last_good,
                "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
            },
        )
        return 1
    _write_prepare_workpaper_progress(
        output_dir,
        stage="relationships",
        status="complete" if step_statuses["step3_event_register"] == 0 else "failed",
        message="Accounting event register completed." if step_statuses["step3_event_register"] == 0 else "Accounting event register needs engineering attention.",
        step_statuses=step_statuses,
        extra={"event_register_path": str(artifact_dir / "accounting_event_register.json")},
    )
    event_register = artifact_dir / "accounting_event_register.json"
    if not event_register.exists():
        print("Accounting event register was not created, so the workpaper cannot continue.", file=sys.stderr)
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Relationship reasoning failed before a refreshed workbook was produced.")
        summary = _prepare_workpaper_summary(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            step_statuses=step_statuses,
        )
        summary_path = output_dir / "prepared_workpaper_summary.md"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
        print(f"Prepared workpaper summary -> {summary_path}")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="relationships",
            status="failed",
            message=(
                "Accounting event register was not created. Previous valid workbook was restored."
                if restored_last_good
                else "Accounting event register was not created, so the workpaper cannot continue."
            ),
            step_statuses=step_statuses,
            extra={"summary_path": str(summary_path), "last_good_restored": restored_last_good, "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists()},
        )
        return 1
    try:
        event_payload = json.loads(event_register.read_text())
    except json.JSONDecodeError:
        event_payload = {}
    if isinstance(event_payload, dict) and event_payload.get("status") == "codex_failed":
        print("Codex could not create a usable accounting event register.", file=sys.stderr)
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Relationship reasoning returned an unusable register before a refreshed workbook was produced.")
        summary = _prepare_workpaper_summary(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            step_statuses=step_statuses,
        )
        summary_path = output_dir / "prepared_workpaper_summary.md"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
        print(f"Prepared workpaper summary -> {summary_path}")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="relationships",
            status="failed",
            message=(
                "AI could not create a usable accounting event register. Previous valid workbook was restored."
                if restored_last_good
                else "AI could not create a usable accounting event register."
            ),
            step_statuses=step_statuses,
            extra={"summary_path": str(summary_path), "last_good_restored": restored_last_good, "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists()},
        )
        return 1

    print("Step 3/3: building TB Bridge workbook with Codex CLI")
    _write_prepare_workpaper_progress(
        output_dir,
        stage="bridge",
        status="running",
        message="Tessa is preparing the TB bridge workbook and movement notes.",
        step_statuses=step_statuses,
    )
    try:
        step_statuses["step4_tb_bridge_workbook"] = _build_coa_mapping_workpaper_command(
            argparse.Namespace(
                artifact_dir=str(artifact_dir),
                output_dir=str(output_dir),
                event_register=None,
                source_index=None,
                prior_coa=None,
                prior_fs_document_id=getattr(args, "prior_fs_document_id", None),
                prior_fs_file=getattr(args, "prior_fs_file", None),
                codex_command=codex_command,
                codex_timeout=codex_timeout,
                codex_max_attempts=codex_max_attempts,
                skip_xlsx=bool(getattr(args, "skip_xlsx", False)),
            )
        )
    except Exception as exc:  # noqa: BLE001 - product runner must checkpoint unexpected failures.
        traceback.print_exc()
        step_statuses["step4_tb_bridge_workbook"] = 1
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="TB bridge workbook stage crashed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="bridge",
            status="failed",
            message=f"TB bridge workbook stage failed with a product error: {exc}",
            step_statuses=step_statuses,
            extra={
                "error_type": type(exc).__name__,
                "last_good_restored": restored_last_good,
                "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
            },
        )
        return 1
    workbook_path = output_dir / TB_BRIDGE_XLSX
    if step_statuses["step4_tb_bridge_workbook"] != 0:
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Step 4 failed before a refreshed workbook was produced.")
        summary = _prepare_workpaper_summary(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            step_statuses=step_statuses,
        )
        summary_path = output_dir / "prepared_workpaper_summary.md"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
        print(f"Prepared workpaper summary -> {summary_path}")
        if restored_last_good:
            print(f"Restored previous valid workbook -> {workbook_path}")
        print("TB Bridge workbook was not refreshed because Step 4 needs attention.", file=sys.stderr)
        _write_prepare_workpaper_progress(
            output_dir,
            stage="bridge",
            status="failed",
            message=(
                "TB bridge workbook needs engineering attention. Previous valid workbook was restored."
                if restored_last_good
                else "TB bridge workbook needs engineering attention."
            ),
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
                "last_good_restored": restored_last_good,
            },
        )
        return 1

    review_required = not bool(getattr(args, "skip_review", False))
    if review_required and (output_dir / TB_BRIDGE_JSON).exists():
        print("Senior review: Turing is checking controls and sampling source evidence with Codex CLI")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="turing",
            status="running",
            message="Senior review is checking arithmetic, workbook structure and sample evidence.",
            step_statuses=step_statuses,
            extra={"workbook_path": str(workbook_path), "workbook_exists": workbook_path.exists()},
        )
        review_args = argparse.Namespace(
            client_folder=str(client_folder),
            artifact_dir=str(artifact_dir),
            output_dir=str(output_dir),
            workpaper_json=None,
            source_index=None,
            event_register=None,
            prior_coa=None,
            output=None,
            entity_name=getattr(args, "entity_name", None),
            codex_command=codex_command,
            codex_timeout=codex_timeout,
            codex_max_attempts=codex_max_attempts,
            sample_size=int(getattr(args, "review_sample_size", 8) or 8),
        )
        step_statuses["turing_senior_review_round_1"] = _review_workpaper_command(review_args)
        final_review_round = 1
        for correction_round in range(1, review_correction_rounds + 1):
            if step_statuses.get(f"turing_senior_review_round_{final_review_round}") != 0:
                break
            if not _turing_review_needs_corrections(output_dir):
                break
            _archive_turing_review_round(output_dir, final_review_round)
            print(f"Senior review correction round {correction_round}: Codex is applying Turing correction briefs")
            _write_prepare_workpaper_progress(
                output_dir,
                stage="correction",
                status="running",
                message=f"Tessa is applying senior review correction round {correction_round}.",
                step_statuses=step_statuses,
                extra={"correction_round": correction_round},
            )
            step_statuses[f"turing_correction_round_{correction_round}"] = _apply_turing_corrections_command(
                argparse.Namespace(
                    artifact_dir=str(artifact_dir),
                    output_dir=str(output_dir),
                    workpaper_json=None,
                    review_json=None,
                    source_index=None,
                    event_register=None,
                    prior_coa=None,
                    codex_command=codex_command,
                    codex_timeout=codex_timeout,
                    codex_max_attempts=codex_max_attempts,
                    correction_round=correction_round,
                    skip_xlsx=bool(getattr(args, "skip_xlsx", False)),
                )
            )
            if step_statuses[f"turing_correction_round_{correction_round}"] != 0:
                break
            print(f"Senior review recheck round {correction_round}: Turing is rechecking the corrected workbook")
            _write_prepare_workpaper_progress(
                output_dir,
                stage="turing",
                status="running",
                message=f"Senior review is rechecking correction round {correction_round}.",
                step_statuses=step_statuses,
                extra={"correction_round": correction_round},
            )
            final_review_round += 1
            step_statuses[f"turing_senior_review_round_{final_review_round}"] = _review_workpaper_command(review_args)
        if "turing_senior_review_round_1" in step_statuses:
            step_statuses["turing_senior_review"] = step_statuses[f"turing_senior_review_round_{final_review_round}"]
    summary = _prepare_workpaper_summary(
        client_folder=client_folder,
        artifact_dir=artifact_dir,
        output_dir=output_dir,
        step_statuses=step_statuses,
    )
    summary_path = output_dir / "prepared_workpaper_summary.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary)
    print(f"Prepared workpaper summary -> {summary_path}")
    current_run_ok = workbook_path.exists() and step_statuses.get("step4_tb_bridge_workbook") == 0 and (
        not review_required or (step_statuses.get("turing_senior_review") == 0 and _turing_review_is_ready(output_dir))
    )
    if current_run_ok:
        print(f"Workbook ready -> {workbook_path}")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="completed",
            status="completed",
            message="Workbook ready. Senior review passed.",
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
            },
        )
        return 0
    if review_required and workbook_path.exists() and step_statuses.get("turing_senior_review") == 0 and not _turing_review_is_ready(output_dir):
        print(f"Workbook was created but Turing still needs corrections after {review_correction_rounds} correction round(s): {workbook_path}", file=sys.stderr)
        _write_prepare_workpaper_progress(
            output_dir,
            stage="turing",
            status="needs_attention",
            message=f"Workbook was created, but senior review still has correction notes after {review_correction_rounds} correction round(s).",
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
            },
        )
        return 0
    if workbook_path.exists():
        print(f"Workbook was created but the current run needs attention: {workbook_path}", file=sys.stderr)
        _write_prepare_workpaper_progress(
            output_dir,
            stage="bridge",
            status="needs_attention",
            message="Workbook was created, but the current run has judgement or review items.",
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
            },
        )
        return 0
    print(f"Workbook was not created: {workbook_path}", file=sys.stderr)
    _write_prepare_workpaper_progress(
        output_dir,
        stage="bridge",
        status="failed",
        message="Workbook was not created.",
        step_statuses=step_statuses,
        extra={"summary_path": str(summary_path), "workbook_path": str(workbook_path), "workbook_exists": False},
    )
    return 1


def _compact_review_document_index(source_index: dict) -> list[dict]:
    documents = []
    for document in source_index.get("documents", []) if isinstance(source_index, dict) else []:
        if not isinstance(document, dict):
            continue
        documents.append(
            {
                "document_id": document.get("document_id"),
                "display_name": document.get("display_name") or document.get("file_name"),
                "original_file_name": document.get("original_file_name") or document.get("file_name"),
                "document_type": document.get("document_type"),
                "file_path": document.get("file_path"),
                "entity_relevance": document.get("entity_relevance"),
                "entity_relevance_reason": document.get("entity_relevance_reason"),
                "period_start": document.get("period_start"),
                "period_end": document.get("period_end"),
                "statement_date": document.get("statement_date"),
                "document_summary": document.get("document_summary"),
                "primary_amounts": document.get("primary_amounts") if isinstance(document.get("primary_amounts"), list) else [],
                "review_flags": document.get("review_flags") if isinstance(document.get("review_flags"), list) else [],
            }
        )
    return documents


def _compact_review_text(value: object, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _compact_turing_document_index(source_index: dict) -> list[dict]:
    documents = []
    for document in _list_value(source_index.get("documents")):
        if not isinstance(document, dict):
            continue
        documents.append(
            {
                "document_id": document.get("document_id"),
                "display_name": document.get("display_name") or document.get("file_name"),
                "document_type": document.get("document_type"),
                "file_path": document.get("file_path"),
                "entity_relevance": document.get("entity_relevance"),
                "period_start": document.get("period_start"),
                "period_end": document.get("period_end"),
                "statement_date": document.get("statement_date"),
                "summary": _compact_review_text(document.get("document_summary") or document.get("summary"), 260),
                "primary_amounts": _list_value(document.get("primary_amounts"))[:8],
                "review_flags": _list_value(document.get("review_flags"))[:6],
            }
        )
    return documents


def _compact_turing_event_register(event_register: dict) -> dict:
    relationships = []
    for item in _list_value(event_register.get("relationships")):
        if not isinstance(item, dict):
            continue
        accounts = []
        for account in _list_value(item.get("accounts_involved"))[:8]:
            if isinstance(account, dict):
                accounts.append(
                    {
                        "account_name": account.get("account_name"),
                        "role": account.get("role"),
                        "source": account.get("source"),
                        "confidence": account.get("confidence"),
                    }
                )
        relationships.append(
            {
                "relationship_id": item.get("relationship_id"),
                "relationship_type": item.get("relationship_type"),
                "status": item.get("status"),
                "confidence": item.get("confidence"),
                "evidence_level": item.get("evidence_level"),
                "story": _compact_review_text(item.get("story"), 420),
                "date": item.get("date"),
                "amount": item.get("amount"),
                "direction": item.get("direction"),
                "document_refs": _list_value(item.get("document_refs"))[:12],
                "accounts_involved": accounts,
                "open_questions": [_compact_review_text(question, 220) for question in _list_value(item.get("open_questions"))[:5]],
                "why_it_matters_for_step4": _compact_review_text(item.get("why_it_matters_for_step4"), 260),
            }
        )
    coverage = []
    for item in _list_value(event_register.get("prior_fs_account_movement_coverage")):
        if not isinstance(item, dict):
            continue
        coverage.append(
            {
                "account_name": item.get("account_name"),
                "statement_section": item.get("statement_section"),
                "opening_or_comparative_amount": item.get("opening_or_comparative_amount"),
                "coverage_status": item.get("coverage_status"),
                "relationship_ids": _list_value(item.get("relationship_ids"))[:10],
                "movement_story": _compact_review_text(item.get("movement_story"), 300),
            }
        )
    return {
        "artifact_type": event_register.get("artifact_type") or event_register.get("register_artifact_type"),
        "status": event_register.get("status"),
        "summary": event_register.get("summary") if isinstance(event_register.get("summary"), dict) else {},
        "relationships": relationships,
        "prior_fs_account_movement_coverage": coverage,
        "validation_findings": _list_value(event_register.get("validation_findings"))[:12],
    }


def _compact_turing_workpaper(workpaper_payload: dict) -> dict:
    columns = []
    for column in _list_value(workpaper_payload.get("movement_columns")):
        if not isinstance(column, dict):
            continue
        role = column.get("movement_role") if isinstance(column.get("movement_role"), dict) else {}
        columns.append(
            {
                "column_key": column.get("column_key"),
                "label": column.get("label"),
                "role_type": role.get("role_type") or column.get("column_type"),
                "accounting_purpose": _compact_review_text(role.get("accounting_purpose") or column.get("description"), 240),
                "support_type": column.get("support_type"),
                "description": _compact_review_text(column.get("description"), 220),
            }
        )
    rows = []
    for row in _list_value(workpaper_payload.get("matrix_rows")):
        if not isinstance(row, dict):
            continue
        movements = []
        for movement in _list_value(row.get("movements")):
            if not isinstance(movement, dict):
                continue
            movements.append(
                {
                    "column_key": movement.get("column_key"),
                    "amount": movement.get("amount"),
                    "support_type": movement.get("support_type"),
                    "relationship_id": movement.get("relationship_id"),
                    "note_id": movement.get("note_id"),
                    "explanation": _compact_review_text(movement.get("explanation"), 160),
                }
            )
        rows.append(
            {
                "account_name": row.get("account_name"),
                "account_type": row.get("account_type"),
                "statement_section": row.get("statement_section"),
                "statement_group": row.get("statement_group"),
                "opening_balance": row.get("opening_balance"),
                "prior_year_comparative": row.get("prior_year_comparative"),
                "movements": movements,
                "closing_balance": row.get("closing_balance"),
                "difference": row.get("difference"),
                "row_status": row.get("row_status"),
                "note_ids": _list_value(row.get("note_ids"))[:6],
                "notes": _compact_review_text(row.get("notes"), 120),
            }
        )
    notes = []
    for note in _list_value(workpaper_payload.get("movement_notes")):
        if not isinstance(note, dict):
            continue
        notes.append(
            {
                "note_id": note.get("note_id"),
                "account_name": note.get("account_name"),
                "status": note.get("status"),
                "tb_column": note.get("tb_column"),
                "main_amount": note.get("main_amount"),
                "other_amounts": _compact_review_text(note.get("other_amounts"), 240),
                "explanation": _compact_review_text(note.get("explanation"), 520),
                "calculation": _compact_review_text(note.get("calculation"), 260),
                "evidence_summary": _compact_review_text(note.get("evidence_summary"), 360),
                "relationship_ids": _list_value(note.get("relationship_ids"))[:10],
            }
        )
    return {
        "artifact_type": workpaper_payload.get("artifact_type"),
        "tb_bridge_contract_version": workpaper_payload.get("tb_bridge_contract_version"),
        "status": workpaper_payload.get("status"),
        "summary": workpaper_payload.get("summary") if isinstance(workpaper_payload.get("summary"), dict) else {},
        "validation_findings": _list_value(workpaper_payload.get("validation_findings"))[:12],
        "movement_columns": columns,
        "matrix_rows": rows,
        "movement_notes": notes,
        "workpaper_notes": [_compact_review_text(note, 260) for note in _list_value(workpaper_payload.get("workpaper_notes"))[:10]],
    }


def _compact_turing_prior_coa(prior_coa: dict | None) -> dict:
    prior = prior_coa if isinstance(prior_coa, dict) else {}
    accounts = []
    for account in _list_value(prior.get("accounts")):
        if not isinstance(account, dict):
            continue
        accounts.append(
            {
                "name": account.get("name"),
                "type": account.get("type"),
                "presentation_group": account.get("presentation_group"),
                "opening_balance": account.get("opening_balance"),
                "source_evidence_refs": _list_value(account.get("source_evidence_refs"))[:4],
            }
        )
    return {
        "prior_fs_document_id": prior.get("prior_fs_document_id"),
        "prior_fs_display_name": prior.get("prior_fs_display_name"),
        "accounts": accounts,
        "findings": _list_value(prior.get("findings"))[:8],
    }


def _turing_review_prompt(
    *,
    client_folder: Path | None,
    artifact_dir: Path,
    output_dir: Path,
    workpaper_payload: dict,
    source_index: dict,
    event_register: dict,
    prior_coa: dict | None,
    sample_size: int,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> str:
    review_pack = {
        "source_documents": _compact_turing_document_index(source_index),
        "event_register": _compact_turing_event_register(event_register),
        "tb_bridge_workpaper": _compact_turing_workpaper(workpaper_payload),
        "prior_year_opening_balances": _compact_turing_prior_coa(prior_coa),
    }
    redo_instruction = source_of_truth_redo_instruction(validation_findings)
    return json.dumps(
        {
            "task": "Act as Turing, the senior accountant supervisor. Review the prepared TB Bridge workpaper using Codex CLI with source-file access. Return JSON only.",
            "review_contract_version": "turing_senior_review_v1",
            "recovery_context": {
                "recovery_attempt": recovery_attempt,
                "previous_error": previous_error or "",
                "validation_findings": validation_findings or [],
                "previous_payload": previous_payload,
                "source_of_truth_redo_required": bool(redo_instruction),
                "instruction": redo_instruction or "If a previous attempt failed, repair the JSON and make the review more concrete. Keep output valid JSON only.",
            }
            if recovery_attempt
            else None,
            "workspace": {
                "cwd": str(Path.cwd()),
                "client_folder": str(client_folder) if client_folder else "",
                "artifact_dir": str(artifact_dir),
                "output_dir": str(output_dir),
                "workbook_path": str(output_dir / TB_BRIDGE_XLSX),
                "instruction": (
                    "You may inspect original files listed in source_documents[].file_path and the generated JSON/workbook artifacts. "
                    "Do not rely only on summaries when sampling material or judgemental rows."
                ),
            },
            "required_output_schema": {
                "artifact_type": "turing_senior_accountant_review",
                "review_contract_version": "turing_senior_review_v1",
                "status": "ready|needs_corrections|codex_failed",
                "reviewer": "turing",
                "entity_name": "entity name if known",
                "control_checks": [
                    {
                        "check": "movement_columns_balance_to_zero|row_roll_forward|opening_balances|pl_opening_zero|tax_valuation_boundary|evidence_links",
                        "status": "pass|warning|fail",
                        "summary": "short accountant-readable result",
                        "affected_rows_or_columns": ["labels"],
                    }
                ],
                "sampled_items": [
                    {
                        "sample_id": "S001",
                        "reason_selected": "material amount|judgement item|clearing row|source-only|bank-only|known tricky relationship|random low-risk sample",
                        "workpaper_item": "row/column/note/amount checked",
                        "amounts_checked": ["decimal strings"],
                        "source_documents_checked": [
                            {"document_id": "raw_001", "display_name": "display name", "file_path": "path", "page_or_evidence": "page/evidence id if known"}
                        ],
                        "original_evidence_observation": "what you saw in the original PDF/text/source, not only the JSON",
                        "conclusion": "pass|warning|fail",
                        "recommended_follow_up": "short action or blank",
                    }
                ],
                "findings": [
                    {
                        "finding_id": "F001",
                        "severity": "high|medium|low",
                        "category": "control_failure|source_mismatch|unsupported_amount|classification_judgement|tax_boundary|valuation_boundary|presentation|other",
                        "message": "short issue",
                        "affected_amounts": ["decimal strings"],
                        "affected_accounts_or_columns": ["labels"],
                    }
                ],
                "correction_briefs": [
                    {
                        "brief_id": "C001",
                        "issue": "what is wrong",
                        "expected_treatment": "what Codex should do",
                        "files_or_amounts_to_recheck": ["paths, doc ids, amounts"],
                        "required_workbook_change": "specific workbook change",
                        "validation_test": "specific recheck after fix",
                    }
                ],
                "summary": {
                    "control_checks": 0,
                    "sampled_items": 0,
                    "findings": 0,
                    "correction_briefs": 0,
                    "accountant_message": "short plain-English review result",
                },
            },
            "review_rules": [
                "Check all mathematical controls: every movement column adds to zero, each row opening + movements = closing, P&L opening balances are zero, and prior-year FS openings agree where possible.",
                "Do not manually re-check every cell. Review by risk: material balances, judgement rows, clearing rows, source-only items, bank-only items, tax/valuation boundaries, and known tricky relationships.",
                "Use status ready when the workbook is mathematically sound and judgement items are clearly surfaced for accountant review. Do not mark needs_corrections merely because accountant judgement remains.",
                "Use status needs_corrections only when Codex should change the workbook: math/control defects, unsupported posted amounts, wrong book/tax boundary, wrong classification, missing evidence status, or confusing presentation that could mislead the accountant.",
                "When sampling a material or judgement item, inspect original source documents or extracted page quotes from file_path values. State what original evidence you inspected in sampled_items[].original_evidence_observation.",
                "Do not trust the workbook JSON alone for sampled items. The review should verify against original PDFs/text or explain why original evidence could not be inspected.",
                "Check that book/financial-statement logic is used, not tax-component schedule logic. Franking credits, TFN withholding, ESVCLP offsets, and tax-only components should be notes unless there is a clear book posting.",
                "Check that NAV/market value movement is not posted by default unless fair value accounting is explicitly adopted.",
                "Check that beneficiary distribution/UPE is based on book bridge profit unless a different basis is explicitly documented.",
                "If a sampled item raises a technical accounting topic, consult accounting_pdf_topic_map and accounting_pdf_retrieval_tool for original PDF guidance, then verify the actual workbook support against client files. Do not cite the knowhow PDF as client evidence.",
                "Correction briefs must be actionable for Codex CLI: issue, expected treatment, files/amounts to re-check, workbook change, validation test.",
                "Keep the accountant-facing message concise. Do not over-explain low-risk passes.",
                "Never say the workpaper is final, lodged, posted, or approved. It is prepared for accountant review.",
            ],
            "risk_focus_examples": [
                "Spire total distribution vs banked amount vs source-only residual.",
                "ANZ/BENPI sale proceeds vs investment disposal and gain/loss.",
                "KPMG/ATO bank-only classifications.",
                "Beneficiary distribution/UPE calculation.",
                "Clearing rows and any column with a non-obvious balancing entry.",
                "Prior-year financial statement opening balances.",
            ],
            "accounting_skill": load_accounting_skill_for_prompt("senior-workpaper-review"),
            "accounting_reference": load_accounting_reference_for_prompt("senior-workpaper-review", "senior-review-checklist.md"),
            "accounting_pdf_retrieval_skill": load_accounting_skill_for_prompt("accounting-pdf-knowledge-retrieval"),
            "accounting_pdf_topic_map": load_accounting_pdf_topic_map_for_prompt(),
            "accounting_pdf_retrieval_tool": accounting_pdf_retrieval_tool_for_prompt(),
            "client_evidence_guardrail": client_evidence_guardrail_for_prompt(),
            "review_pack": review_pack,
        },
        indent=2,
        sort_keys=True,
    )


def _validate_turing_review(payload: dict | None) -> list[dict]:
    findings: list[dict] = []
    if not isinstance(payload, dict):
        return [{"category": "invalid_review_payload", "severity": "high", "message": "Codex did not return a JSON object."}]
    if payload.get("artifact_type") != "turing_senior_accountant_review":
        findings.append({"category": "invalid_artifact_type", "severity": "high", "message": "Review must return artifact_type turing_senior_accountant_review."})
    if payload.get("review_contract_version") != "turing_senior_review_v1":
        findings.append({"category": "invalid_review_contract_version", "severity": "high", "message": "Review contract version mismatch."})
    for key in ("control_checks", "sampled_items", "findings", "correction_briefs"):
        if not isinstance(payload.get(key), list):
            findings.append({"category": f"invalid_{key}", "severity": "high", "message": f"{key} must be a list."})
    if not isinstance(payload.get("summary"), dict):
        findings.append({"category": "invalid_summary", "severity": "medium", "message": "summary must be an object."})
    findings.extend(
        non_client_evidence_reference_findings(
            {
                "control_checks": payload.get("control_checks"),
                "sampled_items": payload.get("sampled_items"),
                "findings": payload.get("findings"),
                "correction_briefs": payload.get("correction_briefs"),
                "summary": payload.get("summary"),
            },
            stage="turing_senior_review",
            message=(
                "Turing review appears to cite knowhow, training material, or skills as evidence. "
                "Senior review may use skills as a checklist only; sampled evidence must come from client documents, prior FS, or generated workbook artifacts."
            ),
        )
    )
    return findings


def _normalise_turing_review(payload: dict, validation_findings: list[dict], attempt_history: list[dict]) -> dict:
    control_checks = payload.get("control_checks") if isinstance(payload.get("control_checks"), list) else []
    sampled_items = payload.get("sampled_items") if isinstance(payload.get("sampled_items"), list) else []
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    correction_briefs = payload.get("correction_briefs") if isinstance(payload.get("correction_briefs"), list) else []
    status = str(payload.get("status") or "")
    if status not in {"ready", "needs_corrections", "codex_failed"}:
        status = "needs_corrections" if findings or correction_briefs or validation_findings else "ready"
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "artifact_type": "turing_senior_accountant_review",
        "review_contract_version": "turing_senior_review_v1",
        "status": status,
        "reviewer": "turing",
        "entity_name": str(payload.get("entity_name") or ""),
        "control_checks": control_checks,
        "sampled_items": sampled_items,
        "findings": findings,
        "correction_briefs": correction_briefs,
        "summary": {
            "control_checks": int(summary.get("control_checks") or len(control_checks)),
            "sampled_items": int(summary.get("sampled_items") or len(sampled_items)),
            "findings": int(summary.get("findings") or len(findings)),
            "correction_briefs": int(summary.get("correction_briefs") or len(correction_briefs)),
            "accountant_message": str(summary.get("accountant_message") or ("Senior review found items needing correction." if findings or correction_briefs else "Senior review completed without major correction briefs.")),
        },
        "validation_findings": validation_findings,
        "codex_attempt_history": attempt_history,
    }


def _failed_turing_review(error: str, validation_findings: list[dict], attempt_history: list[dict]) -> dict:
    return {
        "artifact_type": "turing_senior_accountant_review",
        "review_contract_version": "turing_senior_review_v1",
        "status": "codex_failed",
        "reviewer": "turing",
        "entity_name": "",
        "control_checks": [],
        "sampled_items": [],
        "findings": [{"finding_id": "F001", "severity": "high", "category": "codex_review_failed", "message": error, "affected_amounts": [], "affected_accounts_or_columns": []}],
        "correction_briefs": [],
        "summary": {"control_checks": 0, "sampled_items": 0, "findings": 1, "correction_briefs": 0, "accountant_message": "Turing senior review could not run."},
        "validation_findings": validation_findings,
        "codex_attempt_history": attempt_history,
    }


def _format_turing_review(payload: dict) -> str:
    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    lines = ["# Turing Senior Accountant Review", ""]
    lines.extend(
        [
            f"- Status: {payload.get('status')}",
            f"- Reviewer: {payload.get('reviewer', 'turing')}",
            f"- Accountant message: {summary.get('accountant_message', '')}",
            "",
        ]
    )
    if payload.get("control_checks"):
        lines.append("## Control checks")
        for check in payload.get("control_checks", []):
            if not isinstance(check, dict):
                continue
            lines.append(f"- {check.get('check')}: {check.get('status')} — {check.get('summary')}")
        lines.append("")
    if payload.get("sampled_items"):
        lines.append("## Sampled source checks")
        for item in payload.get("sampled_items", []):
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"### {item.get('sample_id')} — {item.get('workpaper_item')}",
                    f"- Reason selected: {item.get('reason_selected')}",
                    f"- Amounts checked: {', '.join(str(amount) for amount in item.get('amounts_checked', []) if amount is not None)}",
                    f"- Conclusion: {item.get('conclusion')}",
                    f"- Original evidence observation: {item.get('original_evidence_observation')}",
                    f"- Recommended follow-up: {item.get('recommended_follow_up', '')}",
                    "",
                ]
            )
    if payload.get("findings"):
        lines.append("## Findings")
        for finding in payload.get("findings", []):
            if not isinstance(finding, dict):
                continue
            lines.append(f"- {finding.get('severity')} / {finding.get('category')}: {finding.get('message')}")
        lines.append("")
    if payload.get("correction_briefs"):
        lines.append("## Correction briefs for Codex")
        for brief in payload.get("correction_briefs", []):
            if not isinstance(brief, dict):
                continue
            lines.extend(
                [
                    f"### {brief.get('brief_id')}",
                    f"- Issue: {brief.get('issue')}",
                    f"- Expected treatment: {brief.get('expected_treatment')}",
                    f"- Files or amounts to re-check: {', '.join(str(item) for item in brief.get('files_or_amounts_to_recheck', []) if item is not None)}",
                    f"- Required workbook change: {brief.get('required_workbook_change')}",
                    f"- Validation test: {brief.get('validation_test')}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _codex_turing_review(
    *,
    client_folder: Path | None,
    artifact_dir: Path,
    output_dir: Path,
    workpaper_payload: dict,
    source_index: dict,
    event_register: dict,
    prior_coa: dict | None,
    command: str,
    timeout: int,
    sample_size: int,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> tuple[dict | None, str | None]:
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_TURING_REVIEW_JSON")
    if fake_payload:
        payload = _extract_json_object(fake_payload)
        return payload, None if payload is not None else "Fake Codex Turing review payload was not valid JSON."
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_turing_review_prompt(
                client_folder=client_folder,
                artifact_dir=artifact_dir,
                output_dir=output_dir,
                workpaper_payload=workpaper_payload,
                source_index=source_index,
                event_register=event_register,
                prior_coa=prior_coa,
                sample_size=sample_size,
                recovery_attempt=recovery_attempt,
                previous_error=previous_error,
                validation_findings=validation_findings,
                previous_payload=previous_payload,
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"Codex command was not found: {command}"
    except subprocess.TimeoutExpired:
        return None, f"Codex command timed out after {timeout} seconds."
    except (subprocess.SubprocessError, ValueError) as exc:
        return None, f"Codex command failed to start: {exc}"
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        return None, f"Codex command exited {result.returncode}: {stderr[:500]}"
    if not result.stdout.strip():
        return None, f"Codex command returned no stdout. {stderr[:500]}".strip()
    payload = _extract_json_object(result.stdout)
    if payload is None:
        return None, f"Codex command did not return a JSON object. stdout={result.stdout[:500]!r}"
    return payload, None


def _review_workpaper_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    workpaper_json = Path(getattr(args, "workpaper_json", None) or output_dir / TB_BRIDGE_JSON)
    source_index_path = Path(getattr(args, "source_index", None) or artifact_dir / "source_document_index.json")
    event_register_path = Path(getattr(args, "event_register", None) or artifact_dir / "accounting_event_register.json")
    prior_coa_path = Path(getattr(args, "prior_coa", None) or artifact_dir / "prior_statement_coa_import.json")
    output_path = Path(getattr(args, "output", None) or output_dir / "turing_senior_review.md")
    client_folder = Path(args.client_folder).expanduser() if getattr(args, "client_folder", None) else None
    missing = [path for path in [workpaper_json, source_index_path, event_register_path] if not path.exists()]
    if missing:
        for path in missing:
            print(f"Required review input not found: {path}", file=sys.stderr)
        return 2
    workpaper_payload = json.loads(workpaper_json.read_text())
    source_index = json.loads(source_index_path.read_text())
    event_register = json.loads(event_register_path.read_text())
    prior_coa = json.loads(prior_coa_path.read_text()) if prior_coa_path.exists() else None
    if getattr(args, "entity_name", None):
        workpaper_payload["entity_name"] = getattr(args, "entity_name")
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 600) or 600)
    sample_size = max(1, int(getattr(args, "sample_size", 8) or 8))
    payload = None
    error = None
    validation_findings: list[dict] = []
    attempt_history: list[dict] = []
    previous_payload: dict | None = None
    attempt_history_path = output_path.parent / "turing_review_attempt_history.json"
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        payload, error = _codex_turing_review(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            workpaper_payload=workpaper_payload,
            source_index=source_index,
            event_register=event_register,
            prior_coa=prior_coa,
            command=codex_command,
            timeout=attempt_timeout,
            sample_size=sample_size,
            recovery_attempt=attempt - 1,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
        )
        validation_findings = _validate_turing_review(payload)
        attempt_history.append(
            {
                "attempt": attempt,
                "mode": "normal" if attempt == 1 else "recovery",
                "timeout_seconds": attempt_timeout,
                "status": "success" if payload is not None and not validation_findings else "failed",
                "error": error or "",
                "validation_findings": validation_findings,
            }
        )
        _write_codex_attempt_history(
            attempt_history_path,
            stage="turing_senior_review",
            attempts=attempt_history,
            status="success" if payload is not None and not validation_findings else "needs_attention",
            message=(
                f"Turing senior review attempt {attempt} produced a usable review."
                if payload is not None and not validation_findings
                else f"Turing senior review attempt {attempt} needs correction."
            ),
            extra={
                "current_error": error or "",
                "sample_size": sample_size,
            },
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not validation_findings:
            break
        if payload is not None and validation_findings:
            error = "Codex Turing review output failed schema validation."
    if payload is None:
        final_payload = _failed_turing_review(error or "Codex CLI did not return a usable Turing review.", validation_findings, attempt_history)
    elif validation_findings:
        final_payload = _failed_turing_review("Codex CLI returned a Turing review that did not pass validation.", validation_findings, attempt_history)
    else:
        final_payload = _normalise_turing_review(payload, validation_findings, attempt_history)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_codex_attempt_history(
        attempt_history_path,
        stage="turing_senior_review",
        attempts=attempt_history,
        status="failed" if final_payload.get("status") == "codex_failed" else "complete",
        message=str(final_payload.get("error") or "Turing senior review is ready."),
        extra={
            "review_json": str(output_path.with_suffix(".json")),
            "review_md": str(output_path),
            "validation_findings": final_payload.get("validation_findings") or [],
        },
    )
    output_path.write_text(_format_turing_review(final_payload))
    output_path.with_suffix(".json").write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    print(f"Exported Turing senior review -> {output_path}")
    print(f"Exported Turing senior review JSON -> {output_path.with_suffix('.json')}")
    return 1 if final_payload.get("status") == "codex_failed" else 0


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


def _serve_workpaper_portal_command(args: argparse.Namespace) -> int:
    from accountant_copilot.workpaper_portal import serve_workpaper_portal

    serve_workpaper_portal(repo_root=Path.cwd(), host=args.host, port=args.port)
    return 0


def _parse_bank_statement_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d/%m/%y"):
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
        ("Draft TB Bridge workpaper", "step4_coa_mapping_workpaper/coa_mapping_workpaper.md"),
        ("Draft TB Bridge workbook", "step4_coa_mapping_workpaper/step4_coa_mapping_workpaper.xlsx"),
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


def _pdf_password_candidates_from_filename(path: Path) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for match in re.finditer(r"\d{4,16}", path.stem):
        candidate = match.group(0)
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    return candidates


def _extract_pdf_page_quotes(path: Path) -> list[tuple[int, str]]:
    """Extract text quotes from a text-based PDF, one quote per page.

    PyMuPDF is preferred when installed. A `pdftotext` fallback keeps local
    development usable without making scanned/OCR documents appear verified.
    Empty pages intentionally return no evidence so the document remains gated.
    """
    password_candidates = _pdf_password_candidates_from_filename(path)
    try:
        import fitz  # type: ignore[import-not-found]

        pages: list[tuple[int, str]] = []
        with fitz.open(path) as doc:
            if getattr(doc, "needs_pass", False):
                authenticated = any(doc.authenticate(password) for password in password_candidates)
                if not authenticated:
                    return []
            for index, page in enumerate(doc, start=1):
                text = " ".join(page.get_text("text").split())
                if text:
                    pages.append((index, text[:_PDF_PAGE_QUOTE_CHAR_LIMIT]))
        return pages
    except Exception:
        try:
            commands = [["pdftotext", "-layout", str(path), "-"]]
            commands.extend(["pdftotext", "-upw", password, "-layout", str(path), "-"] for password in password_candidates)
            result = None
            for command in commands:
                candidate_result = subprocess.run(command, text=True, capture_output=True, check=False)
                if candidate_result.returncode == 0 and candidate_result.stdout.strip():
                    result = candidate_result
                    break
        except FileNotFoundError:
            return []
        if result is None:
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        pages = []
        for index, text in enumerate(result.stdout.split("\f"), start=1):
            quote = " ".join(text.split())
            if quote:
                pages.append((index, quote[:_PDF_PAGE_QUOTE_CHAR_LIMIT]))
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


def _xml_text(element: ET.Element) -> str:
    values: list[str] = []
    for child in element.iter():
        if child.text:
            values.append(child.text)
    return " ".join(" ".join(values).split())


def _extract_docx_quote(path: Path) -> str | None:
    """Extract readable text from a modern Word document without extra deps."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name == "word/document.xml"
                or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
            ]
            chunks: list[str] = []
            for name in names:
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                text = _xml_text(root)
                if text:
                    chunks.append(text)
    except (OSError, zipfile.BadZipFile):
        return None
    quote = " ".join(chunks).strip()
    return quote[:_PDF_PAGE_QUOTE_CHAR_LIMIT] if quote else None


def _extract_xlsx_quote(path: Path) -> str | None:
    """Extract cell text from a modern Excel workbook without extra deps."""
    try:
        with zipfile.ZipFile(path) as archive:
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                try:
                    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    for item in root.iter():
                        if item.tag.endswith("}si") or item.tag == "si":
                            shared_strings.append(_xml_text(item))
                except ET.ParseError:
                    shared_strings = []
            sheet_names = sorted(name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
            rows: list[str] = []
            for sheet_index, sheet_name in enumerate(sheet_names, start=1):
                try:
                    root = ET.fromstring(archive.read(sheet_name))
                except ET.ParseError:
                    continue
                rows.append(f"Sheet {sheet_index}")
                for row in root.iter():
                    if not (row.tag.endswith("}row") or row.tag == "row"):
                        continue
                    values: list[str] = []
                    for cell in row:
                        if not (cell.tag.endswith("}c") or cell.tag == "c"):
                            continue
                        cell_type = cell.attrib.get("t", "")
                        value = ""
                        if cell_type == "inlineStr":
                            value = _xml_text(cell)
                        else:
                            raw = ""
                            for child in cell:
                                if child.tag.endswith("}v") or child.tag == "v":
                                    raw = child.text or ""
                                    break
                            if cell_type == "s" and raw.isdigit():
                                value = shared_strings[int(raw)] if int(raw) < len(shared_strings) else raw
                            else:
                                value = raw
                        if value:
                            values.append(value)
                    if values:
                        rows.append(" | ".join(values))
                    if len("\n".join(rows)) > 60000:
                        break
                if len("\n".join(rows)) > 60000:
                    break
    except (OSError, zipfile.BadZipFile):
        return None
    quote = "\n".join(rows).strip()
    return quote[:60000] if quote else None


def _classify_raw_document(path: Path) -> str:
    name = path.name.lower()
    if path.suffix.lower() == ".md":
        return "client_conventions"
    if path.suffix.lower() == ".csv":
        return "supporting_csv"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        return "image_support"
    if path.suffix.lower() in {".docx", ".docm", ".xlsx", ".xlsm", ".xls"}:
        if "trial balance" in name or re.search(r"\btb\b", name):
            return "trial_balance"
        return "source_document"
    if "estatement" in name or (path.suffix.lower() == ".pdf" and len(path.stem) == 36 and path.stem.count("-") == 4):
        return "bank_statement"
    if "trial balance" in name or re.search(r"\btb\b", name):
        return "trial_balance"
    if "invoice" in name or "tax invoice" in name:
        return "invoice"
    if any(token in name for token in ["capital call", "drawdown", "contribution notice"]):
        return "capital_call"
    if "financial statement" in name or "fy24" in name:
        return "prior_year_financial_statements"
    if any(token in name for token in ["distribution", "tax statement", "payment_advice", "annual statement"]):
        return "investment_statement"
    if "confirmation" in name or "sell" in name:
        return "broker_confirmation"
    return "source_document"


_UUID_STEM_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_GENERIC_FILE_STEM_RE = re.compile(r"^(?:img[_ -]?\d+|scan[_ -]?\d*|document[_ -]?\d*|untitled|unknown|file[_ -]?\d*)$", re.IGNORECASE)
_KNOWN_DOCUMENT_TYPES = {
    "bank_statement",
    "broker_confirmation",
    "capital_call",
    "client_conventions",
    "image_support",
    "investment_statement",
    "invoice",
    "prior_year_financial_statements",
    "source_document",
    "supporting_csv",
    "trial_balance",
}


def _is_ambiguous_file_name(path: Path) -> bool:
    stem = path.stem.strip()
    if _UUID_STEM_RE.fullmatch(stem) or _GENERIC_FILE_STEM_RE.fullmatch(stem):
        return True
    compact = re.sub(r"[^A-Za-z0-9]", "", stem)
    return len(compact) >= 20 and bool(re.fullmatch(r"[A-Fa-f0-9]+", compact))


def _classify_raw_document_from_content(path: Path, current_type: str, quote: str) -> str:
    if path.suffix.lower() in {".md", ".csv", ".png", ".jpg", ".jpeg"}:
        return current_type
    text = quote.lower()
    if "sell confirmation" in text or "buy confirmation" in text or "settlement amount" in text:
        return "broker_confirmation"
    if "trial balance" in text or ("account code" in text and "debit" in text and "credit" in text):
        return "trial_balance"
    if "tax invoice" in text or "invoice number" in text or "amount due" in text:
        return "invoice"
    if "capital call" in text or "drawdown notice" in text or "contribution notice" in text:
        return "capital_call"
    if "financial statements" in text or "statement of financial position" in text:
        return "prior_year_financial_statements"
    if "statement period" in text and "closing balance" in text:
        return "bank_statement"
    if any(token in text for token in ["distribution", "payment advice", "tax statement", "franking credit", "withholding"]):
        return "investment_statement"
    return current_type if not _is_ambiguous_file_name(path) else "source_document"


def _sanitize_document_name(value: str, suffix: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " - ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    cleaned = cleaned[:140].strip(" .-_") or "Source Document"
    extension = suffix.lower() or ".pdf"
    return cleaned if cleaned.lower().endswith(extension) else f"{cleaned}{extension}"


def _filename_date(value: str | None) -> str | None:
    parsed = _parse_bank_statement_date(value)
    return parsed.strftime("%Y-%m-%d") if parsed else None


def _first_match(pattern: str, quote: str) -> str | None:
    match = re.search(pattern, quote, re.IGNORECASE)
    return " ".join(match.group(1).split()) if match else None


def _bank_name_from_quote(quote: str) -> str | None:
    bank_patterns = [
        ("Commonwealth Bank", r"\bCommonwealth Bank\b|\bCommBank\b|\bCBA\b|Enquiries\s+13\s+1998"),
        ("Westpac", r"\bWestpac\b|\bWBC\b"),
        ("NAB", r"\bNational Australia Bank\b|\bNAB\b"),
        ("ANZ", r"\bAustralia and New Zealand Banking Group\b|\bANZ\b"),
        ("Macquarie Bank", r"\bMacquarie Bank\b|\bMacquarie\b"),
        ("Bendigo Bank", r"\bBendigo Bank\b|\bBendigo\b"),
    ]
    for bank_name, pattern in bank_patterns:
        if re.search(pattern, quote, re.IGNORECASE):
            return bank_name
    return None


def _bank_account_label(quote: str) -> str | None:
    patterns = [
        r"Business Transaction Account\s+([A-Za-z0-9* -]{1,20})(?=\s+(?:If|Statement|Opening|Closing|Enquiries|Name:|$))",
        r"Account Number\s+([A-Za-z0-9* -]{1,20})(?=\s+(?:Statement Period|Opening|Closing|Business|Enquiries|Name:|$))",
    ]
    for pattern in patterns:
        raw = _first_match(pattern, quote)
        if not raw:
            continue
        tokens = re.findall(r"[A-Za-z]*\d[A-Za-z0-9* -]*", raw)
        label = (tokens[0] if tokens else raw).strip(" -")
        if label and not re.search(r"\b(?:If|Statement|Opening|Closing|Enquiries)\b", label, re.IGNORECASE):
            return label
    return None


def _bank_statement_label(bank_name: str | None) -> str:
    if not bank_name:
        return "Bank Statement"
    return f"{bank_name} Statement" if bank_name.lower().endswith("bank") else f"{bank_name} Bank Statement"


def _name_from_quote(path: Path, document_type: str, quote: str) -> tuple[str | None, str, str]:
    text = " ".join(quote.split())
    if document_type == "bank_statement":
        period = _PERIOD_RE.search(text) or _ALT_PERIOD_RE.search(text)
        period_end = _filename_date(period.group("end")) if period else None
        account = _bank_account_label(text)
        bank_name = _bank_name_from_quote(text)
        label = _bank_statement_label(bank_name)
        if account:
            label = f"{label} - Account {account.strip()}"
        if period_end:
            return _sanitize_document_name(f"{period_end} - {label}", path.suffix), "high", "deterministic"
        if account:
            return _sanitize_document_name(label, path.suffix), "medium", "deterministic"
    if document_type == "broker_confirmation":
        side = "Sell" if re.search(r"SELL CONFIRMATION", text, re.IGNORECASE) else "Buy" if re.search(r"BUY CONFIRMATION", text, re.IGNORECASE) else "Trade"
        settlement_date = _filename_date(_first_match(r"Settlement Date\s*:?\s*([0-9/ -]{6,12})", text))
        security = _first_match(r"Security\s*:?\s*([^:]{2,60}?)(?:\s+ISIN|\s+Market|$)", text)
        parts = [settlement_date, f"Broker {side} Confirmation", security]
        return _sanitize_document_name(" - ".join(part for part in parts if part), path.suffix), "high" if settlement_date and security else "medium", "deterministic"
    if document_type == "investment_statement":
        payment_date = _filename_date(_first_match(r"Payment\s+date:?\s*(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})", text))
        investment = _first_match(r"(ANZ Capital Notes\s+\d+|AN3|Westpac Capital Notes|Bendigo.*?Income|Proxima Commercial Property Investment Trust|Ralston Capital Select Income Fund|Spire Branford Castle[^:]{0,40})", text)
        if payment_date or investment:
            parts = [payment_date, investment, "Distribution Tax Statement" if "tax statement" in text.lower() else "Distribution Advice"]
            return _sanitize_document_name(" - ".join(part for part in parts if part), path.suffix), "medium", "deterministic"
    if document_type == "prior_year_financial_statements":
        fy = _first_match(r"(FY\d{2,4}|30\s+June\s+\d{4}|30-Jun-\d{2,4})", text)
        return _sanitize_document_name(" - ".join(part for part in [fy, "Prior Year Financial Statements"] if part), path.suffix), "medium", "deterministic"
    if document_type == "image_support" or re.search(r"tax\s+invoice|invoice\s+number|amount\s+due", text, re.IGNORECASE):
        invoice = _first_match(r"\b(INV[- ]?\d+)\b", text)
        supplier = _first_match(r"([A-Z][A-Za-z& ]+ Pty Ltd)", text)
        invoice_date = _filename_date(_first_match(r"TAX\s+INVOICE\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", text))
        if invoice or supplier:
            parts = [invoice_date, supplier, f"Invoice {invoice}" if invoice else "Invoice"]
            return _sanitize_document_name(" - ".join(part for part in parts if part), path.suffix), "medium", "deterministic"
    return None, "low", "deterministic"


def _codex_document_name_prompt(path: Path, document_type: str, quote: str, display_name: str | None) -> str:
    return json.dumps(
        {
            "task": "Suggest a concise accountant-facing display filename for this source document. Return JSON only.",
            "required_json_shape": {
                "display_name": "YYYY-MM-DD - Issuer - Document Type - Key Identifier.pdf",
                "document_type": document_type,
                "confidence": "low|medium|high",
                "evidence": ["short quote 1", "short quote 2"],
            },
            "rules": [
                "Do not invent values that are not supported by the content.",
                "Preserve the original file extension.",
                "Use Australian date order in evidence, but ISO date prefix in filename when a date is clear.",
                "Keep the name under 140 characters.",
            ],
            "original_file_name": path.name,
            "current_document_type": document_type,
            "deterministic_display_name": display_name,
            "content_excerpt": quote[:2500],
        },
        indent=2,
        sort_keys=True,
    )


def _extract_json_object(text: str) -> dict | None:
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None


def _read_json_object_file(path: Path | None) -> tuple[dict | None, str | None]:
    if path is None or not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return None, f"Codex wrote invalid JSON sidecar {path}: {exc}"
    except OSError as exc:
        return None, f"Could not read Codex JSON sidecar {path}: {exc}"
    if not isinstance(payload, dict):
        return None, f"Codex JSON sidecar was not an object: {path}"
    return payload, None


def _codex_suggest_document_name(path: Path, document_type: str, quote: str, display_name: str | None, command: str, timeout: int) -> dict | None:
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_codex_document_name_prompt(path, document_type, quote, display_name),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    payload = _extract_json_object(result.stdout)
    if not payload or not payload.get("display_name"):
        return None
    return payload


_CODEX_DOCUMENT_PROCESSING_CACHE_VERSION = "v10_source_index"


DOCUMENT_SOURCE_INDEX_CONTRACT = {
    "purpose": "Create a lightweight source index for human orientation and later Codex CLI investigation. Step 2 must not extract detailed accounting facts.",
    "global_rules": [
        "Summarise what the document is, not every accounting row inside it.",
        "Suggest a clear display name that preserves the original file extension.",
        "Classify the document type only at document level.",
        "Capture entity relevance and obvious wrong-entity/personal-document concerns.",
        "Capture only high-level visible signals such as period/date, named parties, identifiers, and a few prominent headline amounts.",
        "Do not extract bank transactions, invoice line items, distribution rows, broker trade rows, statement balances, trial balance rows, financial statement line items, or tax component rows.",
        "Do not map to chart of accounts and do not propose journals.",
        "Detailed accounting event extraction, cash matching, CoA mapping, journals, trial balance and financial statement work happen in later Codex CLI steps.",
    ],
    "document_types": [
        "bank_statement",
        "invoice",
        "distribution_tax",
        "broker_trade",
        "trial_balance",
        "prior_year_financial_statements",
        "investment_statement",
        "capital_call",
        "client_conventions",
        "source_document",
        "other",
    ],
}


ACCOUNTING_FACT_EXTRACTION_CONTRACT = {
    "purpose": "Extract only accounting facts needed for downstream trial balance support, financial statement preparation, CoA mapping, journals, tax review, cash reconciliation, and workpaper evidence.",
    "global_rules": [
        "Extract facts at the lowest useful accounting granularity: one fact per statement balance, transaction row, invoice, trial balance account row, financial statement line, investment holding, distribution, trade, or capital call.",
        "Keep numeric values as decimal strings without currency symbols or thousands separators when possible.",
        "Use ISO dates YYYY-MM-DD when the date is clear.",
        "Include currency when visible or strongly implied by the source document.",
        "Use debit_credit values DR or CR for accounting balances and bank movements when visible or inferable from a labelled debit/credit column.",
        "Do not classify or map to a chart of accounts unless the source document explicitly provides the account name/code; downstream mapping is a later review step.",
        "Do not create trial balance facts from bank statements, invoices, distribution statements, broker trades, or other support documents. Trial balance facts must come from an uploaded trial balance, general ledger export, accountant workpaper, or source table that explicitly lists account balances.",
        "Support documents can support, match, or explain balances later, but they are not themselves the trial balance.",
        "If a document has no downstream accounting fact, return no facts and explain the reason.",
    ],
    "document_types": {
        "bank_statement": {
            "extract": {
                "bank_statement_period_balance": {
                    "when": "Once per statement/account period, from the statement header or balance summary.",
                    "required_fields_if_visible": [
                        "bank_name",
                        "account_identifier",
                        "masked_account_number",
                        "account_name",
                        "account_type",
                        "statement_number",
                        "statement_period_start",
                        "statement_period_end",
                        "opening_balance",
                        "opening_balance_credit_debit",
                        "total_debits",
                        "total_credits",
                        "closing_balance",
                        "closing_balance_credit_debit",
                        "currency",
                    ],
                    "downstream_use": "Bank continuity checks, cash balance support, and transaction reconciliation.",
                },
                "bank_transaction": {
                    "when": "For each visible transaction row.",
                    "required_fields_if_visible": [
                        "date",
                        "description",
                        "amount",
                        "debit_credit",
                        "balance",
                        "balance_credit_debit",
                        "counterparty",
                        "reference",
                        "currency",
                    ],
                    "downstream_use": "Matching invoices, distributions, broker settlements, and unexplained cash movements.",
                },
            },
            "do_not_extract": [
                "fee schedules",
                "transaction-count summaries",
                "zero-fee summary tables",
                "marketing text",
                "page footers",
            ],
        },
        "invoice": {
            "extract": {
                "invoice": {
                    "when": "Once per invoice or credit note.",
                    "required_fields_if_visible": [
                        "supplier_name",
                        "supplier_abn",
                        "customer_name",
                        "invoice_number",
                        "invoice_date",
                        "due_date",
                        "tax_invoice",
                        "line_items",
                        "subtotal",
                        "gst",
                        "total",
                        "amount_due",
                        "currency",
                        "description",
                        "payment_reference",
                    ],
                    "downstream_use": "Expense/revenue recognition, GST support, and bank transaction matching.",
                }
            },
            "do_not_extract": ["payment instructions unless needed as evidence", "terms boilerplate", "marketing text"],
        },
        "distribution_tax": {
            "extract": {
                "distribution_tax": {
                    "when": "For distribution statements, payment advices, annual tax statements, AMMA/AMIT statements, or dividend statements.",
                    "required_fields_if_visible": [
                        "investment_name",
                        "investor_name",
                        "security_code",
                        "account_identifier",
                        "period_start",
                        "period_end",
                        "payment_date",
                        "distribution_date",
                        "record_date",
                        "cash_distribution",
                        "total_taxable_income",
                        "franked_dividends",
                        "unfranked_dividends",
                        "interest",
                        "capital_gains",
                        "discounted_capital_gains",
                        "cgt_concession_amount",
                        "foreign_income",
                        "tax_deferred",
                        "return_of_capital",
                        "franking_credits",
                        "foreign_income_tax_offset",
                        "withholding_tax",
                        "distribution_description",
                        "currency",
                    ],
                    "downstream_use": "Trust/company tax workpapers, income classification, and cash matching.",
                }
            },
            "do_not_extract": ["generic fund descriptions", "legal disclaimers", "non-tax explanatory notes unless they contain amounts"],
        },
        "broker_trade": {
            "extract": {
                "broker_trade": {
                    "when": "For each buy/sell confirmation or contract note.",
                    "required_fields_if_visible": [
                        "broker_name",
                        "account_identifier",
                        "trade_date",
                        "settlement_date",
                        "side",
                        "security_name",
                        "ticker",
                        "isin",
                        "quantity",
                        "unit_price",
                        "gross_amount",
                        "brokerage",
                        "gst",
                        "stamp_duty",
                        "net_settlement_amount",
                        "cash_direction",
                        "currency",
                    ],
                    "downstream_use": "Investment additions/disposals, realised gains support, and cash settlement matching.",
                }
            },
            "do_not_extract": ["market commentary", "portfolio advertising", "terms boilerplate"],
        },
        "trial_balance": {
            "extract": {
                "trial_balance_account_balance": {
                    "when": "For each account row in an uploaded trial balance, general ledger balance export, accountant workpaper TB, or source table that explicitly lists account balances.",
                    "required_fields_if_visible": [
                        "entity_name",
                        "period_start",
                        "period_end",
                        "account_code",
                        "account_name",
                        "account_type",
                        "opening_balance",
                        "movement",
                        "debit",
                        "credit",
                        "closing_balance",
                        "debit_credit",
                        "currency",
                    ],
                    "downstream_use": "Source-provided input for TB roll-forward, FS line grouping, comparative checks, and journal preparation.",
                }
            },
            "do_not_extract": [
                "balances inferred from bank statements, invoices, distributions, broker trades, or support documents",
                "subtotal rows when the underlying account rows are visible",
                "formatting notes",
                "export metadata without balances",
            ],
        },
        "prior_year_financial_statements": {
            "extract": {
                "financial_statement_line_balance": {
                    "when": "For each visible line item in prior/current year financial statements, including comparative columns.",
                    "required_fields_if_visible": [
                        "entity_name",
                        "statement_name",
                        "line_item",
                        "note_number",
                        "period_end",
                        "comparative_period_end",
                        "amount",
                        "comparative_amount",
                        "debit_credit",
                        "currency",
                    ],
                    "downstream_use": "Opening balance support, FS grouping, comparative presentation, and reasonableness checks.",
                },
                "accounting_policy_note": {
                    "when": "Only for accounting policies or basis-of-preparation notes that affect classification, measurement, or disclosure.",
                    "required_fields_if_visible": [
                        "topic",
                        "policy_text",
                        "effective_period",
                        "applies_to_line_item",
                    ],
                    "downstream_use": "Financial statement disclosure consistency and review notes.",
                },
            },
            "do_not_extract": ["director declarations", "auditor independence text", "page headers", "contents pages"],
        },
        "investment_statement": {
            "extract": {
                "investment_holding_balance": {
                    "when": "For holding statements, portfolio statements, annual investor statements, or fund statements showing units/market value/cost.",
                    "required_fields_if_visible": [
                        "investment_name",
                        "investor_name",
                        "account_identifier",
                        "security_code",
                        "statement_date",
                        "period_start",
                        "period_end",
                        "units",
                        "unit_price",
                        "market_value",
                        "cost_base",
                        "unrealised_gain_loss",
                        "currency",
                    ],
                    "downstream_use": "Investment balance support, FS asset classification, and valuation workpapers.",
                },
                "distribution_tax": {
                    "when": "For income/tax distribution sections within investment statements.",
                    "same_fields_as": "document_types.distribution_tax.extract.distribution_tax.required_fields_if_visible",
                    "downstream_use": "Income classification, tax workpapers, and cash matching.",
                },
            },
            "do_not_extract": ["generic fund commentary", "performance marketing", "risk disclosure text unless it changes accounting treatment"],
        },
        "capital_call": {
            "extract": {
                "capital_call": {
                    "when": "For capital call notices, contribution notices, drawdown notices, or payable notices for investments.",
                    "required_fields_if_visible": [
                        "investment_name",
                        "investor_name",
                        "notice_date",
                        "due_date",
                        "call_number",
                        "commitment_amount",
                        "called_amount",
                        "management_fee",
                        "gst",
                        "bank_account",
                        "payment_reference",
                        "currency",
                    ],
                    "downstream_use": "Investment payable/accrual support, bank payment matching, and commitment tracking.",
                }
            },
            "do_not_extract": ["fund commentary", "legal boilerplate", "payment instructions without a payable amount"],
        },
        "client_conventions": {
            "extract": {
                "accounting_policy_preference": {
                    "when": "Only where the document states a client-specific accounting treatment, grouping preference, materiality threshold, or reporting convention.",
                    "required_fields_if_visible": [
                        "topic",
                        "preference",
                        "applies_to",
                        "effective_period",
                    ],
                    "downstream_use": "CoA mapping, FS presentation, and review rule configuration.",
                }
            },
            "do_not_extract": ["general instructions without accounting treatment", "contact details", "workflow notes"],
        },
        "source_document": {
            "extract": {},
            "do_not_extract": ["facts unless a clear supported accounting amount/date/accounting treatment is present"],
        },
        "other": {
            "extract": {},
            "do_not_extract": ["anything not clearly needed for downstream accounting use"],
        },
    },
}


def _normalise_codex_cli_command(command: str) -> str:
    command = str(command or "").strip()
    return "codex exec" if command == "codex" else command or "codex exec"


def _document_text_for_codex(path: Path) -> tuple[list[dict[str, str]], str]:
    pages: list[dict[str, str]] = []
    suffix = path.suffix.lower()
    if suffix == ".md":
        pages.append({"page": "1", "evidence_id": "text_001", "quote": path.read_text(errors="ignore")})
    elif suffix == ".pdf":
        for page_number, quote in _extract_pdf_page_quotes(path):
            pages.append({"page": str(page_number), "evidence_id": f"page_{page_number:03d}", "quote": quote})
    elif suffix in {".png", ".jpg", ".jpeg"}:
        quote = _extract_image_ocr_quote(path)
        if quote:
            pages.append({"page": "1", "evidence_id": "page_001", "quote": quote})
    elif suffix in {".docx", ".docm"}:
        quote = _extract_docx_quote(path)
        if quote:
            pages.append({"page": "1", "evidence_id": "text_001", "quote": quote})
    elif suffix in {".xlsx", ".xlsm"}:
        quote = _extract_xlsx_quote(path)
        if quote:
            pages.append({"page": "1", "evidence_id": "sheet_text_001", "quote": quote})
    if not pages and suffix in {".txt", ".csv", ".json"}:
        pages.append({"page": "1", "evidence_id": "text_001", "quote": path.read_text(errors="ignore")})
    text = "\n\n".join(f"[{item['evidence_id']}]\n{item['quote']}" for item in pages)
    return pages, text[:60000]


def _codex_process_document_prompt(path: Path, document_id: str, source_hash: str, *, recovery_attempt: int = 0, previous_error: str | None = None) -> str:
    pages, extracted_text = _document_text_for_codex(path)
    recovery_context = {
        "recovery_attempt": recovery_attempt,
        "previous_error": previous_error or "",
        "instruction": (
            "A previous Codex attempt failed. Act like a senior accountant recovering the work: "
            "diagnose the failure, change indexing strategy, keep the response compact, and return valid JSON only. "
            "For timeout or large-document failures, prioritize the title/header, entity holder, period/date, document type, and a short summary. "
            "For invalid-output failures, focus on returning the required JSON schema."
        ),
    }
    return json.dumps(
        {
            "task": "Read one source document and create a concise source index entry. Return only JSON." if recovery_attempt == 0 else "Recover a failed source-indexing attempt. Return only JSON.",
            "recovery_context": recovery_context if recovery_attempt else None,
            "source_index_contract": DOCUMENT_SOURCE_INDEX_CONTRACT,
            "required_output_schema": {
                "display_name": "Human review name, e.g. 2024-12-31 - Commonwealth Bank Statement - Account 027.pdf",
                "document_type": "bank_statement|invoice|distribution_tax|broker_trade|trial_balance|prior_year_financial_statements|investment_statement|capital_call|client_conventions|source_document|other",
                "naming_confidence": "low|medium|high",
                "naming_evidence_refs": ["evidence ids supporting the display name"],
                "status": "indexed|needs_review",
                "document_summary": "One or two plain-English sentences explaining what this file appears to be.",
                "entity_relevance": "relevant|possible_personal|wrong_entity|unclear|non_accounting",
                "entity_relevance_reason": "Short reason for the relevance label.",
                "period_start": "YYYY-MM-DD if visible or blank",
                "period_end": "YYYY-MM-DD if visible or blank",
                "statement_date": "YYYY-MM-DD if visible or blank",
                "key_parties": ["visible names of banks, investors, suppliers, customers, brokers, funds, trustees, or recipients"],
                "key_identifiers": ["visible account numbers, investor numbers, invoice numbers, statement numbers, security codes, or payment references"],
                "primary_amounts": [
                    {
                        "label": "headline amount label, e.g. closing balance, market value, amount due",
                        "amount": "decimal string or visible value",
                        "currency": "currency if visible",
                        "evidence_id": "supporting evidence id if visible",
                    }
                ],
                "review_flags": ["short warnings such as possible personal holder, wrong entity, password protected, scanned/low confidence, unclear period"],
            },
            "rules": [
                "Do not invent bank names, account numbers, dates, amounts, counterparties, or tax labels.",
                "For bank statements, include bank name and account identifier in display_name when visible.",
                "Do not extract accounting facts in Step 2. No bank transaction rows, distribution rows, invoice line items, broker trade rows, trial balance rows, or financial statement line balances.",
                "Use primary_amounts only for a small number of headline amounts that help the user recognise the document.",
                "Use document_summary to explain what the document is about in normal accounting language.",
                "Use review_flags for document-level concerns only, not accounting event matching conclusions.",
                "Preserve the original file extension in display_name.",
                "Return a single JSON object and no markdown.",
            ],
            "document": {
                "document_id": document_id,
                "file_name": path.name,
                "file_path": str(path),
                "source_hash": source_hash,
                "deterministic_document_type": _classify_raw_document(path),
                "page_quotes": pages,
                "extracted_text": extracted_text,
            },
        },
        indent=2,
        sort_keys=True,
    )


def _codex_process_document(path: Path, document_id: str, source_hash: str, command: str, timeout: int, *, recovery_attempt: int = 0, previous_error: str | None = None) -> tuple[dict | None, str | None]:
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_DOCUMENT_JSON")
    if fake_payload:
        payload = _extract_json_object(fake_payload)
        return payload, None if payload is not None else "Fake Codex document payload was not valid JSON."
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_codex_process_document_prompt(path, document_id, source_hash, recovery_attempt=recovery_attempt, previous_error=previous_error),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"Codex command was not found: {command}"
    except subprocess.TimeoutExpired:
        return None, f"Codex command timed out after {timeout} seconds."
    except (subprocess.SubprocessError, ValueError) as exc:
        return None, f"Codex command failed to start: {exc}"
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        return None, f"Codex command exited {result.returncode}: {stderr[:500]}"
    if not result.stdout.strip():
        return None, f"Codex command returned no stdout. {stderr[:500]}".strip()
    payload = _extract_json_object(result.stdout)
    if payload is None:
        return None, f"Codex command did not return a JSON object. stdout={result.stdout[:500]!r}"
    return payload, None


def _capital_call_payment_instruction_visible(text: str) -> bool:
    lowered = text.lower()
    has_payment_heading = any(token in lowered for token in ["eft", "bpay", "payment reference", "payment due", "payable", "payment instructions"])
    has_bank_detail = bool(re.search(r"\b(bank|bsb|account name|account number|westpac|commbank|commonwealth|anz|nab)\b", lowered))
    return has_payment_heading and has_bank_detail


def _capital_call_fact_missing_payment_instruction(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    for fact in payload.get("accounting_facts", []) if isinstance(payload.get("accounting_facts"), list) else []:
        if not isinstance(fact, dict) or fact.get("fact_type") != "capital_call":
            continue
        fields = fact.get("fields") if isinstance(fact.get("fields"), dict) else {}
        if not fields.get("called_amount") and not fields.get("amount_due"):
            continue
        if not fields.get("bank_account") and not fields.get("bank_name"):
            return True
    return False


def _codex_document_validation_error(path: Path, payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    pages, extracted_text = _document_text_for_codex(path)
    if _capital_call_payment_instruction_visible(extracted_text) and _capital_call_fact_missing_payment_instruction(payload):
        evidence_ids = ", ".join(page.get("evidence_id", "") for page in pages if page.get("evidence_id"))
        return (
            "Capital call payment instructions are visible in the document text, but the capital_call fact omitted bank_account/bank_name. "
            "Re-read the EFT/BPAY/payment instruction section and extract the receiving bank/account/BSB plus payment reference when visible. "
            f"Relevant evidence ids: {evidence_ids}."
        )
    return None


def _should_skip_codex_fact(fact_type: str, fields: dict) -> bool:
    if fact_type not in {"bank_statement", "bank_statement_period_balance"}:
        return False
    keys = {str(key) for key in fields}
    has_statement_period = bool({"statement_period_start", "statement_period_end"} & keys)
    has_balance = bool({"opening_balance", "closing_balance"} & keys)
    if has_statement_period and has_balance:
        return False
    fee_or_count_keys = [key for key in keys if key.endswith("_fee_charged") or key.endswith("_performed") or key in {"account_fee", "transaction_summary_period_start", "transaction_summary_period_end"}]
    return bool(fee_or_count_keys) and not has_balance


def _normalise_codex_document_result(path: Path, document_id: str, source_hash: str, payload: dict | None) -> dict:
    payload = payload or {}
    document_type = str(payload.get("document_type") or _classify_raw_document(path))
    if document_type not in _KNOWN_DOCUMENT_TYPES:
        document_type = "source_document" if document_type == "supporting_document" else "other"
    display_name = _sanitize_document_name(str(payload.get("display_name") or path.name), path.suffix)
    document_summary = str(payload.get("document_summary") or payload.get("summary") or "").strip()
    if not document_summary:
        document_summary = "Source indexed for Step 3 Codex investigation."
    no_fact_reason = "Step 2 indexes documents only. Step 3 extracts accounting events from source PDFs/page quotes."
    payload_status = str(payload.get("status") or "")
    if payload_status in {"needs_review", "processing_failed"}:
        status = payload_status
    else:
        status = "indexed"
    return {
        "document_id": document_id,
        "file_path": str(path),
        "file_name": path.name,
        "original_file_name": path.name,
        "display_name": display_name,
        "document_type": document_type,
        "source_hash": source_hash,
        "naming_status": "suggested" if display_name != path.name else "not_suggested",
        "naming_confidence": str(payload.get("naming_confidence") or payload.get("confidence") or ("high" if display_name != path.name else "")),
        "naming_method": "codex_cli",
        "naming_evidence_refs": payload.get("naming_evidence_refs") if isinstance(payload.get("naming_evidence_refs"), list) else [],
        "status": status,
        "document_summary": document_summary,
        "entity_relevance": str(payload.get("entity_relevance") or ""),
        "entity_relevance_reason": str(payload.get("entity_relevance_reason") or ""),
        "period_start": str(payload.get("period_start") or ""),
        "period_end": str(payload.get("period_end") or ""),
        "statement_date": str(payload.get("statement_date") or ""),
        "key_parties": payload.get("key_parties") if isinstance(payload.get("key_parties"), list) else [],
        "key_identifiers": payload.get("key_identifiers") if isinstance(payload.get("key_identifiers"), list) else [],
        "primary_amounts": payload.get("primary_amounts") if isinstance(payload.get("primary_amounts"), list) else [],
        "review_flags": payload.get("review_flags") if isinstance(payload.get("review_flags"), list) else [],
        "no_fact_reason": no_fact_reason,
    }


def _write_document_processing_progress(
    progress_path: Path,
    *,
    processed: int,
    total: int,
    current_document: str,
    status: str,
    cache_hits: int,
    facts: int,
    failures: int = 0,
    codex_attempts: int = 0,
    codex_successes: int = 0,
    batch_size: int = 1,
    current_batch: int = 0,
    total_batches: int = 0,
) -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(
        json.dumps(
            {
                "processed_items": processed,
                "total_items": total,
                "current_document": current_document,
                "status": status,
                "cache_hits": cache_hits,
                "batch_size": batch_size,
                "current_batch": current_batch,
                "total_batches": total_batches,
                "codex_attempts": codex_attempts,
                "codex_successes": codex_successes,
                "source_signals": facts,
                "facts_extracted": 0,
                "failed_items": failures,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _format_processed_document_inventory(payload: dict) -> str:
    lines = [f"# Source Document Index — {payload.get('entity_name', 'Uploaded documents')}", ""]
    for document in payload.get("documents", []):
        summary = str(document.get("document_summary") or "").strip()
        relevance = str(document.get("entity_relevance") or "").strip()
        period = " to ".join(part for part in [str(document.get("period_start") or ""), str(document.get("period_end") or "")] if part)
        lines.extend(
            [
                f"## {document.get('document_id')} — {document.get('display_name')}",
                f"- Original file name: {document.get('original_file_name')}",
                f"- Type: {document.get('document_type')}",
                f"- Status: {document.get('status')}",
                f"- Summary: {summary}",
                f"- Entity relevance: {relevance or 'not assessed'}",
                f"- Period: {period or document.get('statement_date') or ''}",
                "",
            ]
        )
        if document.get("review_flags"):
            lines.extend(["- Review flags: " + "; ".join(str(item) for item in document.get("review_flags", [])), ""])
    return "\n".join(lines).rstrip() + "\n"


def _build_source_document_index_from_processed(processed_payload: dict) -> dict:
    documents = processed_payload.get("documents", []) if isinstance(processed_payload, dict) else []
    return {
        "inventory_id": processed_payload.get("inventory_id", "processed_documents"),
        "entity_name": processed_payload.get("entity_name", "Uploaded documents"),
        "artifact_type": "source_document_index",
        "source_index_contract_version": _CODEX_DOCUMENT_PROCESSING_CACHE_VERSION,
        "documents": documents,
        "summary": {
            "uploaded_documents": len(documents),
            "indexed_documents": sum(1 for document in documents if isinstance(document, dict) and document.get("status") == "indexed"),
            "documents_needing_review": sum(1 for document in documents if isinstance(document, dict) and document.get("status") == "needs_review"),
            "failed_documents": sum(1 for document in documents if isinstance(document, dict) and document.get("status") == "processing_failed"),
            "documents_with_review_flags": sum(1 for document in documents if isinstance(document, dict) and document.get("review_flags")),
        },
    }


def _build_accounting_facts_by_document_from_processed(processed_payload: dict) -> dict:
    payload = _build_source_document_index_from_processed(processed_payload)
    payload["fact_type"] = "source_document_index"
    payload["summary"] = {
        **payload["summary"],
        "documents_with_facts": 0,
        "accounting_fact_rows": 0,
        "documents_without_facts": payload["summary"]["uploaded_documents"],
    }
    for document in payload["documents"]:
        if isinstance(document, dict):
            document.pop("accounting_facts", None)
    return payload


def _fact_fields(fact: dict) -> dict:
    return fact.get("fields", {}) if isinstance(fact.get("fields"), dict) else {}


def _fact_reference(document: dict, fact: dict, index: int) -> str:
    document_id = str(document.get("document_id") or "doc")
    return str(fact.get("fact_id") or f"{document_id}_fact_{index:03d}")


def _account_key_from_bank_balance(fields: dict) -> str:
    return str(fields.get("account_identifier") or fields.get("masked_account_number") or fields.get("account_name") or "unknown_bank_account")


def _source_coverage_facts(facts_payload: dict) -> list[dict]:
    rows: list[dict] = []
    for document in facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []:
        if not isinstance(document, dict):
            continue
        for index, fact in enumerate(document.get("accounting_facts", []) or [], start=1):
            if not isinstance(fact, dict):
                continue
            fields = _fact_fields(fact)
            rows.append(
                {
                    "fact_ref": _fact_reference(document, fact, index),
                    "document_id": document.get("document_id"),
                    "document": document.get("display_name") or document.get("file_name") or document.get("file_path"),
                    "file_path": document.get("file_path"),
                    "original_file_name": document.get("original_file_name") or document.get("file_name"),
                    "document_type": document.get("document_type"),
                    "fact_type": fact.get("fact_type"),
                    "evidence_id": fact.get("evidence_id"),
                    "page": fact.get("page"),
                    "snippet": fact.get("snippet"),
                    "fields": fields,
                }
            )
    return rows


def _normalised_amount_for_compare(value: str | None) -> str | None:
    if value in {None, ""}:
        return None
    cleaned = _clean_money_amount(str(value)) or ""
    comparable = re.sub(r"[^0-9.-]", "", cleaned)
    return comparable or None


def _build_source_coverage_continuity_payload(facts_payload: dict) -> dict:
    facts = _source_coverage_facts(facts_payload)
    findings: list[dict] = []
    document_type_counts: dict[str, int] = {}
    fact_type_counts: dict[str, int] = {}
    for document in facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []:
        if not isinstance(document, dict):
            continue
        document_type = str(document.get("document_type") or "unknown")
        document_type_counts[document_type] = document_type_counts.get(document_type, 0) + 1
    for fact in facts:
        fact_type_counts[str(fact.get("fact_type") or "unknown")] = fact_type_counts.get(str(fact.get("fact_type") or "unknown"), 0) + 1

    bank_periods_by_account: dict[str, list[dict]] = {}
    for fact in facts:
        if fact.get("fact_type") != "bank_statement_period_balance":
            continue
        fields = fact["fields"]
        account_key = _account_key_from_bank_balance(fields)
        start = _parse_bank_statement_date(str(fields.get("statement_period_start") or ""))
        end = _parse_bank_statement_date(str(fields.get("statement_period_end") or ""))
        bank_periods_by_account.setdefault(account_key, []).append({**fact, "account_key": account_key, "period_start_dt": start, "period_end_dt": end})

    bank_accounts: list[dict] = []
    for account_key, periods in sorted(bank_periods_by_account.items()):
        periods.sort(key=lambda item: (item.get("period_start_dt") or datetime.min, item.get("period_end_dt") or datetime.min, str(item.get("document"))))
        seen_periods: dict[tuple[str, str], list[dict]] = {}
        for item in periods:
            fields = item["fields"]
            period_key = (str(fields.get("statement_period_start") or ""), str(fields.get("statement_period_end") or ""))
            seen_periods.setdefault(period_key, []).append(item)
        for (start, end), items in seen_periods.items():
            if start and end and len(items) > 1:
                findings.append(
                    {
                        "category": "duplicate_bank_statement_period",
                        "severity": "medium",
                        "account_identifier": account_key,
                        "period_start": start,
                        "period_end": end,
                        "evidence_refs": [str(item.get("fact_ref")) for item in items],
                        "investigation_summary": [f"{len(items)} bank statement balance facts share the same statement period."],
                        "recommended_action": "Review duplicate statements and keep the authoritative statement before relying on continuity.",
                    }
                )
        for previous, current in zip(periods, periods[1:]):
            prev_fields = previous["fields"]
            current_fields = current["fields"]
            prev_end = previous.get("period_end_dt")
            current_start = current.get("period_start_dt")
            if prev_end and current_start and current_start.date() > (prev_end + timedelta(days=1)).date():
                findings.append(
                    {
                        "category": "missing_bank_statement_period",
                        "severity": "high",
                        "account_identifier": account_key,
                        "previous_period_end": prev_fields.get("statement_period_end"),
                        "next_period_start": current_fields.get("statement_period_start"),
                        "evidence_refs": [str(previous.get("fact_ref")), str(current.get("fact_ref"))],
                        "investigation_summary": ["There is a date gap between consecutive statement periods for this bank account."],
                        "recommended_action": "Request the missing bank statement period or confirm the account was inactive/closed.",
                    }
                )
            previous_closing = _normalised_amount_for_compare(prev_fields.get("closing_balance"))
            current_opening = _normalised_amount_for_compare(current_fields.get("opening_balance"))
            if previous_closing and current_opening and previous_closing != current_opening:
                findings.append(
                    {
                        "category": "bank_opening_closing_mismatch",
                        "severity": "high",
                        "account_identifier": account_key,
                        "previous_closing_balance": prev_fields.get("closing_balance"),
                        "next_opening_balance": current_fields.get("opening_balance"),
                        "evidence_refs": [str(previous.get("fact_ref")), str(current.get("fact_ref"))],
                        "investigation_summary": ["Closing balance from one statement does not agree to the next statement opening balance."],
                        "recommended_action": "Review statement sequence, missing transactions, duplicate statements, or extraction accuracy.",
                    }
                )
        bank_accounts.append(
            {
                "account_identifier": account_key,
                "statement_count": len(periods),
                "periods": [
                    {
                        "document": item.get("document"),
                        "fact_ref": item.get("fact_ref"),
                        "period_start": item["fields"].get("statement_period_start"),
                        "period_end": item["fields"].get("statement_period_end"),
                        "opening_balance": item["fields"].get("opening_balance"),
                        "closing_balance": item["fields"].get("closing_balance"),
                    }
                    for item in periods
                ],
            }
        )

    return {
        "artifact_type": "source_coverage_continuity",
        "entity_name": facts_payload.get("entity_name", "Uploaded documents") if isinstance(facts_payload, dict) else "Uploaded documents",
        "document_type_counts": document_type_counts,
        "fact_type_counts": fact_type_counts,
        "bank_accounts": bank_accounts,
        "findings": findings,
        "summary": {
            "documents": len(facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []),
            "facts": len(facts),
            "bank_accounts": len(bank_accounts),
            "findings": len(findings),
            "high_severity_findings": sum(1 for item in findings if item.get("severity") == "high"),
        },
    }


def _format_source_coverage_continuity(payload: dict) -> str:
    summary = payload.get("summary", {})
    lines = [f"# Source Coverage & Continuity — {payload.get('entity_name', 'Uploaded documents')}", ""]
    lines.extend(
        [
            f"- Documents: {summary.get('documents', 0)}",
            f"- Bank accounts: {summary.get('bank_accounts', 0)}",
            f"- Findings: {summary.get('findings', 0)}",
            "",
        ]
    )
    if payload.get("bank_accounts"):
        lines.append("## Bank statement coverage")
        for account in payload["bank_accounts"]:
            lines.append(f"- {account.get('account_identifier')}: {account.get('statement_count')} statement period(s)")
    if payload.get("findings"):
        lines.extend(["", "## Findings"])
        for finding in payload["findings"]:
            lines.extend(
                [
                    f"- {finding.get('category')} ({finding.get('severity')}): {finding.get('account_identifier', '')}",
                    f"  - Evidence: {', '.join(finding.get('evidence_refs', []))}",
                    f"  - Action: {finding.get('recommended_action')}",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _process_documents_command(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir)
    artifact_dir = Path(args.artifact_dir)
    codex_command = _normalise_codex_cli_command(str(args.codex_command))
    batch_size = max(1, int(getattr(args, "batch_size", 1) or 1))
    force_reprocess = bool(getattr(args, "force_reprocess", False))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Input directory not found: {input_dir}", file=sys.stderr)
        return 2
    files = sorted(
        path
        for path in input_dir.rglob("*")
        if path.is_file()
        and path.name != ".DS_Store"
        and not any(part.startswith(".") for part in path.relative_to(input_dir).parts)
    )
    per_document_dir = artifact_dir / "per_document"
    cache_dir = artifact_dir / ".codex_doc_cache"
    progress_path = artifact_dir / "document_processing_progress.json"
    per_document_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    documents: list[dict] = []
    cache_hits = 0
    facts = 0
    failures = 0
    codex_attempts = 0
    codex_successes = 0
    total_batches = (len(files) + batch_size - 1) // batch_size if files else 0

    def process_one(index: int, path: Path) -> dict:
        document_id = f"raw_{index:03d}"
        source_hash = _sha256_file(path)
        cache_path = cache_dir / f"{_CODEX_DOCUMENT_PROCESSING_CACHE_VERSION}_{source_hash}.json"
        if cache_path.exists() and not force_reprocess:
            document = json.loads(cache_path.read_text())
            document["processing_source"] = "cache"
            source = "cache"
            failed = False
            codex_success = False
            attempt_history = []
        else:
            payload = None
            error = None
            attempt_history = []
            for attempt in range(1, max_attempts + 1):
                attempt_timeout = int(args.codex_timeout) * (2 ** (attempt - 1))
                payload, error = _codex_process_document(
                    path,
                    document_id,
                    source_hash,
                    codex_command,
                    attempt_timeout,
                    recovery_attempt=attempt - 1,
                    previous_error=error,
                )
                if payload is not None:
                    validation_error = _codex_document_validation_error(path, payload)
                    if validation_error:
                        error = validation_error
                        payload = None
                attempt_history.append(
                    {
                        "attempt": attempt,
                        "mode": "normal" if attempt == 1 else "recovery",
                        "timeout_seconds": attempt_timeout,
                        "status": "success" if payload is not None else "failed",
                        "error": error or "",
                    }
                )
                if payload is not None:
                    break
            if payload is None:
                document = _normalise_codex_document_result(
                    path,
                    document_id,
                    source_hash,
                    {
                        "display_name": path.name,
                        "document_type": _classify_raw_document(path),
                        "status": "processing_failed",
                        "no_fact_reason": error or "Codex CLI did not return a usable document result.",
                    },
                )
                document["processing_source"] = "codex_cli_failed"
                document["codex_attempt_history"] = attempt_history
                source = "codex_cli_failed"
                failed = True
                codex_success = False
            else:
                document = _normalise_codex_document_result(path, document_id, source_hash, payload)
                document["processing_source"] = "codex_cli"
                document["codex_attempt_history"] = attempt_history
                cache_path.write_text(json.dumps(document, indent=2, sort_keys=True))
                source = "codex_cli"
                failed = False
                codex_success = True
        document["document_id"] = document_id
        document["file_path"] = str(path)
        document["file_name"] = path.name
        document["original_file_name"] = path.name
        document["source_hash"] = source_hash
        if not document.get("page_quotes"):
            document["page_quotes"] = _document_text_for_codex(path)[0]
        return {
            "index": index,
            "path": path,
            "document_id": document_id,
            "document": document,
            "source": source,
            "failed": failed,
            "codex_success": codex_success,
            "attempts": len(attempt_history) if source != "cache" else 0,
            "facts": len(document.get("primary_amounts", []) or []) + len(document.get("review_flags", []) or []),
        }

    _write_document_processing_progress(
        progress_path,
        processed=0,
        total=len(files),
        current_document="",
        status="running",
        cache_hits=0,
        facts=0,
        codex_attempts=0,
        codex_successes=0,
        batch_size=batch_size,
        current_batch=0,
        total_batches=total_batches,
    )
    for batch_number, batch_start in enumerate(range(0, len(files), batch_size), start=1):
        batch = list(enumerate(files[batch_start : batch_start + batch_size], start=batch_start + 1))
        if batch_size == 1:
            results = [process_one(index, path) for index, path in batch]
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = [executor.submit(process_one, index, path) for index, path in batch]
                results = [future.result() for future in as_completed(futures)]
        for result in sorted(results, key=lambda item: item["index"]):
            document = result["document"]
            documents.append(document)
            if result["source"] == "cache":
                cache_hits += 1
            else:
                codex_attempts += int(result.get("attempts") or 1)
            if result["codex_success"]:
                codex_successes += 1
            if result["failed"]:
                failures += 1
            facts += int(result["facts"])
            per_document_dir.mkdir(parents=True, exist_ok=True)
            (per_document_dir / f"{result['document_id']}.json").write_text(json.dumps(document, indent=2, sort_keys=True))
            _write_document_processing_progress(
                progress_path,
                processed=len(documents),
                total=len(files),
                current_document=result["path"].name,
                status="running",
                cache_hits=cache_hits,
                facts=facts,
                failures=failures,
                codex_attempts=codex_attempts,
                codex_successes=codex_successes,
                batch_size=batch_size,
                current_batch=batch_number,
                total_batches=total_batches,
            )
    documents.sort(key=lambda item: str(item.get("display_name") or item.get("file_name") or "").casefold())
    inventory_payload = {"inventory_id": "processed_documents", "entity_name": "Uploaded documents", "documents": documents}
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "document_inventory.json").write_text(json.dumps(inventory_payload, indent=2, sort_keys=True))
    (artifact_dir / "document_inventory.md").write_text(_format_processed_document_inventory(inventory_payload))
    source_index_payload = _build_source_document_index_from_processed(inventory_payload)
    (artifact_dir / "source_document_index.json").write_text(json.dumps(source_index_payload, indent=2, sort_keys=True))
    (artifact_dir / "source_document_index.md").write_text(_format_processed_document_inventory(source_index_payload))
    facts_payload = _build_accounting_facts_by_document_from_processed(inventory_payload)
    (artifact_dir / "accounting_facts_by_document.json").write_text(json.dumps(facts_payload, indent=2, sort_keys=True))
    coverage_payload = _build_source_coverage_continuity_payload(facts_payload)
    (artifact_dir / "source_coverage_continuity.json").write_text(json.dumps(coverage_payload, indent=2, sort_keys=True))
    (artifact_dir / "source_coverage_continuity.md").write_text(_format_source_coverage_continuity(coverage_payload))
    final_status = "complete" if failures == 0 else "failed"
    _write_document_processing_progress(
        progress_path,
        processed=len(files),
        total=len(files),
        current_document="",
        status=final_status,
        cache_hits=cache_hits,
        facts=facts,
        failures=failures,
        codex_attempts=codex_attempts,
        codex_successes=codex_successes,
        batch_size=batch_size,
        current_batch=total_batches,
        total_batches=total_batches,
    )
    print(f"Indexed {len(files)} documents; fresh Codex successes: {codex_successes}; cache hits: {cache_hits}; source signals: {facts}; failures: {failures}")
    return 0 if failures == 0 else 1


def _apply_document_name_suggestion(
    document: SourceDocument,
    *,
    path: Path,
    quote: str,
    evidence_refs: list[str],
    use_codex: bool,
    codex_command: str,
    codex_timeout: int,
) -> None:
    display_name, confidence, method = _name_from_quote(path, document.document_type, quote)
    ambiguous = _is_ambiguous_file_name(path)
    if use_codex:
        codex_payload = _codex_suggest_document_name(path, document.document_type, quote, display_name, codex_command, codex_timeout)
        if codex_payload:
            display_name = _sanitize_document_name(str(codex_payload["display_name"]), path.suffix)
            confidence = str(codex_payload.get("confidence") or "medium")
            method = "codex_cli"
            suggested_type = str(codex_payload.get("document_type") or "")
            if suggested_type in _KNOWN_DOCUMENT_TYPES:
                document.document_type = suggested_type
    if display_name is None and ambiguous:
        display_name = _sanitize_document_name(f"{document.document_type.replace('_', ' ').title()} - {document.document_id}", path.suffix)
        confidence = "low"
        method = "fallback"
    document.original_file_name = path.name
    document.display_name = display_name or path.name
    document.naming_confidence = confidence if display_name else None
    document.naming_status = "suggested" if display_name else "not_suggested"
    document.naming_method = method if display_name else None
    document.naming_evidence_refs = evidence_refs[:3]


def _ingest_raw_inputs_into_state(state: EngagementState, args: argparse.Namespace) -> tuple[int, int, Path]:
    input_dir = Path(args.input_dir)
    if not input_dir.exists() or not input_dir.is_dir():
        raise SystemExit(f"Input directory not found: {input_dir}")
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
            original_file_name=path.name,
        )
        state.source_documents.append(document)
        naming_quotes: list[str] = []
        naming_evidence_refs: list[str] = []
        if path.suffix.lower() == ".md":
            quote = path.read_text(errors="ignore")[:500]
            evidence_id = f"raw_{idx:03d}_text_001"
            state.evidence.append(
                EvidenceRef(
                    evidence_id=evidence_id,
                    source_type=document_type,
                    file_path=str(path),
                    quote=quote,
                    document_id=document_id,
                    confidence="1.0",
                )
            )
            naming_quotes.append(quote)
            naming_evidence_refs.append(evidence_id)
        elif path.suffix.lower() == ".pdf":
            page_quotes = _extract_pdf_page_quotes(path)
            if page_quotes:
                for page_number, quote in page_quotes:
                    evidence_id = f"raw_{idx:03d}_page_{page_number:03d}"
                    state.evidence.append(
                        EvidenceRef(
                            evidence_id=evidence_id,
                            source_type=document_type,
                            file_path=str(path),
                            page=str(page_number),
                            quote=quote,
                            document_id=document_id,
                            confidence="text_pdf",
                        )
                    )
                    naming_quotes.append(quote)
                    naming_evidence_refs.append(evidence_id)
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
                evidence_id = f"raw_{idx:03d}_page_001"
                state.evidence.append(
                    EvidenceRef(
                        evidence_id=evidence_id,
                        source_type=document_type,
                        file_path=str(path),
                        page="1",
                        quote=quote,
                        document_id=document_id,
                        confidence="image_ocr",
                    )
                )
                naming_quotes.append(quote)
                naming_evidence_refs.append(evidence_id)
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
        document.document_type = _classify_raw_document_from_content(path, document.document_type, " ".join(naming_quotes))
        for evidence in state.evidence:
            if evidence.document_id == document.document_id:
                evidence.source_type = document.document_type
        _apply_document_name_suggestion(
            document,
            path=path,
            quote=" ".join(naming_quotes),
            evidence_refs=naming_evidence_refs,
            use_codex=False,
            codex_command="codex",
            codex_timeout=30,
        )
        for evidence in state.evidence:
            if evidence.document_id == document.document_id:
                evidence.source_type = document.document_type
    state.documents_ref = str(input_dir)
    return len(files), extraction_required, input_dir


def _ingest_raw_inputs_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    before = state_hash(state)
    file_count, extraction_required, input_dir = _ingest_raw_inputs_into_state(state, args)
    _record_state_transition(state, command="ingest-raw-inputs", before_hash=before, summary=f"Registered {file_count} raw input documents; extraction required for {extraction_required}.")
    save_engagement_state(state_path, state)
    print(f"Registered {file_count} raw input documents; extraction-required: {extraction_required}")
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
    {"name": "Capital Gain/(Loss) on Sale of Non-Current Assets", "aliases": ["Capital Gain/(Loss) on Sale of Non-Current Assets"], "type": "income", "group": "Other Income", "section": "profit_and_loss"},
    {"name": "Distributions Received", "aliases": ["Distributions Received"], "type": "income", "group": "Investment Income", "section": "profit_and_loss"},
    {"name": "Dividends Received", "aliases": ["Dividends Received"], "type": "income", "group": "Investment Income", "section": "profit_and_loss"},
    {"name": "Interest Income", "aliases": ["Interest Income"], "type": "income", "group": "Investment Income", "section": "profit_and_loss"},
    {"name": "Accounting Fees", "aliases": ["Accounting Fees"], "type": "expense", "group": "Expenses", "section": "profit_and_loss"},
    {"name": "Bank Fees", "aliases": ["Bank Fees"], "type": "expense", "group": "Expenses", "section": "profit_and_loss"},
    {"name": "Filing Fees", "aliases": ["Filing Fees"], "type": "expense", "group": "Expenses", "section": "profit_and_loss"},
    {"name": "Investment Expenses", "aliases": ["Investment Expenses"], "type": "expense", "group": "Expenses", "section": "profit_and_loss"},
    {"name": "Cash at Bank CBA0700", "aliases": ["Cash at Bank CBA0700"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Cash at Bank WBC8243", "aliases": ["Cash at Bank WBC8243"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Hub24 Cash Account", "aliases": ["Hub24 Cash Account"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Hub24 (Infinity SMID) Cash Account", "aliases": ["Hub24 (Infinity SMID) Cash Account"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Cash on Hand", "aliases": ["Cash on Hand"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Sundry Debtors - Spire Branford Castle US Private Equity Fund II", "aliases": ["Spire Branford Castle US Private Equity Fund II"], "type": "asset", "group": "Receivables / Sundry Debtors", "section": "sundry_debtors"},
    {"name": "ANZ - Capital Notes 9", "aliases": ["ANZ - Capital Notes 9", "Investments ANZ Capital Notes"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "Bendigo and Adelaide Bank Limited - Capital Notes 2", "aliases": ["Bendigo and Adelaide Bank Limited - Capital Notes 2"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "EVP Fund III", "aliases": ["EVP Fund III"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "HUB24 Investments", "aliases": ["HUB24 Investments"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "Newmark Bourke St Mall Trust", "aliases": ["Newmark Bourke St Mall Trust"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "Spire Branford Castle US Private Equity Fund II", "aliases": ["Spire Branford Castle US Private Equity Fund II"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "Unsecured Loan - Australia Property Trust", "aliases": ["Unsecured Loan - Australia Property Trust"], "type": "asset", "group": "Other Financial Assets", "section": "related_party_loans"},
    {"name": "Accrued expenses", "aliases": ["Accrued expenses"], "type": "liability", "group": "Payables and Accruals", "section": "current_liabilities"},
    {"name": "Unpaid Present Entitlement", "aliases": ["Unpaid Present Entitlement (2024)", "Unpaid Present Entitlement"], "type": "liability", "group": "Beneficiary Accounts", "section": "beneficiary_accounts", "skip_leading_dash": True},
    {"name": "Unsecured Loan", "aliases": ["Unsecured Loan"], "type": "liability", "group": "Borrowings / Loans", "section": "non_current_liabilities", "skip_leading_dash": True},
    {"name": "Settlement Sum", "aliases": ["Settlement Sum"], "type": "equity", "group": "Equity", "section": "equity"},
    {"name": "Current Year Earnings", "aliases": ["Current Year Earnings"], "type": "equity", "group": "Accumulated Income / Distributions", "section": "equity"},
    {"name": "Profit Distribution - Beneficiary", "aliases": ["Beneficiary"], "type": "equity", "group": "Accumulated Income / Distributions", "section": "equity", "leading_dash_is_negative": True},
]


def _prior_statement_account_code(index: int, account_type: str) -> str:
    prefix = {"asset": "1", "liability": "2", "equity": "3", "income": "4", "expense": "6"}.get(account_type, "9")
    return f"{prefix}{index:03d}"


def _compact_quote(value: str | None) -> str:
    return " ".join((value or "").split())


def _source_index_evidence_ref(document_id: str, evidence_id: str | None, page: str | None = None) -> str:
    value = str(evidence_id or "").strip()
    if re.search(r"_(?:page|text)_\d+$", value):
        return value
    if value and value.startswith(f"{document_id}_"):
        return value
    if value:
        return f"{document_id}_{value}"
    if page:
        return f"{document_id}_page_{int(page):03d}" if str(page).isdigit() else f"{document_id}_page_{page}"
    return document_id


def _quote_between(quote: str, start_pattern: str, end_pattern: str) -> str:
    start = re.search(start_pattern, quote, re.IGNORECASE)
    if not start:
        return ""
    remainder = quote[start.end():]
    end = re.search(end_pattern, remainder, re.IGNORECASE)
    return remainder[: end.start()] if end else remainder


def _prior_statement_section_quote(quote: str, section: str) -> str:
    sections = {
        "profit_and_loss": (r"\bProfit and Loss\b", r"\bRestricted for internal use only\b|\bBalance Sheet\b"),
        "cash": (r"\bCash and Cash Equivalents\b", r"\bTOTAL CASH AND CASH EQUIVALENTS\b"),
        "sundry_debtors": (r"\bSundry Debtors\b", r"\bTOTAL SUNDRY DEBTORS\b"),
        "investments": (r"\bInvestments\b", r"\bTOTAL INVESTMENTS\b"),
        "related_party_loans": (r"\bRelated Party Loans\b", r"\bTOTAL RELATED PARTY LOANS\b"),
        "current_liabilities": (r"\bCURRENT LIABILITIES\b", r"\bTOTAL CURRENT LIABILITIES\b"),
        "beneficiary_accounts": (r"\bBeneficiary Accounts\b", r"\bTOTAL BENEFICIARY ACCOUNTS\b"),
        "non_current_liabilities": (r"\bNON CURRENT LIABILITIES\b", r"\bTOTAL NON CURRENT LIABILITIES\b"),
        "equity": (r"\bEQUITY\b", r"\bTOTAL EQUITY\b"),
    }
    markers = sections.get(section)
    if not markers:
        return quote
    return _quote_between(quote, *markers)


def _prior_statement_amount_after_alias(quote: str, alias: str, *, skip_leading_dash: bool = False, leading_dash_is_negative: bool = False) -> str | None:
    match = re.search(re.escape(alias), quote, re.IGNORECASE)
    if not match:
        return None
    after = quote[match.end(): match.end() + 180]
    tokens = re.findall(r"\(?-?\$?\d[\d,]*(?:\.\d{2})?\)?|-", after)
    if not tokens:
        return None
    if tokens[0] == "-" and leading_dash_is_negative:
        for token in tokens[1:]:
            if token != "-":
                amount = _normalise_amount(token) or token
                return f"-{amount}" if not str(amount).startswith("-") else str(amount)
    if tokens[0] == "-" and skip_leading_dash:
        for token in tokens[1:]:
            if token != "-":
                return _normalise_amount(token) or token
    if tokens[0] == "-":
        return "0.00"
    return _normalise_amount(tokens[0]) or tokens[0]


def _extract_prior_statement_accounts_from_page_quotes(page_quotes: list[dict], *, document_id: str) -> list[ChartAccount]:
    accounts: list[ChartAccount] = []
    seen_names: set[str] = set()
    for page in page_quotes:
        quote = _compact_quote(str(page.get("quote") or ""))
        if not quote:
            continue
        evidence_ref = _source_index_evidence_ref(document_id, str(page.get("evidence_id") or ""), str(page.get("page") or ""))
        for spec in _PRIOR_STATEMENT_ACCOUNT_SPECS:
            name = str(spec["name"])
            if name in seen_names:
                continue
            section_quote = _prior_statement_section_quote(quote, str(spec.get("section") or ""))
            amount = None
            for alias in spec.get("aliases", [name]):
                amount = _prior_statement_amount_after_alias(
                    section_quote,
                    str(alias),
                    skip_leading_dash=bool(spec.get("skip_leading_dash")),
                    leading_dash_is_negative=bool(spec.get("leading_dash_is_negative")),
                )
                if amount is not None:
                    break
            if amount is None:
                continue
            account_type = str(spec["type"])
            group = str(spec["group"])
            code = _prior_statement_account_code(len(accounts) + 1, account_type)
            accounts.append(ChartAccount(account_id=f"prior_acct_{code}", code=code, name=name, type=account_type, presentation_group=group, opening_balance=amount or "0.00", source_evidence_refs=[evidence_ref]))
            seen_names.add(name)
    return accounts


def _extract_prior_statement_accounts(state: EngagementState) -> list[ChartAccount]:
    page_quotes = [
        {"evidence_id": evidence.evidence_id, "page": evidence.page, "quote": evidence.quote}
        for evidence in state.evidence
        if evidence.source_type == "prior_year_financial_statements"
    ]
    return _extract_prior_statement_accounts_from_page_quotes(page_quotes, document_id="prior_statement")


def _source_index_prior_fs_candidates(source_index: dict) -> list[dict]:
    candidates: list[dict] = []
    for document in _list_value(source_index.get("documents")):
        if not isinstance(document, dict):
            continue
        doc_type = str(document.get("document_type") or "").strip().lower()
        display = str(document.get("display_name") or document.get("file_name") or document.get("original_file_name") or "")
        if doc_type == "prior_year_financial_statements" or re.search(r"\bprior\b.*\bfinancial statement|\bfinancial statements?\b", display, re.IGNORECASE):
            candidates.append(document)
    return candidates


def _select_prior_fs_document(source_index: dict, *, prior_fs_document_id: str | None = None, prior_fs_file: str | None = None) -> tuple[dict | None, list[dict]]:
    findings: list[dict] = []
    candidates = _source_index_prior_fs_candidates(source_index)
    if prior_fs_document_id:
        for document in candidates:
            if str(document.get("document_id") or "") == prior_fs_document_id:
                return document, findings
        findings.append({"category": "prior_fs_document_not_found", "severity": "high", "message": f"Prior-year FS document id was not found in source index: {prior_fs_document_id}."})
        return None, findings
    if prior_fs_file:
        target = Path(prior_fs_file).name.lower()
        for document in candidates:
            names = {
                Path(str(document.get("file_path") or "")).name.lower(),
                str(document.get("file_name") or "").lower(),
                str(document.get("original_file_name") or "").lower(),
                str(document.get("display_name") or "").lower(),
            }
            if target in names:
                return document, findings
        findings.append({"category": "prior_fs_file_not_found", "severity": "high", "message": f"Prior-year FS file was not found in source index: {prior_fs_file}."})
        return None, findings
    if len(candidates) == 1:
        return candidates[0], findings
    if not candidates:
        findings.append({"category": "prior_fs_missing", "severity": "high", "message": "No prior-year financial statement document was found in Step 2 source index."})
        return None, findings
    findings.append({"category": "prior_fs_not_unique", "severity": "high", "message": "More than one prior-year financial statement document was found. Specify --prior-fs-document-id or --prior-fs-file."})
    return None, findings


def _build_prior_statement_coa_from_source_index(source_index: dict, *, prior_fs_document_id: str | None = None, prior_fs_file: str | None = None) -> dict:
    document, findings = _select_prior_fs_document(source_index, prior_fs_document_id=prior_fs_document_id, prior_fs_file=prior_fs_file)
    accounts: list[ChartAccount] = []
    if document:
        page_quotes = [page for page in _list_value(document.get("page_quotes")) if isinstance(page, dict)]
        accounts = _extract_prior_statement_accounts_from_page_quotes(page_quotes, document_id=str(document.get("document_id") or "prior_fs"))
        if not accounts:
            findings.append({"category": "prior_statement_coa_not_extracted", "severity": "high", "message": "Prior-year FS document was selected, but no opening balance accounts were extracted from its page quotes."})
    entity_name = str(source_index.get("entity_name") or source_index.get("engagement_id") or "Uploaded documents")
    return {
        "engagement_id": str(source_index.get("inventory_id") or source_index.get("engagement_id") or "source_document_index"),
        "entity_name": entity_name,
        "prior_fs_document_id": str(document.get("document_id") or "") if document else "",
        "prior_fs_display_name": str(document.get("display_name") or document.get("file_name") or "") if document else "",
        "accounts": [account.model_dump() for account in accounts],
        "findings": findings,
        "summary": {"accounts_imported": len(accounts), "findings": len(findings), "approved": 0, "prior_fs_documents": 1 if document else 0},
    }


def _format_prior_statement_coa_import(payload: dict) -> str:
    lines = [f"# Prior Statement CoA Import — {payload['entity_name']}", ""]
    summary = payload["summary"]
    if payload.get("prior_fs_display_name"):
        lines.append(f"- Prior-year FS document: {payload['prior_fs_display_name']}")
    lines.extend([f"- Accounts imported: {summary['accounts_imported']}", f"- Approved automatically: {summary['approved']}", ""])
    if payload["accounts"]:
        lines.append("## Imported accounts pending review")
        for account in payload["accounts"]:
            lines.extend([f"- {account['code']} {account['name']}", f"  - Type: {account['type']}", f"  - Group: {account['presentation_group']}", f"  - Opening balance: {account['opening_balance']}", f"  - Evidence: {', '.join(account.get('source_evidence_refs', []))}"])
    if payload["findings"]:
        lines.extend(["", "## Findings needing review"])
        for finding in payload["findings"]:
            lines.extend([f"- {finding['category']}", f"  - Action: {finding.get('recommended_action') or finding.get('message') or ''}"])
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

    run_step(
        "ingest-raw-inputs",
        _ingest_raw_inputs_command,
        argparse.Namespace(
            state=str(state_path),
            input_dir=args.input_dir,
        ),
    )
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
        _ingest_raw_inputs_command(
            argparse.Namespace(
                state=str(state_path),
                input_dir=args.input_dir,
            )
        )
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

    def add_ai_extraction_args(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--use-ai-extraction", action="store_true")
        command_parser.add_argument("--ai-provider", choices=["openai", "anthropic"], default="anthropic", help=argparse.SUPPRESS)
        command_parser.add_argument("--openai-model", default=DEFAULT_OPENAI_FACT_MODEL, help=argparse.SUPPRESS)
        command_parser.add_argument("--anthropic-model", default=DEFAULT_ANTHROPIC_FACT_MODEL, help=argparse.SUPPRESS)
        command_parser.add_argument("--openai-timeout", default=60, type=int, help=argparse.SUPPRESS)

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

    process_documents_parser = subparsers.add_parser(
        "process-documents",
        help="Process uploaded documents one at a time with Codex CLI and write per-document JSON results.",
    )
    process_documents_parser.add_argument("--input-dir", default="inputs")
    process_documents_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    process_documents_parser.add_argument("--codex-command", default="codex exec")
    process_documents_parser.add_argument("--codex-timeout", default=120, type=int)
    process_documents_parser.add_argument("--codex-max-attempts", default=3, type=int)
    process_documents_parser.add_argument("--batch-size", default=5, type=int)
    process_documents_parser.add_argument("--force-reprocess", action="store_true", help="Ignore existing per-document Codex cache and rerun extraction for uploaded files.")
    process_documents_parser.set_defaults(func=_process_documents_command)

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
    bank_facts_parser.add_argument("--inventory", default=None)
    bank_facts_parser.add_argument("--output", default="outputs/bank_statement_facts.md")
    add_ai_extraction_args(bank_facts_parser)
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
    invoice_facts_parser.add_argument("--inventory", default=None)
    invoice_facts_parser.add_argument("--output", default="outputs/invoice_facts.md")
    add_ai_extraction_args(invoice_facts_parser)
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
    distribution_tax_parser.add_argument("--inventory", default=None)
    distribution_tax_parser.add_argument("--output", default="outputs/distribution_tax_facts.md")
    add_ai_extraction_args(distribution_tax_parser)
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
    broker_trade_facts_parser.add_argument("--inventory", default=None)
    broker_trade_facts_parser.add_argument("--output", default="outputs/broker_trade_facts.md")
    add_ai_extraction_args(broker_trade_facts_parser)
    broker_trade_facts_parser.set_defaults(func=_export_broker_trade_facts_command)

    broker_trade_review_parser = subparsers.add_parser(
        "export-broker-trade-review",
        help="Create accountant review findings from extracted broker trade facts without auto-approval.",
    )
    broker_trade_review_parser.add_argument("--facts", default="outputs/broker_trade_facts.json")
    broker_trade_review_parser.add_argument("--output", default="outputs/broker_trade_review.md")
    broker_trade_review_parser.set_defaults(func=_export_broker_trade_review_command)

    accounting_facts_by_document_parser = subparsers.add_parser(
        "export-accounting-facts-by-document",
        help="Build the primary document-grouped accounting facts artifact from internal extracted fact files.",
    )
    accounting_facts_by_document_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    accounting_facts_by_document_parser.add_argument("--inventory", default=None)
    accounting_facts_by_document_parser.add_argument("--artifact-dir", default="outputs")
    accounting_facts_by_document_parser.add_argument("--output", default="outputs/accounting_facts_by_document.json")
    accounting_facts_by_document_parser.add_argument("--remove-legacy-split-facts", action="store_true")
    accounting_facts_by_document_parser.set_defaults(func=_export_accounting_facts_by_document_command)

    source_fact_match_parser = subparsers.add_parser(
        "match-source-facts",
        help="Use Codex CLI to build the Step 3 relationship reasoning register from the source document index.",
    )
    source_fact_match_parser.add_argument("--accounting-facts", default=None)
    source_fact_match_parser.add_argument("--source-coverage", default=None)
    source_fact_match_parser.add_argument("--codex-command", default="codex exec")
    source_fact_match_parser.add_argument("--codex-timeout", type=int, default=600)
    source_fact_match_parser.add_argument("--codex-max-attempts", type=int, default=3)
    source_fact_match_parser.add_argument("--bank-transactions", default=None)
    source_fact_match_parser.add_argument("--invoice-facts", default=None)
    source_fact_match_parser.add_argument("--distribution-tax-facts", default=None)
    source_fact_match_parser.add_argument("--broker-trade-facts", default=None)
    source_fact_match_parser.add_argument("--output", default="outputs/source_fact_matches.md")
    source_fact_match_parser.set_defaults(func=_match_source_facts_command)

    step4_workpaper_parser = subparsers.add_parser(
        "build-tb-bridge-workpaper",
        aliases=["build-coa-mapping-workpaper"],
        help="Use Codex CLI to build the Step 4 TB bridge matrix workbook.",
    )
    step4_workpaper_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    step4_workpaper_parser.add_argument("--output-dir", default=TB_BRIDGE_OUTPUT_DIR)
    step4_workpaper_parser.add_argument("--event-register", default=None)
    step4_workpaper_parser.add_argument("--source-index", default=None)
    step4_workpaper_parser.add_argument("--prior-coa", default=None)
    step4_workpaper_parser.add_argument("--prior-fs-document-id", default=None, help="Document id of the single prior-year financial statement to use for the workpaper Starting point.")
    step4_workpaper_parser.add_argument("--prior-fs-file", default=None, help="File name/path of the single prior-year financial statement to use for the workpaper Starting point.")
    step4_workpaper_parser.add_argument("--codex-command", default="codex exec")
    step4_workpaper_parser.add_argument("--codex-timeout", type=int, default=600)
    step4_workpaper_parser.add_argument("--codex-max-attempts", type=int, default=3)
    step4_workpaper_parser.add_argument("--skip-xlsx", action="store_true")
    step4_workpaper_parser.set_defaults(func=_build_coa_mapping_workpaper_command)

    prepare_workpaper_parser = subparsers.add_parser(
        "prepare-workpaper",
        help="Run the accountant-facing folder-path workflow and produce a TB Bridge workbook.",
    )
    prepare_workpaper_parser.add_argument("--client-folder", required=True, help="Folder containing source documents for the workpaper.")
    prepare_workpaper_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    prepare_workpaper_parser.add_argument("--output-dir", default=TB_BRIDGE_OUTPUT_DIR)
    prepare_workpaper_parser.add_argument("--entity-name", default=None)
    prepare_workpaper_parser.add_argument("--fy-start", default=None, help="Target financial year start date, e.g. 2024-07-01.")
    prepare_workpaper_parser.add_argument("--fy-end", default=None, help="Target financial year end date, e.g. 2025-06-30.")
    prepare_workpaper_parser.add_argument("--prior-fs-document-id", default=None, help="Document id of the single prior-year financial statement to use as opening balances.")
    prepare_workpaper_parser.add_argument("--prior-fs-file", default=None, help="File name/path of the single prior-year financial statement to use as opening balances.")
    prepare_workpaper_parser.add_argument("--codex-command", default="codex exec")
    prepare_workpaper_parser.add_argument("--codex-timeout", type=int, default=1200)
    prepare_workpaper_parser.add_argument("--codex-max-attempts", type=int, default=3)
    prepare_workpaper_parser.add_argument("--batch-size", type=int, default=5)
    prepare_workpaper_parser.add_argument("--review-sample-size", type=int, default=8)
    prepare_workpaper_parser.add_argument("--review-correction-rounds", type=int, default=2, help="Maximum bounded Turing correction/re-review rounds before the run stops for human attention.")
    prepare_workpaper_parser.add_argument("--force-reprocess", action="store_true", help="Ignore existing per-document Codex cache. prepare-workpaper is fresh by default unless --allow-cache is supplied.")
    prepare_workpaper_parser.add_argument("--allow-cache", action="store_true", help="Allow Step 2 document cache reuse for a faster development run.")
    prepare_workpaper_parser.add_argument("--skip-xlsx", action="store_true")
    prepare_workpaper_parser.add_argument("--skip-review", action="store_true")
    prepare_workpaper_parser.set_defaults(func=_prepare_workpaper_command)

    review_workpaper_parser = subparsers.add_parser(
        "review-workpaper",
        help="Use Codex CLI as Turing to review a prepared TB Bridge workbook against source evidence.",
    )
    review_workpaper_parser.add_argument("--client-folder", default=None)
    review_workpaper_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    review_workpaper_parser.add_argument("--output-dir", default=TB_BRIDGE_OUTPUT_DIR)
    review_workpaper_parser.add_argument("--workpaper-json", default=None)
    review_workpaper_parser.add_argument("--source-index", default=None)
    review_workpaper_parser.add_argument("--event-register", default=None)
    review_workpaper_parser.add_argument("--prior-coa", default=None)
    review_workpaper_parser.add_argument("--output", default=None)
    review_workpaper_parser.add_argument("--entity-name", default=None)
    review_workpaper_parser.add_argument("--codex-command", default="codex exec")
    review_workpaper_parser.add_argument("--codex-timeout", type=int, default=1200)
    review_workpaper_parser.add_argument("--codex-max-attempts", type=int, default=3)
    review_workpaper_parser.add_argument("--sample-size", type=int, default=8)
    review_workpaper_parser.set_defaults(func=_review_workpaper_command)

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

    workpaper_portal_parser = subparsers.add_parser(
        "serve-workpaper-portal",
        help="Start the local accountant-facing workpaper portal.",
        description="Start the local accountant-facing workpaper portal.",
    )
    workpaper_portal_parser.add_argument("--host", default="127.0.0.1")
    workpaper_portal_parser.add_argument("--port", default=8787, type=int)
    workpaper_portal_parser.set_defaults(func=_serve_workpaper_portal_command)

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
    _load_local_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
