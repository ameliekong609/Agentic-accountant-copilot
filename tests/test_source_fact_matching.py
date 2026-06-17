from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

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


def test_match_source_facts_links_bank_transactions_to_invoice_distribution_and_broker_facts(tmp_path: Path):
    bank_transactions = {
        "engagement_id": "match_test",
        "entity_name": "Match Trust",
        "fact_type": "bank_transactions",
        "transactions": [
            {"transaction_date": "23/01/2025", "description": "Payment Emerald INV-0082", "debit": "1,100.00", "credit": None, "balance": "9,000.00", "evidence_id": "bank_ev_1", "file_path": "bank.pdf", "page": "1"},
            {"transaction_date": "23/09/2024", "description": "Distribution WBCPM", "debit": None, "credit": "1,333.32", "balance": "10,333.32", "evidence_id": "bank_ev_2", "file_path": "bank.pdf", "page": "1"},
            {"transaction_date": "10/03/2025", "description": "Broker settlement", "debit": None, "credit": "12,345.67", "balance": "22,679.99", "evidence_id": "bank_ev_3", "file_path": "bank.pdf", "page": "1"},
        ],
        "findings": [],
        "summary": {"bank_documents": 1, "transactions_extracted": 3, "findings": 0},
    }
    invoice_facts = {"facts": [{"invoice_number": "INV-0082", "amount_due": "1,100.00", "due_date": "23/01/2025", "evidence_id": "invoice_ev_1", "file_path": "invoice.png", "page": "1"}]}
    distribution_facts = {"facts": [{"payment_date": "23 September 2024", "components": {"net_cash_distribution": "1,333.32"}, "evidence_id": "dist_ev_1", "file_path": "dist.pdf", "page": "1"}]}
    broker_facts = {"facts": [{"fields": {"settlement_date": "10/03/2025", "settlement_amount": "12,345.67"}, "evidence_id": "broker_ev_1", "file_path": "broker.pdf", "page": "1"}]}
    for name, payload in [("bank.json", bank_transactions), ("invoice.json", invoice_facts), ("dist.json", distribution_facts), ("broker.json", broker_facts)]:
        (tmp_path / name).write_text(json.dumps(payload))
    output = tmp_path / "source_fact_matches.md"

    result = run_cli(
        "match-source-facts",
        "--bank-transactions", str(tmp_path / "bank.json"),
        "--invoice-facts", str(tmp_path / "invoice.json"),
        "--distribution-tax-facts", str(tmp_path / "dist.json"),
        "--broker-trade-facts", str(tmp_path / "broker.json"),
        "--output", str(output),
    )

    assert result.returncode == 0
    payload = json.loads((tmp_path / "source_fact_matches.json").read_text())
    assert payload["summary"] == {"bank_transactions": 3, "source_facts": 3, "matches": 3, "findings": 0}
    assert {match["source_fact_type"] for match in payload["matches"]} == {"invoice", "distribution_tax", "broker_trade"}
    for match in payload["matches"]:
        assert match["approved"] is False
        assert len(match["evidence_refs"]) == 2


def test_match_source_facts_reports_ambiguous_and_unmatched_candidates(tmp_path: Path):
    bank_transactions = {"entity_name": "Match Trust", "transactions": [
        {"transaction_date": "23/01/2025", "description": "Payment 1", "debit": "1,100.00", "credit": None, "evidence_id": "bank_ev_1"},
        {"transaction_date": "23/01/2025", "description": "Payment 2", "debit": "1,100.00", "credit": None, "evidence_id": "bank_ev_2"},
    ], "findings": []}
    invoice_facts = {"facts": [
        {"invoice_number": "INV-0082", "amount_due": "1,100.00", "due_date": "23/01/2025", "evidence_id": "invoice_ev_1"},
        {"invoice_number": "INV-0099", "amount_due": "99.99", "due_date": "24/01/2025", "evidence_id": "invoice_ev_2"},
    ]}
    (tmp_path / "bank.json").write_text(json.dumps(bank_transactions))
    (tmp_path / "invoice.json").write_text(json.dumps(invoice_facts))
    output = tmp_path / "source_fact_matches.md"

    result = run_cli("match-source-facts", "--bank-transactions", str(tmp_path / "bank.json"), "--invoice-facts", str(tmp_path / "invoice.json"), "--output", str(output))

    assert result.returncode == 1
    payload = json.loads((tmp_path / "source_fact_matches.json").read_text())
    assert payload["summary"] == {"bank_transactions": 2, "source_facts": 2, "matches": 0, "findings": 2}
    assert {finding["category"] for finding in payload["findings"]} == {"ambiguous_source_fact_bank_match", "source_fact_bank_match_missing"}
