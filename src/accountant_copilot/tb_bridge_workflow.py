"""Fresh Step 3/4 agent contracts for accountant-style TB bridge work."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


RELATIONSHIP_REASONING_CONTRACT_VERSION = "relationship_reasoning_agent_v2"
TB_BRIDGE_CONTRACT_VERSION = "tb_bridge_matrix_agent_v6"
TB_BRIDGE_OUTPUT_DIR = "outputs/step4_tb_bridge_workpaper"
TB_BRIDGE_JSON = "tb_bridge_workpaper.json"
TB_BRIDGE_MD = "tb_bridge_workpaper.md"
TB_BRIDGE_XLSX = "step4_tb_bridge_workpaper.xlsx"
ACCOUNTING_SKILLS_DIR = Path(__file__).resolve().parents[2] / "knowhow" / "skills"
ACCOUNTING_PDF_KNOWLEDGE_SKILL = "accounting-pdf-knowledge-retrieval"
ACCOUNTING_PDF_TOPIC_MAP = "pdf-topic-map.json"


def load_accounting_skill_for_prompt(skill_name: str, *, char_limit: int = 9000) -> dict[str, str]:
    """Load a repo-contained accountant skill for Codex prompts.

    Skills guide reasoning only. They are never client evidence.
    """
    safe_name = re.sub(r"[^a-z0-9-]", "", str(skill_name or "").casefold())
    path = ACCOUNTING_SKILLS_DIR / safe_name / "SKILL.md"
    if not safe_name or not path.exists():
        return {
            "name": safe_name or skill_name,
            "status": "missing",
            "source": "repo_knowhow_skill",
            "body": "",
            "rule": "Missing skills do not block the workflow.",
        }
    raw = path.read_text(encoding="utf-8", errors="ignore")
    description = ""
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            frontmatter, body = parts[1], parts[2]
            for line in frontmatter.splitlines():
                if line.strip().startswith("description:"):
                    description = line.split(":", 1)[1].strip().strip("\"'")
                    break
    return {
        "name": safe_name,
        "description": description,
        "status": "loaded",
        "source": "repo_knowhow_skill",
        "body": body.strip()[:char_limit],
        "rule": "Use this as accounting judgement guidance only. Do not cite it as client evidence.",
    }


def load_accounting_reference_for_prompt(skill_name: str, reference_name: str, *, char_limit: int = 8000) -> dict[str, str]:
    safe_skill_name = re.sub(r"[^a-z0-9-]", "", str(skill_name or "").casefold())
    safe_reference = re.sub(r"[^a-z0-9_.-]", "", str(reference_name or "").casefold())
    path = ACCOUNTING_SKILLS_DIR / safe_skill_name / "references" / safe_reference
    if not safe_skill_name or not safe_reference or not path.exists():
        return {
            "skill": safe_skill_name or skill_name,
            "name": safe_reference or reference_name,
            "status": "missing",
            "source": "repo_knowhow_reference",
            "body": "",
            "rule": "Missing references do not block the workflow.",
        }
    return {
        "skill": safe_skill_name,
        "name": safe_reference,
        "status": "loaded",
        "source": "repo_knowhow_reference",
        "body": path.read_text(encoding="utf-8", errors="ignore").strip()[:char_limit],
        "rule": "Use this as accounting judgement guidance only. Do not cite it as client evidence.",
    }


def load_accounting_pdf_topic_map_for_prompt(*, char_limit: int = 14000) -> dict[str, Any]:
    reference = load_accounting_reference_for_prompt(ACCOUNTING_PDF_KNOWLEDGE_SKILL, ACCOUNTING_PDF_TOPIC_MAP, char_limit=char_limit)
    if reference.get("status") != "loaded":
        return {
            "status": reference.get("status", "missing"),
            "source": "repo_knowhow_pdf_topic_map",
            "topics": [],
            "rule": "Missing PDF topic map does not block the workflow.",
        }
    try:
        topic_map = json.loads(reference.get("body", "{}"))
    except json.JSONDecodeError:
        return {
            "status": "invalid_json",
            "source": "repo_knowhow_pdf_topic_map",
            "topics": [],
            "rule": "Invalid PDF topic map does not block the workflow.",
        }
    topics = []
    for topic in _as_list(topic_map.get("topics")):
        if not isinstance(topic, dict):
            continue
        topics.append(
            {
                "topic_id": topic.get("topic_id", ""),
                "label": topic.get("label", ""),
                "use_when": topic.get("use_when", ""),
                "triggers": _as_list(topic.get("triggers"))[:12],
                "sections": [
                    {
                        "document": section.get("document", ""),
                        "section": section.get("section", ""),
                        "pdf_pages": section.get("pdf_pages", []),
                        "search_terms": _as_list(section.get("search_terms"))[:8],
                    }
                    for section in _as_list(topic.get("sections"))
                    if isinstance(section, dict)
                ],
            }
        )
    return {
        "status": "loaded",
        "source": "repo_knowhow_pdf_topic_map",
        "map_version": topic_map.get("map_version", ""),
        "documents": topic_map.get("documents", {}),
        "topics": topics,
        "rule": topic_map.get("source_of_truth_rule", "PDF knowhow is judgement guidance only; client evidence remains source of truth."),
    }


def accounting_pdf_retrieval_tool_for_prompt() -> dict[str, Any]:
    return {
        "skill": ACCOUNTING_PDF_KNOWLEDGE_SKILL,
        "map": f"knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/references/{ACCOUNTING_PDF_TOPIC_MAP}",
        "script": f"knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/scripts/retrieve_pdf_topic.py",
        "list_topics_command": (
            f"PYTHONPATH=src .venv/bin/python knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/scripts/retrieve_pdf_topic.py --list-topics"
        ),
        "retrieve_topic_command_template": (
            f"PYTHONPATH=src .venv/bin/python knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/scripts/retrieve_pdf_topic.py --topic <topic_id>"
        ),
        "retrieve_query_command_template": (
            f"PYTHONPATH=src .venv/bin/python knowhow/skills/{ACCOUNTING_PDF_KNOWLEDGE_SKILL}/scripts/retrieve_pdf_topic.py --query \"<technical accounting question>\""
        ),
        "rule": (
            "Use this only as an on-demand accounting book consultation. Do not cite retrieved knowhow PDFs as client evidence; "
            "use the retrieved section to decide what client evidence to inspect and how to classify judgement."
        ),
    }


def client_evidence_guardrail_for_prompt() -> dict[str, Any]:
    return {
        "source_of_truth": [
            "Uploaded client source documents in source_documents/source_document_index.",
            "The selected prior-year financial statements for opening balances and account structure.",
            "Bank statements, invoices, investment statements, tax statements, broker confirmations, capital calls, and other client files.",
        ],
        "allowed_skill_use": [
            "Use accounting skills, CA knowhow PDFs, and retrieved PDF snippets only to guide judgement, classification, review focus, and explanation style.",
            "Use skills to decide what to check, not as evidence that an amount, date, account, counterparty, or treatment exists for this client.",
        ],
        "forbidden_skill_use": [
            "Do not cite knowhow PDFs, retrieved PDF snippets, SKILL.md files, training material, or accounting skills as evidence.",
            "Do not put knowhow/skill files into document_refs, evidence_nodes, source_documents_checked, evidence_summary, or movement notes as support.",
            "Do not infer a client-specific amount, date, account, counterparty, or final treatment solely from a skill or training document.",
        ],
        "if_uncertain": "Mark the item needs_attention or unsupported and say which client evidence is missing.",
    }


_NON_CLIENT_EVIDENCE_TERMS = [
    "knowhow/skills",
    "knowhow\\skills",
    "skill.md",
    "accounting skill",
    "tb-bridge-preparation",
    "accounting-relationship-reasoning",
    "senior-workpaper-review",
    "accounting-pdf-knowledge-retrieval",
    "pdf-topic-map",
    "fin121_csg",
    "maaf121_csg",
    "knowhow/fin121",
    "knowhow/maaf121",
    "fin121 unit",
    "maaf121",
    "candidate study guide",
    "training material",
]

_NON_CLIENT_NEGATED_REFERENCE_TERMS = [
    "no skill",
    "no knowhow",
    "no training material",
    "not cited",
    "not cite",
    "not used as evidence",
    "not used as client evidence",
    "does not cite",
    "does not use",
    "without citing",
    "without using",
]


def _non_client_reference_is_negated(value: str) -> bool:
    lowered = value.casefold()
    if not any(term in lowered for term in _NON_CLIENT_NEGATED_REFERENCE_TERMS):
        return False
    if any(
        phrase in lowered
        for phrase in (
            "used as evidence",
            "cited as evidence",
            "relied on as evidence",
            "source documents checked",
            "evidence_summary: knowhow",
        )
    ) and not any(negated in lowered for negated in ("not used as evidence", "not cited", "does not cite", "without citing")):
        return False
    return True


def _non_client_evidence_hits(value: Any, *, path: str = "$") -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            hits.extend(_non_client_evidence_hits(item, path=f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_non_client_evidence_hits(item, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        lowered = value.casefold()
        if _non_client_reference_is_negated(value):
            return hits
        for term in _NON_CLIENT_EVIDENCE_TERMS:
            if term in lowered:
                hits.append({"path": path, "term": term, "value": value[:240]})
                break
    return hits


def non_client_evidence_reference_findings(value: Any, *, stage: str, message: str) -> list[dict[str, str]]:
    return [
        {
            "category": "non_client_evidence_reference",
            "severity": "high",
            "redo_required": True,
            "message": message,
            "stage": stage,
            "path": hit["path"],
            "term": hit["term"],
            "value": hit["value"],
        }
        for hit in _non_client_evidence_hits(value)[:8]
    ]


def source_of_truth_redo_required(findings: list[dict[str, Any]] | None) -> bool:
    return any(isinstance(finding, dict) and finding.get("category") == "non_client_evidence_reference" for finding in _as_list(findings))


def source_of_truth_redo_instruction(findings: list[dict[str, Any]] | None) -> str:
    if not source_of_truth_redo_required(findings):
        return ""
    return (
        "Redo the output from the affected stage. The previous output used accounting skills, knowhow PDFs, retrieved book snippets, training material, or SKILL.md text as evidence. "
        "Discard those citations. Use skills and retrieved PDF sections only as judgement guidance. Replace support with uploaded client documents, prior-year FS, bank statements, "
        "source document page quotes, or derived arithmetic from client evidence. If client evidence is missing, mark the item needs_attention or unsupported."
    )

STANDARD_MOVEMENT_ROLES: dict[str, dict[str, str]] = {
    "cash_account_movement": {
        "label": "Cash account movement",
        "purpose": "Cash/bank movement by bank, provider, or account nickname.",
        "label_style": "CBA, Westpac, NAB, Macquarie",
    },
    "investment_purchase_or_capital_call": {
        "label": "Investment purchase or capital call",
        "purpose": "New investment funding, capital call, or additional contribution.",
        "label_style": "Investment purchases, EVP capital calls",
    },
    "investment_disposal_proceeds": {
        "label": "Investment disposal proceeds",
        "purpose": "Cash proceeds from sale/redemption of an investment.",
        "label_style": "Use cash account column where practical",
    },
    "asset_disposal_gain_loss": {
        "label": "Asset disposal gain/loss",
        "purpose": "Book profit or loss on sale/redemption of investments or non-current assets.",
        "label_style": "Gain on sale of non-current assets, Loss on disposal of investments",
    },
    "investment_income": {
        "label": "Investment income",
        "purpose": "Dividends, distributions, interest, and similar book income.",
        "label_style": "Investment income, Distribution income, Interest income",
    },
    "source_entitlement_or_accrual": {
        "label": "Source entitlement or accrual",
        "purpose": "Source-led entitlement, receivable, payable, or residual movement.",
        "label_style": "Spire capital, distribution receivable, interest receivable",
    },
    "distribution_receivable_clearance": {
        "label": "Distribution receivable clearance",
        "purpose": "Opening receivable/debtor cleared by cash or source evidence.",
        "label_style": "Clear opening distribution receivable, Clear PY Spire debtor",
    },
    "current_year_receivable_accrual": {
        "label": "Current-year receivable accrual",
        "purpose": "Income earned but not received at year end.",
        "label_style": "Accrue distribution receivable, Accrue interest receivable",
    },
    "expense_payment": {
        "label": "Expense payment",
        "purpose": "Cash-supported expense payments.",
        "label_style": "Accounting fees, Filing fees, Bank fees",
    },
    "prior_period_payable_or_receivable_clearance": {
        "label": "Prior-period payable/receivable clearance",
        "purpose": "Opening payable, accrual, receivable, or debtor cleared/reversed in the current year.",
        "label_style": "Clear PY ASIC fee, Clear opening receivable",
    },
    "current_period_payable_or_accrual": {
        "label": "Current-period payable/accrual",
        "purpose": "Current-year expense, fee, tax, or obligation incurred but unpaid.",
        "label_style": "Accrue ASIC fee, Accrue accounting fees",
    },
    "tax_or_regulatory_clearing": {
        "label": "Tax or regulatory clearing",
        "purpose": "ATO, GST/BAS, ASIC, or regulatory cash movements where support is incomplete.",
        "label_style": "ATO clearing, GST clearing, ASIC clearing",
    },
    "loan_or_related_party_movement": {
        "label": "Loan or related-party movement",
        "purpose": "Loans, related-party accounts, director/shareholder loans, beneficiary funding, or internal transfers.",
        "label_style": "Unsecured loan movement, Director loan, Related-party loan",
    },
    "owner_or_beneficiary_distribution": {
        "label": "Owner or beneficiary distribution",
        "purpose": "UPE, beneficiary distribution, drawings, dividends, or owner allocation.",
        "label_style": "Distribution to beneficiary, Drawings, Dividend payable",
    },
    "fixed_asset_purchase_or_depreciation": {
        "label": "Fixed asset purchase/depreciation",
        "purpose": "Fixed asset additions, disposals, depreciation, or amortisation.",
        "label_style": "Plant and equipment additions, Depreciation",
    },
    "inventory_or_cogs": {
        "label": "Inventory or COGS",
        "purpose": "Trading stock, purchases, cost of goods sold, or inventory adjustments.",
        "label_style": "Inventory movement, Cost of goods sold",
    },
    "payroll_or_superannuation": {
        "label": "Payroll or superannuation",
        "purpose": "Wages, PAYG, superannuation, payroll tax, or employee obligations.",
        "label_style": "Payroll, Superannuation payable, PAYG withholding",
    },
    "lease_or_finance_obligation": {
        "label": "Lease or finance obligation",
        "purpose": "Lease liabilities, hire purchase, finance costs, or principal repayments.",
        "label_style": "Lease liability movement, HP loan",
    },
    "foreign_exchange_movement": {
        "label": "Foreign exchange movement",
        "purpose": "Realised or unrealised FX differences.",
        "label_style": "Foreign exchange gain/loss",
    },
    "intercompany_or_recharge": {
        "label": "Intercompany or recharge",
        "purpose": "Intercompany recoveries, recharges, group funding, or related entity settlements.",
        "label_style": "Intercompany recharge, Related entity recharge",
    },
    "valuation_only_note": {
        "label": "Valuation-only note",
        "purpose": "Market value/NAV information noted but not posted by default.",
        "label_style": "Not posted - valuation note",
    },
    "tax_only_note": {
        "label": "Tax-only note",
        "purpose": "Tax components such as franking credits, TFN withholding, offsets, or gross-ups noted but not posted by default.",
        "label_style": "Not posted - tax component note",
    },
    "wrong_entity_or_personal": {
        "label": "Wrong entity or personal",
        "purpose": "Evidence excluded because it belongs to another person/entity or is out of scope.",
        "label_style": "Excluded",
    },
    "unresolved_clearing": {
        "label": "Unresolved clearing",
        "purpose": "Real book/cash movement where no better accounting destination is supported yet.",
        "label_style": "Unresolved clearing, Missing destination",
    },
    "extension_role": {
        "label": "New proposed role",
        "purpose": "A client-specific or industry-specific role that does not fit the current library.",
        "label_style": "Propose a concise accountant-style label and explain why this should be learned.",
    },
}

_LEGACY_COLUMN_TYPE_ROLE_MAP = {
    "bank_movement": "cash_account_movement",
    "source_accrual": "source_entitlement_or_accrual",
    "disposal_gain_loss": "asset_disposal_gain_loss",
    "sale_gain_loss": "asset_disposal_gain_loss",
    "accrual_cleanup": "prior_period_payable_or_receivable_clearance",
    "current_accrual": "current_period_payable_or_accrual",
    "loan_or_related_party": "loan_or_related_party_movement",
    "beneficiary_distribution": "owner_or_beneficiary_distribution",
    "clearing": "unresolved_clearing",
    "tax_clearing": "tax_or_regulatory_clearing",
    "other": "extension_role",
}


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


def _standard_movement_role_library_for_prompt() -> list[dict[str, str]]:
    return [
        {"role_type": role_type, **details}
        for role_type, details in STANDARD_MOVEMENT_ROLES.items()
    ]


def _normalise_role_type(value: Any) -> str:
    role_type = _text(value).casefold().replace("-", "_").replace(" ", "_")
    return role_type if role_type in STANDARD_MOVEMENT_ROLES else ""


def _infer_movement_role_type(column: dict[str, Any]) -> tuple[str, str]:
    role = _as_dict(column.get("movement_role"))
    role_type = _normalise_role_type(role.get("role_type"))
    if role_type:
        return role_type, "movement_role"
    legacy_type = _text(column.get("column_type")).casefold()
    if legacy_type in _LEGACY_COLUMN_TYPE_ROLE_MAP:
        return _LEGACY_COLUMN_TYPE_ROLE_MAP[legacy_type], "legacy_column_type"
    return "extension_role", "inferred_extension"


def _normalise_movement_role(column: dict[str, Any]) -> dict[str, Any]:
    role_type, source = _infer_movement_role_type(column)
    supplied_role = _as_dict(column.get("movement_role"))
    role_details = STANDARD_MOVEMENT_ROLES.get(role_type, STANDARD_MOVEMENT_ROLES["extension_role"])
    new_role_proposal = _as_dict(supplied_role.get("new_role_proposal"))
    normalised = {
        "role_type": role_type,
        "standard_role_name": supplied_role.get("standard_role_name") or role_details["label"],
        "accounting_purpose": supplied_role.get("accounting_purpose") or role_details["purpose"],
        "label_basis": supplied_role.get("label_basis") or "",
        "source_or_counterparty": supplied_role.get("source_or_counterparty") or "",
        "cash_account": supplied_role.get("cash_account") or "",
        "new_role_proposal": new_role_proposal,
        "role_source": source,
    }
    if role_type == "extension_role" and not new_role_proposal:
        normalised["new_role_proposal"] = {
            "suggested_role_name": _text(column.get("label")) or _text(column.get("column_key")) or "client_specific_role",
            "why_existing_roles_do_not_fit": "Tessa did not map this column to a standard role yet.",
            "affected_accounts": [],
            "suggested_reuse_rule": "Review and promote only if this pattern recurs.",
        }
    return normalised


def _accountant_label_warning(column: dict[str, Any], role_type: str) -> str:
    label = _text(column.get("label"))
    lowered = label.casefold()
    if not label:
        return "Movement column is missing a readable accountant-style label."
    if role_type == "asset_disposal_gain_loss" and not any(term in lowered for term in ("gain", "loss", "disposal", "sale of non-current", "profit on sale")):
        return "Disposal/gain-loss columns should be labelled by accounting purpose, not only by the security or event name."
    if role_type == "cash_account_movement" and not any(term in lowered for term in ("bank", "cash", "cba", "westpac", "nab", "anz", "macquarie", "commbank")):
        return "Cash movement columns should normally use the bank/provider/account nickname found in evidence."
    if role_type in {"prior_period_payable_or_receivable_clearance", "distribution_receivable_clearance"} and not any(term in lowered for term in ("clear", "clearing", "py", "prior", "opening")):
        return "Prior-period clearance columns should make the clearing purpose obvious."
    if role_type == "current_period_payable_or_accrual" and not any(term in lowered for term in ("accrue", "accrual", "payable")):
        return "Current-period accrual columns should make the accrual/payable purpose obvious."
    if role_type == "owner_or_beneficiary_distribution" and not any(term in lowered for term in ("beneficiary", "distribution", "drawings", "dividend", "owner", "upe")):
        return "Owner/beneficiary distribution columns should name the owner, beneficiary, UPE, drawings, or dividend purpose."
    return ""


def _movement_role_validation_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    column_roles: dict[str, str] = {}
    for column in _as_list(payload.get("movement_columns")):
        if not isinstance(column, dict):
            continue
        column_key = _text(column.get("column_key")) or "<missing column_key>"
        supplied_role = _as_dict(column.get("movement_role"))
        role_type, source = _infer_movement_role_type(column)
        column_roles[column_key] = role_type
        if not supplied_role:
            findings.append(
                {
                    "category": "movement_role_inferred",
                    "severity": "medium",
                    "message": f"{column_key} did not include movement_role; inferred {role_type} from existing column metadata.",
                    "column_key": column_key,
                    "role_type": role_type,
                }
            )
        elif source != "movement_role":
            findings.append(
                {
                    "category": "unknown_movement_role",
                    "severity": "medium",
                    "message": f"{column_key} used an unknown role; treated as {role_type} for learning-loop review.",
                    "column_key": column_key,
                    "supplied_role_type": supplied_role.get("role_type"),
                    "role_type": role_type,
                }
            )
        if role_type == "extension_role":
            proposal = _as_dict(supplied_role.get("new_role_proposal"))
            if not _text(proposal.get("why_existing_roles_do_not_fit")):
                findings.append(
                    {
                        "category": "extension_role_needs_learning_brief",
                        "severity": "medium",
                        "message": f"{column_key} is a new role candidate. Add why existing standard roles do not fit before promoting it to the library.",
                        "column_key": column_key,
                    }
                )
        warning = _accountant_label_warning(column, role_type)
        if warning:
            findings.append(
                {
                    "category": "movement_column_label_needs_accountant_style",
                    "severity": "medium",
                    "message": f"{column_key}: {warning}",
                    "column_key": column_key,
                    "role_type": role_type,
                    "label": column.get("label"),
                }
            )
    note_only_roles = {"tax_only_note", "valuation_only_note", "wrong_entity_or_personal"}
    for row in _as_list(payload.get("matrix_rows")):
        if not isinstance(row, dict):
            continue
        for movement in _as_list(row.get("movements")):
            if not isinstance(movement, dict):
                continue
            column_key = _text(movement.get("column_key"))
            role_type = column_roles.get(column_key)
            if role_type in note_only_roles:
                findings.append(
                    {
                        "category": "note_only_role_has_matrix_movement",
                        "severity": "medium",
                        "message": f"{column_key} is {role_type} but appears in the TB matrix. Keep tax-only, valuation-only, and wrong-entity items in notes/exclusions unless the accountant adopts a book posting.",
                        "column_key": column_key,
                        "role_type": role_type,
                    }
                )
    return findings


def _account_text_blob(value: dict[str, Any]) -> str:
    return " ".join(
        _text(value.get(key))
        for key in ("account_name", "account_type", "statement_section", "statement_group", "notes")
    ).casefold()


def _relationship_text_blob(relationship: dict[str, Any]) -> str:
    return json.dumps(relationship, sort_keys=True, default=str).casefold()


def _movement_column_roles(payload: dict[str, Any]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for column in _as_list(payload.get("movement_columns")):
        if not isinstance(column, dict):
            continue
        key = _text(column.get("column_key"))
        if key:
            roles[key] = _infer_movement_role_type(column)[0]
    return roles


def _non_cash_split_validation_findings(payload: dict[str, Any], relationship_register: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    column_roles = _movement_column_roles(payload)
    role_types_present = set(column_roles.values())
    relationships_by_id = {
        _text(relationship.get("relationship_id")): relationship
        for relationship in _as_list(relationship_register.get("relationships"))
        if isinstance(relationship, dict) and _text(relationship.get("relationship_id"))
    }

    has_gain_loss_column = "asset_disposal_gain_loss" in role_types_present
    has_receivable_or_source_column = bool(
        role_types_present
        & {
            "source_entitlement_or_accrual",
            "current_year_receivable_accrual",
            "distribution_receivable_clearance",
            "prior_period_payable_or_receivable_clearance",
        }
    )

    for row in _as_list(payload.get("matrix_rows")):
        if not isinstance(row, dict):
            continue
        row_blob = _account_text_blob(row)
        movements = [movement for movement in _as_list(row.get("movements")) if isinstance(movement, dict)]
        if not movements:
            continue
        movement_roles = {column_roles.get(_text(movement.get("column_key")), "") for movement in movements}
        non_zero_movements = [movement for movement in movements if (_decimal(movement.get("amount")) or Decimal("0")) != Decimal("0")]
        if not non_zero_movements:
            continue
        linked_relationship_text = " ".join(
            _relationship_text_blob(relationships_by_id.get(_text(movement.get("relationship_id")), {}))
            for movement in movements
        )
        note_text = " ".join(_text(row.get(key)) for key in ("notes", "calculation", "evidence_summary")).casefold()

        gain_loss_blob = " ".join(_text(row.get(key)) for key in ("account_name", "statement_group")).casefold()
        if (
            "cash_account_movement" in movement_roles
            and any(term in gain_loss_blob for term in ("gain", "disposal", "sale of non-current", "profit on sale", "loss on", "gain/(loss"))
            and not has_gain_loss_column
        ):
            findings.append(
                {
                    "category": "gain_loss_needs_separate_movement_column",
                    "severity": "high",
                    "message": (
                        f"{_text(row.get('account_name'))} is a gain/loss or disposal row but is posted through a cash-account column. "
                        "Create an asset_disposal_gain_loss movement column for the gain/loss residual, and keep bank columns for the cash/carrying-value leg."
                    ),
                }
            )

        receivable_row = any(term in row_blob for term in ("debtor", "receivable", "sundry debt"))
        source_roll_forward_story = any(
            term in (linked_relationship_text + " " + note_text)
            for term in ("source", "entitlement", "distribution", "accrual", "less", "leaves", "residual")
        )
        cash_only = movement_roles <= {"cash_account_movement"} and "cash_account_movement" in movement_roles
        opening = _decimal(row.get("opening_balance")) or Decimal("0")
        closing = _decimal(row.get("closing_balance")) or Decimal("0")
        if (
            receivable_row
            and cash_only
            and source_roll_forward_story
            and opening != Decimal("0")
            and closing != Decimal("0")
            and not has_receivable_or_source_column
        ):
            findings.append(
                {
                    "category": "receivable_roll_forward_needs_source_column",
                    "severity": "high",
                    "message": (
                        f"{_text(row.get('account_name'))} appears to combine opening receivable, cash receipts, source entitlement, and closing receivable into one net cash movement. "
                        "Split it into a cash_account_movement column for bank receipts/clearing and a source_entitlement_or_accrual or receivable/accrual column for the source-supported residual."
                    ),
                }
            )
    return findings


def _normalise_movement_column_roles(payload: dict[str, Any]) -> dict[str, Any]:
    transformed = dict(payload)
    columns = []
    for column in _as_list(transformed.get("movement_columns")):
        if not isinstance(column, dict):
            continue
        normalised = dict(column)
        normalised["movement_role"] = _normalise_movement_role(normalised)
        if not _text(normalised.get("column_type")):
            normalised["column_type"] = normalised["movement_role"]["role_type"]
        columns.append(normalised)
    transformed["movement_columns"] = columns
    return transformed


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


def _collect_document_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            refs.update(_collect_document_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_collect_document_refs(item))
    elif isinstance(value, str):
        refs.update(re.findall(r"\braw_\d+\b", value))
    return refs


def _excluded_document_relevance_overrides(relationship_register: dict[str, Any]) -> dict[str, dict[str, str]]:
    overrides: dict[str, dict[str, str]] = {}
    for relationship in _as_list(relationship_register.get("relationships")):
        if not isinstance(relationship, dict):
            continue
        status = _text(relationship.get("status")).casefold()
        relationship_type = _text(relationship.get("relationship_type")).casefold()
        if status != "excluded" and relationship_type != "entity_exclusion":
            continue
        relationship_id = _text(relationship.get("relationship_id"))
        story = _text(relationship.get("story")) or _text(relationship.get("why_it_matters_for_step4"))
        reason_parts = ["Excluded by Step 3 relationship reasoning"]
        if relationship_id:
            reason_parts.append(f"({relationship_id})")
        if story:
            reason_parts.append(f": {story}")
        reason = " ".join(reason_parts)
        for document_id in _collect_document_refs(relationship):
            overrides[document_id] = {
                "entity_relevance": "excluded",
                "entity_relevance_reason": reason,
                "relationship_id": relationship_id,
            }
    return overrides


def relationship_reasoning_context(source_index: dict[str, Any], coverage_payload: dict[str, Any] | None) -> dict[str, Any]:
    documents = [_doc_ref(document) for document in _as_list(source_index.get("documents")) if isinstance(document, dict)]
    prior_fs_documents = [
        document
        for document in documents
        if _text(document.get("document_type")) == "prior_year_financial_statements"
        or re.search(r"\bfinancial statements?\b|\bprior\b", _text(document.get("display_name")), re.IGNORECASE)
    ]
    return {
        "relationship_reasoning_contract_version": RELATIONSHIP_REASONING_CONTRACT_VERSION,
        "agent_mode": "codex_cli_digital_accountant",
        "entity_name": source_index.get("entity_name") or "Uploaded documents",
        "target_financial_year": {
            "start": source_index.get("target_fy_start") or "",
            "end": source_index.get("target_fy_end") or "",
            "instruction": "If provided, use this as the reporting FY. Keep out-of-period evidence visible, but label whether it supports opening balances, FY movement, post-year activity, or exclusion.",
        },
        "workspace": {
            "cwd": str(Path.cwd()),
            "instruction": (
                "You are running inside the client repository with access to the file_path values. "
                "Open/read PDFs or page quotes as needed. Do not rely only on the short document summary when a relationship needs evidence."
            ),
            "input_documents_dir": "inputs",
            "source_document_index": "outputs/raw_inputs_pdf_extraction/source_document_index.json",
        },
        "documents": documents,
        "prior_year_fs_documents": prior_fs_documents,
        "source_coverage_continuity": coverage_payload or {},
        "investigation_method": [
            "1. Prior-FS anchored pass: read the prior-year financial statement first and identify the opening balance sheet rows and prior-year P&L comparatives that define the TB bridge starting point.",
            "2. Movement-by-row pass: for each prior-FS balance-sheet row, ask what FY movement explains the change: cash roll-forward, investment sale/capital call, receivable/payable bridge, loan/transfer movement, accrual, beneficiary/UPE, or no movement found.",
            "3. P&L/current-year pass: identify current-year income and expense events supported by source documents and/or bank movements, without treating prior-year P&L comparative amounts as opening balances.",
            "4. Leftover sweep: after prior-FS rows are explained, search remaining bank-only, source-only, personal/wrong-entity, out-of-period, and unresolved items.",
            "5. Step 4 handoff: explain movements and give optional matrix_hints, but do not decide final movement column names. Step 4 owns the accountant-style movement columns.",
        ],
        "accounting_skill": load_accounting_skill_for_prompt("accounting-relationship-reasoning"),
        "accounting_pdf_retrieval_skill": load_accounting_skill_for_prompt(ACCOUNTING_PDF_KNOWLEDGE_SKILL),
        "accounting_pdf_topic_map": load_accounting_pdf_topic_map_for_prompt(),
        "accounting_pdf_retrieval_tool": accounting_pdf_retrieval_tool_for_prompt(),
        "client_evidence_guardrail": client_evidence_guardrail_for_prompt(),
        "rules": [
            "Step 3 is relationship reasoning, not final posting. Do not output Dr/Cr or chart-of-account mappings.",
            "Act like a digital junior accountant: inspect original PDFs/page quotes, reason across bank statements, prior FS, broker confirmations, tax statements, investment statements, invoices, capital calls, and correspondence.",
            "Build accountant-useful relationships. A relationship can be a direct source+bank match, bank-only classification, source-only accrual/valuation, opening balance support, bank roll-forward, investment sale roll-forward, receivable/payable bridge, loan/transfer bridge, entity exclusion, or unresolved trace.",
            "Start from the prior-year FS where one exists. The first investigation pass must anchor the relationship register to opening balance rows and prior-year P&L comparatives, then explain current-year movements against those rows.",
            "After the prior-FS anchored pass, run both remaining directions: source-first for documents that imply cash/balances and bank-first for cash movements that still need classification.",
            "When source instructions name a bank/payee/reference, search that path first before same-amount alternatives.",
            "Use prior-year financial statement documents as opening-balance evidence. Mark accounts_involved source as prior_fs when an account name comes from the prior FS.",
            "For each material prior-FS balance-sheet row, create or reference a relationship that explains one of: movement found, no movement found, out-of-period support only, excluded/wrong-entity support, or unresolved trace.",
            "Use prior-year P&L lines only as comparative context. Do not treat prior-year income/expense amounts as opening balances.",
            "If a document is clearly personal/wrong-entity, make a one-document exclusion relationship with a clear reason.",
            "Assume documents are relevant unless there is clear personal/wrong-entity/non-accounting evidence.",
            "If target_financial_year is provided, do not guess a different FY from file names alone. Use document dates and bank dates to classify in-period, opening/prior-year, post-year, or out-of-period evidence.",
            "Do useful arithmetic. If source total splits into cash-supported and residual source-only components, show the equation and the residual.",
            "Do useful roll-forwards. For bank accounts, reconcile opening to closing from statement balances and meaningful movements if available; label this evidence-derived if it is calculated rather than a single stated line.",
            "Do useful investment bridge reasoning. If prior FS carries an investment at cost and a sale receipt clears it, calculate gain/loss as proceeds minus opening carrying value when evidence supports a full sale.",
            "Do useful receivable/payable bridge reasoning. Combine opening debtor/creditor from prior FS, cash receipts/payments, and source entitlements/accruals to explain closing residuals.",
            "If a technical accounting topic is triggered (FX, inventory, revenue cut-off, deferred tax, leases, provisions, financial instruments, fair value, consolidation, hedging, or similar), use accounting_pdf_topic_map to identify the right book section and run accounting_pdf_retrieval_tool before concluding the accounting treatment.",
            "Retrieved PDF knowhow is only a book consultation. Use it to decide what to check in client evidence; do not cite the book as source evidence in the output.",
            "Do not label an item bank-only merely because Step 2 did not extract the exact source line. Step 2 intentionally has no detailed facts. Read the document.",
            "For bank-only items such as KPMG, ATO, bank fees, interest, platform fees, broker/FNZ settlements, or internal transfers, classify the cash movement and state the limitation that no external support is attached.",
            "If a transfer or payment path is plausible but not proven, keep it needs_attention and explain the missing link. Do not make a clean match.",
            "Keep stories short and useful. The story should let a junior accountant understand what happened without reading a paragraph of file names.",
            "Movement column names are not final in Step 3. Use matrix_hints only to help Step 4 decide columns such as CBA, Westpac, ANZ sale, Spire distribution, KPMG fees, ASIC accrual, beneficiary distribution, or client-specific equivalents.",
            "Never fabricate evidence. Distinguish evidence_stated, evidence_derived, and judgement.",
            "Return exactly one JSON object and no markdown.",
        ],
        "relationship_examples_to_learn_from_not_hardcode": [
            {
                "pattern": "bank_roll_forward",
                "story": "CBA opening and closing balances reconcile by summing monthly statement movements. The annual movement is evidence-derived from the statement chain, not a single source-stated transaction.",
            },
            {
                "pattern": "distribution_receivable_roll_forward",
                "story": "Opening Spire debtor plus FY25 Spire distribution entitlement less CBA receipts leaves a 30 June debtor. Show the cash-supported portion and source-only residual separately.",
            },
            {
                "pattern": "investment_sale_roll_forward",
                "story": "ANZ or BENPI sale proceeds received in Westpac clear the opening investment cost. Difference between proceeds and opening carrying value is a gain/loss relationship for Step 4.",
            },
            {
                "pattern": "loan_or_transfer_bridge",
                "story": "Large Westpac transfer components can support a loan/transfer bridge only when the component arithmetic and bank descriptions make sense; unresolved CBA amounts should stay needs_attention until traced.",
            },
        ],
    }


def build_relationship_reasoning_prompt(
    source_index: dict[str, Any],
    coverage_payload: dict[str, Any] | None,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict[str, Any]] | None = None,
    previous_payload: dict[str, Any] | None = None,
) -> str:
    recovery_context: dict[str, Any] = {}
    if recovery_attempt:
        redo_instruction = source_of_truth_redo_instruction(validation_findings)
        recovery_context = {
            "recovery_attempt": recovery_attempt,
            "previous_error": previous_error or "",
            "validation_findings_to_fix": validation_findings or [],
            "previous_payload_summary": _compact_recovery_payload(previous_payload or {}),
            "source_of_truth_redo_required": bool(redo_instruction),
            "instruction": redo_instruction or "Repair the JSON so it satisfies the contract. Do not use deterministic fallback.",
        }
    return json.dumps(
        {
            "role": "You are Codex CLI acting as a digital accountant preparing Step 3 relationship reasoning for TB bridge work.",
            "task": (
                "Read the source document index and original file paths, then build a relationship register that explains what happened, "
                "what evidence supports it, what arithmetic or judgement was used, and what remains unresolved. "
                "This is the heavy reasoning step before the TB bridge matrix. No Dr/Cr or CoA mapping yet."
            ),
            "required_output_schema": {
                "artifact_type": "relationship_reasoning_register",
                "relationship_reasoning_contract_version": RELATIONSHIP_REASONING_CONTRACT_VERSION,
                "status": "ready|needs_attention",
                "agent": "codex_cli",
                "relationships": [
                    {
                        "relationship_id": "rel_001",
                        "relationship_type": "source_bank_match|bank_only_classification|source_only_balance_or_accrual|bank_roll_forward|investment_sale_roll_forward|distribution_receivable_roll_forward|loan_or_transfer_bridge|opening_balance_support|entity_exclusion|unresolved_trace|other",
                        "status": "ready_for_bridge|needs_attention|excluded|informational",
                        "confidence": "high|medium|low",
                        "evidence_level": "source_and_bank|bank_only|source_only|evidence_derived|mixed|no_accounting_event",
                        "story": "one or two concise sentences in accountant language",
                        "date": "YYYY-MM-DD or blank",
                        "amount": "decimal string or blank",
                        "direction": "receipt|payment|non_cash|mixed|blank",
                        "accounts_involved": [
                            {
                                "account_name": "account name, preferably from prior FS where relevant",
                                "role": "cash|investment|receivable|payable|loan|income|expense|equity|clearing|other",
                                "source": "prior_fs|source_document|bank_statement|codex_inferred|unknown",
                                "confidence": "high|medium|low",
                            }
                        ],
                        "evidence_nodes": [
                            {
                                "node_id": "ev_001",
                                "node_type": "source_document|bank_statement|prior_fs|statement_balance|bank_transaction|document_summary|other",
                                "document_refs": ["raw_001"],
                                "evidence_refs": ["page_001"],
                                "date": "YYYY-MM-DD or blank",
                                "amount": "decimal string or blank",
                                "description": "short evidence point without long filenames",
                                "support_level": "evidence_stated|evidence_derived|judgement",
                            }
                        ],
                        "derived_nodes": [
                            {
                                "node_id": "calc_001",
                                "meaning": "what was calculated",
                                "amount": "decimal string or blank",
                                "formula": "plain formula using evidence node labels",
                                "inputs": ["ev_001", "ev_002"],
                                "support_level": "evidence_derived|judgement",
                            }
                        ],
                        "matrix_hints": [
                            {
                                "account_name": "likely TB bridge row",
                                "movement_column": "accountant-style column name such as CBA, Westpac, Spire capital, Gain on sale of non-current assets",
                                "amount": "signed decimal string using accountant bridge sign convention if clear",
                                "support_type": "direct_evidence|evidence_derived|judgement|unsupported",
                                "reason": "short reason",
                            }
                        ],
                        "open_questions": ["only real open question, if any"],
                        "document_refs": ["raw_001"],
                        "why_it_matters_for_step4": "short bridge relevance",
                    }
                ],
                "prior_fs_account_movement_coverage": [
                    {
                        "account_name": "prior-year FS row or comparative line",
                        "statement_section": "Balance sheet|Profit and loss|Unknown",
                        "opening_or_comparative_amount": "decimal string or blank",
                        "coverage_status": "movement_explained|no_movement_found|source_only|bank_only|needs_attention|excluded|not_applicable",
                        "relationship_ids": ["rel_001"],
                        "movement_story": "one concise sentence explaining how this prior-FS row carries into Step 4",
                        "step4_column_hint": "optional hint only; Step 4 decides final movement column name",
                    }
                ],
                "investigation_log": ["short notes on approach"],
                "summary": {
                    "relationships": 0,
                    "prior_fs_accounts_considered": 0,
                    "ready_for_bridge": 0,
                    "needs_attention": 0,
                    "excluded": 0,
                },
            },
            "context": relationship_reasoning_context(source_index, coverage_payload),
            "recovery_context": recovery_context,
        },
        indent=2,
        sort_keys=True,
    )


def _valid_doc_refs(source_index: dict[str, Any]) -> set[str]:
    return {
        _text(document.get("document_id"))
        for document in _as_list(source_index.get("documents"))
        if isinstance(document, dict) and _text(document.get("document_id"))
    }


def validate_relationship_register(payload: dict[str, Any] | None, source_index: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return [{"category": "invalid_relationship_payload", "severity": "high", "message": "Codex did not return a JSON object."}]
    findings: list[dict[str, Any]] = []
    if payload.get("artifact_type") != "relationship_reasoning_register":
        findings.append({"category": "invalid_artifact_type", "severity": "high", "message": "Step 3 must return artifact_type relationship_reasoning_register."})
    if payload.get("relationship_reasoning_contract_version") != RELATIONSHIP_REASONING_CONTRACT_VERSION:
        findings.append({"category": "invalid_contract_version", "severity": "high", "message": f"Step 3 must use {RELATIONSHIP_REASONING_CONTRACT_VERSION}."})
    relationships = payload.get("relationships")
    if not isinstance(relationships, list):
        findings.append({"category": "invalid_relationships", "severity": "high", "message": "relationships must be a list."})
        return findings
    if not relationships:
        findings.append({"category": "empty_relationship_register", "severity": "high", "message": "Step 3 returned no relationships."})
    non_client_hits = _non_client_evidence_hits(
        {
            "relationships": payload.get("relationships"),
            "prior_fs_account_movement_coverage": payload.get("prior_fs_account_movement_coverage"),
            "investigation_log": payload.get("investigation_log"),
        }
    )
    for hit in non_client_hits[:8]:
        findings.append(
            {
                "category": "non_client_evidence_reference",
                "severity": "high",
                "redo_required": True,
                "message": (
                    "Step 3 output appears to cite knowhow, training material, or skills as evidence. "
                    "Skills may guide judgement only; cite client documents/prior FS as evidence instead."
                ),
                "path": hit["path"],
                "term": hit["term"],
                "value": hit["value"],
            }
        )
    coverage = payload.get("prior_fs_account_movement_coverage")
    if coverage is not None and not isinstance(coverage, list):
        findings.append({"category": "invalid_prior_fs_account_movement_coverage", "severity": "high", "message": "prior_fs_account_movement_coverage must be a list when supplied."})
    valid_docs = _valid_doc_refs(source_index)
    seen_ids: set[str] = set()
    for index, relationship in enumerate(relationships, start=1):
        if not isinstance(relationship, dict):
            findings.append({"category": "invalid_relationship", "severity": "high", "message": f"relationships[{index}] is not an object."})
            continue
        relationship_id = _text(relationship.get("relationship_id"))
        if not relationship_id:
            findings.append({"category": "missing_relationship_id", "severity": "medium", "message": f"relationships[{index}] is missing relationship_id."})
        elif relationship_id in seen_ids:
            findings.append({"category": "duplicate_relationship_id", "severity": "medium", "message": f"Duplicate relationship_id: {relationship_id}."})
        seen_ids.add(relationship_id)
        if _text(relationship.get("status")) not in {"ready_for_bridge", "needs_attention", "excluded", "informational"}:
            findings.append({"category": "invalid_relationship_status", "severity": "medium", "message": f"{relationship_id or index} has invalid status."})
        if _text(relationship.get("confidence")) not in {"high", "medium", "low"}:
            findings.append({"category": "invalid_confidence", "severity": "medium", "message": f"{relationship_id or index} must use high, medium, or low confidence."})
        if not _text(relationship.get("story")):
            findings.append({"category": "missing_story", "severity": "medium", "message": f"{relationship_id or index} needs an accountant-friendly story."})
        doc_refs = {_text(ref) for ref in _as_list(relationship.get("document_refs")) if _text(ref)}
        for node in _as_list(relationship.get("evidence_nodes")):
            if isinstance(node, dict):
                doc_refs.update(_text(ref) for ref in _as_list(node.get("document_refs")) if _text(ref))
        unknown_docs = sorted(ref for ref in doc_refs if valid_docs and ref not in valid_docs)
        if unknown_docs:
            findings.append({"category": "unknown_document_refs", "severity": "medium", "message": f"{relationship_id or index} references unknown document ids: {', '.join(unknown_docs[:8])}."})
    return findings


def normalise_relationship_register(payload: dict[str, Any], source_index: dict[str, Any], validation_findings: list[dict[str, Any]]) -> dict[str, Any]:
    relationships = [item for item in _as_list(payload.get("relationships")) if isinstance(item, dict)]
    prior_fs_coverage = [item for item in _as_list(payload.get("prior_fs_account_movement_coverage")) if isinstance(item, dict)]
    summary = {
        "documents_considered": len(_as_list(source_index.get("documents"))),
        "relationships": len(relationships),
        "prior_fs_accounts_considered": len(prior_fs_coverage),
        "ready_for_bridge": sum(1 for item in relationships if item.get("status") == "ready_for_bridge"),
        "needs_attention": sum(1 for item in relationships if item.get("status") == "needs_attention"),
        "excluded": sum(1 for item in relationships if item.get("status") == "excluded"),
        "informational": sum(1 for item in relationships if item.get("status") == "informational"),
        "validation_findings": len(validation_findings),
    }
    status = _text(payload.get("status")) or ("needs_attention" if summary["needs_attention"] or validation_findings else "ready")
    if status not in {"ready", "needs_attention"}:
        status = "needs_attention"
    return {
        "artifact_type": "relationship_reasoning_register",
        "register_artifact_type": "relationship_reasoning_register",
        "relationship_reasoning_contract_version": RELATIONSHIP_REASONING_CONTRACT_VERSION,
        "matching_contract_version": RELATIONSHIP_REASONING_CONTRACT_VERSION,
        "entity_name": source_index.get("entity_name") or "Uploaded documents",
        "status": status,
        "agent": "codex_cli",
        "relationships": relationships,
        "prior_fs_account_movement_coverage": prior_fs_coverage,
        "investigation_log": [str(item) for item in _as_list(payload.get("investigation_log"))],
        "summary": summary,
        "validation_findings": validation_findings,
    }


def failed_relationship_register(source_index: dict[str, Any], error: str, attempt_history: list[dict[str, Any]], validation_findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    findings = validation_findings or []
    return {
        "artifact_type": "relationship_reasoning_register",
        "register_artifact_type": "relationship_reasoning_register",
        "relationship_reasoning_contract_version": RELATIONSHIP_REASONING_CONTRACT_VERSION,
        "matching_contract_version": RELATIONSHIP_REASONING_CONTRACT_VERSION,
        "entity_name": source_index.get("entity_name") or "Uploaded documents",
        "status": "codex_failed",
        "agent": "codex_cli",
        "relationships": [
            {
                "relationship_id": "rel_codex_unavailable",
                "relationship_type": "unresolved_trace",
                "status": "needs_attention",
                "confidence": "low",
                "evidence_level": "no_accounting_event",
                "story": "Codex CLI could not produce the Step 3 relationship register.",
                "open_questions": [error],
                "document_refs": [],
            }
        ],
        "prior_fs_account_movement_coverage": [],
        "investigation_log": ["No deterministic fallback was used because Step 3 requires Codex CLI."],
        "summary": {"documents_considered": len(_as_list(source_index.get("documents"))), "relationships": 1, "ready_for_bridge": 0, "needs_attention": 1, "excluded": 0, "informational": 0, "validation_findings": len(findings)},
        "validation_findings": findings,
        "codex_attempt_history": attempt_history,
        "error": error,
    }


def format_relationship_register(payload: dict[str, Any]) -> str:
    summary = _as_dict(payload.get("summary"))
    lines = [f"# Step 3 Relationship Register - {payload.get('entity_name') or 'Uploaded documents'}", ""]
    lines.extend(
        [
            f"- Status: {payload.get('status', 'unknown')}",
            f"- Agent: {payload.get('agent', 'codex_cli')}",
            f"- Relationships: {summary.get('relationships', 0)}",
            f"- Prior-FS accounts considered: {summary.get('prior_fs_accounts_considered', 0)}",
            f"- Ready for bridge: {summary.get('ready_for_bridge', 0)}",
            f"- Needs attention: {summary.get('needs_attention', 0)}",
            f"- Excluded: {summary.get('excluded', 0)}",
            "",
        ]
    )
    coverage = [item for item in _as_list(payload.get("prior_fs_account_movement_coverage")) if isinstance(item, dict)]
    if coverage:
        lines.append("## Prior-FS Anchored Coverage")
        for item in coverage:
            detail = "; ".join(
                part
                for part in [
                    item.get("statement_section"),
                    item.get("opening_or_comparative_amount"),
                    item.get("coverage_status"),
                ]
                if part
            )
            lines.append(f"- {item.get('account_name', 'Prior-FS row')}: {item.get('movement_story', '')}")
            if detail:
                lines.append(f"  - {detail}")
        lines.append("")
    for item in _as_list(payload.get("relationships")):
        if not isinstance(item, dict):
            continue
        lines.append(f"- {item.get('relationship_id', 'relationship')}: {item.get('story', '')}")
        detail = "; ".join(part for part in [item.get("relationship_type"), item.get("status"), item.get("evidence_level"), item.get("amount")] if part)
        if detail:
            lines.append(f"  - {detail}")
        if item.get("open_questions"):
            lines.append(f"  - Open: {'; '.join(str(q) for q in _as_list(item.get('open_questions')))}")
    return "\n".join(lines).rstrip() + "\n"


def build_tb_bridge_prompt(
    relationship_register: dict[str, Any],
    source_index: dict[str, Any],
    prior_coa: dict[str, Any] | None,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict[str, Any]] | None = None,
    previous_payload: dict[str, Any] | None = None,
) -> str:
    recovery_context: dict[str, Any] = {}
    if recovery_attempt:
        redo_instruction = source_of_truth_redo_instruction(validation_findings)
        recovery_context = {
            "recovery_attempt": recovery_attempt,
            "previous_error": previous_error or "",
            "validation_findings_to_fix": validation_findings or [],
            "previous_payload_summary": _compact_recovery_payload(previous_payload or {}),
            "source_of_truth_redo_required": bool(redo_instruction),
            "instruction": redo_instruction or "Repair the JSON so it satisfies the TB bridge matrix contract. Do not use deterministic fallback.",
        }
    prior_accounts = []
    for account in _as_list(_as_dict(prior_coa).get("accounts")):
        if isinstance(account, dict):
            prior_accounts.append(
                {
                    "account_id": account.get("account_id") or "",
                    "code": account.get("code") or "",
                    "name": account.get("name") or "",
                    "type": account.get("type") or "",
                    "presentation_group": account.get("presentation_group") or "",
                    "opening_balance": account.get("opening_balance") or "",
                    "source_evidence_refs": _as_list(account.get("source_evidence_refs")),
                }
            )
    return json.dumps(
        {
            "role": "You are Codex CLI acting as a real junior accountant preparing a TB bridge matrix workpaper.",
            "task": (
                "Use the Step 3 relationship register and the single prior-year financial statement opening balances to build an accountant-style TB bridge matrix. "
                "This is not final financial statements. It is the bridge from prior FS opening balances through evidence-supported movements to draft closing positions."
            ),
            "response_contract": [
                "Return one valid JSON object only. Do not include Markdown, apologies, commentary, or a refusal-style explanation.",
                "Do not say you cannot complete the full workpaper. If evidence is incomplete, still return the complete schema with needs_attention rows, judgement support types, movement notes, and relationship_coverage entries.",
                "If time or context is tight, prioritise a structurally valid accountant-style bridge over perfection: include every prior-FS account row, create at least one balanced movement column or explicit needs_attention placeholder, and explain unresolved items in movement_notes.",
                "Never omit required top-level keys. A partial prose answer is worse than a conservative JSON workpaper with visible review notes.",
            ],
            "required_output_schema": {
                "artifact_type": "tb_bridge_workpaper",
                "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
                "status": "ready|needs_attention",
                "agent": "codex_cli",
                "accounts": [
                    {
                        "account_name": "row label",
                        "account_type": "asset|liability|equity|income|expense|clearing",
                        "statement_section": "Balance sheet|Profit and loss|Clearing / attention",
                        "statement_group": "Cash and Cash Equivalents|Receivables / Sundry Debtors|Investments|Other Assets|Payables and Accruals|Borrowings / Loans|Equity|Income|Expenses|Clearing|client-specific group",
                        "opening_balance": "decimal string for balance-sheet rows; 0.00 or blank for P&L rows",
                        "prior_year_comparative": "prior-year P&L comparative for income/expense rows; blank for balance-sheet rows",
                        "opening_source": "prior_fs|codex_new|codex_alias",
                        "reason": "short reason if new or aliased",
                    }
                ],
                "movement_columns": [
                    {
                        "column_key": "short stable key",
                        "label": "client-derived accountant adjustment column, e.g. bank name, disposal gain/loss, source investment/accrual, clear prior payable, accrue current expense, beneficiary/owner distribution",
                        "column_type": "legacy compatibility only; prefer movement_role.role_type",
                        "movement_role": {
                            "role_type": "one standard role_type from context.standard_movement_role_library, or extension_role",
                            "standard_role_name": "human readable role name from the library",
                            "accounting_purpose": "why this movement column exists in accounting terms",
                            "label_basis": "cash_account|prior_fs_account|source_counterparty|book_adjustment|owner_distribution|new_role",
                            "source_or_counterparty": "short source/payee/counterparty label if relevant",
                            "cash_account": "bank/provider/account nickname if relevant",
                            "new_role_proposal": {
                                "suggested_role_name": "only for extension_role",
                                "why_existing_roles_do_not_fit": "only for extension_role",
                                "affected_accounts": ["account names"],
                                "suggested_reuse_rule": "when this role should be reused for future clients",
                            },
                        },
                        "support_type": "direct_evidence|evidence_derived|judgement|unsupported",
                        "note_id": "N001",
                        "description": "one short sentence",
                    }
                ],
                "matrix_rows": [
                    {
                        "row_id": "row_001",
                        "account_name": "must match accounts.account_name",
                        "account_type": "asset|liability|equity|income|expense|clearing",
                        "statement_section": "Balance sheet|Profit and loss|Clearing / attention",
                        "statement_group": "same group as account",
                        "opening_balance": "decimal string for balance-sheet rows; 0.00 or blank for P&L rows",
                        "prior_year_comparative": "prior-year P&L comparative for income/expense rows; blank for balance-sheet rows",
                        "movements": [
                            {
                                "column_key": "must match movement_columns.column_key",
                                "amount": "signed decimal string",
                                "support_type": "direct_evidence|evidence_derived|judgement|unsupported",
                                "relationship_id": "rel id from Step 3",
                                "note_id": "N001",
                                "explanation": "very short cell note",
                            }
                        ],
                        "closing_balance": "decimal string",
                        "difference": "decimal string if known, otherwise blank",
                        "row_status": "ready|needs_attention|excluded",
                        "note_ids": ["N001"],
                        "notes": "short row note, not the full explanation",
                    }
                ],
                "movement_notes": [
                    {
                        "note_id": "N001",
                        "tb_row": 1,
                        "account_name": "exact account row from matrix_rows",
                        "statement_section": "Balance sheet|Profit and loss|Clearing / attention",
                        "statement_group": "row group from matrix_rows",
                        "status": "ready|needs_attention|not_posted|excluded",
                        "tb_column": "movement column label or Not posted",
                        "main_amount": "decimal string or blank",
                        "other_amounts": "short searchable amounts, e.g. 540000; 560053.96; 20053.96",
                        "opening_balance": "decimal string or blank",
                        "closing_balance": "decimal string or blank",
                        "explanation": "prior-FS account-led story for junior accountant: start from this TB row/opening balance, explain movements, evidence, and closing or unresolved treatment",
                        "calculation": "short formula or blank",
                        "evidence_summary": "source/bank evidence with short source names; include where to find support without long paths",
                        "relationship_ids": ["rel_001"],
                        "source_note_ids": ["optional original event note ids if this note combines them"],
                    }
                ],
                "relationship_coverage": [
                    {
                        "relationship_id": "rel id from Step 3",
                        "matrix_status": "included|not_posted_note|not_monetary|excluded|needs_attention",
                        "notes": "short note",
                    }
                ],
                "summary": {
                    "accounts": 0,
                    "movement_columns": 0,
                    "matrix_rows": 0,
                    "ready_rows": 0,
                    "needs_attention_rows": 0,
                },
                "workpaper_notes": ["short notes on approach"],
            },
            "context": {
                "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
                "entity_name": relationship_register.get("entity_name") or source_index.get("entity_name") or "Uploaded documents",
                "relationship_register": relationship_register,
                "prior_year_fs_source": {
                    "document_id": _as_dict(prior_coa).get("prior_fs_document_id", ""),
                    "display_name": _as_dict(prior_coa).get("prior_fs_display_name", ""),
                    "rule": "Use exactly one prior-year financial statement as the opening balance source.",
                },
                "prior_fs_accounts": prior_accounts,
                "source_documents": [_doc_ref(document, include_page_quotes=False) for document in _as_list(source_index.get("documents")) if isinstance(document, dict)],
                "matrix_sign_convention": "Positive amounts increase the row balance; negative amounts decrease the row balance.",
                "accounting_skill": load_accounting_skill_for_prompt("tb-bridge-preparation"),
                "accounting_pdf_retrieval_skill": load_accounting_skill_for_prompt(ACCOUNTING_PDF_KNOWLEDGE_SKILL),
                "accounting_pdf_topic_map": load_accounting_pdf_topic_map_for_prompt(),
                "accounting_pdf_retrieval_tool": accounting_pdf_retrieval_tool_for_prompt(),
                "client_evidence_guardrail": client_evidence_guardrail_for_prompt(),
                "standard_movement_role_library": _standard_movement_role_library_for_prompt(),
                "movement_note_explanation_patterns": [
                    "If a gross bank transfer pool is split between loan, owner, beneficiary, director, related-party, clearing, or other rows, explain the gross pool, each allocation, the residual, and what receiving support is missing.",
                    "If a prior-year balance is cleared through a different row or column, explain the opening amount, where it was cleared, and how that affects the amount left in the original row.",
                    "If an investment or asset sale clears prior carrying value and creates a gain/loss, explain proceeds or settlement, carrying value, known costs if they are posted, and the residual gain/loss.",
                    "If a receivable/payable/debtor/creditor roll-forward uses opening balance, current-year source entitlement or obligation, and cash receipts/payments, show the short formula and explain the closing residual.",
                    "If an invoice or service period crosses financial years, explain the expense/prepayment/accrual split and the date basis used.",
                    "If source amount and bank amount differ because of fees, withholding, GST, brokerage, timing, or netting, explain the gross amount, deductions/additions, and matched cash amount.",
                    "If a bank-only tax, BAS, ATO, ASIC, payroll, super, or regulatory item is posted, parked, or not posted, explain the support that is missing and why the draft treatment was chosen.",
                    "If valuation, NAV, tax-only, franking, withholding, offset, or disclosure-only information is not posted, explain that it is noted only unless a book-accounting treatment is confirmed.",
                    "If two reasonable accountant treatments exist, state the chosen draft treatment and the plausible alternative in one short sentence so a junior can review the judgement without reverse-engineering the workpaper.",
                ],
                "column_design_principles": [
                    "Do not use a fixed global column list. Derive column names from this client's prior FS rows, bank accounts, source documents, and accounting purpose.",
                    "Design columns as accountant adjustment buckets, not as one column per document, evidence event, or relationship id.",
                    "Prefer a compact matrix. Use the fewest columns that still make the bridge reviewable by a junior accountant.",
                    "Every movement column must declare movement_role.role_type using the standard movement role library. The role is the accounting meaning; the label is the client-specific accountant heading.",
                    "If none of the standard roles fit, use role_type extension_role with a learning brief explaining why the existing library does not fit, which accounts it affects, and when it should be reused. Do not silently invent a new untyped bucket.",
                    "Create bank columns for material bank accounts using the client-specific bank/provider names found in evidence. Example pattern: CBA and Westpac for one client; NAB and Macquarie for another.",
                    "Create disposal/gain-loss columns when prior-FS non-current assets or investments are sold. Label them by accounting purpose, not merely by source document.",
                    "Create source/accrual columns for material investment, receivable, payable, or income relationships when the source document drives the accounting movement.",
                    "Create prior-period cleanup columns only when a prior-FS payable/accrual/receivable is being cleared. Label them as clear PY [account/purpose] using this client's account names.",
                    "Create current-period accrual columns only when FY movement is supported but unpaid/unreceived at year end. Label them as accrue [account/purpose].",
                    "Create beneficiary, dividend, partner, or owner-distribution columns based on the entity type and prior-FS equity/liability structure.",
                    "If a large prior-FS loan, UPE, beneficiary account, director/shareholder loan, or related-party account exists, use it as the natural posting row for unexplained related-party/internal bank transfers when that is the most accountant-like bridge. Mark those movements judgement or needs_attention and state what counterparty support is missing.",
                    "Only create a new unresolved clearing row when no plausible prior-FS account exists, or posting to an existing account would be misleading.",
                    "Use Movement Notes to retain the detailed evidence trail, calculations, source document names, and judgement warnings that were compressed out of the main matrix.",
                    "Prefer human accountant workpaper headings over AI event headings. Column labels should look like CBA, Westpac, Gain on sale of non-current assets, Spire capital, Pick up accounting fee, Clear PY ASIC fee, Accrue ASIC fee, Distribution to beneficiary, or client-specific equivalents derived from evidence.",
                    "Use bank/cash columns for material cash-driven movements where that makes the bridge easier to follow. For example, a CBA or Westpac column can show the cash row side and the related investment, receivable, payable, loan, income, or expense side.",
                    "Use separate non-cash/source columns for accruals, gains/losses, source-only entitlements, fee pickups, prior-year clears, current-year accruals, and beneficiary/UPE distributions.",
                    "Do not combine cash movement, source entitlement, and gain/loss into one broad event column when a human accountant would split them across cash and accounting-adjustment columns.",
                    "Bank columns are not net-change plug columns. If a cash-supported relationship also has a book gain/loss, source entitlement, accrual, residual receivable, prepayment split, or beneficiary/UPE allocation, create a separate accounting-purpose column for that non-cash component.",
                    "A cash_account_movement column may pair the bank row with the related carrying-value, debtor, creditor, loan, income, or expense row, but it should not be the only column carrying a sale gain/loss or source-accrual story.",
                    "Avoid visible technical columns such as Opening cents true-up unless absolutely necessary. Prefer absorbing small cents differences into opening balances/rounding notes, or include them in Movement Notes only.",
                ],
                "rules": [
                    "Use prior FS accounts as the starting row set. Add new rows only when needed to explain FY25 movement or clearing.",
                    "Treat movement column design as a Step 4 accounting decision. Step 3 matrix_hints are suggestions only; choose final column names that make the TB bridge easiest for a junior accountant to review.",
                    "Split prior-year financial statement values by statement type. Balance-sheet accounts use the prior FS amount as the opening balance. P&L accounts such as income, revenue, expenses, and interest income use the prior FS amount only as prior_year_comparative and start the FY25 bridge from 0.00.",
                    "Do not create a fake Reset PY P&L movement column. The prior-year comparative column is separate from FY25 movements.",
                    "Order rows by statement section: Balance sheet first, Profit and loss second, Clearing / attention last. Within Balance sheet, use practical accounting groups such as Cash, Receivables, Investments, Payables/Accruals, Loans, and Equity. Within P&L, show Income before Expenses.",
                    "Use common groups but adapt to the client evidence. Assets may include Cash and Cash Equivalents, Receivables / Sundry Debtors, Investments, and Other Assets. Liabilities may include Payables and Accruals, Beneficiary Accounts, Borrowings / Loans, and Clearing.",
                    "Movement columns are balanced accountant-style book movements created from evidence, not fixed system buckets. Every movement column must sum to 0.00 across all matrix rows.",
                    "Prefer human-style accountant adjustment columns over event-style columns. For example, a bank account column may contain multiple bank-driven movements, while a disposal gain/loss column may contain several related sale rows. Do not create many narrow columns when one accountant adjustment bucket is clearer.",
                    "For cash-supported events, use a bank-named movement column (for example CBA or Westpac) for the cash/counterparty leg, then split every material non-cash component into its own accounting-purpose column.",
                    "For investment sale bridges, use at least two accountant-style roles when there is a gain/loss: a cash_account_movement column for the bank receipt/proceeds/carrying-value leg, and an asset_disposal_gain_loss column for the book gain/loss residual. Never post a gain/loss row through only a bank column.",
                    "For distribution receivable bridges, use at least two accountant-style roles when there is a source entitlement or closing receivable: a cash_account_movement column for bank receipts/opening receivable clearing and a source_entitlement_or_accrual, current_year_receivable_accrual, or distribution_receivable_clearance column for source-supported entitlements and residual receivables. Do not collapse opening debtor + current entitlement - cash receipts into one net bank movement.",
                    "Use client-specific labels generated from evidence. Do not copy examples as mandatory names. If this client has CBA/Westpac, those may be bank columns; if another client has NAB/Macquarie, use those instead. If this client has a Spire receivable, a Spire/source column may be useful; another client should get its own source/investment label.",
                    "A label may use a client source name only when the role genuinely needs that source label, such as source_entitlement_or_accrual. Sale/gain/loss, bank, payroll, tax, clearing, and accrual labels should usually describe accounting purpose first.",
                    "Use the Step 3 prior_fs_account_movement_coverage map to decide which prior-FS rows need movement columns, which need no movement, and which need attention notes.",
                    "Follow book/financial-statement movement logic, not a tax component workpaper. Do not post franking credits, TFN withholding, ESVCLP tax offsets, or other tax-only components into the TB matrix by default. Put them in movement_notes with status not_posted unless the source evidence clearly supports a book receivable/payable posting.",
                    "Do not post market value/NAV/valuation changes into the TB matrix by default. Put valuation information in movement_notes with status not_posted unless fair value accounting treatment is explicitly adopted. If adopted, the valuation column must still balance to zero.",
                    "Clearing rows are only for unresolved book/cash movements that must balance a movement column. Do not create GST clearing, ATO clearing, or theoretical clearing rows unless a real book/cash movement requires them. If an existing prior-FS loan/UPE/related-party row is the natural accountant destination, use that row with judgement support instead of creating a separate generic clearing row.",
                    "Do not collapse a relationship to its net residual when the accountant needs the gross bridge. Show each balanced movement column with all affected rows so the column totals to zero.",
                    "For receivable bridges, keep the column split accountant-friendly. A cash column can pair the bank receipt with receivable/loan/source rows, while a source_entitlement_or_accrual column can recognise source-supported entitlement and closing receivable. The junior accountant should be able to see both the bank collection and the source/accrual component without recalculating the note.",
                    "For investment sale bridges, keep the column split accountant-friendly. A bank column can pair proceeds with the investment carrying value, while a gain/loss column records the residual against the P&L gain/loss row. Do not let a bank column become the gain/loss column.",
                    "For bank rows, do not use a standalone bank roll-forward column unless it is balanced by the other side of the movement. The cash movement must be paired with an investment, income, expense, receivable, payable, loan, beneficiary, owner account, or unresolved clearing row.",
                    "For Spire-style split distributions, show the source-supported total and the cash-supported/residual parts through a balanced book movement. Use movement_notes to explain that the market value/NAV is not posted by default.",
                    "All Step 3 monetary relationships should be represented if possible, including ready, source-only, bank-only, and needs-attention relationships. Colour/support type will tell the accountant how reliable the cell is.",
                    "Use support_type direct_evidence for source/bank stated amounts, evidence_derived for arithmetic/roll-forward cells, judgement where accountant judgement is needed, and unsupported only for placeholders that still need tracing.",
                    "Do not hide useful components inside one net total. For grouped bank-only relationships such as KPMG/accounting fees, inspect the relationship evidence_nodes and either split the movement into useful bank/payee components or explain in the row note why only an aggregate is available.",
                    "If the user or source evidence refers to accounting fee pickup/accrual amounts and those exact amounts are not in the evidence set, do not invent them. Keep the KPMG/bank-only payments as supported movements and add a needs-attention note that fee pickup/accrual support is missing.",
                    "For investment sale rows, bridge opening investment cost to nil if Step 3 supports a full sale; keep the cash, investment, and gain/loss sides in one balanced sale column.",
                    "For receivable rows, combine prior opening debtor, cash receipts, source entitlement/accrual, and residual closing debtor through balanced columns. Do not show the row as a single net bank movement when source evidence creates a current-year entitlement or residual receivable.",
                    "For bank rows, reconcile opening to closing through the same balanced book movement columns; if annual movement is calculated from statements, explain that in movement_notes.",
                    "For loan/UPE/related-party rows, do not leave a large prior-FS account unchanged while moving related bank transfers to a separate unresolved row unless there is no reasonable link. If bank descriptions indicate internal transfers, unknown related accounts, beneficiary/owner funding, or related-party movement, post to the existing loan/UPE/related-party row using support_type judgement and a needs_attention note.",
                    "For beneficiary/UPE rows, use Step 3 client conventions and the draft P&L bridge. If evidence says 100% of distributable trust income goes to a beneficiary, calculate the current-year beneficiary distribution from the bridge when enough supported income/expense data exists. This beneficiary distribution must be a balanced movement column, not the same thing as ANZ/BENPI/Spire investment distributions. If it cannot be calculated, create an explicit needs-attention movement or row note; do not leave UPE unchanged silently.",
                    "For the TB Bridge, beneficiary/UPE distribution should be based on book profit from the matrix, not tax gross-up components. Do not add franking credits, TFN withholding, ESVCLP offsets, or tax-only items into the beneficiary distribution movement unless the accountant explicitly adopts that as a book posting.",
                    "Beneficiary/UPE movement notes may mention tax-only components only when the wording clearly says they are excluded, not posted, or noted only. If a posted beneficiary note says it includes/adds/grosses up franking credits, TFN withholding, ESVCLP offsets, or tax-only components, the validation repair pass will reject it.",
                    "If a technical accounting treatment is unclear, consult accounting_pdf_topic_map and accounting_pdf_retrieval_tool for original PDF guidance, then apply that guidance only to inspect/classify client evidence. Do not cite the knowhow PDF as workpaper support.",
                    "Relationship coverage must include every Step 3 relationship. Mark tax-only/valuation relationships as not_posted_note when they are useful notes but not TB movements. Mark non-monetary/excluded relationships as not_monetary or excluded, but do not omit them.",
                    "Create movement_notes primarily by TB Bridge row, in the same order as matrix_rows. The Movement Notes tab is a row-by-row explanation of the first tab, not a separate event register.",
                    "Each matrix row with material movement, nil movement, needs-attention status, or important not-posted context should have at least one movement note whose account_name exactly matches that row.",
                    "Movement notes should be account-led, not column-led: start with the prior-FS row/opening balance, explain what happened during the year, list the short source names or bank statements supporting it, and end with the closing/proposed treatment or open question.",
                    "For judgement-heavy, netted, split, or easily misunderstood rows, include a short 'why this number' sentence. Explain the gross pool or obvious source amount, the allocations/deductions, the residual, and any reasonable alternative treatment. Keep it generic and client-evidence based; do not hardcode example client names or amounts.",
                    "If one event affects several accounts, do not leave it as one floating note only. Mention the event in each relevant account-row note, with the account-specific side of the story.",
                    "Keep not-posted tax-only or valuation points under the relevant account row where possible. If there is no relevant row, create a clearly labelled not-posted note at the bottom.",
                    "Notes should be searchable by dollars and immediately explain the relationship/calculation.",
                    "Do not hide unexplained numbers. If an amount is not traced, keep it as needs_attention and say why.",
                    "Keep text short in the TB Bridge. Put longer explanations in movement_notes. Row notes should be a short pointer such as 'See N004.'",
                    "Never fabricate evidence or force balance by inventing a clearing row without labelling it judgement/unsupported.",
                ],
            },
            "recovery_context": recovery_context,
        },
        indent=2,
        sort_keys=True,
    )


def validate_tb_bridge_workpaper(payload: dict[str, Any] | None, relationship_register: dict[str, Any], prior_coa: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return [{"category": "invalid_tb_bridge_payload", "severity": "high", "message": "Codex did not return a JSON object."}]
    findings: list[dict[str, Any]] = []
    if payload.get("artifact_type") != "tb_bridge_workpaper":
        findings.append({"category": "invalid_artifact_type", "severity": "high", "message": "Step 4 must return artifact_type tb_bridge_workpaper."})
    if payload.get("tb_bridge_contract_version") != TB_BRIDGE_CONTRACT_VERSION:
        findings.append({"category": "invalid_contract_version", "severity": "high", "message": f"Step 4 must use {TB_BRIDGE_CONTRACT_VERSION}."})
    for key in ("accounts", "movement_columns", "matrix_rows", "movement_notes"):
        if not isinstance(payload.get(key), list):
            findings.append({"category": f"invalid_{key}", "severity": "high", "message": f"{key} must be a list."})
    if findings:
        return findings
    non_client_hits = _non_client_evidence_hits(
        {
            "accounts": payload.get("accounts"),
            "movement_columns": payload.get("movement_columns"),
            "matrix_rows": payload.get("matrix_rows"),
            "movement_notes": payload.get("movement_notes"),
            "relationship_coverage": payload.get("relationship_coverage"),
            "workpaper_notes": payload.get("workpaper_notes"),
        }
    )
    for hit in non_client_hits[:8]:
        findings.append(
            {
                "category": "non_client_evidence_reference",
                "severity": "high",
                "redo_required": True,
                "message": (
                    "Step 4 output appears to cite knowhow, training material, or skills as evidence. "
                    "Skills may guide judgement only; movement support must come from client documents, prior FS, or derived arithmetic."
                ),
                "path": hit["path"],
                "term": hit["term"],
                "value": hit["value"],
            }
        )
    account_names = {_text(account.get("account_name")) for account in _as_list(payload.get("accounts")) if isinstance(account, dict) and _text(account.get("account_name"))}
    column_keys = {_text(column.get("column_key")) for column in _as_list(payload.get("movement_columns")) if isinstance(column, dict) and _text(column.get("column_key"))}
    relationship_ids = {_text(item.get("relationship_id")) for item in _as_list(relationship_register.get("relationships")) if isinstance(item, dict) and _text(item.get("relationship_id"))}
    prior_names = {_text(account.get("name")) for account in _as_list(_as_dict(prior_coa).get("accounts")) if isinstance(account, dict) and _text(account.get("name"))}
    missing_prior = sorted(prior_names - account_names)
    if missing_prior:
        findings.append({"category": "missing_prior_fs_accounts", "severity": "high", "message": f"TB bridge accounts must include prior-FS accounts: {', '.join(missing_prior[:8])}."})
    if not column_keys:
        findings.append({"category": "no_movement_columns", "severity": "high", "message": "TB bridge needs at least one movement column."})
    findings.extend(_movement_role_validation_findings(payload))
    findings.extend(_non_cash_split_validation_findings(payload, relationship_register))
    note_ids = {_text(item.get("note_id")) for item in _as_list(payload.get("movement_notes")) if isinstance(item, dict) and _text(item.get("note_id"))}
    if not note_ids:
        findings.append({"category": "no_movement_notes", "severity": "high", "message": "TB bridge needs searchable movement_notes for workbook review."})
    for note in _as_list(payload.get("movement_notes")):
        if not isinstance(note, dict):
            continue
        beneficiary_tax_violation = _beneficiary_tax_boundary_violation(note)
        if beneficiary_tax_violation:
            findings.append(
                {
                    "category": "beneficiary_distribution_includes_tax_only_components",
                    "severity": "high",
                    "message": (
                        "Beneficiary/UPE movement must be based on book bridge profit. "
                        f"Move tax-only wording out of posted beneficiary note {beneficiary_tax_violation['note_id']} "
                        f"field {beneficiary_tax_violation['field']} unless it clearly says the tax-only component is excluded/not posted. "
                        "Franking credits, TFN withholding, ESVCLP offsets, and tax gross-ups belong in not-posted movement notes unless explicitly adopted as a book posting."
                    ),
                    "offending_note_id": beneficiary_tax_violation["note_id"],
                    "offending_field": beneficiary_tax_violation["field"],
                    "offending_text": beneficiary_tax_violation["value"],
                }
            )
    coverage_ids = {_text(item.get("relationship_id")) for item in _as_list(payload.get("relationship_coverage")) if isinstance(item, dict) and _text(item.get("relationship_id"))}
    missing_coverage = sorted(relationship_ids - coverage_ids)
    if missing_coverage:
        findings.append({"category": "missing_relationship_coverage", "severity": "high", "message": f"Relationship coverage must include every Step 3 relationship. Missing: {', '.join(missing_coverage[:10])}."})
    has_beneficiary_convention = any(
        "beneficiar" in json.dumps(item, sort_keys=True).casefold() and ("100" in json.dumps(item, sort_keys=True) or "distributable" in json.dumps(item, sort_keys=True).casefold())
        for item in _as_list(relationship_register.get("relationships"))
        if isinstance(item, dict)
    )
    has_beneficiary_movement = False
    column_totals: dict[str, Decimal] = {key: Decimal("0") for key in column_keys}
    for index, row in enumerate(_as_list(payload.get("matrix_rows")), start=1):
        if not isinstance(row, dict):
            findings.append({"category": "invalid_matrix_row", "severity": "high", "message": f"matrix_rows[{index}] is not an object."})
            continue
        account_name = _text(row.get("account_name"))
        if account_name and account_name not in account_names:
            findings.append({"category": "unknown_row_account", "severity": "high", "message": f"Matrix row uses account not in accounts: {account_name}."})
        opening_value = _decimal(row.get("opening_balance"))
        if _is_pnl_account_type(row.get("account_type")) and opening_value not in {None, Decimal("0")}:
            findings.append({"category": "pnl_opening_balance_not_zero", "severity": "high", "message": f"{account_name} is a P&L row and must not carry the prior-year amount as opening balance."})
        _decimal(row.get("closing_balance"))
        for movement in _as_list(row.get("movements")):
            if not isinstance(movement, dict):
                continue
            key = _text(movement.get("column_key"))
            if key and key not in column_keys:
                findings.append({"category": "unknown_movement_column", "severity": "high", "message": f"{account_name} references unknown movement column: {key}."})
            amount = _decimal(movement.get("amount"))
            if amount is None:
                findings.append({"category": "invalid_movement_amount", "severity": "high", "message": f"{account_name} has non-numeric movement amount."})
            elif key in column_totals:
                column_totals[key] += amount
            if "beneficiar" in (account_name + " " + _text(movement.get("column_key")) + " " + _text(movement.get("explanation"))).casefold() and amount not in {None, Decimal("0")}:
                has_beneficiary_movement = True
            movement_note_id = _text(movement.get("note_id"))
            if movement_note_id and note_ids and movement_note_id not in note_ids:
                findings.append({"category": "unknown_movement_note_id", "severity": "medium", "message": f"{account_name} movement references unknown movement note: {movement_note_id}."})
            rel_id = _text(movement.get("relationship_id"))
            if rel_id and relationship_ids and rel_id not in relationship_ids:
                findings.append({"category": "unknown_relationship_id", "severity": "medium", "message": f"{account_name} movement references unknown Step 3 relationship: {rel_id}."})
            if _text(movement.get("support_type")) not in {"direct_evidence", "evidence_derived", "judgement", "unsupported"}:
                findings.append({"category": "invalid_support_type", "severity": "medium", "message": f"{account_name} movement has invalid support_type."})
    if has_beneficiary_convention and not has_beneficiary_movement:
        findings.append(
            {
                "category": "beneficiary_distribution_not_bridged",
                "severity": "high",
                "message": "Client convention indicates beneficiary distribution/UPE should be addressed. Calculate the draft movement from the bridge or include an explicit non-zero needs-attention placeholder tied to the convention.",
            }
        )
    unbalanced = [(key, total) for key, total in sorted(column_totals.items()) if abs(total) > Decimal("0.01")]
    if unbalanced:
        shown = ", ".join(f"{key}={total:.2f}" for key, total in unbalanced[:10])
        findings.append(
            {
                "category": "unbalanced_movement_columns",
                "severity": "high",
                "message": f"Every Step 4 movement column must sum to 0.00. Unbalanced columns: {shown}.",
            }
        )
    return findings


def normalise_tb_bridge_workpaper(payload: dict[str, Any], relationship_register: dict[str, Any], validation_findings: list[dict[str, Any]]) -> dict[str, Any]:
    payload = _normalise_movement_column_roles(payload)
    accounts = [item for item in _as_list(payload.get("accounts")) if isinstance(item, dict)]
    movement_columns = [item for item in _as_list(payload.get("movement_columns")) if isinstance(item, dict)]
    matrix_rows = [item for item in _as_list(payload.get("matrix_rows")) if isinstance(item, dict)]
    movement_notes = [item for item in _as_list(payload.get("movement_notes")) if isinstance(item, dict)]
    summary = {
        "accounts": len(accounts),
        "movement_columns": len(movement_columns),
        "matrix_rows": len(matrix_rows),
        "movement_notes": len(movement_notes),
        "ready_rows": sum(1 for row in matrix_rows if row.get("row_status") == "ready"),
        "needs_attention_rows": sum(1 for row in matrix_rows if row.get("row_status") == "needs_attention"),
        "validation_findings": len(validation_findings),
    }
    status = _text(payload.get("status")) or ("needs_attention" if validation_findings or summary["needs_attention_rows"] else "ready")
    if status not in {"ready", "needs_attention"}:
        status = "needs_attention"
    return {
        "artifact_type": "tb_bridge_workpaper",
        "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
        "source_relationship_contract_version": relationship_register.get("relationship_reasoning_contract_version") or "",
        "entity_name": relationship_register.get("entity_name") or "Uploaded documents",
        "status": status,
        "agent": "codex_cli",
        "accounts": accounts,
        "movement_columns": movement_columns,
        "matrix_rows": matrix_rows,
        "movement_notes": movement_notes,
        "relationship_coverage": [item for item in _as_list(payload.get("relationship_coverage")) if isinstance(item, dict)],
        "summary": summary,
        "workpaper_notes": [str(item) for item in _as_list(payload.get("workpaper_notes"))],
        "validation_findings": validation_findings,
    }


def _row_led_movement_notes(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    original_notes = {
        _text(note.get("note_id")): note
        for note in _as_list(payload.get("movement_notes"))
        if isinstance(note, dict) and _text(note.get("note_id"))
    }
    column_labels = {
        _text(column.get("column_key")): _text(column.get("label")) or _text(column.get("column_key"))
        for column in _as_list(payload.get("movement_columns"))
        if isinstance(column, dict) and _text(column.get("column_key"))
    }
    row_notes: list[dict[str, Any]] = []
    row_note_ids: dict[str, str] = {}
    linked_original_note_ids: set[str] = set()
    for row_index, row in enumerate(_as_list(payload.get("matrix_rows")), start=1):
        if not isinstance(row, dict):
            continue
        account_name = _text(row.get("account_name"))
        if not account_name:
            continue
        movements = [movement for movement in _as_list(row.get("movements")) if isinstance(movement, dict)]
        source_note_ids = [_text(note_id) for note_id in _as_list(row.get("note_ids")) if _text(note_id)]
        source_note_ids.extend(_text(movement.get("note_id")) for movement in movements if _text(movement.get("note_id")))
        source_note_ids = [note_id for index, note_id in enumerate(source_note_ids) if note_id and note_id not in source_note_ids[:index]]
        linked_original_note_ids.update(source_note_ids)
        linked_notes = [original_notes[note_id] for note_id in source_note_ids if note_id in original_notes]
        row_note_id = f"R{row_index:03d}"
        row_note_ids[account_name] = row_note_id
        movement_phrases: list[str] = []
        movement_amounts: list[str] = []
        relationship_ids: list[str] = []
        support_statuses: list[str] = []
        for movement in movements:
            amount = _money_text(movement.get("amount"), _text(movement.get("amount")))
            column_label = column_labels.get(_text(movement.get("column_key")), _text(movement.get("column_key")))
            support = _text(movement.get("support_type"))
            explanation = _text(movement.get("explanation"))
            relationship_id = _text(movement.get("relationship_id"))
            if amount:
                movement_amounts.append(amount)
            if relationship_id:
                relationship_ids.append(relationship_id)
            if support:
                support_statuses.append(support)
            movement_phrases.append(_shorten(f"{column_label}: {amount} ({support or 'support not labelled'}). {explanation}", 260))
        relationship_ids.extend(
            str(rel_id)
            for note in linked_notes
            for rel_id in _as_list(note.get("relationship_ids"))
            if _text(rel_id)
        )
        row_status = _text(row.get("row_status")) or "ready"
        linked_statuses = {_text(note.get("status")) for note in linked_notes if _text(note.get("status"))}
        status = row_status
        if "needs_attention" in linked_statuses or row_status == "needs_attention" or "unsupported" in support_statuses or "judgement" in support_statuses:
            status = "needs_attention"
        elif row_status == "excluded" or "excluded" in linked_statuses:
            status = "excluded"
        elif "not_posted" in linked_statuses and not movements:
            status = "not_posted"
        opening = _money_text(row.get("opening_balance"), _text(row.get("opening_balance")))
        closing = _money_text(row.get("closing_balance"), _text(row.get("closing_balance")))
        comparative = _money_text(row.get("prior_year_comparative"), _text(row.get("prior_year_comparative")))
        row_start = f"Opening {opening}" if opening else ("Prior comparative " + comparative if comparative else "No opening amount")
        row_end = f"closing {closing}" if closing else "closing not finalised"
        source_explanations = [note.get("explanation") for note in linked_notes]
        explanation_parts = [
            f"{account_name}: {row_start}; {row_end}.",
            " Movements: " + " ".join(movement_phrases) if movement_phrases else " No FY movement identified from uploaded evidence.",
            " Context: " + _join_unique(source_explanations, limit=900) if source_explanations else "",
        ]
        calculations = [note.get("calculation") for note in linked_notes if _text(note.get("calculation"))]
        evidence = [note.get("evidence_summary") for note in linked_notes if _text(note.get("evidence_summary"))]
        all_amounts = [opening, comparative, *movement_amounts, closing]
        for note in linked_notes:
            all_amounts.append(note.get("main_amount"))
            all_amounts.append(note.get("other_amounts"))
        row_notes.append(
            {
                "note_id": row_note_id,
                "tb_row": row_index,
                "account_name": account_name,
                "statement_section": _text(row.get("statement_section")),
                "statement_group": _text(row.get("statement_group")),
                "status": status,
                "tb_column": _join_unique([column_labels.get(_text(m.get("column_key")), _text(m.get("column_key"))) for m in movements], limit=280) if movements else "No movement",
                "main_amount": _money_text(row.get("difference"), "") or (movement_amounts[0] if movement_amounts else ""),
                "other_amounts": _join_unique(all_amounts, limit=700),
                "opening_balance": opening,
                "closing_balance": closing,
                "explanation": _shorten("".join(explanation_parts), 1600),
                "calculation": _join_unique(calculations, limit=900) or (f"{opening} + movements = {closing}" if opening or closing else ""),
                "evidence_summary": _join_unique(evidence, limit=1100),
                "relationship_ids": [rel_id for index, rel_id in enumerate(relationship_ids) if rel_id and rel_id not in relationship_ids[:index]],
                "source_note_ids": source_note_ids,
            }
        )
    orphan_notes = [
        note
        for note_id, note in original_notes.items()
        if note_id not in linked_original_note_ids
    ]
    for offset, note in enumerate(orphan_notes, start=1):
        row_notes.append(
            {
                "note_id": f"R{len(row_notes) + 1:03d}",
                "tb_row": "",
                "account_name": "Not posted / other evidence",
                "statement_section": "Not posted",
                "statement_group": "",
                "status": _text(note.get("status")) or "not_posted",
                "tb_column": _text(note.get("tb_column")) or "Not posted",
                "main_amount": _text(note.get("main_amount")),
                "other_amounts": _text(note.get("other_amounts")),
                "opening_balance": "",
                "closing_balance": "",
                "explanation": _text(note.get("explanation")),
                "calculation": _text(note.get("calculation")),
                "evidence_summary": _text(note.get("evidence_summary")),
                "relationship_ids": _as_list(note.get("relationship_ids")),
                "source_note_ids": [_text(note.get("note_id")) or f"orphan_{offset:03d}"],
            }
        )
    return row_notes, row_note_ids


def _apply_row_led_movement_notes(payload: dict[str, Any]) -> dict[str, Any]:
    transformed = dict(payload)
    row_notes, row_note_ids = _row_led_movement_notes(transformed)
    transformed["movement_notes"] = row_notes
    rows: list[dict[str, Any]] = []
    for row in _as_list(transformed.get("matrix_rows")):
        if not isinstance(row, dict):
            continue
        normalised = dict(row)
        row_note_id = row_note_ids.get(_text(row.get("account_name")))
        if row_note_id:
            normalised["source_note_ids"] = _as_list(row.get("note_ids"))
            normalised["note_ids"] = [row_note_id]
            normalised["notes"] = f"See {row_note_id}."
            movements = []
            for movement in _as_list(normalised.get("movements")):
                if isinstance(movement, dict):
                    updated = dict(movement)
                    updated["source_note_id"] = updated.get("note_id")
                    updated["note_id"] = row_note_id
                    movements.append(updated)
            normalised["movements"] = movements
        rows.append(normalised)
    transformed["matrix_rows"] = rows
    summary = dict(_as_dict(transformed.get("summary")))
    summary["movement_notes"] = len(row_notes)
    transformed["summary"] = summary
    return transformed


def _prior_pnl_comparatives(prior_coa: dict[str, Any] | None) -> dict[str, str]:
    comparatives: dict[str, str] = {}
    for account in _as_list(_as_dict(prior_coa).get("accounts")):
        if not isinstance(account, dict) or not _is_pnl_account_type(account.get("type")):
            continue
        name = _text(account.get("name"))
        if name:
            comparatives[name] = _money_text(account.get("opening_balance"), "")
    return comparatives


def _split_pnl_comparatives(payload: dict[str, Any], prior_coa: dict[str, Any] | None = None) -> dict[str, Any]:
    """Keep BS openings separate from P&L prior-year comparatives."""
    comparatives = _prior_pnl_comparatives(prior_coa)
    transformed = dict(payload)

    def normalise_item(item: dict[str, Any]) -> dict[str, Any]:
        normalised = dict(item)
        account_type = normalised.get("account_type")
        statement_group = normalised.get("statement_group")
        section = _text(normalised.get("statement_section")) or _statement_section_for_account(account_type, statement_group)
        normalised["statement_section"] = section
        if _is_pnl_account_type(account_type):
            name = _text(normalised.get("account_name"))
            comparative = _text(normalised.get("prior_year_comparative")) or comparatives.get(name, "") or _money_text(normalised.get("opening_balance"), "")
            normalised["prior_year_comparative"] = comparative
            normalised["opening_balance"] = "0.00"
        else:
            normalised["prior_year_comparative"] = _text(normalised.get("prior_year_comparative"))
        return normalised

    accounts = [normalise_item(account) for account in _as_list(transformed.get("accounts")) if isinstance(account, dict)]
    rows: list[dict[str, Any]] = []
    used_column_keys: set[str] = set()
    for row in _as_list(transformed.get("matrix_rows")):
        if not isinstance(row, dict):
            continue
        normalised = normalise_item(row)
        if _is_pnl_account_type(normalised.get("account_type")):
            filtered_movements = []
            for movement in _as_list(normalised.get("movements")):
                if not isinstance(movement, dict):
                    continue
                column_key = _text(movement.get("column_key"))
                explanation = _text(movement.get("explanation")).casefold()
                if column_key in {"py_pnl_reset", "reset_py_pnl"} or "reset py" in explanation or "prior year reset" in explanation:
                    continue
                filtered_movements.append(movement)
            normalised["movements"] = filtered_movements
        for movement in _as_list(normalised.get("movements")):
            if isinstance(movement, dict) and _text(movement.get("column_key")):
                used_column_keys.add(_text(movement.get("column_key")))
        rows.append(normalised)

    columns = [
        column
        for column in _as_list(transformed.get("movement_columns"))
        if isinstance(column, dict)
        and _text(column.get("column_key")) not in {"py_pnl_reset", "reset_py_pnl"}
        and (_text(column.get("column_key")) in used_column_keys or not used_column_keys)
    ]

    def sort_key(item: dict[str, Any]) -> tuple[int, int, int, str]:
        account_type = _account_type(item.get("account_type"))
        type_order = {"asset": 0, "liability": 1, "equity": 2, "income": 3, "revenue": 3, "expense": 4, "clearing": 5}.get(account_type, 9)
        return (
            _section_order(item.get("statement_section")),
            type_order,
            _group_order(item.get("statement_group")),
            _text(item.get("account_name")).casefold(),
        )

    transformed["accounts"] = sorted(accounts, key=sort_key)
    transformed["matrix_rows"] = sorted(rows, key=sort_key)
    transformed["movement_columns"] = columns
    return transformed


def failed_tb_bridge_workpaper(relationship_register: dict[str, Any], error: str, attempt_history: list[dict[str, Any]], validation_findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "artifact_type": "tb_bridge_workpaper",
        "tb_bridge_contract_version": TB_BRIDGE_CONTRACT_VERSION,
        "entity_name": relationship_register.get("entity_name") or "Uploaded documents",
        "status": "codex_failed",
        "agent": "codex_cli",
        "accounts": [],
        "movement_columns": [],
        "matrix_rows": [],
        "movement_notes": [],
        "relationship_coverage": [],
        "summary": {"accounts": 0, "movement_columns": 0, "matrix_rows": 0, "movement_notes": 0, "ready_rows": 0, "needs_attention_rows": 0, "validation_findings": len(validation_findings or [])},
        "workpaper_notes": ["No deterministic fallback was used because Step 4 requires Codex CLI."],
        "validation_findings": validation_findings or [],
        "codex_attempt_history": attempt_history,
        "error": error,
    }


def format_tb_bridge_workpaper(payload: dict[str, Any]) -> str:
    summary = _as_dict(payload.get("summary"))
    lines = [f"# Step 4 TB Bridge Matrix - {payload.get('entity_name') or 'Uploaded documents'}", ""]
    lines.extend(
        [
            f"- Status: {payload.get('status', 'unknown')}",
            f"- Agent: {payload.get('agent', 'codex_cli')}",
            f"- Accounts: {summary.get('accounts', 0)}",
            f"- Movement columns: {summary.get('movement_columns', 0)}",
            f"- Movement notes: {summary.get('movement_notes', 0)}",
            f"- Matrix rows: {summary.get('matrix_rows', 0)}",
            f"- Needs attention rows: {summary.get('needs_attention_rows', 0)}",
            "",
        ]
    )
    for row in _as_list(payload.get("matrix_rows"))[:40]:
        if not isinstance(row, dict):
            continue
        lines.append(f"- {row.get('account_name', '')}: opening {row.get('opening_balance', '')} -> closing {row.get('closing_balance', '')} ({row.get('row_status', '')})")
    return "\n".join(lines).rstrip() + "\n"


def relationship_table_items(payload: dict[str, Any], docs_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _as_list(payload.get("relationships")):
        if not isinstance(item, dict):
            continue
        doc_refs = [str(ref) for ref in _as_list(item.get("document_refs")) if str(ref).strip()]
        for node in _as_list(item.get("evidence_nodes")):
            if isinstance(node, dict):
                doc_refs.extend(str(ref) for ref in _as_list(node.get("document_refs")) if str(ref).strip())
        documents = []
        seen: set[str] = set()
        for ref in doc_refs:
            doc = docs_by_id.get(ref)
            if doc and ref not in seen:
                documents.append(doc)
                seen.add(ref)
        rows.append(
            {
                "id": item.get("relationship_id") or f"rel_{len(rows) + 1:03d}",
                "relationshipType": item.get("relationship_type") or "",
                "status": item.get("status") or "",
                "confidence": item.get("confidence") or "",
                "evidenceLevel": item.get("evidence_level") or "",
                "story": item.get("story") or "",
                "date": item.get("date") or "",
                "amount": item.get("amount") or "",
                "direction": item.get("direction") or "",
                "accountsInvolved": _as_list(item.get("accounts_involved")),
                "evidenceNodes": _as_list(item.get("evidence_nodes")),
                "derivedNodes": _as_list(item.get("derived_nodes")),
                "matrixHints": _as_list(item.get("matrix_hints")),
                "openQuestions": _as_list(item.get("open_questions")),
                "whyItMattersForStep4": item.get("why_it_matters_for_step4") or "",
                "documents": documents,
                "technical": item,
            }
        )
    rows.sort(key=lambda row: ({"needs_attention": 0, "ready_for_bridge": 1, "informational": 2, "excluded": 3}.get(str(row.get("status")), 9), str(row.get("story", "")).casefold()))
    return rows


def matrix_preview(payload: dict[str, Any]) -> dict[str, Any]:
    columns = [column for column in _as_list(payload.get("movement_columns")) if isinstance(column, dict)]
    rows = [row for row in _as_list(payload.get("matrix_rows")) if isinstance(row, dict)]
    return {
        "artifactType": payload.get("artifact_type") or "",
        "status": payload.get("status") or "not_generated",
        "summary": _as_dict(payload.get("summary")),
        "columns": columns,
        "rows": rows,
        "movementNotes": [note for note in _as_list(payload.get("movement_notes")) if isinstance(note, dict)],
        "notes": _as_list(payload.get("workpaper_notes")),
        "validationFindings": _as_list(payload.get("validation_findings")),
    }


def write_tb_bridge_workbook_builder(output_dir: Path, node_modules_dir: str | None = None) -> Path:
    builder = output_dir / "build_tb_bridge_workpaper.mjs"
    node_modules = node_modules_dir or "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"
    builder.write_text(
        f"""import fs from 'node:fs/promises';
