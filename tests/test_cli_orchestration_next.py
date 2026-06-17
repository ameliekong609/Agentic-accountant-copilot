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
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        **kwargs,
    )
    path.write_text(state.model_dump_json())


def test_run_engagement_exports_review_packet_when_blocked(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    packet_dir = tmp_path / "review_packet"
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

    result = _run_cli("run-engagement", "--state", str(state_path), "--review-packet-dir", str(packet_dir))

    assert result.returncode == 1
    assert "Engagement blocked" in result.stdout
    assert (packet_dir / "README.md").exists()
    loaded = json.loads(state_path.read_text())
    assert loaded["state_transitions"][-1]["command"] == "run-engagement"
    assert loaded["state_transitions"][-1]["before_hash"] != loaded["state_transitions"][-1]["after_hash"]


def test_run_engagement_clean_path_records_output_and_manifest(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    manifest = tmp_path / "release_manifest.json"
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

    result = _run_cli("run-engagement", "--state", str(state_path), "--release-manifest", str(manifest))

    assert result.returncode == 0
    assert "Engagement ready" in result.stdout
    data = json.loads(manifest.read_text())
    assert data["lifecycle_status"] == "released"
    assert data["final_state_hash"]


def test_evidence_can_link_to_document_and_review_packet_groups_it(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    doc_path = tmp_path / "bank.csv"
    packet_dir = tmp_path / "packet"
    doc_path.write_text("date,amount\n2025-01-10,1000\n")
    _write_state(state_path)
    _run_cli(
        "record-document", "--state", str(state_path), "--document-id", "doc_bank", "--file-path", str(doc_path),
        "--document-type", "bank_statement", "--entity", "XYZ Trust", "--period-start", "2025-01-01", "--period-end", "2025-01-31",
    )

    recorded = _run_cli(
        "record-evidence", "--state", str(state_path), "--evidence-id", "ev_bank_1", "--source-type", "bank_statement",
        "--file-path", str(doc_path), "--document-id", "doc_bank", "--row", "2", "--quote", "2025-01-10,1000",
    )
    assert recorded.returncode == 0
    loaded = json.loads(state_path.read_text())
    assert loaded["evidence"][0]["document_id"] == "doc_bank"

    _run_cli("export-review-packet", "--state", str(state_path), "--output-dir", str(packet_dir))
    summary = (packet_dir / "evidence_summary.md").read_text()
    assert "doc_bank" in summary
    assert "ev_bank_1" in summary


def test_import_verifier_result_records_output_and_blocks_on_failure(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    verifier = tmp_path / "verifier.json"
    _write_state(state_path)
    verifier.write_text(json.dumps({
        "output_id": "out_fs",
        "file_path": "outputs/fs.xlsx",
        "artifact_type": "financial_statements",
        "status": "failed",
        "findings": [{"check": "workbook_balanced", "detail": "Trial balance out by 100"}],
    }))

    result = _run_cli("import-verifier-result", "--state", str(state_path), "--verifier-result", str(verifier))

    assert result.returncode == 1
    loaded = json.loads(state_path.read_text())
    assert loaded["output_artifacts"][0]["verifier_status"] == "failed"
    assert loaded["exceptions"][-1]["category"] == "output_workbook_balanced"
    assert loaded["exceptions"][-1]["severity"] == "critical"


def test_recommend_templates_outputs_entity_type_rules(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(state_path)

    result = _run_cli("recommend-templates", "--state", str(state_path))

    assert result.returncode == 0
    assert "discretionary_trust" in result.stdout
    assert "beneficiary distributions" in result.stdout.lower()


def test_export_review_ui_creates_static_html(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    output = tmp_path / "review.html"
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

    result = _run_cli("export-review-ui", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 1
    text = output.read_text()
    assert "<html" in text.lower()
    assert "exc_bank" in text
    assert "Accountant Review" in text
