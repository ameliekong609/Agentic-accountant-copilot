from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.engagement import EngagementState

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str):
    return subprocess.run([sys.executable, "-m", "accountant_copilot.cli", *args], cwd=ROOT, env={"PYTHONPATH": "src"}, text=True, capture_output=True, check=False)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_state(path: Path) -> None:
    path.write_text(EngagementState(engagement_id="internal_001", entity_name="Internal Trust", entity_type="discretionary_trust", fy_start="2024-07-01", fy_end="2025-06-30", documents_ref="docs", coa_ref="coa").model_dump_json())


def test_run_engagement_internal_inputs_exports_review_outputs_and_stops_at_gate(tmp_path: Path):
    state = tmp_path / "state.json"
    bank = tmp_path / "bank.csv"
    events = tmp_path / "events.csv"
    tb = tmp_path / "trial_balance.csv"
    packet = tmp_path / "review_packet"
    ui = tmp_path / "review.html"
    statements = tmp_path / "statements"
    write_state(state)
    write_csv(bank, [{"date": "2025-01-10", "description": "Dividend REF123", "amount": "100.00"}, {"date": "2025-01-12", "description": "Unmatched", "amount": "50.00"}])
    write_csv(events, [{"date": "2025-01-10", "description": "Dividend support REF123", "amount": "100.00"}])
    write_csv(tb, [{"code": "1000", "name": "Cash", "type": "asset", "presentation_group": "Current assets", "balance": "150.00"}])

    result = run_cli(
        "run-engagement",
        "--state", str(state),
        "--bank-csv", str(bank),
        "--events-csv", str(events),
        "--trial-balance-csv", str(tb),
        "--statement-package-dir", str(statements),
        "--review-packet-dir", str(packet),
        "--review-ui", str(ui),
        "--amount-tolerance", "0.01",
        "--date-window-days", "1",
    )

    assert result.returncode == 1
    assert "Engagement blocked" in result.stdout
    assert (packet / "open_exceptions.md").exists()
    assert ui.exists()
    assert (statements / "balance_sheet.md").exists()
    data = json.loads(state.read_text())
    assert data["matches_ref"]
    assert data["statements_ref"] == str(statements)
    assert any(item["category"] == "unmatched_bank_transaction" for item in data["exceptions"])


def test_apply_review_ui_decisions_round_trip_updates_state(tmp_path: Path):
    state = tmp_path / "state.json"
    decisions = tmp_path / "review_decisions.json"
    write_state(state)
    run_cli("record-coa-account", "--state", str(state), "--account-id", "acct_cash", "--code", "1000", "--name", "Cash", "--type", "asset", "--presentation-group", "Current assets", "--opening-balance", "100.00")
    run_cli("record-adjustment", "--state", str(state), "--adjustment-id", "adj_1", "--description", "Accrual", "--debit-account", "Expense", "--credit-account", "Accrued expenses", "--amount", "25.00", "--date", "2025-06-30")
    bad_bank = tmp_path / "bank.csv"
    events = tmp_path / "events.csv"
    out = tmp_path / "matches.json"
    write_csv(bad_bank, [{"date": "2025-01-12", "description": "Unmatched", "amount": "50.00"}])
    write_csv(events, [{"date": "2025-01-10", "description": "Support", "amount": "100.00"}])
    run_cli("match-transactions", "--state", str(state), "--bank-csv", str(bad_bank), "--events-csv", str(events), "--output", str(out))
    data = json.loads(state.read_text())
    exception_id = next(item["exception_id"] for item in data["exceptions"] if item["category"] == "unmatched_bank_transaction")
    decisions.write_text(json.dumps({
        "engagement_id": "internal_001",
        "decisions": [{"exception_id": exception_id, "action": "resolved", "rationale": "Classified internally.", "approved_by": "Amelie"}],
        "coa_decisions": [{"account_id": "acct_cash", "action": "approve", "rationale": "CoA ok.", "approved_by": "Amelie"}],
        "adjustment_decisions": [{"adjustment_id": "adj_1", "action": "approve", "rationale": "Adjustment ok.", "approved_by": "Amelie"}],
        "preference_decisions": [],
        "output_verifier_decisions": []
    }))

    result = run_cli("apply-review-ui-decisions", "--state", str(state), "--decisions", str(decisions))

    assert result.returncode == 0, result.stderr
    data = json.loads(state.read_text())
    assert data["coa_review_status"] == "approved"
    assert data["adjustment_proposals"][0]["status"] == "approved"
    assert all(item["status"] != "open" for item in data["exceptions"] if item["exception_id"] == exception_id)
    assert len(data["decisions"]) >= 3
