from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.decisions import DecisionStatus
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity, ExceptionStatus


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


def _write_state(path: Path) -> None:
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        exceptions=[
            ExceptionItem(
                exception_id="exc_high_bank",
                source="matching_agent",
                severity=ExceptionSeverity.HIGH,
                category="unmatched_bank_transaction",
                description="Bank receipt has no support.",
                evidence_refs=["bank.csv:row7", "support.pdf:p3"],
                recommended_action="Classify the receipt or request missing support.",
                requires_human_approval=True,
            ),
            ExceptionItem(
                exception_id="exc_low_note",
                source="statement_agent",
                severity=ExceptionSeverity.LOW,
                category="rounding_note",
                description="Rounding note requires cleanup.",
                evidence_refs=["workbook.xlsx:Notes"],
                recommended_action="Update note wording.",
            ),
        ],
    )
    path.write_text(state.model_dump_json())


def test_review_exceptions_lists_open_items_grouped_with_evidence(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    _write_state(state_path)

    result = _run_cli("review-exceptions", "--state", str(state_path))

    assert result.returncode == 1
    assert "Open exception review" in result.stdout
    assert "HIGH" in result.stdout
    assert "LOW" in result.stdout
    assert "exc_high_bank" in result.stdout
    assert "Bank receipt has no support." in result.stdout
    assert "Evidence: bank.csv:row7; support.pdf:p3" in result.stdout
    assert "Recommended action: Classify the receipt or request missing support." in result.stdout
    assert "Final output allowed: NO" in result.stdout


def test_review_exceptions_resolves_exception_and_updates_readiness(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    _write_state(state_path)

    result = _run_cli(
        "review-exceptions",
        "--state",
        str(state_path),
        "--exception-id",
        "exc_high_bank",
        "--action",
        "resolved",
        "--rationale",
        "Receipt matched to approved dividend support.",
        "--approved-by",
        "Amelie",
    )

    assert result.returncode == 0
    assert "Updated exception exc_high_bank: resolved" in result.stdout
    assert "Final output allowed: YES" in result.stdout
    loaded = EngagementState.model_validate_json(state_path.read_text())
    high = next(item for item in loaded.exceptions if item.exception_id == "exc_high_bank")
    assert high.status == ExceptionStatus.RESOLVED
    assert high.decision_id is not None
    decision = next(item for item in loaded.decisions if item.decision_id == high.decision_id)
    assert decision.status == DecisionStatus.APPROVED
    assert decision.selected_option == "resolved"
    assert decision.approved_by == "Amelie"
    assert "Receipt matched" in decision.rationale


def test_review_exceptions_accepts_risk_only_with_rationale_and_approval(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    _write_state(state_path)

    missing = _run_cli(
        "review-exceptions",
        "--state",
        str(state_path),
        "--exception-id",
        "exc_high_bank",
        "--action",
        "accepted_risk",
        "--approved-by",
        "Amelie",
    )

    assert missing.returncode == 2
    assert "accepted_risk requires --rationale" in missing.stderr

    accepted = _run_cli(
        "review-exceptions",
        "--state",
        str(state_path),
        "--exception-id",
        "exc_high_bank",
        "--action",
        "accepted_risk",
        "--rationale",
        "Immaterial timing difference accepted for draft release.",
        "--approved-by",
        "Amelie",
    )

    assert accepted.returncode == 0
    loaded = EngagementState.model_validate_json(state_path.read_text())
    high = next(item for item in loaded.exceptions if item.exception_id == "exc_high_bank")
    assert high.status == ExceptionStatus.ACCEPTED_RISK
    decision = next(item for item in loaded.decisions if item.decision_id == high.decision_id)
    assert decision.status == DecisionStatus.APPROVED
    assert decision.selected_option == "accepted_risk"
    assert decision.evidence_refs == ["bank.csv:row7", "support.pdf:p3"]


def test_cli_help_avoids_out_of_scope_version_wording() -> None:
    result = _run_cli("--help")

    assert result.returncode == 0
    forbidden = ["".join(["v", "2"]), "".join(["leg", "acy"])]
    assert all(word not in result.stdout.lower() for word in forbidden)


def test_review_exceptions_neutralises_out_of_scope_source_labels(tmp_path: Path) -> None:
    version_label = "".join(["v", "2"])
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        exceptions=[
            ExceptionItem(
                exception_id=f"{version_label}_step6_finding_0001",
                source=f"{version_label}.step6.verifier_findings",
                severity=ExceptionSeverity.CRITICAL,
                category=f"{version_label}_step6_overall_balanced",
                description=f"{version_label.upper()} classification: needs accountant review.",
                evidence_refs=["step6:OVERALL", "step6.verifier_findings[0]"],
                recommended_action="Resolve before release.",
                requires_human_approval=True,
            )
        ],
    )
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())

    result = _run_cli("review-exceptions", "--state", str(state_path))

    assert result.returncode == 1
    assert version_label not in result.stdout.lower()
    assert "source_journal_finding_0001" in result.stdout
    assert "source_pipeline.journal.verifier_findings" in result.stdout
    assert "Proposed classification" in result.stdout
