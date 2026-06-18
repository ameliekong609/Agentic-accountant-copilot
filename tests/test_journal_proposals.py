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


def test_propose_journals_from_approved_coa_mappings(tmp_path: Path):
    state = EngagementState(engagement_id="journal_proposals", entity_name="Journal Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.append(ChartAccount(account_id="acct_600", code="600", name="Accounting Fees", type="expense", presentation_group="Expenses", opening_balance="0.00"))
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    applied = {
        "applied_mappings": [{
            "mapping_id": "map_invoice",
            "action": "approve",
            "decision_id": "decision_approve_coa_mapping_0001",
            "source_fact_type": "invoice",
            "candidate_account_id": "acct_600",
            "amount": "1100.00",
            "source_evidence_id": "invoice_ev",
            "evidence_refs": ["invoice_ev", "acct_600"],
        }]
    }
    applied_path = tmp_path / "applied_coa_mapping_decisions.json"
    applied_path.write_text(json.dumps(applied))
    output = tmp_path / "journal_proposals.md"

    result = run_cli("propose-journals", "--state", str(state_path), "--applied-mappings", str(applied_path), "--output", str(output))

    assert result.returncode == 1
    updated = json.loads(state_path.read_text())
    proposals = updated["adjustment_proposals"]
    assert len(proposals) == 1
    assert proposals[0]["status"] == "pending_review"
    assert proposals[0]["debit_account"] == "acct_600"
    assert proposals[0]["credit_account"] == "pending_review_offset"
    assert proposals[0]["source_evidence_refs"] == ["invoice_ev", "acct_600", "decision_approve_coa_mapping_0001"]
    payload = json.loads((tmp_path / "journal_proposals.json").read_text())
    assert payload["summary"] == {"proposals_created": 1, "blocked_mappings": 0, "approved": 0}


def test_propose_journals_blocks_unapproved_or_missing_accounts(tmp_path: Path):
    state = EngagementState(engagement_id="journal_proposals", entity_name="Journal Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    applied_path = tmp_path / "applied.json"
    applied_path.write_text(json.dumps({"applied_mappings": [{"mapping_id": "map_missing", "action": "approve", "candidate_account_id": "acct_missing", "amount": "20.00", "source_fact_type": "invoice"}]}))

    result = run_cli("propose-journals", "--state", str(state_path), "--applied-mappings", str(applied_path), "--output", str(tmp_path / "journal_proposals.md"))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "journal_proposals.json").read_text())
    assert payload["summary"] == {"proposals_created": 0, "blocked_mappings": 1, "approved": 0}
    assert payload["findings"][0]["category"] == "journal_proposal_account_missing"
