from __future__ import annotations

import json
import os
import sys
import zipfile
from argparse import Namespace
from pathlib import Path

from accountant_copilot.cli import (
    _classify_raw_document_from_content,
    _codex_document_validation_error,
    _codex_process_document_prompt,
    _document_text_for_codex,
    _normalise_codex_document_result,
    _process_documents_command,
    _source_match_context,
)

ROOT = Path(__file__).resolve().parents[1]


def write_docx(path: Path, text: str) -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def write_xlsx(path: Path) -> None:
    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<si><t>Account</t></si><si><t>Cash at Bank</t></si><si><t>Balance</t></si>"
        "</sst>"
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>2</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>26152.26</v></c></row>'
        "</sheetData></worksheet>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", shared)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def test_document_text_for_codex_reads_docx_and_xlsx(tmp_path: Path):
    docx = tmp_path / "minutes.docx"
    xlsx = tmp_path / "trial_balance.xlsx"
    write_docx(docx, "Prior year financial statements for XYZ Trust")
    write_xlsx(xlsx)

    docx_pages, docx_text = _document_text_for_codex(docx)
    xlsx_pages, xlsx_text = _document_text_for_codex(xlsx)

    assert docx_pages[0]["evidence_id"] == "text_001"
    assert "Prior year financial statements" in docx_text
    assert xlsx_pages[0]["evidence_id"] == "sheet_text_001"
    assert "Cash at Bank" in xlsx_text
    assert "26152.26" in xlsx_text


def test_process_documents_writes_per_doc_outputs_and_uses_cache(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "bank.md").write_text("Commonwealth Bank Statement Account 027 Closing Balance $1,234.56")
    artifact_dir = tmp_path / "outputs"
    fake_codex_result = {
        "display_name": "2024-12-31 - Commonwealth Bank Statement - Account 027.md",
        "document_type": "bank_statement",
        "naming_confidence": "high",
        "naming_evidence_refs": ["text_001"],
        "status": "indexed",
        "document_summary": "Commonwealth Bank statement for account 027.",
        "entity_relevance": "relevant",
        "primary_amounts": [{"label": "closing balance", "amount": "1234.56", "currency": "AUD", "evidence_id": "text_001"}],
    }
    args = Namespace(input_dir=str(input_dir), artifact_dir=str(artifact_dir), codex_command="codex", codex_timeout=1)

    monkeypatch.setenv("ACCOUNTANT_COPILOT_FAKE_CODEX_DOCUMENT_JSON", json.dumps(fake_codex_result))
    assert _process_documents_command(args) == 0
    monkeypatch.delenv("ACCOUNTANT_COPILOT_FAKE_CODEX_DOCUMENT_JSON")
    assert _process_documents_command(args) == 0

    per_doc = json.loads((artifact_dir / "per_document" / "raw_001.json").read_text())
    progress = json.loads((artifact_dir / "document_processing_progress.json").read_text())
    grouped = json.loads((artifact_dir / "accounting_facts_by_document.json").read_text())

    assert per_doc["display_name"] == "2024-12-31 - Commonwealth Bank Statement - Account 027.md"
    assert per_doc["processing_source"] == "cache"
    assert "accounting_facts" not in per_doc
    assert per_doc["document_summary"] == "Commonwealth Bank statement for account 027."
    assert progress["status"] == "complete"
    assert progress["cache_hits"] == 1
    assert progress["codex_attempts"] == 0
    assert progress["codex_successes"] == 0
    assert progress["batch_size"] == 1
    assert progress["current_batch"] == 1
    assert progress["total_batches"] == 1
    assert progress["facts_extracted"] == 0
    assert grouped["artifact_type"] == "source_document_index"
    assert grouped["summary"]["accounting_fact_rows"] == 0
    assert (artifact_dir / "source_document_index.json").exists()


