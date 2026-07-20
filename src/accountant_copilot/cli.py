"""Command-line interface for the Agentic Accountant Copilot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import traceback
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree as ET

from accountant_copilot.tb_bridge_workflow import (
    RELATIONSHIP_REASONING_CONTRACT_VERSION,
    TB_BRIDGE_CONTRACT_VERSION,
    TB_BRIDGE_JSON,
    TB_BRIDGE_MD,
    TB_BRIDGE_OUTPUT_DIR,
    TB_BRIDGE_XLSX,
    build_relationship_reasoning_prompt,
    build_tb_bridge_prompt,
    accounting_pdf_retrieval_tool_for_prompt,
    client_evidence_guardrail_for_prompt,
    enrich_tb_bridge_payload_for_workbook,
    failed_relationship_register,
    failed_tb_bridge_workpaper,
    format_relationship_register,
    format_tb_bridge_workpaper,
    load_accounting_reference_for_prompt,
    load_accounting_pdf_topic_map_for_prompt,
    load_accounting_skill_for_prompt,
    non_client_evidence_reference_findings,
    normalise_relationship_register,
    normalise_tb_bridge_workpaper,
    repair_tb_bridge_workbook_hyperlinks,
    source_of_truth_redo_instruction,
    validate_relationship_register,
    validate_tb_bridge_workpaper,
    write_tb_bridge_workbook_builder,
)

_PDF_PAGE_QUOTE_CHAR_LIMIT = 8000
SOURCE_MATCHING_CONTRACT_VERSION = RELATIONSHIP_REASONING_CONTRACT_VERSION
COA_MAPPING_CONTRACT_VERSION = TB_BRIDGE_CONTRACT_VERSION


def _load_local_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip("\"'")


_AMOUNT_RE = re.compile(r"(?:[$€£]\s?-?\d[\d,]*(?:\.\d{2})?|-?\d{1,3}(?:,\d{3})+(?:\.\d{2})?)")
_DATE_RE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b")


def _unique_matches(pattern: re.Pattern[str], text: str, limit: int = 8) -> list[str]:
    seen: list[str] = []
    for match in pattern.findall(text):
        value = match.strip()
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def _clean_money_amount(amount: str | None) -> str | None:
    if amount is None:
        return None
    return amount.replace("$ ", "$").replace("€ ", "€").replace("£ ", "£").replace("+ ", "").replace("+", "").strip()


def _money_value(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.-]", "", str(value))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _date_value(value: str | None) -> str | None:
    parsed = _parse_bank_statement_date(value)
    return parsed.strftime("%Y-%m-%d") if parsed else None


def _bank_transaction_amount(transaction: dict) -> float | None:
    return _money_value(transaction.get("debit") or transaction.get("credit"))


def _source_fact_match_candidates(invoice_payload: dict | None, distribution_payload: dict | None, broker_payload: dict | None) -> list[dict]:
    candidates: list[dict] = []
    for fact in (invoice_payload or {}).get("facts", []):
        candidates.append({
            "source_fact_type": "invoice",
            "amount": _money_value(fact.get("amount_due")),
            "date": _date_value(fact.get("due_date") or fact.get("invoice_date")),
            "evidence_id": fact.get("evidence_id"),
            "file_path": fact.get("file_path"),
            "page": fact.get("page"),
            "label": fact.get("invoice_number") or "invoice",
        })
    for fact in (distribution_payload or {}).get("facts", []):
        components = fact.get("components", {}) or {}
        amount = components.get("net_cash_distribution") or components.get("cash_distribution")
        candidates.append({
            "source_fact_type": "distribution_tax",
            "amount": _money_value(amount),
            "date": _date_value(fact.get("payment_date") or fact.get("record_date")),
            "evidence_id": fact.get("evidence_id"),
            "file_path": fact.get("file_path"),
            "page": fact.get("page"),
            "label": "distribution_tax",
        })
    for fact in (broker_payload or {}).get("facts", []):
        fields = fact.get("fields", {}) or {}
        candidates.append({
            "source_fact_type": "broker_trade",
            "amount": _money_value(fields.get("settlement_amount") or fields.get("consideration")),
            "date": _date_value(fields.get("settlement_date") or fields.get("transaction_date")),
            "evidence_id": fact.get("evidence_id"),
            "file_path": fact.get("file_path"),
            "page": fact.get("page"),
            "label": fact.get("side") or "broker_trade",
        })
    return [candidate for candidate in candidates if candidate.get("amount") is not None]


def _build_source_fact_matches_payload(bank_payload: dict, invoice_payload: dict | None, distribution_payload: dict | None, broker_payload: dict | None) -> dict:
    transactions = bank_payload.get("transactions", [])
    source_facts = _source_fact_match_candidates(invoice_payload, distribution_payload, broker_payload)
    matches: list[dict] = []
    findings: list[dict] = []
    for fact in source_facts:
        candidates = []
        for transaction in transactions:
            amount = _bank_transaction_amount(transaction)
            if amount is None or abs(amount - fact["amount"]) > 0.005:
                continue
            if fact.get("date") and _date_value(transaction.get("transaction_date")) != fact["date"]:
                continue
            candidates.append(transaction)
        if len(candidates) == 1:
            transaction = candidates[0]
            matches.append({
                "source_fact_type": fact["source_fact_type"],
                "source_evidence_id": fact.get("evidence_id"),
                "bank_evidence_id": transaction.get("evidence_id"),
                "amount": f"{fact['amount']:.2f}",
                "date": fact.get("date"),
                "match_type": "exact_amount_date",
                "approved": False,
                "evidence_refs": [ref for ref in [fact.get("evidence_id"), transaction.get("evidence_id")] if ref],
            })
        elif len(candidates) > 1:
            findings.append({
                "category": "ambiguous_source_fact_bank_match",
                "source_fact_type": fact["source_fact_type"],
                "source_evidence_id": fact.get("evidence_id"),
                "candidate_bank_evidence_ids": [item.get("evidence_id") for item in candidates],
                "recommended_action": "Accountant to choose the correct bank transaction or mark the source fact unmatched.",
            })
        else:
            findings.append({
                "category": "source_fact_bank_match_missing",
                "source_fact_type": fact["source_fact_type"],
                "source_evidence_id": fact.get("evidence_id"),
                "amount": f"{fact['amount']:.2f}",
                "date": fact.get("date"),
                "recommended_action": "Accountant to locate bank evidence, adjust matching tolerance, or record why no bank match is expected.",
            })
    return {
        "engagement_id": bank_payload.get("engagement_id"),
        "entity_name": bank_payload.get("entity_name"),
        "match_type": "source_fact_to_bank_transaction",
        "matches": matches,
        "findings": findings,
        "summary": {"bank_transactions": len(transactions), "source_facts": len(source_facts), "matches": len(matches), "findings": len(findings)},
    }


def _format_source_fact_matches(payload: dict) -> str:
    lines = [f"# Source Fact Bank Matches — {payload.get('entity_name') or 'engagement'}", ""]
    summary = payload["summary"]
    lines.extend([f"- Bank transactions: {summary['bank_transactions']}", f"- Source facts: {summary['source_facts']}", f"- Matches: {summary['matches']}", f"- Findings: {summary['findings']}", ""])
    if payload["matches"]:
        lines.append("## Proposed matches")
        for match in payload["matches"]:
            lines.extend([f"- {match['source_fact_type']}: {match['amount']} on {match.get('date') or 'unknown date'}", f"  - Approved: {match['approved']}", f"  - Evidence: {', '.join(match.get('evidence_refs', []))}"])
    if payload["findings"]:
        lines.extend(["", "## Findings needing review"])
        for finding in payload["findings"]:
            lines.extend([f"- {finding['category']}: {finding.get('source_fact_type')}", f"  - Evidence: {finding.get('source_evidence_id')}", f"  - Action: {finding['recommended_action']}"])
    return "\n".join(lines).rstrip() + "\n"


def _source_match_fact_rows_from_accounting_payload(facts_payload: dict) -> list[dict]:
    rows = _source_coverage_facts(facts_payload)
    for row in rows:
        fields = row.get("fields", {}) if isinstance(row.get("fields"), dict) else {}
        row["amount_candidates"] = [
            _clean_money_amount(str(fields.get(key)))
            for key in [
                "amount",
                "debit",
                "credit",
                "amount_due",
                "cash_distribution",
                "total_taxable_income",
                "net_settlement_amount",
                "settlement_amount",
                "gross_amount",
                "called_amount",
                "market_value",
                "closing_balance",
                "opening_balance",
            ]
            if fields.get(key) is not None and fields.get(key) != ""
        ]
        row["date_candidates"] = [
            str(fields.get(key))
            for key in [
                "date",
                "transaction_date",
                "invoice_date",
                "due_date",
                "payment_date",
                "distribution_date",
                "record_date",
                "trade_date",
                "settlement_date",
                "notice_date",
                "statement_date",
                "period_end",
                "statement_period_end",
            ]
            if fields.get(key) is not None and fields.get(key) != ""
        ]
        row["description_candidates"] = [
            str(fields.get(key))
            for key in [
                "description",
                "counterparty",
                "reference",
                "supplier_name",
                "investment_name",
                "security_name",
                "account_name",
                "line_item",
            ]
            if fields.get(key) is not None and fields.get(key) != ""
        ]
    return rows


_SOURCE_MATCH_EVIDENCE_KEYWORDS = [
    "payment instruction",
    "payment instructions",
    "eft",
    "bpay",
    "bank:",
    "bank ",
    "bsb",
    "account name",
    "account number",
    "payment reference",
    "reference",
    "distribution",
    "cash distribution",
    "gross cash distribution",
    "net cash distribution",
    "less: distribution",
    "market value summary",
    "performance summary",
    "one registry",
    "att:",
    "investor",
    "investor no",
    "registration",
    "address",
    "person 1",
    "person 2",
    "issued to",
    "benefit of the party",
    "westpac",
    "commbank",
    "commonwealth",
    "anz",
    "nab",
    "automic",
    "capital call",
    "due date",
]


def _source_match_excerpt(text: str, keyword: str, radius: int = 450) -> str:
    lowered = text.lower()
    index = lowered.find(keyword.lower())
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(text), index + len(keyword) + radius)
    return " ".join(text[start:end].split())


def _source_match_document_pages(document: dict) -> list[dict[str, str]]:
    page_quotes = document.get("page_quotes") if isinstance(document.get("page_quotes"), list) else []
    pages = [page for page in page_quotes if isinstance(page, dict) and page.get("quote")]
    if pages:
        return [{"page": str(page.get("page") or ""), "evidence_id": str(page.get("evidence_id") or ""), "quote": str(page.get("quote") or "")} for page in pages]
    file_path = document.get("file_path")
    if not file_path:
        return []
    path = Path(str(file_path))
    if not path.exists():
        return []
    try:
        return _document_text_for_codex(path)[0]
    except Exception:
        return []


def _source_match_document_evidence(facts_payload: dict) -> list[dict]:
    documents: list[dict] = []
    for document in facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []:
        if not isinstance(document, dict):
            continue
        excerpts: list[dict[str, str]] = []
        seen: set[str] = set()
        for page in _source_match_document_pages(document):
            quote = str(page.get("quote") or "")
            for keyword in _SOURCE_MATCH_EVIDENCE_KEYWORDS:
                excerpt = _source_match_excerpt(quote, keyword)
                if not excerpt or excerpt in seen:
                    continue
                seen.add(excerpt)
                excerpts.append(
                    {
                        "page": str(page.get("page") or ""),
                        "evidence_id": str(page.get("evidence_id") or ""),
                        "keyword": keyword,
                        "excerpt": excerpt[:1400],
                    }
                )
                if len(excerpts) >= 5:
                    break
            if len(excerpts) >= 5:
                break
        if not excerpts:
            continue
        documents.append(
            {
                "document_id": document.get("document_id"),
                "display_name": document.get("display_name") or document.get("file_name") or document.get("file_path"),
                "document_type": document.get("document_type"),
                "file_path": document.get("file_path"),
                "payment_or_matching_evidence": excerpts,
            }
        )
    return documents


def _source_match_document_index(facts_payload: dict) -> list[dict]:
    documents: list[dict] = []
    for document in facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []:
        if not isinstance(document, dict):
            continue
        pages = _source_match_document_pages(document)
        combined = " ".join(str(page.get("quote") or "") for page in pages)
        documents.append(
            {
                "document_id": document.get("document_id"),
                "display_name": document.get("display_name") or document.get("file_name") or document.get("file_path"),
                "original_file_name": document.get("original_file_name") or document.get("file_name"),
                "document_type": document.get("document_type"),
                "file_path": document.get("file_path"),
                "status": document.get("status"),
                "summary": document.get("document_summary") or "",
                "entity_relevance": document.get("entity_relevance") or "",
                "entity_relevance_reason": document.get("entity_relevance_reason") or "",
                "period_start": document.get("period_start") or "",
                "period_end": document.get("period_end") or "",
                "statement_date": document.get("statement_date") or "",
                "key_parties": document.get("key_parties") if isinstance(document.get("key_parties"), list) else [],
                "key_identifiers": document.get("key_identifiers") if isinstance(document.get("key_identifiers"), list) else [],
                "primary_amounts": document.get("primary_amounts") if isinstance(document.get("primary_amounts"), list) else [],
                "review_flags": document.get("review_flags") if isinstance(document.get("review_flags"), list) else [],
                "page_count": len(pages),
                "visible_dates": _unique_matches(_DATE_RE, combined, limit=10),
                "visible_amounts": _unique_matches(_AMOUNT_RE, combined, limit=12),
            }
        )
    return documents


def _source_match_context(facts_payload: dict, coverage_payload: dict | None) -> dict:
    facts = _source_match_fact_rows_from_accounting_payload(facts_payload)
    return {
        "matching_contract_version": SOURCE_MATCHING_CONTRACT_VERSION,
        "agent_mode": "codex_cli_workspace_investigation",
        "entity_name": facts_payload.get("entity_name", "Uploaded documents") if isinstance(facts_payload, dict) else "Uploaded documents",
        "workspace": {
            "cwd": str(Path.cwd()),
            "instruction": (
                "You are running as Codex CLI in this repository. You may inspect the supplied file_path values, "
                "search extracted page quotes, and compare original document text before returning the JSON event register."
            ),
            "important_files": {
                "source_document_index": "outputs/raw_inputs_pdf_extraction/source_document_index.json",
                "source_coverage_continuity": "outputs/raw_inputs_pdf_extraction/source_coverage_continuity.json",
                "input_documents_dir": "inputs",
            },
        },
        "document_index": _source_match_document_index(facts_payload),
        "facts": facts,
        "source_document_evidence": _source_match_document_evidence(facts_payload),
        "source_coverage_continuity": coverage_payload or {},
        "rules": [
            "Use Codex CLI as an investigative bookkeeper with workspace access: compare document names, document summaries, dates, amounts, descriptions, bank account identifiers, coverage findings, page quotes, and original source files.",
            "Step 2 is only a source document index. It deliberately does not extract detailed accounting facts. Build accounting events by reading source PDFs/page quotes and source document paths yourself.",
            "When you inspect original files or page quotes to support an event, include the document id in document_refs and cite the page/evidence id or exact amount/date in investigation_summary.",
            "Step 3 must build an Accounting Event Register, not CoA mappings or postings. Every review item should answer: what happened, what evidence supports it, and what is missing or judgemental.",
            "Run two passes. Source-first matching rule: read each source document's payment instructions, expected receiving bank/payee/account, payment reference, and due/payment date before searching bank statements. Cash-first matching rule: inspect bank statement documents and classify or resolve meaningful cash movements, even when no external source document exists. If legacy facts include every bank_transaction, review those too.",
            "Entity-first review rule: before matching or proposing journals, check whether the source document appears addressed to or held for the reporting entity/trust/company. If it appears to belong to Person 1/Person 2 or another non-reporting party, confidently mark it as entity_mismatch / likely irrelevant and recommend excluding it from this engagement unless the client confirms it belongs to the reporting entity.",
            "Assume documents are relevant unless there is a clear party/entity mismatch, a personal holder, or evidence that the document belongs to another entity. Do not create Step 3 review issues merely because an otherwise relevant document is old, out of period, or needs later accounting treatment.",
            "For capital calls, first search bank statement documents for the instructed receiving bank/payee/account/reference. Only then consider other-bank or related-party payments.",
            "If the source instructs payment to Westpac/AUTOMIC but the candidate cash movement is CommBank/ZXY, do not present a clean match. Mark it unresolved or a low/medium hypothesis unless source evidence explicitly supports that intermediary path.",
            "For broker sale confirmations, if cash date, amount, security, and broker/payee support a bank receipt but gross/net extraction labels appear inconsistent, keep the cash match in proposed_matches and add a separate unresolved extraction_gap item for the field check.",
            "Cash-first classification rule: if a bank transaction has no separate source document but the description is business-meaningful (for example KPMG service/accounting fees, ATO tax payments/refunds, bank fees, interest, platform fees, broker/FNZ cash settlements, or internal transfers), put it in proposed_matches as match_type bank_only_classification with source_fact_refs and bank_fact_refs empty if needed, and cite bank statement document ids in document_refs. State the inferred business meaning and clearly note that no external source document is attached.",
            "Before calling an investment distribution receipt bank-only, check investment/market value/AMIT statement facts and source_document_evidence for dated distribution rows, cash distribution totals, registry names, and exact amounts. A dated distribution line can support a source_and_bank event even when the bank receipt settles days or weeks later and the annual total does not equal the single receipt.",
            "Grouping rule: grouping is allowed only when the row is one coherent economic story, such as quarterly distributions for the same security into the same bank account, several bank-only KPMG service-fee payments, or a transfer sweep supported by component deposits. When you group, do not hide the components: include each component amount, date, source document id, bank document id where relevant, and the group total in investigation_summary. Do not group unrelated counterparties, unrelated document types, or items that need different judgement.",
            "General roll-up and residual rule: when a statement shows an annual/period total that breaks into dated components, banked receipts/payments, residual receivables/payables, withholding, fees, or timing differences, do the simple arithmetic yourself. Preserve both the total and the components in the event register narrative, for example total amount = bank-supported component + source-only residual. This rule must work for future clients and document types, not only the sample engagement.",
            "General multi-amount bank explanation rule: when one bank movement appears related to several source or bank amounts, add the component total and compare it to the bank movement. If there is a difference, explain the difference plainly instead of merely saying unmatched. Use this for transfers following sale deposits, distributions split across dates, batch payments, refunds offset against tax, or similar future-client patterns.",
            "If a source document page quote supports an event but Step 2 did not extract the exact line as a structured fact, include that source document id in document_refs and cite the page/evidence id in evidence_refs or investigation_summary. Do not label the event bank_only merely because the exact line is missing from source_fact_refs.",
            "Do not frame ordinary bank-only KPMG, ATO, bank fee, interest, or platform-fee transactions primarily as missing invoices. The useful Step 3 output is the cash classification and limitation, so downstream CoA mapping can decide the account treatment.",
            "If an event is monetary, populate amount with the event amount or grouped total. For unresolved monetary items with several candidate amounts, state the amounts in investigation_summary and use missing_or_judgement to explain the ambiguity.",
            "For each proposed match/classification, include event_type, event_readiness, evidence_level, event_meaning, evidence_summary, and missing_or_judgement.",
            "Use event_readiness complete for clean source+bank events and clear bank-only cash classifications; needs_judgement for plausible but judgement-heavy events; needs_support for items missing evidence or classification; excluded for wrong-entity/personal/non-accounting documents.",
            "Use evidence_level source_and_bank, bank_only, source_only, or no_accounting_event.",
            "Do not suggest debit accounts, credit accounts, CoA accounts, journals, or trial balance impact in Step 3. Those belong to Step 4. You may describe the business meaning, such as ATO tax account cash movement, KPMG service fee, investment distribution, capital call funding, broker settlement, internal transfer, bank/platform fee, or interest cash movement.",
            "If a bank transaction cannot be classified from the bank description, amount, date, direction, or surrounding context, put it in unresolved_items with issue_type unclassified_bank_transaction and explain what is missing.",
            "If a source fact should have cash support but no bank transaction can be found after checking instructed bank/payee/reference and reasonable timing differences, put it in unresolved_items with issue_type missing_bank_match or unmatched_source_fact.",
            "Do not book, approve, or ask the junior accountant to approve every row. Proposed matches are an event register for Step 4; only surface real exceptions, missing support, ambiguity, or judgement points.",
            "If a relationship is plausible but not proven, put it in hypotheses, not proposed_matches.",
            "If extraction appears incomplete or a source cannot be matched, put it in unresolved_items with why and what to check next.",
            "Use source_fact_refs and bank_fact_refs only when supplied facts exist. Otherwise leave those arrays empty and cite document_refs plus page/evidence ids.",
            "Never fabricate evidence, dates, amounts, accounts, or transactions.",
        ],
    }


def _codex_source_match_prompt(
    facts_payload: dict,
    coverage_payload: dict | None,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> str:
    return build_relationship_reasoning_prompt(
        facts_payload,
        coverage_payload,
        recovery_attempt=recovery_attempt,
        previous_error=previous_error,
        validation_findings=validation_findings,
        previous_payload=previous_payload,
    )


def _codex_investigate_source_matches(
    facts_payload: dict,
    coverage_payload: dict | None,
    command: str,
    timeout: int,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> tuple[dict | None, str | None]:
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_SOURCE_MATCH_JSON")
    if fake_payload:
        payload = _extract_json_object(fake_payload)
        return payload, None if payload is not None else "Fake Codex source match payload was not valid JSON."
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_codex_source_match_prompt(
                facts_payload,
                coverage_payload,
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


def _list_value(value: object) -> list:
    return value if isinstance(value, list) else []


def _validate_investigative_source_matches(payload: dict | None, facts_payload: dict) -> list[dict]:
    return validate_relationship_register(payload, facts_payload)


def _normalise_investigative_source_matches(payload: dict, facts_payload: dict, validation_findings: list[dict]) -> dict:
    return normalise_relationship_register(payload, facts_payload, validation_findings)


def _codex_failed_source_match_payload(facts_payload: dict, error: str, attempt_history: list[dict], validation_findings: list[dict] | None = None) -> dict:
    return failed_relationship_register(facts_payload, error, attempt_history, validation_findings)


def _format_investigative_source_matches(payload: dict) -> str:
    return format_relationship_register(payload)


def _load_optional_json(path: str | None) -> dict | None:
    return json.loads(Path(path).read_text()) if path else None


def _match_source_facts_from_accounting_command(args: argparse.Namespace) -> int:
    accounting_facts_path = Path(args.accounting_facts)
    if not accounting_facts_path.exists():
        print(f"Accounting facts file not found: {accounting_facts_path}", file=sys.stderr)
        return 2
    facts_payload = json.loads(accounting_facts_path.read_text())
    coverage_payload = _load_optional_json(getattr(args, "source_coverage", None))
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 120) or 120)
    payload = None
    error = None
    validation_findings: list[dict] = []
    attempt_history: list[dict] = []
    previous_payload: dict | None = None
    output = Path(args.output)
    progress_path = output.parent / "relationship_reasoning_progress.json"
    attempt_history_path = output.parent / "relationship_reasoning_attempt_history.json"
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        _write_step_progress(
            progress_path,
            {
                "stage": "relationship_reasoning",
                "status": "running",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": f"Investigating accounting relationships attempt {attempt} of {max_attempts}.",
            },
        )
        payload, error = _codex_investigate_source_matches(
            facts_payload,
            coverage_payload,
            codex_command,
            attempt_timeout,
            recovery_attempt=attempt - 1,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
        )
        validation_findings = _validate_investigative_source_matches(payload, facts_payload)
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
            stage="relationship_reasoning",
            attempts=attempt_history,
            status="success" if payload is not None and not validation_findings else "needs_attention",
            message=(
                f"Relationship reasoning attempt {attempt} produced a usable event register."
                if payload is not None and not validation_findings
                else f"Relationship reasoning attempt {attempt} needs correction."
            ),
            extra={
                "current_error": error or "",
                "relationship_count": len(payload.get("relationships") if isinstance(payload, dict) and isinstance(payload.get("relationships"), list) else []),
            },
        )
        _write_step_progress(
            progress_path,
            {
                "stage": "relationship_reasoning",
                "status": "success" if payload is not None and not validation_findings else "needs_attention",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": (
                    f"Relationship reasoning attempt {attempt} produced a usable event register."
                    if payload is not None and not validation_findings
                    else f"Relationship reasoning attempt {attempt} needs correction."
                ),
                "error": error or "",
                "validation_findings": validation_findings,
            },
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not validation_findings:
            break
        if payload is not None and validation_findings:
            error = "Codex source matching output failed schema validation."
    if payload is None:
        final_payload = _codex_failed_source_match_payload(facts_payload, error or "Codex CLI did not return a usable source matching result.", attempt_history, validation_findings)
    elif validation_findings:
        final_payload = _codex_failed_source_match_payload(facts_payload, "Codex CLI returned a source matching result that did not pass validation.", attempt_history, validation_findings)
    else:
        final_payload = _normalise_investigative_source_matches(payload, facts_payload, validation_findings)
        final_payload["codex_attempt_history"] = attempt_history
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_investigative_source_matches(final_payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    event_register_md = output.parent / "accounting_event_register.md"
    event_register_json = output.parent / "accounting_event_register.json"
    relationship_register_md = output.parent / "relationship_reasoning_register.md"
    relationship_register_json = output.parent / "relationship_reasoning_register.json"
    event_register_md.write_text(_format_investigative_source_matches(final_payload))
    event_register_json.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    relationship_register_md.write_text(_format_investigative_source_matches(final_payload))
    relationship_register_json.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    print(f"Exported Codex relationship reasoning -> {relationship_register_md}")
    print(f"Exported Codex relationship reasoning JSON -> {relationship_register_json}")
    if final_payload.get("status") == "codex_failed":
        _write_codex_attempt_history(
            attempt_history_path,
            stage="relationship_reasoning",
            attempts=attempt_history,
            status="failed",
            message=str(final_payload.get("error") or "Relationship reasoning failed."),
            extra={"validation_findings": final_payload.get("validation_findings") or []},
        )
        _write_step_progress(
            progress_path,
            {
                "stage": "relationship_reasoning",
                "status": "failed",
                "attempts": attempt_history,
                "message": str(final_payload.get("error") or "Relationship reasoning failed."),
                "validation_findings": final_payload.get("validation_findings") or [],
            },
        )
        return 1
    _write_codex_attempt_history(
        attempt_history_path,
        stage="relationship_reasoning",
        attempts=attempt_history,
        status="complete" if not final_payload.get("validation_findings") else "needs_attention",
        message="Accounting event register is ready." if not final_payload.get("validation_findings") else "Accounting event register was produced with validation notes.",
        extra={
            "relationship_count": len(final_payload.get("relationships") if isinstance(final_payload.get("relationships"), list) else []),
            "event_register_path": str(event_register_json),
        },
    )
    _write_step_progress(
        progress_path,
        {
            "stage": "relationship_reasoning",
            "status": "complete" if not final_payload.get("validation_findings") else "needs_attention",
            "attempts": attempt_history,
            "message": "Accounting event register is ready." if not final_payload.get("validation_findings") else "Accounting event register was produced with validation notes.",
            "relationship_count": len(final_payload.get("relationships") if isinstance(final_payload.get("relationships"), list) else []),
            "event_register_path": str(event_register_json),
        },
    )
    return 0 if not final_payload.get("validation_findings") else 1


def _match_source_facts_command(args: argparse.Namespace) -> int:
    if getattr(args, "accounting_facts", None):
        return _match_source_facts_from_accounting_command(args)
    if not getattr(args, "bank_transactions", None):
        print("--bank-transactions is required unless --accounting-facts is supplied.", file=sys.stderr)
        return 2
    bank_payload = json.loads(Path(args.bank_transactions).read_text())
    payload = _build_source_fact_matches_payload(
        bank_payload,
        _load_optional_json(getattr(args, "invoice_facts", None)),
        _load_optional_json(getattr(args, "distribution_tax_facts", None)),
        _load_optional_json(getattr(args, "broker_trade_facts", None)),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_format_source_fact_matches(payload))
    json_output = output.with_suffix(".json")
    json_output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"Exported source fact matches → {output}")
    print(f"Exported source fact matches JSON → {json_output}")
    return 0 if not payload["findings"] else 1


def _codex_coa_mapping_prompt(
    event_register: dict,
    source_index: dict,
    prior_coa: dict | None = None,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> str:
    return build_tb_bridge_prompt(
        event_register,
        source_index,
        prior_coa,
        recovery_attempt=recovery_attempt,
        previous_error=previous_error,
        validation_findings=validation_findings,
        previous_payload=previous_payload,
    )


def _codex_map_coa_from_events(
    event_register: dict,
    source_index: dict,
    prior_coa: dict | None,
    command: str,
    timeout: int,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
    candidate_output_path: Path | None = None,
) -> tuple[dict | None, str | None]:
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_TB_BRIDGE_JSON") or os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_COA_MAPPING_JSON")
    if fake_payload:
        payload = _extract_json_object(fake_payload)
        return payload, None if payload is not None else "Fake Codex TB bridge payload was not valid JSON."
    if candidate_output_path is not None and candidate_output_path.exists():
        try:
            candidate_output_path.unlink()
        except OSError:
            pass
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_codex_coa_mapping_prompt(
                event_register,
                source_index,
                prior_coa,
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
    sidecar_payload, sidecar_error = _read_json_object_file(candidate_output_path)
    if result.returncode != 0:
        if sidecar_payload is not None:
            return sidecar_payload, None
        return None, f"Codex command exited {result.returncode}: {stderr[:500]}"
    if not result.stdout.strip():
        if sidecar_payload is not None:
            return sidecar_payload, None
        if sidecar_error:
            return None, sidecar_error
        return None, f"Codex command returned no stdout. {stderr[:500]}".strip()
    payload = _extract_json_object(result.stdout)
    if payload is None:
        if sidecar_payload is not None:
            return sidecar_payload, None
        if sidecar_error:
            return None, sidecar_error
        return None, f"Codex command did not return a JSON object. stdout={result.stdout[:500]!r}"
    return payload, None


def _validate_coa_mapping_workpaper(payload: dict | None, event_register: dict, prior_coa: dict | None = None) -> list[dict]:
    return validate_tb_bridge_workpaper(payload, event_register, prior_coa)


def _blocking_validation_findings(findings: list[dict]) -> list[dict]:
    return [finding for finding in findings if isinstance(finding, dict) and finding.get("severity") == "high"]


def _normalise_coa_mapping_workpaper(payload: dict, event_register: dict, validation_findings: list[dict]) -> dict:
    return normalise_tb_bridge_workpaper(payload, event_register, validation_findings)


def _codex_failed_coa_mapping_payload(event_register: dict, error: str, attempt_history: list[dict], validation_findings: list[dict] | None = None) -> dict:
    return failed_tb_bridge_workpaper(event_register, error, attempt_history, validation_findings)


def _format_coa_mapping_workpaper(payload: dict) -> str:
    return format_tb_bridge_workpaper(payload)


def _build_coa_mapping_workpaper_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    event_register_path = Path(getattr(args, "event_register", None) or artifact_dir / "accounting_event_register.json")
    source_index_path = Path(getattr(args, "source_index", None) or artifact_dir / "source_document_index.json")
    prior_coa_path = Path(getattr(args, "prior_coa", None) or artifact_dir / "prior_statement_coa_import.json")
    if not event_register_path.exists():
        print(f"Accounting event register not found: {event_register_path}", file=sys.stderr)
        return 2
    if not source_index_path.exists():
        print(f"Source document index not found: {source_index_path}", file=sys.stderr)
        return 2
    event_register = json.loads(event_register_path.read_text())
    source_index = json.loads(source_index_path.read_text())
    if getattr(args, "prior_coa", None):
        prior_coa = json.loads(prior_coa_path.read_text()) if prior_coa_path.exists() else {}
    else:
        prior_coa = _build_prior_statement_coa_from_source_index(
            source_index,
            prior_fs_document_id=getattr(args, "prior_fs_document_id", None),
            prior_fs_file=getattr(args, "prior_fs_file", None),
        )
        prior_coa_path.parent.mkdir(parents=True, exist_ok=True)
        prior_coa_path.write_text(json.dumps(prior_coa, indent=2, sort_keys=True))
        prior_coa_path.with_suffix(".md").write_text(_format_prior_statement_coa_import(prior_coa))
        blocking_findings = [finding for finding in _list_value(prior_coa.get("findings")) if isinstance(finding, dict) and finding.get("severity") == "high"]
        if blocking_findings or not _list_value(prior_coa.get("accounts")):
            for finding in blocking_findings:
                print(f"{finding.get('category')}: {finding.get('message') or finding.get('recommended_action') or ''}", file=sys.stderr)
            print(f"Prior-year FS opening balance import is not usable: {prior_coa_path}", file=sys.stderr)
            return 2
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 600) or 600)
    payload = None
    error = None
    validation_findings: list[dict] = []
    attempt_history: list[dict] = []
    previous_payload: dict | None = None
    generation_progress_path = output_dir / "tb_bridge_generation_progress.json"
    attempt_history_path = output_dir / "tb_bridge_attempt_history.json"
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "tb_bridge_generation",
                "status": "running",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": f"Preparing TB bridge attempt {attempt} of {max_attempts}.",
            },
        )
        payload, error = _codex_map_coa_from_events(
            event_register,
            source_index,
            prior_coa,
            codex_command,
            attempt_timeout,
            recovery_attempt=attempt - 1,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
            candidate_output_path=output_dir / TB_BRIDGE_JSON,
        )
        validation_findings = _validate_coa_mapping_workpaper(payload, event_register, prior_coa)
        blocking_findings = _blocking_validation_findings(validation_findings)
        attempt_history.append(
            {
                "attempt": attempt,
                "mode": "normal" if attempt == 1 else "recovery",
                "timeout_seconds": attempt_timeout,
                "status": "success" if payload is not None and not blocking_findings else "failed",
                "error": error or "",
                "validation_findings": validation_findings,
            }
        )
        _write_codex_attempt_history(
            attempt_history_path,
            stage="tb_bridge_generation",
            attempts=attempt_history,
            status="success" if payload is not None and not blocking_findings else "needs_attention",
            message=(
                f"TB bridge attempt {attempt} produced a usable workbook shape."
                if payload is not None and not blocking_findings
                else f"TB bridge attempt {attempt} needs correction."
            ),
            extra={
                "current_error": error or "",
                "blocking_findings": blocking_findings,
                "candidate_output_path": str(output_dir / TB_BRIDGE_JSON),
            },
        )
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "tb_bridge_generation",
                "status": "success" if payload is not None and not blocking_findings else "needs_attention",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": (
                    f"TB bridge attempt {attempt} produced a usable workbook shape."
                    if payload is not None and not blocking_findings
                    else f"TB bridge attempt {attempt} needs correction."
                ),
                "error": error or "",
                "validation_findings": validation_findings,
            },
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not blocking_findings:
            break
        if payload is not None and blocking_findings:
            error = "Codex TB bridge output failed schema validation."
    if payload is None:
        final_payload = _codex_failed_coa_mapping_payload(event_register, error or "Codex CLI did not return a usable TB bridge result.", attempt_history, validation_findings)
    elif _blocking_validation_findings(validation_findings):
        final_payload = _codex_failed_coa_mapping_payload(event_register, "Codex CLI returned a TB bridge result that did not pass validation.", attempt_history, validation_findings)
    else:
        final_payload = _normalise_coa_mapping_workpaper(payload, event_register, validation_findings)
        final_payload["codex_attempt_history"] = attempt_history
    output_dir.mkdir(parents=True, exist_ok=True)
    final_payload = enrich_tb_bridge_payload_for_workbook(final_payload, event_register, source_index, prior_coa)
    json_output = output_dir / TB_BRIDGE_JSON
    md_output = output_dir / TB_BRIDGE_MD
    json_output.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    md_output.write_text(_format_coa_mapping_workpaper(final_payload))
    print(f"Exported Codex TB Bridge Matrix JSON -> {json_output}")
    print(f"Exported Codex TB Bridge Matrix notes -> {md_output}")
    if final_payload.get("status") == "codex_failed":
        _write_codex_attempt_history(
            attempt_history_path,
            stage="tb_bridge_generation",
            attempts=attempt_history,
            status="failed",
            message=str(final_payload.get("error") or "TB bridge generation failed."),
            extra={"validation_findings": final_payload.get("validation_findings") or []},
        )
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "tb_bridge_generation",
                "status": "failed",
                "attempts": attempt_history,
                "message": str(final_payload.get("error") or "TB bridge generation failed."),
                "validation_findings": final_payload.get("validation_findings") or [],
            },
        )
        return 1
    _write_codex_attempt_history(
        attempt_history_path,
        stage="tb_bridge_generation",
        attempts=attempt_history,
        status="complete" if not _blocking_validation_findings(final_payload.get("validation_findings") or []) else "needs_attention",
        message="TB bridge data is ready." if not _blocking_validation_findings(final_payload.get("validation_findings") or []) else "TB bridge data was produced with validation notes.",
        extra={
            "workpaper_json": str(json_output),
            "workpaper_md": str(md_output),
            "validation_findings": final_payload.get("validation_findings") or [],
        },
    )
    if not getattr(args, "skip_xlsx", False):
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "workbook_build",
                "status": "running",
                "attempts": attempt_history,
                "message": "Building the Excel workbook from the TB bridge data.",
                "json_path": str(json_output),
            },
        )
        builder = write_tb_bridge_workbook_builder(
            output_dir,
            os.environ.get("ACCOUNTANT_COPILOT_NODE_MODULES", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"),
        )
        node_bin = os.environ.get("ACCOUNTANT_COPILOT_NODE", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
        result = subprocess.run([node_bin, str(builder)], cwd=Path.cwd(), text=True, capture_output=True, check=False)
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        if result.returncode != 0:
            _write_step_progress(
                generation_progress_path,
                {
                    "stage": "workbook_build",
                    "status": "failed",
                    "attempts": attempt_history,
                    "message": "Excel workbook build failed.",
                    "returncode": result.returncode,
                    "stdout": (result.stdout or "")[-2000:],
                    "stderr": (result.stderr or "")[-2000:],
                },
            )
            return result.returncode
        repaired = repair_tb_bridge_workbook_hyperlinks(output_dir / TB_BRIDGE_XLSX)
        if repaired:
            print(f"Repaired Evidence Index hyperlinks -> {repaired} link(s)")
    _write_step_progress(
        generation_progress_path,
        {
            "stage": "workbook_build",
            "status": "complete",
            "attempts": attempt_history,
            "message": "TB bridge workbook data is ready.",
            "workbook_path": str(output_dir / TB_BRIDGE_XLSX),
            "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
        },
    )
    return 0 if not _blocking_validation_findings(final_payload.get("validation_findings") or []) else 1


def _write_tb_bridge_outputs(
    *,
    output_dir: Path,
    payload: dict,
    event_register: dict,
    source_index: dict,
    prior_coa: dict | None,
    skip_xlsx: bool,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    final_payload = enrich_tb_bridge_payload_for_workbook(payload, event_register, source_index, prior_coa)
    json_output = output_dir / TB_BRIDGE_JSON
    md_output = output_dir / TB_BRIDGE_MD
    json_output.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    md_output.write_text(_format_coa_mapping_workpaper(final_payload))
    print(f"Exported Codex TB Bridge Matrix JSON -> {json_output}")
    print(f"Exported Codex TB Bridge Matrix notes -> {md_output}")
    if skip_xlsx:
        return 0
    builder = write_tb_bridge_workbook_builder(
        output_dir,
        os.environ.get("ACCOUNTANT_COPILOT_NODE_MODULES", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"),
    )
    node_bin = os.environ.get("ACCOUNTANT_COPILOT_NODE", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
    result = subprocess.run([node_bin, str(builder)], cwd=Path.cwd(), text=True, capture_output=True, check=False)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode == 0:
        repaired = repair_tb_bridge_workbook_hyperlinks(output_dir / TB_BRIDGE_XLSX)
        if repaired:
            print(f"Repaired Evidence Index hyperlinks -> {repaired} link(s)")
    return result.returncode


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


def _write_turing_correction_round_log(
    *,
    output_dir: Path,
    correction_round: int,
    review_payload: dict,
    attempt_history: list[dict],
    status: str,
    error: str = "",
    validation_findings: list[dict] | None = None,
    corrected_payload: dict | None = None,
    output_return_code: int | None = None,
) -> None:
    round_label = str(correction_round or "latest")
    json_path = output_dir / f"turing_correction_round_{round_label}_log.json"
    md_path = output_dir / f"turing_correction_round_{round_label}_log.md"
    briefs = review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else []
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    corrected_summary = corrected_payload.get("summary") if isinstance(corrected_payload, dict) and isinstance(corrected_payload.get("summary"), dict) else {}
    payload = {
        "artifact_type": "turing_correction_round_log",
        "correction_round": correction_round,
        "status": status,
        "error": error,
        "review_status_before_correction": review_payload.get("status"),
        "findings_before_correction": findings,
        "correction_briefs": briefs,
        "attempt_history": attempt_history,
        "validation_findings_after_correction": validation_findings or [],
        "corrected_workpaper_summary": corrected_summary,
        "output_return_code": output_return_code,
        "outputs": {
            "tb_bridge_json": str(output_dir / TB_BRIDGE_JSON),
            "tb_bridge_markdown": str(output_dir / TB_BRIDGE_MD),
            "tb_bridge_workbook": str(output_dir / TB_BRIDGE_XLSX),
            "review_json": str(output_dir / "turing_senior_review.json"),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    lines = [f"# Turing Correction Round {round_label}", "", f"- Status: {status}"]
    if error:
        lines.append(f"- Error: {error}")
    if output_return_code is not None:
        lines.append(f"- Output return code: {output_return_code}")
    lines.extend(["", "## Issues Turing Asked Tessa To Fix"])
    if briefs:
        for brief in briefs:
            if not isinstance(brief, dict):
                continue
            lines.extend(
                [
                    f"### {brief.get('brief_id', 'brief')}",
                    f"- Issue: {brief.get('issue', '')}",
                    f"- Expected treatment: {brief.get('expected_treatment', '')}",
                    f"- Required workbook change: {brief.get('required_workbook_change', '')}",
                    f"- Validation test: {brief.get('validation_test', '')}",
                    "",
                ]
            )
    else:
        lines.append("- No correction briefs were supplied.")
    lines.append("## Attempts")
    for attempt in attempt_history:
        if not isinstance(attempt, dict):
            continue
        lines.append(
            f"- Attempt {attempt.get('attempt')}: {attempt.get('status')} "
            f"(timeout {attempt.get('timeout_seconds')}s)"
        )
        if attempt.get("error"):
            lines.append(f"  Error: {attempt.get('error')}")
        findings_after = attempt.get("validation_findings") if isinstance(attempt.get("validation_findings"), list) else []
        if findings_after:
            lines.append(f"  Validation findings: {len(findings_after)}")
    if corrected_summary:
        lines.extend(["", "## Corrected Workpaper Summary"])
        for key, value in corrected_summary.items():
            lines.append(f"- {key}: {value}")
    md_path.write_text("\n".join(lines).rstrip() + "\n")


def _apply_turing_corrections_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    workpaper_json = Path(getattr(args, "workpaper_json", None) or output_dir / TB_BRIDGE_JSON)
    review_json = Path(getattr(args, "review_json", None) or output_dir / "turing_senior_review.json")
    source_index_path = Path(getattr(args, "source_index", None) or artifact_dir / "source_document_index.json")
    event_register_path = Path(getattr(args, "event_register", None) or artifact_dir / "accounting_event_register.json")
    prior_coa_path = Path(getattr(args, "prior_coa", None) or artifact_dir / "prior_statement_coa_import.json")
    missing = [path for path in [workpaper_json, review_json, source_index_path, event_register_path] if not path.exists()]
    if missing:
        for path in missing:
            print(f"Required correction input not found: {path}", file=sys.stderr)
        return 2
    workpaper_payload = json.loads(workpaper_json.read_text())
    review_payload = json.loads(review_json.read_text())
    source_index = json.loads(source_index_path.read_text())
    event_register = json.loads(event_register_path.read_text())
    prior_coa = json.loads(prior_coa_path.read_text()) if prior_coa_path.exists() else None
    correction_round = int(getattr(args, "correction_round", 0) or 0)
    correction_findings = _review_correction_findings(review_payload)
    if not correction_findings:
        print("Turing review did not include correction briefs, so no correction pass is required.")
        return 0
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 600) or 600)
    payload = None
    error = "Turing senior review found correction briefs. Apply the briefs and return the complete corrected TB bridge workpaper JSON."
    validation_findings: list[dict] = correction_findings
    attempt_history: list[dict] = []
    attempt_history_path = output_dir / f"turing_correction_round_{correction_round or 'latest'}_attempt_history.json"
    previous_payload: dict | None = workpaper_payload
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        payload, error = _codex_map_coa_from_events(
            event_register,
            source_index,
            prior_coa,
            codex_command,
            attempt_timeout,
            recovery_attempt=attempt,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
            candidate_output_path=output_dir / TB_BRIDGE_JSON,
        )
        validation_findings = _validate_coa_mapping_workpaper(payload, event_register, prior_coa)
        attempt_history.append(
            {
                "attempt": attempt,
                "mode": "turing_correction",
                "timeout_seconds": attempt_timeout,
                "status": "success" if payload is not None and not validation_findings else "failed",
                "error": error or "",
                "validation_findings": validation_findings,
            }
        )
        _write_codex_attempt_history(
            attempt_history_path,
            stage="turing_correction",
            attempts=attempt_history,
            status="success" if payload is not None and not validation_findings else "needs_attention",
            message=(
                f"Turing correction round {correction_round or 'latest'} attempt {attempt} produced a usable corrected workpaper."
                if payload is not None and not validation_findings
                else f"Turing correction round {correction_round or 'latest'} attempt {attempt} needs correction."
            ),
            extra={"correction_round": correction_round, "current_error": error or ""},
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not validation_findings:
            break
        if payload is not None and validation_findings:
            error = "Codex correction output failed schema validation."
    if payload is None or validation_findings:
        failure_payload = _codex_failed_coa_mapping_payload(
            event_register,
            error or "Codex CLI did not return a usable corrected TB bridge result.",
            attempt_history,
            validation_findings,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "tb_bridge_correction_failed.json").write_text(json.dumps(failure_payload, indent=2, sort_keys=True))
        _write_turing_correction_round_log(
            output_dir=output_dir,
            correction_round=correction_round,
            review_payload=review_payload,
            attempt_history=attempt_history,
            status="failed",
            error=error or "Codex CLI did not return a usable corrected TB bridge result.",
            validation_findings=validation_findings,
            output_return_code=1,
        )
        print("Codex could not apply Turing corrections.", file=sys.stderr)
        return 1
    final_payload = _normalise_coa_mapping_workpaper(payload, event_register, validation_findings)
    final_payload["codex_attempt_history"] = attempt_history
    final_payload["turing_correction_source"] = {
        "review_status": review_payload.get("status"),
        "correction_briefs": review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else [],
        "review_summary": review_payload.get("summary") if isinstance(review_payload.get("summary"), dict) else {},
    }
    output_return_code = _write_tb_bridge_outputs(
        output_dir=output_dir,
        payload=final_payload,
        event_register=event_register,
        source_index=source_index,
        prior_coa=prior_coa,
        skip_xlsx=bool(getattr(args, "skip_xlsx", False)),
    )
    _write_turing_correction_round_log(
        output_dir=output_dir,
        correction_round=correction_round,
        review_payload=review_payload,
        attempt_history=attempt_history,
        status="applied" if output_return_code == 0 else "output_failed",
        error="" if output_return_code == 0 else "Corrected JSON was produced, but workbook output failed.",
        validation_findings=validation_findings,
        corrected_payload=final_payload,
        output_return_code=output_return_code,
    )
    return output_return_code


def _prepare_workpaper_update_run_context(artifact_dir: Path, *, entity_name: str | None, fy_start: str | None, fy_end: str | None) -> None:
    context = {
        key: value
        for key, value in {
            "entity_name": entity_name,
            "target_fy_start": fy_start,
            "target_fy_end": fy_end,
        }.items()
        if value
    }
    if not context:
        return
    for file_name in [
        "document_inventory.json",
        "source_document_index.json",
        "accounting_facts_by_document.json",
        "source_coverage_continuity.json",
    ]:
        path = artifact_dir / file_name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload.update(context)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _prepare_workpaper_summary(
    *,
    client_folder: Path,
    artifact_dir: Path,
    output_dir: Path,
    step_statuses: dict[str, int],
) -> str:
    workbook_path = output_dir / TB_BRIDGE_XLSX
    tb_json_path = output_dir / TB_BRIDGE_JSON
    review_path = output_dir / "turing_senior_review.md"
    source_index_path = artifact_dir / "source_document_index.json"
    event_register_path = artifact_dir / "accounting_event_register.json"
    lines = ["# Prepared Workpaper Summary", ""]
    lines.extend(
        [
            f"- Client folder: {client_folder}",
            f"- Source index: {source_index_path}",
            f"- Event register: {event_register_path}",
            f"- TB Bridge workbook: {workbook_path}",
            f"- Turing senior review: {review_path}",
            "",
        ]
    )
    lines.append("## Run status")
    for label, code in step_statuses.items():
        state = "completed" if code == 0 else "completed with warnings" if label == "step2_source_index" and source_index_path.exists() else "needs attention"
        lines.append(f"- {label}: {state} (exit {code})")
    if tb_json_path.exists():
        try:
            tb_payload = json.loads(tb_json_path.read_text())
        except json.JSONDecodeError:
            tb_payload = {}
        summary = tb_payload.get("summary") if isinstance(tb_payload.get("summary"), dict) else {}
        findings = tb_payload.get("validation_findings") if isinstance(tb_payload.get("validation_findings"), list) else []
        lines.extend(["", "## Workbook checks"])
        lines.append(f"- Accounts: {summary.get('accounts', 0)}")
        lines.append(f"- Movement columns: {summary.get('movement_columns', 0)}")
        lines.append(f"- Movement notes: {summary.get('movement_notes', 0)}")
        lines.append(f"- Validation findings: {len(findings)}")
        if findings:
            lines.append("")
            lines.append("## Needs attention")
            for finding in findings[:12]:
                if not isinstance(finding, dict):
                    continue
                message = finding.get("message") or finding.get("category") or finding
                lines.append(f"- {message}")
    if review_path.exists():
        try:
            review_payload = json.loads(review_path.with_suffix(".json").read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            review_payload = {}
        review_summary = review_payload.get("summary") if isinstance(review_payload.get("summary"), dict) else {}
        review_findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
        public_review_findings = _public_turing_findings(review_payload) if isinstance(review_payload, dict) else []
        internal_review_notes = max(0, len(review_findings) - len(public_review_findings))
        lines.extend(["", "## Turing senior review"])
        lines.append(f"- Status: {'ready' if _turing_review_is_ready(output_dir) else review_payload.get('status', 'review_created')}")
        lines.append(f"- Sampled items: {review_summary.get('sampled_items', len(review_payload.get('sampled_items', []) if isinstance(review_payload.get('sampled_items'), list) else []))}")
        lines.append(f"- Material findings shown to accountant: {len(public_review_findings)}")
        lines.append(f"- Internal low-risk notes handled by Tessa/Turing: {internal_review_notes}")
        if public_review_findings:
            lines.append("")
            lines.append("## Material review items")
            for finding in public_review_findings[:10]:
                if not isinstance(finding, dict):
                    continue
                lines.append(f"- {finding.get('severity', 'review')} / {finding.get('category', 'judgement')}: {finding.get('message', '')}")
    lines.extend(
        [
            "",
            "## Accountant-facing instruction",
            "Open the TB Bridge workbook first. Use Movement Notes to search important amounts and Evidence Index to open source PDFs.",
            "",
        ]
    )
    return "\n".join(lines)


def _remove_previous_event_register_outputs(artifact_dir: Path) -> None:
    for file_name in [
        "source_fact_matches.md",
        "source_fact_matches.json",
        "accounting_event_register.md",
        "accounting_event_register.json",
        "relationship_reasoning_register.md",
        "relationship_reasoning_register.json",
        "relationship_reasoning_progress.json",
        "relationship_reasoning_attempt_history.json",
    ]:
        path = artifact_dir / file_name
        if path.exists():
                path.unlink()


def _last_good_workpaper_dir(output_dir: Path) -> Path:
    return output_dir / "_last_good"


def _workpaper_promotable_files() -> list[str]:
    return [
        TB_BRIDGE_JSON,
        TB_BRIDGE_MD,
        TB_BRIDGE_XLSX,
        "turing_senior_review.md",
        "turing_senior_review.json",
        f"{TB_BRIDGE_XLSX}.inspect.ndjson",
    ]


def _snapshot_previous_workpaper_outputs(output_dir: Path) -> bool:
    workbook_path = output_dir / TB_BRIDGE_XLSX
    if not workbook_path.exists():
        return False
    snapshot_dir = _last_good_workpaper_dir(output_dir)
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for file_name in _workpaper_promotable_files():
        source = output_dir / file_name
        if source.exists() and source.is_file():
            shutil.copy2(source, snapshot_dir / file_name)
            copied.append(file_name)
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(
            {
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
                "files": copied,
                "reason": "Last valid workbook snapshot before starting a new run.",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return bool(copied)


def _restore_last_good_workpaper_outputs(output_dir: Path, *, reason: str) -> bool:
    snapshot_dir = _last_good_workpaper_dir(output_dir)
    workbook_path = snapshot_dir / TB_BRIDGE_XLSX
    if not workbook_path.exists():
        return False
    restored: list[str] = []
    for file_name in _workpaper_promotable_files():
        source = snapshot_dir / file_name
        if source.exists() and source.is_file():
            shutil.copy2(source, output_dir / file_name)
            restored.append(file_name)
    (output_dir / "last_good_workpaper_restored.json").write_text(
        json.dumps(
            {
                "restored_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "files": restored,
                "snapshot_dir": str(snapshot_dir),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return bool(restored)


def _prepare_workpaper_progress_path(output_dir: Path) -> Path:
    return output_dir / "prepare_workpaper_progress.json"


def _write_prepare_workpaper_progress(
    output_dir: Path,
    *,
    stage: str,
    status: str,
    message: str,
    step_statuses: dict[str, int] | None = None,
    extra: dict | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "status": status,
        "message": message,
        "step_statuses": step_statuses or {},
    }
    if extra:
        payload.update(extra)
    _prepare_workpaper_progress_path(output_dir).write_text(json.dumps(payload, indent=2, sort_keys=True))


def _write_step_progress(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    path.write_text(json.dumps(checkpoint, indent=2, sort_keys=True))


def _write_codex_attempt_history(
    path: Path,
    *,
    stage: str,
    attempts: list[dict],
    status: str,
    message: str,
    extra: dict | None = None,
) -> None:
    payload = {
        "stage": stage,
        "status": status,
        "message": message,
        "attempt_count": len(attempts),
        "last_attempt": attempts[-1] if attempts else {},
        "attempts": attempts,
    }
    if extra:
        payload.update(extra)
    _write_step_progress(path, payload)


def _remove_previous_workpaper_outputs(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    generated_names = {
        TB_BRIDGE_JSON,
        TB_BRIDGE_MD,
        TB_BRIDGE_XLSX,
        "prepared_workpaper_summary.md",
        "turing_senior_review.md",
        "turing_senior_review.json",
        "tb_bridge_correction_failed.json",
        "build_tb_bridge_workpaper.mjs",
        "last_good_workpaper_restored.json",
        "prepare_workpaper_progress.json",
        "tb_bridge_generation_progress.json",
        "tb_bridge_attempt_history.json",
        "turing_review_attempt_history.json",
    }
    generated_patterns = [
        "preview_*.png",
        "*.inspect.ndjson",
        "turing_senior_review_round_*.*",
    ]
    for file_name in generated_names:
        path = output_dir / file_name
        if path.exists():
            path.unlink()
    for pattern in generated_patterns:
        for path in output_dir.glob(pattern):
            if path.is_file():
                path.unlink()


def _prepare_workpaper_command(args: argparse.Namespace) -> int:
    client_folder = Path(args.client_folder).expanduser()
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    codex_timeout = int(getattr(args, "codex_timeout", 1200) or 1200)
    codex_max_attempts = int(getattr(args, "codex_max_attempts", 3) or 3)
    review_correction_rounds = max(0, int(getattr(args, "review_correction_rounds", 2) or 0))
    force_reprocess = bool(getattr(args, "force_reprocess", False)) or not bool(getattr(args, "allow_cache", False))
    if not client_folder.exists() or not client_folder.is_dir():
        print(f"Client folder not found: {client_folder}", file=sys.stderr)
        return 2

    had_last_good_workbook = _snapshot_previous_workpaper_outputs(output_dir)
    _remove_previous_workpaper_outputs(output_dir)
    if had_last_good_workbook:
        print(f"Saved previous valid workbook snapshot -> {_last_good_workpaper_dir(output_dir)}")

    source_index = artifact_dir / "source_document_index.json"
    print(f"Preparing accountant workpaper from: {client_folder}")
    _write_prepare_workpaper_progress(
        output_dir,
        stage="indexing",
        status="running",
        message="Tessa is reading the uploaded files and building the evidence index.",
        extra={
            "client_folder": str(client_folder),
            "last_good_snapshot_available": had_last_good_workbook,
        },
    )
    print("Step 1/3: indexing source documents with Codex CLI")
    step_statuses: dict[str, int] = {}
    try:
        step_statuses["step2_source_index"] = _process_documents_command(
            argparse.Namespace(
                input_dir=str(client_folder),
                artifact_dir=str(artifact_dir),
                codex_command=codex_command,
                codex_timeout=codex_timeout,
                codex_max_attempts=codex_max_attempts,
                batch_size=int(getattr(args, "batch_size", 5) or 5),
                force_reprocess=force_reprocess,
            )
        )
    except Exception as exc:  # noqa: BLE001 - product runner must checkpoint unexpected failures.
        traceback.print_exc()
        step_statuses["step2_source_index"] = 1
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Source indexing crashed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="indexing",
            status="failed",
            message=(
                "Tessa could not finish reading the uploaded files. Previous valid workbook was restored."
                if restored_last_good
                else "Tessa could not finish reading the uploaded files. No refreshed workbook was produced."
            ),
            step_statuses=step_statuses,
            extra={
                "error_type": type(exc).__name__,
                "last_good_restored": restored_last_good,
                "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
            },
        )
        return 1
    _write_prepare_workpaper_progress(
        output_dir,
        stage="indexing",
        status="complete" if step_statuses["step2_source_index"] == 0 else "needs_attention",
        message="Evidence index completed." if step_statuses["step2_source_index"] == 0 else "Evidence index completed with documents needing attention.",
        step_statuses=step_statuses,
        extra={"source_index_path": str(source_index)},
    )
    _prepare_workpaper_update_run_context(
        artifact_dir,
        entity_name=getattr(args, "entity_name", None),
        fy_start=getattr(args, "fy_start", None),
        fy_end=getattr(args, "fy_end", None),
    )
    accounting_facts = artifact_dir / "accounting_facts_by_document.json"
    source_coverage = artifact_dir / "source_coverage_continuity.json"
    if not source_index.exists() or not accounting_facts.exists():
        print("Source index was not created, so the workpaper cannot continue.", file=sys.stderr)
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Source indexing failed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="indexing",
            status="failed",
            message=(
                "Source index was not created. Previous valid workbook was restored."
                if restored_last_good
                else "Source index was not created, so the workpaper cannot continue."
            ),
            step_statuses=step_statuses,
            extra={"last_good_restored": restored_last_good, "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists()},
        )
        return 1

    print("Step 2/3: building accounting event register with Codex CLI")
    _write_prepare_workpaper_progress(
        output_dir,
        stage="relationships",
        status="running",
        message="Tessa is investigating relationships between prior-year balances, bank movements and source documents.",
        step_statuses=step_statuses,
    )
    _remove_previous_event_register_outputs(artifact_dir)
    try:
        step_statuses["step3_event_register"] = _match_source_facts_command(
            argparse.Namespace(
                accounting_facts=str(accounting_facts),
                source_coverage=str(source_coverage) if source_coverage.exists() else None,
                codex_command=codex_command,
                codex_timeout=codex_timeout,
                codex_max_attempts=codex_max_attempts,
                bank_transactions=None,
                invoice_facts=None,
                distribution_tax_facts=None,
                broker_trade_facts=None,
                output=str(artifact_dir / "source_fact_matches.md"),
            )
        )
    except Exception as exc:  # noqa: BLE001 - product runner must checkpoint unexpected failures.
        traceback.print_exc()
        step_statuses["step3_event_register"] = 1
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Relationship reasoning crashed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="relationships",
            status="failed",
            message=f"Movement reasoning failed with a product error: {exc}",
            step_statuses=step_statuses,
            extra={
                "error_type": type(exc).__name__,
                "last_good_restored": restored_last_good,
                "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
            },
        )
        return 1
    _write_prepare_workpaper_progress(
        output_dir,
        stage="relationships",
        status="complete" if step_statuses["step3_event_register"] == 0 else "failed",
        message="Accounting event register completed." if step_statuses["step3_event_register"] == 0 else "Accounting event register needs engineering attention.",
        step_statuses=step_statuses,
        extra={"event_register_path": str(artifact_dir / "accounting_event_register.json")},
    )
    event_register = artifact_dir / "accounting_event_register.json"
    if not event_register.exists():
        print("Accounting event register was not created, so the workpaper cannot continue.", file=sys.stderr)
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Relationship reasoning failed before a refreshed workbook was produced.")
        summary = _prepare_workpaper_summary(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            step_statuses=step_statuses,
        )
        summary_path = output_dir / "prepared_workpaper_summary.md"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
        print(f"Prepared workpaper summary -> {summary_path}")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="relationships",
            status="failed",
            message=(
                "Accounting event register was not created. Previous valid workbook was restored."
                if restored_last_good
                else "Accounting event register was not created, so the workpaper cannot continue."
            ),
            step_statuses=step_statuses,
            extra={"summary_path": str(summary_path), "last_good_restored": restored_last_good, "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists()},
        )
        return 1
    try:
        event_payload = json.loads(event_register.read_text())
    except json.JSONDecodeError:
        event_payload = {}
    if isinstance(event_payload, dict) and event_payload.get("status") == "codex_failed":
        print("Codex could not create a usable accounting event register.", file=sys.stderr)
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Relationship reasoning returned an unusable register before a refreshed workbook was produced.")
        summary = _prepare_workpaper_summary(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            step_statuses=step_statuses,
        )
        summary_path = output_dir / "prepared_workpaper_summary.md"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
        print(f"Prepared workpaper summary -> {summary_path}")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="relationships",
            status="failed",
            message=(
                "AI could not create a usable accounting event register. Previous valid workbook was restored."
                if restored_last_good
                else "AI could not create a usable accounting event register."
            ),
            step_statuses=step_statuses,
            extra={"summary_path": str(summary_path), "last_good_restored": restored_last_good, "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists()},
        )
        return 1

    print("Step 3/3: building TB Bridge workbook with Codex CLI")
    _write_prepare_workpaper_progress(
        output_dir,
        stage="bridge",
        status="running",
        message="Tessa is preparing the TB bridge workbook and movement notes.",
        step_statuses=step_statuses,
    )
    try:
        step_statuses["step4_tb_bridge_workbook"] = _build_coa_mapping_workpaper_command(
            argparse.Namespace(
                artifact_dir=str(artifact_dir),
                output_dir=str(output_dir),
                event_register=None,
                source_index=None,
                prior_coa=None,
                prior_fs_document_id=getattr(args, "prior_fs_document_id", None),
                prior_fs_file=getattr(args, "prior_fs_file", None),
                codex_command=codex_command,
                codex_timeout=codex_timeout,
                codex_max_attempts=codex_max_attempts,
                skip_xlsx=bool(getattr(args, "skip_xlsx", False)),
            )
        )
    except Exception as exc:  # noqa: BLE001 - product runner must checkpoint unexpected failures.
        traceback.print_exc()
        step_statuses["step4_tb_bridge_workbook"] = 1
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="TB bridge workbook stage crashed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="bridge",
            status="failed",
            message=f"TB bridge workbook stage failed with a product error: {exc}",
            step_statuses=step_statuses,
            extra={
                "error_type": type(exc).__name__,
                "last_good_restored": restored_last_good,
                "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
            },
        )
        return 1
    workbook_path = output_dir / TB_BRIDGE_XLSX
    if step_statuses["step4_tb_bridge_workbook"] != 0:
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Step 4 failed before a refreshed workbook was produced.")
        summary = _prepare_workpaper_summary(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            step_statuses=step_statuses,
        )
        summary_path = output_dir / "prepared_workpaper_summary.md"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
        print(f"Prepared workpaper summary -> {summary_path}")
        if restored_last_good:
            print(f"Restored previous valid workbook -> {workbook_path}")
        print("TB Bridge workbook was not refreshed because Step 4 needs attention.", file=sys.stderr)
        _write_prepare_workpaper_progress(
            output_dir,
            stage="bridge",
            status="failed",
            message=(
                "TB bridge workbook needs engineering attention. Previous valid workbook was restored."
                if restored_last_good
                else "TB bridge workbook needs engineering attention."
            ),
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
                "last_good_restored": restored_last_good,
            },
        )
        return 1

    review_required = not bool(getattr(args, "skip_review", False))
    if review_required and (output_dir / TB_BRIDGE_JSON).exists():
        print("Senior review: Turing is checking controls and sampling source evidence with Codex CLI")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="turing",
            status="running",
            message="Senior review is checking arithmetic, workbook structure and sample evidence.",
            step_statuses=step_statuses,
            extra={"workbook_path": str(workbook_path), "workbook_exists": workbook_path.exists()},
        )
        review_args = argparse.Namespace(
            client_folder=str(client_folder),
            artifact_dir=str(artifact_dir),
            output_dir=str(output_dir),
            workpaper_json=None,
            source_index=None,
            event_register=None,
            prior_coa=None,
            output=None,
            entity_name=getattr(args, "entity_name", None),
            codex_command=codex_command,
            codex_timeout=codex_timeout,
            codex_max_attempts=codex_max_attempts,
            sample_size=int(getattr(args, "review_sample_size", 8) or 8),
        )
        step_statuses["turing_senior_review_round_1"] = _review_workpaper_command(review_args)
        final_review_round = 1
        for correction_round in range(1, review_correction_rounds + 1):
            if step_statuses.get(f"turing_senior_review_round_{final_review_round}") != 0:
                break
            if not _turing_review_needs_corrections(output_dir):
                break
            _archive_turing_review_round(output_dir, final_review_round)
            print(f"Senior review correction round {correction_round}: Codex is applying Turing correction briefs")
            _write_prepare_workpaper_progress(
                output_dir,
                stage="correction",
                status="running",
                message=f"Tessa is applying senior review correction round {correction_round}.",
                step_statuses=step_statuses,
                extra={"correction_round": correction_round},
            )
            step_statuses[f"turing_correction_round_{correction_round}"] = _apply_turing_corrections_command(
                argparse.Namespace(
                    artifact_dir=str(artifact_dir),
                    output_dir=str(output_dir),
                    workpaper_json=None,
                    review_json=None,
                    source_index=None,
                    event_register=None,
                    prior_coa=None,
                    codex_command=codex_command,
                    codex_timeout=codex_timeout,
                    codex_max_attempts=codex_max_attempts,
                    correction_round=correction_round,
                    skip_xlsx=bool(getattr(args, "skip_xlsx", False)),
                )
            )
            if step_statuses[f"turing_correction_round_{correction_round}"] != 0:
                break
            print(f"Senior review recheck round {correction_round}: Turing is rechecking the corrected workbook")
            _write_prepare_workpaper_progress(
                output_dir,
                stage="turing",
                status="running",
                message=f"Senior review is rechecking correction round {correction_round}.",
                step_statuses=step_statuses,
                extra={"correction_round": correction_round},
            )
            final_review_round += 1
            step_statuses[f"turing_senior_review_round_{final_review_round}"] = _review_workpaper_command(review_args)
        if "turing_senior_review_round_1" in step_statuses:
            step_statuses["turing_senior_review"] = step_statuses[f"turing_senior_review_round_{final_review_round}"]
    summary = _prepare_workpaper_summary(
        client_folder=client_folder,
        artifact_dir=artifact_dir,
        output_dir=output_dir,
        step_statuses=step_statuses,
    )
    summary_path = output_dir / "prepared_workpaper_summary.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary)
    print(f"Prepared workpaper summary -> {summary_path}")
    current_run_ok = workbook_path.exists() and step_statuses.get("step4_tb_bridge_workbook") == 0 and (
        not review_required or (step_statuses.get("turing_senior_review") == 0 and _turing_review_is_ready(output_dir))
    )
    if current_run_ok:
        print(f"Workbook ready -> {workbook_path}")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="completed",
            status="completed",
            message="Workbook ready. Senior review passed.",
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
            },
        )
        return 0
    if review_required and workbook_path.exists() and step_statuses.get("turing_senior_review") == 0 and not _turing_review_is_ready(output_dir):
        print(f"Workbook was created but Turing still needs corrections after {review_correction_rounds} correction round(s): {workbook_path}", file=sys.stderr)
        _write_prepare_workpaper_progress(
            output_dir,
            stage="turing",
            status="needs_attention",
            message=f"Workbook was created, but senior review still has correction notes after {review_correction_rounds} correction round(s).",
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
            },
        )
        return 0
    if workbook_path.exists():
        print(f"Workbook was created but the current run needs attention: {workbook_path}", file=sys.stderr)
        _write_prepare_workpaper_progress(
            output_dir,
            stage="bridge",
            status="needs_attention",
            message="Workbook was created, but the current run has judgement or review items.",
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
            },
        )
        return 0
    print(f"Workbook was not created: {workbook_path}", file=sys.stderr)
    _write_prepare_workpaper_progress(
        output_dir,
        stage="bridge",
        status="failed",
        message="Workbook was not created.",
        step_statuses=step_statuses,
        extra={"summary_path": str(summary_path), "workbook_path": str(workbook_path), "workbook_exists": False},
    )
    return 1


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


def _serve_workpaper_portal_command(args: argparse.Namespace) -> int:
    from accountant_copilot.workpaper_portal import serve_workpaper_portal

    serve_workpaper_portal(repo_root=Path.cwd(), host=args.host, port=args.port)
    return 0


def _parse_bank_statement_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_amount(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    try:
        amount = float(cleaned)
    except ValueError:
        return str(value)
    if negative:
        amount = -amount
    return f"{amount:.2f}"


def _pdf_password_candidates_from_filename(path: Path) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for match in re.finditer(r"\d{4,16}", path.stem):
        candidate = match.group(0)
        if candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)
    return candidates


def _extract_pdf_page_quotes(path: Path) -> list[tuple[int, str]]:
    """Extract text quotes from a text-based PDF, one quote per page.

    PyMuPDF is preferred when installed. A `pdftotext` fallback keeps local
    development usable without making scanned/OCR documents appear verified.
    Empty pages intentionally return no evidence so the document remains gated.
    """
    password_candidates = _pdf_password_candidates_from_filename(path)
    try:
        import fitz  # type: ignore[import-not-found]

        pages: list[tuple[int, str]] = []
        with fitz.open(path) as doc:
            if getattr(doc, "needs_pass", False):
                authenticated = any(doc.authenticate(password) for password in password_candidates)
                if not authenticated:
                    return []
            for index, page in enumerate(doc, start=1):
                text = " ".join(page.get_text("text").split())
                if text:
                    pages.append((index, text[:_PDF_PAGE_QUOTE_CHAR_LIMIT]))
        return pages
    except Exception:
        try:
            commands = [["pdftotext", "-layout", str(path), "-"]]
            commands.extend(["pdftotext", "-upw", password, "-layout", str(path), "-"] for password in password_candidates)
            result = None
            for command in commands:
                candidate_result = subprocess.run(command, text=True, capture_output=True, check=False)
                if candidate_result.returncode == 0 and candidate_result.stdout.strip():
                    result = candidate_result
                    break
        except FileNotFoundError:
            return []
        if result is None:
            return []
        if result.returncode != 0 or not result.stdout.strip():
            return []
        pages = []
        for index, text in enumerate(result.stdout.split("\f"), start=1):
            quote = " ".join(text.split())
            if quote:
                pages.append((index, quote[:_PDF_PAGE_QUOTE_CHAR_LIMIT]))
        return pages


def _extract_image_ocr_quote(path: Path) -> str | None:
    """Extract text from an image using local Tesseract when available.

    OCR output is treated as evidence with OCR confidence, not as approved
    accounting treatment. If Tesseract is unavailable or no text is produced,
    return None so the source remains gated.
    """
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout", "--psm", "6"],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    quote = " ".join(result.stdout.split())
    return quote[:1000] if quote else None


def _xml_text(element: ET.Element) -> str:
    values: list[str] = []
    for child in element.iter():
        if child.text:
            values.append(child.text)
    return " ".join(" ".join(values).split())


def _extract_docx_quote(path: Path) -> str | None:
    """Extract readable text from a modern Word document without extra deps."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name == "word/document.xml"
                or re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)
            ]
            chunks: list[str] = []
            for name in names:
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                text = _xml_text(root)
                if text:
                    chunks.append(text)
    except (OSError, zipfile.BadZipFile):
        return None
    quote = " ".join(chunks).strip()
    return quote[:_PDF_PAGE_QUOTE_CHAR_LIMIT] if quote else None


