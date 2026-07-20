"""Prior-year financial statement opening balance extraction."""

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

from accountant_copilot.common import _list_value, _normalise_amount

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
