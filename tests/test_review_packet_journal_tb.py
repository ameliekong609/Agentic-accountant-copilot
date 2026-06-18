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


def test_review_packet_includes_journal_tb_impact_section(tmp_path: Path):
    state = EngagementState(engagement_id="packet", entity_name="Packet Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.append(ChartAccount(account_id="acct_600", code="600", name="Accounting Fees", type="expense", presentation_group="Expenses", opening_balance="0.00"))
    state.adjustment_proposals.append(AdjustmentProposal(adjustment_id="journal_map_invoice", description="Proposed invoice journal", debit_account="acct_600", credit_account="pending_review_offset", amount="1100.00", date="2025-06-30", source_evidence_refs=["invoice_ev", "acct_600"]))
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())
    (tmp_path / "journal_proposals.md").write_text("# Journal Proposals\n\n- journal_map_invoice pending review\n")
    (tmp_path / "applied_coa_mapping_decisions.json").write_text(json.dumps({"summary": {"applied": 1, "approved": 1, "rejected": 0}}))
    output_dir = tmp_path / "review_packet"

    result = run_cli("export-review-packet", "--state", str(state_path), "--output-dir", str(output_dir))

    assert result.returncode in {0, 1}
    impact = (output_dir / "journal_tb_impact.md").read_text()
    assert "Journal / TB Impact Review" in impact
    assert "journal_map_invoice" in impact
    assert "pending_review_offset" in impact
    readme = (output_dir / "README.md").read_text()
    assert "journal_tb_impact.md" in readme