import path from 'node:path';
import {{ SpreadsheetFile, Workbook }} from '@oai/artifact-tool';

const outputDir = {json.dumps(str(output_dir.resolve()))};
const payload = JSON.parse(await fs.readFile(path.join(outputDir, {json.dumps(TB_BRIDGE_JSON)}), 'utf8'));
const workbook = Workbook.create();

function text(value) {{ return value === undefined || value === null ? '' : String(value); }}
function numberValue(value) {{
  const n = Number(text(value).replace(/[$,]/g, ''));
  return Number.isFinite(n) ? n : null;
}}
function money(value) {{
  const n = numberValue(value);
  return n === null ? '' : n;
}}
function colName(index) {{
  let name = '';
  let n = index + 1;
  while (n > 0) {{
    const r = (n - 1) % 26;
    name = String.fromCharCode(65 + r) + name;
    n = Math.floor((n - 1) / 26);
  }}
  return name;
}}
function writeTable(sheet, startRow, startCol, rows) {{
  if (!rows.length) return;
  sheet.getRangeByIndexes(startRow, startCol, rows.length, rows[0].length).values = rows;
}}
function styleHeader(range) {{
  range.format.fill.color = '#163f4d';
  range.format.font.color = '#ffffff';
  range.format.font.bold = true;
  range.format.wrapText = true;
}}
function styleSubHeader(range) {{
  range.format.fill.color = '#e7f1ef';
  range.format.font.color = '#12343b';
  range.format.font.bold = true;
}}
function styleCurrency(range) {{
  range.setNumberFormat('$#,##0.00;[Red]($#,##0.00);-');
}}
function supportFill(support) {{
  if (support === 'direct_evidence') return '#e8f6ef';
  if (support === 'evidence_derived') return '#eef4ff';
  if (support === 'judgement') return '#fff7df';
  if (support === 'unsupported') return '#fdecec';
  return '';
}}