def _extract_xlsx_quote(path: Path) -> str | None:
    """Extract cell text from a modern Excel workbook without extra deps."""
    try:
        with zipfile.ZipFile(path) as archive:
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                try:
                    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                    for item in root.iter():
                        if item.tag.endswith("}si") or item.tag == "si":
                            shared_strings.append(_xml_text(item))
                except ET.ParseError:
                    shared_strings = []
            sheet_names = sorted(name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name))
            rows: list[str] = []
            for sheet_index, sheet_name in enumerate(sheet_names, start=1):
                try:
                    root = ET.fromstring(archive.read(sheet_name))
                except ET.ParseError:
                    continue
                rows.append(f"Sheet {sheet_index}")
                for row in root.iter():
                    if not (row.tag.endswith("}row") or row.tag == "row"):
                        continue
                    values: list[str] = []
                    for cell in row:
                        if not (cell.tag.endswith("}c") or cell.tag == "c"):
                            continue
                        cell_type = cell.attrib.get("t", "")
                        value = ""
                        if cell_type == "inlineStr":
                            value = _xml_text(cell)
                        else:
                            raw = ""
                            for child in cell:
                                if child.tag.endswith("}v") or child.tag == "v":
                                    raw = child.text or ""
                                    break
                            if cell_type == "s" and raw.isdigit():
                                value = shared_strings[int(raw)] if int(raw) < len(shared_strings) else raw
                            else:
                                value = raw
                        if value:
                            values.append(value)
                    if values:
                        rows.append(" | ".join(values))
                    if len("\n".join(rows)) > 60000:
                        break
                if len("\n".join(rows)) > 60000:
                    break
    except (OSError, zipfile.BadZipFile):
        return None
    quote = "\n".join(rows).strip()
    return quote[:60000] if quote else None


