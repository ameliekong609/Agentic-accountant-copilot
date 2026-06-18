from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.artifacts import AdjustmentProposal, ChartAccount
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


def test_export_reviewed_journals_outputs_only_approved_journals(tmp_path: Path):
    state = EngagementState(engagement_id="journal_export", entity_name="Journal Export Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.extend([
        ChartAccount(account_id="acct_600", code="600", name="Accounting Fees", type="expense", presentation_group="Expenses", opening_balance="0.00"),
        ChartAccount(account_id="acct_100", code="100", name="Cash", type="asset", presentation_group="Cash", opening_balance="1000.00"),
    ])
    state.adjustment_proposals.extend([
        AdjustmentProposal(adjustment_id="journal_approved", description="Approved invoice", debit_account="acct_600", credit_account="acct_100", amount="110.00", date="2025-06-30", source_evidence_refs=["invoice_ev"], status="approved", decision_id="decision_1"),
        AdjustmentProposal(adjustment_id="journal_rejected", description="Rejected invoice", debit_account="acct_600", credit_account="acct_100", amount="50.00", date="2025-06-30", status="rejected"),
    ])
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output_dir = tmp_path / "reviewed_journals"

    result = run_cli("export-reviewed-journals", "--state", str(state_path), "--output-dir", str(output_dir))

    assert result.returncode == 0
    payload = json.loads((output_dir / "reviewed_journals.json").read_text())
    assert payload["summary"] == {"exported": 1, "excluded_pending_or_rejected": 1}
    assert payload["journals"][0]["adjustment_id"] == "journal_approved"
    rows = list(csv.DictReader((output_dir / "reviewed_journals.csv").open()))
    assert rows[0]["adjustment_id"] == "journal_approved"
    assert "journal_approved" in (output_dir / "reviewed_journals.md").read_text()


def test_export_reviewed_journals_fails_on_placeholder_offset(tmp_path: Path):
    state = EngagementState(engagement_id="journal_export", entity_name="Journal Export Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.adjustment_proposals.append(AdjustmentProposal(adjustment_id="journal_bad", description="Bad journal", debit_account="acct_600", credit_account="pending_review_offset", amount="110.00", date="2025-06-30", status="approved"))
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())

    result = run_cli("export-reviewed-journals", "--state", str(state_path), "--output-dir", str(tmp_path / "out"))

    assert result.returncode == 1
    assert "pending_review_offset" in result.stderr
