from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

from accountant_copilot.tb_bridge_workflow import RELATIONSHIP_REASONING_CONTRACT_VERSION, TB_BRIDGE_CONTRACT_VERSION
from scripts.check_workpaper_quality import check_workpaper_quality


def _write_minimal_workbook(path: Path) -> None:
    workbook_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheets>
    <sheet name="TB Bridge" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
    <sheet name="Movement Notes" sheetId="2" r:id="rId2" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
    <sheet name="Evidence Index" sheetId="3" r:id="rId3" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
  </sheets>
</workbook>
"""
    evidence_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="inputs/source.pdf" TargetMode="External"/>
</Relationships>
"""
    with ZipFile(path, "w") as workbook:
        workbook.writestr("xl/workbook.xml", workbook_xml)
        workbook.writestr("xl/worksheets/_rels/sheet3.xml.rels", evidence_rels)


def test_workpaper_quality_check_passes_valid_artifacts(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    output_dir = tmp_path / "workpaper"
    artifact_dir.mkdir()
    output_dir.mkdir()
    (artifact_dir / "source_document_index.json").write_text(
        json.dumps(
            {
                "entity_name": "Quality Client",
                "documents": [{"document_id": "raw_001", "display_name": "Bank statement.pdf", "file_path": "inputs/bank.pdf"}],
            }
        )
    )
    (artifact_dir / "relationship_reasoning_register.json").write_text(
        json.dumps(
            {
                "artifact_type": "relationship_reasoning_register",
                "relationship_reasoning_contract_version": RELATIONSHIP_REASONING_CONTRACT_VERSION,
                "status": "ready",
                "relationships": [
                    {
                        "relationship_id": "rel_001",
                        "status": "ready_for_bridge",
                        "confidence": "high",
                        "story": "Bank receipt supports income.",
                        "document_refs": ["raw_001"],
                        "evidence_nodes": [{"document_refs": ["raw_001"]}],
                    }
                ],
                "prior_fs_account_movement_coverage": [],
                "investigation_log": [],
            }
        )
    )
    (artifact_dir / "prior_statement_coa_import.json").write_text(
        json.dumps({"accounts": [{"name": "Cash at Bank", "type": "asset", "opening_balance": "0.00"}]})
    )
    (output_dir / "tb_bridge_workpaper.json").write_text(
        json.dumps(
            {
                "artifact_type": "tb_bridge_workpaper",
                "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
                "status": "ready",
                "agent": "codex_cli",
                "accounts": [
                    {"account_name": "Cash at Bank", "account_type": "asset", "statement_group": "Cash", "opening_balance": "0.00"},
                    {"account_name": "Income", "account_type": "income", "statement_group": "Income", "opening_balance": "0.00"},
                ],
                "movement_columns": [
                    {
                        "column_key": "bank",
                        "label": "Bank",
                        "movement_role": {"role_type": "cash_account_movement"},
                        "support_type": "direct_evidence",
                    }
                ],
                "matrix_rows": [
                    {
                        "account_name": "Cash at Bank",
                        "account_type": "asset",
                        "opening_balance": "0.00",
                        "movements": [{"column_key": "bank", "amount": "100.00", "support_type": "direct_evidence", "relationship_id": "rel_001", "note_id": "N001"}],
                        "closing_balance": "100.00",
                        "row_status": "ready",
                    },
                    {
                        "account_name": "Income",
                        "account_type": "income",
                        "opening_balance": "0.00",
                        "movements": [{"column_key": "bank", "amount": "-100.00", "support_type": "direct_evidence", "relationship_id": "rel_001", "note_id": "N001"}],
                        "closing_balance": "-100.00",
                        "row_status": "ready",
                    },
                ],
                "movement_notes": [{"note_id": "N001", "account_name": "Income", "status": "ready", "relationship_ids": ["rel_001"]}],
                "relationship_coverage": [{"relationship_id": "rel_001", "matrix_status": "included"}],
                "summary": {"matrix_rows": 2, "movement_columns": 1, "movement_notes": 1},
            }
        )
    )
    (output_dir / "turing_senior_review.json").write_text(
        json.dumps(
            {
                "artifact_type": "turing_senior_accountant_review",
                "review_contract_version": "turing_senior_review_v1",
                "status": "ready",
                "control_checks": [],
                "sampled_items": [],
                "findings": [],
                "correction_briefs": [],
                "summary": {},
            }
        )
    )
    _write_minimal_workbook(output_dir / "step4_tb_bridge_workpaper.xlsx")

    result = check_workpaper_quality(artifact_dir, output_dir)

    assert result["status"] == "pass"
    assert result["severity_counts"]["high"] == 0


def test_workpaper_quality_check_blocks_unbalanced_columns(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    output_dir = tmp_path / "workpaper"
    artifact_dir.mkdir()
    output_dir.mkdir()
    (artifact_dir / "source_document_index.json").write_text(json.dumps({"documents": [{"document_id": "raw_001"}]}))
    (artifact_dir / "relationship_reasoning_register.json").write_text(
        json.dumps(
            {
                "artifact_type": "relationship_reasoning_register",
                "relationship_reasoning_contract_version": RELATIONSHIP_REASONING_CONTRACT_VERSION,
                "relationships": [{"relationship_id": "rel_001", "status": "ready_for_bridge", "confidence": "high", "story": "ok", "document_refs": ["raw_001"]}],
            }
        )
    )
    (artifact_dir / "prior_statement_coa_import.json").write_text(json.dumps({"accounts": [{"name": "Cash at Bank"}]}))
    (output_dir / "tb_bridge_workpaper.json").write_text(
        json.dumps(
            {
                "artifact_type": "tb_bridge_workpaper",
                "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
                "accounts": [{"account_name": "Cash at Bank", "account_type": "asset", "opening_balance": "0.00"}],
                "movement_columns": [{"column_key": "bank", "label": "Bank", "movement_role": {"role_type": "cash_account_movement"}}],
                "matrix_rows": [{"account_name": "Cash at Bank", "account_type": "asset", "opening_balance": "0.00", "movements": [{"column_key": "bank", "amount": "100.00", "support_type": "direct_evidence", "relationship_id": "rel_001"}], "closing_balance": "100.00"}],
                "movement_notes": [{"note_id": "N001", "status": "ready"}],
                "relationship_coverage": [{"relationship_id": "rel_001", "matrix_status": "included"}],
            }
        )
    )
    (output_dir / "turing_senior_review.json").write_text(json.dumps({"status": "ready", "correction_briefs": []}))
    _write_minimal_workbook(output_dir / "step4_tb_bridge_workpaper.xlsx")

    result = check_workpaper_quality(artifact_dir, output_dir)

    assert result["status"] == "fail"
    assert any(finding["category"] == "unbalanced_movement_columns" for finding in result["blocking_findings"])