def _classify_raw_document(path: Path) -> str:
    name = path.name.lower()
    if path.suffix.lower() == ".md":
        return "client_conventions"
    if path.suffix.lower() == ".csv":
        return "supporting_csv"
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        return "image_support"
    if path.suffix.lower() in {".docx", ".docm", ".xlsx", ".xlsm", ".xls"}:
        if "trial balance" in name or re.search(r"\btb\b", name):
            return "trial_balance"
        return "source_document"
    if "estatement" in name or (path.suffix.lower() == ".pdf" and len(path.stem) == 36 and path.stem.count("-") == 4):
        return "bank_statement"
    if "trial balance" in name or re.search(r"\btb\b", name):
        return "trial_balance"
    if "invoice" in name or "tax invoice" in name:
        return "invoice"
    if any(token in name for token in ["capital call", "drawdown", "contribution notice"]):
        return "capital_call"
    if "financial statement" in name or "fy24" in name:
        return "prior_year_financial_statements"
    if any(token in name for token in ["distribution", "tax statement", "payment_advice", "annual statement"]):
        return "investment_statement"
    if "confirmation" in name or "sell" in name:
        return "broker_confirmation"
    return "source_document"


_UUID_STEM_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_GENERIC_FILE_STEM_RE = re.compile(r"^(?:img[_ -]?\d+|scan[_ -]?\d*|document[_ -]?\d*|untitled|unknown|file[_ -]?\d*)$", re.IGNORECASE)
_KNOWN_DOCUMENT_TYPES = {
    "bank_statement",
    "broker_confirmation",
    "capital_call",
    "client_conventions",
    "image_support",
    "investment_statement",
    "invoice",
    "prior_year_financial_statements",
    "source_document",
    "supporting_csv",
    "trial_balance",
}