const matrix = workbook.worksheets.add('TB Bridge');
matrix.showGridLines = false;
const columns = Array.isArray(payload.movement_columns) ? payload.movement_columns : [];
const rows = Array.isArray(payload.matrix_rows) ? payload.matrix_rows : [];
const headers = ['Section', 'Group', 'Account', 'Opening balance', 'PY comparative', ...columns.map(c => text(c.label || c.column_key)), 'Closing', 'Difference', 'Status', 'Note ID'];
const table = [headers];
for (const row of rows) {{
  const byColumn = new Map();
  for (const movement of Array.isArray(row.movements) ? row.movements : []) {{
    const key = text(movement.column_key);
    const existing = Number(byColumn.get(key)?.amount || 0);
    const amount = Number(money(movement.amount) || 0);
    const support = text(movement.support_type);
    byColumn.set(key, {{ amount: existing + amount, support, explanation: text(movement.explanation) }});
  }}
  table.push([
    text(row.statement_section),
    text(row.statement_group),
    text(row.account_name),
    money(row.opening_balance),
    money(row.prior_year_comparative),
    ...columns.map(c => {{
      const cell = byColumn.get(text(c.column_key));
      return cell ? cell.amount : '';
    }}),
    money(row.closing_balance),
    money(row.difference),
    text(row.row_status),
    Array.isArray(row.note_ids) && row.note_ids.length ? row.note_ids.join(', ') : text(row.notes),
  ]);
}}
writeTable(matrix, 0, 0, table);
const totalRow = rows.length + 1;
matrix.getCell(totalRow, 2).values = [['Column total']];
for (let c = 0; c < columns.length; c++) {{
  const col = colName(5 + c);
  matrix.getCell(totalRow, 5 + c).formulas = [[`=SUM(${{col}}2:${{col}}${{rows.length + 1}})`]];
}}
styleHeader(matrix.getRangeByIndexes(0, 0, 1, headers.length));
matrix.freezePanes.freezeRows(1);
matrix.freezePanes.freezeColumns(3);
styleCurrency(matrix.getRangeByIndexes(1, 3, Math.max(rows.length + 1, 1), Math.max(columns.length + 4, 1)));
matrix.getRangeByIndexes(0, 0, table.length + 1, headers.length).format.borders = {{ preset: 'all', style: 'thin', color: '#d8e2e7' }};
matrix.getRangeByIndexes(0, 0, table.length + 1, headers.length).format.wrapText = true;
for (let r = 0; r < rows.length; r++) {{
  const row = rows[r];
  const byColumn = new Map();
  for (const movement of Array.isArray(row.movements) ? row.movements : []) {{
    byColumn.set(text(movement.column_key), text(movement.support_type));
  }}
  const section = text(row.statement_section);
  const sectionCell = matrix.getCell(r + 1, 0);
  if (section === 'Balance sheet') sectionCell.format.fill.color = '#edf7f5';
  if (section === 'Profit and loss') sectionCell.format.fill.color = '#f1f5fb';
  if (section === 'Clearing / attention') sectionCell.format.fill.color = '#fff4e6';
  for (let c = 0; c < columns.length; c++) {{
    const fill = supportFill(byColumn.get(text(columns[c].column_key)));
    if (fill) matrix.getCell(r + 1, 5 + c).format.fill.color = fill;
  }}
  const status = text(row.row_status);
  if (status === 'needs_attention') matrix.getCell(r + 1, headers.length - 2).format.fill.color = '#fff4e6';
  if (status === 'ready') matrix.getCell(r + 1, headers.length - 2).format.fill.color = '#e8f6ef';
  if (status === 'excluded') matrix.getCell(r + 1, headers.length - 2).format.fill.color = '#f1f5f9';
}}
matrix.getRangeByIndexes(totalRow, 0, 1, headers.length).format.fill.color = '#f6f8fa';
matrix.getRangeByIndexes(totalRow, 0, 1, headers.length).format.font.bold = true;
matrix.getRangeByIndexes(0, 0, table.length + 1, headers.length).format.autofitColumns();
matrix.getRangeByIndexes(0, 0, table.length + 1, Math.min(headers.length, 12)).format.wrapText = true;
if (headers.length > 12) {{
  matrix.getRangeByIndexes(0, 12, table.length + 1, headers.length - 12).format.wrapText = false;
}}
matrix.getRangeByIndexes(0, 0, table.length + 1, Math.min(headers.length, 3)).format.font.bold = true;
matrix.getRange('A:A').format.columnWidth = 18;
matrix.getRange('B:B').format.columnWidth = 28;
matrix.getRange('C:C').format.columnWidth = 42;
matrix.getRangeByIndexes(0, headers.length - 1, table.length + 1, 1).format.columnWidth = 16;
matrix.getRangeByIndexes(0, headers.length - 1, table.length + 1, 1).format.wrapText = true;

