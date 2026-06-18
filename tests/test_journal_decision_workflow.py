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


def _state_with_journal(tmp_path: Path) -> Path:
    state = EngagementState(engagement_id="journal_decisions", entity_name="Journal Decision Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.extend([
        ChartAccount(account_id="acct_600", code="600", name="Accounting Fees", type="expense", presentation_group="Expenses", opening_balance="0.00"),
        ChartAccount(account_id="acct_100", code="100", name="Cash at Bank", type="asset", presentation_group="Cash", opening_balance="1000.00"),
    ])
    state.adjustment_proposals.append(AdjustmentProposal(adjustment_id="journal_map_invoice", description="Invoice journal", debit_account="acct_600", credit_account="pending_review_offset", amount="110.00", date="2025-06-30", source_evidence_refs=["invoice_ev", "acct_600", "mapping_decision_1"]))
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    return state_path


def test_export_and_apply_journal_decisions_resolves_offset_and_approves(tmp_path: Path):
    state_path = _state_with_journal(tmp_path)
    template = tmp_path / "journal_decisions_template.json"

    exported = run_cli("export-journal-decision-template", "--state", str(state_path), "--output", str(template))

    assert exported.returncode == 0
    payload = json.loads(template.read_text())
    decision = payload["journal_decisions"][0]
    assert decision["adjustment_id"] == "journal_map_invoice"
    assert decision["action"] == ""
    assert decision["offset_account_id"] == ""
    decision.update({"action": "approve", "offset_account_id": "acct_100", "approved_by": "Reviewer", "rationale": "Approve invoice expense paid from bank."})
    decisions = tmp_path / "journal_decisions.json"
    decisions.write_text(json.dumps(payload))
    output = tmp_path / "applied_journal_decisions.json"

    applied = run_cli("apply-journal-decisions", "--state", str(state_path), "--decisions", str(decisions), "--output", str(output))

    assert applied.returncode == 0
    updated = json.loads(state_path.read_text())
    proposal = updated["adjustment_proposals"][0]
    assert proposal["status"] == "approved"
    assert proposal["credit_account"] == "acct_100"
    assert proposal["decision_id"] == updated["decisions"][0]["decision_id"]
    assert updated["decisions"][0]["selected_option"] == "approve_journal"
    assert json.loads(output.read_text())["summary"] == {"approved": 1, "rejected": 0, "applied": 1}


def test_apply_journal_decisions_blocks_placeholder_without_offset(tmp_path: Path):
    state_path = _state_with_journal(tmp_path)
    decisions = tmp_path / "bad_journal_decisions.json"
    decisions.write_text(json.dumps({"journal_decisions": [{"adjustment_id": "journal_map_invoice", "action": "approve", "approved_by": "Reviewer", "rationale": "Missing offset."}]}))

    result = run_cli("apply-journal-decisions", "--state", str(state_path), "--decisions", str(decisions), "--output", str(tmp_path / "out.json"))

    assert result.returncode == 2
    assert "offset_account_id" in result.stderr