def test_process_documents_includes_nested_uploaded_files(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "inputs"
    nested = input_dir / "bank" / "westpac"
    nested.mkdir(parents=True)
    (nested / "statement.md").write_text("Westpac statement closing balance $10")
    artifact_dir = tmp_path / "outputs"
    fake_codex_result = {
        "display_name": "2025-06-30 - Westpac Bank Statement.md",
        "document_type": "bank_statement",
        "naming_confidence": "high",
        "naming_evidence_refs": ["text_001"],
        "status": "indexed",
        "document_summary": "Westpac bank statement.",
        "entity_relevance": "relevant",
        "primary_amounts": [{"label": "closing balance", "amount": "10.00", "currency": "AUD", "evidence_id": "text_001"}],
    }
    args = Namespace(input_dir=str(input_dir), artifact_dir=str(artifact_dir), codex_command="codex", codex_timeout=1)

    monkeypatch.setenv("ACCOUNTANT_COPILOT_FAKE_CODEX_DOCUMENT_JSON", json.dumps(fake_codex_result))
    assert _process_documents_command(args) == 0

    inventory = json.loads((artifact_dir / "source_document_index.json").read_text())
    assert len(inventory["documents"]) == 1
    assert inventory["documents"][0]["original_file_name"] == "statement.md"


def test_process_documents_force_reprocess_ignores_existing_cache(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "bank.md").write_text("Commonwealth Bank Statement Account 027 Closing Balance $1,234.56")
    artifact_dir = tmp_path / "outputs"
    first_payload = {
        "display_name": "First Bank Statement.md",
        "document_type": "bank_statement",
        "document_summary": "First bank statement.",
        "primary_amounts": [{"label": "closing balance", "amount": "1234.56"}],
    }
    second_payload = {
        "display_name": "Second Bank Statement.md",
        "document_type": "bank_statement",
        "document_summary": "Second bank statement.",
        "primary_amounts": [{"label": "closing balance", "amount": "9999.00"}],
    }
    args = Namespace(input_dir=str(input_dir), artifact_dir=str(artifact_dir), codex_command="codex", codex_timeout=1, force_reprocess=False)

    monkeypatch.setenv("ACCOUNTANT_COPILOT_FAKE_CODEX_DOCUMENT_JSON", json.dumps(first_payload))
    assert _process_documents_command(args) == 0
    monkeypatch.setenv("ACCOUNTANT_COPILOT_FAKE_CODEX_DOCUMENT_JSON", json.dumps(second_payload))
    args.force_reprocess = True
    assert _process_documents_command(args) == 0

    per_doc = json.loads((artifact_dir / "per_document" / "raw_001.json").read_text())
    progress = json.loads((artifact_dir / "document_processing_progress.json").read_text())

    assert per_doc["display_name"] == "Second Bank Statement.md"
    assert per_doc["document_summary"] == "Second bank statement."
    assert "accounting_facts" not in per_doc
    assert progress["cache_hits"] == 0
    assert progress["codex_successes"] == 1


def test_process_documents_recovery_attempt_passes_failure_context_and_increases_timeout(tmp_path: Path, monkeypatch):
    import accountant_copilot.document_indexing as document_indexing

    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "large.pdf").write_bytes(b"%PDF-1.4\n")
    artifact_dir = tmp_path / "outputs"
    calls = []

    def fake_codex_process_document(path, document_id, source_hash, command, timeout, *, recovery_attempt=0, previous_error=None):
        calls.append({"timeout": timeout, "recovery_attempt": recovery_attempt, "previous_error": previous_error})
        if len(calls) == 1:
            return None, "Codex command timed out after 5 seconds."
        return {
            "display_name": "Recovered Large Statement.pdf",
            "document_type": "source_document",
            "status": "indexed",
            "document_summary": "Recovered large source document.",
        }, None

    monkeypatch.setattr(document_indexing, "_codex_process_document", fake_codex_process_document)
    args = Namespace(input_dir=str(input_dir), artifact_dir=str(artifact_dir), codex_command="codex", codex_timeout=5, codex_max_attempts=3, force_reprocess=True)

    assert _process_documents_command(args) == 0

    per_doc = json.loads((artifact_dir / "per_document" / "raw_001.json").read_text())
    progress = json.loads((artifact_dir / "document_processing_progress.json").read_text())

    assert calls == [
        {"timeout": 5, "recovery_attempt": 0, "previous_error": None},
        {"timeout": 10, "recovery_attempt": 1, "previous_error": "Codex command timed out after 5 seconds."},
    ]
    assert per_doc["processing_source"] == "codex_cli"
    assert per_doc["codex_attempt_history"][1]["mode"] == "recovery"
    assert progress["codex_attempts"] == 2


