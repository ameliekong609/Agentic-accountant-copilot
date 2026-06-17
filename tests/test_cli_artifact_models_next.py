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


def _write_state(path: Path, **kwargs) -> None:
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        **kwargs,
    )
    path.write_text(state.model_dump_json())


def test_review_packet_round_trip_filled_template_updates_readiness(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    packet = tmp_path / "packet"
    _write_state(
        state_path,
        exceptions=[ExceptionItem(
            exception_id="exc_bank",
            source="matching_agent",
            severity=ExceptionSeverity.HIGH,
            category="unmatched_bank_transaction",
            description="Needs review.",
            recommended_action="Classify item.",
            requires_human_approval=True,
        )],
    )

    exported = _run_cli("export-review-packet", "--state", str(state_path), "--output-dir", str(packet))
    assert exported.returncode == 1
    template_path = packet / "review_decisions_template.json"
    blank_apply = _run_cli("review-exceptions", "--state", str(state_path), "--decisions", str(template_path))
    assert blank_apply.returncode == 2
    assert "requires rationale" in blank_apply.stderr

    template = json.loads(template_path.read_text())
    template["decisions"][0]["action"] = "resolved"
    template["decisions"][0]["rationale"] = "Matched to support."
    template["decisions"][0]["approved_by"] = "Amelie"
    template_path.write_text(json.dumps(template))

    applied = _run_cli("review-exceptions", "--state", str(state_path), "--decisions", str(template_path))
    assert applied.returncode == 0
    assert "Final output allowed: YES" in applied.stdout


def test_record_document_registers_hash_and_review_packet_lists_documents(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    doc = tmp_path / "bank.csv"
    packet = tmp_path / "packet"
    doc.write_text("date,description,amount\n2025-01-10,Dividend,1000\n")
    _write_state(state_path)

    recorded = _run_cli(
        "record-document",
        "--state", str(state_path),
        "--document-id", "doc_bank_001",
        "--file-path", str(doc),
        "--document-type", "bank_statement",
        "--entity", "XYZ Trust",
        "--period-start", "2025-01-01",
        "--period-end", "2025-01-31",
    )
    assert recorded.returncode == 0
    loaded = json.loads(state_path.read_text())
    assert loaded["source_documents"][0]["document_id"] == "doc_bank_001"
    assert len(loaded["source_documents"][0]["source_hash"]) == 64

    listed = _run_cli("list-documents", "--state", str(state_path))
    assert "doc_bank_001" in listed.stdout
    assert "bank_statement" in listed.stdout

    _run_cli("export-review-packet", "--state", str(state_path), "--output-dir", str(packet))
    assert "doc_bank_001" in (packet / "document_summary.md").read_text()


def test_structured_coa_account_review_and_approval(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(state_path, coa_review_required=True)

    added = _run_cli(
        "record-coa-account",
        "--state", str(state_path),
        "--account-id", "acct_cash",
        "--code", "1000",
        "--name", "Cash at Bank",
        "--type", "asset",
        "--presentation-group", "Current assets",
        "--opening-balance", "1000.00",
        "--evidence-ref", "ev_bank_row_1",
    )
    assert added.returncode == 0
    review = _run_cli("review-coa", "--state", str(state_path))
    assert "acct_cash" in review.stdout
    assert review.returncode == 1

    approved = _run_cli(
        "approve-coa",
        "--state", str(state_path),
        "--account-id", "acct_cash",
        "--approved-by", "Amelie",
        "--rationale", "Account classification approved.",
    )
    assert approved.returncode == 0
    loaded = json.loads(state_path.read_text())
    assert loaded["chart_accounts"][0]["status"] == "approved"


def test_structured_adjustment_proposal_approve_registers_decision(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(state_path)

    proposed = _run_cli(
        "record-adjustment",
        "--state", str(state_path),
        "--adjustment-id", "adj_dist",
        "--description", "Year-end distribution accrual",
        "--debit-account", "Distribution expense",
        "--credit-account", "Distribution payable",
        "--amount", "5000.00",
        "--date", "2025-06-30",
        "--evidence-ref", "ev_distribution",
    )
    assert proposed.returncode == 0
    review = _run_cli("review-adjustments", "--state", str(state_path))
    assert "adj_dist" in review.stdout
    assert review.returncode == 1

    approved = _run_cli(
        "approve-adjustment",
        "--state", str(state_path),
        "--adjustment-id", "adj_dist",
        "--approved-by", "Amelie",
        "--rationale", "Distribution accrual approved.",
    )
    assert approved.returncode == 0
    loaded = json.loads(state_path.read_text())
    assert loaded["adjustment_proposals"][0]["status"] == "approved"
    assert loaded["adjustment_proposals"][0]["decision_id"]


def test_output_artifact_registry_blocks_release_on_failed_verifier(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    manifest = tmp_path / "manifest.json"
    _write_state(
        state_path,
        decisions=[AccountantDecision(
            decision_id="decision_final_signoff_0001",
            question="release?",
            selected_option="final_signoff",
            rationale="approved",
            status=DecisionStatus.APPROVED,
            approved_by="Amelie",
        )],
    )

    recorded = _run_cli(
        "record-output",
        "--state", str(state_path),
        "--output-id", "out_fs",
        "--file-path", "outputs/fs.xlsx",
        "--artifact-type", "financial_statements",
        "--verifier-status", "failed",
    )
    assert recorded.returncode == 0

    blocked = _run_cli("export-release-manifest", "--state", str(state_path), "--output", str(manifest))
    assert blocked.returncode == 1
    assert "verifier status is not passing" in blocked.stderr

    passed = _run_cli(
        "record-output",
        "--state", str(state_path),
        "--output-id", "out_fs",
        "--file-path", "outputs/fs.xlsx",
        "--artifact-type", "financial_statements",
        "--verifier-status", "passed",
    )
    assert passed.returncode == 0
    released = _run_cli("export-release-manifest", "--state", str(state_path), "--output", str(manifest))
    assert released.returncode == 0
    data = json.loads(manifest.read_text())
    assert "out_fs" in data["output_artifact_ids"]


def test_ci_workflow_exists() -> None:
    workflow = ROOT / ".github" / "workflows" / "test.yml"
    assert workflow.exists()
    text = workflow.read_text()
    assert "python-version: '3.11'" in text
    assert "pytest -q" in text
    assert "compileall" in text
