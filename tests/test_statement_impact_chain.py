from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.artifacts import AdjustmentProposal, ChartAccount
from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
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


def _approved_state(tmp_path: Path) -> Path:
    state = EngagementState(engagement_id="statement_chain", entity_name="Statement Chain Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.extend([
        ChartAccount(account_id="acct_100", code="100", name="Cash", type="asset", presentation_group="Cash and Cash Equivalents", opening_balance="1000.00", status="approved"),
        ChartAccount(account_id="acct_400", code="400", name="Distribution Income", type="income", presentation_group="Revenue", opening_balance="0.00", status="approved"),
        ChartAccount(account_id="acct_600", code="600", name="Accounting Fees", type="expense", presentation_group="Expenses", opening_balance="0.00", status="approved"),
    ])
    state.coa_review_status = "approved"
    state.adjustment_proposals.extend([
        AdjustmentProposal(adjustment_id="journal_income", description="Distribution income", debit_account="acct_100", credit_account="acct_400", amount="200.00", date="2025-06-30", status="approved", decision_id="decision_j1", source_evidence_refs=["dist_ev"]),
        AdjustmentProposal(adjustment_id="journal_fee", description="Accounting fees", debit_account="acct_600", credit_account="acct_100", amount="50.00", date="2025-06-30", status="approved", decision_id="decision_j2", source_evidence_refs=["invoice_ev"]),
    ])
    state.adjustment_review_status = "approved"
    state.decisions.append(AccountantDecision(decision_id="decision_final_signoff_0001", question="release?", selected_option="final_signoff", rationale="Approved for test.", status=DecisionStatus.APPROVED, approved_by="Reviewer"))
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())
    return state_path


def test_build_post_journal_tb_and_statement_mapping_and_draft(tmp_path: Path):
    state_path = _approved_state(tmp_path)
    reviewed_dir = tmp_path / "reviewed_journals"
    assert run_cli("export-reviewed-journals", "--state", str(state_path), "--output-dir", str(reviewed_dir)).returncode == 0

    tb_md = tmp_path / "post_journal_trial_balance.md"
    tb = run_cli("build-post-journal-tb", "--state", str(state_path), "--reviewed-journals", str(reviewed_dir / "reviewed_journals.json"), "--output", str(tb_md))

    assert tb.returncode == 0
    tb_payload = json.loads((tmp_path / "post_journal_trial_balance.json").read_text())
    assert tb_payload["summary"] == {"accounts": 3, "journals_included": 2, "excluded_journals": 0, "balanced_movements": True, "findings": 0}
    balances = {row["account_id"]: row for row in tb_payload["accounts"]}
    assert balances["acct_100"]["ending_balance"] == "1150.00"
    assert balances["acct_400"]["ending_balance"] == "-200.00"
    assert balances["acct_600"]["ending_balance"] == "50.00"

    mapping = run_cli("preview-statement-line-mapping", "--post-journal-tb", str(tmp_path / "post_journal_trial_balance.json"), "--output", str(tmp_path / "statement_line_mapping.md"))
    assert mapping.returncode == 0
    mapping_payload = json.loads((tmp_path / "statement_line_mapping.json").read_text())
    assert mapping_payload["summary"] == {"mapped_accounts": 3, "findings": 0}
    assert {row["statement"] for row in mapping_payload["mapped_accounts"]} == {"balance_sheet", "profit_and_loss"}

    draft = run_cli("render-draft-statements-from-tb", "--post-journal-tb", str(tmp_path / "post_journal_trial_balance.json"), "--mapping", str(tmp_path / "statement_line_mapping.json"), "--output-dir", str(tmp_path / "draft_statements"))
    assert draft.returncode == 0
    draft_payload = json.loads((tmp_path / "draft_statements" / "draft_statements.json").read_text())
    assert draft_payload["status"] == "internal_review_only"
    assert draft_payload["summary"]["mapping_findings"] == 0
    assert "internal_review_only" in (tmp_path / "draft_statements" / "draft_statements.md").read_text()


def test_statement_chain_release_gate_reports_missing_artifacts(tmp_path: Path):
    state_path = _approved_state(tmp_path)

    result = run_cli("inspect-statement-chain-readiness", "--state", str(state_path), "--artifact-dir", str(tmp_path), "--json")

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["statement_chain_ready"] is False
    assert "post_journal_trial_balance.json" in payload["missing_artifacts"]
