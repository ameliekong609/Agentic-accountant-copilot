from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src", **(env or {})},
        text=True,
        capture_output=True,
        check=False,
    )


def _write_source_index(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "entity_name": "Match Trust",
                "documents": [
                    {
                        "document_id": "raw_001",
                        "display_name": "2024-11-30 - CBA Bank Statement - Account 027.pdf",
                        "document_type": "bank_statement",
                        "file_path": "inputs/cba.pdf",
                        "document_summary": "CBA bank statement.",
                    },
                    {
                        "document_id": "raw_002",
                        "display_name": "2024-11-04 - Capital Call Notice.pdf",
                        "document_type": "capital_call",
                        "file_path": "inputs/capital-call.pdf",
                        "document_summary": "EVP capital call notice.",
                    },
                    {
                        "document_id": "raw_003",
                        "display_name": "2024-06-30 - Match Trust - Financial Statements.pdf",
                        "document_type": "prior_year_financial_statements",
                        "file_path": "inputs/fy24-fs.pdf",
                        "document_summary": "Prior-year financial statements.",
                    },
                ],
                "accounting_fact_rows": 0,
            }
        )
    )


def _relationship_payload(contract_version: str) -> dict:
    return {
        "artifact_type": "relationship_reasoning_register",
        "relationship_reasoning_contract_version": contract_version,
        "status": "needs_attention",
        "agent": "codex_cli",
        "relationships": [
            {
                "relationship_id": "rel_001",
                "relationship_type": "source_bank_match",
                "status": "ready_for_bridge",
                "confidence": "high",
                "evidence_level": "source_and_bank",
                "story": "EVP capital call source and CBA payment agree on amount, but payee pathway still needs review.",
                "date": "2024-11-04",
                "amount": "30000.00",
                "direction": "payment",
                "document_refs": ["raw_001", "raw_002"],
                "evidence_nodes": [
                    {"node_id": "ev_001", "node_type": "source_document", "document_refs": ["raw_002"], "amount": "30000.00"},
                    {"node_id": "ev_002", "node_type": "bank_transaction", "document_refs": ["raw_001"], "amount": "30000.00"},
                ],
                "derived_nodes": [],
                "matrix_hints": [{"account_name": "EVP Fund III", "column": "EVP capital", "amount": "30000.00"}],
                "accounts_involved": [{"account_name": "EVP Fund III", "role": "investment", "source": "prior_fs", "confidence": "medium"}],
            }
        ],
        "prior_fs_account_movement_coverage": [
            {
                "account_name": "EVP Fund III",
                "statement_section": "Balance sheet",
                "opening_or_comparative_amount": "92500.00",
                "coverage_status": "movement_explained",
                "relationship_ids": ["rel_001"],
                "movement_story": "EVP opening investment is increased by the matched capital call payment.",
                "step4_column_hint": "EVP capital calls",
            }
        ],
        "investigation_log": ["Read source and bank documents before deciding the relationship."],
        "validation_findings": [],
    }


def _write_step4_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    from accountant_copilot import cli

    relationship_path = tmp_path / "relationship_reasoning_register.json"
    relationship_path.write_text(json.dumps(_relationship_payload(cli.SOURCE_MATCHING_CONTRACT_VERSION)))
    source_index_path = tmp_path / "source_document_index.json"
    _write_source_index(source_index_path)
    prior_coa_path = tmp_path / "prior_coa.json"
    prior_coa_path.write_text(
        json.dumps(
            {
                "prior_fs_document_id": "raw_003",
                "accounts": [
                    {
                        "account_id": "acct_anz",
                        "code": "",
                        "name": "ANZ - Capital Notes 9",
                        "type": "asset",
                        "presentation_group": "Investments",
                        "opening_balance": "540000.00",
                    }
                ],
            }
        )
    )
    return relationship_path, source_index_path, prior_coa_path


def test_codex_relationship_reasoning_uses_source_index_artifact(tmp_path: Path):
    from accountant_copilot import cli

    source_index_path = tmp_path / "source_document_index.json"
    _write_source_index(source_index_path)
    output = tmp_path / "source_fact_matches.md"
    fake_codex_payload = _relationship_payload(cli.SOURCE_MATCHING_CONTRACT_VERSION)

    result = run_cli(
        "match-source-facts",
        "--accounting-facts",
        str(source_index_path),
        "--codex-command",
        "codex exec",
        "--codex-max-attempts",
        "1",
        "--output",
        str(output),
        env={"ACCOUNTANT_COPILOT_FAKE_CODEX_SOURCE_MATCH_JSON": json.dumps(fake_codex_payload)},
    )

    assert result.returncode == 0
    payload = json.loads((tmp_path / "relationship_reasoning_register.json").read_text())
    assert payload["artifact_type"] == "relationship_reasoning_register"
    assert payload["agent"] == "codex_cli"
    assert payload["summary"]["relationships"] == 1
    assert payload["summary"]["prior_fs_accounts_considered"] == 1
    assert payload["relationships"][0]["relationship_id"] == "rel_001"
    assert payload["prior_fs_account_movement_coverage"][0]["account_name"] == "EVP Fund III"
    assert payload["relationships"][0]["accounts_involved"][0]["source"] == "prior_fs"
    compatibility_payload = json.loads((tmp_path / "accounting_event_register.json").read_text())
    assert compatibility_payload["register_artifact_type"] == "relationship_reasoning_register"


def test_codex_relationship_prompt_requires_digital_accountant_reasoning() -> None:
    from accountant_copilot import cli

    payload = json.loads(cli._codex_source_match_prompt({"entity_name": "Match Trust", "documents": []}, None))
    prompt_text = json.dumps(payload).lower()

    assert payload["required_output_schema"]["relationship_reasoning_contract_version"] == cli.SOURCE_MATCHING_CONTRACT_VERSION
    assert payload["required_output_schema"]["artifact_type"] == "relationship_reasoning_register"
    assert "digital junior accountant" in prompt_text
    assert "prior-fs anchored pass" in prompt_text
    assert "movement-by-row pass" in prompt_text
    assert "source-first" in prompt_text
    assert "bank-first" in prompt_text
    assert "step 4 owns" in prompt_text
    assert "do not output dr/cr" in prompt_text
    assert "do useful arithmetic" in prompt_text
    assert "do useful roll-forwards" in prompt_text
    assert "prior fs" in prompt_text
    assert "prior_fs_account_movement_coverage" in prompt_text
    assert "matrix_hints" in prompt_text


def test_tb_bridge_prompt_makes_movement_columns_step4_decision() -> None:
    from accountant_copilot.tb_bridge_workflow import build_tb_bridge_prompt

    source_index = {"entity_name": "Match Trust", "documents": []}
    relationship_register = _relationship_payload("relationship_reasoning_agent_v2")
    prior_coa = {
        "prior_fs_document_id": "raw_003",
        "prior_fs_display_name": "2024-06-30 - Match Trust - Financial Statements.pdf",
        "accounts": [
            {
                "account_id": "acct_evp",
                "name": "EVP Fund III",
                "type": "asset",
                "presentation_group": "Investments",
                "opening_balance": "92500.00",
            }
        ],
    }

    prompt = json.loads(build_tb_bridge_prompt(relationship_register, source_index, prior_coa))
    prompt_text = json.dumps(prompt).lower()

    assert "movement column design as a step 4 accounting decision" in prompt_text
    assert "step 3 matrix_hints are suggestions only" in prompt_text
    assert "prior_fs_account_movement_coverage" in prompt_text
    assert "choose final column names" in prompt_text
    assert "do not use a fixed global column list" in prompt_text
    assert "derive column names from this client's prior fs rows" in prompt_text
    assert "bank/provider names found in evidence" in prompt_text
    assert "movement_note_explanation_patterns" in prompt["context"]
    assert "gross bank transfer pool" in prompt_text
    assert "chosen draft treatment and the plausible alternative" in prompt_text
    assert "why this number" in prompt_text
    assert "do not hardcode example client names or amounts" in prompt_text
    assert "existing prior-fs loan/upe/related-party row" in prompt_text
    assert "movement notes should be account-led" in prompt_text
    assert "standard_movement_role_library" in prompt_text
    assert "cash_account_movement" in prompt_text
    assert "asset_disposal_gain_loss" in prompt_text
    assert "extension_role" in prompt_text
    assert "new_role_proposal" in prompt_text
    assert "the role is the accounting meaning" in prompt_text
    assert "tb-bridge-preparation" in prompt_text
    assert "client files are evidence" in prompt_text
    assert "not tax schedule logic" in prompt_text
    assert "accounting-pdf-knowledge-retrieval" in prompt_text
    assert "pdf-topic-map.json" in prompt_text
    assert "retrieve_pdf_topic.py" in prompt_text
    assert "inventory_cogs_working_capital" in prompt_text
    assert "inventory" in prompt_text


