from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.cli import _build_invoice_facts_payload
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


def test_export_invoice_facts_extracts_ocr_invoice_with_evidence(tmp_path: Path):
    state = EngagementState(
        engagement_id="invoice_test",
        entity_name="Invoice Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_invoice",
            file_path="inputs/IMG_5022.png",
            document_type="image_support",
            entity="Invoice Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_026_page_001",
            source_type="image_support",
            file_path="inputs/IMG_5022.png",
            document_id="doc_invoice",
            page="1",
            quote=(
                "EMERALD TAX INVOICE 11 Dec 2024 XYZ Financial Pty Ltd Account Number Financial Trust "
                "Emerald Family Enterprise Group Pty Ltd Invoice Number INV-0082 Reference ABN 949697 "
                "Description Unit Price GST Amount AUD Portfolio Management Services From 01/01/2025 to "
                "31/12/2025 10% 4,000.00 Subtotal 1,000.00 Total GST 10% 100.00 "
                "Amount Due AUD 1,100.00 Due Date: 23/01/2025"
            ),
            confidence="image_ocr",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "invoice_facts.md"

    result = run_cli("export-invoice-facts", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    assert "# Invoice Facts" in output.read_text()
    payload = json.loads((tmp_path / "invoice_facts.json").read_text())
    assert payload["summary"] == {"invoice_documents": 1, "facts_extracted": 1, "findings": 0}
    fact = payload["facts"][0]
    assert fact["invoice_number"] == "INV-0082"
    assert fact["invoice_date"] == "11 Dec 2024"
    assert fact["due_date"] == "23/01/2025"
    assert fact["supplier"] == "Emerald Family Enterprise Group Pty Ltd"
    assert fact["description"] == "Portfolio Management Services"
    assert fact["service_period_start"] == "01/01/2025"
    assert fact["service_period_end"] == "31/12/2025"
    assert fact["subtotal"] == "1,000.00"
    assert fact["gst"] == "100.00"
    assert fact["amount_due"] == "1,100.00"
    assert fact["evidence_id"] == "raw_026_page_001"
    assert fact["confidence"] == "image_ocr"
    progress = json.loads((tmp_path / "invoice_facts.progress.json").read_text())
    partial = json.loads((tmp_path / "invoice_facts.partial.json").read_text())
    assert progress["status"] == "complete"
    assert progress["processed_items"] == 1
    assert progress["total_items"] == 1
    assert partial["progress"]["facts_extracted"] == 1


def test_invoice_facts_can_use_anthropic_structured_extraction(monkeypatch):
    state = EngagementState(
        engagement_id="invoice_api_test",
        entity_name="Invoice Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_invoice",
            file_path="inputs/loose_invoice.pdf",
            document_type="image_support",
            entity="Invoice Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_001_page_001",
            source_type="image_support",
            file_path="inputs/loose_invoice.pdf",
            document_id="doc_invoice",
            page="1",
            quote="TAX INVOICE Supplier: Example Pty Ltd Invoice ABC-77 Amount Due 2,420.00",
            confidence="text_pdf",
        )
    )
    monkeypatch.setenv(
        "ACCOUNTANT_COPILOT_FAKE_ANTHROPIC_FACT_JSON",
        json.dumps(
            {
                "extracted": True,
                "fact_type": "invoice",
                "fields": {
                    "invoice_number": "ABC-77",
                    "invoice_date": "not shown",
                    "due_date": "not shown",
                    "supplier": "Example Pty Ltd",
                    "description": "Invoice from source evidence",
                    "amount_due": "2,420.00",
                },
                "confidence": "medium",
                "reason": "Structured extraction from loose invoice text.",
            }
        ),
    )

    payload = _build_invoice_facts_payload(state, use_ai_extraction=True, ai_provider="anthropic")

    assert payload["summary"]["facts_extracted"] == 1
    fact = payload["facts"][0]
    assert fact["invoice_number"] == "ABC-77"
    assert fact["supplier"] == "Example Pty Ltd"
    assert fact["amount_due"] == "2,420.00"
    assert fact["extraction_method"] == "ai"
    assert fact["ai_provider"] == "anthropic"


def test_invoice_facts_reuse_successful_ai_cache(monkeypatch, tmp_path: Path):
    state = EngagementState(
        engagement_id="invoice_cache_test",
        entity_name="Invoice Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_invoice",
            file_path="inputs/invoice.pdf",
            document_type="image_support",
            entity="Invoice Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="invoice-hash",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="ev_invoice",
            source_type="image_support",
            file_path="inputs/invoice.pdf",
            document_id="doc_invoice",
            page="1",
            quote="TAX INVOICE Supplier: Cache Pty Ltd Invoice CACHE-1 Amount Due 9,900.00",
            confidence="text_pdf",
        )
    )
    monkeypatch.setenv(
        "ACCOUNTANT_COPILOT_FAKE_ANTHROPIC_FACT_JSON",
        json.dumps(
            {
                "extracted": True,
                "fact_type": "invoice",
                "fields": {
                    "invoice_number": "CACHE-1",
                    "invoice_date": "not shown",
                    "due_date": "not shown",
                    "supplier": "Cache Pty Ltd",
                    "description": "Cached invoice",
                    "amount_due": "9,900.00",
                },
                "confidence": "medium",
            }
        ),
    )
    cache_dir = tmp_path / ".ai_cache"

    first = _build_invoice_facts_payload(state, use_ai_extraction=True, ai_provider="anthropic", cache_dir=cache_dir)
    monkeypatch.delenv("ACCOUNTANT_COPILOT_FAKE_ANTHROPIC_FACT_JSON")
    second = _build_invoice_facts_payload(state, use_ai_extraction=True, ai_provider="anthropic", cache_dir=cache_dir)

    assert first["facts"][0]["invoice_number"] == "CACHE-1"
    assert second["facts"][0]["invoice_number"] == "CACHE-1"
    assert list(cache_dir.glob("*.json"))


