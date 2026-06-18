from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "accountant_copilot" / "review_app.py"


def run_cli(*args: str):
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=ROOT,
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )


def test_streamlit_review_app_starts_with_document_upload_and_control_tabs():
    assert APP.exists()
    source = APP.read_text()
    assert "st.file_uploader" in source
    assert "Upload source documents" in source
    assert "Accountant Review" in source
    assert "Release blockers" in source
    assert "apply-accountant-review-workbench" in source
    assert "accept_multiple_files=True" in source
    assert "Source Extraction Review" in source
    assert "Run intake" in source
    assert "Extract accounting facts" in source
    assert "Build review packet" in source
    assert "Build release candidate" in source
    assert "Final export" in source


def test_source_review_items_explain_incomplete_and_wrong_type_findings(tmp_path: Path):
    from accountant_copilot.review_app import _source_review_items

    (tmp_path / "invoice_facts.json").write_text(json.dumps({
        "findings": [
            {
                "category": "invoice_fact_extraction_incomplete",
                "document_id": "raw_017",
                "evidence_id": "raw_017_page_001",
                "recommended_action": "Review invoice OCR/text and improve parser or record an accountant decision.",
            }
        ]
    }))
    (tmp_path / "distribution_tax_facts.json").write_text(json.dumps({
        "findings": [
            {
                "category": "distribution_tax_fact_extraction_incomplete",
                "document_id": "raw_008",
                "recommended_action": "Review distribution/tax statement evidence.",
            }
        ]
    }))
    (tmp_path / "engagement_state.json").write_text(json.dumps({
        "source_documents": [
            {"document_id": "raw_017", "file_path": "inputs/Confirmation-SELL.PDF", "document_type": "broker_confirmation"},
            {"document_id": "raw_008", "file_path": "inputs/AN3_Payment_Advice.pdf", "document_type": "investment_statement"},
        ]
    }))

    items = _source_review_items(tmp_path)

    assert len(items) == 2
    assert items[0]["issue_type"] == "wrong document-type candidate"
    assert items[0]["file_path"] == "inputs/Confirmation-SELL.PDF"
    assert items[0]["blocks_release"] is True
    assert items[1]["issue_type"] == "incomplete extraction"
    assert "Review distribution" in items[1]["recommended_action"]


def test_guided_workflow_commands_are_available_for_ui():
    from accountant_copilot.review_app import _workflow_steps

    steps = _workflow_steps("inputs", "outputs/raw_inputs_pdf_extraction", "outputs/raw_inputs_pdf_extraction/engagement_state.json")
    labels = [step["label"] for step in steps]

    assert labels[:4] == ["Run intake", "Build document inventory", "Extract accounting facts", "Match source facts"]
    assert "Build review packet" in labels
    assert "Build release candidate" in labels
    assert "Final export" in labels
    assert all(step["command"] for step in steps)


def test_serve_accountant_review_ui_command_is_registered():
    result = run_cli("serve-accountant-review-ui", "--help")
    assert result.returncode == 0
    assert "Streamlit" in result.stdout
    assert "--state" in result.stdout
    assert "--artifact-dir" in result.stdout
    assert "--input-dir" in result.stdout
