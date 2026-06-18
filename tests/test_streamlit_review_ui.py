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
    from accountant_copilot.review_app import _main_tab_labels

    assert APP.exists()
    source = APP.read_text()
    assert "st.file_uploader" in source
    assert "Upload source documents" in source
    assert "Accountant Review" in source
    assert "Release blockers" in source
    assert "apply-accountant-review-workbench" in source
    assert "accept_multiple_files=True" in source
    assert "Accounting facts" in source
    assert "Process documents and build inventory" in source
    assert "Run intake" not in source
    assert "Build document inventory" not in source
    assert "Extract accounting facts" in source
    assert "Build review packet" in source
    assert "Build release candidate" in source
    assert "Final export" in source
    assert "Workspace details" not in source
    assert "Advanced technical paths" not in source
    assert "Technical" not in source
    assert "State path" not in source
    assert "Artifact directory" not in source
    assert "Status dashboard" in source
    assert "Reviewed Trial Balance" in source
    assert "Draft Statements" in source
    assert "Final Output" in source
    assert "Default reviewer" in source
    assert "Default rationale" in source
    assert "Approve all visible CoA accounts" in source
    assert "Accounting facts" in source
    assert "Editable CoA review table" in source
    assert "Build reviewed TB and draft statements" in source
    assert _main_tab_labels()[0] == "1 Upload source documents"
    assert "0 Engagement setup" not in _main_tab_labels()
    assert "2 Intake & inventory" in _main_tab_labels()
    assert "3 Extract facts" in _main_tab_labels()
    assert "4 Match & review sources" in _main_tab_labels()
    assert "5 CoA & mappings" in _main_tab_labels()
    assert "6 Trial balance & statements" in _main_tab_labels()
    assert "Document inventory review" in source


def test_upload_step_does_not_show_stale_workspace_dashboard_before_upload():
    from accountant_copilot.review_app import DEFAULT_ARTIFACT_DIR, DEFAULT_INPUT_DIR, DEFAULT_STATE

    source = APP.read_text()

    assert "with upload_tab:\n        _render_status_dashboard(artifact_dir)" not in source
    assert DEFAULT_ARTIFACT_DIR == Path("outputs/streamlit_review_workspace")
    assert DEFAULT_STATE == Path("outputs/streamlit_review_workspace/engagement_state.json")
    assert DEFAULT_INPUT_DIR == Path("outputs/streamlit_review_workspace/uploads")


def test_streamlit_workflow_initializes_clean_engagement_state(tmp_path: Path):
    from accountant_copilot.review_app import _ensure_engagement_state_for_step, _workflow_steps

    upload_dir = tmp_path / "uploads"
    state_path = tmp_path / "engagement_state.json"
    steps = _workflow_steps(str(upload_dir), str(tmp_path), str(state_path))

    created = _ensure_engagement_state_for_step(steps[0])

    assert created == state_path
    assert upload_dir.is_dir()
    state = json.loads(state_path.read_text())
    assert state["engagement_id"] == "streamlit_local"
    assert state["entity_name"] == "Uploaded engagement"
    assert state["source_documents"] == []


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


def test_accounting_fact_rows_show_document_level_output_with_multiple_facts(tmp_path: Path):
    from accountant_copilot.review_app import _accounting_fact_rows

    (tmp_path / "engagement_state.json").write_text(json.dumps({"source_documents": [
        {"document_id": "raw_001", "file_path": "inputs/bank.pdf", "document_type": "bank_statement"},
        {"document_id": "raw_002", "file_path": "inputs/an3.pdf", "document_type": "investment_statement"},
    ]}))
    (tmp_path / "bank_statement_facts.json").write_text(json.dumps({"facts": [{"document_id": "raw_001", "account_number": "123", "closing_balance": "100.00", "statement_period": "Jan 2025", "evidence_id": "raw_001_page_001"}], "findings": [{"document_id": "raw_001", "category": "page_noise"}]}))
    (tmp_path / "distribution_tax_facts.json").write_text(json.dumps({"facts": [{"document_id": "raw_002", "investment_name": "ANZ Capital Notes 9", "security_code": "AN3PL", "payment_date": "20 June 2024", "amount": "6,450.30", "evidence_id": "raw_002_page_001"}], "findings": []}))

    rows = _accounting_fact_rows(tmp_path)

    assert rows == [
        {"document": "bank.pdf", "document_type": "bank_statement", "fact_type": "bank_statement", "accounting_facts": "Account 123; period Jan 2025; closing balance 100.00", "evidence": "raw_001_page_001", "status": "extracted"},
        {"document": "an3.pdf", "document_type": "investment_statement", "fact_type": "distribution_tax", "accounting_facts": "ANZ Capital Notes 9; AN3PL; payment 20 June 2024; amount 6,450.30", "evidence": "raw_002_page_001", "status": "extracted"},
    ]


