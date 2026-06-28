import json
import os
import subprocess
import sys
from pathlib import Path


def run_cli(*args: str):
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_export_accounting_facts_by_document_replaces_split_fact_artifacts(tmp_path: Path):
    state = tmp_path / "engagement_state.json"
    state.write_text(json.dumps({
        "engagement_id": "demo",
        "entity_name": "Demo Entity",
        "source_documents": [
            {"document_id": "raw_001", "file_path": "inputs/bank.pdf", "display_name": "2025-01-31 - Westpac Bank Statement - Account 123.pdf", "document_type": "bank_statement"},
            {"document_id": "raw_002", "file_path": "inputs/an3.pdf", "document_type": "investment_statement"},
            {"document_id": "raw_003", "file_path": "inputs/support.pdf", "document_type": "supporting_document"},
        ],
        "evidence": [
            {"document_id": "raw_001", "evidence_id": "raw_001_page_001"},
            {"document_id": "raw_002", "evidence_id": "raw_002_page_002"},
            {"document_id": "raw_003", "evidence_id": "raw_003_page_001"},
        ],
    }))
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    (artifact_dir / "bank_statement_facts.json").write_text(json.dumps({
        "facts": [{
            "document_id": "raw_001",
            "file_path": "inputs/bank.pdf",
            "page": "1",
            "evidence_id": "raw_001_page_001",
            "extraction_method": "ai",
            "ai_provider": "anthropic",
            "closing_balance": "100.00",
            "statement_period_start": "1 Jan 2025",
            "statement_period_end": "31 Jan 2025",
        }]
    }))
    (artifact_dir / "distribution_tax_facts.json").write_text(json.dumps({
        "facts": [{
            "document_id": "raw_002",
            "file_path": "inputs/an3.pdf",
            "page": "2",
            "evidence_id": "raw_002_page_002",
            "investment_name": "ANZ Capital Notes 9",
            "amount": "6,450.30",
            "components": {"franking_credit_tax_offset": "2,150.38"},
        }]
    }))

    output = tmp_path / "accounting_facts_by_document.json"
    result = run_cli(
        "export-accounting-facts-by-document",
        "--state", str(state),
        "--artifact-dir", str(artifact_dir),
        "--output", str(output),
    )

    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(output.read_text())
    assert payload["fact_type"] == "accounting_facts_by_document"
    assert payload["summary"] == {
        "uploaded_documents": 3,
        "documents_with_facts": 2,
        "accounting_fact_rows": 2,
        "documents_without_facts": 1,
    }
    docs = {doc["document_id"]: doc for doc in payload["documents"]}
    assert docs["raw_001"]["display_name"] == "2025-01-31 - Westpac Bank Statement - Account 123.pdf"
    assert docs["raw_001"]["accounting_facts"][0]["fact_type"] == "bank_statement"
    assert docs["raw_001"]["accounting_facts"][0]["extraction_method"] == "ai"
    assert docs["raw_001"]["accounting_facts"][0]["page"] == "1"
    assert docs["raw_002"]["accounting_facts"][0]["fields"]["components"]["franking_credit_tax_offset"] == "2,150.38"
    assert docs["raw_003"]["status"] == "no_fact_extracted"
    assert docs["raw_003"]["accounting_facts"] == []
    assert docs["raw_003"]["no_fact_reason"] == "No accounting fact extractor is mapped for document type `supporting_document`."


def test_export_accounting_facts_by_document_removes_legacy_split_fact_files(tmp_path: Path):
    state = tmp_path / "engagement_state.json"
    state.write_text(json.dumps({
        "engagement_id": "demo",
        "entity_name": "Demo Entity",
        "source_documents": [
            {"document_id": "raw_001", "file_path": "inputs/bank.pdf", "document_type": "bank_statement"},
        ],
    }))
    internal_dir = tmp_path / ".internal_facts"
    internal_dir.mkdir()
    (internal_dir / "bank_statement_facts.json").write_text(json.dumps({
        "facts": [{"document_id": "raw_001", "file_path": "inputs/bank.pdf", "evidence_id": "raw_001_page_001", "closing_balance": "100.00"}]
    }))
    legacy_root = tmp_path / "artifacts"
    legacy_root.mkdir()
    legacy_files = [
        legacy_root / "bank_statement_facts.json",
        legacy_root / "bank_statement_facts.md",
        legacy_root / "bank_transactions.json",
        legacy_root / "bank_transactions.md",
        legacy_root / "invoice_facts.json",
        legacy_root / "invoice_facts.md",
        legacy_root / "distribution_tax_facts.json",
        legacy_root / "distribution_tax_facts.md",
        legacy_root / "broker_trade_facts.json",
        legacy_root / "broker_trade_facts.md",
    ]
    for path in legacy_files:
        path.write_text("legacy")

    output = legacy_root / "accounting_facts_by_document.json"
    result = run_cli(
        "export-accounting-facts-by-document",
        "--state", str(state),
        "--artifact-dir", str(internal_dir),
        "--output", str(output),
        "--remove-legacy-split-facts",
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert output.exists()
    assert not (internal_dir / "bank_statement_facts.json").exists()
    assert all(not path.exists() for path in legacy_files)
