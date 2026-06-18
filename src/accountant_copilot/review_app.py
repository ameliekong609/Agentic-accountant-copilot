"""Streamlit accountant review UI for the Agentic Accountant Copilot.

This app is intentionally a review front-end. It can stage uploaded source
files and download/apply review workbench decisions, but the same deterministic
CLI controls still validate and persist approvals.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # pragma: no cover - import availability is exercised by launching Streamlit.
    import streamlit as st
except ModuleNotFoundError:  # pragma: no cover
    st = None  # type: ignore[assignment]


DEFAULT_STATE = Path("outputs/streamlit_review_workspace/engagement_state.json")
DEFAULT_ARTIFACT_DIR = Path("outputs/streamlit_review_workspace")
DEFAULT_INPUT_DIR = Path("outputs/streamlit_review_workspace/uploads")


def _main_tab_labels() -> list[str]:
    return [
        "1 Upload source documents",
        "2 Intake & inventory",
        "3 Extract facts",
        "4 Match & review sources",
        "5 CoA & mappings",
        "6 Trial balance & statements",
        "7 Accountant review",
        "8 Final package",
        "9 Artifacts",
        "10 Apply decisions",
    ]


def _app_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    args, _unknown = parser.parse_known_args()
    return args


def _query_param(name: str, default: str) -> str:
    try:
        value = st.query_params.get(name)  # type: ignore[union-attr]
        return str(value or default)
    except Exception:
        return default


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _save_uploads(uploaded_files: list[Any], input_dir: Path) -> list[Path]:
    input_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for uploaded in uploaded_files:
        target = input_dir / uploaded.name
        target.write_bytes(uploaded.getbuffer())
        saved.append(target)
    return saved


def _dashboard_summary(artifact_dir: Path) -> dict[str, Any]:
    state = _load_json(artifact_dir / "engagement_state.json", {})
    blockers = _load_json(artifact_dir / "release_blockers.json", {"blockers": []}).get("blockers", [])
    tb = _load_json(artifact_dir / "post_journal_trial_balance.json", {})
    draft = _load_json(artifact_dir / "draft_statements" / "draft_statements.json", {})
    release_manifest = artifact_dir / "release_candidate" / "release_candidate_manifest.json"
    final_manifest = artifact_dir / "final_release_manifest.json"
    coa_pending = sum(1 for account in state.get("chart_accounts", []) if account.get("status") != "approved")
    approved_journals = sum(1 for journal in state.get("adjustment_proposals", []) if journal.get("status") == "approved")
    source_review_count = len(_source_review_items(artifact_dir))
    tb_summary = tb.get("summary", {})
    draft_findings = draft.get("findings", [])
    if source_review_count:
        next_action = "Review source extraction issues before relying on extracted facts."
    elif coa_pending:
        next_action = "Review and approve pending CoA accounts."
    elif blockers:
        next_action = "Review release blockers and accountant decisions."
    elif not release_manifest.exists():
        next_action = "Build release candidate."
    elif not final_manifest.exists():
        next_action = "Complete final sign-off and export final package."
    else:
        next_action = "Final output package is available."
    return {
        "entity_name": state.get("entity_name", "New engagement"),
        "fy_start": state.get("fy_start", ""),
        "fy_end": state.get("fy_end", ""),
        "documents": len(state.get("source_documents", [])),
        "release_blockers": len(blockers),
        "source_review_items": source_review_count,
        "coa_pending": coa_pending,
        "approved_journals": approved_journals,
        "tb_balanced": tb_summary.get("is_balanced"),
        "pending_journals_excluded": tb_summary.get("pending_journals_excluded"),
        "draft_status": draft.get("status", "missing"),
        "draft_findings": len(draft_findings) if isinstance(draft_findings, list) else draft_findings,
        "release_candidate": release_manifest.exists(),
        "final_manifest": final_manifest.exists(),
        "next_action": next_action,
    }


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    return subprocess.run([sys.executable, "-m", "accountant_copilot.cli", *args], cwd=cwd, env=env, text=True, capture_output=True, check=False)


def _step_path_after_option(step: dict[str, Any], option: str) -> Path | None:
    commands = step.get("command")
    if not commands:
        return None
    command_list = commands if isinstance(commands[0], list) else [commands]
    for command in command_list:
        for idx, value in enumerate(command):
            if value == option and idx + 1 < len(command):
                return Path(command[idx + 1])
    return None


def _step_state_path(step: dict[str, Any]) -> Path | None:
    return _step_path_after_option(step, "--state")


def _step_input_dir(step: dict[str, Any]) -> Path | None:
    return _step_path_after_option(step, "--input-dir")


def _ensure_engagement_state_for_step(step: dict[str, Any]) -> Path | None:
    input_dir = _step_input_dir(step)
    if input_dir is not None:
        input_dir.mkdir(parents=True, exist_ok=True)
    state_path = _step_state_path(step)
    if state_path is None or state_path.exists():
        return state_path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    initial_state = {
        "engagement_id": "streamlit_local",
        "entity_name": "Uploaded engagement",
        "entity_type": None,
        "fy_start": "",
        "fy_end": "",
        "exceptions": [],
        "decisions": [],
        "preferences": [],
        "evidence": [],
        "source_documents": [],
        "chart_accounts": [],
        "adjustment_proposals": [],
        "output_artifacts": [],
        "state_transitions": [],
        "agent_tasks": [],
        "coa_review_required": False,
        "coa_review_status": "not_required",
        "adjustment_review_status": "not_started",
        "lifecycle_status": "intake",
    }
    state_path.write_text(json.dumps(initial_state, indent=2, sort_keys=True))
    return state_path


def _workflow_steps(input_dir: str, artifact_dir: str, state_path: str) -> list[dict[str, Any]]:
    return [
        {
            "label": "Process documents and build inventory",
            "description": "Register uploaded source documents, extract page-level evidence, and summarize the document inventory for accountant review.",
            "user_output": "Document inventory is ready for review.",
            "review_action": "Review the detected document list now. If document types/pages look right, continue to extraction.",
            "command": [
                ["ingest-raw-inputs", "--state", state_path, "--input-dir", input_dir],
                ["export-document-inventory", "--state", state_path, "--output", f"{artifact_dir}/document_inventory.md"],
            ],
            "outputs": [state_path, f"{artifact_dir}/document_inventory.md"],
        },
        {
            "label": "Extract accounting facts",
            "description": "Extract bank, invoice, distribution/tax, and broker trade facts from source evidence.",
            "user_output": "Accounting facts are ready; extraction review items are listed separately.",
            "review_action": "Review extracted facts first, then any extraction review items before matching.",
            "command": [
                ["export-bank-statement-facts", "--state", state_path, "--output", f"{artifact_dir}/bank_statement_facts.md"],
                ["export-bank-transactions", "--state", state_path, "--output", f"{artifact_dir}/bank_transactions.md"],
                ["export-invoice-facts", "--state", state_path, "--output", f"{artifact_dir}/invoice_facts.md"],
                ["export-distribution-tax-facts", "--state", state_path, "--output", f"{artifact_dir}/distribution_tax_facts.md"],
                ["export-broker-trade-facts", "--state", state_path, "--output", f"{artifact_dir}/broker_trade_facts.md"],
            ],
            "outputs": [
                f"{artifact_dir}/bank_statement_facts.json",
                f"{artifact_dir}/bank_transactions.json",
                f"{artifact_dir}/invoice_facts.json",
                f"{artifact_dir}/distribution_tax_facts.json",
                f"{artifact_dir}/broker_trade_facts.json",
            ],
        },
        {
            "label": "Match source facts",
            "description": "Match invoice, distribution/tax, and broker facts to bank transaction evidence.",
            "user_output": "Source fact matches are ready.",
            "review_action": "Review unmatched or uncertain matches now; continue only when expected matches look sensible.",
            "command": [
                "match-source-facts",
                "--bank-transactions",
                f"{artifact_dir}/bank_transactions.json",
                "--invoice-facts",
                f"{artifact_dir}/invoice_facts.json",
                "--distribution-tax-facts",
                f"{artifact_dir}/distribution_tax_facts.json",
                "--broker-trade-facts",
                f"{artifact_dir}/broker_trade_facts.json",
                "--output",
                f"{artifact_dir}/source_fact_matches.md",
            ],
            "outputs": [f"{artifact_dir}/source_fact_matches.json"],
        },
        {
            "label": "Build CoA and mappings",
            "description": "Import candidate accounts and suggest unapproved source-fact-to-CoA mappings.",
            "user_output": "CoA and mapping suggestions are ready.",
            "review_action": "Review and approve CoA/mapping suggestions now.",
            "command": [
                ["import-coa-from-prior-statements", "--state", state_path, "--output", f"{artifact_dir}/prior_statement_coa_import.md"],
                [
                    "suggest-coa-mappings",
                    "--state",
                    state_path,
                    "--invoice-facts",
                    f"{artifact_dir}/invoice_facts.json",
                    "--distribution-tax-facts",
                    f"{artifact_dir}/distribution_tax_facts.json",
                    "--broker-trade-facts",
                    f"{artifact_dir}/broker_trade_facts.json",
                    "--output",
                    f"{artifact_dir}/coa_mapping_suggestions.md",
                ],
            ],
            "outputs": [f"{artifact_dir}/prior_statement_coa_import.md", f"{artifact_dir}/coa_mapping_suggestions.json"],
        },
        {
            "label": "Build review packet",
            "description": "Refresh release blockers, accountant workbench, review UI bundle, and review packet links.",
            "user_output": "Accountant review packet is ready.",
            "review_action": "Review blockers, CoA decisions, journals, draft approval, and sign-off now.",
            "command": [
                ["export-accountant-review-workbench", "--state", state_path, "--artifact-dir", artifact_dir, "--output", f"{artifact_dir}/accountant_review_workbench.json"],
                ["explain-release-blockers", "--state", state_path, "--artifact-dir", artifact_dir, "--output", f"{artifact_dir}/release_blockers.md"],
                ["export-review-ui-bundle", "--state", state_path, "--artifact-dir", artifact_dir, "--output-dir", f"{artifact_dir}/review_ui_bundle"],
            ],
            "outputs": [f"{artifact_dir}/accountant_review_workbench.json", f"{artifact_dir}/release_blockers.json"],
        },
        {
            "label": "Build reviewed TB and draft statements",
            "description": "Export reviewed journals, build post-journal TB, preview statement mapping, and render internal-review draft statements.",
            "user_output": "Reviewed trial balance and internal draft statements are ready.",
            "review_action": "Review the trial balance and draft statements now.",
            "command": [
                ["export-reviewed-journals", "--state", state_path, "--output-dir", f"{artifact_dir}/reviewed_journals"],
                ["build-post-journal-tb", "--state", state_path, "--reviewed-journals", f"{artifact_dir}/reviewed_journals/reviewed_journals.json", "--output", f"{artifact_dir}/post_journal_trial_balance.md"],
                ["preview-statement-line-mapping", "--post-journal-tb", f"{artifact_dir}/post_journal_trial_balance.json", "--output", f"{artifact_dir}/statement_line_mapping.md"],
                ["render-draft-statements-from-tb", "--post-journal-tb", f"{artifact_dir}/post_journal_trial_balance.json", "--mapping", f"{artifact_dir}/statement_line_mapping.json", "--output-dir", f"{artifact_dir}/draft_statements"],
            ],
            "outputs": [
                f"{artifact_dir}/reviewed_journals/reviewed_journals.json",
                f"{artifact_dir}/post_journal_trial_balance.json",
                f"{artifact_dir}/statement_line_mapping.json",
                f"{artifact_dir}/draft_statements/draft_statements.json",
            ],
        },
        {
            "label": "Build release candidate",
            "description": "Package controlled release artifacts after accountant approvals clear blockers.",
            "user_output": "Release candidate package is ready.",
            "review_action": "Review release package blockers now.",
            "command": ["build-release-candidate-package", "--state", state_path, "--artifact-dir", artifact_dir, "--output-dir", f"{artifact_dir}/release_candidate"],
            "outputs": [f"{artifact_dir}/release_candidate/release_candidate_manifest.json"],
        },
        {
            "label": "Final export",
            "description": "Export final manifest only after final sign-off and clean release-candidate verification.",
            "user_output": "Final release manifest is ready.",
            "review_action": "Review final output package now.",
            "command": [
                "export-final-release-manifest",
                "--state",
                state_path,
                "--release-candidate",
                f"{artifact_dir}/release_candidate/release_candidate_manifest.json",
                "--output",
                f"{artifact_dir}/final_release_manifest.json",
            ],
            "outputs": [f"{artifact_dir}/final_release_manifest.json"],
        },
    ]


def _run_step_command(command: Any, cwd: Path) -> list[subprocess.CompletedProcess[str]]:
    commands = command if command and isinstance(command[0], list) else [command]
    return [_run_cli(list(args), cwd) for args in commands]


def _workflow_stage_groups(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_label = {step["label"]: step for step in steps}
    definitions = [
        ("2 Intake & inventory", ["Process documents and build inventory"]),
        ("3 Extract facts", ["Extract accounting facts"]),
        ("4 Match & review sources", ["Match source facts"]),
        ("5 CoA & mappings", ["Build CoA and mappings"]),
        ("6 Trial balance & statements", ["Build reviewed TB and draft statements"]),
        ("7 Accountant review", ["Build review packet"]),
        ("8 Final package", ["Build release candidate", "Final export"]),
    ]
    return [{"title": title, "steps": [by_label[label] for label in labels if label in by_label]} for title, labels in definitions]


def _workflow_step_button_key(stage_title: str, step_index: int, step: dict[str, Any]) -> str:
    stage_slug = re.sub(r"[^a-z0-9]+", "_", stage_title.lower()).strip("_")
    step_slug = re.sub(r"[^a-z0-9]+", "_", step["label"].lower()).strip("_")
    return f"run_step_{stage_slug}_{step_index}_{step_slug}"


def _workflow_output_readiness_text(label: str, outputs_present: int, outputs_total: int) -> str:
    if outputs_total == 0:
        return "This step has no separate review file."
    if outputs_present >= outputs_total:
        if label.endswith("."):
            return label
        return f"{label} is ready to review."
    return f"{label} is not ready yet."


def _workflow_result_summary(step: dict[str, Any], results: list[subprocess.CompletedProcess[str]], outputs_present: int, outputs_total: int) -> dict[str, Any]:
    failures = [result for result in results if result.returncode != 0]
    if failures and outputs_total and outputs_present >= outputs_total:
        return {
            "status": "Needs review",
            "message": step.get("review_action", "Review this step output before continuing."),
            "show_technical_output": False,
        }
    if failures:
        return {
            "status": "Needs attention",
            "message": "This step needs attention before continuing.",
            "show_technical_output": False,
        }
    if outputs_total and outputs_present < outputs_total:
        return {
            "status": "Check outputs",
            "message": "This step ran, but the expected output is not available yet.",
            "show_technical_output": False,
        }
    return {
        "status": "Done",
        "message": step.get("user_output", "This step is complete."),
        "show_technical_output": False,
    }


def _source_documents_by_id(artifact_dir: Path) -> dict[str, dict[str, Any]]:
    state = _load_json(artifact_dir / "engagement_state.json", {})
    return {doc.get("document_id", ""): doc for doc in state.get("source_documents", [])}


def _source_review_items(artifact_dir: Path) -> list[dict[str, Any]]:
    docs = _source_documents_by_id(artifact_dir)
    layers = [
        ("invoice", artifact_dir / "invoice_facts.json"),
        ("distribution/tax", artifact_dir / "distribution_tax_facts.json"),
        ("broker trade", artifact_dir / "broker_trade_facts.json"),
        ("bank statement", artifact_dir / "bank_statement_facts.json"),
    ]
    items: list[dict[str, Any]] = []
    for layer, path in layers:
        for finding in _load_json(path, {}).get("findings", []):
            doc_id = finding.get("document_id", "")
            doc = docs.get(doc_id, {})
            actual_type = doc.get("document_type", "unknown")
            issue_type = "incomplete extraction"
            if layer == "invoice" and actual_type == "broker_confirmation":
                issue_type = "wrong document-type candidate"
            items.append({
                "layer": layer,
                "document_id": doc_id,
                "file_path": doc.get("file_path", "unknown"),
                "document_type": actual_type,
                "issue_type": issue_type,
                "category": finding.get("category", "finding"),
                "missing_fields": ", ".join(finding.get("missing_fields", [])),
                "evidence_id": finding.get("evidence_id", ""),
                "recommended_action": finding.get("recommended_action", "Review source evidence and record accountant decision."),
                "blocks_release": True,
            })
    return items


def _source_resolution_payload(issue: dict[str, Any], action: str, reviewer: str, rationale: str) -> dict[str, Any]:
    return {
        "document_id": issue.get("document_id", ""),
        "file_path": issue.get("file_path", ""),
        "layer": issue.get("layer", ""),
        "issue_type": issue.get("issue_type", ""),
        "action": action,
        "reviewer": reviewer,
        "rationale": rationale,
        "recommended_action": issue.get("recommended_action", ""),
        "blocks_release": action not in {"mark_out_of_scope", "accept_risk", "resolved"},
    }


def _save_source_resolution(artifact_dir: Path, payload: dict[str, Any]) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "source_issue_resolutions.json"
    existing = _load_json(path, {"resolutions": []})
    resolutions = existing.setdefault("resolutions", [])
    resolutions.append(payload)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True))
    return path


def _final_package_preview(artifact_dir: Path) -> list[dict[str, Any]]:
    candidates = [
        ("Draft financial statements", "statement", artifact_dir / "draft_statements" / "draft_statements.md"),
        ("Release candidate manifest", "manifest", artifact_dir / "release_candidate" / "release_candidate_manifest.json"),
        ("Final release manifest", "manifest", artifact_dir / "final_release_manifest.json"),
        ("Review packet", "workpaper", artifact_dir / "review_packet" / "README.md"),
    ]
    return [{"label": label, "kind": kind, "path": path} for label, kind, path in candidates if path.exists()]


def _document_inventory_rows(artifact_dir: Path) -> list[dict[str, Any]]:
    inventory = _load_json(artifact_dir / "document_inventory.json", {"documents": []})
    rows: list[dict[str, Any]] = []
    for doc in inventory.get("documents", []):
        file_path = str(doc.get("file_path", ""))
        rows.append({
            "document_id": doc.get("document_id", ""),
            "file_name": Path(file_path).name or file_path,
            "document_type": doc.get("document_type", "unknown"),
            "pages": len(doc.get("pages", [])),
            "evidence_count": doc.get("evidence_count", 0),
            "status": doc.get("status", "unknown"),
            "review": "looks_ok",
        })
    return rows


def _coa_review_rows(workbench: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for account in workbench.get("sections", {}).get("coa_accounts", []):
        rows.append({
            "account_id": str(account.get("account_id", "")),
            "code": str(account.get("code", "")),
            "name": str(account.get("name", "")),
            "action": str(account.get("action", "")),
            "approved_by": str(account.get("approved_by", "")),
            "rationale": str(account.get("rationale", "")),
        })
    return rows


def _accounting_fact_summary(fact_type: str, fact: dict[str, Any]) -> str:
    if fact_type == "bank_statement":
        account = fact.get("account_number") or fact.get("account_number_raw") or fact.get("account_key_raw") or "bank account"
        period = fact.get("statement_period") or " → ".join(x for x in [fact.get("statement_period_start"), fact.get("statement_period_end")] if x)
        parts = [f"Account {account}"]
        if period:
            parts.append(f"period {period}")
        if fact.get("closing_balance"):
            parts.append(f"closing balance {fact.get('closing_balance')}")
        return "; ".join(parts)
    if fact_type == "bank_transaction":
        amount = fact.get("credit") or fact.get("debit") or "amount not extracted"
        direction = "credit" if fact.get("credit") else "debit" if fact.get("debit") else "transaction"
        return "; ".join(x for x in [fact.get("transaction_date"), fact.get("description"), f"{direction} {amount}"] if x)
    if fact_type == "invoice":
        return "; ".join(x for x in [fact.get("supplier"), fact.get("invoice_number"), fact.get("invoice_date"), f"amount {fact.get('amount_due')}" if fact.get("amount_due") else None, f"GST {fact.get('gst')}" if fact.get("gst") else None] if x)
    if fact_type == "distribution_tax":
        name = fact.get("investment_name") or fact.get("document_type") or "distribution/tax item"
        return "; ".join(x for x in [name, fact.get("security_code"), f"payment {fact.get('payment_date')}" if fact.get("payment_date") else None, f"amount {fact.get('amount')}" if fact.get("amount") else None] if x)
    if fact_type == "broker_trade":
        fields = fact.get("fields", {}) if isinstance(fact.get("fields"), dict) else {}
        return "; ".join(x for x in [fact.get("side"), fields.get("security"), f"settlement {fields.get('settlement_date')}" if fields.get("settlement_date") else None, f"amount {fields.get('settlement_amount')}" if fields.get("settlement_amount") else None] if x)
    return "Extracted accounting fact"


def _accounting_fact_rows(artifact_dir: Path) -> list[dict[str, str]]:
    state = _load_json(artifact_dir / "engagement_state.json", {})
    documents = {
        doc.get("document_id", ""): doc
        for doc in state.get("source_documents", [])
        if doc.get("document_id")
    }
    specs = [
        ("bank_statement", "bank_statement_facts.json", "facts"),
        ("bank_transaction", "bank_transactions.json", "transactions"),
        ("invoice", "invoice_facts.json", "facts"),
        ("distribution_tax", "distribution_tax_facts.json", "facts"),
        ("broker_trade", "broker_trade_facts.json", "facts"),
    ]
    rows: list[dict[str, str]] = []
    documents_with_facts: set[str] = set()
    for fact_type, filename, key in specs:
        data = _load_json(artifact_dir / filename, {})
        facts = data.get(key, []) if isinstance(data, dict) else []
        for fact in facts if isinstance(facts, list) else []:
            document_id = str(fact.get("document_id", ""))
            if document_id:
                documents_with_facts.add(document_id)
            doc = documents.get(document_id, {})
            file_path = str(doc.get("file_path") or fact.get("file_path") or "")
            rows.append({
                "document": Path(file_path).name or file_path or document_id,
                "document_type": str(doc.get("document_type") or fact.get("document_type") or "unknown"),
                "fact_type": fact_type,
                "accounting_facts": _accounting_fact_summary(fact_type, fact),
                "evidence": str(fact.get("evidence_id", "")),
                "status": "extracted",
            })
    for document_id, doc in documents.items():
        if document_id in documents_with_facts:
            continue
        file_path = str(doc.get("file_path") or "")
        rows.append({
            "document": Path(file_path).name or file_path or document_id,
            "document_type": str(doc.get("document_type") or "unknown"),
            "fact_type": "none",
            "accounting_facts": "No accounting fact extracted yet",
            "evidence": "",
            "status": "no_fact_extracted",
        })
    return rows


def _accounting_fact_output_summary(rows: list[dict[str, str]]) -> dict[str, int]:
    documents = {row["document"] for row in rows}
    documents_with_facts = {row["document"] for row in rows if row.get("status") == "extracted"}
    accounting_fact_rows = sum(1 for row in rows if row.get("status") == "extracted")
    return {
        "uploaded_documents": len(documents),
        "documents_with_facts": len(documents_with_facts),
        "accounting_fact_rows": accounting_fact_rows,
        "documents_without_facts": len(documents - documents_with_facts),
    }


def _render_accounting_facts_output(artifact_dir: Path) -> None:
    rows = _accounting_fact_rows(artifact_dir)
    if not rows:
        st.info("Accounting facts will appear here after extraction runs.")
        return
    summary = _accounting_fact_output_summary(rows)
    cols = st.columns(4)
    cols[0].metric("Uploaded documents", summary["uploaded_documents"])
    cols[1].metric("Documents with facts", summary["documents_with_facts"])
    cols[2].metric("Accounting fact rows", summary["accounting_fact_rows"])
    cols[3].metric("Documents without facts", summary["documents_without_facts"])
    st.markdown("**Accounting facts**")
    st.write("Each row is an extracted accounting fact linked back to its source document and evidence. Some documents can produce multiple facts.")
    st.dataframe(rows, use_container_width=True)


def _render_document_inventory_review(artifact_dir: Path) -> None:
    rows = _document_inventory_rows(artifact_dir)
    if not rows:
        st.info("Document inventory review will appear here after this step produces an inventory.")
        return
    st.markdown("**Document inventory review**")
    st.write("Confirm the detected document list before continuing to extraction. Update the review column if a document needs attention.")
    st.data_editor(rows, use_container_width=True, key="inline_document_inventory_review")


def _render_workflow_orchestrator(steps: list[dict[str, Any]], cwd: Path, artifact_dir: Path, title: str = "Workflow stage") -> None:
    st.header(title)
    st.write("Work through this stage, review its output here, then move to the next stage tab.")
    for idx, step in enumerate(steps, start=1):
        with st.expander(f"{idx}. {step['label']}", expanded=idx <= 3):
            st.write(step["description"])
            st.caption("Click the button to run this step. The output and review controls will appear here after it runs.")
            button_key = _workflow_step_button_key(title, idx, step)
            if st.button(step["label"], key=button_key):
                _ensure_engagement_state_for_step(step)
                results = _run_step_command(step["command"], cwd)
                refreshed_outputs = [Path(path) for path in step.get("outputs", [])]
                refreshed_count = sum(path.exists() for path in refreshed_outputs)
                summary = _workflow_result_summary(step, results, refreshed_count, len(refreshed_outputs))
                if summary["status"] == "Done":
                    st.success(summary["message"])
                elif summary["status"] in {"Needs review", "Check outputs"}:
                    st.warning(summary["message"])
                else:
                    st.error(summary["message"])
            if step["label"] == "Process documents and build inventory":
                _render_document_inventory_review(artifact_dir)


def _render_status_dashboard(artifact_dir: Path) -> None:
    st.header("Status dashboard")
    summary = _dashboard_summary(artifact_dir)
    st.subheader(summary["entity_name"])
    if summary["fy_start"] or summary["fy_end"]:
        st.caption(f"Financial year: {summary['fy_start']} → {summary['fy_end']}")
    cols = st.columns(6)
    cols[0].metric("Documents", summary["documents"])
    cols[1].metric("Source issues", summary["source_review_items"])
    cols[2].metric("CoA pending", summary["coa_pending"])
    cols[3].metric("Approved journals", summary["approved_journals"])
    cols[4].metric("Release blockers", summary["release_blockers"])
    cols[5].metric("Draft status", summary["draft_status"])
    st.info(f"Next action: {summary['next_action']}")


def _render_trial_balance_review(artifact_dir: Path) -> None:
    st.header("Reviewed Trial Balance")
    tb = _load_json(artifact_dir / "post_journal_trial_balance.json", {})
    if not tb:
        st.warning("No reviewed/post-journal trial balance found yet. Run the workflow through reviewed journals and post-journal TB.")
        return
    summary = tb.get("summary", {})
    if summary.get("is_balanced") is True:
        st.success(f"Trial balance is balanced. {summary.get('pending_journals_excluded', 0)} pending journals included/excluded by controls.")
    else:
        st.error("Trial balance is not confirmed balanced yet.")
    accounts = tb.get("accounts", [])
    if accounts:
        st.dataframe(accounts, use_container_width=True)


def _render_draft_statements_review(artifact_dir: Path) -> None:
    st.header("Draft Statements")
    draft = _load_json(artifact_dir / "draft_statements" / "draft_statements.json", {})
    if not draft:
        st.warning("No draft statements found yet. Run the draft statements workflow step first.")
        return
    st.warning("Internal review draft — not client ready.")
    st.write(f"Status: `{draft.get('status', 'unknown')}`")
    findings = draft.get("findings", [])
    st.write(f"Findings: `{len(findings) if isinstance(findings, list) else findings}`")
    statements = draft.get("statements", {})
    for name, rows in statements.items():
        with st.expander(name.replace("_", " ").title(), expanded=name in {"balance_sheet", "income_statement"}):
            if isinstance(rows, list) and rows:
                st.dataframe(rows, use_container_width=True)
            else:
                st.write(rows or "No rows available.")


def _render_final_output(artifact_dir: Path) -> None:
    st.header("Final Output")
    st.write("Clean package view for internal review: statements first, then release/final manifests and supporting workpapers.")
    preview_items = _final_package_preview(artifact_dir)
    if preview_items:
        for item in preview_items:
            path = item["path"]
            with st.expander(f"{item['label']} ({item['kind']})", expanded=item["kind"] == "statement"):
                st.caption(str(path))
                if path.suffix == ".json":
                    st.json(_load_json(path, {}))
                else:
                    st.text_area(item["label"], path.read_text()[:20000], height=260, key=f"final_preview_{path}")
    else:
        st.warning("No final package artifacts are available yet.")
    candidate = artifact_dir / "release_candidate" / "release_candidate_manifest.json"
    final_manifest = artifact_dir / "final_release_manifest.json"
    if candidate.exists():
        st.success("Release candidate package exists.")
    else:
        st.warning("Release candidate has not been built yet.")
    if final_manifest.exists():
        st.success("Final release manifest exists.")
    else:
        st.info("Final export is waiting for clean release candidate verification and accountant final sign-off.")


def _render_blockers(blockers: list[dict[str, Any]]) -> None:
    st.subheader("Release blockers")
    if not blockers:
        st.success("No release blockers detected by the current blocker report.")
        return
    for blocker in blockers:
        st.error(f"{blocker.get('category', 'blocker')}: {blocker.get('message', '')}")
        st.caption(f"Artifact: {blocker.get('artifact', '')}")
        st.write(f"Required action: {blocker.get('required_action', '')}")


def _decision_fields(prefix: str, include_offset: bool = False, default_reviewer: str = "", default_rationale: str = "") -> dict[str, str]:
    action = st.selectbox("Action", ["", "approve", "reject"], key=f"{prefix}_action")
    offset = ""
    if include_offset:
        offset = st.text_input("Offset account ID", key=f"{prefix}_offset")
    approved_by = st.text_input("Reviewer", value=default_reviewer, key=f"{prefix}_reviewer")
    rationale = st.text_area("Rationale", value=default_rationale, key=f"{prefix}_rationale")
    payload = {"action": action, "approved_by": approved_by, "rationale": rationale}
    if include_offset:
        payload["offset_account_id"] = offset
    return payload


def _render_workbench(workbench: dict[str, Any]) -> dict[str, Any]:
    edited = json.loads(json.dumps(workbench))
    sections = edited.setdefault("sections", {})
    st.subheader("Review defaults")
    default_reviewer = st.text_input("Default reviewer", key="default_reviewer")
    default_rationale = st.text_area("Default rationale", key="default_rationale")

    st.subheader("CoA Review")
    coa_accounts = sections.get("coa_accounts", [])
    coa_rows = _coa_review_rows(edited)
    if coa_rows:
        st.markdown("**Editable CoA review table**")
        edited_rows = st.data_editor(coa_rows, use_container_width=True, key="editable_coa_review_table")
        account_by_id = {account.get("account_id"): account for account in coa_accounts}
        for row in edited_rows:
            account = account_by_id.get(row.get("account_id"))
            if account is not None:
                account.update({"action": row.get("action", ""), "approved_by": row.get("approved_by", ""), "rationale": row.get("rationale", "")})
        st.download_button("Download CoA review rows", json.dumps(edited_rows, indent=2, sort_keys=True), file_name="coa_review_rows.json", mime="application/json")
    if coa_accounts and st.button("Approve all visible CoA accounts"):
        for account in coa_accounts:
            account["action"] = "approve"
            account["approved_by"] = default_reviewer
            account["rationale"] = default_rationale or "Reviewed and approved in accountant review UI."
    if not coa_accounts:
        st.info("No CoA accounts pending review.")
    for idx, account in enumerate(coa_accounts):
        with st.expander(f"{account.get('code')} — {account.get('name')} ({account.get('status')})", expanded=idx < 3):
            st.write({k: account.get(k) for k in ["account_id", "type", "presentation_group", "opening_balance"]})
            account.update(_decision_fields(f"coa_{idx}", default_reviewer=default_reviewer, default_rationale=default_rationale))

    st.subheader("Journal Review")
    journals = sections.get("journal_decisions", [])
    if not journals:
        st.info("No journals pending review.")
    for idx, journal in enumerate(journals):
        with st.expander(f"{journal.get('adjustment_id')} — {journal.get('amount')}"):
            st.write({k: journal.get(k) for k in ["description", "debit_account", "credit_account", "amount", "status"]})
            journal.update(_decision_fields(f"journal_{idx}", include_offset=True, default_reviewer=default_reviewer, default_rationale=default_rationale))

    st.subheader("Draft Statement Review")
    draft = sections.setdefault("draft_statement_review", {})
    st.write(f"Status: `{draft.get('draft_status', 'missing')}`")
    st.write(f"Findings: `{draft.get('draft_findings', 0)}`")
    draft.setdefault("decision", {}).update(_decision_fields("draft_review", default_reviewer=default_reviewer, default_rationale=default_rationale))

    st.subheader("Final Sign-off")
    st.warning("Only sign off after release candidate verification is clean.")
    sections.setdefault("final_signoff", {}).update(_decision_fields("final_signoff", default_reviewer=default_reviewer, default_rationale=default_rationale))
    return edited


def main() -> None:
    if st is None:
        raise RuntimeError("Streamlit is not installed. Install with `python3.11 -m pip install -e .[ui]`.")

    st.set_page_config(page_title="Accountant Review Workbench", layout="wide")
    st.title("Accountant Review Workbench")
    st.caption("Start by uploading source documents, then review blockers and accountant decisions. Approvals still go through deterministic CLI controls.")

    app_args = _app_args()
    repo_root = Path.cwd()
    state_path = Path(_query_param("state", app_args.state))
    artifact_dir = Path(_query_param("artifact_dir", app_args.artifact_dir))
    input_dir = Path(_query_param("input_dir", app_args.input_dir))

    workflow_steps = _workflow_steps(str(input_dir), str(artifact_dir), str(state_path))
    stage_groups = _workflow_stage_groups(workflow_steps)
    upload_tab, intake_tab, extract_tab, match_tab, coa_tab, tb_tab, review_tab, final_tab, artifacts_tab, apply_tab = st.tabs(_main_tab_labels())

    with upload_tab:
        st.header("Upload source documents")
        st.write("Upload PDFs, images, CSVs, spreadsheets, or other source files into the engagement input folder. This does not approve accounting treatment.")
        uploaded_files = st.file_uploader("Upload source documents", accept_multiple_files=True)
        if st.button("Save uploaded documents"):
            saved = _save_uploads(uploaded_files or [], input_dir)
            if saved:
                st.success(f"Saved {len(saved)} file(s).")
                for path in saved:
                    st.write(str(path))
            else:
                st.info("No files selected.")

    with intake_tab:
        _render_workflow_orchestrator(stage_groups[0]["steps"], repo_root, artifact_dir, stage_groups[0]["title"])

    with extract_tab:
        _render_workflow_orchestrator(stage_groups[1]["steps"], repo_root, artifact_dir, stage_groups[1]["title"])
        _render_accounting_facts_output(artifact_dir)

    with match_tab:
        _render_workflow_orchestrator(stage_groups[2]["steps"], repo_root, artifact_dir, stage_groups[2]["title"])

    with coa_tab:
        _render_workflow_orchestrator(stage_groups[3]["steps"], repo_root, artifact_dir, stage_groups[3]["title"])

    with tb_tab:
        _render_workflow_orchestrator(stage_groups[4]["steps"], repo_root, artifact_dir, stage_groups[4]["title"])
        _render_trial_balance_review(artifact_dir)
        _render_draft_statements_review(artifact_dir)

    workbench = _load_json(artifact_dir / "accountant_review_workbench.json", {})
    blockers = _load_json(artifact_dir / "release_blockers.json", {"blockers": []})

    with review_tab:
        _render_workflow_orchestrator(stage_groups[5]["steps"], repo_root, artifact_dir, stage_groups[5]["title"])
        _render_blockers(blockers.get("blockers", []))
        if not workbench:
            st.warning("No accountant_review_workbench.json found. Run export-accountant-review-workbench first.")
        else:
            edited = _render_workbench(workbench)
            st.download_button("Download filled workbench JSON", json.dumps(edited, indent=2, sort_keys=True), file_name="accountant_review_workbench_filled.json", mime="application/json")

    with artifacts_tab:
        st.header("Review artifacts")
        for label, rel in {
            "Release blockers": "release_blockers.md",
            "Draft statements": "draft_statements/draft_statements.md",
            "Post-journal TB": "post_journal_trial_balance.md",
            "Statement mapping": "statement_line_mapping.md",
            "Review packet": "review_packet/README.md",
        }.items():
            path = artifact_dir / rel
            if path.exists():
                st.subheader(label)
                st.caption(str(path))
                st.text_area(label, path.read_text()[:20000], height=220, key=f"artifact_{rel}")

    with final_tab:
        _render_workflow_orchestrator(stage_groups[6]["steps"], repo_root, artifact_dir, stage_groups[6]["title"])
        _render_final_output(artifact_dir)

    with apply_tab:
        st.header("Apply decisions through controls")
        st.write("Upload/downloaded filled workbench JSON here, then run the deterministic apply command. This app does not bypass validation.")
        filled = st.file_uploader("Filled workbench JSON", type=["json"], key="filled_workbench")
        target = artifact_dir / "accountant_review_workbench_filled.json"
        if st.button("Stage filled workbench for apply") and filled is not None:
            target.write_bytes(filled.getbuffer())
            st.success(f"Staged {target}")
        if st.button("Run apply decisions"):
            result = _run_cli(["apply-accountant-review-workbench", "--state", str(state_path), "--workbench", str(target), "--artifact-dir", str(artifact_dir), "--output", str(artifact_dir / "applied_accountant_review_workbench.json")], repo_root)
            if result.returncode == 0:
                st.success("Decisions applied.")
            else:
                st.error("Decisions could not be applied. Check the uploaded workbench and try again.")


if __name__ == "__main__":
    main()
