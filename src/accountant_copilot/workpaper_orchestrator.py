"""Full financial statement workpaper orchestration."""

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

from typing import Callable

from accountant_copilot.common import _normalise_codex_cli_command
from accountant_copilot.senior_review import (
    _archive_turing_review_round,
    _public_turing_findings,
    _turing_review_is_ready,
    _turing_review_needs_corrections,
)
from accountant_copilot.contract_utils import TB_BRIDGE_JSON, TB_BRIDGE_MD, TB_BRIDGE_XLSX

def _prepare_workpaper_update_run_context(artifact_dir: Path, *, entity_name: str | None, fy_start: str | None, fy_end: str | None) -> None:
    context = {
        key: value
        for key, value in {
            "entity_name": entity_name,
            "target_fy_start": fy_start,
            "target_fy_end": fy_end,
        }.items()
        if value
    }
    if not context:
        return
    for file_name in [
        "document_inventory.json",
        "source_document_index.json",
        "accounting_facts_by_document.json",
        "source_coverage_continuity.json",
    ]:
        path = artifact_dir / file_name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payload.update(context)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True))

def _prepare_workpaper_summary(
    *,
    client_folder: Path,
    artifact_dir: Path,
    output_dir: Path,
    step_statuses: dict[str, int],
) -> str:
    workbook_path = output_dir / TB_BRIDGE_XLSX
    tb_json_path = output_dir / TB_BRIDGE_JSON
    review_path = output_dir / "turing_senior_review.md"
    source_index_path = artifact_dir / "source_document_index.json"
    event_register_path = artifact_dir / "accounting_event_register.json"
    lines = ["# Prepared Workpaper Summary", ""]
    lines.extend(
        [
            f"- Client folder: {client_folder}",
            f"- Source index: {source_index_path}",
            f"- Event register: {event_register_path}",
            f"- TB Bridge workbook: {workbook_path}",
            f"- Turing senior review: {review_path}",
            "",
        ]
    )
    lines.append("## Run status")
    for label, code in step_statuses.items():
        state = "completed" if code == 0 else "completed with warnings" if label == "step2_source_index" and source_index_path.exists() else "needs attention"
        lines.append(f"- {label}: {state} (exit {code})")
    if tb_json_path.exists():
        try:
            tb_payload = json.loads(tb_json_path.read_text())
        except json.JSONDecodeError:
            tb_payload = {}
        summary = tb_payload.get("summary") if isinstance(tb_payload.get("summary"), dict) else {}
        findings = tb_payload.get("validation_findings") if isinstance(tb_payload.get("validation_findings"), list) else []
        lines.extend(["", "## Workbook checks"])
        lines.append(f"- Accounts: {summary.get('accounts', 0)}")
        lines.append(f"- Movement columns: {summary.get('movement_columns', 0)}")
        lines.append(f"- Movement notes: {summary.get('movement_notes', 0)}")
        lines.append(f"- Validation findings: {len(findings)}")
        if findings:
            lines.append("")
            lines.append("## Needs attention")
            for finding in findings[:12]:
                if not isinstance(finding, dict):
                    continue
                message = finding.get("message") or finding.get("category") or finding
                lines.append(f"- {message}")
    if review_path.exists():
        try:
            review_payload = json.loads(review_path.with_suffix(".json").read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            review_payload = {}
        review_summary = review_payload.get("summary") if isinstance(review_payload.get("summary"), dict) else {}
        review_findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
        public_review_findings = _public_turing_findings(review_payload) if isinstance(review_payload, dict) else []
        internal_review_notes = max(0, len(review_findings) - len(public_review_findings))
        lines.extend(["", "## Turing senior review"])
        lines.append(f"- Status: {'ready' if _turing_review_is_ready(output_dir) else review_payload.get('status', 'review_created')}")
        lines.append(f"- Sampled items: {review_summary.get('sampled_items', len(review_payload.get('sampled_items', []) if isinstance(review_payload.get('sampled_items'), list) else []))}")
        lines.append(f"- Material findings shown to accountant: {len(public_review_findings)}")
        lines.append(f"- Internal low-risk notes handled by Tessa/Turing: {internal_review_notes}")
        if public_review_findings:
            lines.append("")
            lines.append("## Material review items")
            for finding in public_review_findings[:10]:
                if not isinstance(finding, dict):
                    continue
                lines.append(f"- {finding.get('severity', 'review')} / {finding.get('category', 'judgement')}: {finding.get('message', '')}")
    lines.extend(
        [
            "",
            "## Accountant-facing instruction",
            "Open the TB Bridge workbook first. Use Movement Notes to search important amounts and Evidence Index to open source PDFs.",
            "",
        ]
    )
    return "\n".join(lines)

def _remove_previous_event_register_outputs(artifact_dir: Path) -> None:
    for file_name in [
        "source_fact_matches.md",
        "source_fact_matches.json",
        "accounting_event_register.md",
        "accounting_event_register.json",
        "relationship_reasoning_register.md",
        "relationship_reasoning_register.json",
        "relationship_reasoning_progress.json",
        "relationship_reasoning_attempt_history.json",
    ]:
        path = artifact_dir / file_name
        if path.exists():
                path.unlink()

def _last_good_workpaper_dir(output_dir: Path) -> Path:
    return output_dir / "_last_good"

def _workpaper_promotable_files() -> list[str]:
    return [
        TB_BRIDGE_JSON,
        TB_BRIDGE_MD,
        TB_BRIDGE_XLSX,
        "turing_senior_review.md",
        "turing_senior_review.json",
        f"{TB_BRIDGE_XLSX}.inspect.ndjson",
    ]

def _snapshot_previous_workpaper_outputs(output_dir: Path) -> bool:
    workbook_path = output_dir / TB_BRIDGE_XLSX
    if not workbook_path.exists():
        return False
    snapshot_dir = _last_good_workpaper_dir(output_dir)
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for file_name in _workpaper_promotable_files():
        source = output_dir / file_name
        if source.exists() and source.is_file():
            shutil.copy2(source, snapshot_dir / file_name)
            copied.append(file_name)
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(
            {
                "snapshot_at": datetime.now(timezone.utc).isoformat(),
                "files": copied,
                "reason": "Last valid workbook snapshot before starting a new run.",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return bool(copied)

def _restore_last_good_workpaper_outputs(output_dir: Path, *, reason: str) -> bool:
    snapshot_dir = _last_good_workpaper_dir(output_dir)
    workbook_path = snapshot_dir / TB_BRIDGE_XLSX
    if not workbook_path.exists():
        return False
    restored: list[str] = []
    for file_name in _workpaper_promotable_files():
        source = snapshot_dir / file_name
        if source.exists() and source.is_file():
            shutil.copy2(source, output_dir / file_name)
            restored.append(file_name)
    (output_dir / "last_good_workpaper_restored.json").write_text(
        json.dumps(
            {
                "restored_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "files": restored,
                "snapshot_dir": str(snapshot_dir),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return bool(restored)

def _prepare_workpaper_progress_path(output_dir: Path) -> Path:
    return output_dir / "prepare_workpaper_progress.json"

def _write_prepare_workpaper_progress(
    output_dir: Path,
    *,
    stage: str,
    status: str,
    message: str,
    step_statuses: dict[str, int] | None = None,
    extra: dict | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "status": status,
        "message": message,
        "step_statuses": step_statuses or {},
    }
    if extra:
        payload.update(extra)
    _prepare_workpaper_progress_path(output_dir).write_text(json.dumps(payload, indent=2, sort_keys=True))

def _remove_previous_workpaper_outputs(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    generated_names = {
        TB_BRIDGE_JSON,
        TB_BRIDGE_MD,
        TB_BRIDGE_XLSX,
        "prepared_workpaper_summary.md",
        "turing_senior_review.md",
        "turing_senior_review.json",
        "tb_bridge_correction_failed.json",
        "build_tb_bridge_workpaper.mjs",
        "last_good_workpaper_restored.json",
        "prepare_workpaper_progress.json",
        "tb_bridge_generation_progress.json",
        "tb_bridge_attempt_history.json",
        "turing_review_attempt_history.json",
    }
    generated_patterns = [
        "preview_*.png",
        "*.inspect.ndjson",
        "turing_senior_review_round_*.*",
    ]
    for file_name in generated_names:
        path = output_dir / file_name
        if path.exists():
            path.unlink()
    for pattern in generated_patterns:
        for path in output_dir.glob(pattern):
            if path.is_file():
                path.unlink()

def prepare_workpaper_command(
    args: argparse.Namespace,
    *,
    process_documents_command: Callable[[argparse.Namespace], int],
    match_source_facts_command: Callable[[argparse.Namespace], int],
    build_coa_mapping_workpaper_command: Callable[[argparse.Namespace], int],
    review_workpaper_command: Callable[[argparse.Namespace], int],
    apply_turing_corrections_command: Callable[[argparse.Namespace], int],
) -> int:
    client_folder = Path(args.client_folder).expanduser()
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    codex_timeout = int(getattr(args, "codex_timeout", 1200) or 1200)
    codex_max_attempts = int(getattr(args, "codex_max_attempts", 3) or 3)
    review_correction_rounds = max(0, int(getattr(args, "review_correction_rounds", 2) or 0))
    force_reprocess = bool(getattr(args, "force_reprocess", False)) or not bool(getattr(args, "allow_cache", False))
    if not client_folder.exists() or not client_folder.is_dir():
        print(f"Client folder not found: {client_folder}", file=sys.stderr)
        return 2

    had_last_good_workbook = _snapshot_previous_workpaper_outputs(output_dir)
    _remove_previous_workpaper_outputs(output_dir)
    if had_last_good_workbook:
        print(f"Saved previous valid workbook snapshot -> {_last_good_workpaper_dir(output_dir)}")

    source_index = artifact_dir / "source_document_index.json"
    print(f"Preparing accountant workpaper from: {client_folder}")
    _write_prepare_workpaper_progress(
        output_dir,
        stage="indexing",
        status="running",
        message="Tessa is reading the uploaded files and building the evidence index.",
        extra={
            "client_folder": str(client_folder),
            "last_good_snapshot_available": had_last_good_workbook,
        },
    )
    print("Step 1/3: indexing source documents with Codex CLI")
    step_statuses: dict[str, int] = {}
    try:
        step_statuses["step2_source_index"] = process_documents_command(
            argparse.Namespace(
                input_dir=str(client_folder),
                artifact_dir=str(artifact_dir),
                codex_command=codex_command,
                codex_timeout=codex_timeout,
                codex_max_attempts=codex_max_attempts,
                batch_size=int(getattr(args, "batch_size", 5) or 5),
                force_reprocess=force_reprocess,
            )
        )
    except Exception as exc:  # noqa: BLE001 - product runner must checkpoint unexpected failures.
        traceback.print_exc()
        step_statuses["step2_source_index"] = 1
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Source indexing crashed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="indexing",
            status="failed",
            message=(
                "Tessa could not finish reading the uploaded files. Previous valid workbook was restored."
                if restored_last_good
                else "Tessa could not finish reading the uploaded files. No refreshed workbook was produced."
            ),
            step_statuses=step_statuses,
            extra={
                "error_type": type(exc).__name__,
                "last_good_restored": restored_last_good,
                "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
            },
        )
        return 1
    _write_prepare_workpaper_progress(
        output_dir,
        stage="indexing",
        status="complete" if step_statuses["step2_source_index"] == 0 else "needs_attention",
        message="Evidence index completed." if step_statuses["step2_source_index"] == 0 else "Evidence index completed with documents needing attention.",
        step_statuses=step_statuses,
        extra={"source_index_path": str(source_index)},
    )
    _prepare_workpaper_update_run_context(
        artifact_dir,
        entity_name=getattr(args, "entity_name", None),
        fy_start=getattr(args, "fy_start", None),
        fy_end=getattr(args, "fy_end", None),
    )
    accounting_facts = artifact_dir / "accounting_facts_by_document.json"
    source_coverage = artifact_dir / "source_coverage_continuity.json"
    if not source_index.exists() or not accounting_facts.exists():
        print("Source index was not created, so the workpaper cannot continue.", file=sys.stderr)
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Source indexing failed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="indexing",
            status="failed",
            message=(
                "Source index was not created. Previous valid workbook was restored."
                if restored_last_good
                else "Source index was not created, so the workpaper cannot continue."
            ),
            step_statuses=step_statuses,
            extra={"last_good_restored": restored_last_good, "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists()},
        )
        return 1

    print("Step 2/3: building accounting event register with Codex CLI")
    _write_prepare_workpaper_progress(
        output_dir,
        stage="relationships",
        status="running",
        message="Tessa is investigating relationships between prior-year balances, bank movements and source documents.",
        step_statuses=step_statuses,
    )
    _remove_previous_event_register_outputs(artifact_dir)
    try:
        step_statuses["step3_event_register"] = match_source_facts_command(
            argparse.Namespace(
                accounting_facts=str(accounting_facts),
                source_coverage=str(source_coverage) if source_coverage.exists() else None,
                codex_command=codex_command,
                codex_timeout=codex_timeout,
                codex_max_attempts=codex_max_attempts,
                bank_transactions=None,
                invoice_facts=None,
                distribution_tax_facts=None,
                broker_trade_facts=None,
                output=str(artifact_dir / "source_fact_matches.md"),
            )
        )
    except Exception as exc:  # noqa: BLE001 - product runner must checkpoint unexpected failures.
        traceback.print_exc()
        step_statuses["step3_event_register"] = 1
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Relationship reasoning crashed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="relationships",
            status="failed",
            message=f"Movement reasoning failed with a product error: {exc}",
            step_statuses=step_statuses,
            extra={
                "error_type": type(exc).__name__,
                "last_good_restored": restored_last_good,
                "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
            },
        )
        return 1
    _write_prepare_workpaper_progress(
        output_dir,
        stage="relationships",
        status="complete" if step_statuses["step3_event_register"] == 0 else "failed",
        message="Accounting event register completed." if step_statuses["step3_event_register"] == 0 else "Accounting event register needs engineering attention.",
        step_statuses=step_statuses,
        extra={"event_register_path": str(artifact_dir / "accounting_event_register.json")},
    )
    event_register = artifact_dir / "accounting_event_register.json"
    if not event_register.exists():
        print("Accounting event register was not created, so the workpaper cannot continue.", file=sys.stderr)
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Relationship reasoning failed before a refreshed workbook was produced.")
        summary = _prepare_workpaper_summary(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            step_statuses=step_statuses,
        )
        summary_path = output_dir / "prepared_workpaper_summary.md"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
        print(f"Prepared workpaper summary -> {summary_path}")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="relationships",
            status="failed",
            message=(
                "Accounting event register was not created. Previous valid workbook was restored."
                if restored_last_good
                else "Accounting event register was not created, so the workpaper cannot continue."
            ),
            step_statuses=step_statuses,
            extra={"summary_path": str(summary_path), "last_good_restored": restored_last_good, "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists()},
        )
        return 1
    try:
        event_payload = json.loads(event_register.read_text())
    except json.JSONDecodeError:
        event_payload = {}
    if isinstance(event_payload, dict) and event_payload.get("status") == "codex_failed":
        print("Codex could not create a usable accounting event register.", file=sys.stderr)
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Relationship reasoning returned an unusable register before a refreshed workbook was produced.")
        summary = _prepare_workpaper_summary(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            step_statuses=step_statuses,
        )
        summary_path = output_dir / "prepared_workpaper_summary.md"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
        print(f"Prepared workpaper summary -> {summary_path}")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="relationships",
            status="failed",
            message=(
                "AI could not create a usable accounting event register. Previous valid workbook was restored."
                if restored_last_good
                else "AI could not create a usable accounting event register."
            ),
            step_statuses=step_statuses,
            extra={"summary_path": str(summary_path), "last_good_restored": restored_last_good, "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists()},
        )
        return 1

    print("Step 3/3: building TB Bridge workbook with Codex CLI")
    _write_prepare_workpaper_progress(
        output_dir,
        stage="bridge",
        status="running",
        message="Tessa is preparing the TB bridge workbook and movement notes.",
        step_statuses=step_statuses,
    )
    try:
        step_statuses["step4_tb_bridge_workbook"] = build_coa_mapping_workpaper_command(
            argparse.Namespace(
                artifact_dir=str(artifact_dir),
                output_dir=str(output_dir),
                event_register=None,
                source_index=None,
                prior_coa=None,
                prior_fs_document_id=getattr(args, "prior_fs_document_id", None),
                prior_fs_file=getattr(args, "prior_fs_file", None),
                codex_command=codex_command,
                codex_timeout=codex_timeout,
                codex_max_attempts=codex_max_attempts,
                skip_xlsx=bool(getattr(args, "skip_xlsx", False)),
            )
        )
    except Exception as exc:  # noqa: BLE001 - product runner must checkpoint unexpected failures.
        traceback.print_exc()
        step_statuses["step4_tb_bridge_workbook"] = 1
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="TB bridge workbook stage crashed before a refreshed workbook was produced.")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="bridge",
            status="failed",
            message=f"TB bridge workbook stage failed with a product error: {exc}",
            step_statuses=step_statuses,
            extra={
                "error_type": type(exc).__name__,
                "last_good_restored": restored_last_good,
                "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
            },
        )
        return 1
    workbook_path = output_dir / TB_BRIDGE_XLSX
    if step_statuses["step4_tb_bridge_workbook"] != 0:
        restored_last_good = _restore_last_good_workpaper_outputs(output_dir, reason="Step 4 failed before a refreshed workbook was produced.")
        summary = _prepare_workpaper_summary(
            client_folder=client_folder,
            artifact_dir=artifact_dir,
            output_dir=output_dir,
            step_statuses=step_statuses,
        )
        summary_path = output_dir / "prepared_workpaper_summary.md"
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary)
        print(f"Prepared workpaper summary -> {summary_path}")
        if restored_last_good:
            print(f"Restored previous valid workbook -> {workbook_path}")
        print("TB Bridge workbook was not refreshed because Step 4 needs attention.", file=sys.stderr)
        _write_prepare_workpaper_progress(
            output_dir,
            stage="bridge",
            status="failed",
            message=(
                "TB bridge workbook needs engineering attention. Previous valid workbook was restored."
                if restored_last_good
                else "TB bridge workbook needs engineering attention."
            ),
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
                "last_good_restored": restored_last_good,
            },
        )
        return 1

    review_required = not bool(getattr(args, "skip_review", False))
    if review_required and (output_dir / TB_BRIDGE_JSON).exists():
        print("Senior review: Turing is checking controls and sampling source evidence with Codex CLI")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="turing",
            status="running",
            message="Senior review is checking arithmetic, workbook structure and sample evidence.",
            step_statuses=step_statuses,
            extra={"workbook_path": str(workbook_path), "workbook_exists": workbook_path.exists()},
        )
        review_args = argparse.Namespace(
            client_folder=str(client_folder),
            artifact_dir=str(artifact_dir),
            output_dir=str(output_dir),
            workpaper_json=None,
            source_index=None,
            event_register=None,
            prior_coa=None,
            output=None,
            entity_name=getattr(args, "entity_name", None),
            codex_command=codex_command,
            codex_timeout=codex_timeout,
            codex_max_attempts=codex_max_attempts,
            sample_size=int(getattr(args, "review_sample_size", 8) or 8),
        )
        step_statuses["turing_senior_review_round_1"] = review_workpaper_command(review_args)
        final_review_round = 1
        for correction_round in range(1, review_correction_rounds + 1):
            if step_statuses.get(f"turing_senior_review_round_{final_review_round}") != 0:
                break
            if not _turing_review_needs_corrections(output_dir):
                break
            _archive_turing_review_round(output_dir, final_review_round)
            print(f"Senior review correction round {correction_round}: Codex is applying Turing correction briefs")
            _write_prepare_workpaper_progress(
                output_dir,
                stage="correction",
                status="running",
                message=f"Tessa is applying senior review correction round {correction_round}.",
                step_statuses=step_statuses,
                extra={"correction_round": correction_round},
            )
            step_statuses[f"turing_correction_round_{correction_round}"] = apply_turing_corrections_command(
                argparse.Namespace(
                    artifact_dir=str(artifact_dir),
                    output_dir=str(output_dir),
                    workpaper_json=None,
                    review_json=None,
                    source_index=None,
                    event_register=None,
                    prior_coa=None,
                    codex_command=codex_command,
                    codex_timeout=codex_timeout,
                    codex_max_attempts=codex_max_attempts,
                    correction_round=correction_round,
                    skip_xlsx=bool(getattr(args, "skip_xlsx", False)),
                )
            )
            if step_statuses[f"turing_correction_round_{correction_round}"] != 0:
                break
            print(f"Senior review recheck round {correction_round}: Turing is rechecking the corrected workbook")
            _write_prepare_workpaper_progress(
                output_dir,
                stage="turing",
                status="running",
                message=f"Senior review is rechecking correction round {correction_round}.",
                step_statuses=step_statuses,
                extra={"correction_round": correction_round},
            )
            final_review_round += 1
            step_statuses[f"turing_senior_review_round_{final_review_round}"] = review_workpaper_command(review_args)
        if "turing_senior_review_round_1" in step_statuses:
            step_statuses["turing_senior_review"] = step_statuses[f"turing_senior_review_round_{final_review_round}"]
    summary = _prepare_workpaper_summary(
        client_folder=client_folder,
        artifact_dir=artifact_dir,
        output_dir=output_dir,
        step_statuses=step_statuses,
    )
    summary_path = output_dir / "prepared_workpaper_summary.md"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary)
    print(f"Prepared workpaper summary -> {summary_path}")
    current_run_ok = workbook_path.exists() and step_statuses.get("step4_tb_bridge_workbook") == 0 and (
        not review_required or (step_statuses.get("turing_senior_review") == 0 and _turing_review_is_ready(output_dir))
    )
    if current_run_ok:
        print(f"Workbook ready -> {workbook_path}")
        _write_prepare_workpaper_progress(
            output_dir,
            stage="completed",
            status="completed",
            message="Workbook ready. Senior review passed.",
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
            },
        )
        return 0
    if review_required and workbook_path.exists() and step_statuses.get("turing_senior_review") == 0 and not _turing_review_is_ready(output_dir):
        print(f"Workbook was created but Turing still needs corrections after {review_correction_rounds} correction round(s): {workbook_path}", file=sys.stderr)
        _write_prepare_workpaper_progress(
            output_dir,
            stage="turing",
            status="needs_attention",
            message=f"Workbook was created, but senior review still has correction notes after {review_correction_rounds} correction round(s).",
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
            },
        )
        return 0
    if workbook_path.exists():
        print(f"Workbook was created but the current run needs attention: {workbook_path}", file=sys.stderr)
        _write_prepare_workpaper_progress(
            output_dir,
            stage="bridge",
            status="needs_attention",
            message="Workbook was created, but the current run has judgement or review items.",
            step_statuses=step_statuses,
            extra={
                "summary_path": str(summary_path),
                "workbook_path": str(workbook_path),
                "workbook_exists": workbook_path.exists(),
            },
        )
        return 0
    print(f"Workbook was not created: {workbook_path}", file=sys.stderr)
    _write_prepare_workpaper_progress(
        output_dir,
        stage="bridge",
        status="failed",
        message="Workbook was not created.",
        step_statuses=step_statuses,
        extra={"summary_path": str(summary_path), "workbook_path": str(workbook_path), "workbook_exists": False},
    )
    return 1
