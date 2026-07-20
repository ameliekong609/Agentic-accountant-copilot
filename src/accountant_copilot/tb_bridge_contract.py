"""TB bridge prompt contract, validation, formatting and workbook payload enrichment."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from accountant_copilot.accounting_knowledge import (
    ACCOUNTING_PDF_KNOWLEDGE_SKILL,
    accounting_pdf_retrieval_tool_for_prompt,
    client_evidence_guardrail_for_prompt,
    load_accounting_pdf_topic_map_for_prompt,
    load_accounting_skill_for_prompt,
    _non_client_evidence_hits,
    source_of_truth_redo_instruction,
)
from accountant_copilot.contract_utils import (
    TB_BRIDGE_CONTRACT_VERSION,
    _account_type,
    _as_dict,
    _as_list,
    _beneficiary_tax_boundary_violation,
    _compact_recovery_payload,
    _decimal,
    _doc_ref,
    _group_order,
    _is_pnl_account_type,
    _join_unique,
    _money_text,
    _section_order,
    _shorten,
    _statement_section_for_account,
    _text,
)
from accountant_copilot.movement_roles import (
    _movement_role_validation_findings,
    _non_cash_split_validation_findings,
    _normalise_movement_column_roles,
    _standard_movement_role_library_for_prompt,
)
from accountant_copilot.relationship_contract import _excluded_document_relevance_overrides

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
