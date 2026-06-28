from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.artifacts import SourceDocument
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.evidence import EvidenceRef

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


def test_export_document_inventory_groups_page_evidence_with_dates_amounts_and_tags(tmp_path: Path):
    state = EngagementState(
        engagement_id="inventory_test",
        entity_name="Inventory Trust",
        entity_type="trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
    )
    state.source_documents.append(
        SourceDocument(
            document_id="doc_bank",
            file_path="inputs/eStatement.pdf",
            document_type="bank_statement",
            entity="Inventory Trust",
            period_start="2024-07-01",
            period_end="2025-06-30",
            source_hash="abc123",
            original_file_name="eStatement.pdf",
            display_name="2025-06-30 - Westpac Bank Statement - Account 027.pdf",
            naming_confidence="high",
            naming_status="suggested",
            naming_method="deterministic",
            naming_evidence_refs=["ev_page_1"],
        )
    )
    state.evidence.append(
        EvidenceRef(
            evidence_id="ev_page_1",
            source_type="bank_statement",
            file_path="inputs/eStatement.pdf",
            document_id="doc_bank",
            page="1",
            quote="Bank statement closing balance 30/06/2025 $1,234.56 interest received",
            confidence="text_pdf",
        )
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(state.model_dump_json())
    output = tmp_path / "document_inventory.md"

    result = run_cli("export-document-inventory", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    text = output.read_text()
    assert "# Document Inventory" in text
    assert "inputs/eStatement.pdf" in text
    assert "2025-06-30 - Westpac Bank Statement - Account 027.pdf" in text
    assert "Display name: 2025-06-30 - Westpac Bank Statement - Account 027.pdf" in text
    assert "Suggested name" not in text
    assert "Naming status: suggested" in text
    assert "bank_statement" in text
    assert "Page 1" in text
    assert "30/06/2025" in text
    assert "$1,234.56" in text
    assert "bank" in text
    assert "interest" in text
    payload = json.loads((tmp_path / "document_inventory.json").read_text())
    assert payload["documents"][0]["evidence_count"] == 1
    assert payload["documents"][0]["display_name"] == "2025-06-30 - Westpac Bank Statement - Account 027.pdf"
    assert payload["documents"][0]["naming_method"] == "deterministic"
    assert payload["documents"][0]["pages"][0]["amounts"] == ["$1,234.56"]
    assert "interest" in payload["documents"][0]["tags"]
