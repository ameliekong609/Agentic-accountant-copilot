"""Accounting relationship reasoning between source evidence and cash movements."""

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
    _AMOUNT_RE,
    _DATE_RE,
    _bank_transaction_amount,
    _clean_money_amount,
    _date_value,
    _extract_json_object,
    _list_value,
    _load_optional_json,
    _money_value,
    _normalise_codex_cli_command,
    _unique_matches,
    _write_codex_attempt_history,
    _write_step_progress,
)
from accountant_copilot.document_indexing import _document_text_for_codex, _source_coverage_facts
from accountant_copilot.contract_utils import RELATIONSHIP_REASONING_CONTRACT_VERSION
from accountant_copilot.relationship_contract import (
    build_relationship_reasoning_prompt,
    failed_relationship_register,
    format_relationship_register,
    normalise_relationship_register,
    validate_relationship_register,
)

SOURCE_MATCHING_CONTRACT_VERSION = RELATIONSHIP_REASONING_CONTRACT_VERSION

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

def _validate_investigative_source_matches(payload: dict | None, facts_payload: dict) -> list[dict]:
    return validate_relationship_register(payload, facts_payload)

def _normalise_investigative_source_matches(payload: dict, facts_payload: dict, validation_findings: list[dict]) -> dict:
    return normalise_relationship_register(payload, facts_payload, validation_findings)

def _codex_failed_source_match_payload(facts_payload: dict, error: str, attempt_history: list[dict], validation_findings: list[dict] | None = None) -> dict:
    return failed_relationship_register(facts_payload, error, attempt_history, validation_findings)

def _format_investigative_source_matches(payload: dict) -> str:
    return format_relationship_register(payload)

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
