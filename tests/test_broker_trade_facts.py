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


def test_export_broker_trade_facts_extracts_confirmation_fields(tmp_path: Path):
    state = EngagementState(engagement_id="broker_test", entity_name="Broker Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.source_documents.append(SourceDocument(document_id="doc_broker", file_path="inputs/Confirmation.PDF", document_type="broker_confirmation", entity="Broker Trust", period_start="2024-07-01", period_end="2025-06-30", source_hash="abc"))
    state.evidence.append(EvidenceRef(evidence_id="raw_017_page_001", source_type="broker_confirmation", file_path="inputs/Confirmation.PDF", document_id="doc_broker", page="1", quote="SELL CONFIRMATION TAX INVOICE Transaction Date: 06/03/2025 Settlement Date: 10/03/2025 Settlement Amount: 12,345.67 Consideration: 12,400.00 Quantity: 100.00 Price: 124.00 Brokerage: 54.33 Company: Example Ltd Security: EXM ISIN: AU000000EXM1", confidence="text_pdf"))
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "broker_trade_facts.md"

    result = run_cli("export-broker-trade-facts", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    payload = json.loads((tmp_path / "broker_trade_facts.json").read_text())
    assert payload["summary"] == {"broker_documents": 1, "facts_extracted": 1, "findings": 0}
    fact = payload["facts"][0]
    assert fact["side"] == "sell"
    assert fact["fields"]["transaction_date"] == "06/03/2025"
    assert fact["fields"]["settlement_amount"] == "12,345.67"
    assert fact["evidence_id"] == "raw_017_page_001"


def test_export_broker_trade_review_creates_unapproved_findings(tmp_path: Path):
    facts = {"engagement_id": "broker_review", "entity_name": "Broker Trust", "fact_type": "broker_trade_facts", "facts": [{"document_id": "doc_broker", "file_path": "inputs/Confirmation.PDF", "page": "1", "evidence_id": "raw_017_page_001", "side": "sell", "fields": {"settlement_amount": "12,345.67"}, "confidence": "text_pdf"}], "findings": [], "summary": {"broker_documents": 1, "facts_extracted": 1, "findings": 0}}
    facts_path = tmp_path / "broker_trade_facts.json"
    facts_path.write_text(json.dumps(facts))
    output = tmp_path / "broker_trade_review.md"

    result = run_cli("export-broker-trade-review", "--facts", str(facts_path), "--output", str(output))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "broker_trade_review.json").read_text())
    assert payload["summary"] == {"facts_reviewed": 1, "source_findings_reviewed": 0, "review_findings": 3, "approved": 0}
    assert {finding["category"] for finding in payload["review_findings"]} == {"broker_disposal_classification_review_required", "broker_gain_loss_review_required", "broker_bank_settlement_match_review_required"}
    assert all(finding["approved"] is False for finding in payload["review_findings"])
