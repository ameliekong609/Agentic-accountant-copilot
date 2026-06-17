from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.engagement import EngagementState

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=ROOT,
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )


def _write_state(path: Path, **kwargs) -> None:
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        **kwargs,
    )
    path.write_text(state.model_dump_json())


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_ci_installs_dev_dependencies_before_pytest() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    assert "python3.11 -m pip install -e .[dev]" in workflow
    assert workflow.index("python3.11 -m pip install -e .[dev]") < workflow.index("python3.11 -m pytest")


def test_ingest_source_document_creates_document_and_row_evidence_idempotently(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    bank_csv = tmp_path / "bank.csv"
    _write_state(state_path)
    _write_csv(bank_csv, [{"date": "2025-01-10", "description": "Dividend", "amount": "1000.00"}])

    for _ in range(2):
        result = _run_cli(
            "ingest-source-document",
            "--state", str(state_path),
            "--document-id", "doc_bank",
            "--file-path", str(bank_csv),
            "--document-type", "bank_statement",
            "--entity", "XYZ Trust",
            "--period-start", "2025-01-01",
            "--period-end", "2025-01-31",
        )
        assert result.returncode == 0, result.stderr

    loaded = json.loads(state_path.read_text())
    assert [doc["document_id"] for doc in loaded["source_documents"]] == ["doc_bank"]
    evidence = [item for item in loaded["evidence"] if item["document_id"] == "doc_bank"]
    assert len(evidence) == 1
    assert evidence[0]["amount"] == "1000.00"
    assert evidence[0]["date"] == "2025-01-10"
    assert "Dividend" in evidence[0]["quote"]


def test_match_transactions_writes_matches_and_idempotent_exceptions(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    bank_csv = tmp_path / "bank.csv"
    events_csv = tmp_path / "events.csv"
    matches_path = tmp_path / "matches.json"
    _write_state(state_path)
    _write_csv(bank_csv, [
        {"date": "2025-01-10", "description": "Dividend", "amount": "1000.00"},
        {"date": "2025-01-11", "description": "Unknown", "amount": "50.00"},
    ])
    _write_csv(events_csv, [{"date": "2025-01-10", "description": "Dividend support", "amount": "1000.00"}])

    for _ in range(2):
        result = _run_cli(
            "match-transactions",
            "--state", str(state_path),
            "--bank-csv", str(bank_csv),
            "--events-csv", str(events_csv),
            "--output", str(matches_path),
        )
        assert result.returncode == 1

    matches = json.loads(matches_path.read_text())
    assert len(matches["matches"]) == 1
    assert len(matches["unmatched_bank_transactions"]) == 1
    loaded = json.loads(state_path.read_text())
    matching_exceptions = [item for item in loaded["exceptions"] if item["source"] == "deterministic_matching"]
    assert len(matching_exceptions) == 1
    assert matching_exceptions[0]["category"] == "unmatched_bank_transaction"


def test_render_draft_statements_registers_output_and_verifier_result(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    output = tmp_path / "draft.md"
    verifier = tmp_path / "verifier.json"
    _write_state(
        state_path,
        decisions=[AccountantDecision(
            decision_id="decision_final_signoff_0001",
            question="release?",
            selected_option="final_signoff",
            rationale="approved",
            status=DecisionStatus.APPROVED,
            approved_by="Amelie",
        )],
    )

    result = _run_cli(
        "render-draft-statements",
        "--state", str(state_path),
        "--output", str(output),
        "--verifier-result", str(verifier),
    )

    assert result.returncode == 0, result.stderr
    assert "# Draft Financial Statements" in output.read_text()
    verifier_payload = json.loads(verifier.read_text())
    assert verifier_payload["status"] == "passed"
    loaded = json.loads(state_path.read_text())
    assert loaded["output_artifacts"][-1]["file_path"] == str(output)
    assert loaded["output_artifacts"][-1]["verifier_status"] == "passed"


def test_review_ui_contains_downloadable_decision_json_template(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    output = tmp_path / "review.html"
    _write_state(state_path)

    result = _run_cli("export-review-ui", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    html = output.read_text()
    assert "review_decisions_template.json" in html
    assert "Copy decision JSON" in html


def test_demo_script_runs_blocked_path(tmp_path: Path) -> None:
    demo_output = tmp_path / "demo"
    result = _run_cli("run-demo", "--output-dir", str(demo_output))

    assert result.returncode == 0, result.stderr
    assert (demo_output / "blocked" / "review_packet" / "README.md").exists()
    assert (demo_output / "clean" / "release_manifest.json").exists()
