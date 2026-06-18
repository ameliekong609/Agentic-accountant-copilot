from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.artifacts import ChartAccount
from accountant_copilot.state.engagement import EngagementState

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


def test_suggest_coa_mappings_creates_unapproved_mapping_findings(tmp_path: Path):
    state = EngagementState(
        engagement_id="coa_mapping_test",
        entity_name="CoA Mapping Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.chart_accounts.extend([
        ChartAccount(account_id="acct_400", code="400", name="Distribution Income", type="income", presentation_group="Revenue", opening_balance="0.00"),
        ChartAccount(account_id="acct_610", code="610", name="Accounting Fees", type="expense", presentation_group="Expenses", opening_balance="0.00"),
        ChartAccount(account_id="acct_120", code="120", name="Investment Portfolio", type="asset", presentation_group="Investments", opening_balance="0.00"),
    ])
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    invoice = {"facts": [{"invoice_number": "INV-0082", "description": "Portfolio Management Services", "amount_due": "1,100.00", "evidence_id": "invoice_ev"}]}
    distribution = {"facts": [{"components": {"net_cash_distribution": "1,333.32"}, "evidence_id": "dist_ev"}]}
    broker = {"facts": [{"side": "sell", "fields": {"settlement_amount": "12,345.67"}, "evidence_id": "broker_ev"}]}
    for name, payload in [("invoice.json", invoice), ("dist.json", distribution), ("broker.json", broker)]:
        (tmp_path / name).write_text(json.dumps(payload))
    output = tmp_path / "coa_mapping_suggestions.md"

    result = run_cli(
        "suggest-coa-mappings",
        "--state", str(state_path),
        "--invoice-facts", str(tmp_path / "invoice.json"),
        "--distribution-tax-facts", str(tmp_path / "dist.json"),
        "--broker-trade-facts", str(tmp_path / "broker.json"),
        "--output", str(output),
    )

    assert result.returncode == 1
    payload = json.loads((tmp_path / "coa_mapping_suggestions.json").read_text())
    assert payload["summary"] == {"source_facts": 3, "suggestions": 3, "findings": 3, "approved": 0}
    assert {item["source_fact_type"] for item in payload["suggestions"]} == {"invoice", "distribution_tax", "broker_trade"}
    assert all(item["approved"] is False for item in payload["suggestions"])
    assert {item["category"] for item in payload["findings"]} == {"coa_mapping_review_required"}


def test_suggest_coa_mappings_reports_missing_coa_accounts(tmp_path: Path):
    state = EngagementState(engagement_id="coa_missing", entity_name="CoA Missing Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    invoice = {"facts": [{"invoice_number": "INV-0082", "amount_due": "1,100.00", "evidence_id": "invoice_ev"}]}
    (tmp_path / "invoice.json").write_text(json.dumps(invoice))
    output = tmp_path / "coa_mapping_suggestions.md"

    result = run_cli("suggest-coa-mappings", "--state", str(state_path), "--invoice-facts", str(tmp_path / "invoice.json"), "--output", str(output))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "coa_mapping_suggestions.json").read_text())
    assert payload["summary"] == {"source_facts": 1, "suggestions": 0, "findings": 1, "approved": 0}
    assert payload["findings"][0]["category"] == "coa_mapping_account_missing"
