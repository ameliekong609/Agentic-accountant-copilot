from __future__ import annotations

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


def test_preview_tb_impact_groups_only_approved_balanced_journals(tmp_path: Path):
    state = EngagementState(engagement_id="tb_preview", entity_name="TB Preview Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.extend([
        ChartAccount(account_id="acct_600", code="600", name="Accounting Fees", type="expense", presentation_group="Expenses", opening_balance="0.00"),
        ChartAccount(account_id="acct_100", code="100", name="Cash", type="asset", presentation_group="Cash", opening_balance="1000.00"),
    ])
    state.adjustment_proposals.extend([
        AdjustmentProposal(adjustment_id="journal_approved", description="Approved invoice", debit_account="acct_600", credit_account="acct_100", amount="110.00", date="2025-06-30", source_evidence_refs=["invoice_ev"], status="approved", decision_id="decision_1"),
        AdjustmentProposal(adjustment_id="journal_pending", description="Pending invoice", debit_account="acct_600", credit_account="acct_100", amount="50.00", date="2025-06-30", status="pending_review"),
    ])
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "tb_impact_preview.md"

    result = run_cli("preview-tb-impact", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "tb_impact_preview.json").read_text())
    assert payload["summary"] == {"approved_journals": 1, "excluded_journals": 1, "findings": 1, "balanced": True}
    assert payload["account_impacts"]["acct_600"]["debits"] == "110.00"
    assert payload["account_impacts"]["acct_100"]["credits"] == "110.00"
    assert payload["findings"][0]["category"] == "tb_preview_unapproved_journal_excluded"


def test_preview_tb_impact_flags_placeholder_offsets(tmp_path: Path):
    state = EngagementState(engagement_id="tb_preview", entity_name="TB Preview Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.append(ChartAccount(account_id="acct_600", code="600", name="Accounting Fees", type="expense", presentation_group="Expenses", opening_balance="0.00"))
    state.adjustment_proposals.append(AdjustmentProposal(adjustment_id="journal_bad", description="Bad invoice", debit_account="acct_600", credit_account="pending_review_offset", amount="110.00", date="2025-06-30", status="approved"))
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())

    result = run_cli("preview-tb-impact", "--state", str(state_path), "--output", str(tmp_path / "tb_impact_preview.md"))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "tb_impact_preview.json").read_text())
    assert payload["summary"]["balanced"] is False
    assert payload["findings"][0]["category"] == "tb_preview_placeholder_offset"
