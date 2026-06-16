from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.adapters.v2 import import_v2_exceptions
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionSeverity


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def test_import_v2_exceptions_maps_unmatched_and_verifier_findings(tmp_path: Path) -> None:
    step5 = tmp_path / "step5.json"
    step6 = tmp_path / "step6.json"
    _write_json(
        step5,
        {
            "matches": [],
            "unmatched_bank": [
                {
                    "statement_id": "stmt_1",
                    "row_index": 7,
                    "bank_account_id": "CBA123",
                    "date": "2025-01-10",
                    "description": "Unknown deposit",
                    "amount": 1000.0,
                    "direction": "in",
                    "user_classification": None,
                    "classification_reason": None,
                    "linked_coa_code": None,
                }
            ],
            "unmatched_events": [
                {
                    "event_id": "dist_1",
                    "event_type": "distribution_received",
                    "counterparty": "Fund A",
                    "date": "2025-06-30",
                    "net_cash_amount": 250.0,
                    "source_file": "fund_a.pdf",
                    "user_classification": "accrual",
                    "classification_reason": "Year-end accrual proposed by AI.",
                }
            ],
            "verifier_findings": [],
        },
    )
    _write_json(
        step6,
        {
            "entries": [],
            "coa_additions": [],
            "suspense_balance": 0,
            "verifier_findings": [
                {
                    "file": "step6:matches",
                    "row_name": "M0007",
                    "check": "matches_have_entries",
                    "detail": "Match M0007 has no journal entry.",
                }
            ],
        },
    )

    exceptions = import_v2_exceptions(step5_path=step5, step6_path=step6)

    assert len(exceptions) == 3
    by_category = {item.category: item for item in exceptions}
    assert by_category["v2_unmatched_bank_transaction"].severity == ExceptionSeverity.HIGH
    assert by_category["v2_unmatched_bank_transaction"].requires_human_approval is True
    assert "Unknown deposit" in by_category["v2_unmatched_bank_transaction"].description
    assert by_category["v2_unmatched_event"].severity == ExceptionSeverity.MEDIUM
    assert "Year-end accrual proposed" in by_category["v2_unmatched_event"].description
    assert by_category["v2_step6_matches_have_entries"].severity == ExceptionSeverity.CRITICAL
    assert "M0007" in by_category["v2_step6_matches_have_entries"].description


def test_import_v2_exceptions_cli_writes_engagement_state(tmp_path: Path) -> None:
    step5 = tmp_path / "step5.json"
    step6 = tmp_path / "step6.json"
    output = tmp_path / "engagement_state.json"
    _write_json(step5, {"matches": [], "unmatched_bank": [], "unmatched_events": [], "verifier_findings": []})
    _write_json(
        step6,
        {
            "entries": [],
            "coa_additions": [],
            "suspense_balance": 0,
            "verifier_findings": [
                {
                    "file": "step6:OVERALL",
                    "row_name": "ALL_ENTRIES",
                    "check": "overall_balanced",
                    "detail": "Total book does not balance.",
                }
            ],
        },
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "accountant_copilot.cli",
            "import-v2-exceptions",
            "--step5",
            str(step5),
            "--step6",
            str(step6),
            "--output",
            str(output),
            "--engagement-id",
            "xyz_fy2025",
            "--entity-name",
            "XYZ Trust",
            "--fy-start",
            "2024-07-01",
            "--fy-end",
            "2025-06-30",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Imported 1 V2 exception" in result.stdout
    loaded = EngagementState.model_validate_json(output.read_text())
    assert loaded.engagement_id == "xyz_fy2025"
    assert loaded.exceptions[0].severity == ExceptionSeverity.CRITICAL
