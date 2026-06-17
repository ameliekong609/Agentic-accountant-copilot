from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity, ExceptionStatus
from accountant_copilot.state.preferences import PreferenceScope, PreferenceStatus

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=ROOT,
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )


def _state(path: Path, *, blocked: bool = False) -> None:
    exceptions = []
    if blocked:
        exceptions.append(
            ExceptionItem(
                exception_id="exc_blocking",
                source="journal_agent",
                severity=ExceptionSeverity.CRITICAL,
                category="journal_overall_balanced",
                description="Journal does not balance.",
                evidence_refs=["journal:OVERALL"],
                recommended_action="Resolve before release.",
                requires_human_approval=True,
            )
        )
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        exceptions=exceptions,
    )
    path.write_text(state.model_dump_json())


def test_sign_off_engagement_blocks_when_readiness_fails(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    _state(state_path, blocked=True)

    result = _run_cli(
        "sign-off-engagement",
        "--state",
        str(state_path),
        "--approved-by",
        "Amelie",
        "--rationale",
        "Ready for release.",
    )

    assert result.returncode == 1
    assert "Cannot sign off engagement" in result.stderr
    loaded = EngagementState.model_validate_json(state_path.read_text())
    assert loaded.decisions == []


def test_sign_off_engagement_records_approved_final_decision(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    _state(state_path, blocked=False)

    result = _run_cli(
        "sign-off-engagement",
        "--state",
        str(state_path),
        "--approved-by",
        "Amelie",
        "--rationale",
        "All review gates cleared.",
    )

    assert result.returncode == 0
    assert "Engagement signed off by Amelie" in result.stdout
    loaded = EngagementState.model_validate_json(state_path.read_text())
    decision = loaded.decisions[-1]
    assert decision.decision_id == "decision_final_signoff_0001"
    assert decision.status == DecisionStatus.APPROVED
    assert decision.selected_option == "final_signoff"
    assert decision.approved_by == "Amelie"
    assert "All review gates cleared" in decision.rationale


def test_review_exceptions_batch_updates_multiple_items_atomically(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    decisions_path = tmp_path / "decisions.json"
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        exceptions=[
            ExceptionItem(
                exception_id="exc_one",
                source="matching_agent",
                severity=ExceptionSeverity.HIGH,
                category="unmatched_bank_transaction",
                description="Needs support.",
                recommended_action="Review.",
                requires_human_approval=True,
            ),
            ExceptionItem(
                exception_id="exc_two",
                source="statement_agent",
                severity=ExceptionSeverity.LOW,
                category="rounding_note",
                description="Rounding note.",
                recommended_action="Document.",
                requires_human_approval=True,
            ),
        ],
    )
    state_path.write_text(state.model_dump_json())
    decisions_path.write_text(json.dumps({
        "decisions": [
            {"exception_id": "exc_one", "action": "resolved", "rationale": "Support received.", "approved_by": "Amelie"},
            {"exception_id": "exc_two", "action": "accepted_risk", "rationale": "Immaterial.", "approved_by": "Amelie"},
        ]
    }))

    result = _run_cli("review-exceptions", "--state", str(state_path), "--decisions", str(decisions_path))

    assert result.returncode == 0
    assert "Applied 2 exception review decisions" in result.stdout
    loaded = EngagementState.model_validate_json(state_path.read_text())
    assert [item.status for item in loaded.exceptions] == [ExceptionStatus.RESOLVED, ExceptionStatus.ACCEPTED_RISK]
    assert len(loaded.decisions) == 2


def test_review_exceptions_batch_rejects_invalid_input_without_state_change(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    decisions_path = tmp_path / "bad_decisions.json"
    _state(state_path, blocked=True)
    original = state_path.read_text()
    decisions_path.write_text(json.dumps({"decisions": [{"exception_id": "missing", "action": "resolved", "rationale": "x", "approved_by": "Amelie"}]}))

    result = _run_cli("review-exceptions", "--state", str(state_path), "--decisions", str(decisions_path))

    assert result.returncode == 2
    assert "Unknown exception_id in batch: missing" in result.stderr
    assert state_path.read_text() == original


def test_export_workpaper_pack_creates_review_folder(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    out_dir = tmp_path / "workpaper_pack"
    _state(state_path, blocked=True)

    result = _run_cli("export-workpaper-pack", "--state", str(state_path), "--output-dir", str(out_dir))

    assert result.returncode == 1
    assert "Exported workpaper pack" in result.stdout
    expected = {"engagement_summary.md", "exception_review.md", "audit_trail.md", "readiness.json", "decisions.json"}
    assert expected == {p.name for p in out_dir.iterdir()}
    assert "Final output allowed: NO" in (out_dir / "engagement_summary.md").read_text()
    assert "Open exception review" in (out_dir / "exception_review.md").read_text()
    readiness = json.loads((out_dir / "readiness.json").read_text())
    assert readiness["final_output_allowed"] is False


def test_record_and_list_preferences(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    _state(state_path, blocked=False)

    recorded = _run_cli(
        "record-preference",
        "--state",
        str(state_path),
        "--scope",
        "client",
        "--subject",
        "XYZ Trust",
        "--rule",
        "Present investment income by fund manager.",
        "--approved-by",
        "Amelie",
        "--evidence-ref",
        "decision:client_review",
    )

    assert recorded.returncode == 0
    assert "Recorded preference" in recorded.stdout
    loaded = EngagementState.model_validate_json(state_path.read_text())
    pref = loaded.preferences[-1]
    assert pref.scope == PreferenceScope.CLIENT
    assert pref.status == PreferenceStatus.APPROVED
    assert pref.approved_by == "Amelie"
    assert pref.evidence_refs == ["decision:client_review"]

    listed = _run_cli("list-preferences", "--state", str(state_path))
    assert listed.returncode == 0
    assert "Present investment income by fund manager." in listed.stdout
    assert "approved" in listed.stdout
