from __future__ import annotations

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


def test_review_packet_journal_tb_section_links_decision_preview_and_export_artifacts(tmp_path: Path):
    state = EngagementState(engagement_id="packet_upgrade", entity_name="Packet Upgrade Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.extend([
        ChartAccount(account_id="acct_600", code="600", name="Accounting Fees", type="expense", presentation_group="Expenses", opening_balance="0.00"),
        ChartAccount(account_id="acct_100", code="100", name="Cash", type="asset", presentation_group="Cash", opening_balance="1000.00"),
    ])
    state.adjustment_proposals.append(AdjustmentProposal(adjustment_id="journal_approved", description="Approved invoice", debit_account="acct_600", credit_account="acct_100", amount="110.00", date="2025-06-30", status="approved", decision_id="decision_1"))
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())
    for filename in ["journal_decisions_template.json", "applied_journal_decisions.json", "tb_impact_preview.md"]:
        (tmp_path / filename).write_text("fixture")
    reviewed_dir = tmp_path / "reviewed_journals"
    reviewed_dir.mkdir()
    (reviewed_dir / "reviewed_journals.md").write_text("fixture")

    result = run_cli("export-review-packet", "--state", str(state_path), "--output-dir", str(tmp_path / "review_packet"))

    assert result.returncode in {0, 1}
    impact = (tmp_path / "review_packet" / "journal_tb_impact.md").read_text()
    assert "Journal decision template" in impact
    assert "Applied journal decisions" in impact
    assert "TB impact preview" in impact
    assert "Reviewed journals" in impact
    assert "Approved journals: 1" in impact
