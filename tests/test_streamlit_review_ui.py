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
    assert "Engagement setup" in source
    assert "Status dashboard" in source
    assert "Reviewed Trial Balance" in source
    assert "Draft Statements" in source
    assert "Final Output" in source
    assert "Default reviewer" in source
    assert "Default rationale" in source
    assert "Approve all visible CoA accounts" in source
    assert "Resolve source issue" in source
    assert "Editable CoA review table" in source
    assert "Build reviewed TB and draft statements" in source
    assert "2 Intake & inventory" in source
    assert "3 Extract facts" in source
    assert "4 Match & review sources" in source
    assert "5 CoA & mappings" in source
    assert "6 Trial balance & statements" in source
    assert "Document inventory review" in source


def test_document_inventory_rows_are_reviewable_inline(tmp_path: Path):
    from accountant_copilot.review_app import _document_inventory_rows

    (tmp_path / "document_inventory.json").write_text(json.dumps({
        "documents": [
            {
                "document_id": "raw_001",
                "file_path": "inputs/bank.pdf",
                "document_type": "bank_statement",
                "evidence_count": 2,
                "status": "registered",
                "pages": [{"page": "1"}, {"page": "2"}],
            }
        ]
    }))

    rows = _document_inventory_rows(tmp_path)

    assert rows == [{
        "document_id": "raw_001",
        "file_name": "bank.pdf",
        "document_type": "bank_statement",
        "pages": 2,
        "evidence_count": 2,
        "status": "registered",
        "review": "looks_ok",
    }]


def test_dashboard_summary_surfaces_tb_draft_and_release_status(tmp_path: Path):
    from accountant_copilot.review_app import _dashboard_summary

    (tmp_path / "engagement_state.json").write_text(json.dumps({
        "entity_name": "Demo Trust",
        "fy_start": "2024-07-01",
        "fy_end": "2025-06-30",
        "chart_accounts": [{"account_id": "a1", "status": "pending_review"}],
        "adjustment_proposals": [{"adjustment_id": "j1", "status": "approved"}],
    }))
    (tmp_path / "release_blockers.json").write_text(json.dumps({"blockers": [{"category": "CoA"}, {"category": "final_signoff"}]}))
    (tmp_path / "post_journal_trial_balance.json").write_text(json.dumps({
        "summary": {"is_balanced": True, "pending_journals_excluded": 0},
        "accounts": [{"account_id": "a1", "opening_balance": "100", "approved_debits": "0", "approved_credits": "0", "ending_balance": "100"}],
    }))
    draft_dir = tmp_path / "draft_statements"
    draft_dir.mkdir()
    (draft_dir / "draft_statements.json").write_text(json.dumps({"status": "internal_review_only", "findings": [], "statements": {"balance_sheet": []}}))

    summary = _dashboard_summary(tmp_path)

    assert summary["entity_name"] == "Demo Trust"
    assert summary["release_blockers"] == 2
    assert summary["coa_pending"] == 1
    assert summary["approved_journals"] == 1
    assert summary["tb_balanced"] is True
    assert summary["draft_status"] == "internal_review_only"
    assert summary["next_action"].startswith("Review")


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
    assert "Build reviewed TB and draft statements" in labels
    assert "Build release candidate" in labels
    assert "Final export" in labels
    assert all(step["command"] for step in steps)


def test_source_resolution_payload_and_coa_table_rows_are_ui_friendly(tmp_path: Path):
    from accountant_copilot.review_app import _coa_review_rows, _save_source_resolution, _source_resolution_payload

    issue = {
        "layer": "invoice",
        "document_id": "raw_017",
        "file_path": "inputs/Confirmation-SELL.PDF",
        "issue_type": "wrong document-type candidate",
        "recommended_action": "Route to broker trade layer.",
    }
    payload = _source_resolution_payload(issue, action="mark_out_of_scope", reviewer="Amelie", rationale="Broker confirmation, not invoice")
    assert payload["document_id"] == "raw_017"
    assert payload["action"] == "mark_out_of_scope"
    assert payload["reviewer"] == "Amelie"
    assert payload["blocks_release"] is False
    saved_path = _save_source_resolution(tmp_path, payload)
    saved = json.loads(saved_path.read_text())
    assert saved["resolutions"] == [payload]

    rows = _coa_review_rows({"sections": {"coa_accounts": [{"account_id": "a1", "code": "100", "name": "Cash"}]}})
    assert rows == [{"account_id": "a1", "code": "100", "name": "Cash", "action": "", "approved_by": "", "rationale": ""}]