const notes = workbook.worksheets.add('Movement Notes');
notes.showGridLines = false;
const noteRows = [['TB Row', 'Section', 'Group', 'Account', 'Status', 'Note ID', 'TB Column', 'Opening', 'Closing', 'Main amount', 'Other amounts', 'Explanation', 'Calculation', 'Evidence', 'Relationships']];
for (const note of Array.isArray(payload.movement_notes) ? payload.movement_notes : []) {{
  noteRows.push([
    text(note.tb_row),
    text(note.statement_section),
    text(note.statement_group),
    text(note.account_name),
    text(note.status),
    text(note.note_id),
    text(note.tb_column),
    money(note.opening_balance),
    money(note.closing_balance),
    money(note.main_amount),
    text(note.other_amounts),
    text(note.explanation),
    text(note.calculation),
    text(note.evidence_summary),
    Array.isArray(note.relationship_ids) ? note.relationship_ids.join(', ') : '',
  ]);
}}
writeTable(notes, 0, 0, noteRows);
styleHeader(notes.getRange('A1:O1'));
notes.freezePanes.freezeRows(1);
notes.freezePanes.freezeColumns(4);
notes.getRangeByIndexes(0, 0, noteRows.length, 15).format.borders = {{ preset: 'all', style: 'thin', color: '#d8e2e7' }};
styleCurrency(notes.getRangeByIndexes(1, 7, Math.max(noteRows.length - 1, 1), 3));
notes.getRange('A:O').format.autofitColumns();
notes.getRange('A:A').format.columnWidth = 10;
notes.getRange('B:B').format.columnWidth = 18;
notes.getRange('C:C').format.columnWidth = 24;
notes.getRange('D:D').format.columnWidth = 42;
notes.getRange('G:G').format.columnWidth = 30;
notes.getRange('K:K').format.columnWidth = 34;
notes.getRange('L:L').format.columnWidth = 78;
notes.getRange('M:M').format.columnWidth = 48;
notes.getRange('N:N').format.columnWidth = 54;
notes.getRange('A:O').format.wrapText = true;
const noteCount = Array.isArray(payload.movement_notes) ? payload.movement_notes.length : 0;
for (let r = 0; r < noteCount; r++) {{
  const note = payload.movement_notes[r];
  const status = text(note.status);
  if (status === 'needs_attention') notes.getCell(r + 1, 4).format.fill.color = '#fff4e6';
  if (status === 'ready') notes.getCell(r + 1, 4).format.fill.color = '#e8f6ef';
  if (status === 'excluded' || status === 'not_posted') notes.getCell(r + 1, 4).format.fill.color = '#f1f5f9';
}}

