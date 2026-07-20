"""Source document indexing and lightweight evidence inventory."""

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
    _clean_money_amount,
    _extract_json_object,
    _normalise_codex_cli_command,
    _normalise_amount,
    _parse_bank_statement_date,
    _sha256_file,
    _write_step_progress,
    _xml_text,
)

_PDF_PAGE_QUOTE_CHAR_LIMIT = 8000

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
