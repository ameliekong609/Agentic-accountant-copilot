"""TB bridge generation and correction application commands."""

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
    _extract_json_object,
    _list_value,
    _normalise_codex_cli_command,
    _read_json_object_file,
    _write_codex_attempt_history,
    _write_step_progress,
)
from accountant_copilot.prior_fs import (
    _build_prior_statement_coa_from_source_index,
    _format_prior_statement_coa_import,
)
from accountant_copilot.senior_review import _review_correction_findings
from accountant_copilot.tb_bridge_workflow import (
    TB_BRIDGE_CONTRACT_VERSION,
    TB_BRIDGE_JSON,
    TB_BRIDGE_MD,
    TB_BRIDGE_XLSX,
    build_tb_bridge_prompt,
    enrich_tb_bridge_payload_for_workbook,
    failed_tb_bridge_workpaper,
    format_tb_bridge_workpaper,
    normalise_tb_bridge_workpaper,
    repair_tb_bridge_workbook_hyperlinks,
    validate_tb_bridge_workpaper,
    write_tb_bridge_workbook_builder,
)

COA_MAPPING_CONTRACT_VERSION = TB_BRIDGE_CONTRACT_VERSION

def _codex_coa_mapping_prompt(
    event_register: dict,
    source_index: dict,
    prior_coa: dict | None = None,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
) -> str:
    return build_tb_bridge_prompt(
        event_register,
        source_index,
        prior_coa,
        recovery_attempt=recovery_attempt,
        previous_error=previous_error,
        validation_findings=validation_findings,
        previous_payload=previous_payload,
    )

