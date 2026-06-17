from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.engagement import EngagementState

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str):
    return subprocess.run([sys.executable, "-m", "accountant_copilot.cli", *args], cwd=ROOT, env={"PYTHONPATH": "src"}, text=True, capture_output=True, check=False)


def write_state(path: Path) -> None:
    path.write_text(EngagementState(engagement_id="internal_002", entity_name="Internal Trust", entity_type="discretionary_trust", fy_start="2024-07-01", fy_end="2025-06-30", documents_ref="docs", coa_ref="coa", decisions=[AccountantDecision(decision_id="decision_final_signoff_0001", question="release?", selected_option="final_signoff", rationale="ok", status=DecisionStatus.APPROVED, approved_by="Amelie")]).model_dump_json())


def test_render_xlsx_statements_writes_valid_workbook_and_verifier(tmp_path: Path):
    state = tmp_path / "state.json"
    workbook = tmp_path / "statements.xlsx"
    verifier = tmp_path / "xlsx_verifier.json"
    write_state(state)

    result = run_cli("render-xlsx-statements", "--state", str(state), "--output", str(workbook), "--verifier-result", str(verifier))

    assert result.returncode == 0, result.stderr
    assert workbook.exists()
    with zipfile.ZipFile(workbook) as archive:
        assert "xl/workbook.xml" in archive.namelist()
        assert "xl/worksheets/sheet1.xml" in archive.namelist()
    payload = json.loads(verifier.read_text())
    assert payload["status"] == "passed"
    data = json.loads(state.read_text())
    assert any(item["artifact_type"] == "xlsx_financial_statements" for item in data["output_artifacts"])


def test_export_local_ui_wrapper_and_document_intake_plan(tmp_path: Path):
    state = tmp_path / "state.json"
    review_ui = tmp_path / "review.html"
    wrapper = tmp_path / "local_ui" / "index.html"
    write_state(state)
    run_cli("export-review-ui", "--state", str(state), "--output", str(review_ui))

    result = run_cli("export-local-ui", "--state", str(state), "--review-ui", str(review_ui), "--output", str(wrapper))

    assert result.returncode == 0
    text = wrapper.read_text()
    assert "Internal Trust" in text
    assert "review.html" in text
    plan = (ROOT / "docs" / "DOCUMENT_INTAKE_PLAN.md").read_text()
    assert "PDF" in plan and "Excel" in plan and "source quote contract" in plan
