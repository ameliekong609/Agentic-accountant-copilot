"""Senior accountant review and bounded correction helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from accountant_copilot.common import (
    _extract_json_object,
    _list_value,
    _normalise_codex_cli_command,
    _write_codex_attempt_history,
)
from accountant_copilot.accounting_knowledge import (
    accounting_pdf_retrieval_tool_for_prompt,
    client_evidence_guardrail_for_prompt,
    load_accounting_pdf_topic_map_for_prompt,
    load_accounting_reference_for_prompt,
    load_accounting_skill_for_prompt,
    non_client_evidence_reference_findings,
    source_of_truth_redo_instruction,
)
from accountant_copilot.contract_utils import TB_BRIDGE_JSON, TB_BRIDGE_XLSX

def _turing_review_needs_corrections(output_dir: Path) -> bool:
    review_json = output_dir / "turing_senior_review.json"
    if not review_json.exists():
        return False
    try:
        review_payload = json.loads(review_json.read_text())
    except json.JSONDecodeError:
        return False
    return review_payload.get("status") == "needs_corrections" and bool(review_payload.get("correction_briefs"))

def _severity_rank(value: object) -> int:
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(str(value or "").strip().lower(), 2)

def _is_internal_presentation_finding(finding: dict) -> bool:
    if str(finding.get("category") or "").strip().lower() != "presentation":
        return False
    message = str(finding.get("message") or "").casefold()
    return (
        ("evidence index" in message or "source hyperlink" in message or "hyperlink" in message)
        and ("blank" in message or "invisible" in message or "pdf cell" in message or "link" in message)
    )

def _public_turing_findings(review_payload: dict) -> list[dict]:
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    public: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if _is_internal_presentation_finding(finding):
            continue
        if _severity_rank(finding.get("severity")) <= 1:
            continue
        public.append(finding)
    return public

def _turing_review_has_blocking_items(review_payload: dict) -> bool:
    if review_payload.get("status") == "codex_failed":
        return True
    if _public_turing_findings(review_payload):
        return True
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    correction_briefs = review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else []
    if not correction_briefs:
        return False
    if findings:
        return any(isinstance(finding, dict) and finding in _public_turing_findings(review_payload) for finding in findings)
    return True

def _turing_review_is_ready(output_dir: Path) -> bool:
    review_json = output_dir / "turing_senior_review.json"
    if not review_json.exists():
        return False
    try:
        review_payload = json.loads(review_json.read_text())
    except json.JSONDecodeError:
        return False
    if review_payload.get("status") == "ready":
        return True
    if review_payload.get("status") == "needs_corrections" and not _turing_review_has_blocking_items(review_payload):
        return True
    return False

def _archive_turing_review_round(output_dir: Path, round_number: int) -> None:
    for suffix in [".md", ".json"]:
        current = output_dir / f"turing_senior_review{suffix}"
        if current.exists():
            archived = output_dir / f"turing_senior_review_round_{round_number}{suffix}"
            archived.write_text(current.read_text())

def _review_correction_findings(review_payload: dict) -> list[dict]:
    findings: list[dict] = []
    for index, brief in enumerate(review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else [], start=1):
        if not isinstance(brief, dict):
            continue
        brief_id = brief.get("brief_id") or f"C{index:03d}"
        message_parts = [
            f"Turing correction brief {brief_id}.",
            f"Issue: {brief.get('issue') or ''}",
            f"Expected treatment: {brief.get('expected_treatment') or ''}",
            f"Required workbook change: {brief.get('required_workbook_change') or ''}",
            f"Validation test: {brief.get('validation_test') or ''}",
        ]
        files_or_amounts = brief.get("files_or_amounts_to_recheck")
        if isinstance(files_or_amounts, list) and files_or_amounts:
            message_parts.append("Files or amounts to re-check: " + "; ".join(str(item) for item in files_or_amounts if item is not None))
        findings.append(
            {
                "category": "turing_correction_brief",
                "severity": "high",
                "message": " ".join(part for part in message_parts if part.strip()),
                "brief": brief,
            }
        )
    review_findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    for finding in review_findings:
        if not isinstance(finding, dict):
            continue
        findings.append(
            {
                "category": f"turing_review_finding:{finding.get('category') or 'other'}",
                "severity": finding.get("severity") or "medium",
                "message": finding.get("message") or "",
                "finding": finding,
            }
        )
    return findings

def _compact_review_text(value: object, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."

def _compact_turing_document_index(source_index: dict) -> list[dict]:
    documents = []
    for document in _list_value(source_index.get("documents")):
        if not isinstance(document, dict):
            continue
        documents.append(
            {
                "document_id": document.get("document_id"),
                "display_name": document.get("display_name") or document.get("file_name"),
                "document_type": document.get("document_type"),
                "file_path": document.get("file_path"),
                "entity_relevance": document.get("entity_relevance"),
                "period_start": document.get("period_start"),
                "period_end": document.get("period_end"),
                "statement_date": document.get("statement_date"),
                "summary": _compact_review_text(document.get("document_summary") or document.get("summary"), 260),
                "primary_amounts": _list_value(document.get("primary_amounts"))[:8],
                "review_flags": _list_value(document.get("review_flags"))[:6],
            }
        )
    return documents

def _compact_turing_event_register(event_register: dict) -> dict:
    relationships = []
    for item in _list_value(event_register.get("relationships")):
        if not isinstance(item, dict):
            continue
        accounts = []
        for account in _list_value(item.get("accounts_involved"))[:8]:
            if isinstance(account, dict):
                accounts.append(
                    {
                        "account_name": account.get("account_name"),
                        "role": account.get("role"),
                        "source": account.get("source"),
                        "confidence": account.get("confidence"),
                    }
                )
        relationships.append(
            {
                "relationship_id": item.get("relationship_id"),
                "relationship_type": item.get("relationship_type"),
                "status": item.get("status"),
                "confidence": item.get("confidence"),
                "evidence_level": item.get("evidence_level"),
                "story": _compact_review_text(item.get("story"), 420),
                "date": item.get("date"),
                "amount": item.get("amount"),
                "direction": item.get("direction"),
                "document_refs": _list_value(item.get("document_refs"))[:12],
                "accounts_involved": accounts,
                "open_questions": [_compact_review_text(question, 220) for question in _list_value(item.get("open_questions"))[:5]],
                "why_it_matters_for_step4": _compact_review_text(item.get("why_it_matters_for_step4"), 260),
            }
        )
    coverage = []
    for item in _list_value(event_register.get("prior_fs_account_movement_coverage")):
        if not isinstance(item, dict):
            continue
        coverage.append(
            {
                "account_name": item.get("account_name"),
                "statement_section": item.get("statement_section"),
                "opening_or_comparative_amount": item.get("opening_or_comparative_amount"),
                "coverage_status": item.get("coverage_status"),
                "relationship_ids": _list_value(item.get("relationship_ids"))[:10],
                "movement_story": _compact_review_text(item.get("movement_story"), 300),
            }
        )
    return {
        "artifact_type": event_register.get("artifact_type") or event_register.get("register_artifact_type"),
        "status": event_register.get("status"),
        "summary": event_register.get("summary") if isinstance(event_register.get("summary"), dict) else {},
        "relationships": relationships,
        "prior_fs_account_movement_coverage": coverage,
        "validation_findings": _list_value(event_register.get("validation_findings"))[:12],
    }

def _compact_turing_workpaper(workpaper_payload: dict) -> dict:
    columns = []
    for column in _list_value(workpaper_payload.get("movement_columns")):
        if not isinstance(column, dict):
            continue
        role = column.get("movement_role") if isinstance(column.get("movement_role"), dict) else {}
        columns.append(
            {
                "column_key": column.get("column_key"),
                "label": column.get("label"),
                "role_type": role.get("role_type") or column.get("column_type"),
                "accounting_purpose": _compact_review_text(role.get("accounting_purpose") or column.get("description"), 240),
                "support_type": column.get("support_type"),
                "description": _compact_review_text(column.get("description"), 220),
            }
        )
    rows = []
    for row in _list_value(workpaper_payload.get("matrix_rows")):
        if not isinstance(row, dict):
            continue
        movements = []
        for movement in _list_value(row.get("movements")):
            if not isinstance(movement, dict):
                continue
            movements.append(
                {
                    "column_key": movement.get("column_key"),
                    "amount": movement.get("amount"),
                    "support_type": movement.get("support_type"),
                    "relationship_id": movement.get("relationship_id"),
                    "note_id": movement.get("note_id"),
                    "explanation": _compact_review_text(movement.get("explanation"), 160),
                }
            )
        rows.append(
            {
                "account_name": row.get("account_name"),
                "account_type": row.get("account_type"),
                "statement_section": row.get("statement_section"),
                "statement_group": row.get("statement_group"),
                "opening_balance": row.get("opening_balance"),
                "prior_year_comparative": row.get("prior_year_comparative"),
                "movements": movements,
                "closing_balance": row.get("closing_balance"),
                "difference": row.get("difference"),
                "row_status": row.get("row_status"),
                "note_ids": _list_value(row.get("note_ids"))[:6],
                "notes": _compact_review_text(row.get("notes"), 120),
            }
        )
    notes = []
    for note in _list_value(workpaper_payload.get("movement_notes")):
        if not isinstance(note, dict):
            continue
        notes.append(
            {
                "note_id": note.get("note_id"),
                "account_name": note.get("account_name"),
                "status": note.get("status"),
                "tb_column": note.get("tb_column"),
                "main_amount": note.get("main_amount"),
                "other_amounts": _compact_review_text(note.get("other_amounts"), 240),
                "explanation": _compact_review_text(note.get("explanation"), 520),
                "calculation": _compact_review_text(note.get("calculation"), 260),
                "evidence_summary": _compact_review_text(note.get("evidence_summary"), 360),
                "relationship_ids": _list_value(note.get("relationship_ids"))[:10],
            }
        )
    return {
        "artifact_type": workpaper_payload.get("artifact_type"),
        "tb_bridge_contract_version": workpaper_payload.get("tb_bridge_contract_version"),
        "status": workpaper_payload.get("status"),
        "summary": workpaper_payload.get("summary") if isinstance(workpaper_payload.get("summary"), dict) else {},
        "validation_findings": _list_value(workpaper_payload.get("validation_findings"))[:12],
        "movement_columns": columns,
        "matrix_rows": rows,
        "movement_notes": notes,
        "workpaper_notes": [_compact_review_text(note, 260) for note in _list_value(workpaper_payload.get("workpaper_notes"))[:10]],
    }

def _compact_turing_prior_coa(prior_coa: dict | None) -> dict:
    prior = prior_coa if isinstance(prior_coa, dict) else {}
    accounts = []
    for account in _list_value(prior.get("accounts")):
        if not isinstance(account, dict):
            continue
        accounts.append(
            {
                "name": account.get("name"),
                "type": account.get("type"),
                "presentation_group": account.get("presentation_group"),
                "opening_balance": account.get("opening_balance"),
                "source_evidence_refs": _list_value(account.get("source_evidence_refs"))[:4],
            }
        )
    return {
        "prior_fs_document_id": prior.get("prior_fs_document_id"),
        "prior_fs_display_name": prior.get("prior_fs_display_name"),
        "accounts": accounts,
        "findings": _list_value(prior.get("findings"))[:8],
    }

def _turing_review_prompt(
    *,
    client_folder: Path | None,
    artifact_dir: Path,
    output_dir: Path,
    workpaper_payload: dict,
    source_index: dict,
    event_register: dict,
    prior_coa: dict | None,
    sample_size: int,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> str:
    review_pack = {
        "source_documents": _compact_turing_document_index(source_index),
        "event_register": _compact_turing_event_register(event_register),
        "tb_bridge_workpaper": _compact_turing_workpaper(workpaper_payload),
        "prior_year_opening_balances": _compact_turing_prior_coa(prior_coa),
    }
    redo_instruction = source_of_truth_redo_instruction(validation_findings)
    return json.dumps(
        {
            "task": "Act as Turing, the senior accountant supervisor. Review the prepared TB Bridge workpaper using Codex CLI with source-file access. Return JSON only.",
            "review_contract_version": "turing_senior_review_v1",
            "recovery_context": {
                "recovery_attempt": recovery_attempt,
                "previous_error": previous_error or "",
                "validation_findings": validation_findings or [],
                "previous_payload": previous_payload,
                "source_of_truth_redo_required": bool(redo_instruction),
                "instruction": redo_instruction or "If a previous attempt failed, repair the JSON and make the review more concrete. Keep output valid JSON only.",
            }
            if recovery_attempt
            else None,
            "workspace": {
                "cwd": str(Path.cwd()),
                "client_folder": str(client_folder) if client_folder else "",
                "artifact_dir": str(artifact_dir),
                "output_dir": str(output_dir),
                "workbook_path": str(output_dir / TB_BRIDGE_XLSX),
                "instruction": (
                    "You may inspect original files listed in source_documents[].file_path and the generated JSON/workbook artifacts. "
                    "Do not rely only on summaries when sampling material or judgemental rows."
                ),
            },
            "required_output_schema": {
                "artifact_type": "turing_senior_accountant_review",
                "review_contract_version": "turing_senior_review_v1",
                "status": "ready|needs_corrections|codex_failed",
                "reviewer": "turing",
                "entity_name": "entity name if known",
                "control_checks": [
                    {
                        "check": "movement_columns_balance_to_zero|row_roll_forward|opening_balances|pl_opening_zero|tax_valuation_boundary|evidence_links",
                        "status": "pass|warning|fail",
                        "summary": "short accountant-readable result",
                        "affected_rows_or_columns": ["labels"],
                    }
                ],
                "sampled_items": [
                    {
                        "sample_id": "S001",
                        "reason_selected": "material amount|judgement item|clearing row|source-only|bank-only|known tricky relationship|random low-risk sample",
                        "workpaper_item": "row/column/note/amount checked",
                        "amounts_checked": ["decimal strings"],
                        "source_documents_checked": [
                            {"document_id": "raw_001", "display_name": "display name", "file_path": "path", "page_or_evidence": "page/evidence id if known"}
                        ],
                        "original_evidence_observation": "what you saw in the original PDF/text/source, not only the JSON",
                        "conclusion": "pass|warning|fail",
                        "recommended_follow_up": "short action or blank",
                    }
                ],
                "findings": [
                    {
                        "finding_id": "F001",
                        "severity": "high|medium|low",
                        "category": "control_failure|source_mismatch|unsupported_amount|classification_judgement|tax_boundary|valuation_boundary|presentation|other",
                        "message": "short issue",
                        "affected_amounts": ["decimal strings"],
                        "affected_accounts_or_columns": ["labels"],
                    }
                ],
                "correction_briefs": [
                    {
                        "brief_id": "C001",
                        "issue": "what is wrong",
                        "expected_treatment": "what Codex should do",
                        "files_or_amounts_to_recheck": ["paths, doc ids, amounts"],
                        "required_workbook_change": "specific workbook change",
                        "validation_test": "specific recheck after fix",
                    }
                ],
                "summary": {
                    "control_checks": 0,
                    "sampled_items": 0,
                    "findings": 0,
                    "correction_briefs": 0,
                    "accountant_message": "short plain-English review result",
                },
            },
            "review_rules": [
                "Check all mathematical controls: every movement column adds to zero, each row opening + movements = closing, P&L opening balances are zero, and prior-year FS openings agree where possible.",
                "Do not manually re-check every cell. Review by risk: material balances, judgement rows, clearing rows, source-only items, bank-only items, tax/valuation boundaries, and known tricky relationships.",
                "Use status ready when the workbook is mathematically sound and judgement items are clearly surfaced for accountant review. Do not mark needs_corrections merely because accountant judgement remains.",
                "Use status needs_corrections only when Codex should change the workbook: math/control defects, unsupported posted amounts, wrong book/tax boundary, wrong classification, missing evidence status, or confusing presentation that could mislead the accountant.",
                "When sampling a material or judgement item, inspect original source documents or extracted page quotes from file_path values. State what original evidence you inspected in sampled_items[].original_evidence_observation.",
                "Do not trust the workbook JSON alone for sampled items. The review should verify against original PDFs/text or explain why original evidence could not be inspected.",
                "Check that book/financial-statement logic is used, not tax-component schedule logic. Franking credits, TFN withholding, ESVCLP offsets, and tax-only components should be notes unless there is a clear book posting.",
                "Check that NAV/market value movement is not posted by default unless fair value accounting is explicitly adopted.",
                "Check that beneficiary distribution/UPE is based on book bridge profit unless a different basis is explicitly documented.",
                "If a sampled item raises a technical accounting topic, consult accounting_pdf_topic_map and accounting_pdf_retrieval_tool for original PDF guidance, then verify the actual workbook support against client files. Do not cite the knowhow PDF as client evidence.",
                "Correction briefs must be actionable for Codex CLI: issue, expected treatment, files/amounts to re-check, workbook change, validation test.",
                "Keep the accountant-facing message concise. Do not over-explain low-risk passes.",
                "Never say the workpaper is final, lodged, posted, or approved. It is prepared for accountant review.",
            ],
            "risk_focus_examples": [
                "Spire total distribution vs banked amount vs source-only residual.",
                "ANZ/BENPI sale proceeds vs investment disposal and gain/loss.",
                "KPMG/ATO bank-only classifications.",
                "Beneficiary distribution/UPE calculation.",
                "Clearing rows and any column with a non-obvious balancing entry.",
                "Prior-year financial statement opening balances.",
            ],
            "accounting_skill": load_accounting_skill_for_prompt("senior-workpaper-review"),
            "accounting_reference": load_accounting_reference_for_prompt("senior-workpaper-review", "senior-review-checklist.md"),
            "accounting_pdf_retrieval_skill": load_accounting_skill_for_prompt("accounting-pdf-knowledge-retrieval"),
            "accounting_pdf_topic_map": load_accounting_pdf_topic_map_for_prompt(),
            "accounting_pdf_retrieval_tool": accounting_pdf_retrieval_tool_for_prompt(),
            "client_evidence_guardrail": client_evidence_guardrail_for_prompt(),
            "review_pack": review_pack,
        },
        indent=2,
        sort_keys=True,
    )

def _validate_turing_review(payload: dict | None) -> list[dict]:
    findings: list[dict] = []
    if not isinstance(payload, dict):
        return [{"category": "invalid_review_payload", "severity": "high", "message": "Codex did not return a JSON object."}]
    if payload.get("artifact_type") != "turing_senior_accountant_review":
        findings.append({"category": "invalid_artifact_type", "severity": "high", "message": "Review must return artifact_type turing_senior_accountant_review."})
    if payload.get("review_contract_version") != "turing_senior_review_v1":
        findings.append({"category": "invalid_review_contract_version", "severity": "high", "message": "Review contract version mismatch."})
    for key in ("control_checks", "sampled_items", "findings", "correction_briefs"):
        if not isinstance(payload.get(key), list):
            findings.append({"category": f"invalid_{key}", "severity": "high", "message": f"{key} must be a list."})
    if not isinstance(payload.get("summary"), dict):
        findings.append({"category": "invalid_summary", "severity": "medium", "message": "summary must be an object."})
    findings.extend(
        non_client_evidence_reference_findings(
            {
                "control_checks": payload.get("control_checks"),
                "sampled_items": payload.get("sampled_items"),
                "findings": payload.get("findings"),
                "correction_briefs": payload.get("correction_briefs"),
                "summary": payload.get("summary"),
            },
            stage="turing_senior_review",
            message=(
                "Turing review appears to cite knowhow, training material, or skills as evidence. "
                "Senior review may use skills as a checklist only; sampled evidence must come from client documents, prior FS, or generated workbook artifacts."
            ),
        )
    )
    return findings

def _normalise_turing_review(payload: dict, validation_findings: list[dict], attempt_history: list[dict]) -> dict:
    control_checks = payload.get("control_checks") if isinstance(payload.get("control_checks"), list) else []
    sampled_items = payload.get("sampled_items") if isinstance(payload.get("sampled_items"), list) else []
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    correction_briefs = payload.get("correction_briefs") if isinstance(payload.get("correction_briefs"), list) else []
    status = str(payload.get("status") or "")
    if status not in {"ready", "needs_corrections", "codex_failed"}:
        status = "needs_corrections" if findings or correction_briefs or validation_findings else "ready"
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "artifact_type": "turing_senior_accountant_review",
        "review_contract_version": "turing_senior_review_v1",
        "status": status,
        "reviewer": "turing",
        "entity_name": str(payload.get("entity_name") or ""),
        "control_checks": control_checks,
        "sampled_items": sampled_items,
        "findings": findings,
        "correction_briefs": correction_briefs,
        "summary": {
            "control_checks": int(summary.get("control_checks") or len(control_checks)),
            "sampled_items": int(summary.get("sampled_items") or len(sampled_items)),
            "findings": int(summary.get("findings") or len(findings)),
            "correction_briefs": int(summary.get("correction_briefs") or len(correction_briefs)),
            "accountant_message": str(summary.get("accountant_message") or ("Senior review found items needing correction." if findings or correction_briefs else "Senior review completed without major correction briefs.")),
        },
        "validation_findings": validation_findings,
        "codex_attempt_history": attempt_history,
    }

def _failed_turing_review(error: str, validation_findings: list[dict], attempt_history: list[dict]) -> dict:
    return {
        "artifact_type": "turing_senior_accountant_review",
        "review_contract_version": "turing_senior_review_v1",
        "status": "codex_failed",
        "reviewer": "turing",
        "entity_name": "",
        "control_checks": [],
        "sampled_items": [],
        "findings": [{"finding_id": "F001", "severity": "high", "category": "codex_review_failed", "message": error, "affected_amounts": [], "affected_accounts_or_columns": []}],
        "correction_briefs": [],
        "summary": {"control_checks": 0, "sampled_items": 0, "findings": 1, "correction_briefs": 0, "accountant_message": "Turing senior review could not run."},
        "validation_findings": validation_findings,
        "codex_attempt_history": attempt_history,
    }

def _format_turing_review(payload: dict) -> str:
    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    lines = ["# Turing Senior Accountant Review", ""]
    lines.extend(
        [
            f"- Status: {payload.get('status')}",
            f"- Reviewer: {payload.get('reviewer', 'turing')}",
            f"- Accountant message: {summary.get('accountant_message', '')}",
            "",
        ]
    )
    if payload.get("control_checks"):
        lines.append("## Control checks")
        for check in payload.get("control_checks", []):
            if not isinstance(check, dict):
                continue
            lines.append(f"- {check.get('check')}: {check.get('status')} — {check.get('summary')}")
        lines.append("")
    if payload.get("sampled_items"):
        lines.append("## Sampled source checks")
        for item in payload.get("sampled_items", []):
            if not isinstance(item, dict):
                continue
            lines.extend(
                [
                    f"### {item.get('sample_id')} — {item.get('workpaper_item')}",
                    f"- Reason selected: {item.get('reason_selected')}",
                    f"- Amounts checked: {', '.join(str(amount) for amount in item.get('amounts_checked', []) if amount is not None)}",
                    f"- Conclusion: {item.get('conclusion')}",
                    f"- Original evidence observation: {item.get('original_evidence_observation')}",
                    f"- Recommended follow-up: {item.get('recommended_follow_up', '')}",
                    "",
                ]
            )
    if payload.get("findings"):
        lines.append("## Findings")
        for finding in payload.get("findings", []):
            if not isinstance(finding, dict):
                continue
            lines.append(f"- {finding.get('severity')} / {finding.get('category')}: {finding.get('message')}")
        lines.append("")
    if payload.get("correction_briefs"):
        lines.append("## Correction briefs for Codex")
        for brief in payload.get("correction_briefs", []):
            if not isinstance(brief, dict):
                continue
            lines.extend(
                [
                    f"### {brief.get('brief_id')}",
                    f"- Issue: {brief.get('issue')}",
                    f"- Expected treatment: {brief.get('expected_treatment')}",
                    f"- Files or amounts to re-check: {', '.join(str(item) for item in brief.get('files_or_amounts_to_recheck', []) if item is not None)}",
                    f"- Required workbook change: {brief.get('required_workbook_change')}",
                    f"- Validation test: {brief.get('validation_test')}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"

def _codex_turing_review(
    *,
    client_folder: Path | None,
    artifact_dir: Path,
    output_dir: Path,
    workpaper_payload: dict,
    source_index: dict,
    event_register: dict,
    prior_coa: dict | None,
    command: str,
    timeout: int,
    sample_size: int,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> tuple[dict | None, str | None]:
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_TURING_REVIEW_JSON")
    if fake_payload:
        payload = _extract_json_object(fake_payload)
        return payload, None if payload is not None else "Fake Codex Turing review payload was not valid JSON."
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_turing_review_prompt(
                client_folder=client_folder,
                artifact_dir=artifact_dir,
                output_dir=output_dir,
                workpaper_payload=workpaper_payload,
                source_index=source_index,
                event_register=event_register,
                prior_coa=prior_coa,
                sample_size=sample_size,
                recovery_attempt=recovery_attempt,
                previous_error=previous_error,
                validation_findings=validation_findings,
                previous_payload=previous_payload,
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"Codex command was not found: {command}"
    except subprocess.TimeoutExpired:
        return None, f"Codex command timed out after {timeout} seconds."
    except (subprocess.SubprocessError, ValueError) as exc:
        return None, f"Codex command failed to start: {exc}"
    stderr = (result.stderr or "").strip()
    if result.returncode != 0:
        return None, f"Codex command exited {result.returncode}: {stderr[:500]}"
    if not result.stdout.strip():
        return None, f"Codex command returned no stdout. {stderr[:500]}".strip()
    payload = _extract_json_object(result.stdout)
    if payload is None:
        return None, f"Codex command did not return a JSON object. stdout={result.stdout[:500]!r}"
    return payload, None

def _review_workpaper_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    workpaper_json = Path(getattr(args, "workpaper_json", None) or output_dir / TB_BRIDGE_JSON)
    source_index_path = Path(getattr(args, "source_index", None) or artifact_dir / "source_document_index.json")
    event_register_path = Path(getattr(args, "event_register", None) or artifact_dir / "accounting_event_register.json")
    prior_coa_path = Path(getattr(args, "prior_coa", None) or artifact_dir / "prior_statement_coa_import.json")
    output_path = Path(getattr(args, "output", None) or output_dir / "turing_senior_review.md")
    client_folder = Path(args.client_folder).expanduser() if getattr(args, "client_folder", None) else None
    missing = [path for path in [workpaper_json, source_index_path, event_register_path] if not path.exists()]
    if missing:
        for path in missing:
            print(f"Required review input not found: {path}", file=sys.stderr)
        return 2
    workpaper_payload = json.loads(workpaper_json.read_text())
    source_index = json.loads(source_index_path.read_text())
    event_register = json.loads(event_register_path.read_text())
    prior_coa = json.loads(prior_coa_path.read_text()) if prior_coa_path.exists() else None
    if getattr(args, "entity_name", None):
        workpaper_payload["entity_name"] = getattr(args, "entity_name")
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 600) or 600)
    sample_size = max(1, int(getattr(args, "sample_size", 8) or 8))
    payload = None
    error = None
    validation_findings: list[dict] = []
    attempt_history: list[dict] = []
    previous_payload: dict | None = None
    attempt_history_path = output_path.parent / "turing_review_attempt_history.json"
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        payload, error = _codex_turing_review(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            workpaper_payload=workpaper_payload,
            source_index=source_index,
            event_register=event_register,
            prior_coa=prior_coa,
            command=codex_command,
            timeout=attempt_timeout,
            sample_size=sample_size,
            recovery_attempt=attempt - 1,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
        )
        validation_findings = _validate_turing_review(payload)
        attempt_history.append(
            {
                "attempt": attempt,
                "mode": "normal" if attempt == 1 else "recovery",
                "timeout_seconds": attempt_timeout,
                "status": "success" if payload is not None and not validation_findings else "failed",
                "error": error or "",
                "validation_findings": validation_findings,
            }
        )
        _write_codex_attempt_history(
            attempt_history_path,
            stage="turing_senior_review",
            attempts=attempt_history,
            status="success" if payload is not None and not validation_findings else "needs_attention",
            message=(
                f"Turing senior review attempt {attempt} produced a usable review."
                if payload is not None and not validation_findings
                else f"Turing senior review attempt {attempt} needs correction."
            ),
            extra={
                "current_error": error or "",
                "sample_size": sample_size,
            },
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not validation_findings:
            break
        if payload is not None and validation_findings:
            error = "Codex Turing review output failed schema validation."
    if payload is None:
        final_payload = _failed_turing_review(error or "Codex CLI did not return a usable Turing review.", validation_findings, attempt_history)
    elif validation_findings:
        final_payload = _failed_turing_review("Codex CLI returned a Turing review that did not pass validation.", validation_findings, attempt_history)
    else:
        final_payload = _normalise_turing_review(payload, validation_findings, attempt_history)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_codex_attempt_history(
        attempt_history_path,
        stage="turing_senior_review",
        attempts=attempt_history,
        status="failed" if final_payload.get("status") == "codex_failed" else "complete",
        message=str(final_payload.get("error") or "Turing senior review is ready."),
        extra={
            "review_json": str(output_path.with_suffix(".json")),
            "review_md": str(output_path),
            "validation_findings": final_payload.get("validation_findings") or [],
        },
    )
    output_path.write_text(_format_turing_review(final_payload))
    output_path.with_suffix(".json").write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    print(f"Exported Turing senior review -> {output_path}")
    print(f"Exported Turing senior review JSON -> {output_path.with_suffix('.json')}")
    return 1 if final_payload.get("status") == "codex_failed" else 0