def test_process_documents_fails_without_usable_codex_json_and_does_not_cache(tmp_path: Path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "bank.md").write_text("Commonwealth Bank Statement Account 027 Closing Balance $1,234.56")
    artifact_dir = tmp_path / "outputs"
    args = Namespace(input_dir=str(input_dir), artifact_dir=str(artifact_dir), codex_command=f"{sys.executable} -c 'print()'", codex_timeout=1, codex_max_attempts=1)

    assert _process_documents_command(args) == 1

    progress = json.loads((artifact_dir / "document_processing_progress.json").read_text())
    per_doc = json.loads((artifact_dir / "per_document" / "raw_001.json").read_text())

    assert progress["status"] == "failed"
    assert progress["failed_items"] == 1
    assert progress["codex_attempts"] == 1
    assert progress["codex_successes"] == 0
    assert progress["batch_size"] == 1
    assert per_doc["status"] == "processing_failed"
    assert per_doc["processing_source"] == "codex_cli_failed"
    assert not list((artifact_dir / ".codex_doc_cache").glob("*.json"))


def test_codex_processing_contract_is_source_index_only(tmp_path: Path):
    source = tmp_path / "trial-balance.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    prompt = json.loads(_codex_process_document_prompt(source, "raw_010", "hash"))
    schema = prompt["required_output_schema"]

    assert "trial_balance" in schema["document_type"]
    assert "prior_year_financial_statements" in schema["document_type"]
    assert "accounting_facts" not in schema
    assert prompt["source_index_contract"]["purpose"].endswith("Step 2 must not extract detailed accounting facts.")
    assert any("Do not extract accounting facts in Step 2" in rule for rule in prompt["rules"])