def test_accounting_knowhow_skills_load_into_relationship_prompt() -> None:
    from accountant_copilot.tb_bridge_workflow import build_relationship_reasoning_prompt, load_accounting_pdf_topic_map_for_prompt, load_accounting_skill_for_prompt

    source_index = {
        "documents": [
            {
                "document_id": "raw_001",
                "display_name": "2024-06-30 - Prior Year Financial Statements.pdf",
                "document_type": "prior_year_financial_statements",
                "file_path": "inputs/fy24.pdf",
            }
        ]
    }

    skill = load_accounting_skill_for_prompt("accounting-relationship-reasoning")
    assert skill["status"] == "loaded"
    assert "relationship register" in skill["body"].lower()
    topic_map = load_accounting_pdf_topic_map_for_prompt()
    assert topic_map["status"] == "loaded"
    topic_ids = {topic["topic_id"] for topic in topic_map["topics"]}
    assert "foreign_currency_fx" in topic_ids
    assert "inventory_cogs_working_capital" in topic_ids

    prompt_text = build_relationship_reasoning_prompt(source_index, None).lower()
    assert "accounting-relationship-reasoning" in prompt_text
    assert "accounting-pdf-knowledge-retrieval" in prompt_text
    assert "pdf-topic-map.json" in prompt_text
    assert "retrieve_pdf_topic.py" in prompt_text
    assert "start from the prior-year financial statements" in prompt_text
    assert "do not output dr/cr" in prompt_text
    assert "foreign_currency_fx" in prompt_text
    assert "revenue_receivables_cutoff" in prompt_text


def test_relationship_validator_rejects_skill_as_evidence() -> None:
    from accountant_copilot.tb_bridge_workflow import RELATIONSHIP_REASONING_CONTRACT_VERSION, build_relationship_reasoning_prompt, validate_relationship_register

    source_index = {"documents": [{"document_id": "raw_001", "display_name": "Client bank.pdf"}]}
    payload = {
        "artifact_type": "relationship_reasoning_register",
        "relationship_reasoning_contract_version": RELATIONSHIP_REASONING_CONTRACT_VERSION,
        "status": "ready",
        "agent": "codex_cli",
        "relationships": [
            {
                "relationship_id": "rel_001",
                "relationship_type": "bank_only_classification",
                "status": "ready_for_bridge",
                "confidence": "high",
                "story": "The accounting skill supports posting this as income.",
                "document_refs": ["raw_001"],
                "evidence_nodes": [
                    {"node_id": "ev_001", "node_type": "source_document", "document_refs": ["raw_001"], "description": "Client document"}
                ],
            }
        ],
        "prior_fs_account_movement_coverage": [],
    }

    findings = validate_relationship_register(payload, source_index)
    assert any(item["category"] == "non_client_evidence_reference" and item["severity"] == "high" for item in findings)
    assert any(item.get("redo_required") is True for item in findings)

    retry_prompt = json.loads(build_relationship_reasoning_prompt(source_index, None, recovery_attempt=1, validation_findings=findings))
    assert retry_prompt["recovery_context"]["source_of_truth_redo_required"] is True
    assert "redo the output" in retry_prompt["recovery_context"]["instruction"].lower()


def test_tb_bridge_validator_rejects_knowhow_as_evidence() -> None:
    from accountant_copilot.tb_bridge_workflow import TB_BRIDGE_CONTRACT_VERSION, build_tb_bridge_prompt, validate_tb_bridge_workpaper

    payload = {
        "artifact_type": "tb_bridge_workpaper",
        "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
        "status": "ready",
        "agent": "codex_cli",
        "accounts": [
            {"account_name": "Cash at Bank", "account_type": "asset", "statement_section": "Balance sheet", "statement_group": "Cash", "opening_balance": "0.00"},
            {"account_name": "Income", "account_type": "income", "statement_section": "Profit and loss", "statement_group": "Income", "opening_balance": "0.00"},
        ],
        "movement_columns": [
            {
                "column_key": "bank",
                "label": "Bank",
                "movement_role": {"role_type": "cash_account_movement"},
                "support_type": "direct_evidence",
                "note_id": "N001",
                "description": "Bank movement.",
            }
        ],
        "matrix_rows": [
            {
                "row_id": "row_001",
                "account_name": "Cash at Bank",
                "account_type": "asset",
                "statement_section": "Balance sheet",
                "statement_group": "Cash",
                "opening_balance": "0.00",
                "movements": [{"column_key": "bank", "amount": "100.00", "support_type": "direct_evidence", "relationship_id": "rel_001", "note_id": "N001"}],
                "closing_balance": "100.00",
                "row_status": "ready",
            },
            {
                "row_id": "row_002",
                "account_name": "Income",
                "account_type": "income",
                "statement_section": "Profit and loss",
                "statement_group": "Income",
                "opening_balance": "0.00",
                "movements": [{"column_key": "bank", "amount": "-100.00", "support_type": "direct_evidence", "relationship_id": "rel_001", "note_id": "N001"}],
                "closing_balance": "-100.00",
                "row_status": "ready",
            },
        ],
        "movement_notes": [
            {
                "note_id": "N001",
                "account_name": "Income",
                "status": "ready",
                "evidence_summary": "Supported by knowhow/skills/tb-bridge-preparation/SKILL.md.",
                "relationship_ids": ["rel_001"],
            }
        ],
        "relationship_coverage": [{"relationship_id": "rel_001", "matrix_status": "included"}],
    }
    relationship_register = {"relationships": [{"relationship_id": "rel_001"}]}
    prior_coa = {"accounts": [{"name": "Cash at Bank"}]}

    findings = validate_tb_bridge_workpaper(payload, relationship_register, prior_coa)
    assert any(item["category"] == "non_client_evidence_reference" and item["severity"] == "high" for item in findings)
    assert any(item.get("redo_required") is True for item in findings)

    retry_prompt = json.loads(build_tb_bridge_prompt(relationship_register, {"documents": []}, prior_coa, recovery_attempt=1, validation_findings=findings))
    assert retry_prompt["recovery_context"]["source_of_truth_redo_required"] is True
    assert "redo the output" in retry_prompt["recovery_context"]["instruction"].lower()


def test_turing_review_validator_rejects_training_material_as_evidence() -> None:
    from accountant_copilot import cli

    payload = {
        "artifact_type": "turing_senior_accountant_review",
        "review_contract_version": "turing_senior_review_v1",
        "status": "ready",
        "reviewer": "turing",
        "entity_name": "Client",
        "control_checks": [],
        "sampled_items": [
            {
                "sample_id": "S001",
                "reason_selected": "judgement item",
                "workpaper_item": "Income",
                "amounts_checked": ["100.00"],
                "source_documents_checked": [{"document_id": "fin121_csg.pdf", "display_name": "Candidate Study Guide", "file_path": "knowhow/fin121_csg.pdf"}],
                "original_evidence_observation": "Candidate Study Guide supports the accounting treatment.",
                "conclusion": "pass",
                "recommended_follow_up": "",
            }
        ],
        "findings": [],
        "correction_briefs": [],
        "summary": {"control_checks": 0, "sampled_items": 1, "findings": 0, "correction_briefs": 0, "accountant_message": "Ready."},
    }

    findings = cli._validate_turing_review(payload)
    assert any(item["category"] == "non_client_evidence_reference" and item["severity"] == "high" for item in findings)
    assert any(item.get("redo_required") is True for item in findings)


