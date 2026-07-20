"""Reusable accounting movement-role grammar and validation."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from accountant_copilot.contract_utils import _as_dict, _as_list, _decimal, _text

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
