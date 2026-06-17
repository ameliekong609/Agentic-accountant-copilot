"""Command-line interface for the Agentic Accountant Copilot."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from accountant_copilot.adapters.source_pipeline import import_source_pipeline_controls
from accountant_copilot.orchestrator.planner import build_readiness_report, plan_next_tasks
from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.evidence import EvidenceRef
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionStatus
from accountant_copilot.state.preferences import PreferenceRule, PreferenceScope, PreferenceStatus


DEFAULT_STATE_PATH = Path("outputs/engagement_state.json")
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
    if action_value not in {ExceptionStatus.RESOLVED.value, ExceptionStatus.ACCEPTED_RISK.value, ExceptionStatus.REJECTED.value}:
        _usage_error(f"invalid batch action for {exception_id}: {action_value}")
    if not rationale:
        _usage_error(f"batch decision for {exception_id} requires rationale")
    if not approved_by:
        _usage_error(f"batch decision for {exception_id} requires approved_by")
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


def _review_coa_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
    lines = [
        "CoA review",
        f"Engagement: {state.entity_name}",
        f"CoA ref: {state.coa_ref or 'none recorded'}",
        f"Status: {state.coa_review_status}",
        "Required decision: approve chart of accounts names, types, opening balances, and presentation grouping.",
    ]
    print("\n".join(lines) + "\n")
    return 0 if state.coa_review_status == "approved" else 1


def _approve_coa_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    decision = AccountantDecision(
        decision_id=f"decision_approve_coa_{len(state.decisions) + 1:04d}",
        question="Approve chart of accounts for this engagement?",
        selected_option="approve_coa",
        rationale=args.rationale,
        status=DecisionStatus.APPROVED,
        approved_by=args.approved_by,
        evidence_refs=[state.coa_ref] if state.coa_ref else [],
    )
    state.decisions.append(decision)
    state.coa_review_required = True
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
    if not items:
        lines.append("No adjustment or journal review items recorded.")
    for item in items:
        lines.extend(_format_exception_item(item))
    print("\n".join(lines) + "\n")
    return 0 if not [item for item in items if item.is_open or item.status == ExceptionStatus.REJECTED] else 1


def _record_adjustment_decision(args: argparse.Namespace, selected_option: str, status: ExceptionStatus) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)
    item = _find_exception(state, args.exception_id)
    decision = AccountantDecision(
        decision_id=f"decision_{selected_option}_{len(state.decisions) + 1:04d}",
        question=f"{selected_option.replace('_', ' ').title()} {item.exception_id}?",
        selected_option=selected_option,
        rationale=args.rationale,
        status=DecisionStatus.APPROVED,
        approved_by=args.approved_by,
        evidence_refs=list(item.evidence_refs),
    )
    state.decisions.append(decision)
    item.status = status
    item.decision_id = decision.decision_id
    state.adjustment_review_status = "approved" if status == ExceptionStatus.RESOLVED else "rejected"
    save_engagement_state(state_path, state)
    print(f"Recorded {selected_option} for {item.exception_id}")
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
        lines.append(f"- {evidence.evidence_id}: {evidence.source_type} {evidence.file_path} {evidence.quote or ''}".rstrip())
    return "\n".join(lines) + "\n"


def _export_review_packet_command(args: argparse.Namespace) -> int:
    state = load_engagement_state(Path(args.state))
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
        "",
        f"Readiness: {payload['readiness_summary']}",
    ]) + "\n"
    (output_dir / "README.md").write_text(readme)
    (output_dir / "open_exceptions.md").write_text(format_exception_review(state))
    (output_dir / "evidence_summary.md").write_text(_format_evidence_summary(state))
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
    state.lifecycle_status = "released"
    save_engagement_state(state_path, state)
    payload = inspect_engagement(state)
    manifest = {
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "lifecycle_status": state.lifecycle_status,
        "final_output_allowed": payload["final_output_allowed"],
        "signoff_decision_id": signoff.decision_id,
        "workpaper_pack": args.workpaper_pack,
        "audit_trail": args.audit_trail,
        "created_outputs": [ref for ref in [state.statements_ref, args.workpaper_pack, args.audit_trail] if ref],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"Exported release manifest → {output}")
    return 0


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

    validate_parser = subparsers.add_parser(
        "validate-state",
        help="Validate engagement state JSON before running state-changing commands.",
    )
    validate_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    validate_parser.set_defaults(func=_validate_state_command)

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
    evidence_parser.set_defaults(func=_record_evidence_command)

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

    coa_approve_parser = subparsers.add_parser(
        "approve-coa",
        help="Record accountant approval for chart of accounts.",
    )
    coa_approve_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    coa_approve_parser.add_argument("--approved-by", required=True)
    coa_approve_parser.add_argument("--rationale", required=True)
    coa_approve_parser.set_defaults(func=_approve_coa_command)

    adjustment_review_parser = subparsers.add_parser(
        "review-adjustments",
        help="List adjustment and journal review items.",
    )
    adjustment_review_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    adjustment_review_parser.set_defaults(func=_review_adjustments_command)

    approve_adjustment_parser = subparsers.add_parser(
        "approve-adjustment",
        help="Approve a proposed adjustment or journal review item.",
    )
    approve_adjustment_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    approve_adjustment_parser.add_argument("--exception-id", required=True)
    approve_adjustment_parser.add_argument("--approved-by", required=True)
    approve_adjustment_parser.add_argument("--rationale", required=True)
    approve_adjustment_parser.set_defaults(func=_approve_adjustment_command)

    reject_adjustment_parser = subparsers.add_parser(
        "reject-adjustment",
        help="Reject a proposed adjustment or journal review item.",
    )
    reject_adjustment_parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    reject_adjustment_parser.add_argument("--exception-id", required=True)
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
