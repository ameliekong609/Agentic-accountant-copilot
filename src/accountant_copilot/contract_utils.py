"""Shared contract constants and low-level helpers for financial statement workflow artifacts."""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

RELATIONSHIP_REASONING_CONTRACT_VERSION = "relationship_reasoning_agent_v2"

TB_BRIDGE_CONTRACT_VERSION = "tb_bridge_matrix_agent_v6"

TB_BRIDGE_OUTPUT_DIR = "outputs/step4_tb_bridge_workpaper"

TB_BRIDGE_JSON = "tb_bridge_workpaper.json"

TB_BRIDGE_MD = "tb_bridge_workpaper.md"

TB_BRIDGE_XLSX = "step4_tb_bridge_workpaper.xlsx"

def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []

def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}

def _text(value: Any) -> str:
    return str(value or "").strip()

def _decimal(value: Any) -> Decimal | None:
    raw = _text(value).replace("$", "").replace(",", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None

_PNL_ACCOUNT_TYPES = {"income", "revenue", "expense"}

_TAX_ONLY_TERMS = ["franking", "tfn", "withholding", "gross-up", "gross up", "esvclp", "tax offset", "tax-only", "tax only"]

_TAX_ONLY_EXCLUSION_TERMS = [
    "not posted",
    "not_posted",
    "not post",
    "not included",
    "not include",
    "excluded",
    "excluding",
    "exclude",
    "notes only",
    "note only",
    "not part of book",
    "not part of the book",
    "book-profit-only",
    "book profit only",
    "tax-only components are noted",
    "tax only components are noted",
]

_TAX_ONLY_INCLUSION_TERMS = [
    "includes",
    "include ",
    "included",
    "including",
    "plus",
    "add ",
    "added",
    "grossed",
    "based on tax",
    "tax distribution",
]

def _account_type(value: Any) -> str:
    return _text(value).casefold()

def _is_pnl_account_type(value: Any) -> bool:
    return _account_type(value) in _PNL_ACCOUNT_TYPES

def _contains_tax_only_term(value: str) -> bool:
    lowered = value.casefold()
    return any(term in lowered for term in _TAX_ONLY_TERMS)

def _tax_only_mention_is_clearly_excluded(value: str) -> bool:
    lowered = value.casefold()
    if not _contains_tax_only_term(lowered):
        return True
    fragments = [fragment.strip() for fragment in re.split(r"(?<=[.;])\s+|\n+", lowered) if fragment.strip()]
    tax_fragments = [fragment for fragment in fragments if _contains_tax_only_term(fragment)]
    if not tax_fragments:
        tax_fragments = [lowered]
    for fragment in tax_fragments:
        has_exclusion = any(term in fragment for term in _TAX_ONLY_EXCLUSION_TERMS)
        has_inclusion = any(term in fragment for term in _TAX_ONLY_INCLUSION_TERMS)
        if "not included" in fragment or "not include" in fragment:
            has_inclusion = False
        if not has_exclusion or has_inclusion:
            return False
    return True

def _beneficiary_tax_boundary_violation(note: dict[str, Any]) -> dict[str, str] | None:
    if _text(note.get("status")) == "not_posted":
        return None
    note_blob = json.dumps(note, sort_keys=True).casefold()
    is_beneficiary_note = "beneficiar" in note_blob or "upe" in note_blob or "present entitlement" in note_blob
    if not is_beneficiary_note:
        return None
    for field in ("calculation", "explanation", "evidence_summary", "tb_column", "other_amounts"):
        value = _text(note.get(field))
        if not _contains_tax_only_term(value):
            continue
        if _tax_only_mention_is_clearly_excluded(value):
            continue
        return {
            "note_id": _text(note.get("note_id")) or "unknown",
            "field": field,
            "value": value[:280],
        }
    return None

def _statement_section_for_account(account_type: Any, statement_group: Any = "") -> str:
    account_type_text = _account_type(account_type)
    group_text = _text(statement_group).casefold()
    if account_type_text in _PNL_ACCOUNT_TYPES:
        return "Profit and loss"
    if account_type_text == "clearing" or "clearing" in group_text:
        return "Clearing / attention"
    return "Balance sheet"

def _money_text(value: Any, default: str = "") -> str:
    parsed = _decimal(value)
    if parsed is None:
        return default
    return f"{parsed:.2f}"

def _shorten(value: Any, limit: int = 1200) -> str:
    text = " ".join(_text(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."

def _join_unique(values: list[Any], *, sep: str = "; ", limit: int = 1200) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for value in values:
        text = _shorten(value, limit)
        if not text or text in seen:
            continue
        seen.add(text)
        parts.append(text)
    return _shorten(sep.join(parts), limit)

def _group_order(value: Any) -> int:
    text = _text(value).casefold()
    preferred = [
        "cash",
        "receivable",
        "sundry debtor",
        "investment",
        "other asset",
        "payable",
        "accrual",
        "beneficiary",
        "borrowing",
        "loan",
        "equity",
        "income",
        "revenue",
        "expense",
        "clearing",
    ]
    for index, needle in enumerate(preferred):
        if needle in text:
            return index
    return 99

def _section_order(value: Any) -> int:
    return {"Balance sheet": 0, "Profit and loss": 1, "Clearing / attention": 2}.get(_text(value), 9)

def _doc_ref(document: dict[str, Any], *, include_page_quotes: bool = True) -> dict[str, Any]:
    page_quotes = _as_list(document.get("page_quotes"))
    ref = {
        "document_id": document.get("document_id") or "",
        "display_name": document.get("display_name") or document.get("file_name") or document.get("original_file_name") or "",
        "original_file_name": document.get("original_file_name") or document.get("file_name") or "",
        "document_type": document.get("document_type") or "",
        "file_path": document.get("file_path") or "",
        "summary": document.get("document_summary") or document.get("summary") or "",
        "entity_relevance": document.get("entity_relevance") or "",
        "entity_relevance_reason": document.get("entity_relevance_reason") or "",
        "period_start": document.get("period_start") or "",
        "period_end": document.get("period_end") or "",
        "statement_date": document.get("statement_date") or "",
        "key_parties": _as_list(document.get("key_parties"))[:12],
        "key_identifiers": _as_list(document.get("key_identifiers"))[:12],
        "primary_amounts": _as_list(document.get("primary_amounts"))[:12],
        "review_flags": _as_list(document.get("review_flags")),
        "page_count": len(page_quotes),
    }
    if include_page_quotes:
        ref["page_quotes"] = [
            {
                "page": page.get("page"),
                "evidence_id": page.get("evidence_id"),
                "quote": " ".join(_text(page.get("quote")).split())[:1200],
            }
            for page in page_quotes[:3]
            if isinstance(page, dict) and page.get("quote")
        ]
    return ref

def _compact_recovery_payload(value: Any, *, depth: int = 0) -> Any:
    """Keep retry context useful without resending a whole prior workbook."""

    if depth > 5:
        return "[truncated]"
    if isinstance(value, str):
        text = " ".join(value.split())
        return text[:900] + ("..." if len(text) > 900 else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        max_items = 24 if depth <= 2 else 8
        compact = [_compact_recovery_payload(item, depth=depth + 1) for item in value[:max_items]]
        if len(value) > max_items:
            compact.append({"_omitted_items": len(value) - max_items})
        return compact
    if isinstance(value, dict):
        skip_keys = {
            "source_documents",
            "relationship_register",
            "source_index",
            "document_inventory",
            "accounting_facts_by_document",
            "page_quotes",
            "codex_attempt_history",
        }
        priority_keys = [
            "artifact_type",
            "relationship_reasoning_contract_version",
            "tb_bridge_contract_version",
            "status",
            "summary",
            "validation_findings",
            "relationships",
            "prior_fs_account_movement_coverage",
            "accounts",
            "movement_columns",
            "matrix_rows",
            "movement_notes",
            "relationship_coverage",
            "workpaper_notes",
        ]
        keys = [key for key in priority_keys if key in value and key not in skip_keys]
        keys.extend(key for key in value.keys() if key not in keys and key not in skip_keys)
        max_keys = 28 if depth <= 1 else 14
        selected = keys[:max_keys]
        compact_dict = {key: _compact_recovery_payload(value.get(key), depth=depth + 1) for key in selected}
        if len(keys) > max_keys:
            compact_dict["_omitted_keys"] = len(keys) - max_keys
        return compact_dict
    return str(value)[:900]