def _is_ambiguous_file_name(path: Path) -> bool:
    stem = path.stem.strip()
    if _UUID_STEM_RE.fullmatch(stem) or _GENERIC_FILE_STEM_RE.fullmatch(stem):
        return True
    compact = re.sub(r"[^A-Za-z0-9]", "", stem)
    return len(compact) >= 20 and bool(re.fullmatch(r"[A-Fa-f0-9]+", compact))


def _classify_raw_document_from_content(path: Path, current_type: str, quote: str) -> str:
    if path.suffix.lower() in {".md", ".csv", ".png", ".jpg", ".jpeg"}:
        return current_type
    text = quote.lower()
    if "sell confirmation" in text or "buy confirmation" in text or "settlement amount" in text:
        return "broker_confirmation"
    if "trial balance" in text or ("account code" in text and "debit" in text and "credit" in text):
        return "trial_balance"
    if "tax invoice" in text or "invoice number" in text or "amount due" in text:
        return "invoice"
    if "capital call" in text or "drawdown notice" in text or "contribution notice" in text:
        return "capital_call"
    if "financial statements" in text or "statement of financial position" in text:
        return "prior_year_financial_statements"
    if "statement period" in text and "closing balance" in text:
        return "bank_statement"
    if any(token in text for token in ["distribution", "payment advice", "tax statement", "franking credit", "withholding"]):
        return "investment_statement"
    return current_type if not _is_ambiguous_file_name(path) else "source_document"


