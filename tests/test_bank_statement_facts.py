from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from accountant_copilot.state.artifacts import SourceDocument
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.evidence import EvidenceRef

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str):
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=ROOT,
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )


def test_export_bank_statement_facts_extracts_period_closing_balance_and_evidence(tmp_path: Path):
    state = EngagementState(
        engagement_id="bank_test",
        entity_name="Bank Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_bank",
            file_path="inputs/eStatement.pdf",
            document_type="bank_statement",
            entity="Bank Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_001_page_001",
            source_type="bank_statement",
            file_path="inputs/eStatement.pdf",
            document_id="doc_bank",
            page="1",
            quote="Statement Period 1 Jan 2025 - 31 Jan 2025 Opening Balance $30,211.09 CR Total Credits + $10,080.31 Total Debits - $9,966.39 Closing Balance $30,325.01 CR Business Transaction Account",
            confidence="text_pdf",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "bank_statement_facts.md"

    result = run_cli("export-bank-statement-facts", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    text = output.read_text()
    assert "# Bank Statement Facts" in text
    assert "inputs/eStatement.pdf" in text
    assert "1 Jan 2025" in text
    assert "31 Jan 2025" in text
    assert "$30,325.01" in text
    assert "raw_001_page_001" in text
    payload = json.loads((tmp_path / "bank_statement_facts.json").read_text())
    fact = payload["facts"][0]
    assert fact["statement_period_start"] == "1 Jan 2025"
    assert fact["statement_period_end"] == "31 Jan 2025"
    assert fact["opening_balance"] == "$30,211.09"
    assert fact["opening_balance_sign"] == "CR"
    assert fact["closing_balance"] == "$30,325.01"
    assert fact["closing_balance_sign"] == "CR"
    assert fact["total_credits"] == "$10,080.31"
    assert fact["total_credits_sign"] == "+"
    assert fact["total_debits"] == "$9,966.39"
    assert fact["total_debits_sign"] == "-"
    assert fact["status"] == "extracted"
    assert fact["evidence_id"] == "raw_001_page_001"


def test_export_bank_statement_facts_flags_missing_opening_balance(tmp_path: Path):
    state = EngagementState(
        engagement_id="bank_missing_opening_test",
        entity_name="Bank Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_bank",
            file_path="inputs/bank.pdf",
            document_type="bank_statement",
            entity="Bank Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_003_page_001",
            source_type="bank_statement",
            file_path="inputs/bank.pdf",
            document_id="doc_bank",
            page="1",
            quote="Statement Period 1 Feb 2025 - 28 Feb 2025 Closing Balance $42,100.00 DR Business Transaction Account",
            confidence="text_pdf",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "bank_statement_facts.md"

    result = run_cli("export-bank-statement-facts", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "bank_statement_facts.json").read_text())
    assert payload["facts"][0]["closing_balance"] == "$42,100.00"
    finding = payload["findings"][0]
    assert finding["category"] == "bank_statement_fact_missing"
    assert finding["document_id"] == "doc_bank"
    assert finding["evidence_id"] == "raw_003_page_001"
    assert finding["missing_fields"] == ["opening_balance"]


def test_export_bank_statement_facts_flags_missing_period_or_balance(tmp_path: Path):
    state = EngagementState(
        engagement_id="bank_missing_test",
        entity_name="Bank Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_bank",
            file_path="inputs/bank.pdf",
            document_type="bank_statement",
            entity="Bank Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_002_page_001",
            source_type="bank_statement",
            file_path="inputs/bank.pdf",
            document_id="doc_bank",
            page="1",
            quote="Business Transaction Account fees and transaction summary only",
            confidence="text_pdf",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "bank_statement_facts.md"

    result = run_cli("export-bank-statement-facts", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "bank_statement_facts.json").read_text())
    finding = payload["findings"][0]
    assert finding["category"] == "bank_statement_fact_missing"
    assert finding["document_id"] == "doc_bank"
    assert finding["evidence_id"] == "raw_002_page_001"
    assert set(finding["missing_fields"]) == {"statement_period", "closing_balance"}


def test_export_bank_transactions_extracts_rows_with_evidence(tmp_path: Path):
    state = EngagementState(
        engagement_id="bank_transactions_test",
        entity_name="Bank Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_bank",
            file_path="inputs/eStatement.pdf",
            document_type="bank_statement",
            entity="Bank Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_004_page_001",
            source_type="bank_statement",
            file_path="inputs/eStatement.pdf",
            document_id="doc_bank",
            page="1",
            quote=(
                "DATE TRANSACTION DESCRIPTION DEBIT CREDIT BALANCE "
                "06/08/24 STATEMENT OPENING BALANCE 7,434.80 "
                "13/09/24 Deposit Benpi Qrt Dst 001324422453 3,604.23 11,039.03 "
                "20/09/24 Deposit-Debenture/Note Interest Cap Notes 9 Dist C9S24/00114 6,476.08 17,515.11 "
                "06/11/24 CLOSING BALANCE 17,515.11"
            ),
            confidence="text_pdf",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "bank_transactions.md"

    result = run_cli("export-bank-transactions", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    text = output.read_text()
    assert "# Bank Transactions" in text
    payload = json.loads((tmp_path / "bank_transactions.json").read_text())
    assert payload["summary"] == {"bank_documents": 1, "transactions_extracted": 2, "findings": 0}
    first = payload["transactions"][0]
    assert first["transaction_date"] == "13/09/24"
    assert first["description"] == "Deposit Benpi Qrt Dst 001324422453"
    assert first["credit"] == "3,604.23"
    assert first["debit"] is None
    assert first["balance"] == "11,039.03"
    assert first["evidence_id"] == "raw_004_page_001"
    assert first["confidence"] == "text_pdf_pattern"



def test_export_bank_continuity_flags_balance_breaks(tmp_path: Path):
    facts_path = tmp_path / "bank_statement_facts.json"
    facts_path.write_text(
        json.dumps(
            {
                "engagement_id": "bank_continuity_test",
                "entity_name": "Bank Trust",
                "fact_type": "bank_statement_facts",
                "facts": [
                    {
                        "document_id": "doc_jan",
                        "file_path": "inputs/jan.pdf",
                        "page": "1",
                        "evidence_id": "ev_jan",
                        "statement_period_start": "1 Jan 2025",
                        "statement_period_end": "31 Jan 2025",
                        "opening_balance": "$100.00",
                        "opening_balance_sign": "CR",
                        "closing_balance": "$150.00",
                        "closing_balance_sign": "CR",
                    },
                    {
                        "document_id": "doc_feb",
                        "file_path": "inputs/feb.pdf",
                        "page": "1",
                        "evidence_id": "ev_feb",
                        "statement_period_start": "1 Feb 2025",
                        "statement_period_end": "28 Feb 2025",
                        "opening_balance": "$155.00",
                        "opening_balance_sign": "CR",
                        "closing_balance": "$200.00",
                        "closing_balance_sign": "CR",
                    },
                ],
                "findings": [],
                "summary": {"bank_documents": 2, "facts_extracted": 2, "findings": 0},
            }
        )
    )
    output = tmp_path / "bank_continuity.md"

    result = run_cli("export-bank-continuity", "--facts", str(facts_path), "--output", str(output))

    assert result.returncode == 1
    text = output.read_text()
    assert "# Bank Continuity Check" in text
    payload = json.loads((tmp_path / "bank_continuity.json").read_text())
    assert payload["summary"] == {"comparisons": 1, "findings": 1}
    finding = payload["findings"][0]
    assert finding["category"] == "bank_continuity_break"
    assert finding["prior_evidence_id"] == "ev_jan"
    assert finding["current_evidence_id"] == "ev_feb"
    assert finding["prior_closing_balance"] == "$150.00 CR"
    assert finding["current_opening_balance"] == "$155.00 CR"


def test_export_bank_continuity_passes_matching_sequence(tmp_path: Path):
    facts_path = tmp_path / "bank_statement_facts.json"
    facts_path.write_text(
        json.dumps(
            {
                "engagement_id": "bank_continuity_clean_test",
                "entity_name": "Bank Trust",
                "fact_type": "bank_statement_facts",
                "facts": [
                    {
                        "document_id": "doc_jan",
                        "file_path": "inputs/jan.pdf",
                        "page": "1",
                        "evidence_id": "ev_jan",
                        "statement_period_start": "1 Jan 2025",
                        "statement_period_end": "31 Jan 2025",
                        "opening_balance": "$100.00",
                        "opening_balance_sign": "CR",
                        "closing_balance": "$150.00",
                        "closing_balance_sign": "CR",
                    },
                    {
                        "document_id": "doc_feb",
                        "file_path": "inputs/feb.pdf",
                        "page": "1",
                        "evidence_id": "ev_feb",
                        "statement_period_start": "1 Feb 2025",
                        "statement_period_end": "28 Feb 2025",
                        "opening_balance": "$150.00",
                        "opening_balance_sign": "CR",
                        "closing_balance": "$200.00",
                        "closing_balance_sign": "CR",
                    },
                ],
                "findings": [],
                "summary": {"bank_documents": 2, "facts_extracted": 2, "findings": 0},
            }
        )
    )
    output = tmp_path / "bank_continuity.md"

    result = run_cli("export-bank-continuity", "--facts", str(facts_path), "--output", str(output))

    assert result.returncode == 0
    payload = json.loads((tmp_path / "bank_continuity.json").read_text())
    assert payload["summary"] == {"comparisons": 1, "findings": 0}
    assert payload["comparisons"][0]["status"] == "matched"