def test_content_classifier_recognises_trial_balance_invoice_and_capital_call(tmp_path: Path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n")

    assert _classify_raw_document_from_content(source, "source_document", "Trial Balance Account Code Debit Credit Closing Balance") == "trial_balance"
    assert _classify_raw_document_from_content(source, "source_document", "TAX INVOICE Invoice Number INV-123 Amount Due $100.00") == "invoice"
    assert _classify_raw_document_from_content(source, "source_document", "Capital Call Notice Called Amount $20,000 Due Date 30 June 2025") == "capital_call"


def test_capital_call_validation_requires_visible_payment_bank_account(tmp_path: Path):
    source = tmp_path / "capital-call.md"
    source.write_text(
        "Capital Call Notice\n"
        "Date of Capital Call 25-Jul-24\n"
        "Payment Due Date 08-Aug-24\n"
        "Capital Call $4,000.00\n"
        "EFT Account Name: AUTOMIC PTY LTD - EVP Fund III\n"
        "Bank: Westpac\n"
        "BSB: 036-000\n"
        "Reference: 2593155-11827-EVPIII\n"
    )
    missing_bank_payload = {
        "document_type": "capital_call",
        "accounting_facts": [
            {
                "fact_type": "capital_call",
                "fields": {
                    "called_amount": "4000.00",
                    "due_date": "2024-08-08",
                    "payment_reference": "2593155-11827-EVPIII",
                },
            }
        ],
    }
    complete_payload = {
        "document_type": "capital_call",
        "accounting_facts": [
            {
                "fact_type": "capital_call",
                "fields": {
                    "called_amount": "4000.00",
                    "due_date": "2024-08-08",
                    "bank_account": "AUTOMIC PTY LTD - EVP Fund III; Westpac; BSB 036-000",
                    "payment_reference": "2593155-11827-EVPIII",
                },
            }
        ],
    }

    assert "omitted bank_account/bank_name" in (_codex_document_validation_error(source, missing_bank_payload) or "")
    assert _codex_document_validation_error(source, complete_payload) is None


def test_source_match_context_carries_payment_instruction_evidence_for_source_first_matching():
    payload = {
        "entity_name": "Match Trust",
        "documents": [
            {
                "document_id": "raw_044",
                "display_name": "2024-07-25 - EVP Fund III Capital Call Notice.pdf",
                "document_type": "capital_call",
                "file_path": "inputs/capital-call.pdf",
                "page_quotes": [
                    {
                        "page": "1",
                        "evidence_id": "page_001",
                        "quote": "Capital Call $4,000.00 Payment Due Date 08-Aug-24 EFT Account Name AUTOMIC PTY LTD - EVP Fund III Bank: Westpac Reference 2593155-11827-EVPIII",
                    }
                ],
                "accounting_facts": [
                    {
                        "fact_type": "capital_call",
                        "evidence_id": "page_001",
                        "page": "1",
                        "fields": {"called_amount": "4000.00", "due_date": "2024-08-08"},
                    }
                ],
            }
        ],
    }

    context = _source_match_context(payload, None)
    evidence = context["source_document_evidence"][0]["payment_or_matching_evidence"][0]["excerpt"]

    assert "Source-first matching rule" in " ".join(context["rules"])
    assert "Westpac" in evidence
    assert "AUTOMIC" in evidence


def test_codex_bank_statement_source_index_drops_old_fact_payload(tmp_path: Path):
    source = tmp_path / "7A6AA8FA-4A80-4C13-B00D-4C0E985626CA.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    payload = {
        "display_name": "2024-11-30 - CommBank Bank Statement - Account 4068.9187.1.1.pdf",
        "document_type": "bank_statement",
        "accounting_facts": [
            {
                "fact_type": "bank_statement_period_balance",
                "page": "1",
                "evidence_id": "page_001",
                "confidence": "high",
                "snippet": "01 Nov 2024 OPENING BALANCE $64,925.01 CR ... 30 Nov 2024 CLOSING BALANCE $34,925.01 CR",
                "fields": {
                    "bank_name": "Commonwealth Bank",
                    "account_identifier": "4068.9187.1.1",
                    "statement_period_start": "2024-11-01",
                    "statement_period_end": "2024-11-30",
                    "opening_balance": "64925.01",
                    "closing_balance": "34925.01",
                    "closing_balance_credit_debit": "CR",
                    "currency": "AUD",
                },
            },
            {
                "fact_type": "bank_transaction",
                "page": "1",
                "evidence_id": "page_001_row_002",
                "confidence": "high",
                "snippet": "04 Nov Transfer to xx4237 CommBank app 30,000.00 $34,925.01 CR",
                "fields": {
                    "date": "2024-11-04",
                    "description": "Transfer to xx4237 CommBank app",
                    "amount": "30000.00",
                    "debit_credit": "DR",
                    "balance": "34925.01",
                    "currency": "AUD",
                },
            },
            {
                "fact_type": "bank_statement_period_balance",
                "page": "2",
                "evidence_id": "page_002",
                "confidence": "medium",
                "snippet": "Transaction Summary for 1st October 2024 to 31st October 2024",
                "fields": {
                    "account_fee": "0.00",
                    "cheque_deposit_performed": "0",
                    "transaction_summary_period_start": "2024-10-01",
                    "transaction_summary_period_end": "2024-10-31",
                },
            },
        ],
    }

    result = _normalise_codex_document_result(source, "raw_005", "hash", payload)

    assert "accounting_facts" not in result
    assert result["status"] == "indexed"
    assert result["no_fact_reason"].startswith("Step 2 indexes documents only")


def test_codex_prompt_includes_bank_transaction_rows_for_cba_statement():
    source = ROOT / "inputs" / "7A6AA8FA-4A80-4C13-B00D-4C0E985626CA.pdf"
    if not source.exists():
        return

    prompt = json.loads(_codex_process_document_prompt(source, "raw_005", "hash"))
    extracted_text = prompt["document"]["extracted_text"]

    assert "OPENING BALANCE" in extracted_text
    assert "Transfer to xx4237 CommBank app" in extracted_text
    assert "CLOSING BALANCE" in extracted_text
