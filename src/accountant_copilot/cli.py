"""Command-line interface for the Agentic Accountant Copilot."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from accountant_copilot.adapters.source_pipeline import import_source_pipeline_exceptions
from accountant_copilot.orchestrator.planner import build_readiness_report, plan_next_tasks
from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionStatus


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


def _review_exceptions_command(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_engagement_state(state_path)

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


def _import_source_exceptions_command(args: argparse.Namespace) -> int:
    exceptions = import_source_pipeline_exceptions(
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
        exceptions=exceptions,
    )
    output_path = Path(args.output)
    save_engagement_state(output_path, state)
    noun = "exception" if len(exceptions) == 1 else "exceptions"
    print(f"Imported {len(exceptions)} source pipeline {noun} → {output_path}")
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
    review_parser.set_defaults(func=_review_exceptions_command)

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
