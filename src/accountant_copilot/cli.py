"""Command-line interface for the Agentic Accountant Copilot."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from accountant_copilot.adapters.v2 import import_v2_exceptions
from accountant_copilot.orchestrator.planner import build_readiness_report, plan_next_tasks
from accountant_copilot.state.engagement import EngagementState


DEFAULT_STATE_PATH = Path("outputs/engagement_state.json")


def load_engagement_state(path: Path) -> EngagementState:
    """Load an engagement state JSON file."""
    try:
        return EngagementState.model_validate_json(path.read_text())
    except FileNotFoundError as exc:
        raise SystemExit(f"Engagement state not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Engagement state is not valid JSON: {path}: {exc}") from exc
    except KeyError as exc:
        raise SystemExit(f"Engagement state missing required field {exc!s}: {path}") from exc


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


def _import_v2_exceptions_command(args: argparse.Namespace) -> int:
    exceptions = import_v2_exceptions(
        step5_path=Path(args.step5),
        step6_path=Path(args.step6),
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
        matches_ref=str(Path(args.step5)),
        journals_ref=str(Path(args.step6)),
        exceptions=exceptions,
    )
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(state.model_dump_json())
    noun = "exception" if len(exceptions) == 1 else "exceptions"
    print(f"Imported {len(exceptions)} V2 {noun} → {output_path}")
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

    import_parser = subparsers.add_parser(
        "import-v2-exceptions",
        help="Import legacy V2 Step 5/6 issues into a new engagement state exception queue.",
    )
    import_parser.add_argument("--step5", required=True, help="Path to V2 outputs/step5.json")
    import_parser.add_argument("--step6", required=True, help="Path to V2 outputs/step6.json")
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
    import_parser.set_defaults(func=_import_v2_exceptions_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