def test_turing_review_validator_allows_negated_training_material_control() -> None:
    from accountant_copilot import cli

    payload = {
        "artifact_type": "turing_senior_accountant_review",
        "review_contract_version": "turing_senior_review_v1",
        "status": "ready",
        "reviewer": "turing",
        "entity_name": "Client",
        "control_checks": [
            {
                "check_id": "C001",
                "check_name": "Client evidence only",
                "status": "pass",
                "summary": "No skill, knowhow, or training material is cited as client evidence.",
            }
        ],
        "sampled_items": [],
        "findings": [],
        "correction_briefs": [],
        "summary": {"control_checks": 1, "sampled_items": 0, "findings": 0, "correction_briefs": 0, "accountant_message": "Ready."},
    }

    findings = cli._validate_turing_review(payload)

    assert not any(item["category"] == "non_client_evidence_reference" for item in findings)


def test_tb_bridge_validator_warns_for_untyped_event_flavour_column() -> None:
    from accountant_copilot import cli
    from accountant_copilot.tb_bridge_workflow import TB_BRIDGE_CONTRACT_VERSION

    payload = {
        "artifact_type": "tb_bridge_workpaper",
        "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
        "status": "ready",
        "agent": "codex_cli",
        "accounts": [
            {"account_name": "ANZ - Capital Notes 9", "account_type": "asset", "statement_group": "Investments", "opening_balance": "540000.00"},
            {"account_name": "Cash at Bank WBC8243", "account_type": "asset", "statement_group": "Cash and Cash Equivalents", "opening_balance": "0.00"},
            {"account_name": "Gain on sale of non-current assets", "account_type": "income", "statement_group": "Income", "opening_balance": "0.00"},
        ],
        "movement_columns": [
            {
                "column_key": "anz_sale",
                "label": "ANZ sale",
                "column_type": "sale_gain_loss",
                "support_type": "direct_evidence",
                "note_id": "N001",
                "description": "Legacy event-style sale column.",
            }
        ],
        "matrix_rows": [
            {"row_id": "row_001", "account_name": "ANZ - Capital Notes 9", "account_type": "asset", "statement_group": "Investments", "opening_balance": "540000.00", "movements": [{"column_key": "anz_sale", "amount": "-540000.00", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": "N001"}], "closing_balance": "0.00", "row_status": "ready", "note_ids": ["N001"]},
            {"row_id": "row_002", "account_name": "Cash at Bank WBC8243", "account_type": "asset", "statement_group": "Cash and Cash Equivalents", "opening_balance": "0.00", "movements": [{"column_key": "anz_sale", "amount": "560053.96", "support_type": "direct_evidence", "relationship_id": "rel_001", "note_id": "N001"}], "closing_balance": "560053.96", "row_status": "ready", "note_ids": ["N001"]},
            {"row_id": "row_003", "account_name": "Gain on sale of non-current assets", "account_type": "income", "statement_group": "Income", "opening_balance": "0.00", "movements": [{"column_key": "anz_sale", "amount": "-20053.96", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": "N001"}], "closing_balance": "-20053.96", "row_status": "ready", "note_ids": ["N001"]},
        ],
        "movement_notes": [{"note_id": "N001", "status": "ready", "relationship_ids": ["rel_001"]}],
        "relationship_coverage": [{"relationship_id": "rel_001", "matrix_status": "included"}],
        "workpaper_notes": [],
    }
    relationship_register = {"relationships": [{"relationship_id": "rel_001"}]}
    prior_coa = {"accounts": [{"name": "ANZ - Capital Notes 9", "type": "asset"}]}

    findings = cli._validate_coa_mapping_workpaper(payload, relationship_register, prior_coa)
    categories = {finding["category"] for finding in findings}

    assert "movement_role_inferred" in categories
    assert "movement_column_label_needs_accountant_style" in categories
    assert not any(finding["severity"] == "high" for finding in findings)


def test_tb_bridge_validator_rejects_gain_loss_row_inside_bank_column() -> None:
    from accountant_copilot import cli
    from accountant_copilot.tb_bridge_workflow import TB_BRIDGE_CONTRACT_VERSION

    payload = {
        "artifact_type": "tb_bridge_workpaper",
        "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
        "status": "ready",
        "agent": "codex_cli",
        "accounts": [
            {"account_name": "Cash at Bank", "account_type": "asset", "statement_group": "Cash and Cash Equivalents", "opening_balance": "0.00"},
            {"account_name": "Investment A", "account_type": "asset", "statement_group": "Investments", "opening_balance": "100.00"},
            {"account_name": "Capital Gain/(Loss) on Sale of Non-Current Assets", "account_type": "income", "statement_group": "Income", "opening_balance": "0.00"},
        ],
        "movement_columns": [
            {
                "column_key": "westpac",
                "label": "Westpac",
                "movement_role": {"role_type": "cash_account_movement"},
                "support_type": "direct_evidence",
                "note_id": "N001",
                "description": "Bank movement.",
            }
        ],
        "matrix_rows": [
            {"row_id": "row_001", "account_name": "Cash at Bank", "account_type": "asset", "statement_group": "Cash and Cash Equivalents", "opening_balance": "0.00", "movements": [{"column_key": "westpac", "amount": "110.00", "support_type": "direct_evidence", "relationship_id": "rel_001", "note_id": "N001"}], "closing_balance": "110.00", "row_status": "ready", "note_ids": ["N001"]},
            {"row_id": "row_002", "account_name": "Investment A", "account_type": "asset", "statement_group": "Investments", "opening_balance": "100.00", "movements": [{"column_key": "westpac", "amount": "-100.00", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": "N001"}], "closing_balance": "0.00", "row_status": "ready", "note_ids": ["N001"]},
            {"row_id": "row_003", "account_name": "Capital Gain/(Loss) on Sale of Non-Current Assets", "account_type": "income", "statement_group": "Income", "opening_balance": "0.00", "movements": [{"column_key": "westpac", "amount": "-10.00", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": "N001"}], "closing_balance": "-10.00", "row_status": "ready", "note_ids": ["N001"]},
        ],
        "movement_notes": [{"note_id": "N001", "account_name": "Capital Gain/(Loss) on Sale of Non-Current Assets", "status": "ready", "relationship_ids": ["rel_001"]}],
        "relationship_coverage": [{"relationship_id": "rel_001", "matrix_status": "included"}],
    }

    findings = cli._validate_coa_mapping_workpaper(
        payload,
        {"relationships": [{"relationship_id": "rel_001"}]},
        {"accounts": [{"name": "Cash at Bank"}, {"name": "Investment A"}, {"name": "Capital Gain/(Loss) on Sale of Non-Current Assets"}]},
    )

    assert any(finding["category"] == "gain_loss_needs_separate_movement_column" and finding["severity"] == "high" for finding in findings)


def test_tb_bridge_validator_rejects_netted_receivable_roll_forward() -> None:
    from accountant_copilot import cli
    from accountant_copilot.tb_bridge_workflow import TB_BRIDGE_CONTRACT_VERSION

    payload = {
        "artifact_type": "tb_bridge_workpaper",
        "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
        "status": "ready",
        "agent": "codex_cli",
        "accounts": [
            {"account_name": "Cash at Bank", "account_type": "asset", "statement_group": "Cash and Cash Equivalents", "opening_balance": "0.00"},
            {"account_name": "Sundry Debtors - Fund A", "account_type": "asset", "statement_group": "Receivables / Sundry Debtors", "opening_balance": "40.00"},
            {"account_name": "Distributions Received", "account_type": "income", "statement_group": "Income", "opening_balance": "0.00"},
        ],
        "movement_columns": [
            {
                "column_key": "cba",
                "label": "CBA",
                "movement_role": {"role_type": "cash_account_movement"},
                "support_type": "direct_evidence",
                "note_id": "N001",
                "description": "Bank movement.",
            }
        ],
        "matrix_rows": [
            {"row_id": "row_001", "account_name": "Cash at Bank", "account_type": "asset", "statement_group": "Cash and Cash Equivalents", "opening_balance": "0.00", "movements": [{"column_key": "cba", "amount": "50.00", "support_type": "direct_evidence", "relationship_id": "rel_001", "note_id": "N001"}], "closing_balance": "50.00", "row_status": "ready", "note_ids": ["N001"]},
            {"row_id": "row_002", "account_name": "Sundry Debtors - Fund A", "account_type": "asset", "statement_group": "Receivables / Sundry Debtors", "opening_balance": "40.00", "movements": [{"column_key": "cba", "amount": "-20.00", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": "N001"}], "closing_balance": "20.00", "row_status": "ready", "note_ids": ["N001"]},
            {"row_id": "row_003", "account_name": "Distributions Received", "account_type": "income", "statement_group": "Income", "opening_balance": "0.00", "movements": [{"column_key": "cba", "amount": "-30.00", "support_type": "direct_evidence", "relationship_id": "rel_001", "note_id": "N001"}], "closing_balance": "-30.00", "row_status": "ready", "note_ids": ["N001"]},
        ],
        "movement_notes": [{"note_id": "N001", "account_name": "Sundry Debtors - Fund A", "status": "ready", "relationship_ids": ["rel_001"]}],
        "relationship_coverage": [{"relationship_id": "rel_001", "matrix_status": "included"}],
    }

    findings = cli._validate_coa_mapping_workpaper(
        payload,
        {
            "relationships": [
                {
                    "relationship_id": "rel_001",
                    "accounting_story": "Opening receivable plus current source distribution entitlement less bank receipts leaves a residual receivable.",
                }
            ]
        },
        {"accounts": [{"name": "Cash at Bank"}, {"name": "Sundry Debtors - Fund A"}, {"name": "Distributions Received"}]},
    )

    assert any(finding["category"] == "receivable_roll_forward_needs_source_column" and finding["severity"] == "high" for finding in findings)


def test_codex_relationship_reasoning_does_not_fallback_when_codex_unusable(tmp_path: Path):
    source_index_path = tmp_path / "source_document_index.json"
    _write_source_index(source_index_path)
    output = tmp_path / "source_fact_matches.md"

    result = run_cli(
        "match-source-facts",
        "--accounting-facts",
        str(source_index_path),
        "--codex-command",
        "__missing_codex_source_match_command__",
        "--codex-max-attempts",
        "1",
        "--output",
        str(output),
    )

    assert result.returncode == 1
    payload = json.loads((tmp_path / "relationship_reasoning_register.json").read_text())
    assert payload["status"] == "codex_failed"
    assert payload["relationships"][0]["relationship_id"] == "rel_codex_unavailable"
    assert "No deterministic fallback" in payload["investigation_log"][0]


def test_codex_relationship_reasoning_validates_document_refs(tmp_path: Path):
    from accountant_copilot import cli

    source_index_path = tmp_path / "source_document_index.json"
    _write_source_index(source_index_path)
    output = tmp_path / "source_fact_matches.md"
    fake_codex_payload = _relationship_payload(cli.SOURCE_MATCHING_CONTRACT_VERSION)
    fake_codex_payload["relationships"][0]["document_refs"] = ["raw_missing"]

    result = run_cli(
        "match-source-facts",
        "--accounting-facts",
        str(source_index_path),
        "--codex-max-attempts",
        "1",
        "--output",
        str(output),
        env={"ACCOUNTANT_COPILOT_FAKE_CODEX_SOURCE_MATCH_JSON": json.dumps(fake_codex_payload)},
    )

    assert result.returncode == 1
    payload = json.loads((tmp_path / "relationship_reasoning_register.json").read_text())
    assert payload["status"] == "codex_failed"
    assert {finding["category"] for finding in payload["validation_findings"]} == {"unknown_document_refs"}


def test_codex_relationship_reasoning_retries_with_validation_feedback(tmp_path: Path, monkeypatch):
    from accountant_copilot import cli

    source_index_path = tmp_path / "source_document_index.json"
    _write_source_index(source_index_path)
    output = tmp_path / "source_fact_matches.md"
    calls = []

    def fake_codex(source_index, coverage_payload, command, timeout, *, recovery_attempt=0, previous_error=None, validation_findings=None, previous_payload=None):
        calls.append(
            {
                "recovery_attempt": recovery_attempt,
                "validation_findings": validation_findings or [],
                "previous_payload": previous_payload,
            }
        )
        payload = _relationship_payload(cli.SOURCE_MATCHING_CONTRACT_VERSION)
        if len(calls) == 1:
            payload["relationships"][0]["document_refs"] = ["raw_missing"]
        return payload, None

    monkeypatch.setattr(cli, "_codex_investigate_source_matches", fake_codex)
    result = cli._match_source_facts_from_accounting_command(
        argparse.Namespace(
            accounting_facts=str(source_index_path),
            source_coverage=None,
            codex_command="codex exec",
            codex_timeout=1,
            codex_max_attempts=2,
            output=str(output),
        )
    )

    assert result == 0
    assert len(calls) == 2
    assert calls[1]["recovery_attempt"] == 1
    assert calls[1]["previous_payload"] is not None
    assert {finding["category"] for finding in calls[1]["validation_findings"]} == {"unknown_document_refs"}
    payload = json.loads((tmp_path / "relationship_reasoning_register.json").read_text())
    assert payload["summary"]["relationships"] == 1
    assert payload["validation_findings"] == []


def test_codex_tb_bridge_workpaper_builds_matrix_from_relationships(tmp_path: Path, monkeypatch):
    from accountant_copilot import cli

    relationship_path, source_index_path, prior_coa_path = _write_step4_inputs(tmp_path)
    output_dir = tmp_path / "step4"

    def fake_codex(relationship_register, source_index, prior_coa, command, timeout, *, recovery_attempt=0, previous_error=None, validation_findings=None, previous_payload=None, candidate_output_path=None):
        assert recovery_attempt == 0
        assert relationship_register["relationships"][0]["relationship_id"] == "rel_001"
        assert source_index["documents"][0]["document_id"] == "raw_001"
        assert prior_coa["accounts"][0]["name"] == "ANZ - Capital Notes 9"
        return {
            "artifact_type": "tb_bridge_workpaper",
            "tb_bridge_contract_version": cli.COA_MAPPING_CONTRACT_VERSION,
            "status": "ready",
            "agent": "codex_cli",
            "accounts": [
                {
                    "account_name": "ANZ - Capital Notes 9",
                    "account_type": "asset",
                    "statement_group": "Investments",
                    "opening_balance": "540000.00",
                    "opening_source": "prior_fs",
                    "reason": "Prior FS opening investment.",
                },
                {
                    "account_name": "Cash at Bank WBC8243",
                    "account_type": "asset",
                    "statement_group": "Cash and Cash Equivalents",
                    "opening_balance": "0.00",
                    "opening_source": "codex_new",
                    "reason": "Cash row needed for balanced sale column.",
                },
                {
                    "account_name": "Gain on sale of non-current assets",
                    "account_type": "income",
                    "statement_group": "Income",
                    "opening_balance": "0.00",
                    "opening_source": "codex_new",
                    "reason": "Gain row needed for balanced sale column.",
                },
            ],
            "movement_columns": [
                {
                    "column_key": "anz_sale",
                    "label": "Gain on sale of non-current assets",
                    "column_type": "asset_disposal_gain_loss",
                    "movement_role": {
                        "role_type": "asset_disposal_gain_loss",
                        "standard_role_name": "Asset disposal gain/loss",
                        "accounting_purpose": "Book profit or loss on sale/redemption of investments or non-current assets.",
                        "label_basis": "book_adjustment",
                        "source_or_counterparty": "ANZ Capital Notes 9",
                        "cash_account": "Westpac",
                        "new_role_proposal": {},
                    },
                    "support_type": "direct_evidence",
                    "note_id": "N001",
                    "description": "Balanced ANZ sale movement.",
                },
            ],
            "matrix_rows": [
                {
                    "row_id": "row_001",
                    "account_name": "ANZ - Capital Notes 9",
                    "account_type": "asset",
                    "statement_group": "Investments",
                    "opening_balance": "540000.00",
                    "movements": [
                        {"column_key": "anz_sale", "amount": "-540000.00", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": "N001", "explanation": "clear opening investment"},
                    ],
                    "closing_balance": "0.00",
                    "difference": "0.00",
                    "row_status": "ready",
                    "note_ids": ["N001"],
                    "notes": "See N001.",
                },
                {
                    "row_id": "row_002",
                    "account_name": "Cash at Bank WBC8243",
                    "account_type": "asset",
                    "statement_group": "Cash and Cash Equivalents",
                    "opening_balance": "0.00",
                    "movements": [
                        {"column_key": "anz_sale", "amount": "560053.96", "support_type": "direct_evidence", "relationship_id": "rel_001", "note_id": "N001", "explanation": "net sale proceeds"},
                    ],
                    "closing_balance": "560053.96",
                    "difference": "0.00",
                    "row_status": "ready",
                    "note_ids": ["N001"],
                    "notes": "See N001.",
                },
                {
                    "row_id": "row_003",
                    "account_name": "Gain on sale of non-current assets",
                    "account_type": "income",
                    "statement_group": "Income",
                    "opening_balance": "0.00",
                    "movements": [
                        {"column_key": "anz_sale", "amount": "-20053.96", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": "N001", "explanation": "net gain"},
                    ],
                    "closing_balance": "-20053.96",
                    "difference": "0.00",
                    "row_status": "ready",
                    "note_ids": ["N001"],
                    "notes": "See N001.",
                }
            ],
            "movement_notes": [
                {
                    "note_id": "N001",
                    "status": "ready",
                    "tb_column": "ANZ sale",
                    "main_amount": "560053.96",
                    "other_amounts": "540000.00; 20053.96",
                    "explanation": "Westpac received sale proceeds; the column clears the ANZ investment and records the gain.",
                    "calculation": "560053.96 - 540000.00 = 20053.96",
                    "evidence_summary": "Source and bank agree on sale proceeds.",
                    "relationship_ids": ["rel_001"],
                }
            ],
            "relationship_coverage": [{"relationship_id": "rel_001", "matrix_status": "included", "notes": "Included."}],
            "summary": {"accounts": 3, "movement_columns": 1, "matrix_rows": 3, "movement_notes": 1, "ready_rows": 3, "needs_attention_rows": 0},
            "workpaper_notes": ["Bridge matrix, not final posting."],
        }, None

    monkeypatch.setattr(cli, "_codex_map_coa_from_events", fake_codex)
    result = cli._build_coa_mapping_workpaper_command(
        argparse.Namespace(
            artifact_dir=str(tmp_path),
            output_dir=str(output_dir),
            event_register=str(relationship_path),
            source_index=str(source_index_path),
            prior_coa=str(prior_coa_path),
            codex_command="codex exec",
            codex_timeout=1,
            codex_max_attempts=1,
            skip_xlsx=True,
        )
    )

    assert result == 0
    payload = json.loads((output_dir / "tb_bridge_workpaper.json").read_text())
    assert payload["artifact_type"] == "tb_bridge_workpaper"
    assert payload["agent"] == "codex_cli"
    assert payload["summary"]["matrix_rows"] == 3
    row = next(row for row in payload["matrix_rows"] if row["account_name"] == "ANZ - Capital Notes 9")
    assert row["opening_balance"] == "540000.00"
    assert row["movements"][0]["amount"] == "-540000.00"
    assert row["closing_balance"] == "0.00"
    note = next(note for note in payload["movement_notes"] if note["account_name"] == "ANZ - Capital Notes 9")
    assert note["note_id"].startswith("R")
    assert note["source_note_ids"] == ["N001"]


def test_codex_tb_bridge_workpaper_accepts_json_sidecar_when_stdout_is_summary(tmp_path: Path, monkeypatch):
    from accountant_copilot import cli

    relationship_path, source_index_path, prior_coa_path = _write_step4_inputs(tmp_path)
    output_dir = tmp_path / "step4"

    sidecar_payload = {
        "artifact_type": "tb_bridge_workpaper",
        "tb_bridge_contract_version": cli.COA_MAPPING_CONTRACT_VERSION,
        "status": "ready",
        "agent": "codex_cli",
        "accounts": [
            {"account_name": "ANZ - Capital Notes 9", "account_type": "asset", "statement_group": "Investments", "opening_balance": "540000.00", "opening_source": "prior_fs"}
        ],
            "movement_columns": [
            {
                "column_key": "opening_review",
                "label": "Opening review",
                "column_type": "other",
                "movement_role": {
                    "role_type": "extension_role",
                    "standard_role_name": "New proposed role",
                    "accounting_purpose": "Minimal test-only review column.",
                    "label_basis": "new_role",
                    "source_or_counterparty": "",
                    "cash_account": "",
                    "new_role_proposal": {
                        "suggested_role_name": "Opening review",
                        "why_existing_roles_do_not_fit": "This test fixture has no FY movement and only checks sidecar loading.",
                        "affected_accounts": ["ANZ - Capital Notes 9"],
                        "suggested_reuse_rule": "Do not promote; test fixture only.",
                    },
                },
                "support_type": "evidence_derived",
                "note_id": "N001",
                "description": "No FY25 movement in this minimal test.",
            }
        ],
        "matrix_rows": [
            {
                "row_id": "row_001",
                "account_name": "ANZ - Capital Notes 9",
                "account_type": "asset",
                "statement_group": "Investments",
                "opening_balance": "540000.00",
                "movements": [{"column_key": "opening_review", "amount": "0.00", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": "N001", "explanation": "Covered in Step 3."}],
                "closing_balance": "540000.00",
                "difference": "0.00",
                "row_status": "ready",
                "note_ids": ["N001"],
                "notes": "See N001.",
            }
        ],
        "movement_notes": [{"note_id": "N001", "status": "ready", "tb_column": "Opening review", "main_amount": "0.00", "explanation": "Relationship covered.", "relationship_ids": ["rel_001"]}],
        "relationship_coverage": [{"relationship_id": "rel_001", "matrix_status": "included", "notes": "Included."}],
        "summary": {"accounts": 1, "movement_columns": 1, "matrix_rows": 1, "movement_notes": 1},
        "workpaper_notes": [],
    }

    def fake_run(*args, **kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / cli.TB_BRIDGE_JSON).write_text(json.dumps(sidecar_payload))
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="Built the TB bridge workpaper JSON here:\n\n[tb_bridge_workpaper.json](...)", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    result = cli._build_coa_mapping_workpaper_command(
        argparse.Namespace(
            artifact_dir=str(tmp_path),
            output_dir=str(output_dir),
            event_register=str(relationship_path),
            source_index=str(source_index_path),
            prior_coa=str(prior_coa_path),
            codex_command="codex exec",
            codex_timeout=1,
            codex_max_attempts=1,
            skip_xlsx=True,
        )
    )

    assert result == 0
    payload = json.loads((output_dir / "tb_bridge_workpaper.json").read_text())
    assert payload["status"] == "ready"
    assert payload["summary"]["accounts"] == 1
    assert payload["codex_attempt_history"][0]["status"] == "success"


def test_codex_tb_bridge_workpaper_does_not_fallback_when_codex_unusable(tmp_path: Path):
    from accountant_copilot import cli

    relationship_path, source_index_path, prior_coa_path = _write_step4_inputs(tmp_path)
    output_dir = tmp_path / "step4"

    result = cli._build_coa_mapping_workpaper_command(
        argparse.Namespace(
            artifact_dir=str(tmp_path),
            output_dir=str(output_dir),
            event_register=str(relationship_path),
            source_index=str(source_index_path),
            prior_coa=str(prior_coa_path),
            codex_command="__missing_codex_coa_mapping_command__",
            codex_timeout=1,
            codex_max_attempts=1,
            skip_xlsx=True,
        )
    )

    assert result == 1
    payload = json.loads((output_dir / "tb_bridge_workpaper.json").read_text())
    assert payload["status"] == "codex_failed"
    assert payload["matrix_rows"] == []
    assert "No deterministic fallback" in payload["workpaper_notes"][0]


def _minimal_beneficiary_tb_payload(note: dict) -> dict:
    from accountant_copilot.tb_bridge_workflow import TB_BRIDGE_CONTRACT_VERSION

    return {
        "artifact_type": "tb_bridge_workpaper",
        "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
        "status": "ready",
        "agent": "codex_cli",
        "accounts": [
            {"account_name": "Unpaid Present Entitlement", "account_type": "liability", "statement_group": "Beneficiary Accounts", "opening_balance": "0.00"},
            {"account_name": "Profit Distribution - Beneficiary", "account_type": "equity", "statement_group": "Equity", "opening_balance": "0.00"},
        ],
        "movement_columns": [
            {
                "column_key": "beneficiary_distribution",
                "label": "Beneficiary distribution",
                "column_type": "beneficiary_distribution",
                "movement_role": {
                    "role_type": "owner_or_beneficiary_distribution",
                    "standard_role_name": "Owner or beneficiary distribution",
                    "accounting_purpose": "UPE, beneficiary distribution, drawings, dividends, or owner allocation.",
                    "label_basis": "owner_distribution",
                    "source_or_counterparty": "Beneficiary",
                    "cash_account": "",
                    "new_role_proposal": {},
                },
                "support_type": "evidence_derived",
                "note_id": note["note_id"],
                "description": "Book profit distributed to beneficiary.",
            }
        ],
        "matrix_rows": [
            {
                "row_id": "row_001",
                "account_name": "Unpaid Present Entitlement",
                "account_type": "liability",
                "statement_group": "Beneficiary Accounts",
                "opening_balance": "0.00",
                "movements": [{"column_key": "beneficiary_distribution", "amount": "100.00", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": note["note_id"], "explanation": "Beneficiary distribution"}],
                "closing_balance": "100.00",
                "row_status": "ready",
                "note_ids": [note["note_id"]],
            },
            {
                "row_id": "row_002",
                "account_name": "Profit Distribution - Beneficiary",
                "account_type": "equity",
                "statement_group": "Equity",
                "opening_balance": "0.00",
                "movements": [{"column_key": "beneficiary_distribution", "amount": "-100.00", "support_type": "evidence_derived", "relationship_id": "rel_001", "note_id": note["note_id"], "explanation": "Beneficiary distribution"}],
                "closing_balance": "-100.00",
                "row_status": "ready",
                "note_ids": [note["note_id"]],
            },
        ],
        "movement_notes": [note],
        "relationship_coverage": [{"relationship_id": "rel_001", "matrix_status": "included", "notes": "Included."}],
        "workpaper_notes": [],
    }


def test_tb_bridge_validator_allows_tax_only_exclusion_wording_in_beneficiary_note():
    from accountant_copilot import cli

    relationship_register = {
        "relationships": [{"relationship_id": "rel_001", "story": "100 percent of distributable income is allocated to the beneficiary."}]
    }
    prior_coa = {
        "accounts": [
            {"name": "Unpaid Present Entitlement", "type": "liability"},
            {"name": "Profit Distribution - Beneficiary", "type": "equity"},
        ]
    }
    payload = _minimal_beneficiary_tb_payload(
        {
            "note_id": "N001",
            "status": "ready",
            "tb_column": "Beneficiary distribution",
            "main_amount": "100.00",
            "other_amounts": "Franking credits and TFN withholding are noted only and not posted.",
            "explanation": "Beneficiary distribution is based on book profit only; franking, withholding, ESVCLP offsets and tax gross-ups are excluded/not posted.",
            "calculation": "Book profit only = 100.00.",
            "evidence_summary": "Tax-only components are not posted to the TB bridge.",
            "relationship_ids": ["rel_001"],
        }
    )

    findings = cli._validate_coa_mapping_workpaper(payload, relationship_register, prior_coa)
    assert {finding["category"] for finding in findings} == set()


def test_tb_bridge_validator_points_to_beneficiary_tax_only_inclusion():
    from accountant_copilot import cli

    relationship_register = {
        "relationships": [{"relationship_id": "rel_001", "story": "100 percent of distributable income is allocated to the beneficiary."}]
    }
    prior_coa = {
        "accounts": [
            {"name": "Unpaid Present Entitlement", "type": "liability"},
            {"name": "Profit Distribution - Beneficiary", "type": "equity"},
        ]
    }
    payload = _minimal_beneficiary_tb_payload(
        {
            "note_id": "N001",
            "status": "ready",
            "tb_column": "Beneficiary distribution",
            "main_amount": "100.00",
            "other_amounts": "100.00",
            "explanation": "Beneficiary distribution includes franking credits and TFN withholding.",
            "calculation": "Book profit plus franking credits less TFN withholding.",
            "evidence_summary": "Tax statement components included.",
            "relationship_ids": ["rel_001"],
        }
    )

    findings = cli._validate_coa_mapping_workpaper(payload, relationship_register, prior_coa)
    tax_findings = [finding for finding in findings if finding["category"] == "beneficiary_distribution_includes_tax_only_components"]
    assert len(tax_findings) == 1
    assert tax_findings[0]["offending_note_id"] == "N001"
    assert tax_findings[0]["offending_field"] in {"calculation", "explanation", "evidence_summary"}


def test_prepare_workpaper_orchestrates_folder_to_workbook(tmp_path: Path, monkeypatch):
    from accountant_copilot import cli

    client_folder = tmp_path / "client_docs"
    client_folder.mkdir()
    (client_folder / "source.txt").write_text("source document")
    artifact_dir = tmp_path / "artifacts"
    output_dir = tmp_path / "workpaper"
    calls: list[str] = []

    def fake_process_documents(args):
        calls.append("process")
        artifact = Path(args.artifact_dir)
        artifact.mkdir(parents=True, exist_ok=True)
        source_index = {
            "entity_name": "Uploaded documents",
            "documents": [
                {
                    "document_id": "raw_001",
                    "display_name": "Source.txt",
                    "document_type": "source_document",
                    "file_path": str(client_folder / "source.txt"),
                }
            ],
        }
        (artifact / "source_document_index.json").write_text(json.dumps(source_index))
        (artifact / "accounting_facts_by_document.json").write_text(json.dumps(source_index))
        (artifact / "source_coverage_continuity.json").write_text(json.dumps({"entity_name": "Uploaded documents"}))
        return 0

    def fake_match_source_facts(args):
        calls.append("match")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = _relationship_payload(cli.SOURCE_MATCHING_CONTRACT_VERSION)
        (output.parent / "accounting_event_register.json").write_text(json.dumps(payload))
        (output.parent / "accounting_event_register.md").write_text("event register")
        return 0

    def fake_build_workpaper(args):
        calls.append("workpaper")
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / cli.TB_BRIDGE_XLSX).write_text("xlsx placeholder")
        (out / cli.TB_BRIDGE_JSON).write_text(
            json.dumps(
                {
                    "summary": {"accounts": 1, "movement_columns": 1, "movement_notes": 1},
                    "validation_findings": [],
                }
            )
        )
        return 0

    def fake_review_workpaper(args):
        calls.append("review")
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "turing_senior_review.md").write_text("review")
        (out / "turing_senior_review.json").write_text(
            json.dumps(
                {
                    "artifact_type": "turing_senior_accountant_review",
                    "review_contract_version": "turing_senior_review_v1",
                    "status": "ready",
                    "reviewer": "turing",
                    "summary": {"sampled_items": 1, "findings": 0, "correction_briefs": 0},
                    "findings": [],
                    "sampled_items": [],
                    "correction_briefs": [],
                    "control_checks": [],
                }
            )
        )
        return 0

    monkeypatch.setattr(cli, "_process_documents_command", fake_process_documents)
    monkeypatch.setattr(cli, "_match_source_facts_command", fake_match_source_facts)
    monkeypatch.setattr(cli, "_build_coa_mapping_workpaper_command", fake_build_workpaper)
    monkeypatch.setattr(cli, "_review_workpaper_command", fake_review_workpaper)

    result = cli._prepare_workpaper_command(
        argparse.Namespace(
            client_folder=str(client_folder),
            artifact_dir=str(artifact_dir),
            output_dir=str(output_dir),
            entity_name="Match Trust",
            prior_fs_document_id=None,
            prior_fs_file=None,
            codex_command="codex exec",
            codex_timeout=1,
            codex_max_attempts=1,
            batch_size=5,
            force_reprocess=False,
            allow_cache=False,
            review_correction_rounds=2,
            skip_xlsx=False,
            skip_review=False,
            review_sample_size=8,
        )
    )

    assert result == 0
    assert calls == ["process", "match", "workpaper", "review"]
    assert (output_dir / cli.TB_BRIDGE_XLSX).exists()
    assert "Workbook checks" in (output_dir / "prepared_workpaper_summary.md").read_text()
    source_index = json.loads((artifact_dir / "source_document_index.json").read_text())
    assert source_index["entity_name"] == "Match Trust"


def test_prepare_workpaper_applies_turing_corrections_until_ready(tmp_path: Path, monkeypatch):
    from accountant_copilot import cli

    client_folder = tmp_path / "client_docs"
    client_folder.mkdir()
    (client_folder / "source.txt").write_text("source document")
    artifact_dir = tmp_path / "artifacts"
    output_dir = tmp_path / "workpaper"
    calls: list[str] = []
    review_calls = 0

    def fake_process_documents(args):
        calls.append("process")
        assert args.force_reprocess is True
        artifact = Path(args.artifact_dir)
        artifact.mkdir(parents=True, exist_ok=True)
        source_index = {
            "entity_name": "Uploaded documents",
            "documents": [{"document_id": "raw_001", "display_name": "Source.txt", "file_path": str(client_folder / "source.txt")}],
        }
        (artifact / "source_document_index.json").write_text(json.dumps(source_index))
        (artifact / "accounting_facts_by_document.json").write_text(json.dumps(source_index))
        return 0

    def fake_match_source_facts(args):
        calls.append("match")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = _relationship_payload(cli.SOURCE_MATCHING_CONTRACT_VERSION)
        (output.parent / "accounting_event_register.json").write_text(json.dumps(payload))
        return 0

    def fake_build_workpaper(args):
        calls.append("workpaper")
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / cli.TB_BRIDGE_XLSX).write_text("xlsx v1")
        (out / cli.TB_BRIDGE_JSON).write_text(json.dumps({"summary": {"accounts": 1, "movement_columns": 1, "movement_notes": 1}, "validation_findings": []}))
        return 0

    def fake_review_workpaper(args):
        nonlocal review_calls
        review_calls += 1
        calls.append("review")
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        if review_calls == 1:
            payload = {
                "artifact_type": "turing_senior_accountant_review",
                "review_contract_version": "turing_senior_review_v1",
                "status": "needs_corrections",
                "reviewer": "turing",
                "summary": {"sampled_items": 1, "findings": 1, "correction_briefs": 1},
                "findings": [{"finding_id": "F001", "severity": "medium", "category": "control_failure", "message": "Needs a rounding control."}],
                "sampled_items": [],
                "correction_briefs": [
                    {
                        "brief_id": "C001",
                        "issue": "Rounding control missing.",
                        "expected_treatment": "Add explicit rounding control.",
                        "files_or_amounts_to_recheck": ["0.10"],
                        "required_workbook_change": "Add rounding row.",
                        "validation_test": "Signed totals equal zero.",
                    }
                ],
                "control_checks": [],
            }
        else:
            payload = {
                "artifact_type": "turing_senior_accountant_review",
                "review_contract_version": "turing_senior_review_v1",
                "status": "ready",
                "reviewer": "turing",
                "summary": {"sampled_items": 1, "findings": 0, "correction_briefs": 0},
                "findings": [],
                "sampled_items": [],
                "correction_briefs": [],
                "control_checks": [],
            }
        (out / "turing_senior_review.md").write_text("review")
        (out / "turing_senior_review.json").write_text(json.dumps(payload))
        return 0

    def fake_apply_corrections(args):
        calls.append("correct")
        out = Path(args.output_dir)
        (out / cli.TB_BRIDGE_XLSX).write_text("xlsx corrected")
        (out / cli.TB_BRIDGE_JSON).write_text(json.dumps({"summary": {"accounts": 1, "movement_columns": 1, "movement_notes": 1}, "validation_findings": [], "corrected": True}))
        return 0

    monkeypatch.setattr(cli, "_process_documents_command", fake_process_documents)
    monkeypatch.setattr(cli, "_match_source_facts_command", fake_match_source_facts)
    monkeypatch.setattr(cli, "_build_coa_mapping_workpaper_command", fake_build_workpaper)
    monkeypatch.setattr(cli, "_review_workpaper_command", fake_review_workpaper)
    monkeypatch.setattr(cli, "_apply_turing_corrections_command", fake_apply_corrections)

    result = cli._prepare_workpaper_command(
        argparse.Namespace(
            client_folder=str(client_folder),
            artifact_dir=str(artifact_dir),
            output_dir=str(output_dir),
            entity_name=None,
            prior_fs_document_id=None,
            prior_fs_file=None,
            codex_command="codex exec",
            codex_timeout=1,
            codex_max_attempts=1,
            batch_size=5,
            force_reprocess=False,
            allow_cache=False,
            review_correction_rounds=2,
            skip_xlsx=False,
            skip_review=False,
            review_sample_size=8,
        )
    )

    assert result == 0
    assert calls == ["process", "match", "workpaper", "review", "correct", "review"]
    assert (output_dir / "turing_senior_review_round_1.json").exists()
    assert json.loads((output_dir / cli.TB_BRIDGE_JSON).read_text())["corrected"] is True


def test_prepare_workpaper_stops_after_bounded_turing_correction_rounds(tmp_path: Path, monkeypatch):
    from accountant_copilot import cli

    client_folder = tmp_path / "client_docs"
    client_folder.mkdir()
    artifact_dir = tmp_path / "artifacts"
    output_dir = tmp_path / "workpaper"
    calls: list[str] = []

    def fake_process_documents(args):
        calls.append("process")
        artifact = Path(args.artifact_dir)
        artifact.mkdir(parents=True, exist_ok=True)
        source_index = {"entity_name": "Uploaded documents", "documents": [{"document_id": "raw_001", "display_name": "Source.txt"}]}
        (artifact / "source_document_index.json").write_text(json.dumps(source_index))
        (artifact / "accounting_facts_by_document.json").write_text(json.dumps(source_index))
        return 0

    def fake_match_source_facts(args):
        calls.append("match")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        (output.parent / "accounting_event_register.json").write_text(json.dumps(_relationship_payload(cli.SOURCE_MATCHING_CONTRACT_VERSION)))
        return 0

    def fake_build_workpaper(args):
        calls.append("workpaper")
        out = Path(args.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / cli.TB_BRIDGE_XLSX).write_text("xlsx")
        (out / cli.TB_BRIDGE_JSON).write_text(json.dumps({"summary": {"accounts": 1, "movement_columns": 1, "movement_notes": 1}, "validation_findings": []}))
        return 0

    def fake_review_workpaper(args):
        calls.append("review")
        out = Path(args.output_dir)
        payload = {
            "artifact_type": "turing_senior_accountant_review",
            "review_contract_version": "turing_senior_review_v1",
            "status": "needs_corrections",
            "reviewer": "turing",
            "summary": {"sampled_items": 1, "findings": 1, "correction_briefs": 1},
            "findings": [{"finding_id": "F001", "severity": "medium", "category": "control_failure", "message": "Still wrong."}],
            "sampled_items": [],
            "correction_briefs": [{"brief_id": "C001", "issue": "Still wrong.", "expected_treatment": "Fix it.", "required_workbook_change": "Fix workbook.", "validation_test": "Ready review."}],
            "control_checks": [],
        }
        (out / "turing_senior_review.md").write_text("review")
        (out / "turing_senior_review.json").write_text(json.dumps(payload))
        return 0

    def fake_apply_corrections(args):
        calls.append("correct")
        return 0

    monkeypatch.setattr(cli, "_process_documents_command", fake_process_documents)
    monkeypatch.setattr(cli, "_match_source_facts_command", fake_match_source_facts)
    monkeypatch.setattr(cli, "_build_coa_mapping_workpaper_command", fake_build_workpaper)
    monkeypatch.setattr(cli, "_review_workpaper_command", fake_review_workpaper)
    monkeypatch.setattr(cli, "_apply_turing_corrections_command", fake_apply_corrections)

    result = cli._prepare_workpaper_command(
        argparse.Namespace(
            client_folder=str(client_folder),
            artifact_dir=str(artifact_dir),
            output_dir=str(output_dir),
            entity_name=None,
            prior_fs_document_id=None,
            prior_fs_file=None,
            codex_command="codex exec",
            codex_timeout=1,
            codex_max_attempts=1,
            batch_size=5,
            force_reprocess=True,
            allow_cache=False,
            review_correction_rounds=1,
            skip_xlsx=False,
            skip_review=False,
            review_sample_size=8,
        )
    )

    assert result == 0
    assert calls == ["process", "match", "workpaper", "review", "correct", "review"]
    assert (output_dir / cli.TB_BRIDGE_XLSX).exists()
    progress = json.loads((output_dir / "prepare_workpaper_progress.json").read_text())
    assert progress["status"] == "needs_attention"


def test_prepare_workpaper_restores_last_good_workbook_when_step3_fails(tmp_path: Path, monkeypatch):
    from accountant_copilot import cli

    client_folder = tmp_path / "client_docs"
    client_folder.mkdir()
    artifact_dir = tmp_path / "artifacts"
    output_dir = tmp_path / "workpaper"
    output_dir.mkdir()
    stale_workbook = output_dir / cli.TB_BRIDGE_XLSX
    stale_workbook.write_text("old workbook")
    (output_dir / cli.TB_BRIDGE_JSON).write_text(json.dumps({"status": "old"}))
    calls: list[str] = []

    def fake_process_documents(args):
        calls.append("process")
        artifact = Path(args.artifact_dir)
        artifact.mkdir(parents=True, exist_ok=True)
        source_index = {
            "entity_name": "Uploaded documents",
            "documents": [{"document_id": "raw_001", "display_name": "Source.pdf", "file_path": str(client_folder / "Source.pdf")}],
        }
        (artifact / "source_document_index.json").write_text(json.dumps(source_index))
        (artifact / "accounting_facts_by_document.json").write_text(json.dumps(source_index))
        return 0

    def fake_match_source_facts(args):
        calls.append("match")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {"artifact_type": "relationship_reasoning_register", "status": "codex_failed", "relationships": []}
        (output.parent / "accounting_event_register.json").write_text(json.dumps(payload))
        return 1

    def fail_if_build_workpaper(args):
        raise AssertionError("Step 4 should not run when Step 3 failed")

    monkeypatch.setattr(cli, "_process_documents_command", fake_process_documents)
    monkeypatch.setattr(cli, "_match_source_facts_command", fake_match_source_facts)
    monkeypatch.setattr(cli, "_build_coa_mapping_workpaper_command", fail_if_build_workpaper)

    result = cli._prepare_workpaper_command(
        argparse.Namespace(
            client_folder=str(client_folder),
            artifact_dir=str(artifact_dir),
            output_dir=str(output_dir),
            entity_name=None,
            prior_fs_document_id=None,
            prior_fs_file=None,
            codex_command="codex exec",
            codex_timeout=1,
            codex_max_attempts=1,
            batch_size=5,
            force_reprocess=False,
            allow_cache=False,
            review_correction_rounds=2,
            skip_xlsx=False,
            skip_review=False,
            review_sample_size=8,
        )
    )

    assert result == 1
    assert calls == ["process", "match"]
    assert stale_workbook.exists()
    assert stale_workbook.read_text() == "old workbook"
    restored = json.loads((output_dir / "last_good_workpaper_restored.json").read_text())
    assert restored["reason"] == "Relationship reasoning returned an unusable register before a refreshed workbook was produced."


def test_prepare_workpaper_source_index_crash_uses_accountant_facing_progress(tmp_path: Path, monkeypatch):
    from accountant_copilot import cli

    client_folder = tmp_path / "client_docs"
    client_folder.mkdir()
    artifact_dir = tmp_path / "artifacts"
    output_dir = tmp_path / "workpaper"
    output_dir.mkdir()
    stale_workbook = output_dir / cli.TB_BRIDGE_XLSX
    stale_workbook.write_text("old workbook")

    def crash_during_source_index(args):
        raise FileNotFoundError("outputs/raw_inputs_pdf_extraction/per_document/raw_001.json")

    monkeypatch.setattr(cli, "_process_documents_command", crash_during_source_index)

    result = cli._prepare_workpaper_command(
        argparse.Namespace(
            client_folder=str(client_folder),
            artifact_dir=str(artifact_dir),
            output_dir=str(output_dir),
            entity_name=None,
            prior_fs_document_id=None,
            prior_fs_file=None,
            codex_command="codex exec",
            codex_timeout=1,
            codex_max_attempts=1,
            batch_size=5,
            force_reprocess=False,
            allow_cache=False,
            review_correction_rounds=2,
            skip_xlsx=False,
            skip_review=False,
            review_sample_size=8,
        )
    )

    progress = json.loads((output_dir / "prepare_workpaper_progress.json").read_text())

    assert result == 1
    assert stale_workbook.read_text() == "old workbook"
    assert progress["status"] == "failed"
    assert progress["message"] == "Tessa could not finish reading the uploaded files. Previous valid workbook was restored."
    assert "FileNotFoundError" not in progress["message"]
    assert "raw_001.json" not in progress["message"]
    assert progress["error_type"] == "FileNotFoundError"
    assert progress["last_good_restored"] is True
    assert progress["workbook_exists"] is True


def test_source_coverage_counts_documents_and_fact_types_separately(tmp_path: Path):
    from accountant_copilot import cli

    facts_path = tmp_path / "accounting_facts_by_document.json"
    facts_path.write_text(
        json.dumps(
            {
                "documents": [
                    {"document_id": "raw_001", "document_type": "bank_statement", "accounting_facts": [{"fact_type": "bank_transaction"}]},
                    {"document_id": "raw_002", "document_type": "capital_call", "accounting_facts": [{"fact_type": "capital_call"}]},
                ]
            }
        )
    )
    payload = cli._build_source_coverage_continuity_payload(json.loads(facts_path.read_text()))

    assert payload["document_type_counts"] == {"bank_statement": 1, "capital_call": 1}
    assert payload["fact_type_counts"] == {"bank_transaction": 1, "capital_call": 1}


def test_refresh_tb_bridge_inspect_hyperlink_labels(tmp_path: Path):
    from accountant_copilot.tb_bridge_workflow import refresh_tb_bridge_inspect_hyperlink_labels

    workbook_path = tmp_path / "step4_tb_bridge_workpaper.xlsx"
    inspect_path = tmp_path / "step4_tb_bridge_workpaper.xlsx.inspect.ndjson"
    inspect_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "table",
                        "sheet": "Evidence Index",
                        "values": [
                            ["Display name", "PDF"],
                            [
                                "Source.pdf",
                                "HYPERLINK is not implemented. linkLocation=file:///tmp/Source.pdf, friendlyName=Click here",
                            ],
                        ],
                    }
                ),
                json.dumps(
                    {
                        "kind": "region",
                        "sheet": "Evidence Index",
                        "preview": [
                            ["PDF"],
                            ["HYPERLINK is not implemented. linkLocation=file:///tmp/Source.pdf, friendlyName=Open file"],
                            ["HYPERLINK is not implemented. linkLocation=file:///tmp/Source..."],
                        ],
                    }
                ),
            ]
        )
        + "\n"
    )

    replacements = refresh_tb_bridge_inspect_hyperlink_labels(workbook_path)
    lines = [json.loads(line) for line in inspect_path.read_text().splitlines()]

    assert replacements == 3
    assert lines[0]["values"][1][1] == "Click here"
    assert lines[1]["preview"][1][0] == "Open file"
    assert lines[1]["preview"][2][0] == "Click here"
