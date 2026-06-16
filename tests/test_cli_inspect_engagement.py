from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity


def test_inspect_engagement_cli_reports_blockers_and_next_task(tmp_path: Path) -> None:
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Australia Financial Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        exceptions=[
            ExceptionItem(
                source="matching_agent",
                severity=ExceptionSeverity.HIGH,
                category="unmatched_bank_transaction",
                description="Bank receipt has no supporting event.",
                recommended_action="Ask accountant to classify or provide missing support.",
                requires_human_approval=True,
            ),
            ExceptionItem(
                source="journal_agent",
                severity=ExceptionSeverity.LOW,
                category="rounding_note",
                description="Rounding note required.",
                recommended_action="Include in audit trail.",
            ),
        ],
    )
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "accountant_copilot.cli",
            "inspect-engagement",
            "--state",
            str(state_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Engagement: XYZ Australia Financial Trust" in result.stdout
    assert "FY: 2024-07-01 to 2025-06-30" in result.stdout
    assert "Open exceptions: 2" in result.stdout
    assert "Blocking exceptions: 1" in result.stdout
    assert "Human approvals needed: 1" in result.stdout
    assert "Final output allowed: NO" in result.stdout
    assert "Recommended next task: reviewer_agent" in result.stdout
    assert "Review and resolve open exception queue" in result.stdout


def test_inspect_engagement_cli_can_emit_json_for_clean_state(tmp_path: Path) -> None:
    state = EngagementState(
        engagement_id="clean_fy2025",
        entity_name="Clean Trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
    )
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "accountant_copilot.cli",
            "inspect-engagement",
            "--state",
            str(state_path),
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["engagement_id"] == "clean_fy2025"
    assert payload["final_output_allowed"] is True
    assert payload["recommended_next_task"]["agent_type"] == "financial_statement_agent"