def _sanitize_document_name(value: str, suffix: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " - ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    cleaned = cleaned[:140].strip(" .-_") or "Source Document"
    extension = suffix.lower() or ".pdf"
    return cleaned if cleaned.lower().endswith(extension) else f"{cleaned}{extension}"


def _extract_json_object(text: str) -> dict | None:
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None


def _read_json_object_file(path: Path | None) -> tuple[dict | None, str | None]:
    if path is None or not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return None, f"Codex wrote invalid JSON sidecar {path}: {exc}"
    except OSError as exc:
        return None, f"Could not read Codex JSON sidecar {path}: {exc}"
    if not isinstance(payload, dict):
        return None, f"Codex JSON sidecar was not an object: {path}"
    return payload, None


_CODEX_DOCUMENT_PROCESSING_CACHE_VERSION = "v10_source_index"
DOCUMENT_SOURCE_INDEX_CONTRACT = {
    "purpose": "Create a lightweight source index for human orientation and later Codex CLI investigation. Step 2 must not extract detailed accounting facts.",
    "global_rules": [
        "Summarise what the document is, not every accounting row inside it.",
        "Suggest a clear display name that preserves the original file extension.",
        "Classify the document type only at document level.",
        "Capture entity relevance and obvious wrong-entity/personal-document concerns.",
        "Capture only high-level visible signals such as period/date, named parties, identifiers, and a few prominent headline amounts.",
        "Do not extract bank transactions, invoice line items, distribution rows, broker trade rows, statement balances, trial balance rows, financial statement line items, or tax component rows.",
        "Do not map to chart of accounts and do not propose journals.",
        "Detailed accounting event extraction, cash matching, CoA mapping, journals, trial balance and financial statement work happen in later Codex CLI steps.",
    ],
    "document_types": [
        "bank_statement",
        "invoice",
        "distribution_tax",
        "broker_trade",
        "trial_balance",
        "prior_year_financial_statements",
        "investment_statement",
        "capital_call",
        "client_conventions",
        "source_document",
        "other",
    ],
}


def _normalise_codex_cli_command(command: str) -> str:
    command = str(command or "").strip()
    return "codex exec" if command == "codex" else command or "codex exec"


def _document_text_for_codex(path: Path) -> tuple[list[dict[str, str]], str]:
    pages: list[dict[str, str]] = []
    suffix = path.suffix.lower()
    if suffix == ".md":
        pages.append({"page": "1", "evidence_id": "text_001", "quote": path.read_text(errors="ignore")})
    elif suffix == ".pdf":
        for page_number, quote in _extract_pdf_page_quotes(path):
            pages.append({"page": str(page_number), "evidence_id": f"page_{page_number:03d}", "quote": quote})
    elif suffix in {".png", ".jpg", ".jpeg"}:
        quote = _extract_image_ocr_quote(path)
        if quote:
            pages.append({"page": "1", "evidence_id": "page_001", "quote": quote})
    elif suffix in {".docx", ".docm"}:
        quote = _extract_docx_quote(path)
        if quote:
            pages.append({"page": "1", "evidence_id": "text_001", "quote": quote})
    elif suffix in {".xlsx", ".xlsm"}:
        quote = _extract_xlsx_quote(path)
        if quote:
            pages.append({"page": "1", "evidence_id": "sheet_text_001", "quote": quote})
    if not pages and suffix in {".txt", ".csv", ".json"}:
        pages.append({"page": "1", "evidence_id": "text_001", "quote": path.read_text(errors="ignore")})
    text = "\n\n".join(f"[{item['evidence_id']}]\n{item['quote']}" for item in pages)
    return pages, text[:60000]


def _codex_process_document_prompt(path: Path, document_id: str, source_hash: str, *, recovery_attempt: int = 0, previous_error: str | None = None) -> str:
    pages, extracted_text = _document_text_for_codex(path)
    recovery_context = {
        "recovery_attempt": recovery_attempt,
        "previous_error": previous_error or "",
        "instruction": (
            "A previous Codex attempt failed. Act like a senior accountant recovering the work: "
            "diagnose the failure, change indexing strategy, keep the response compact, and return valid JSON only. "
            "For timeout or large-document failures, prioritize the title/header, entity holder, period/date, document type, and a short summary. "
            "For invalid-output failures, focus on returning the required JSON schema."
        ),
    }
    return json.dumps(
        {
            "task": "Read one source document and create a concise source index entry. Return only JSON." if recovery_attempt == 0 else "Recover a failed source-indexing attempt. Return only JSON.",
            "recovery_context": recovery_context if recovery_attempt else None,
            "source_index_contract": DOCUMENT_SOURCE_INDEX_CONTRACT,
            "required_output_schema": {
                "display_name": "Human review name, e.g. 2024-12-31 - Commonwealth Bank Statement - Account 027.pdf",
                "document_type": "bank_statement|invoice|distribution_tax|broker_trade|trial_balance|prior_year_financial_statements|investment_statement|capital_call|client_conventions|source_document|other",
                "naming_confidence": "low|medium|high",
                "naming_evidence_refs": ["evidence ids supporting the display name"],
                "status": "indexed|needs_review",
                "document_summary": "One or two plain-English sentences explaining what this file appears to be.",
                "entity_relevance": "relevant|possible_personal|wrong_entity|unclear|non_accounting",
                "entity_relevance_reason": "Short reason for the relevance label.",
                "period_start": "YYYY-MM-DD if visible or blank",
                "period_end": "YYYY-MM-DD if visible or blank",
                "statement_date": "YYYY-MM-DD if visible or blank",
                "key_parties": ["visible names of banks, investors, suppliers, customers, brokers, funds, trustees, or recipients"],
                "key_identifiers": ["visible account numbers, investor numbers, invoice numbers, statement numbers, security codes, or payment references"],
                "primary_amounts": [
                    {
                        "label": "headline amount label, e.g. closing balance, market value, amount due",
                        "amount": "decimal string or visible value",
                        "currency": "currency if visible",
                        "evidence_id": "supporting evidence id if visible",
                    }
                ],
                "review_flags": ["short warnings such as possible personal holder, wrong entity, password protected, scanned/low confidence, unclear period"],
            },
            "rules": [
                "Do not invent bank names, account numbers, dates, amounts, counterparties, or tax labels.",
                "For bank statements, include bank name and account identifier in display_name when visible.",
                "Do not extract accounting facts in Step 2. No bank transaction rows, distribution rows, invoice line items, broker trade rows, trial balance rows, or financial statement line balances.",
                "Use primary_amounts only for a small number of headline amounts that help the user recognise the document.",
                "Use document_summary to explain what the document is about in normal accounting language.",
                "Use review_flags for document-level concerns only, not accounting event matching conclusions.",
                "Preserve the original file extension in display_name.",
                "Return a single JSON object and no markdown.",
            ],
            "document": {
                "document_id": document_id,
                "file_name": path.name,
                "file_path": str(path),
                "source_hash": source_hash,
                "deterministic_document_type": _classify_raw_document(path),
                "page_quotes": pages,
                "extracted_text": extracted_text,
            },
        },
        indent=2,
        sort_keys=True,
    )


def _codex_process_document(path: Path, document_id: str, source_hash: str, command: str, timeout: int, *, recovery_attempt: int = 0, previous_error: str | None = None) -> tuple[dict | None, str | None]:
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_DOCUMENT_JSON")
    if fake_payload:
        payload = _extract_json_object(fake_payload)
        return payload, None if payload is not None else "Fake Codex document payload was not valid JSON."
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_codex_process_document_prompt(path, document_id, source_hash, recovery_attempt=recovery_attempt, previous_error=previous_error),
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


def _capital_call_payment_instruction_visible(text: str) -> bool:
    lowered = text.lower()
    has_payment_heading = any(token in lowered for token in ["eft", "bpay", "payment reference", "payment due", "payable", "payment instructions"])
    has_bank_detail = bool(re.search(r"\b(bank|bsb|account name|account number|westpac|commbank|commonwealth|anz|nab)\b", lowered))
    return has_payment_heading and has_bank_detail


def _capital_call_fact_missing_payment_instruction(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    for fact in payload.get("accounting_facts", []) if isinstance(payload.get("accounting_facts"), list) else []:
        if not isinstance(fact, dict) or fact.get("fact_type") != "capital_call":
            continue
        fields = fact.get("fields") if isinstance(fact.get("fields"), dict) else {}
        if not fields.get("called_amount") and not fields.get("amount_due"):
            continue
        if not fields.get("bank_account") and not fields.get("bank_name"):
            return True
    return False


def _codex_document_validation_error(path: Path, payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    pages, extracted_text = _document_text_for_codex(path)
    if _capital_call_payment_instruction_visible(extracted_text) and _capital_call_fact_missing_payment_instruction(payload):
        evidence_ids = ", ".join(page.get("evidence_id", "") for page in pages if page.get("evidence_id"))
        return (
            "Capital call payment instructions are visible in the document text, but the capital_call fact omitted bank_account/bank_name. "
            "Re-read the EFT/BPAY/payment instruction section and extract the receiving bank/account/BSB plus payment reference when visible. "
            f"Relevant evidence ids: {evidence_ids}."
        )
    return None


def _normalise_codex_document_result(path: Path, document_id: str, source_hash: str, payload: dict | None) -> dict:
    payload = payload or {}
    document_type = str(payload.get("document_type") or _classify_raw_document(path))
    if document_type not in _KNOWN_DOCUMENT_TYPES:
        document_type = "source_document" if document_type == "supporting_document" else "other"
    display_name = _sanitize_document_name(str(payload.get("display_name") or path.name), path.suffix)
    document_summary = str(payload.get("document_summary") or payload.get("summary") or "").strip()
    if not document_summary:
        document_summary = "Source indexed for Step 3 Codex investigation."
    no_fact_reason = "Step 2 indexes documents only. Step 3 extracts accounting events from source PDFs/page quotes."
    payload_status = str(payload.get("status") or "")
    if payload_status in {"needs_review", "processing_failed"}:
        status = payload_status
    else:
        status = "indexed"
    return {
        "document_id": document_id,
        "file_path": str(path),
        "file_name": path.name,
        "original_file_name": path.name,
        "display_name": display_name,
        "document_type": document_type,
        "source_hash": source_hash,
        "naming_status": "suggested" if display_name != path.name else "not_suggested",
        "naming_confidence": str(payload.get("naming_confidence") or payload.get("confidence") or ("high" if display_name != path.name else "")),
        "naming_method": "codex_cli",
        "naming_evidence_refs": payload.get("naming_evidence_refs") if isinstance(payload.get("naming_evidence_refs"), list) else [],
        "status": status,
        "document_summary": document_summary,
        "entity_relevance": str(payload.get("entity_relevance") or ""),
        "entity_relevance_reason": str(payload.get("entity_relevance_reason") or ""),
        "period_start": str(payload.get("period_start") or ""),
        "period_end": str(payload.get("period_end") or ""),
        "statement_date": str(payload.get("statement_date") or ""),
        "key_parties": payload.get("key_parties") if isinstance(payload.get("key_parties"), list) else [],
        "key_identifiers": payload.get("key_identifiers") if isinstance(payload.get("key_identifiers"), list) else [],
        "primary_amounts": payload.get("primary_amounts") if isinstance(payload.get("primary_amounts"), list) else [],
        "review_flags": payload.get("review_flags") if isinstance(payload.get("review_flags"), list) else [],
        "no_fact_reason": no_fact_reason,
    }


def _write_document_processing_progress(
    progress_path: Path,
    *,
    processed: int,
    total: int,
    current_document: str,
    status: str,
    cache_hits: int,
    facts: int,
    failures: int = 0,
    codex_attempts: int = 0,
    codex_successes: int = 0,
    batch_size: int = 1,
    current_batch: int = 0,
    total_batches: int = 0,
) -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(
        json.dumps(
            {
                "processed_items": processed,
                "total_items": total,
                "current_document": current_document,
                "status": status,
                "cache_hits": cache_hits,
                "batch_size": batch_size,
                "current_batch": current_batch,
                "total_batches": total_batches,
                "codex_attempts": codex_attempts,
                "codex_successes": codex_successes,
                "source_signals": facts,
                "facts_extracted": 0,
                "failed_items": failures,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _format_processed_document_inventory(payload: dict) -> str:
    lines = [f"# Source Document Index — {payload.get('entity_name', 'Uploaded documents')}", ""]
    for document in payload.get("documents", []):
        summary = str(document.get("document_summary") or "").strip()
        relevance = str(document.get("entity_relevance") or "").strip()
        period = " to ".join(part for part in [str(document.get("period_start") or ""), str(document.get("period_end") or "")] if part)
        lines.extend(
            [
                f"## {document.get('document_id')} — {document.get('display_name')}",
                f"- Original file name: {document.get('original_file_name')}",
                f"- Type: {document.get('document_type')}",
                f"- Status: {document.get('status')}",
                f"- Summary: {summary}",
                f"- Entity relevance: {relevance or 'not assessed'}",
                f"- Period: {period or document.get('statement_date') or ''}",
                "",
            ]
        )
        if document.get("review_flags"):
            lines.extend(["- Review flags: " + "; ".join(str(item) for item in document.get("review_flags", [])), ""])
    return "\n".join(lines).rstrip() + "\n"


def _build_source_document_index_from_processed(processed_payload: dict) -> dict:
    documents = processed_payload.get("documents", []) if isinstance(processed_payload, dict) else []
    return {
        "inventory_id": processed_payload.get("inventory_id", "processed_documents"),
        "entity_name": processed_payload.get("entity_name", "Uploaded documents"),
        "artifact_type": "source_document_index",
        "source_index_contract_version": _CODEX_DOCUMENT_PROCESSING_CACHE_VERSION,
        "documents": documents,
        "summary": {
            "uploaded_documents": len(documents),
            "indexed_documents": sum(1 for document in documents if isinstance(document, dict) and document.get("status") == "indexed"),
            "documents_needing_review": sum(1 for document in documents if isinstance(document, dict) and document.get("status") == "needs_review"),
            "failed_documents": sum(1 for document in documents if isinstance(document, dict) and document.get("status") == "processing_failed"),
            "documents_with_review_flags": sum(1 for document in documents if isinstance(document, dict) and document.get("review_flags")),
        },
    }


def _build_accounting_facts_by_document_from_processed(processed_payload: dict) -> dict:
    payload = _build_source_document_index_from_processed(processed_payload)
    payload["fact_type"] = "source_document_index"
    payload["summary"] = {
        **payload["summary"],
        "documents_with_facts": 0,
        "accounting_fact_rows": 0,
        "documents_without_facts": payload["summary"]["uploaded_documents"],
    }
    for document in payload["documents"]:
        if isinstance(document, dict):
            document.pop("accounting_facts", None)
    return payload


def _fact_fields(fact: dict) -> dict:
    return fact.get("fields", {}) if isinstance(fact.get("fields"), dict) else {}


def _fact_reference(document: dict, fact: dict, index: int) -> str:
    document_id = str(document.get("document_id") or "doc")
    return str(fact.get("fact_id") or f"{document_id}_fact_{index:03d}")


def _account_key_from_bank_balance(fields: dict) -> str:
    return str(fields.get("account_identifier") or fields.get("masked_account_number") or fields.get("account_name") or "unknown_bank_account")


def _source_coverage_facts(facts_payload: dict) -> list[dict]:
    rows: list[dict] = []
    for document in facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []:
        if not isinstance(document, dict):
            continue
        for index, fact in enumerate(document.get("accounting_facts", []) or [], start=1):
            if not isinstance(fact, dict):
                continue
            fields = _fact_fields(fact)
            rows.append(
                {
                    "fact_ref": _fact_reference(document, fact, index),
                    "document_id": document.get("document_id"),
                    "document": document.get("display_name") or document.get("file_name") or document.get("file_path"),
                    "file_path": document.get("file_path"),
                    "original_file_name": document.get("original_file_name") or document.get("file_name"),
                    "document_type": document.get("document_type"),
                    "fact_type": fact.get("fact_type"),
                    "evidence_id": fact.get("evidence_id"),
                    "page": fact.get("page"),
                    "snippet": fact.get("snippet"),
                    "fields": fields,
                }
            )
    return rows


def _normalised_amount_for_compare(value: str | None) -> str | None:
    if value in {None, ""}:
        return None
    cleaned = _clean_money_amount(str(value)) or ""
    comparable = re.sub(r"[^0-9.-]", "", cleaned)
    return comparable or None


def _build_source_coverage_continuity_payload(facts_payload: dict) -> dict:
    facts = _source_coverage_facts(facts_payload)
    findings: list[dict] = []
    document_type_counts: dict[str, int] = {}
    fact_type_counts: dict[str, int] = {}
    for document in facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []:
        if not isinstance(document, dict):
            continue
        document_type = str(document.get("document_type") or "unknown")
        document_type_counts[document_type] = document_type_counts.get(document_type, 0) + 1
    for fact in facts:
        fact_type_counts[str(fact.get("fact_type") or "unknown")] = fact_type_counts.get(str(fact.get("fact_type") or "unknown"), 0) + 1

    bank_periods_by_account: dict[str, list[dict]] = {}
    for fact in facts:
        if fact.get("fact_type") != "bank_statement_period_balance":
            continue
        fields = fact["fields"]
        account_key = _account_key_from_bank_balance(fields)
        start = _parse_bank_statement_date(str(fields.get("statement_period_start") or ""))
        end = _parse_bank_statement_date(str(fields.get("statement_period_end") or ""))
        bank_periods_by_account.setdefault(account_key, []).append({**fact, "account_key": account_key, "period_start_dt": start, "period_end_dt": end})

    bank_accounts: list[dict] = []
    for account_key, periods in sorted(bank_periods_by_account.items()):
        periods.sort(key=lambda item: (item.get("period_start_dt") or datetime.min, item.get("period_end_dt") or datetime.min, str(item.get("document"))))
        seen_periods: dict[tuple[str, str], list[dict]] = {}
        for item in periods:
            fields = item["fields"]
            period_key = (str(fields.get("statement_period_start") or ""), str(fields.get("statement_period_end") or ""))
            seen_periods.setdefault(period_key, []).append(item)
        for (start, end), items in seen_periods.items():
            if start and end and len(items) > 1:
                findings.append(
                    {
                        "category": "duplicate_bank_statement_period",
                        "severity": "medium",
                        "account_identifier": account_key,
                        "period_start": start,
                        "period_end": end,
                        "evidence_refs": [str(item.get("fact_ref")) for item in items],
                        "investigation_summary": [f"{len(items)} bank statement balance facts share the same statement period."],
                        "recommended_action": "Review duplicate statements and keep the authoritative statement before relying on continuity.",
                    }
                )
        for previous, current in zip(periods, periods[1:]):
            prev_fields = previous["fields"]
            current_fields = current["fields"]
            prev_end = previous.get("period_end_dt")
            current_start = current.get("period_start_dt")
            if prev_end and current_start and current_start.date() > (prev_end + timedelta(days=1)).date():
                findings.append(
                    {
                        "category": "missing_bank_statement_period",
                        "severity": "high",
                        "account_identifier": account_key,
                        "previous_period_end": prev_fields.get("statement_period_end"),
                        "next_period_start": current_fields.get("statement_period_start"),
                        "evidence_refs": [str(previous.get("fact_ref")), str(current.get("fact_ref"))],
                        "investigation_summary": ["There is a date gap between consecutive statement periods for this bank account."],
                        "recommended_action": "Request the missing bank statement period or confirm the account was inactive/closed.",
                    }
                )
            previous_closing = _normalised_amount_for_compare(prev_fields.get("closing_balance"))
            current_opening = _normalised_amount_for_compare(current_fields.get("opening_balance"))
            if previous_closing and current_opening and previous_closing != current_opening:
                findings.append(
                    {
                        "category": "bank_opening_closing_mismatch",
                        "severity": "high",
                        "account_identifier": account_key,
                        "previous_closing_balance": prev_fields.get("closing_balance"),
                        "next_opening_balance": current_fields.get("opening_balance"),
                        "evidence_refs": [str(previous.get("fact_ref")), str(current.get("fact_ref"))],
                        "investigation_summary": ["Closing balance from one statement does not agree to the next statement opening balance."],
                        "recommended_action": "Review statement sequence, missing transactions, duplicate statements, or extraction accuracy.",
                    }
                )
        bank_accounts.append(
            {
                "account_identifier": account_key,
                "statement_count": len(periods),
                "periods": [
                    {
                        "document": item.get("document"),
                        "fact_ref": item.get("fact_ref"),
                        "period_start": item["fields"].get("statement_period_start"),
                        "period_end": item["fields"].get("statement_period_end"),
                        "opening_balance": item["fields"].get("opening_balance"),
                        "closing_balance": item["fields"].get("closing_balance"),
                    }
                    for item in periods
                ],
            }
        )

    return {
        "artifact_type": "source_coverage_continuity",
        "entity_name": facts_payload.get("entity_name", "Uploaded documents") if isinstance(facts_payload, dict) else "Uploaded documents",
        "document_type_counts": document_type_counts,
        "fact_type_counts": fact_type_counts,
        "bank_accounts": bank_accounts,
        "findings": findings,
        "summary": {
            "documents": len(facts_payload.get("documents", []) if isinstance(facts_payload, dict) else []),
            "facts": len(facts),
            "bank_accounts": len(bank_accounts),
            "findings": len(findings),
            "high_severity_findings": sum(1 for item in findings if item.get("severity") == "high"),
        },
    }


def _format_source_coverage_continuity(payload: dict) -> str:
    summary = payload.get("summary", {})
    lines = [f"# Source Coverage & Continuity — {payload.get('entity_name', 'Uploaded documents')}", ""]
    lines.extend(
        [
            f"- Documents: {summary.get('documents', 0)}",
            f"- Bank accounts: {summary.get('bank_accounts', 0)}",
            f"- Findings: {summary.get('findings', 0)}",
            "",
        ]
    )
    if payload.get("bank_accounts"):
        lines.append("## Bank statement coverage")
        for account in payload["bank_accounts"]:
            lines.append(f"- {account.get('account_identifier')}: {account.get('statement_count')} statement period(s)")
    if payload.get("findings"):
        lines.extend(["", "## Findings"])
        for finding in payload["findings"]:
            lines.extend(
                [
                    f"- {finding.get('category')} ({finding.get('severity')}): {finding.get('account_identifier', '')}",
                    f"  - Evidence: {', '.join(finding.get('evidence_refs', []))}",
                    f"  - Action: {finding.get('recommended_action')}",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _process_documents_command(args: argparse.Namespace) -> int:
    input_dir = Path(args.input_dir)
    artifact_dir = Path(args.artifact_dir)
    codex_command = _normalise_codex_cli_command(str(args.codex_command))
    batch_size = max(1, int(getattr(args, "batch_size", 1) or 1))
    force_reprocess = bool(getattr(args, "force_reprocess", False))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    if not input_dir.exists() or not input_dir.is_dir():
        print(f"Input directory not found: {input_dir}", file=sys.stderr)
        return 2
    files = sorted(
        path
        for path in input_dir.rglob("*")
        if path.is_file()
        and path.name != ".DS_Store"
        and not any(part.startswith(".") for part in path.relative_to(input_dir).parts)
    )
    per_document_dir = artifact_dir / "per_document"
    cache_dir = artifact_dir / ".codex_doc_cache"
    progress_path = artifact_dir / "document_processing_progress.json"
    per_document_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    documents: list[dict] = []
    cache_hits = 0
    facts = 0
    failures = 0
    codex_attempts = 0
    codex_successes = 0
    total_batches = (len(files) + batch_size - 1) // batch_size if files else 0

    def process_one(index: int, path: Path) -> dict:
        document_id = f"raw_{index:03d}"
        source_hash = _sha256_file(path)
        cache_path = cache_dir / f"{_CODEX_DOCUMENT_PROCESSING_CACHE_VERSION}_{source_hash}.json"
        if cache_path.exists() and not force_reprocess:
            document = json.loads(cache_path.read_text())
            document["processing_source"] = "cache"
            source = "cache"
            failed = False
            codex_success = False
            attempt_history = []
        else:
            payload = None
            error = None
            attempt_history = []
            for attempt in range(1, max_attempts + 1):
                attempt_timeout = int(args.codex_timeout) * (2 ** (attempt - 1))
                payload, error = _codex_process_document(
                    path,
                    document_id,
                    source_hash,
                    codex_command,
                    attempt_timeout,
                    recovery_attempt=attempt - 1,
                    previous_error=error,
                )
                if payload is not None:
                    validation_error = _codex_document_validation_error(path, payload)
                    if validation_error:
                        error = validation_error
                        payload = None
                attempt_history.append(
                    {
                        "attempt": attempt,
                        "mode": "normal" if attempt == 1 else "recovery",
                        "timeout_seconds": attempt_timeout,
                        "status": "success" if payload is not None else "failed",
                        "error": error or "",
                    }
                )
                if payload is not None:
                    break
            if payload is None:
                document = _normalise_codex_document_result(
                    path,
                    document_id,
                    source_hash,
                    {
                        "display_name": path.name,
                        "document_type": _classify_raw_document(path),
                        "status": "processing_failed",
                        "no_fact_reason": error or "Codex CLI did not return a usable document result.",
                    },
                )
                document["processing_source"] = "codex_cli_failed"
                document["codex_attempt_history"] = attempt_history
                source = "codex_cli_failed"
                failed = True
                codex_success = False
            else:
                document = _normalise_codex_document_result(path, document_id, source_hash, payload)
                document["processing_source"] = "codex_cli"
                document["codex_attempt_history"] = attempt_history
                cache_path.write_text(json.dumps(document, indent=2, sort_keys=True))
                source = "codex_cli"
                failed = False
                codex_success = True
        document["document_id"] = document_id
        document["file_path"] = str(path)
        document["file_name"] = path.name
        document["original_file_name"] = path.name
        document["source_hash"] = source_hash
        if not document.get("page_quotes"):
            document["page_quotes"] = _document_text_for_codex(path)[0]
        return {
            "index": index,
            "path": path,
            "document_id": document_id,
            "document": document,
            "source": source,
            "failed": failed,
            "codex_success": codex_success,
            "attempts": len(attempt_history) if source != "cache" else 0,
            "facts": len(document.get("primary_amounts", []) or []) + len(document.get("review_flags", []) or []),
        }

    _write_document_processing_progress(
        progress_path,
        processed=0,
        total=len(files),
        current_document="",
        status="running",
        cache_hits=0,
        facts=0,
        codex_attempts=0,
        codex_successes=0,
        batch_size=batch_size,
        current_batch=0,
        total_batches=total_batches,
    )
    for batch_number, batch_start in enumerate(range(0, len(files), batch_size), start=1):
        batch = list(enumerate(files[batch_start : batch_start + batch_size], start=batch_start + 1))
        if batch_size == 1:
            results = [process_one(index, path) for index, path in batch]
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = [executor.submit(process_one, index, path) for index, path in batch]
                results = [future.result() for future in as_completed(futures)]
        for result in sorted(results, key=lambda item: item["index"]):
            document = result["document"]
            documents.append(document)
            if result["source"] == "cache":
                cache_hits += 1
            else:
                codex_attempts += int(result.get("attempts") or 1)
            if result["codex_success"]:
                codex_successes += 1
            if result["failed"]:
                failures += 1
            facts += int(result["facts"])
            per_document_dir.mkdir(parents=True, exist_ok=True)
            (per_document_dir / f"{result['document_id']}.json").write_text(json.dumps(document, indent=2, sort_keys=True))
            _write_document_processing_progress(
                progress_path,
                processed=len(documents),
                total=len(files),
                current_document=result["path"].name,
                status="running",
                cache_hits=cache_hits,
                facts=facts,
                failures=failures,
                codex_attempts=codex_attempts,
                codex_successes=codex_successes,
                batch_size=batch_size,
                current_batch=batch_number,
                total_batches=total_batches,
            )
    documents.sort(key=lambda item: str(item.get("display_name") or item.get("file_name") or "").casefold())
    inventory_payload = {"inventory_id": "processed_documents", "entity_name": "Uploaded documents", "documents": documents}
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "document_inventory.json").write_text(json.dumps(inventory_payload, indent=2, sort_keys=True))
    (artifact_dir / "document_inventory.md").write_text(_format_processed_document_inventory(inventory_payload))
    source_index_payload = _build_source_document_index_from_processed(inventory_payload)
    (artifact_dir / "source_document_index.json").write_text(json.dumps(source_index_payload, indent=2, sort_keys=True))
    (artifact_dir / "source_document_index.md").write_text(_format_processed_document_inventory(source_index_payload))
    facts_payload = _build_accounting_facts_by_document_from_processed(inventory_payload)
    (artifact_dir / "accounting_facts_by_document.json").write_text(json.dumps(facts_payload, indent=2, sort_keys=True))
    coverage_payload = _build_source_coverage_continuity_payload(facts_payload)
    (artifact_dir / "source_coverage_continuity.json").write_text(json.dumps(coverage_payload, indent=2, sort_keys=True))
    (artifact_dir / "source_coverage_continuity.md").write_text(_format_source_coverage_continuity(coverage_payload))
    final_status = "complete" if failures == 0 else "failed"
    _write_document_processing_progress(
        progress_path,
        processed=len(files),
        total=len(files),
        current_document="",
        status=final_status,
        cache_hits=cache_hits,
        facts=facts,
        failures=failures,
        codex_attempts=codex_attempts,
        codex_successes=codex_successes,
        batch_size=batch_size,
        current_batch=total_batches,
        total_batches=total_batches,
    )
    print(f"Indexed {len(files)} documents; fresh Codex successes: {codex_successes}; cache hits: {cache_hits}; source signals: {facts}; failures: {failures}")
    return 0 if failures == 0 else 1


_PRIOR_STATEMENT_ACCOUNT_SPECS = [
    {"name": "Capital Gain/(Loss) on Sale of Non-Current Assets", "aliases": ["Capital Gain/(Loss) on Sale of Non-Current Assets"], "type": "income", "group": "Other Income", "section": "profit_and_loss"},
    {"name": "Distributions Received", "aliases": ["Distributions Received"], "type": "income", "group": "Investment Income", "section": "profit_and_loss"},
    {"name": "Dividends Received", "aliases": ["Dividends Received"], "type": "income", "group": "Investment Income", "section": "profit_and_loss"},
    {"name": "Interest Income", "aliases": ["Interest Income"], "type": "income", "group": "Investment Income", "section": "profit_and_loss"},
    {"name": "Accounting Fees", "aliases": ["Accounting Fees"], "type": "expense", "group": "Expenses", "section": "profit_and_loss"},
    {"name": "Bank Fees", "aliases": ["Bank Fees"], "type": "expense", "group": "Expenses", "section": "profit_and_loss"},
    {"name": "Filing Fees", "aliases": ["Filing Fees"], "type": "expense", "group": "Expenses", "section": "profit_and_loss"},
    {"name": "Investment Expenses", "aliases": ["Investment Expenses"], "type": "expense", "group": "Expenses", "section": "profit_and_loss"},
    {"name": "Cash at Bank CBA0700", "aliases": ["Cash at Bank CBA0700"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Cash at Bank WBC8243", "aliases": ["Cash at Bank WBC8243"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Hub24 Cash Account", "aliases": ["Hub24 Cash Account"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Hub24 (Infinity SMID) Cash Account", "aliases": ["Hub24 (Infinity SMID) Cash Account"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Cash on Hand", "aliases": ["Cash on Hand"], "type": "asset", "group": "Cash and Cash Equivalents", "section": "cash"},
    {"name": "Sundry Debtors - Spire Branford Castle US Private Equity Fund II", "aliases": ["Spire Branford Castle US Private Equity Fund II"], "type": "asset", "group": "Receivables / Sundry Debtors", "section": "sundry_debtors"},
    {"name": "ANZ - Capital Notes 9", "aliases": ["ANZ - Capital Notes 9", "Investments ANZ Capital Notes"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "Bendigo and Adelaide Bank Limited - Capital Notes 2", "aliases": ["Bendigo and Adelaide Bank Limited - Capital Notes 2"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "EVP Fund III", "aliases": ["EVP Fund III"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "HUB24 Investments", "aliases": ["HUB24 Investments"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "Newmark Bourke St Mall Trust", "aliases": ["Newmark Bourke St Mall Trust"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "Spire Branford Castle US Private Equity Fund II", "aliases": ["Spire Branford Castle US Private Equity Fund II"], "type": "asset", "group": "Investments", "section": "investments"},
    {"name": "Unsecured Loan - Australia Property Trust", "aliases": ["Unsecured Loan - Australia Property Trust"], "type": "asset", "group": "Other Financial Assets", "section": "related_party_loans"},
    {"name": "Accrued expenses", "aliases": ["Accrued expenses"], "type": "liability", "group": "Payables and Accruals", "section": "current_liabilities"},
    {"name": "Unpaid Present Entitlement", "aliases": ["Unpaid Present Entitlement (2024)", "Unpaid Present Entitlement"], "type": "liability", "group": "Beneficiary Accounts", "section": "beneficiary_accounts", "skip_leading_dash": True},
    {"name": "Unsecured Loan", "aliases": ["Unsecured Loan"], "type": "liability", "group": "Borrowings / Loans", "section": "non_current_liabilities", "skip_leading_dash": True},
    {"name": "Settlement Sum", "aliases": ["Settlement Sum"], "type": "equity", "group": "Equity", "section": "equity"},
    {"name": "Current Year Earnings", "aliases": ["Current Year Earnings"], "type": "equity", "group": "Accumulated Income / Distributions", "section": "equity"},
    {"name": "Profit Distribution - Beneficiary", "aliases": ["Beneficiary"], "type": "equity", "group": "Accumulated Income / Distributions", "section": "equity", "leading_dash_is_negative": True},
]


def _prior_statement_account_code(index: int, account_type: str) -> str:
    prefix = {"asset": "1", "liability": "2", "equity": "3", "income": "4", "expense": "6"}.get(account_type, "9")
    return f"{prefix}{index:03d}"


def _compact_quote(value: str | None) -> str:
    return " ".join((value or "").split())


def _source_index_evidence_ref(document_id: str, evidence_id: str | None, page: str | None = None) -> str:
    value = str(evidence_id or "").strip()
    if re.search(r"_(?:page|text)_\d+$", value):
        return value
    if value and value.startswith(f"{document_id}_"):
        return value
    if value:
        return f"{document_id}_{value}"
    if page:
        return f"{document_id}_page_{int(page):03d}" if str(page).isdigit() else f"{document_id}_page_{page}"
    return document_id


def _quote_between(quote: str, start_pattern: str, end_pattern: str) -> str:
    start = re.search(start_pattern, quote, re.IGNORECASE)
    if not start:
        return ""
    remainder = quote[start.end():]
    end = re.search(end_pattern, remainder, re.IGNORECASE)
    return remainder[: end.start()] if end else remainder


def _prior_statement_section_quote(quote: str, section: str) -> str:
    sections = {
        "profit_and_loss": (r"\bProfit and Loss\b", r"\bRestricted for internal use only\b|\bBalance Sheet\b"),
        "cash": (r"\bCash and Cash Equivalents\b", r"\bTOTAL CASH AND CASH EQUIVALENTS\b"),
        "sundry_debtors": (r"\bSundry Debtors\b", r"\bTOTAL SUNDRY DEBTORS\b"),
        "investments": (r"\bInvestments\b", r"\bTOTAL INVESTMENTS\b"),
        "related_party_loans": (r"\bRelated Party Loans\b", r"\bTOTAL RELATED PARTY LOANS\b"),
        "current_liabilities": (r"\bCURRENT LIABILITIES\b", r"\bTOTAL CURRENT LIABILITIES\b"),
        "beneficiary_accounts": (r"\bBeneficiary Accounts\b", r"\bTOTAL BENEFICIARY ACCOUNTS\b"),
        "non_current_liabilities": (r"\bNON CURRENT LIABILITIES\b", r"\bTOTAL NON CURRENT LIABILITIES\b"),
        "equity": (r"\bEQUITY\b", r"\bTOTAL EQUITY\b"),
    }
    markers = sections.get(section)
    if not markers:
        return quote
    return _quote_between(quote, *markers)


def _prior_statement_amount_after_alias(quote: str, alias: str, *, skip_leading_dash: bool = False, leading_dash_is_negative: bool = False) -> str | None:
    match = re.search(re.escape(alias), quote, re.IGNORECASE)
    if not match:
        return None
    after = quote[match.end(): match.end() + 180]
    tokens = re.findall(r"\(?-?\$?\d[\d,]*(?:\.\d{2})?\)?|-", after)
    if not tokens:
        return None
    if tokens[0] == "-" and leading_dash_is_negative:
        for token in tokens[1:]:
            if token != "-":
                amount = _normalise_amount(token) or token
                return f"-{amount}" if not str(amount).startswith("-") else str(amount)
    if tokens[0] == "-" and skip_leading_dash:
        for token in tokens[1:]:
            if token != "-":
                return _normalise_amount(token) or token
    if tokens[0] == "-":
        return "0.00"
    return _normalise_amount(tokens[0]) or tokens[0]


def _extract_prior_statement_accounts_from_page_quotes(page_quotes: list[dict], *, document_id: str) -> list[dict]:
    accounts: list[dict] = []
    seen_names: set[str] = set()
    for page in page_quotes:
        quote = _compact_quote(str(page.get("quote") or ""))
        if not quote:
            continue
        evidence_ref = _source_index_evidence_ref(document_id, str(page.get("evidence_id") or ""), str(page.get("page") or ""))
        for spec in _PRIOR_STATEMENT_ACCOUNT_SPECS:
            name = str(spec["name"])
            if name in seen_names:
                continue
            section_quote = _prior_statement_section_quote(quote, str(spec.get("section") or ""))
            amount = None
            for alias in spec.get("aliases", [name]):
                amount = _prior_statement_amount_after_alias(
                    section_quote,
                    str(alias),
                    skip_leading_dash=bool(spec.get("skip_leading_dash")),
                    leading_dash_is_negative=bool(spec.get("leading_dash_is_negative")),
                )
                if amount is not None:
                    break
            if amount is None:
                continue
            account_type = str(spec["type"])
            group = str(spec["group"])
            code = _prior_statement_account_code(len(accounts) + 1, account_type)
            accounts.append(
                {
                    "account_id": f"prior_acct_{code}",
                    "code": code,
                    "name": name,
                    "type": account_type,
                    "presentation_group": group,
                    "opening_balance": amount or "0.00",
                    "source_evidence_refs": [evidence_ref],
                }
            )
            seen_names.add(name)
    return accounts


def _source_index_prior_fs_candidates(source_index: dict) -> list[dict]:
    candidates: list[dict] = []
    for document in _list_value(source_index.get("documents")):
        if not isinstance(document, dict):
            continue
        doc_type = str(document.get("document_type") or "").strip().lower()
        display = str(document.get("display_name") or document.get("file_name") or document.get("original_file_name") or "")
        if doc_type == "prior_year_financial_statements" or re.search(r"\bprior\b.*\bfinancial statement|\bfinancial statements?\b", display, re.IGNORECASE):
            candidates.append(document)
    return candidates


def _select_prior_fs_document(source_index: dict, *, prior_fs_document_id: str | None = None, prior_fs_file: str | None = None) -> tuple[dict | None, list[dict]]:
    findings: list[dict] = []
    candidates = _source_index_prior_fs_candidates(source_index)
    if prior_fs_document_id:
        for document in candidates:
            if str(document.get("document_id") or "") == prior_fs_document_id:
                return document, findings
        findings.append({"category": "prior_fs_document_not_found", "severity": "high", "message": f"Prior-year FS document id was not found in source index: {prior_fs_document_id}."})
        return None, findings
    if prior_fs_file:
        target = Path(prior_fs_file).name.lower()
        for document in candidates:
            names = {
                Path(str(document.get("file_path") or "")).name.lower(),
                str(document.get("file_name") or "").lower(),
                str(document.get("original_file_name") or "").lower(),
                str(document.get("display_name") or "").lower(),
            }
            if target in names:
                return document, findings
        findings.append({"category": "prior_fs_file_not_found", "severity": "high", "message": f"Prior-year FS file was not found in source index: {prior_fs_file}."})
        return None, findings
    if len(candidates) == 1:
        return candidates[0], findings
    if not candidates:
        findings.append({"category": "prior_fs_missing", "severity": "high", "message": "No prior-year financial statement document was found in Step 2 source index."})
        return None, findings
    findings.append({"category": "prior_fs_not_unique", "severity": "high", "message": "More than one prior-year financial statement document was found. Specify --prior-fs-document-id or --prior-fs-file."})
    return None, findings


def _build_prior_statement_coa_from_source_index(source_index: dict, *, prior_fs_document_id: str | None = None, prior_fs_file: str | None = None) -> dict:
    document, findings = _select_prior_fs_document(source_index, prior_fs_document_id=prior_fs_document_id, prior_fs_file=prior_fs_file)
    accounts: list[dict] = []
    if document:
        page_quotes = [page for page in _list_value(document.get("page_quotes")) if isinstance(page, dict)]
        accounts = _extract_prior_statement_accounts_from_page_quotes(page_quotes, document_id=str(document.get("document_id") or "prior_fs"))
        if not accounts:
            findings.append({"category": "prior_statement_coa_not_extracted", "severity": "high", "message": "Prior-year FS document was selected, but no opening balance accounts were extracted from its page quotes."})
    entity_name = str(source_index.get("entity_name") or source_index.get("engagement_id") or "Uploaded documents")
    return {
        "engagement_id": str(source_index.get("inventory_id") or source_index.get("engagement_id") or "source_document_index"),
        "entity_name": entity_name,
        "prior_fs_document_id": str(document.get("document_id") or "") if document else "",
        "prior_fs_display_name": str(document.get("display_name") or document.get("file_name") or "") if document else "",
        "accounts": accounts,
        "findings": findings,
        "summary": {"accounts_imported": len(accounts), "findings": len(findings), "approved": 0, "prior_fs_documents": 1 if document else 0},
    }


def _format_prior_statement_coa_import(payload: dict) -> str:
    lines = [f"# Prior Statement CoA Import — {payload['entity_name']}", ""]
    summary = payload["summary"]
    if payload.get("prior_fs_display_name"):
        lines.append(f"- Prior-year FS document: {payload['prior_fs_display_name']}")
    lines.extend([f"- Accounts imported: {summary['accounts_imported']}", f"- Approved automatically: {summary['approved']}", ""])
    if payload["accounts"]:
        lines.append("## Imported accounts pending review")
        for account in payload["accounts"]:
            lines.extend([f"- {account['code']} {account['name']}", f"  - Type: {account['type']}", f"  - Group: {account['presentation_group']}", f"  - Opening balance: {account['opening_balance']}", f"  - Evidence: {', '.join(account.get('source_evidence_refs', []))}"])
    if payload["findings"]:
        lines.extend(["", "## Findings needing review"])
        for finding in payload["findings"]:
            lines.extend([f"- {finding['category']}", f"  - Action: {finding.get('recommended_action') or finding.get('message') or ''}"])
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accountant-copilot",
        description="Prepare an AI-assisted financial statement workpaper from a client document pack.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    process_documents_parser = subparsers.add_parser(
        "process-documents",
        help="Build the source evidence index from an uploaded document folder.",
    )
    process_documents_parser.add_argument("--input-dir", default="inputs")
    process_documents_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    process_documents_parser.add_argument("--codex-command", default="codex exec")
    process_documents_parser.add_argument("--codex-timeout", default=120, type=int)
    process_documents_parser.add_argument("--codex-max-attempts", default=3, type=int)
    process_documents_parser.add_argument("--batch-size", default=5, type=int)
    process_documents_parser.add_argument(
        "--force-reprocess",
        action="store_true",
        help="Ignore existing per-document cache and reread uploaded files.",
    )
    process_documents_parser.set_defaults(func=_process_documents_command)

    source_fact_match_parser = subparsers.add_parser(
        "match-source-facts",
        help="Build the relationship reasoning register from the source evidence index.",
    )
    source_fact_match_parser.add_argument("--accounting-facts", default=None)
    source_fact_match_parser.add_argument("--source-coverage", default=None)
    source_fact_match_parser.add_argument("--codex-command", default="codex exec")
    source_fact_match_parser.add_argument("--codex-timeout", type=int, default=600)
    source_fact_match_parser.add_argument("--codex-max-attempts", type=int, default=3)
    source_fact_match_parser.add_argument("--bank-transactions", default=None, help=argparse.SUPPRESS)
    source_fact_match_parser.add_argument("--invoice-facts", default=None, help=argparse.SUPPRESS)
    source_fact_match_parser.add_argument("--distribution-tax-facts", default=None, help=argparse.SUPPRESS)
    source_fact_match_parser.add_argument("--broker-trade-facts", default=None, help=argparse.SUPPRESS)
    source_fact_match_parser.add_argument("--output", default="outputs/source_fact_matches.md")
    source_fact_match_parser.set_defaults(func=_match_source_facts_command)

    step4_workpaper_parser = subparsers.add_parser(
        "build-tb-bridge-workpaper",
        help="Build the accountant-style TB bridge workbook from relationship reasoning.",
    )
    step4_workpaper_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    step4_workpaper_parser.add_argument("--output-dir", default=TB_BRIDGE_OUTPUT_DIR)
    step4_workpaper_parser.add_argument("--event-register", default=None)
    step4_workpaper_parser.add_argument("--source-index", default=None)
    step4_workpaper_parser.add_argument("--prior-coa", default=None)
    step4_workpaper_parser.add_argument(
        "--prior-fs-document-id",
        default=None,
        help="Document id of the single prior-year financial statement to use as opening balances.",
    )
    step4_workpaper_parser.add_argument(
        "--prior-fs-file",
        default=None,
        help="File name/path of the single prior-year financial statement to use as opening balances.",
    )
    step4_workpaper_parser.add_argument("--codex-command", default="codex exec")
    step4_workpaper_parser.add_argument("--codex-timeout", type=int, default=600)
    step4_workpaper_parser.add_argument("--codex-max-attempts", type=int, default=3)
    step4_workpaper_parser.add_argument("--skip-xlsx", action="store_true")
    step4_workpaper_parser.set_defaults(func=_build_coa_mapping_workpaper_command)

    prepare_workpaper_parser = subparsers.add_parser(
        "prepare-workpaper",
        help="Run the full financial statement workpaper preparation workflow.",
    )
    prepare_workpaper_parser.add_argument("--client-folder", required=True, help="Folder containing source documents for the workpaper.")
    prepare_workpaper_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    prepare_workpaper_parser.add_argument("--output-dir", default=TB_BRIDGE_OUTPUT_DIR)
    prepare_workpaper_parser.add_argument("--entity-name", default=None)
    prepare_workpaper_parser.add_argument("--fy-start", default=None, help="Target financial year start date, e.g. 2024-07-01.")
    prepare_workpaper_parser.add_argument("--fy-end", default=None, help="Target financial year end date, e.g. 2025-06-30.")
    prepare_workpaper_parser.add_argument("--prior-fs-document-id", default=None, help="Document id of the single prior-year financial statement to use as opening balances.")
    prepare_workpaper_parser.add_argument("--prior-fs-file", default=None, help="File name/path of the single prior-year financial statement to use as opening balances.")
    prepare_workpaper_parser.add_argument("--codex-command", default="codex exec")
    prepare_workpaper_parser.add_argument("--codex-timeout", type=int, default=1200)
    prepare_workpaper_parser.add_argument("--codex-max-attempts", type=int, default=3)
    prepare_workpaper_parser.add_argument("--batch-size", type=int, default=5)
    prepare_workpaper_parser.add_argument("--review-sample-size", type=int, default=8)
    prepare_workpaper_parser.add_argument(
        "--review-correction-rounds",
        type=int,
        default=2,
        help="Maximum bounded senior-review correction rounds before stopping for human attention.",
    )
    prepare_workpaper_parser.add_argument(
        "--force-reprocess",
        action="store_true",
        help="Ignore existing per-document cache. prepare-workpaper is fresh by default unless --allow-cache is supplied.",
    )
    prepare_workpaper_parser.add_argument("--allow-cache", action="store_true", help="Allow source-index cache reuse for a faster development run.")
    prepare_workpaper_parser.add_argument("--skip-xlsx", action="store_true")
    prepare_workpaper_parser.add_argument("--skip-review", action="store_true")
    prepare_workpaper_parser.set_defaults(func=_prepare_workpaper_command)

    review_workpaper_parser = subparsers.add_parser(
        "review-workpaper",
        help="Run the senior accountant review over a prepared TB bridge workbook.",
    )
    review_workpaper_parser.add_argument("--client-folder", default=None)
    review_workpaper_parser.add_argument("--artifact-dir", default="outputs/raw_inputs_pdf_extraction")
    review_workpaper_parser.add_argument("--output-dir", default=TB_BRIDGE_OUTPUT_DIR)
    review_workpaper_parser.add_argument("--workpaper-json", default=None)
    review_workpaper_parser.add_argument("--source-index", default=None)
    review_workpaper_parser.add_argument("--event-register", default=None)
    review_workpaper_parser.add_argument("--prior-coa", default=None)
    review_workpaper_parser.add_argument("--output", default=None)
    review_workpaper_parser.add_argument("--entity-name", default=None)
    review_workpaper_parser.add_argument("--codex-command", default="codex exec")
    review_workpaper_parser.add_argument("--codex-timeout", type=int, default=1200)
    review_workpaper_parser.add_argument("--codex-max-attempts", type=int, default=3)
    review_workpaper_parser.add_argument("--sample-size", type=int, default=8)
    review_workpaper_parser.set_defaults(func=_review_workpaper_command)

    workpaper_portal_parser = subparsers.add_parser(
        "serve-workpaper-portal",
        help="Start the local accountant-facing workpaper portal.",
        description="Start the local accountant-facing workpaper portal.",
    )
    workpaper_portal_parser.add_argument("--host", default="127.0.0.1")
    workpaper_portal_parser.add_argument("--port", default=8787, type=int)
    workpaper_portal_parser.set_defaults(func=_serve_workpaper_portal_command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _load_local_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
