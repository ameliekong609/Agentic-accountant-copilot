from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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
