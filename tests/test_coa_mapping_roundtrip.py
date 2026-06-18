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


def test_export_coa_mapping_decision_template_and_apply_approvals(tmp_path: Path):
    state = EngagementState(engagement_id="mapping_roundtrip", entity_name="Mapping Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.append(ChartAccount(account_id="acct_400", code="400", name="Distribution Income", type="income", presentation_group="Revenue", opening_balance="0.00"))
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    mappings = {
        "engagement_id": "mapping_roundtrip",
        "suggestions": [{"source_fact_type": "distribution_tax", "source_evidence_id": "dist_ev", "candidate_account_id": "acct_400", "candidate_account_code": "400", "candidate_account_name": "Distribution Income", "amount": "1,333.32", "approved": False, "evidence_refs": ["dist_ev", "acct_400"]}],
        "summary": {"source_facts": 1, "suggestions": 1, "findings": 1, "approved": 0},
    }
    mappings_path = tmp_path / "coa_mapping_suggestions.json"
    mappings_path.write_text(json.dumps(mappings))
    template = tmp_path / "coa_mapping_decisions_template.json"

    exported = run_cli("export-coa-mapping-template", "--mappings", str(mappings_path), "--output", str(template))

    assert exported.returncode == 0
    payload = json.loads(template.read_text())
    assert payload["mapping_decisions"][0]["action"] == ""
    payload["mapping_decisions"][0].update({"action": "approve", "approved_by": "Reviewer", "rationale": "Agrees to map distribution to income."})
    decisions = tmp_path / "coa_mapping_decisions.json"
    decisions.write_text(json.dumps(payload))
    applied_output = tmp_path / "applied_mapping_decisions.json"

    applied = run_cli("apply-coa-mapping-decisions", "--state", str(state_path), "--mappings", str(mappings_path), "--decisions", str(decisions), "--output", str(applied_output))

    assert applied.returncode == 0
    updated = json.loads(state_path.read_text())
    assert len(updated["decisions"]) == 1
    decision = updated["decisions"][0]
    assert decision["selected_option"] == "approve_coa_mapping"
    assert decision["evidence_refs"] == ["dist_ev", "acct_400"]
    result_payload = json.loads(applied_output.read_text())
    assert result_payload["summary"] == {"approved": 1, "rejected": 0, "applied": 1}


def test_apply_coa_mapping_decisions_rejects_unknown_mapping_id(tmp_path: Path):
    state = EngagementState(engagement_id="mapping_roundtrip", entity_name="Mapping Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    mappings_path = tmp_path / "coa_mapping_suggestions.json"
    mappings_path.write_text(json.dumps({"suggestions": []}))
    decisions = {"mapping_decisions": [{"mapping_id": "missing", "action": "approve", "approved_by": "Reviewer", "rationale": "No."}]}
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(json.dumps(decisions))

    result = run_cli("apply-coa-mapping-decisions", "--state", str(state_path), "--mappings", str(mappings_path), "--decisions", str(decisions_path), "--output", str(tmp_path / "out.json"))

    assert result.returncode == 2
    assert "Unknown mapping_id" in result.stderr
