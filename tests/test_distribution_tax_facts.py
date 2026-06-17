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
