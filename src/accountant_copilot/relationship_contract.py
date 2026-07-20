"""Relationship reasoning prompt contract, validation and formatting."""
from __future__ import annotations

import json
import re
from pathlib import Path
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
    RELATIONSHIP_REASONING_CONTRACT_VERSION,
    _as_dict,
    _as_list,
    _compact_recovery_payload,
    _doc_ref,
    _text,
)

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
