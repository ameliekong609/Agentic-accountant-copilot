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


def _state_with_ui_items(tmp_path: Path) -> Path:
    state = EngagementState(
        engagement_id="review_ui_flow",
        entity_name="Review UI Flow Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.chart_accounts.append(
        ChartAccount(
            account_id="acct_100",
            code="100",
            name="Cash",
            type="asset",
            presentation_group="Cash",
            opening_balance="100.00",
            status="pending_review",
        )
    )
    state.coa_review_status = "pending_review"
    state.adjustment_proposals.append(
        AdjustmentProposal(
            adjustment_id="journal_cash",
            description="Cash adjustment",
            debit_account="acct_100",
            credit_account="pending_review_offset",
            amount="10.00",
            date="2025-06-30",
            status="pending_review",
        )
    )
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())
    draft_dir = tmp_path / "draft_statements"
    draft_dir.mkdir()
    (draft_dir / "draft_statements.json").write_text(json.dumps({"status": "internal_review_only", "findings": []}))
    return state_path


def test_export_accountant_review_ui_generates_local_static_workbench(tmp_path: Path):
    state_path = _state_with_ui_items(tmp_path)
    output_dir = tmp_path / "accountant_review_ui"

    result = run_cli(
        "export-accountant-review-ui",
        "--state",
        str(state_path),
        "--artifact-dir",
        str(tmp_path),
        "--output-dir",
        str(output_dir),
    )

    assert result.returncode == 0
    index = output_dir / "index.html"
    app_js = output_dir / "app.js"
    bundle = output_dir / "review_ui_bundle.json"
    workbench = output_dir / "accountant_review_workbench.json"
    assert index.exists()
    assert app_js.exists()
    assert bundle.exists()
    assert workbench.exists()
    html = index.read_text()
    js = app_js.read_text()
    assert "Accountant Review Workbench" in html
    assert "CoA Review" in html
    assert "Journal Review" in html
    assert "Draft Statement Review" in html
    assert "Release Blockers" in html
    assert "Final Sign-off" in html
    assert "downloadWorkbench" in js
    assert "apply-accountant-review-workbench" in html
    assert "fetch(" not in js
    payload = json.loads(workbench.read_text())
    assert payload["sections"]["coa_accounts"][0]["action"] == ""
    assert payload["sections"]["journal_decisions"][0]["offset_account_id"] == ""