def test_extract_tab_renders_accounting_facts_not_triage_first():
    source = APP.read_text()
    extract_block = source.split("with extract_tab:", 1)[1].split("with match_tab:", 1)[0]
    match_block = source.split("with match_tab:", 1)[1].split("with coa_tab:", 1)[0]

    assert "_render_accounting_facts_output(artifact_dir)" in extract_block
    assert "_render_source_extraction_review(artifact_dir)" not in extract_block
    assert "_render_source_extraction_review(artifact_dir)" not in match_block


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

    assert labels[:3] == ["Process documents and build inventory", "Extract accounting facts", "Match source facts"]
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
    assert [step["label"] for step in groups[0]["steps"]] == ["Process documents and build inventory"]
    assert [step["label"] for step in groups[1]["steps"]] == ["Extract accounting facts"]


def test_intake_and_inventory_are_one_product_action():
    from accountant_copilot.review_app import _workflow_steps

    steps = _workflow_steps("inputs", "outputs/raw_inputs_pdf_extraction", "outputs/raw_inputs_pdf_extraction/engagement_state.json")
    by_label = {step["label"]: step for step in steps}

    assert "Run intake" not in by_label
    assert "Build document inventory" not in by_label
    intake_inventory = by_label["Process documents and build inventory"]
    assert intake_inventory["user_output"] == "Document inventory is ready for review."
    assert intake_inventory["command"][0][0] == "ingest-raw-inputs"
    assert intake_inventory["command"][1][0] == "export-document-inventory"


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

    assert by_label["Process documents and build inventory"]["user_output"] == "Document inventory is ready for review."
    assert by_label["Extract accounting facts"]["review_action"] == "Review extracted facts first, then any extraction review items before matching."
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


def test_workflow_orchestrator_does_not_show_ready_copy_before_run():
    import inspect
    from accountant_copilot.review_app import _render_workflow_orchestrator

    source = inspect.getsource(_render_workflow_orchestrator)

    assert "Output:" not in source
    assert "readiness_text" not in source
    assert "_workflow_output_readiness_text" not in source


def test_workflow_orchestrator_renders_inventory_review_once_per_pass():
    import inspect
    from accountant_copilot.review_app import _render_workflow_orchestrator

    source = inspect.getsource(_render_workflow_orchestrator)

    assert source.count("_render_document_inventory_review(artifact_dir)") == 1


def test_workflow_result_summary_hides_raw_stdout_for_good_ux():
    from subprocess import CompletedProcess
    from accountant_copilot.review_app import _workflow_result_summary

    step = {"label": "Process documents and build inventory", "outputs": ["engagement_state.json", "document_inventory.md"], "user_output": "Document inventory is ready for review."}
    result = CompletedProcess(args=["ingest-raw-inputs"], returncode=0, stdout="very long cli blob", stderr="")

    summary = _workflow_result_summary(step, [result], outputs_present=2, outputs_total=2)

    assert summary["status"] == "Done"
    assert summary["message"] == "Document inventory is ready for review."
    assert "2 of 2" not in summary["message"]
    assert "expected outputs" not in summary["message"]
    assert "cli" not in summary["message"].lower()
    assert summary["show_technical_output"] is False


def test_workflow_orchestrator_hides_technical_command_output_from_steps():
    import inspect
    from accountant_copilot.review_app import _render_workflow_orchestrator

    source = inspect.getsource(_render_workflow_orchestrator)

    assert "Technical command output" not in source
    assert "Exit code:" not in source


def test_workflow_result_summary_treats_exported_review_findings_as_review_state():
    from subprocess import CompletedProcess
    from accountant_copilot.review_app import _workflow_result_summary

    step = {"label": "Extract accounting facts", "outputs": ["bank_statement_facts.json", "invoice_facts.json"]}
    results = [
        CompletedProcess(args=["export-bank-statement-facts"], returncode=1, stdout="Exported bank statement facts JSON", stderr=""),
        CompletedProcess(args=["export-invoice-facts"], returncode=1, stdout="Exported invoice facts JSON", stderr=""),
    ]

    summary = _workflow_result_summary(step, results, outputs_present=2, outputs_total=2)

    assert summary["status"] == "Needs review"
    assert summary["message"] == "Review this step output before continuing."
    assert summary["show_technical_output"] is False


def test_serve_accountant_review_ui_command_is_registered():
    result = run_cli("serve-accountant-review-ui", "--help")
    assert result.returncode == 0
    assert "Streamlit" in result.stdout
    assert "--state" in result.stdout
    assert "--artifact-dir" in result.stdout
    assert "--input-dir" in result.stdout
