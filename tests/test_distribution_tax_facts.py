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


def write_text_pdf(path: Path, text: str) -> None:
    try:
        import fitz  # type: ignore[import-not-found]

        doc = fitz.open()
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 540, 720), text, fontsize=10)
        doc.save(path)
        doc.close()
        return
    except Exception:
        pass

    stream = f"BT /F1 10 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
        b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
        b"5 0 obj << /Length " + str(len(stream)).encode() + b" >> stream\n" + stream + b"\nendstream endobj\n",
    ]
    content = b"%PDF-1.4\n"
    offsets = []
    for obj in objects:
        offsets.append(len(content))
        content += obj
    xref_at = len(content)
    content += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        content += f"{offset:010d} 00000 n \n".encode()
    content += f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    path.write_bytes(content)


def test_export_distribution_tax_facts_extracts_payment_advice_with_evidence(tmp_path: Path):
    state = EngagementState(
        engagement_id="distribution_tax_test",
        entity_name="Distribution Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_distribution",
            file_path="inputs/WBCPM_Distribution_Advice_2024_09_23.pdf",
            document_type="investment_statement",
            entity="Distribution Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_035_page_001",
            source_type="investment_statement",
            file_path="inputs/WBCPM_Distribution_Advice_2024_09_23.pdf",
            document_id="doc_distribution",
            page="1",
            quote=(
                "Payment Advice Key details Payment date: 23 September 2024 Record date: 13 September 2024 "
                "Quarterly distribution Cash Distribution 1,234.56 Franking credit tax offset 98.76 "
                "TFN amounts withheld - Net cash distribution 1,333.32"
            ),
            confidence="text_pdf",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "distribution_tax_facts.md"

    result = run_cli("export-distribution-tax-facts", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    assert "# Distribution and Tax Facts" in output.read_text()
    payload = json.loads((tmp_path / "distribution_tax_facts.json").read_text())
    assert payload["summary"] == {"distribution_tax_documents": 1, "facts_extracted": 1, "findings": 0}
    fact = payload["facts"][0]
    assert fact["payment_date"] == "23 September 2024"
    assert fact["record_date"] == "13 September 2024"
    assert fact["components"]["cash_distribution"] == "1,234.56"
    assert fact["components"]["franking_credit_tax_offset"] == "98.76"
    assert fact["components"]["tfn_withholding"] == "0.00"
    assert fact["components"]["net_cash_distribution"] == "1,333.32"
    assert fact["evidence_id"] == "raw_035_page_001"


def test_export_distribution_tax_facts_extracts_an3_payment_advice_from_pdf_text(tmp_path: Path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    pdf_path = input_dir / "AN3_Payment_Advice_2024_06_20.pdf"
    write_text_pdf(
        pdf_path,
        "Security Code Record Date Payment Date TFN ABN AN3PL 7 June 2024 20 June 2024 Not Quoted "
        "DISTRIBUTION ADVICE The details of your June ANZ Capital Notes 9 distribution of A$1.4295 per Note "
        "for the period from 20 March 2024 to 19 June 2024 are set out below. "
        "NUMBER OF NOTES FRANKED AMOUNT UNFRANKED AMOUNT LESS TAX NET AMOUNT FRANKING CREDIT "
        "5,400 A$5,017.55 A$2,701.75 A$1,269.00 A$6,450.30 A$2,150.38 "
        "PAYMENT INSTRUCTIONS NET AMOUNT: A$6,450.30",
    )
    state = EngagementState(
        engagement_id="distribution_tax_an3_test",
        entity_name="Distribution Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_an3",
            file_path=str(pdf_path),
            document_type="investment_statement",
            entity="Distribution Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_008_page_001",
            source_type="investment_statement",
            file_path=str(pdf_path),
            document_id="doc_an3",
            page="1",
            quote=(
                "DISTRIBUTION ADVICE The details of your June ANZ Capital Notes 9 distribution of "
                "A$1.4295 per Note for the period from 20 March 2024 to 19 June 2024 are set out below. "
                "PAYMENT INSTRUCTIONS"
            ),
            confidence="text_pdf",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "distribution_tax_facts.md"

    result = run_cli("export-distribution-tax-facts", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads((tmp_path / "distribution_tax_facts.json").read_text())
    assert payload["summary"] == {"distribution_tax_documents": 1, "facts_extracted": 1, "findings": 0}
    fact = payload["facts"][0]
    assert fact["document_id"] == "doc_an3"
    assert fact["evidence_id"] == "raw_008_page_001"
    assert fact["file_path"] == str(pdf_path)
    assert fact["payment_date"] == "20 June 2024"
    assert fact["record_date"] == "7 June 2024"
    assert fact["security_code"] == "AN3PL"
    assert fact["investment_name"] == "ANZ Capital Notes 9"
    assert fact["amount"] == "6,450.30"
    assert fact["components"]["franked_amount"] == "5,017.55"
    assert fact["components"]["unfranked_amount"] == "2,701.75"
    assert fact["components"]["tfn_withholding"] == "1,269.00"
    assert fact["components"]["net_cash_distribution"] == "6,450.30"
    assert fact["components"]["franking_credit_tax_offset"] == "2,150.38"
    assert fact["confidence"] == "text_pdf"
    assert "ANZ Capital Notes 9" in fact["snippet"]


def test_export_distribution_tax_facts_reports_unparsed_candidate_document(tmp_path: Path):
    state = EngagementState(
        engagement_id="distribution_tax_finding_test",
        entity_name="Distribution Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_tax",
            file_path="inputs/Example Tax Statement.pdf",
            document_type="investment_statement",
            entity="Distribution Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="raw_007_page_001",
            source_type="investment_statement",
            file_path="inputs/Example Tax Statement.pdf",
            document_id="doc_tax",
            page="1",
            quote="Distribution tax statement with components of distribution but no parseable numeric component lines.",
            confidence="text_pdf",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "distribution_tax_facts.md"

    result = run_cli("export-distribution-tax-facts", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "distribution_tax_facts.json").read_text())
    assert payload["summary"] == {"distribution_tax_documents": 1, "facts_extracted": 0, "findings": 1}
    assert payload["findings"][0]["category"] == "distribution_tax_fact_extraction_incomplete"


def test_export_distribution_tax_review_creates_unapproved_accountant_findings(tmp_path: Path):
    facts = {
        "engagement_id": "distribution_review_test",
        "entity_name": "Distribution Trust",
        "fact_type": "distribution_tax_facts",
        "facts": [
            {
                "document_id": "doc_distribution",
                "file_path": "inputs/WBCPM_Distribution_Advice_2024_09_23.pdf",
                "page": "1",
                "evidence_id": "raw_035_page_001",
                "payment_date": "23 September 2024",
                "record_date": "13 September 2024",
                "components": {
                    "net_cash_distribution": "1,333.32",
                    "franking_credit_tax_offset": "98.76",
                },
                "confidence": "text_pdf",
            }
        ],
        "findings": [
            {
                "category": "distribution_tax_fact_extraction_incomplete",
                "document_id": "doc_unparsed",
                "file_path": "inputs/AN3_Payment_Advice_2024_09_20.pdf",
                "recommended_action": "Review source document.",
            }
        ],
        "summary": {"distribution_tax_documents": 2, "facts_extracted": 1, "findings": 1},
    }
    facts_path = tmp_path / "distribution_tax_facts.json"
    facts_path.write_text(json.dumps(facts))
    output = tmp_path / "distribution_tax_review.md"

    result = run_cli("export-distribution-tax-review", "--facts", str(facts_path), "--output", str(output))

    assert result.returncode == 1
    assert "# Distribution and Tax Accounting Review" in output.read_text()
    payload = json.loads((tmp_path / "distribution_tax_review.json").read_text())
    assert payload["summary"] == {"facts_reviewed": 1, "source_findings_reviewed": 1, "review_findings": 4, "approved": 0}
    categories = {finding["category"] for finding in payload["review_findings"]}
    assert categories == {
        "distribution_income_mapping_review_required",
        "distribution_tax_component_review_required",
        "distribution_bank_match_review_required",
        "distribution_source_extraction_review_required",
    }
    assert all(finding["approved"] is False for finding in payload["review_findings"])
    assert payload["review_findings"][0]["evidence_id"] == "raw_035_page_001"