def test_export_invoice_facts_ignores_non_invoice_documents(tmp_path: Path):
    state = EngagementState(
        engagement_id="invoice_ignore_test",
        entity_name="Invoice Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_confirmation",
            file_path="inputs/Confirmation.pdf",
            document_type="broker_confirmation",
            entity="Invoice Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_017_page_001",
            source_type="broker_confirmation",
            file_path="inputs/Confirmation.pdf",
            document_id="doc_confirmation",
            page="1",
            quote="Trade Confirmation Invoice reference settlement amount 123.45",
            confidence="text_pdf",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "invoice_facts.md"

    result = run_cli("export-invoice-facts", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    payload = json.loads((tmp_path / "invoice_facts.json").read_text())
    assert payload["summary"] == {"invoice_documents": 0, "facts_extracted": 0, "findings": 0}


def test_export_invoice_review_creates_accountant_review_findings_without_approval(tmp_path: Path):
    facts = {
        "engagement_id": "invoice_review_test",
        "entity_name": "Invoice Trust",
        "fact_type": "invoice_facts",
        "facts": [
            {
                "invoice_number": "INV-0082",
                "invoice_date": "11 Dec 2024",
                "due_date": "23/01/2025",
                "supplier": "Emerald Family Enterprise Group Pty Ltd",
                "description": "Portfolio Management Services",
                "service_period_start": "01/01/2025",
                "service_period_end": "31/12/2025",
                "subtotal": "1,000.00",
                "gst": "100.00",
                "amount_due": "1,100.00",
                "evidence_id": "raw_026_page_001",
                "file_path": "inputs/IMG_5022.png",
                "page": "1",
                "confidence": "image_ocr",
            }
        ],
        "findings": [],
        "summary": {"invoice_documents": 1, "facts_extracted": 1, "findings": 0},
    }
    facts_path = tmp_path / "invoice_facts.json"
    facts_path.write_text(json.dumps(facts))
    output = tmp_path / "invoice_review.md"

    result = run_cli("export-invoice-review", "--facts", str(facts_path), "--output", str(output))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "invoice_review.json").read_text())
    assert payload["summary"] == {"invoices_reviewed": 1, "review_findings": 2, "approved": 0}
    finding_categories = {finding["category"] for finding in payload["review_findings"]}
    assert finding_categories == {"invoice_accounting_treatment_review_required", "invoice_ocr_evidence_review_required"}
    treatment = payload["review_findings"][0]
    assert treatment["candidate_treatment"] == "portfolio_management_fee_or_service_expense"
    assert treatment["approved"] is False
    assert treatment["evidence_id"] == "raw_026_page_001"