const evidence = workbook.worksheets.add('Evidence Index');
evidence.showGridLines = false;
const documentRows = [['Display name', 'Type', 'Entity relevance', 'Period / date', 'Summary', 'PDF']];
const linkFormulas = [];
function excelQuote(value) {{ return text(value).replace(/"/g, '""'); }}
function fileUrl(value) {{
  const raw = text(value);
  if (!raw) return '';
  const absolute = path.isAbsolute(raw) ? raw : path.resolve(raw);
  return 'file://' + absolute.split(path.sep).map(encodeURIComponent).join('/');
}}
for (const document of Array.isArray(payload.source_documents) ? payload.source_documents : []) {{
  documentRows.push([
    text(document.display_name),
    text(document.document_type),
    text(document.entity_relevance),
    [text(document.period_start), text(document.period_end)].filter(Boolean).join(' to ') || text(document.statement_date),
    text(document.summary),
    '',
  ]);
  const url = fileUrl(document.file_path);
  linkFormulas.push([url ? `=HYPERLINK("${{excelQuote(url)}}","Click here")` : '']);
}}
writeTable(evidence, 0, 0, documentRows);
if (linkFormulas.length) {{
  evidence.getRangeByIndexes(1, 5, linkFormulas.length, 1).formulas = linkFormulas;
}}
styleHeader(evidence.getRange('A1:F1'));
evidence.getRangeByIndexes(0, 0, documentRows.length, 6).format.borders = {{ preset: 'all', style: 'thin', color: '#d8e2e7' }};
evidence.getRange('A:F').format.autofitColumns();
evidence.getRange('A:A').format.columnWidth = 42;
evidence.getRange('E:E').format.columnWidth = 60;
evidence.getRange('A:F').format.wrapText = true;

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(path.join(outputDir, {json.dumps(TB_BRIDGE_XLSX)}));
console.log(path.join(outputDir, {json.dumps(TB_BRIDGE_XLSX)}));
""",
        encoding="utf-8",
    )
    node_modules_path = output_dir / "node_modules"
    source_modules = Path(node_modules)
    if source_modules.exists() and not node_modules_path.exists():
        try:
            node_modules_path.symlink_to(source_modules, target_is_directory=True)
        except FileExistsError:
            pass
    return builder


def repair_tb_bridge_workbook_hyperlinks(xlsx_path: Path) -> int:
    """Convert cached unsupported HYPERLINK formulas into real Excel hyperlinks.

    The artifact workbook engine can write HYPERLINK formulas but caches them as
    unsupported formula results. Excel will sometimes recalculate them, but the
    safer accountant-facing output is a normal hyperlink relationship with
    visible "Click here" text.
    """

    if not xlsx_path.exists():
        return 0

    main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    hyperlink_rel_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
    relationship_tag = f"{{{pkg_rel_ns}}}Relationship"
    ET.register_namespace("", main_ns)
    ET.register_namespace("r", rel_ns)

    formula_re = re.compile(r'^HYPERLINK\("(?P<url>(?:[^"]|"")*)","(?P<label>(?:[^"]|"")*)"\)$')

    with zipfile.ZipFile(xlsx_path, "r") as zin:
        workbook_xml = ET.fromstring(zin.read("xl/workbook.xml"))
        workbook_rels_xml = ET.fromstring(zin.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in workbook_rels_xml}

        evidence_target: str | None = None
        for sheet in workbook_xml.findall(f".//{{{main_ns}}}sheet"):
            if sheet.attrib.get("name") == "Evidence Index":
                rid = sheet.attrib.get(f"{{{rel_ns}}}id")
                target = rel_targets.get(rid or "")
                if target:
                    evidence_target = target.lstrip("/")
                    if not evidence_target.startswith("xl/"):
                        evidence_target = "xl/" + evidence_target
                break

        if not evidence_target or evidence_target not in zin.namelist():
            return 0

        sheet_xml = ET.fromstring(zin.read(evidence_target))
        repaired: list[tuple[str, str, str]] = []
        for cell in sheet_xml.findall(f".//{{{main_ns}}}c"):
            formula = cell.find(f"{{{main_ns}}}f")
            if formula is None or not formula.text:
                continue
            match = formula_re.match(formula.text)
            if not match:
                continue
            url = match.group("url").replace('""', '"')
            label = match.group("label").replace('""', '"') or "Click here"
            ref = cell.attrib.get("r")
            if not ref:
                continue

            for child in list(cell):
                cell.remove(child)
            cell.attrib["t"] = "inlineStr"
            inline = ET.SubElement(cell, f"{{{main_ns}}}is")
            text_node = ET.SubElement(inline, f"{{{main_ns}}}t")
            text_node.text = label
            repaired.append((ref, url, label))

        if not repaired:
            return 0

        existing_hyperlinks = sheet_xml.find(f"{{{main_ns}}}hyperlinks")
        if existing_hyperlinks is not None:
            sheet_xml.remove(existing_hyperlinks)
        hyperlinks = ET.Element(f"{{{main_ns}}}hyperlinks")

        rels_path = str(Path(evidence_target).parent / "_rels" / (Path(evidence_target).name + ".rels"))
        if rels_path in zin.namelist():
            sheet_rels_xml = ET.fromstring(zin.read(rels_path))
        else:
            sheet_rels_xml = ET.Element(f"{{{pkg_rel_ns}}}Relationships")

        existing_ids = {rel.attrib.get("Id", "") for rel in sheet_rels_xml}
        next_index = 1
        for ref, url, _label in repaired:
            while f"rIdHyperlink{next_index}" in existing_ids:
                next_index += 1
            rid = f"rIdHyperlink{next_index}"
            existing_ids.add(rid)
            next_index += 1
            ET.SubElement(
                sheet_rels_xml,
                relationship_tag,
                {
                    "Id": rid,
                    "Type": hyperlink_rel_type,
                    "Target": url,
                    "TargetMode": "External",
                },
            )
            ET.SubElement(hyperlinks, f"{{{main_ns}}}hyperlink", {"ref": ref, f"{{{rel_ns}}}id": rid})

        page_margins = sheet_xml.find(f"{{{main_ns}}}pageMargins")
        if page_margins is not None:
            sheet_xml.insert(list(sheet_xml).index(page_margins), hyperlinks)
        else:
            sheet_xml.append(hyperlinks)

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zout:
                written_rels = False
                for info in zin.infolist():
                    if info.filename == evidence_target:
                        zout.writestr(info, ET.tostring(sheet_xml, encoding="utf-8", xml_declaration=True))
                    elif info.filename == rels_path:
                        zout.writestr(info, ET.tostring(sheet_rels_xml, encoding="utf-8", xml_declaration=True))
                        written_rels = True
                    else:
                        zout.writestr(info, zin.read(info.filename))
                if not written_rels:
                    zout.writestr(rels_path, ET.tostring(sheet_rels_xml, encoding="utf-8", xml_declaration=True))
            shutil.move(str(tmp_path), xlsx_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        refresh_tb_bridge_inspect_hyperlink_labels(xlsx_path)
        return len(repaired)


_INSPECT_HYPERLINK_PLACEHOLDER_RE = re.compile(
    r"^HYPERLINK is not implemented\. linkLocation=(?P<url>.*?)(?:, friendlyName=(?P<label>.*))?$"
)


def _replace_inspect_hyperlink_placeholders(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        match = _INSPECT_HYPERLINK_PLACEHOLDER_RE.match(value)
        if not match:
            return value, 0
        label = (match.group("label") or "").strip() or "Click here"
        return label, 1
    if isinstance(value, list):
        replaced_count = 0
        replaced_values = []
        for item in value:
            replaced, count = _replace_inspect_hyperlink_placeholders(item)
            replaced_values.append(replaced)
            replaced_count += count
        return replaced_values, replaced_count
    if isinstance(value, dict):
        replaced_count = 0
        replaced_values: dict[str, Any] = {}
        for key, item in value.items():
            replaced, count = _replace_inspect_hyperlink_placeholders(item)
            replaced_values[key] = replaced
            replaced_count += count
        return replaced_values, replaced_count
    return value, 0


def refresh_tb_bridge_inspect_hyperlink_labels(xlsx_path: Path) -> int:
    """Align artifact-tool inspect output with repaired Excel hyperlinks.

    The workbook repair converts unsupported HYPERLINK formula cells into real
    hyperlink cells with visible "Click here" text. The artifact-tool inspect
    file is generated before that repair, so Turing can otherwise see stale
    placeholder values and report a presentation issue that no longer exists in
    the actual workbook.
    """

    inspect_path = Path(f"{xlsx_path}.inspect.ndjson")
    if not inspect_path.exists():
        return 0
    updated_lines: list[str] = []
    replacement_count = 0
    changed = False
    for line in inspect_path.read_text(errors="ignore").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            updated_lines.append(line)
            continue
        replaced, count = _replace_inspect_hyperlink_placeholders(payload)
        replacement_count += count
        changed = changed or count > 0
        updated_lines.append(json.dumps(replaced, sort_keys=True))
    if changed:
        inspect_path.write_text("\n".join(updated_lines) + "\n")
    return replacement_count


def enrich_tb_bridge_payload_for_workbook(payload: dict[str, Any], relationship_register: dict[str, Any], source_index: dict[str, Any], prior_coa: dict[str, Any] | None = None) -> dict[str, Any]:
    enriched = _split_pnl_comparatives(payload, prior_coa)
    enriched = _apply_row_led_movement_notes(enriched)
    enriched["tb_bridge_contract_version"] = TB_BRIDGE_CONTRACT_VERSION
    enriched["relationship_register"] = relationship_register
    relevance_overrides = _excluded_document_relevance_overrides(relationship_register)
    source_documents = [_doc_ref(document, include_page_quotes=False) for document in _as_list(source_index.get("documents")) if isinstance(document, dict)]
    for document in source_documents:
        override = relevance_overrides.get(_text(document.get("document_id")))
        if not override:
            continue
        document["entity_relevance"] = override["entity_relevance"]
        document["entity_relevance_reason"] = override["entity_relevance_reason"]
        review_flags = list(_as_list(document.get("review_flags")))
        flag = f"excluded by {override['relationship_id']}" if override.get("relationship_id") else "excluded by Step 3"
        if flag not in review_flags:
            review_flags.append(flag)
        document["review_flags"] = review_flags
    enriched["source_documents"] = source_documents
    enriched["workbook_generated_at"] = datetime.now(timezone.utc).isoformat()
    return enriched
