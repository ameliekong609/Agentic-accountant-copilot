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


def _state_with_review_items(tmp_path: Path) -> Path:
    state = EngagementState(
        engagement_id="workbench_flow",
        entity_name="Workbench Flow Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.chart_accounts.extend([
        ChartAccount(account_id="acct_100", code="100", name="Cash", type="asset", presentation_group="Cash", opening_balance="100.00", status="pending_review"),
        ChartAccount(account_id="acct_400", code="400", name="Income", type="income", presentation_group="Revenue", opening_balance="0.00", status="approved"),
    ])
    state.coa_review_status = "pending_review"
    state.adjustment_proposals.append(
        AdjustmentProposal(
            adjustment_id="journal_income",
            description="Income journal",
            debit_account="acct_100",
            credit_account="pending_review_offset",
            amount="25.00",
            date="2025-06-30",
            status="pending_review",
            source_evidence_refs=["ev_income"],
        )
    )
    state.adjustment_review_status = "pending_review"
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())
    draft_dir = tmp_path / "draft_statements"
    draft_dir.mkdir()
    (draft_dir / "draft_statements.json").write_text(json.dumps({
        "engagement_id": state.engagement_id,
        "entity_name": state.entity_name,
        "status": "internal_review_only",
        "findings": [],
        "summary": {"mapping_findings": 0, "tb_findings": 0},
    }))
    (draft_dir / "draft_statements.md").write_text("# Draft\nStatus: internal_review_only\n")
    return state_path


def test_accountant_review_workbench_exports_and_applies_partial_decisions(tmp_path: Path):
    state_path = _state_with_review_items(tmp_path)
    workbench = tmp_path / "accountant_review_workbench.json"

    exported = run_cli("export-accountant-review-workbench", "--state", str(state_path), "--artifact-dir", str(tmp_path), "--output", str(workbench))

    assert exported.returncode == 0
    payload = json.loads(workbench.read_text())
    assert payload["sections"]["coa_accounts"][0]["action"] == ""
    assert payload["sections"]["journal_decisions"][0]["offset_account_id"] == ""
    assert payload["sections"]["draft_statement_review"]["draft_status"] == "internal_review_only"
    assert (tmp_path / "accountant_review_workbench.md").exists()

    payload["sections"]["coa_accounts"][0].update({"action": "approve", "approved_by": "Reviewer", "rationale": "Account agrees to source."})
    payload["sections"]["journal_decisions"][0].update({"action": "approve", "offset_account_id": "acct_400", "approved_by": "Reviewer", "rationale": "Income offset confirmed."})
    payload["sections"]["draft_statement_review"]["decision"].update({"action": "approve", "approved_by": "Reviewer", "rationale": "Draft agrees to reviewed TB."})
    workbench.write_text(json.dumps(payload))

    applied = run_cli("apply-accountant-review-workbench", "--state", str(state_path), "--workbench", str(workbench), "--artifact-dir", str(tmp_path), "--output", str(tmp_path / "applied_accountant_review_workbench.json"))

    assert applied.returncode == 0
    updated = json.loads(state_path.read_text())
    assert updated["coa_review_status"] == "approved"
    journal = updated["adjustment_proposals"][0]
    assert journal["status"] == "approved"
    assert journal["credit_account"] == "acct_400"
    selected = [decision["selected_option"] for decision in updated["decisions"]]
    assert "approve_coa" in selected
    assert "approve_journal" in selected
    assert "approve_draft_statements" in selected


def test_release_blockers_and_review_ui_bundle(tmp_path: Path):
    state_path = _state_with_review_items(tmp_path)

    explained = run_cli("explain-release-blockers", "--state", str(state_path), "--artifact-dir", str(tmp_path), "--output", str(tmp_path / "release_blockers.md"))

    assert explained.returncode == 1
    blocker_payload = json.loads((tmp_path / "release_blockers.json").read_text())
    categories = {item["category"] for item in blocker_payload["blockers"]}
    assert {"coa", "journal", "statement", "release_candidate", "final_signoff"}.issubset(categories)
    md = (tmp_path / "release_blockers.md").read_text()
    assert "Required action" in md

    bundled = run_cli("export-review-ui-bundle", "--state", str(state_path), "--artifact-dir", str(tmp_path), "--output-dir", str(tmp_path / "review_ui_bundle"))

    assert bundled.returncode == 0
    bundle = json.loads((tmp_path / "review_ui_bundle" / "review_ui_bundle.json").read_text())
    assert bundle["engagement_id"] == "workbench_flow"
    assert "workbench" in bundle
    assert "release_blockers" in bundle
    assert (tmp_path / "review_ui_bundle" / "README.md").exists()
