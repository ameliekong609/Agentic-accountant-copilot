"""Accountant-facing evidence index, review and movement-story previews."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from accountant_copilot.portal_config import (
    EVENT_REGISTER_PATH,
    SOURCE_INDEX_PATH,
    SUMMARY_PATH,
    TB_BRIDGE_JSON_PATH,
    TURING_REVIEW_PATH,
    _read_json,
)

def _read_summary_text(repo_root: Path) -> str:
    path = repo_root / SUMMARY_PATH
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")

def _turing_summary(repo_root: Path) -> dict[str, Any]:
    payload = _read_json(repo_root / TURING_REVIEW_PATH, {})
    summary = payload.get("summary") if isinstance(payload, dict) else {}
    findings = payload.get("findings") if isinstance(payload, dict) else []
    public_findings = [finding for finding in findings if _show_turing_finding_to_accountant(finding)] if isinstance(findings, list) else []
    return {
        "status": payload.get("status") if isinstance(payload, dict) else "",
        "summary": summary if isinstance(summary, dict) else {},
        "findings": [_friendly_turing_finding(finding) for finding in public_findings],
        "internal_note_count": max(0, len(findings) - len(public_findings)) if isinstance(findings, list) else 0,
    }

def _show_turing_finding_to_accountant(finding: Any) -> bool:
    if not isinstance(finding, dict):
        return False
    severity = str(finding.get("severity") or "medium").strip().lower()
    if severity == "low":
        return False
    category = str(finding.get("category") or "").strip().lower()
    message = str(finding.get("message") or "").casefold()
    if category == "presentation" and (
        "evidence index" in message or "source hyperlink" in message or "hyperlink" in message
    ) and ("blank" in message or "invisible" in message or "pdf cell" in message or "link" in message):
        return False
    return True

def _friendly_turing_finding(finding: dict[str, Any]) -> dict[str, Any]:
    message = str(finding.get("message") or "")
    lowered = message.casefold()
    category = str(finding.get("category") or "review").replace("_", " ")
    title = category.title()
    body = message
    check = "Review the related Movement story and source links before relying on this row."
    if "loan" in lowered and "upe" in lowered:
        title = "Loan / UPE Transfers"
        body = (
            "Tessa posted large unexplained bank transfers to the existing loan and UPE rows because those are the most likely balance-sheet accounts. "
            "The uploaded pack does not include receiving-account support, so this is an accountant judgement item."
        )
        check = "Confirm where the money went before relying on the loan/UPE classification."
    elif "investment values" in lowered or "market value" in lowered or "valuation" in lowered:
        title = "Investment Valuation"
        body = (
            "Tessa carried investment balances at prior-year book value. Market value statements were noted but not posted, because valuation movements should only be booked if the accountant confirms fair value treatment."
        )
        check = "Confirm whether the engagement uses cost/book value or fair value for these investments."
    elif "zxy" in lowered or "direct source payees" in lowered:
        title = "Indirect Payment Path"
        body = (
            "Tessa matched these cash movements even though the bank description uses ZXY rather than the direct source payee. This may be fine if ZXY paid or received on behalf of the entity."
        )
        check = "Confirm the payment pathway if this item is material."
    elif "silc" in lowered:
        title = "SILC Source-Only Items"
        body = (
            "Tessa did not post the SILC source-only distributions because the documents do not clearly link to this entity or to a matching bank receipt."
        )
        check = "Keep excluded unless the client confirms these items belong to this entity."
    return {
        **finding,
        "title": title,
        "body": body,
        "check": check,
    }

def _compact_text(value: Any, limit: int = 900) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."

def _doc_refs_for_relationship(relationship: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for ref in relationship.get("document_refs") if isinstance(relationship.get("document_refs"), list) else []:
        if str(ref).strip():
            refs.append(str(ref).strip())
    for node in relationship.get("evidence_nodes") if isinstance(relationship.get("evidence_nodes"), list) else []:
        if not isinstance(node, dict):
            continue
        for ref in node.get("document_refs") if isinstance(node.get("document_refs"), list) else []:
            if str(ref).strip():
                refs.append(str(ref).strip())
    return [ref for index, ref in enumerate(refs) if ref and ref not in refs[:index]]

def _movement_note_check_hint(note: dict[str, Any]) -> str:
    status = str(note.get("status") or "").casefold()
    blob = json.dumps(note, sort_keys=True).casefold()
    if "beneficiary" in blob or "upe" in blob or "profit distribution" in blob:
        return (
            "Recalculate this from the P&L rows shown below. If your result differs, compare the accounting treatment for "
            "fees, prepayments, filing/ATO items, investment expenses, and tax-only components before finalising UPE."
        )
    if status == "ready":
        return "No action required unless this row is selected for review. Use the links below to trace the supporting source or bank statement."
    if "payee" in blob or "needs confirmation" in blob or "confirm" in blob:
        return "Confirm the payee, destination, or client explanation before relying on this movement."
    if "rounding" in blob or "cents" in blob:
        return "Check the cents rounding only if the accountant wants the bridge to match source cents rather than prior-FS rounded dollars."
    if "invoice" in blob or "notice" in blob or "support" in blob:
        return "Open the linked evidence and check whether missing invoice, notice, or support should be attached before posting."
    if "valuation" in blob or "tax-only" in blob or "not posted" in blob:
        return "Check that this remains a note only and is not posted to the book bridge unless the accountant adopts that treatment."
    return "Review the explanation and linked evidence before moving this row from needs-attention to ready."

def _to_decimal_text(value: Any) -> str:
    try:
        amount = float(str(value or "0").replace(",", ""))
    except ValueError:
        amount = 0.0
    return f"{amount:,.2f}"

def _book_profit_bridge_for_note(bridge: dict[str, Any], note: dict[str, Any]) -> dict[str, Any] | None:
    blob = " ".join(
        str(value or "")
        for value in [
            note.get("account_name"),
            note.get("statement_group"),
            note.get("tb_column"),
            note.get("explanation"),
        ]
    ).casefold()
    if not ("beneficiary" in blob or "upe" in blob or "profit distribution" in blob):
        return None
    rows = bridge.get("matrix_rows", []) if isinstance(bridge.get("matrix_rows"), list) else []
    pnl_rows: list[dict[str, Any]] = []
    total = 0.0
    relationship_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("statement_section") != "Profit and loss":
            continue
        try:
            diff = float(str(row.get("difference") or "0").replace(",", ""))
        except ValueError:
            diff = 0.0
        if abs(diff) < 0.005:
            continue
        total += diff
        display_amount = -diff if diff < 0 else diff
        pnl_rows.append(
            {
                "account_name": row.get("account_name") or "",
                "statement_group": row.get("statement_group") or "",
                "amount": f"{display_amount:,.2f}",
                "effect": "adds to profit" if diff < 0 else "reduces profit",
            }
        )
        for movement in row.get("movements", []) if isinstance(row.get("movements"), list) else []:
            if not isinstance(movement, dict):
                continue
            relationship_id = str(movement.get("relationship_id") or "").strip()
            if relationship_id:
                relationship_ids.append(relationship_id)
    if not pnl_rows:
        return None
    draft_profit = -total
    calculation_parts = [
        ("+" if item["effect"] == "adds to profit" else "-") + " " + item["amount"]
        for item in pnl_rows
    ]
    calculation = " ".join(calculation_parts).lstrip("+ ").strip()
    if calculation:
        calculation = f"{calculation} = {_to_decimal_text(draft_profit)} draft book profit"
    return {
        "title": "Book-profit bridge behind this distribution",
        "summary": (
            "This amount is calculated from the draft book P&L rows, not lifted from one source document. "
            "If another workpaper has a different beneficiary distribution, compare the P&L treatments below first."
        ),
        "calculation": calculation,
        "rows": pnl_rows,
        "relationship_ids": [item for index, item in enumerate(relationship_ids) if item and item not in relationship_ids[:index]],
    }

def _row_calculation_tutorial(bridge: dict[str, Any], note: dict[str, Any]) -> dict[str, Any] | None:
    columns = bridge.get("movement_columns", []) if isinstance(bridge.get("movement_columns"), list) else []
    column_labels = {
        str(column.get("column_key")): str(column.get("label") or column.get("column_key") or "")
        for column in columns
        if isinstance(column, dict) and column.get("column_key")
    }
    rows = bridge.get("matrix_rows", []) if isinstance(bridge.get("matrix_rows"), list) else []
    note_id = str(note.get("note_id") or "")
    tb_row = str(note.get("tb_row") or "")
    row = None
    for candidate in rows:
        if not isinstance(candidate, dict):
            continue
        candidate_note_ids = [str(item) for item in candidate.get("note_ids", [])] if isinstance(candidate.get("note_ids"), list) else []
        if note_id and note_id in candidate_note_ids:
            row = candidate
            break
        if tb_row and str(candidate.get("tb_row") or "") == tb_row:
            row = candidate
            break
    if not row:
        return None
    movements = []
    for movement in row.get("movements", []) if isinstance(row.get("movements"), list) else []:
        if not isinstance(movement, dict):
            continue
        column_key = str(movement.get("column_key") or "")
        movements.append(
            {
                "column": column_labels.get(column_key) or column_key or "No column",
                "amount": _to_decimal_text(movement.get("amount")),
                "support_type": str(movement.get("support_type") or ""),
                "explanation": str(movement.get("explanation") or ""),
            }
        )
    opening = _to_decimal_text(row.get("opening_balance"))
    closing = _to_decimal_text(row.get("closing_balance"))
    if movements:
        movement_total = sum(float(str(movement.get("amount", "0")).replace(",", "")) for movement in movements)
        formula = f"{opening} + {_to_decimal_text(movement_total)} = {closing}"
    else:
        formula = f"{opening} + 0.00 = {closing}"
    section = str(row.get("statement_section") or "")
    if section == "Profit and loss":
        tutorial = (
            "Read this as a current-year P&L row. Income rows often appear as credits in the bridge, while expense rows reduce profit. "
            "Use the movement table to see which column created the amount."
        )
    elif section == "Balance sheet":
        tutorial = (
            "Read this left to right: prior-year opening balance, each FY movement, then closing balance. "
            "If the row is marked needs attention, the maths may be right but the accounting treatment still needs judgement."
        )
    else:
        tutorial = "Read this as a workpaper control row. Check the movement source and explanation before relying on it."
    return {
        "title": "How to read this row",
        "tutorial": tutorial,
        "formula": formula,
        "movements": movements,
    }

def _movement_notes_preview(repo_root: Path) -> list[dict[str, Any]]:
    bridge = _read_json(repo_root / TB_BRIDGE_JSON_PATH, {})
    source = _read_json(repo_root / SOURCE_INDEX_PATH, {})
    events = _read_json(repo_root / EVENT_REGISTER_PATH, {})
    if not isinstance(bridge, dict):
        return []
    source_documents = source.get("documents", []) if isinstance(source, dict) and isinstance(source.get("documents"), list) else []
    relationships = events.get("relationships", []) if isinstance(events, dict) and isinstance(events.get("relationships"), list) else []
    docs_by_id = {
        str(document.get("document_id")): document
        for document in source_documents
        if isinstance(document, dict) and document.get("document_id")
    }
    relationships_by_id = {
        str(relationship.get("relationship_id")): relationship
        for relationship in relationships
        if isinstance(relationship, dict) and relationship.get("relationship_id")
    }
    notes: list[dict[str, Any]] = []
    for note in bridge.get("movement_notes", []) if isinstance(bridge.get("movement_notes"), list) else []:
        if not isinstance(note, dict):
            continue
        raw_relationship_ids = note.get("relationship_ids") if isinstance(note.get("relationship_ids"), list) else []
        relationship_ids = [str(item) for item in raw_relationship_ids if str(item).strip()]
        row_tutorial = _row_calculation_tutorial(bridge, note)
        profit_bridge = _book_profit_bridge_for_note(bridge, note)
        if profit_bridge:
            for relationship_id in profit_bridge.get("relationship_ids", []):
                if relationship_id not in relationship_ids:
                    relationship_ids.append(relationship_id)
        doc_refs: list[str] = []
        relationship_stories: list[str] = []
        for relationship_id in relationship_ids:
            relationship = relationships_by_id.get(relationship_id)
            if not relationship:
                continue
            doc_refs.extend(_doc_refs_for_relationship(relationship))
            story = _compact_text(relationship.get("story"), 240)
            if story:
                relationship_stories.append(story)
        evidence_docs = []
        for ref in [ref for index, ref in enumerate(doc_refs) if ref and ref not in doc_refs[:index]][:10]:
            document = docs_by_id.get(ref)
            if not document:
                continue
            file_path = str(document.get("file_path") or "")
            evidence_docs.append(
                {
                    "document_id": ref,
                    "display_name": document.get("display_name") or document.get("file_name") or ref,
                    "document_type": document.get("document_type") or "",
                    "file_path": file_path,
                    "open_url": f"/open/source?path={quote(file_path)}" if file_path else "",
                    "period": " to ".join(str(value) for value in [document.get("period_start"), document.get("period_end")] if value) or document.get("statement_date") or "",
                }
            )
        notes.append(
            {
                "note_id": note.get("note_id") or "",
                "tb_row": note.get("tb_row") or "",
                "account_name": note.get("account_name") or "",
                "statement_section": note.get("statement_section") or "",
                "statement_group": note.get("statement_group") or "",
                "status": note.get("status") or "",
                "tb_column": note.get("tb_column") or "",
                "opening_balance": note.get("opening_balance") or "",
                "closing_balance": note.get("closing_balance") or "",
                "main_amount": note.get("main_amount") or "",
                "other_amounts": note.get("other_amounts") or "",
                "explanation": _compact_text(note.get("explanation"), 1400),
                "calculation": _compact_text(note.get("calculation"), 500),
                "evidence_summary": _compact_text(note.get("evidence_summary"), 650),
                "row_tutorial": row_tutorial,
                "profit_bridge": profit_bridge,
                "context_stories": relationship_stories[:4],
                "evidence_docs": evidence_docs,
                "check_hint": _movement_note_check_hint(note),
            }
        )
    return notes

def _evidence_index_preview(repo_root: Path) -> list[dict[str, Any]]:
    source = _read_json(repo_root / SOURCE_INDEX_PATH, {})
    documents = source.get("documents", []) if isinstance(source, dict) and isinstance(source.get("documents"), list) else []
    rows: list[dict[str, Any]] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        file_path = str(document.get("file_path") or "")
        rows.append(
            {
                "document_id": document.get("document_id") or "",
                "original_file_name": document.get("original_file_name") or document.get("file_name") or "",
                "display_name": document.get("display_name") or document.get("file_name") or "",
                "document_type": document.get("document_type") or "",
                "entity_relevance": document.get("entity_relevance") or document.get("relevance_status") or "",
                "open_url": f"/open/source?path={quote(file_path)}" if file_path else "",
            }
        )
    rows.sort(key=lambda row: (str(row.get("display_name") or "").lower(), str(row.get("original_file_name") or "").lower()))
    return rows

def _artifact_counts(repo_root: Path) -> dict[str, Any]:
    source = _read_json(repo_root / SOURCE_INDEX_PATH, {})
    events = _read_json(repo_root / EVENT_REGISTER_PATH, {})
    bridge = _read_json(repo_root / TB_BRIDGE_JSON_PATH, {})
    documents = source.get("documents") if isinstance(source, dict) else []
    relationships = events.get("relationships") if isinstance(events, dict) else []
    rows = bridge.get("matrix_rows") if isinstance(bridge, dict) else []
    notes = bridge.get("movement_notes") if isinstance(bridge, dict) else []
    columns = bridge.get("movement_columns") if isinstance(bridge, dict) else []
    return {
        "documents": len(documents) if isinstance(documents, list) else 0,
        "matrix_rows": len(rows) if isinstance(rows, list) else 0,
        "movement_notes": len(notes) if isinstance(notes, list) else 0,
        "movement_columns": len(columns) if isinstance(columns, list) else 0,
    }
