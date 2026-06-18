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


def test_import_coa_from_prior_statements_populates_pending_review_accounts(tmp_path: Path):
    state = EngagementState(engagement_id="prior_coa", entity_name="Prior CoA Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.source_documents.append(SourceDocument(document_id="doc_prior", file_path="inputs/FY24.pdf", document_type="prior_year_financial_statements", entity="Prior CoA Trust", period_start="2023-07-01", period_end="2024-06-30", source_hash="abc"))
    state.evidence.extend([
        EvidenceRef(evidence_id="raw_037_page_002", source_type="prior_year_financial_statements", file_path="inputs/FY24.pdf", document_id="doc_prior", page="2", quote="Profit and Loss REVENUE Distributions Received 20,216 Dividends Received 1,084 EXPENSES Accounting Fees 7,644 Investment Expenses 2,012 NET INTEREST Interest Income 1,911", confidence="text_pdf"),
        EvidenceRef(evidence_id="raw_037_page_003", source_type="prior_year_financial_statements", file_path="inputs/FY24.pdf", document_id="doc_prior", page="3", quote="Balance Sheet ASSETS Cash at Bank CBA0700 26,152 Investments ANZ Capital Notes 540,000", confidence="text_pdf"),
    ])
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "prior_coa_import.md"

    result = run_cli("import-coa-from-prior-statements", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    updated = json.loads(state_path.read_text())
    accounts = updated["chart_accounts"]
    names = {account["name"] for account in accounts}
    assert {"Distributions Received", "Accounting Fees", "Interest Income", "Cash at Bank CBA0700", "Investments ANZ Capital Notes"}.issubset(names)
    assert all(account["status"] == "pending_review" for account in accounts)
    assert updated["coa_review_status"] == "pending_review"
    payload = json.loads((tmp_path / "prior_coa_import.json").read_text())
    assert payload["summary"]["accounts_imported"] >= 5
    assert payload["summary"]["approved"] == 0