def test_final_package_preview_prioritizes_statement_and_manifest_artifacts(tmp_path: Path):
    from accountant_copilot.review_app import _final_package_preview

    (tmp_path / "draft_statements").mkdir()
    (tmp_path / "release_candidate").mkdir()
    (tmp_path / "draft_statements" / "draft_statements.md").write_text("# Draft statements")
    (tmp_path / "release_candidate" / "release_candidate_manifest.json").write_text(json.dumps({"status": "ready"}))
    (tmp_path / "final_release_manifest.json").write_text(json.dumps({"status": "signed"}))

    preview = _final_package_preview(tmp_path)

    assert preview[0]["label"] == "Draft financial statements"
    assert preview[0]["kind"] == "statement"
    assert preview[1]["label"] == "Release candidate manifest"
    assert preview[2]["label"] == "Final release manifest"


def test_workflow_stage_groups_break_apart_the_sequence():
    from accountant_copilot.review_app import _workflow_stage_groups, _workflow_steps

    steps = _workflow_steps("inputs", "outputs/raw_inputs_pdf_extraction", "outputs/raw_inputs_pdf_extraction/engagement_state.json")
    groups = _workflow_stage_groups(steps)

    assert [group["title"] for group in groups] == [
        "2 Intake & inventory",
        "3 Extract facts",
        "4 Match & review sources",
        "5 CoA & mappings",
        "6 Trial balance & statements",
        "7 Accountant review",
        "8 Final package",
    ]
    assert [step["label"] for step in groups[0]["steps"]] == ["Run intake", "Build document inventory"]
    assert [step["label"] for step in groups[1]["steps"]] == ["Extract accounting facts"]


def test_stage_step_button_keys_are_unique_across_tabs():
    from accountant_copilot.review_app import _workflow_stage_groups, _workflow_step_button_key, _workflow_steps

    steps = _workflow_steps("inputs", "outputs/raw_inputs_pdf_extraction", "outputs/raw_inputs_pdf_extraction/engagement_state.json")
    groups = _workflow_stage_groups(steps)

    keys = [
        _workflow_step_button_key(group["title"], idx, step)
        for group in groups
        for idx, step in enumerate(group["steps"], start=1)
    ]

    assert len(keys) == len(set(keys))
    assert "run_step_1" not in keys


def test_workflow_steps_embed_outputs_and_review_actions():
    from accountant_copilot.review_app import _workflow_steps

    steps = _workflow_steps("inputs", "outputs/raw_inputs_pdf_extraction", "outputs/raw_inputs_pdf_extraction/engagement_state.json")
    by_label = {step["label"]: step for step in steps}

    assert by_label["Run intake"]["user_output"] == "Documents are registered and ready for inventory."
    assert by_label["Extract accounting facts"]["review_action"] == "Review source extraction issues now, before matching."
    assert by_label["Build CoA and mappings"]["review_action"] == "Review and approve CoA/mapping suggestions now."
    assert by_label["Build reviewed TB and draft statements"]["review_action"] == "Review the trial balance and draft statements now."
    assert by_label["Build release candidate"]["review_action"] == "Review release package blockers now."


def test_workflow_output_readiness_text_is_product_facing():
    from accountant_copilot.review_app import _workflow_output_readiness_text

    assert _workflow_output_readiness_text("Document inventory", outputs_present=1, outputs_total=1) == "Document inventory is ready to review."
    assert _workflow_output_readiness_text("Document inventory", outputs_present=0, outputs_total=1) == "Document inventory is not ready yet."
    assert "Outputs present" not in _workflow_output_readiness_text("Document inventory", outputs_present=1, outputs_total=1)
    assert "1/1" not in _workflow_output_readiness_text("Document inventory", outputs_present=1, outputs_total=1)


def test_workflow_orchestrator_does_not_show_misleading_progress_bar():
    import inspect
    from accountant_copilot.review_app import _render_workflow_orchestrator

    source = inspect.getsource(_render_workflow_orchestrator)

    assert "st.progress" not in source


def test_workflow_orchestrator_renders_inventory_review_once_per_pass():
    import inspect
    from accountant_copilot.review_app import _render_workflow_orchestrator

    source = inspect.getsource(_render_workflow_orchestrator)

    assert source.count("_render_document_inventory_review(artifact_dir)") == 1


def test_workflow_result_summary_hides_raw_stdout_for_good_ux():
    from subprocess import CompletedProcess
    from accountant_copilot.review_app import _workflow_result_summary

    step = {"label": "Run intake", "outputs": ["engagement_state.json", "document_inventory.md"]}
    result = CompletedProcess(args=["ingest-raw-inputs"], returncode=0, stdout="very long cli blob", stderr="")

    summary = _workflow_result_summary(step, [result], outputs_present=2, outputs_total=2)

    assert summary["status"] == "Done"
    assert summary["message"] == "Run intake finished. 2 of 2 expected outputs are available."
    assert "cli" not in summary["message"].lower()
    assert summary["show_technical_output"] is False


def test_serve_accountant_review_ui_command_is_registered():
    result = run_cli("serve-accountant-review-ui", "--help")
    assert result.returncode == 0
    assert "Streamlit" in result.stdout
    assert "--state" in result.stdout
    assert "--artifact-dir" in result.stdout
    assert "--input-dir" in result.stdout