def _codex_map_coa_from_events(
    event_register: dict,
    source_index: dict,
    prior_coa: dict | None,
    command: str,
    timeout: int,
    *,
    recovery_attempt: int = 0,
    previous_error: str | None = None,
    validation_findings: list[dict] | None = None,
    previous_payload: dict | None = None,
    candidate_output_path: Path | None = None,
) -> tuple[dict | None, str | None]:
    fake_payload = os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_TB_BRIDGE_JSON") or os.environ.get("ACCOUNTANT_COPILOT_FAKE_CODEX_COA_MAPPING_JSON")
    if fake_payload:
        payload = _extract_json_object(fake_payload)
        return payload, None if payload is not None else "Fake Codex TB bridge payload was not valid JSON."
    if candidate_output_path is not None and candidate_output_path.exists():
        try:
            candidate_output_path.unlink()
        except OSError:
            pass
    try:
        result = subprocess.run(
            shlex.split(command),
            input=_codex_coa_mapping_prompt(
                event_register,
                source_index,
                prior_coa,
                recovery_attempt=recovery_attempt,
                previous_error=previous_error,
                validation_findings=validation_findings,
                previous_payload=previous_payload,
            ),
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
    sidecar_payload, sidecar_error = _read_json_object_file(candidate_output_path)
    if result.returncode != 0:
        if sidecar_payload is not None:
            return sidecar_payload, None
        return None, f"Codex command exited {result.returncode}: {stderr[:500]}"
    if not result.stdout.strip():
        if sidecar_payload is not None:
            return sidecar_payload, None
        if sidecar_error:
            return None, sidecar_error
        return None, f"Codex command returned no stdout. {stderr[:500]}".strip()
    payload = _extract_json_object(result.stdout)
    if payload is None:
        if sidecar_payload is not None:
            return sidecar_payload, None
        if sidecar_error:
            return None, sidecar_error
        return None, f"Codex command did not return a JSON object. stdout={result.stdout[:500]!r}"
    return payload, None

def _validate_coa_mapping_workpaper(payload: dict | None, event_register: dict, prior_coa: dict | None = None) -> list[dict]:
    return validate_tb_bridge_workpaper(payload, event_register, prior_coa)

def _blocking_validation_findings(findings: list[dict]) -> list[dict]:
    return [finding for finding in findings if isinstance(finding, dict) and finding.get("severity") == "high"]

def _normalise_coa_mapping_workpaper(payload: dict, event_register: dict, validation_findings: list[dict]) -> dict:
    return normalise_tb_bridge_workpaper(payload, event_register, validation_findings)

def _codex_failed_coa_mapping_payload(event_register: dict, error: str, attempt_history: list[dict], validation_findings: list[dict] | None = None) -> dict:
    return failed_tb_bridge_workpaper(event_register, error, attempt_history, validation_findings)

def _format_coa_mapping_workpaper(payload: dict) -> str:
    return format_tb_bridge_workpaper(payload)

def _build_coa_mapping_workpaper_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    event_register_path = Path(getattr(args, "event_register", None) or artifact_dir / "accounting_event_register.json")
    source_index_path = Path(getattr(args, "source_index", None) or artifact_dir / "source_document_index.json")
    prior_coa_path = Path(getattr(args, "prior_coa", None) or artifact_dir / "prior_statement_coa_import.json")
    if not event_register_path.exists():
        print(f"Accounting event register not found: {event_register_path}", file=sys.stderr)
        return 2
    if not source_index_path.exists():
        print(f"Source document index not found: {source_index_path}", file=sys.stderr)
        return 2
    event_register = json.loads(event_register_path.read_text())
    source_index = json.loads(source_index_path.read_text())
    if getattr(args, "prior_coa", None):
        prior_coa = json.loads(prior_coa_path.read_text()) if prior_coa_path.exists() else {}
    else:
        prior_coa = _build_prior_statement_coa_from_source_index(
            source_index,
            prior_fs_document_id=getattr(args, "prior_fs_document_id", None),
            prior_fs_file=getattr(args, "prior_fs_file", None),
        )
        prior_coa_path.parent.mkdir(parents=True, exist_ok=True)
        prior_coa_path.write_text(json.dumps(prior_coa, indent=2, sort_keys=True))
        prior_coa_path.with_suffix(".md").write_text(_format_prior_statement_coa_import(prior_coa))
        blocking_findings = [finding for finding in _list_value(prior_coa.get("findings")) if isinstance(finding, dict) and finding.get("severity") == "high"]
        if blocking_findings or not _list_value(prior_coa.get("accounts")):
            for finding in blocking_findings:
                print(f"{finding.get('category')}: {finding.get('message') or finding.get('recommended_action') or ''}", file=sys.stderr)
            print(f"Prior-year FS opening balance import is not usable: {prior_coa_path}", file=sys.stderr)
            return 2
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 600) or 600)
    payload = None
    error = None
    validation_findings: list[dict] = []
    attempt_history: list[dict] = []
    previous_payload: dict | None = None
    generation_progress_path = output_dir / "tb_bridge_generation_progress.json"
    attempt_history_path = output_dir / "tb_bridge_attempt_history.json"
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "tb_bridge_generation",
                "status": "running",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": f"Preparing TB bridge attempt {attempt} of {max_attempts}.",
            },
        )
        payload, error = _codex_map_coa_from_events(
            event_register,
            source_index,
            prior_coa,
            codex_command,
            attempt_timeout,
            recovery_attempt=attempt - 1,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
            candidate_output_path=output_dir / TB_BRIDGE_JSON,
        )
        validation_findings = _validate_coa_mapping_workpaper(payload, event_register, prior_coa)
        blocking_findings = _blocking_validation_findings(validation_findings)
        attempt_history.append(
            {
                "attempt": attempt,
                "mode": "normal" if attempt == 1 else "recovery",
                "timeout_seconds": attempt_timeout,
                "status": "success" if payload is not None and not blocking_findings else "failed",
                "error": error or "",
                "validation_findings": validation_findings,
            }
        )
        _write_codex_attempt_history(
            attempt_history_path,
            stage="tb_bridge_generation",
            attempts=attempt_history,
            status="success" if payload is not None and not blocking_findings else "needs_attention",
            message=(
                f"TB bridge attempt {attempt} produced a usable workbook shape."
                if payload is not None and not blocking_findings
                else f"TB bridge attempt {attempt} needs correction."
            ),
            extra={
                "current_error": error or "",
                "blocking_findings": blocking_findings,
                "candidate_output_path": str(output_dir / TB_BRIDGE_JSON),
            },
        )
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "tb_bridge_generation",
                "status": "success" if payload is not None and not blocking_findings else "needs_attention",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "timeout_seconds": attempt_timeout,
                "message": (
                    f"TB bridge attempt {attempt} produced a usable workbook shape."
                    if payload is not None and not blocking_findings
                    else f"TB bridge attempt {attempt} needs correction."
                ),
                "error": error or "",
                "validation_findings": validation_findings,
            },
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not blocking_findings:
            break
        if payload is not None and blocking_findings:
            error = "Codex TB bridge output failed schema validation."
    if payload is None:
        final_payload = _codex_failed_coa_mapping_payload(event_register, error or "Codex CLI did not return a usable TB bridge result.", attempt_history, validation_findings)
    elif _blocking_validation_findings(validation_findings):
        final_payload = _codex_failed_coa_mapping_payload(event_register, "Codex CLI returned a TB bridge result that did not pass validation.", attempt_history, validation_findings)
    else:
        final_payload = _normalise_coa_mapping_workpaper(payload, event_register, validation_findings)
        final_payload["codex_attempt_history"] = attempt_history
    output_dir.mkdir(parents=True, exist_ok=True)
    final_payload = enrich_tb_bridge_payload_for_workbook(final_payload, event_register, source_index, prior_coa)
    json_output = output_dir / TB_BRIDGE_JSON
    md_output = output_dir / TB_BRIDGE_MD
    json_output.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    md_output.write_text(_format_coa_mapping_workpaper(final_payload))
    print(f"Exported Codex TB Bridge Matrix JSON -> {json_output}")
    print(f"Exported Codex TB Bridge Matrix notes -> {md_output}")
    if final_payload.get("status") == "codex_failed":
        _write_codex_attempt_history(
            attempt_history_path,
            stage="tb_bridge_generation",
            attempts=attempt_history,
            status="failed",
            message=str(final_payload.get("error") or "TB bridge generation failed."),
            extra={"validation_findings": final_payload.get("validation_findings") or []},
        )
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "tb_bridge_generation",
                "status": "failed",
                "attempts": attempt_history,
                "message": str(final_payload.get("error") or "TB bridge generation failed."),
                "validation_findings": final_payload.get("validation_findings") or [],
            },
        )
        return 1
    _write_codex_attempt_history(
        attempt_history_path,
        stage="tb_bridge_generation",
        attempts=attempt_history,
        status="complete" if not _blocking_validation_findings(final_payload.get("validation_findings") or []) else "needs_attention",
        message="TB bridge data is ready." if not _blocking_validation_findings(final_payload.get("validation_findings") or []) else "TB bridge data was produced with validation notes.",
        extra={
            "workpaper_json": str(json_output),
            "workpaper_md": str(md_output),
            "validation_findings": final_payload.get("validation_findings") or [],
        },
    )
    if not getattr(args, "skip_xlsx", False):
        _write_step_progress(
            generation_progress_path,
            {
                "stage": "workbook_build",
                "status": "running",
                "attempts": attempt_history,
                "message": "Building the Excel workbook from the TB bridge data.",
                "json_path": str(json_output),
            },
        )
        builder = write_tb_bridge_workbook_builder(
            output_dir,
            os.environ.get("ACCOUNTANT_COPILOT_NODE_MODULES", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"),
        )
        node_bin = os.environ.get("ACCOUNTANT_COPILOT_NODE", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
        result = subprocess.run([node_bin, str(builder)], cwd=Path.cwd(), text=True, capture_output=True, check=False)
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        if result.returncode != 0:
            _write_step_progress(
                generation_progress_path,
                {
                    "stage": "workbook_build",
                    "status": "failed",
                    "attempts": attempt_history,
                    "message": "Excel workbook build failed.",
                    "returncode": result.returncode,
                    "stdout": (result.stdout or "")[-2000:],
                    "stderr": (result.stderr or "")[-2000:],
                },
            )
            return result.returncode
        repaired = repair_tb_bridge_workbook_hyperlinks(output_dir / TB_BRIDGE_XLSX)
        if repaired:
            print(f"Repaired Evidence Index hyperlinks -> {repaired} link(s)")
    _write_step_progress(
        generation_progress_path,
        {
            "stage": "workbook_build",
            "status": "complete",
            "attempts": attempt_history,
            "message": "TB bridge workbook data is ready.",
            "workbook_path": str(output_dir / TB_BRIDGE_XLSX),
            "workbook_exists": (output_dir / TB_BRIDGE_XLSX).exists(),
        },
    )
    return 0 if not _blocking_validation_findings(final_payload.get("validation_findings") or []) else 1

def _write_tb_bridge_outputs(
    *,
    output_dir: Path,
    payload: dict,
    event_register: dict,
    source_index: dict,
    prior_coa: dict | None,
    skip_xlsx: bool,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    final_payload = enrich_tb_bridge_payload_for_workbook(payload, event_register, source_index, prior_coa)
    json_output = output_dir / TB_BRIDGE_JSON
    md_output = output_dir / TB_BRIDGE_MD
    json_output.write_text(json.dumps(final_payload, indent=2, sort_keys=True))
    md_output.write_text(_format_coa_mapping_workpaper(final_payload))
    print(f"Exported Codex TB Bridge Matrix JSON -> {json_output}")
    print(f"Exported Codex TB Bridge Matrix notes -> {md_output}")
    if skip_xlsx:
        return 0
    builder = write_tb_bridge_workbook_builder(
        output_dir,
        os.environ.get("ACCOUNTANT_COPILOT_NODE_MODULES", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules"),
    )
    node_bin = os.environ.get("ACCOUNTANT_COPILOT_NODE", "/Users/ameliekong/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node")
    result = subprocess.run([node_bin, str(builder)], cwd=Path.cwd(), text=True, capture_output=True, check=False)
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode == 0:
        repaired = repair_tb_bridge_workbook_hyperlinks(output_dir / TB_BRIDGE_XLSX)
        if repaired:
            print(f"Repaired Evidence Index hyperlinks -> {repaired} link(s)")
    return result.returncode

def _write_turing_correction_round_log(
    *,
    output_dir: Path,
    correction_round: int,
    review_payload: dict,
    attempt_history: list[dict],
    status: str,
    error: str = "",
    validation_findings: list[dict] | None = None,
    corrected_payload: dict | None = None,
    output_return_code: int | None = None,
) -> None:
    round_label = str(correction_round or "latest")
    json_path = output_dir / f"turing_correction_round_{round_label}_log.json"
    md_path = output_dir / f"turing_correction_round_{round_label}_log.md"
    briefs = review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else []
    findings = review_payload.get("findings") if isinstance(review_payload.get("findings"), list) else []
    corrected_summary = corrected_payload.get("summary") if isinstance(corrected_payload, dict) and isinstance(corrected_payload.get("summary"), dict) else {}
    payload = {
        "artifact_type": "turing_correction_round_log",
        "correction_round": correction_round,
        "status": status,
        "error": error,
        "review_status_before_correction": review_payload.get("status"),
        "findings_before_correction": findings,
        "correction_briefs": briefs,
        "attempt_history": attempt_history,
        "validation_findings_after_correction": validation_findings or [],
        "corrected_workpaper_summary": corrected_summary,
        "output_return_code": output_return_code,
        "outputs": {
            "tb_bridge_json": str(output_dir / TB_BRIDGE_JSON),
            "tb_bridge_markdown": str(output_dir / TB_BRIDGE_MD),
            "tb_bridge_workbook": str(output_dir / TB_BRIDGE_XLSX),
            "review_json": str(output_dir / "turing_senior_review.json"),
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    lines = [f"# Turing Correction Round {round_label}", "", f"- Status: {status}"]
    if error:
        lines.append(f"- Error: {error}")
    if output_return_code is not None:
        lines.append(f"- Output return code: {output_return_code}")
    lines.extend(["", "## Issues Turing Asked Tessa To Fix"])
    if briefs:
        for brief in briefs:
            if not isinstance(brief, dict):
                continue
            lines.extend(
                [
                    f"### {brief.get('brief_id', 'brief')}",
                    f"- Issue: {brief.get('issue', '')}",
                    f"- Expected treatment: {brief.get('expected_treatment', '')}",
                    f"- Required workbook change: {brief.get('required_workbook_change', '')}",
                    f"- Validation test: {brief.get('validation_test', '')}",
                    "",
                ]
            )
    else:
        lines.append("- No correction briefs were supplied.")
    lines.append("## Attempts")
    for attempt in attempt_history:
        if not isinstance(attempt, dict):
            continue
        lines.append(
            f"- Attempt {attempt.get('attempt')}: {attempt.get('status')} "
            f"(timeout {attempt.get('timeout_seconds')}s)"
        )
        if attempt.get("error"):
            lines.append(f"  Error: {attempt.get('error')}")
        findings_after = attempt.get("validation_findings") if isinstance(attempt.get("validation_findings"), list) else []
        if findings_after:
            lines.append(f"  Validation findings: {len(findings_after)}")
    if corrected_summary:
        lines.extend(["", "## Corrected Workpaper Summary"])
        for key, value in corrected_summary.items():
            lines.append(f"- {key}: {value}")
    md_path.write_text("\n".join(lines).rstrip() + "\n")

def _apply_turing_corrections_command(args: argparse.Namespace) -> int:
    artifact_dir = Path(args.artifact_dir)
    output_dir = Path(args.output_dir)
    workpaper_json = Path(getattr(args, "workpaper_json", None) or output_dir / TB_BRIDGE_JSON)
    review_json = Path(getattr(args, "review_json", None) or output_dir / "turing_senior_review.json")
    source_index_path = Path(getattr(args, "source_index", None) or artifact_dir / "source_document_index.json")
    event_register_path = Path(getattr(args, "event_register", None) or artifact_dir / "accounting_event_register.json")
    prior_coa_path = Path(getattr(args, "prior_coa", None) or artifact_dir / "prior_statement_coa_import.json")
    missing = [path for path in [workpaper_json, review_json, source_index_path, event_register_path] if not path.exists()]
    if missing:
        for path in missing:
            print(f"Required correction input not found: {path}", file=sys.stderr)
        return 2
    workpaper_payload = json.loads(workpaper_json.read_text())
    review_payload = json.loads(review_json.read_text())
    source_index = json.loads(source_index_path.read_text())
    event_register = json.loads(event_register_path.read_text())
    prior_coa = json.loads(prior_coa_path.read_text()) if prior_coa_path.exists() else None
    correction_round = int(getattr(args, "correction_round", 0) or 0)
    correction_findings = _review_correction_findings(review_payload)
    if not correction_findings:
        print("Turing review did not include correction briefs, so no correction pass is required.")
        return 0
    codex_command = _normalise_codex_cli_command(str(getattr(args, "codex_command", "codex exec")))
    max_attempts = max(1, int(getattr(args, "codex_max_attempts", 3) or 1))
    timeout = int(getattr(args, "codex_timeout", 600) or 600)
    payload = None
    error = "Turing senior review found correction briefs. Apply the briefs and return the complete corrected TB bridge workpaper JSON."
    validation_findings: list[dict] = correction_findings
    attempt_history: list[dict] = []
    attempt_history_path = output_dir / f"turing_correction_round_{correction_round or 'latest'}_attempt_history.json"
    previous_payload: dict | None = workpaper_payload
    for attempt in range(1, max_attempts + 1):
        attempt_timeout = timeout * (2 ** (attempt - 1))
        payload, error = _codex_map_coa_from_events(
            event_register,
            source_index,
            prior_coa,
            codex_command,
            attempt_timeout,
            recovery_attempt=attempt,
            previous_error=error,
            validation_findings=validation_findings,
            previous_payload=previous_payload,
            candidate_output_path=output_dir / TB_BRIDGE_JSON,
        )
        validation_findings = _validate_coa_mapping_workpaper(payload, event_register, prior_coa)
        attempt_history.append(
            {
                "attempt": attempt,
                "mode": "turing_correction",
                "timeout_seconds": attempt_timeout,
                "status": "success" if payload is not None and not validation_findings else "failed",
                "error": error or "",
                "validation_findings": validation_findings,
            }
        )
        _write_codex_attempt_history(
            attempt_history_path,
            stage="turing_correction",
            attempts=attempt_history,
            status="success" if payload is not None and not validation_findings else "needs_attention",
            message=(
                f"Turing correction round {correction_round or 'latest'} attempt {attempt} produced a usable corrected workpaper."
                if payload is not None and not validation_findings
                else f"Turing correction round {correction_round or 'latest'} attempt {attempt} needs correction."
            ),
            extra={"correction_round": correction_round, "current_error": error or ""},
        )
        if payload is not None:
            previous_payload = payload
        if payload is not None and not validation_findings:
            break
        if payload is not None and validation_findings:
            error = "Codex correction output failed schema validation."
    if payload is None or validation_findings:
        failure_payload = _codex_failed_coa_mapping_payload(
            event_register,
            error or "Codex CLI did not return a usable corrected TB bridge result.",
            attempt_history,
            validation_findings,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "tb_bridge_correction_failed.json").write_text(json.dumps(failure_payload, indent=2, sort_keys=True))
        _write_turing_correction_round_log(
            output_dir=output_dir,
            correction_round=correction_round,
            review_payload=review_payload,
            attempt_history=attempt_history,
            status="failed",
            error=error or "Codex CLI did not return a usable corrected TB bridge result.",
            validation_findings=validation_findings,
            output_return_code=1,
        )
        print("Codex could not apply Turing corrections.", file=sys.stderr)
        return 1
    final_payload = _normalise_coa_mapping_workpaper(payload, event_register, validation_findings)
    final_payload["codex_attempt_history"] = attempt_history
    final_payload["turing_correction_source"] = {
        "review_status": review_payload.get("status"),
        "correction_briefs": review_payload.get("correction_briefs") if isinstance(review_payload.get("correction_briefs"), list) else [],
        "review_summary": review_payload.get("summary") if isinstance(review_payload.get("summary"), dict) else {},
    }
    output_return_code = _write_tb_bridge_outputs(
        output_dir=output_dir,
        payload=final_payload,
        event_register=event_register,
        source_index=source_index,
        prior_coa=prior_coa,
        skip_xlsx=bool(getattr(args, "skip_xlsx", False)),
    )
    _write_turing_correction_round_log(
        output_dir=output_dir,
        correction_round=correction_round,
        review_payload=review_payload,
        attempt_history=attempt_history,
        status="applied" if output_return_code == 0 else "output_failed",
        error="" if output_return_code == 0 else "Corrected JSON was produced, but workbook output failed.",
        validation_findings=validation_findings,
        corrected_payload=final_payload,
        output_return_code=output_return_code,
    )
    return output_return_code
