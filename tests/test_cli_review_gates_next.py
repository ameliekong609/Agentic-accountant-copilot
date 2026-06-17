from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity

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


def _write_state(path: Path, **kwargs) -> EngagementState:
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        **kwargs,
    )
    path.write_text(state.model_dump_json())
    return state


def test_source_import_creates_structured_evidence_refs(tmp_path: Path) -> None:
    matching = tmp_path / "matching.json"
    journal = tmp_path / "journal.json"
    state_path = tmp_path / "state.json"
    matching.write_text(json.dumps({
        "unmatched_bank": [{
            "statement_id": "bank_1",
            "row_index": 7,
            "date": "2025-01-10",
            "description": "Dividend receipt",
            "amount": 1000,
            "direction": "credit",
        }],
        "unmatched_events": [{
            "event_id": "event_1",
            "event_type": "distribution",
            "counterparty": "Fund Manager",
            "date": "2025-01-10",
            "net_cash_amount": 1000,
            "source_file": "distribution.pdf",
        }],
    }))
    journal.write_text(json.dumps({
        "verifier_findings": [{
            "check": "overall_balanced",
            "row_name": "journal total",
            "detail": "Difference 1000",
            "file": "journals.json",
        }]
    }))

    result = _run_cli(
        "import-source-exceptions",
        "--matching", str(matching),
        "--journal", str(journal),
        "--output", str(state_path),
        "--engagement-id", "xyz_fy2025",
        "--entity-name", "XYZ Trust",
        "--fy-start", "2024-07-01",
        "--fy-end", "2025-06-30",
    )

    assert result.returncode == 0
    loaded = json.loads(state_path.read_text())
    assert len(loaded["exceptions"]) == 3
    assert len(loaded["evidence"]) == 3
    evidence_ids = {item["evidence_id"] for item in loaded["evidence"]}
    assert "ev_unmatched_bank_0001" in evidence_ids
    assert "ev_unmatched_event_0001" in evidence_ids
    assert "ev_journal_finding_0001" in evidence_ids
    assert loaded["exceptions"][0]["evidence_refs"] == ["ev_unmatched_bank_0001"]


def test_coa_approval_gate_blocks_and_then_allows_signoff(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(state_path, coa_review_required=True)

    blocked = _run_cli("inspect-engagement", "--state", str(state_path))
    assert blocked.returncode == 1
    assert "CoA approval pending" in blocked.stdout

    review = _run_cli("review-coa", "--state", str(state_path))
    assert review.returncode == 1
    assert "CoA review" in review.stdout

    approved = _run_cli(
        "approve-coa",
        "--state", str(state_path),
        "--approved-by", "Amelie",
        "--rationale", "CoA presentation and opening balances approved.",
    )
    assert approved.returncode == 0
    assert "CoA approved by Amelie" in approved.stdout
    loaded = EngagementState.model_validate_json(state_path.read_text())
    assert loaded.coa_review_status == "approved"
    assert loaded.decisions[-1].selected_option == "approve_coa"

    allowed = _run_cli("inspect-engagement", "--state", str(state_path))
    assert allowed.returncode == 0
    assert "Final output allowed: YES" in allowed.stdout


def test_adjustment_gate_review_approve_and_reject(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        exceptions=[
            ExceptionItem(
                exception_id="adj_001",
                source="source_pipeline.journal.verifier_findings",
                severity=ExceptionSeverity.HIGH,
                category="journal_adjustment_review",
                description="Year-end adjustment needs approval.",
                recommended_action="Approve or reject proposed adjustment.",
                requires_human_approval=True,
            )
        ],
    )

    review = _run_cli("review-adjustments", "--state", str(state_path))
    assert review.returncode == 1
    assert "Adjustment review" in review.stdout
    assert "adj_001" in review.stdout

    approved = _run_cli(
        "approve-adjustment",
        "--state", str(state_path),
        "--exception-id", "adj_001",
        "--approved-by", "Amelie",
        "--rationale", "Adjustment ties to support.",
    )
    assert approved.returncode == 0
    loaded = EngagementState.model_validate_json(state_path.read_text())
    assert loaded.exceptions[0].status.value == "resolved"
    assert loaded.decisions[-1].selected_option == "approve_adjustment"

    _write_state(
        state_path,
        exceptions=[
            ExceptionItem(
                exception_id="adj_002",
                source="source_pipeline.journal.verifier_findings",
                severity=ExceptionSeverity.HIGH,
                category="journal_adjustment_review",
                description="Bad adjustment.",
                recommended_action="Approve or reject proposed adjustment.",
                requires_human_approval=True,
            )
        ],
    )
    rejected = _run_cli(
        "reject-adjustment",
        "--state", str(state_path),
        "--exception-id", "adj_002",
        "--approved-by", "Amelie",
        "--rationale", "Adjustment does not tie to support.",
    )
    assert rejected.returncode == 1
    loaded = EngagementState.model_validate_json(state_path.read_text())
    assert loaded.exceptions[0].status.value == "rejected"
    assert loaded.decisions[-1].selected_option == "reject_adjustment"


def test_lifecycle_status_updates_and_release_marks_released(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    output = tmp_path / "manifest.json"
    _write_state(
        state_path,
        coa_review_required=False,
        decisions=[AccountantDecision(
            decision_id="decision_final_signoff_0001",
            question="release?",
            selected_option="final_signoff",
            rationale="approved",
            status=DecisionStatus.APPROVED,
            approved_by="Amelie",
        )],
    )

    inspected = _run_cli("inspect-engagement", "--state", str(state_path))
    assert inspected.returncode == 0
    assert "Lifecycle status: signed_off" in inspected.stdout

    manifest = _run_cli("export-release-manifest", "--state", str(state_path), "--output", str(output))
    assert manifest.returncode == 0
    loaded = EngagementState.model_validate_json(state_path.read_text())
    assert loaded.lifecycle_status == "released"
    assert json.loads(output.read_text())["lifecycle_status"] == "released"


def test_export_review_packet_contains_accountant_handoff_files(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    packet_dir = tmp_path / "packet"
    _write_state(
        state_path,
        exceptions=[ExceptionItem(
            exception_id="exc_review",
            source="matching_agent",
            severity=ExceptionSeverity.HIGH,
            category="unmatched_bank_transaction",
            description="Review item.",
            recommended_action="Classify item.",
            requires_human_approval=True,
        )],
    )

    result = _run_cli("export-review-packet", "--state", str(state_path), "--output-dir", str(packet_dir))

    assert result.returncode == 1
    assert (packet_dir / "README.md").exists()
    assert (packet_dir / "open_exceptions.md").exists()
    assert (packet_dir / "review_decisions_template.json").exists()
    assert (packet_dir / "evidence_summary.md").exists()
    assert (packet_dir / "preference_recommendations.md").exists()
    assert "What the accountant needs to decide" in (packet_dir / "README.md").read_text()
