from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
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


def _write_reviewed_state(path: Path) -> None:
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        exceptions=[
            ExceptionItem(
                exception_id="exc_resolved_bank",
                source="matching_agent",
                severity=ExceptionSeverity.HIGH,
                category="unmatched_bank_transaction",
                description="Bank receipt initially had no support.",
                evidence_refs=["bank.csv:row7", "support.pdf:p3"],
                recommended_action="Classify receipt or request support.",
                status=ExceptionStatus.RESOLVED,
                requires_human_approval=True,
                decision_id="decision_exc_resolved_bank_0001",
            ),
            ExceptionItem(
                exception_id="exc_open_journal",
                source="journal_agent",
                severity=ExceptionSeverity.CRITICAL,
                category="journal_overall_balanced",
                description="Journal does not balance.",
                evidence_refs=["journal:OVERALL"],
                recommended_action="Resolve balancing difference before release.",
                requires_human_approval=True,
            ),
            ExceptionItem(
                exception_id="exc_accepted_rounding",
                source="statement_agent",
                severity=ExceptionSeverity.LOW,
                category="rounding_note",
                description="Rounding difference is immaterial.",
                evidence_refs=["workbook.xlsx:Notes"],
                recommended_action="Document accepted rounding treatment.",
                status=ExceptionStatus.ACCEPTED_RISK,
                requires_human_approval=True,
                decision_id="decision_exc_accepted_rounding_0002",
            ),
        ],
        decisions=[
            AccountantDecision(
                decision_id="decision_exc_resolved_bank_0001",
                question="How should exception exc_resolved_bank be handled?",
                selected_option="resolved",
                rationale="Receipt matched to approved dividend support.",
                status=DecisionStatus.APPROVED,
                approved_by="Amelie",
                evidence_refs=["bank.csv:row7", "support.pdf:p3"],
            ),
            AccountantDecision(
                decision_id="decision_exc_accepted_rounding_0002",
                question="How should exception exc_accepted_rounding be handled?",
                selected_option="accepted_risk",
                rationale="Immaterial $1 rounding difference accepted for draft release.",
                status=DecisionStatus.APPROVED,
                approved_by="Amelie",
                evidence_refs=["workbook.xlsx:Notes"],
            ),
        ],
    )
    path.write_text(state.model_dump_json())


def test_export_audit_trail_writes_markdown_with_readiness_exceptions_and_decisions(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    output_path = tmp_path / "audit_trail.md"
    _write_reviewed_state(state_path)

    result = _run_cli("export-audit-trail", "--state", str(state_path), "--output", str(output_path))

    assert result.returncode == 1
    assert f"Exported audit trail → {output_path}" in result.stdout
    report = output_path.read_text()
    assert report.startswith("# Audit Trail — XYZ Trust")
    assert "Engagement ID: xyz_fy2025" in report
    assert "Entity type: discretionary_trust" in report
    assert "FY: 2024-07-01 to 2025-06-30" in report
    assert "Final output allowed: NO" in report
    assert "Readiness: Final output blocked by open critical exception(s)." in report
    assert "## Exceptions" in report
    assert "### exc_open_journal — critical / open" in report
    assert "Blocking: yes" in report
    assert "Evidence: journal:OVERALL" in report
    assert "### exc_resolved_bank — high / resolved" in report
    assert "Decision: decision_exc_resolved_bank_0001" in report
    assert "Approved by: Amelie" in report
    assert "Rationale: Receipt matched to approved dividend support." in report
    assert "### exc_accepted_rounding — low / accepted_risk" in report
    assert "Rationale: Immaterial $1 rounding difference accepted for draft release." in report
    assert "## Accountant decisions" in report
    assert "Selected option: accepted_risk" in report


def test_export_audit_trail_prints_markdown_when_no_output_path(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    _write_reviewed_state(state_path)

    result = _run_cli("export-audit-trail", "--state", str(state_path))

    assert result.returncode == 1
    assert "# Audit Trail — XYZ Trust" in result.stdout
    assert "## Release readiness" in result.stdout
    assert "exc_open_journal" in result.stdout


def test_export_audit_trail_neutralises_out_of_scope_source_labels(tmp_path: Path) -> None:
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
                evidence_refs=["step6:OVERALL"],
                recommended_action="Resolve before release.",
                requires_human_approval=True,
            )
        ],
    )
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())

    result = _run_cli("export-audit-trail", "--state", str(state_path))

    assert result.returncode == 1
    assert version_label not in result.stdout.lower()
    assert "source_journal_finding_0001" in result.stdout
    assert "source_pipeline.journal.verifier_findings" in result.stdout
    assert "Proposed classification" in result.stdout
