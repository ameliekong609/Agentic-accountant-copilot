from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.engagement import EngagementState

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str):
    return subprocess.run([sys.executable, "-m", "accountant_copilot.cli", *args], cwd=ROOT, env={"PYTHONPATH": "src"}, text=True, capture_output=True, check=False)


def write_state(path: Path) -> None:
    path.write_text(EngagementState(engagement_id="raw_test", entity_name="Raw Trust", entity_type="discretionary_trust", fy_start="2024-07-01", fy_end="2025-06-30", documents_ref="inputs", coa_ref="inputs/prior.pdf").model_dump_json())


def test_ingest_raw_inputs_registers_documents_and_blocks_unextracted_sources(tmp_path: Path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "bank.pdf").write_bytes(b"%PDF-1.4\nraw bank statement")
    (input_dir / "conventions.md").write_text("Use internal conventions.")
    state = tmp_path / "state.json"
    write_state(state)

    result = run_cli("ingest-raw-inputs", "--state", str(state), "--input-dir", str(input_dir))

    assert result.returncode == 1
    assert "extraction-required" in result.stdout
    data = json.loads(state.read_text())
    assert len(data["source_documents"]) == 2
    assert len(data["evidence"]) == 1
    assert data["evidence"][0]["source_type"] == "client_conventions"
    exceptions = {item["category"]: item for item in data["exceptions"]}
    assert "source_extraction_required" in exceptions
    assert exceptions["source_extraction_required"]["severity"] == "high"

    inspected = run_cli("inspect-engagement", "--state", str(state), "--json")
    payload = json.loads(inspected.stdout)
    assert inspected.returncode == 1
    assert payload["final_output_allowed"] is False
    assert payload["blocking_exception_count"] >= 1


def test_run_engagement_from_raw_input_dir_exports_review_packet(tmp_path: Path):
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    (input_dir / "eStatement.pdf").write_bytes(b"%PDF-1.4\nbank")
    state = tmp_path / "state.json"
    packet = tmp_path / "review_packet"
    ui = tmp_path / "review.html"
    write_state(state)

    result = run_cli("run-engagement", "--state", str(state), "--input-dir", str(input_dir), "--review-packet-dir", str(packet), "--review-ui", str(ui))

    assert result.returncode == 1
    assert "Engagement blocked" in result.stdout
    assert (packet / "open_exceptions.md").exists()
    assert ui.exists()
    open_text = (packet / "open_exceptions.md").read_text()
    assert "source_extraction_required" in open_text
