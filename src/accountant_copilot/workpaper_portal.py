"""Local accountant-facing workpaper portal.

This server is intentionally small and local-first. It gives a browser UI for
uploading client files, starting the existing prepare-workpaper workflow, and
downloading the generated Excel workbook.
"""
from __future__ import annotations

import cgi
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse


PORTAL_ROOT = Path("outputs/workpaper_portal")
STATE_FILE = "portal_state.json"
WORKBOOK_PATH = Path("outputs/step4_tb_bridge_workpaper/step4_tb_bridge_workpaper.xlsx")
SUMMARY_PATH = Path("outputs/step4_tb_bridge_workpaper/prepared_workpaper_summary.md")
TURING_REVIEW_PATH = Path("outputs/step4_tb_bridge_workpaper/turing_senior_review.json")
SOURCE_INDEX_PATH = Path("outputs/raw_inputs_pdf_extraction/source_document_index.json")
EVENT_REGISTER_PATH = Path("outputs/raw_inputs_pdf_extraction/accounting_event_register.json")
TB_BRIDGE_JSON_PATH = Path("outputs/step4_tb_bridge_workpaper/tb_bridge_workpaper.json")
PREPARE_PROGRESS_PATH = Path("outputs/step4_tb_bridge_workpaper/prepare_workpaper_progress.json")
DOCUMENT_PROGRESS_PATH = Path("outputs/raw_inputs_pdf_extraction/document_processing_progress.json")
RELATIONSHIP_PROGRESS_PATH = Path("outputs/raw_inputs_pdf_extraction/relationship_reasoning_progress.json")
TB_BRIDGE_PROGRESS_PATH = Path("outputs/step4_tb_bridge_workpaper/tb_bridge_generation_progress.json")
DEMO_SNAPSHOT_PATH = PORTAL_ROOT / "demo_snapshot"
DEMO_DURATION_SECONDS = 30
DEMO_SOURCE_READY_SECONDS = 6
DEMO_WORKBOOK_READY_SECONDS = 22
DEMO_REVIEW_READY_SECONDS = 26


@dataclass(frozen=True)
class WorkpaperPortalConfig:
    repo_root: Path
    host: str = "127.0.0.1"
    port: int = 8787


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _elapsed_seconds_since(value: Any) -> int:
    started = _parse_iso_datetime(value)
    if started is None:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _read_json(path: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _write_portal_job_status(job_dir: Path, job: dict[str, Any]) -> None:
    _write_json(job_dir / "status.json", job)


def _start_engineer_watcher(repo_root: Path, job_dir: Path) -> int | None:
    if os.environ.get("ACCOUNTANT_COPILOT_ENGINEER_WATCHER", "1") == "0":
        return None
    watcher = repo_root / "scripts" / "watch_workpaper_engineer.sh"
    if not watcher.exists():
        return None
    log_path = job_dir / "engineer-watcher.log"
    log_handle = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [str(watcher), str(job_dir)],
            cwd=repo_root,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    except Exception:
        log_handle.close()
        return None
    (job_dir / "engineer-watcher.pid").write_text(str(process.pid), encoding="utf-8")
    return process.pid


def _start_engineer_check(repo_root: Path, job_dir: Path, reason: str) -> int | None:
    checker = repo_root / "scripts" / "run_workpaper_engineer_check.sh"
    if not checker.exists():
        return None
    log_path = job_dir / "engineer-check-launch.log"
    log_handle = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [str(checker), str(job_dir), reason],
            cwd=repo_root,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    except Exception:
        log_handle.close()
        return None
    (job_dir / "engineer-check.pid").write_text(str(process.pid), encoding="utf-8")
    return process.pid


def _clear_generated_workpaper_state(repo_root: Path, *, preserve_document_cache: bool = True) -> None:
    cache_path = repo_root / "outputs/raw_inputs_pdf_extraction/.codex_doc_cache"
    preserved_cache_path = repo_root / PORTAL_ROOT / ".preserved_codex_doc_cache"
    if preserved_cache_path.exists():
        shutil.rmtree(preserved_cache_path)
    if preserve_document_cache and cache_path.exists():
        preserved_cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cache_path), str(preserved_cache_path))
    for path in [
        repo_root / PORTAL_ROOT / "uploads",
        repo_root / PORTAL_ROOT / "jobs",
        repo_root / PORTAL_ROOT / STATE_FILE,
        repo_root / "outputs/raw_inputs_pdf_extraction",
        repo_root / "outputs/step4_tb_bridge_workpaper",
    ]:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    (repo_root / PORTAL_ROOT).mkdir(parents=True, exist_ok=True)
    (repo_root / "outputs/raw_inputs_pdf_extraction").mkdir(parents=True, exist_ok=True)
    (repo_root / "outputs/step4_tb_bridge_workpaper").mkdir(parents=True, exist_ok=True)
    if preserve_document_cache and preserved_cache_path.exists():
        shutil.move(str(preserved_cache_path), str(cache_path))


def _demo_snapshot_available(repo_root: Path) -> bool:
    snapshot = repo_root / DEMO_SNAPSHOT_PATH
    return (
        (snapshot / "raw_inputs_pdf_extraction" / "source_document_index.json").exists()
        and (snapshot / "step4_tb_bridge_workpaper" / "step4_tb_bridge_workpaper.xlsx").exists()
        and (snapshot / "step4_tb_bridge_workpaper" / "tb_bridge_workpaper.json").exists()
    )


def _demo_client_folder_from_snapshot(repo_root: Path) -> Path:
    snapshot_index = repo_root / DEMO_SNAPSHOT_PATH / "raw_inputs_pdf_extraction" / "source_document_index.json"
    payload = _read_json(snapshot_index, {})
    documents = payload.get("documents") if isinstance(payload, dict) else []
    for document in documents if isinstance(documents, list) else []:
        if not isinstance(document, dict):
            continue
        file_path = str(document.get("file_path") or "")
        if not file_path:
            continue
        path = Path(file_path)
        parts = list(path.parts)
        if "client_files" in parts:
            index = parts.index("client_files")
            return Path(*parts[: index + 1])
    return repo_root / PORTAL_ROOT / "uploads" / "demo" / "client_files"


def _restore_demo_snapshot(repo_root: Path) -> Path:
    snapshot = repo_root / DEMO_SNAPSHOT_PATH
    if not _demo_snapshot_available(repo_root):
        raise FileNotFoundError("Demo snapshot is not available yet.")
    for relative in ["raw_inputs_pdf_extraction", "step4_tb_bridge_workpaper"]:
        destination = repo_root / "outputs" / relative
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(snapshot / relative, destination, symlinks=True)
    client_folder = _demo_client_folder_from_snapshot(repo_root)
    snapshot_client_files = snapshot / "client_files"
    if snapshot_client_files.exists():
        if client_folder.exists():
            shutil.rmtree(client_folder)
        client_folder.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(snapshot_client_files, client_folder, symlinks=True)
    return client_folder


def _demo_stage_from_elapsed(elapsed_seconds: int) -> str:
    if elapsed_seconds >= DEMO_DURATION_SECONDS:
        return "completed"
    if elapsed_seconds >= DEMO_REVIEW_READY_SECONDS:
        return "turing"
    if elapsed_seconds >= DEMO_WORKBOOK_READY_SECONDS:
        return "bridge"
    if elapsed_seconds >= DEMO_SOURCE_READY_SECONDS:
        return "relationships"
    return "indexing"


def _demo_visibility(elapsed_seconds: int, status: str) -> dict[str, bool]:
    done = status == "completed" or elapsed_seconds >= DEMO_DURATION_SECONDS
    return {
        "source": done or elapsed_seconds >= DEMO_SOURCE_READY_SECONDS,
        "workbook": done or elapsed_seconds >= DEMO_REVIEW_READY_SECONDS,
        "review": done or elapsed_seconds >= DEMO_REVIEW_READY_SECONDS,
        "final": done,
    }


def _workbook_download_ready(current_stage: str, status: str, *, visibility: dict[str, bool] | None = None) -> bool:
    if status == "completed" or current_stage == "completed":
        return True
    visibility = visibility or {}
    if not visibility.get("workbook", True):
        return False
    return current_stage in {"turing", "correction"}


def _completed_workpaper_progress(repo_root: Path) -> dict[str, Any] | None:
    progress = _read_json(repo_root / PREPARE_PROGRESS_PATH, {})
    if not isinstance(progress, dict):
        return None
    if str(progress.get("stage") or "").strip() != "completed":
        return None
    if str(progress.get("status") or "").strip() != "completed":
        return None
    workbook_path = Path(str(progress.get("workbook_path") or WORKBOOK_PATH))
    if not workbook_path.is_absolute():
        workbook_path = repo_root / workbook_path
    return progress if workbook_path.exists() else None


def _process_is_running(pid_value: Any) -> bool:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _portal_job_dir(repo_root: Path, job: dict[str, Any]) -> Path | None:
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        return None
    return repo_root / PORTAL_ROOT / "jobs" / job_id


def _reconcile_running_job_status(
    repo_root: Path,
    job: dict[str, Any],
    active_process: subprocess.Popen[bytes] | None,
) -> tuple[dict[str, Any], bool]:
    if str(job.get("status") or "") != "running":
        return job, False

    updated = dict(job)
    if active_process is not None:
        exit_code = active_process.poll()
        if exit_code is not None:
            updated["status"] = "completed" if exit_code == 0 else "failed"
            updated["exit_code"] = exit_code
            updated["updated_at"] = _iso_now()
            return updated, True

    completed_progress = _completed_workpaper_progress(repo_root)
    if completed_progress is not None:
        updated["status"] = "completed"
        updated["exit_code"] = 0
        updated["message"] = completed_progress.get("message") or "prepare-workpaper completed"
        updated["updated_at"] = _iso_now()
        return updated, True

    if updated.get("pid") and not _process_is_running(updated.get("pid")):
        updated["status"] = "failed"
        updated["message"] = "prepare-workpaper stopped before writing final status; see progress checkpoints for the latest workbook state"
        updated["updated_at"] = _iso_now()
        return updated, True

    return job, False


def _safe_relative_path(name: str) -> Path:
    cleaned = unquote(name or "").replace("\\", "/").lstrip("/")
    parts = [part for part in cleaned.split("/") if part and part not in {".", ".."}]
    if not parts:
        parts = ["uploaded_file"]
    return Path(*parts)


def _safe_extract_zip(zip_path: Path, destination: Path) -> int:
    count = 0
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            relative = _safe_relative_path(member.filename)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as handle:
                shutil.copyfileobj(source, handle)
            count += 1
    return count


def _scan_files(folder: Path) -> list[dict[str, Any]]:
    if not folder.exists() or not folder.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        if any(part.startswith(".") for part in path.relative_to(folder).parts):
            continue
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "relative_path": str(path.relative_to(folder)),
                "path": str(path),
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
        )
    return files


def _prior_fs_candidates(folder: Path) -> list[dict[str, Any]]:
    scored: list[tuple[int, Path]] = []
    for path in (folder.rglob("*") if folder.exists() else []):
        if not path.is_file():
            continue
        name = path.name.lower()
        score = 0
        if "financial statement" in name or "financial statements" in name:
            score += 50
        if "fy24" in name or "2024" in name or "prior" in name:
            score += 12
        if name.endswith(".pdf"):
            score += 6
        if "tax statement" in name or "bank statement" in name or "distribution" in name:
            score -= 20
        if score > 0:
            scored.append((score, path))
    scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
    return [
        {
            "name": path.name,
            "path": str(path),
            "relative_path": str(path.relative_to(folder)),
            "score": score,
        }
        for score, path in scored[:10]
    ]


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


def _stage_from_log(log_text: str, status: str) -> str:
    if status == "completed":
        return "completed"
    if status == "failed":
        return "failed"
    if "Senior review correction round" in log_text:
        return "correction"
    if "Senior review:" in log_text:
        return "turing"
    if "Step 3/3: building TB Bridge workbook" in log_text:
        return "bridge"
    if "Step 2/3: building accounting event register" in log_text:
        return "relationships"
    if "Step 1/3: indexing source documents" in log_text:
        return "indexing"
    return "starting"


def _public_stage(current: str, status: str) -> str:
    if status == "completed" or current == "completed":
        return "ready"
    if status == "failed":
        return "needs_attention"
    if current in {"turing", "correction"}:
        return "checking"
    if status == "running":
        return "preparing"
    return "idle"


def _public_progress_message(status: str, current_stage: str, progress_message: str, elapsed_seconds: int = 0) -> str:
    elapsed = f" Running for {_format_elapsed(elapsed_seconds)}." if status == "running" and elapsed_seconds else ""
    if status == "completed" or current_stage == "completed":
        return "Workbook is ready for accountant review."
    if status == "failed":
        return "Tessa could not refresh the workbook. The engineering checker is reviewing it. Previous workbook kept if available."
    if status == "running":
        base = progress_message or (
            "Tessa is checking the prepared workpaper."
            if current_stage in {"turing", "correction"}
            else "Tessa is preparing the workpaper."
        )
        if base and base[-1] not in ".!?":
            base += "."
        return f"{base}{elapsed} Full client packs often take 60-90 minutes. You can close this page and come back later."
    return progress_message or ""


def _format_elapsed(seconds: int) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 1:
        return "less than 1 minute"
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins} minute{'s' if mins != 1 else ''}"


def _progress_stage(progress: dict[str, Any], log_text: str, status: str) -> str:
    stage = str(progress.get("stage") or "").strip()
    if stage:
        if stage == "completed":
            return "completed"
        return stage
    return _stage_from_log(log_text, status)


def _stage_status(current: str, status: str, stage: str, *, files_received: bool = False) -> str:
    order = {
        "starting": 0,
        "indexing": 1,
        "relationships": 2,
        "bridge": 3,
        "turing": 4,
        "correction": 4,
        "completed": 5,
    }
    stage_order = {"files": 0, "indexing": 1, "relationships": 2, "bridge": 3, "turing": 4, "ready": 5}
    if stage == "files" and files_received:
        return "complete"
    if status == "completed" or current == "completed":
        return "complete"
    current_order = order.get(current, 0)
    target_order = stage_order.get(stage, 0)
    if status == "failed":
        if current_order > target_order:
            return "complete"
        if current_order == target_order or stage == "ready":
            return "failed"
        return "waiting"
    if current_order > target_order:
        return "complete"
    if current_order == target_order and status == "running":
        return "running"
    return "waiting"


def _public_status_cards(current: str, status: str, *, files_received: bool = False) -> list[dict[str, str]]:
    return [
        {
            "key": "files",
            "label": "Files received",
            "description": "Client file pack is uploaded and ready.",
            "status": _stage_status(current, status, "files", files_received=files_received),
        },
        {
            "key": "indexing",
            "label": "Reading client files",
            "description": "Tessa is reading evidence and building the source index.",
            "status": _stage_status(current, status, "indexing", files_received=files_received),
        },
        {
            "key": "relationships",
            "label": "Understanding accounting movements",
            "description": "Tessa is connecting source documents, bank activity and prior-year balances.",
            "status": _stage_status(current, status, "relationships", files_received=files_received),
        },
        {
            "key": "bridge",
            "label": "Preparing Excel workpaper",
            "description": "Tessa is building the TB bridge and movement stories.",
            "status": _stage_status(current, status, "bridge", files_received=files_received),
        },
        {
            "key": "turing",
            "label": "Senior review",
            "description": "The prepared workbook is being checked before handover.",
            "status": _stage_status(current, status, "turing", files_received=files_received),
        },
        {
            "key": "ready",
            "label": "Ready",
            "description": "Download the workbook and use the row stories beside Excel.",
            "status": _stage_status(current, status, "ready", files_received=files_received),
        },
    ]


def _artifact_milestones(
    repo_root: Path,
    *,
    job: dict[str, Any] | None,
    status: str,
    current_stage: str,
    visibility: dict[str, bool] | None = None,
) -> list[dict[str, str]]:
    if not job:
        return []
    visibility = visibility or {}
    source_ready = (repo_root / SOURCE_INDEX_PATH).exists() and visibility.get("source", True)
    workbook_ready = (repo_root / WORKBOOK_PATH).exists() and visibility.get("workbook", True)
    final_ready = status == "completed" and current_stage == "completed" and workbook_ready
    review_ready = (repo_root / TURING_REVIEW_PATH).exists() and visibility.get("review", True)
    milestones = [
        {
            "label": "Evidence index ready",
            "description": "Tessa has read the uploaded evidence.",
            "status": "complete" if source_ready else "waiting",
        },
        {
            "label": "Draft workbook available",
            "description": "Excel is available while senior review continues.",
            "status": "complete" if workbook_ready else "waiting",
        },
        {
            "label": "Senior review notes ready",
            "description": "Review notes are available for accountant judgement.",
            "status": "complete" if review_ready else "waiting",
        },
        {
            "label": "Final workbook ready",
            "description": "Ready to download and review beside row stories.",
            "status": "complete" if final_ready else "waiting",
        },
    ]
    return milestones


def _public_progress(progress: dict[str, Any], message: str, status: str, current_stage: str) -> dict[str, Any]:
    return {
        "stage": _public_stage(current_stage, status),
        "status": "completed" if status == "completed" else "needs_attention" if status == "failed" else status,
        "message": message,
        "workbook_exists": bool(progress.get("workbook_exists")),
        "summary_path": progress.get("summary_path") or "",
        "workbook_path": progress.get("workbook_path") or "",
    }


def _public_running_detail(current_stage: str, document_progress: dict[str, Any]) -> dict[str, Any]:
    if current_stage == "indexing" and isinstance(document_progress, dict):
        processed = document_progress.get("processed_items", 0)
        total = document_progress.get("total_items", 0)
        return {"processed": processed, "total": total}
    return {}


class WorkpaperPortalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler], config: WorkpaperPortalConfig):
        super().__init__(server_address, handler_class)
        self.config = config
        self.state_path = config.repo_root / PORTAL_ROOT / STATE_FILE
        self.lock = threading.Lock()
        self.active_process: subprocess.Popen[bytes] | None = None

    def load_state(self) -> dict[str, Any]:
        with self.lock:
            return _read_json(self.state_path, {"client_folder": "", "job": None})

    def save_state(self, state: dict[str, Any]) -> None:
        with self.lock:
            state["updated_at"] = _iso_now()
            _write_json(self.state_path, state)


class WorkpaperPortalHandler(BaseHTTPRequestHandler):
    server: WorkpaperPortalServer

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(_INDEX_HTML)
            return
        if parsed.path == "/api/state":
            self._send_json(self._state_payload())
            return
        if parsed.path == "/download/workbook":
            self._send_file(self.server.config.repo_root / WORKBOOK_PATH, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            return
        if parsed.path == "/download/summary":
            self._send_file(self.server.config.repo_root / SUMMARY_PATH, "text/markdown; charset=utf-8")
            return
        if parsed.path == "/open/source":
            self._open_source_file(parsed)
            return
        self._send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self._handle_upload()
            return
        if parsed.path == "/api/start":
            self._start_job()
            return
        if parsed.path == "/api/demo":
            self._start_demo()
            return
        if parsed.path == "/api/reset":
            self._reset_state()
            return
        self._send_json({"error": "Not found"}, status=404)

    def _state_payload(self) -> dict[str, Any]:
        state = self.server.load_state()
        repo = self.server.config.repo_root
        client_folder_raw = str(state.get("client_folder") or "").strip()
        client_folder: Path | None = Path(client_folder_raw).expanduser() if client_folder_raw else None
        if client_folder is not None and not client_folder.is_absolute():
            client_folder = (repo / client_folder).resolve()
        job = state.get("job") if isinstance(state.get("job"), dict) else None
        is_demo = bool(job and job.get("demo"))
        if is_demo and job and job.get("status") == "running":
            elapsed_for_demo = _elapsed_seconds_since(job.get("started_at"))
            if elapsed_for_demo >= DEMO_DURATION_SECONDS:
                job["status"] = "completed"
                job["exit_code"] = 0
                job["updated_at"] = _iso_now()
                state["job"] = job
                self.server.save_state(state)
        elif job and job.get("status") == "running":
            job, changed = _reconcile_running_job_status(repo, job, self.server.active_process)
            if changed:
                state["job"] = job
                self.server.save_state(state)
                job_dir = _portal_job_dir(repo, job)
                if job_dir is not None:
                    _write_portal_job_status(job_dir, job)
        log_text = ""
        if job and job.get("log_path"):
            log_path = Path(str(job["log_path"]))
            if log_path.exists():
                log_text = log_path.read_text(encoding="utf-8", errors="ignore")
        status = str(job.get("status") if job else "idle")
        progress = _read_json(repo / PREPARE_PROGRESS_PATH, {}) if job else {}
        document_progress = _read_json(repo / DOCUMENT_PROGRESS_PATH, {}) if job else {}
        relationship_progress = _read_json(repo / RELATIONSHIP_PROGRESS_PATH, {}) if job else {}
        bridge_progress = _read_json(repo / TB_BRIDGE_PROGRESS_PATH, {}) if job else {}
        elapsed_seconds = _elapsed_seconds_since(job.get("started_at")) if job else 0
        demo_visibility = _demo_visibility(elapsed_seconds, status) if is_demo else {}
        if is_demo and job:
            current_stage = "completed" if status == "completed" else _demo_stage_from_elapsed(elapsed_seconds)
        else:
            current_stage = _progress_stage(progress if isinstance(progress, dict) else {}, log_text, status)
        workbook = repo / WORKBOOK_PATH
        summary = repo / SUMMARY_PATH
        turing = _turing_summary(repo) if job and demo_visibility.get("review", True) else {"status": "", "summary": {}, "findings": []}
        counts = _artifact_counts(repo) if job else {"documents": 0, "matrix_rows": 0, "movement_notes": 0, "movement_columns": 0}
        workbook_ready_for_download = bool(job) and workbook.exists() and _workbook_download_ready(
            current_stage,
            status,
            visibility=demo_visibility,
        )
        if is_demo and job:
            if not demo_visibility.get("source"):
                counts["documents"] = 0
            if not workbook_ready_for_download:
                counts["matrix_rows"] = 0
                counts["movement_notes"] = 0
                counts["movement_columns"] = 0
        progress_message = str(progress.get("message") or "") if isinstance(progress, dict) else ""
        if is_demo and status == "running":
            if current_stage == "indexing":
                progress_message = "Tessa is reading the demo client files and building the evidence index."
            elif current_stage == "relationships":
                progress_message = "Tessa is understanding the accounting movements and source-to-bank relationships."
            elif current_stage == "bridge":
                progress_message = "Tessa is preparing the Excel workpaper and movement stories."
            elif current_stage == "turing":
                progress_message = "Senior review is checking the prepared workbook."
        if not is_demo and status == "running" and current_stage == "indexing" and isinstance(document_progress, dict) and document_progress:
            processed = document_progress.get("processed_items", 0)
            total = document_progress.get("total_items", 0)
            current_document = document_progress.get("current_document") or ""
            progress_message = f"Reading documents: {processed}/{total}"
            if current_document:
                progress_message += f" — {current_document}"
        elif not is_demo and status == "running" and current_stage == "relationships" and isinstance(relationship_progress, dict) and relationship_progress:
            progress_message = str(relationship_progress.get("message") or progress_message or "Investigating accounting relationships.")
        elif not is_demo and status == "running" and current_stage in {"bridge", "turing", "correction"} and isinstance(bridge_progress, dict) and bridge_progress:
            progress_message = str(bridge_progress.get("message") or progress_message or "Preparing the TB bridge workbook.")
        public_message = _public_progress_message(status, current_stage, progress_message, elapsed_seconds)
        return {
            "client_folder": str(client_folder) if client_folder else "",
            "upload_label": state.get("upload_label") or (client_folder.name if client_folder else ""),
            "files": _scan_files(client_folder)[:200] if client_folder else [],
            "file_count": len(_scan_files(client_folder)) if client_folder else 0,
            "prior_fs_candidates": _prior_fs_candidates(client_folder) if client_folder else [],
            "job": job,
            "demo_available": _demo_snapshot_available(repo),
            "demo_mode": is_demo,
            "elapsed_seconds": elapsed_seconds,
            "elapsed_label": _format_elapsed(elapsed_seconds) if elapsed_seconds else "",
            "expected_duration_label": "60-90 minutes for a full client pack",
            "current_stage": _public_stage(current_stage, status),
            "stages": _public_status_cards(current_stage, status, files_received=bool(client_folder)),
            "milestones": _artifact_milestones(repo, job=job, status=status, current_stage=current_stage, visibility=demo_visibility),
            "progress": _public_progress(progress if isinstance(progress, dict) else {}, public_message, status, current_stage),
            "running_detail": _public_running_detail(current_stage, document_progress if isinstance(document_progress, dict) else {}),
            "progress_message": public_message,
            "artifacts": {
                "workbook_exists": workbook_ready_for_download,
                "workbook_path": str(workbook),
                "summary_exists": bool(job) and summary.exists() and demo_visibility.get("review", True),
                "summary_path": str(summary),
            },
            "counts": counts,
            "turing": turing,
            "evidence_index": _evidence_index_preview(repo) if job and demo_visibility.get("source", True) else [],
            "movement_notes": _movement_notes_preview(repo) if workbook_ready_for_download else [],
            "summary_text": _read_summary_text(repo) if job and demo_visibility.get("review", True) else "",
        }

    def _handle_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_json({"error": "Expected multipart upload"}, status=400)
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
            },
        )
        items = form["files"] if "files" in form else []
        if not isinstance(items, list):
            items = [items]
        upload_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        upload_root = self.server.config.repo_root / PORTAL_ROOT / "uploads" / upload_id
        client_folder = upload_root / "client_files"
        client_folder.mkdir(parents=True, exist_ok=True)
        saved = 0
        extracted = 0
        for item in items:
            filename = getattr(item, "filename", "") or "uploaded_file"
            if not getattr(item, "file", None):
                continue
            relative = _safe_relative_path(filename)
            target = upload_root / "raw_uploads" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                shutil.copyfileobj(item.file, handle)
            saved += 1
            if target.suffix.lower() == ".zip" and zipfile.is_zipfile(target):
                extracted += _safe_extract_zip(target, client_folder)
            else:
                final = client_folder / relative
                final.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, final)
                extracted += 1
        if not extracted:
            self._send_json({"error": "No usable files were uploaded"}, status=400)
            return
        state = self.server.load_state()
        state.update(
            {
                "client_folder": str(client_folder),
                "upload_label": f"Upload {upload_id}",
                "uploaded_at": _iso_now(),
                "uploaded_files": saved,
                "extracted_files": extracted,
                "job": None,
            }
        )
        self.server.save_state(state)
        self._send_json({"ok": True, "client_folder": str(client_folder), "uploaded_files": saved, "extracted_files": extracted})

    def _start_job(self) -> None:
        if self.server.active_process is not None and self.server.active_process.poll() is None:
            self._send_json({"error": "A workpaper job is already running"}, status=409)
            return
        body = self._read_json_body()
        state = self.server.load_state()
        client_folder = Path(str(state.get("client_folder") or "")).expanduser()
        if not client_folder.is_absolute():
            client_folder = (self.server.config.repo_root / client_folder).resolve()
        if not client_folder.exists() or not client_folder.is_dir():
            self._send_json({"error": "Upload or choose a client folder first"}, status=400)
            return
        job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        job_dir = self.server.config.repo_root / PORTAL_ROOT / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        log_path = job_dir / "prepare-workpaper.log"
        command = [
            sys.executable,
            "-u",
            "-m",
            "accountant_copilot.cli",
            "prepare-workpaper",
            "--client-folder",
            str(client_folder),
            "--codex-command",
            str(body.get("codex_command") or "codex exec"),
            "--codex-timeout",
            str(int(body.get("codex_timeout") or 1200)),
            "--codex-max-attempts",
            str(int(body.get("codex_max_attempts") or 3)),
            "--batch-size",
            str(int(body.get("batch_size") or 5)),
            "--review-correction-rounds",
            str(int(body.get("review_correction_rounds") or 2)),
        ]
        allow_cache = bool(body.get("allow_cache"))
        command.append("--allow-cache" if allow_cache else "--force-reprocess")
        prior_fs_file = str(body.get("prior_fs_file") or "").strip()
        if prior_fs_file:
            command.extend(["--prior-fs-file", prior_fs_file])
        entity_name = str(body.get("entity_name") or "").strip()
        if entity_name:
            command.extend(["--entity-name", entity_name])
        fy_start = str(body.get("fy_start") or "").strip()
        if fy_start:
            command.extend(["--fy-start", fy_start])
        fy_end = str(body.get("fy_end") or "").strip()
        if fy_end:
            command.extend(["--fy-end", fy_end])
        env = os.environ.copy()
        src_path = str(self.server.config.repo_root / "src")
        env["PYTHONPATH"] = src_path + os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else src_path
        log_handle = log_path.open("wb", buffering=0)
        process = subprocess.Popen(
            command,
            cwd=self.server.config.repo_root,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        self.server.active_process = process
        (job_dir / "pid").write_text(str(process.pid), encoding="utf-8")
        job = {
            "job_id": job_id,
            "status": "running",
            "pid": process.pid,
            "client_folder": str(client_folder),
            "log_path": str(log_path),
            "started_at": _iso_now(),
            "updated_at": _iso_now(),
            "command": " ".join(command[:5] + ["..."]),
            "reading_mode": "reuse_previous" if allow_cache else "fresh",
        }
        _write_portal_job_status(job_dir, job)
        engineer_watcher_pid = _start_engineer_watcher(self.server.config.repo_root, job_dir)
        if engineer_watcher_pid is not None:
            job["engineer_watcher_pid"] = engineer_watcher_pid
            _write_portal_job_status(job_dir, job)
        state["job"] = job
        self.server.save_state(state)
        threading.Thread(target=self._wait_for_job, args=(process, log_handle, job_id, job_dir), daemon=True).start()
        self._send_json({"ok": True, "job": job})

    def _start_demo(self) -> None:
        if self.server.active_process is not None and self.server.active_process.poll() is None:
            self._send_json({"error": "A workpaper job is already running"}, status=409)
            return
        try:
            client_folder = _restore_demo_snapshot(self.server.config.repo_root)
        except Exception as exc:
            self._send_json({"error": f"Demo workpaper is not available: {exc}"}, status=404)
            return
        job_id = datetime.now(timezone.utc).strftime("demo-%Y%m%dT%H%M%SZ")
        job_dir = self.server.config.repo_root / PORTAL_ROOT / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        log_path = job_dir / "prepare-workpaper.log"
        log_path.write_text(
            "Demo replay started.\n"
            "Using the latest completed XYZ/Tenet Legacy workpaper artifacts.\n"
            "No live AI run is being charged for this demo replay.\n",
            encoding="utf-8",
        )
        job = {
            "job_id": job_id,
            "status": "running",
            "pid": None,
            "client_folder": str(client_folder),
            "log_path": str(log_path),
            "started_at": _iso_now(),
            "updated_at": _iso_now(),
            "command": "demo replay",
            "reading_mode": "demo_snapshot",
            "demo": True,
            "demo_duration_seconds": DEMO_DURATION_SECONDS,
        }
        _write_portal_job_status(job_dir, job)
        state = self.server.load_state()
        state.update(
            {
                "client_folder": str(client_folder),
                "upload_label": "Demo client pack",
                "uploaded_at": _iso_now(),
                "uploaded_files": len(_scan_files(client_folder)),
                "extracted_files": len(_scan_files(client_folder)),
                "job": job,
            }
        )
        self.server.active_process = None
        self.server.save_state(state)
        self._send_json({"ok": True, "job": job})

    def _reset_state(self) -> None:
        if self.server.active_process is not None and self.server.active_process.poll() is None:
            self._send_json(
                {"error": "Tessa is still preparing a workpaper. Wait for it to finish before starting over."},
                status=409,
            )
            return
        self.server.active_process = None
        _clear_generated_workpaper_state(self.server.config.repo_root)
        self._send_json({"ok": True})

    def _wait_for_job(self, process: subprocess.Popen[bytes], log_handle: Any, job_id: str, job_dir: Path) -> None:
        exit_code = process.wait()
        try:
            log_handle.close()
        except Exception:
            pass
        state = self.server.load_state()
        job = state.get("job") if isinstance(state.get("job"), dict) else {}
        if job.get("job_id") == job_id:
            job["status"] = "completed" if exit_code == 0 else "failed"
            job["exit_code"] = exit_code
            job["updated_at"] = _iso_now()
            state["job"] = job
            self.server.save_state(state)
            _write_portal_job_status(job_dir, job)
            if exit_code != 0:
                engineer_check_pid = _start_engineer_check(self.server.config.repo_root, job_dir, "failed")
                if engineer_check_pid is not None:
                    job["engineer_check_pid"] = engineer_check_pid
                    job["updated_at"] = _iso_now()
                    state["job"] = job
                    self.server.save_state(state)
                    _write_portal_job_status(job_dir, job)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, content: str, status: int = 200) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str | None = None) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "File not available"}, status=404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def _open_source_file(self, parsed: Any) -> None:
        query = parse_qs(parsed.query or "")
        raw_path = unquote((query.get("path") or [""])[0])
        if not raw_path:
            self._send_json({"error": "Missing source path"}, status=400)
            return
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (self.server.config.repo_root / path).resolve()
        else:
            path = path.resolve()
        repo = self.server.config.repo_root.resolve()
        try:
            path.relative_to(repo)
        except ValueError:
            self._send_json({"error": "Source path is outside the workpaper workspace"}, status=403)
            return
        if not path.exists() or not path.is_file():
            self._send_json({"error": "Source file not available"}, status=404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)


def serve_workpaper_portal(repo_root: Path, host: str = "127.0.0.1", port: int = 8787) -> None:
    config = WorkpaperPortalConfig(repo_root=repo_root.resolve(), host=host, port=port)
    server = WorkpaperPortalServer((host, port), WorkpaperPortalHandler, config)
    url = f"http://{host}:{port}"
    print(f"Starting accountant workpaper portal on {url}")
    print(f"Repo: {config.repo_root}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping accountant workpaper portal.")
    finally:
        server.server_close()


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Workpaper Portal</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f4f7f8;
      color: #222b35;
      font-synthesis: none;
      text-rendering: optimizeLegibility;
      -webkit-font-smoothing: antialiased;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f4f7f8; }
    button, input, select, textarea { font: inherit; }
    .shell { padding: 24px; display: grid; gap: 16px; }
    header { display: flex; justify-content: space-between; align-items: flex-end; gap: 20px; }
    h1 { margin: 0; font-size: 28px; line-height: 1.1; letter-spacing: 0; }
    .eyebrow { margin: 0 0 5px; color: #647383; font-weight: 800; font-size: 12px; text-transform: uppercase; letter-spacing: 0; }
    .muted { color: #677586; }
    .grid { display: grid; grid-template-columns: 420px minmax(0, 1fr); gap: 16px; align-items: start; }
    .panel { border: 1px solid #d7e1e8; border-radius: 8px; background: white; overflow: hidden; }
    .full-width { grid-column: 1 / -1; }
    .panel-body { padding: 16px; display: grid; gap: 14px; }
    .panel-title { padding: 14px 16px; border-bottom: 1px solid #e4eaef; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .panel-title h2 { margin: 0; font-size: 16px; }
    .stack { display: grid; gap: 10px; }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .check-row { display: flex; gap: 8px; align-items: flex-start; color: #536273; font-size: 13px; line-height: 1.35; }
    .check-row input { margin-top: 2px; }
    .button, button, label.upload {
      display: inline-flex; align-items: center; justify-content: center; gap: 8px;
      min-height: 40px; border-radius: 8px; border: 1px solid #cbd7df; background: white;
      padding: 0 14px; font-weight: 850; color: #24313d; cursor: pointer; text-decoration: none;
    }
    button.primary, .button.primary, label.upload { background: #107c68; border-color: #107c68; color: white; }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    input[type="text"], select {
      width: 100%; min-height: 40px; border: 1px solid #cbd7df; border-radius: 8px; padding: 0 12px; background: white;
    }
    .summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
    .metric { border: 1px solid #d7e1e8; border-radius: 8px; background: white; padding: 12px 14px; }
    .metric strong { display: block; font-size: 24px; }
    .metric span { color: #6a7785; font-size: 13px; font-weight: 700; }
    .steps { display: grid; grid-template-columns: repeat(6, minmax(130px, 1fr)); gap: 10px; }
    .step { border: 1px solid #d7e1e8; border-radius: 8px; background: #fff; padding: 12px; display: grid; gap: 8px; min-height: 92px; }
    .step strong { font-size: 14px; }
    .step span.description { color: #667585; font-size: 12px; line-height: 1.35; }
    .status { width: fit-content; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 900; background: #edf2f6; color: #4c5b6a; }
    .status.complete, .status.completed { background: #e1f3ec; color: #0b6c58; }
    .status.running { background: #fff1ce; color: #835900; }
    .status.failed { background: #fde7e7; color: #a0333d; }
    .table-wrap { max-height: 360px; overflow: auto; border: 1px solid #e3e9ee; border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #e6edf1; padding: 9px 10px; text-align: left; vertical-align: top; font-size: 13px; }
    th { position: sticky; top: 0; background: #f0f4f6; color: #5f6c7a; text-transform: uppercase; font-size: 11px; letter-spacing: 0; }
    .output { display: grid; gap: 12px; }
    .callout { border: 1px solid #cfe2dc; background: #f4fbf8; border-radius: 8px; padding: 14px; }
    .callout.warn { border-color: #edd28d; background: #fff9e9; }
    .callout.fail { border-color: #efb5b9; background: #fff3f3; }
    .milestones { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .milestone { border: 1px solid #e0e8ee; border-radius: 8px; padding: 10px 12px; background: #fff; display: grid; gap: 4px; }
    .milestone.complete { border-color: #b8ddcf; background: #f4fbf8; }
    .milestone strong { font-size: 13px; }
    .milestone span { color: #687688; font-size: 12px; line-height: 1.35; }
    .findings { display: grid; gap: 8px; }
    .finding { border: 1px solid #e0e8ee; border-radius: 8px; padding: 10px 12px; background: #fff; }
    .finding strong { display: block; margin-bottom: 3px; }
    .notes-grid { display: grid; grid-template-columns: minmax(260px, 340px) minmax(640px, 1fr); gap: 16px; align-items: start; }
    .note-list { max-height: 620px; overflow: auto; display: grid; gap: 8px; }
    .note-item {
      width: 100%; min-height: auto; display: grid; gap: 6px; justify-content: stretch; text-align: left;
      border-color: #dde6ec; background: #fff; padding: 10px 12px;
    }
    .note-item.selected { border-color: #107c68; box-shadow: 0 0 0 2px rgba(16, 124, 104, 0.12); }
    .note-item .note-top { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
    .note-item strong { overflow-wrap: anywhere; }
    .note-detail { border: 1px solid #d7e1e8; border-radius: 8px; background: #fbfdfe; padding: 18px; display: grid; gap: 16px; min-height: 520px; }
    .note-detail h3 { margin: 0; font-size: 20px; line-height: 1.2; }
    .note-meta { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .note-metric { border: 1px solid #dfe8ee; background: white; border-radius: 8px; padding: 9px 10px; }
    .note-metric span { display: block; color: #687688; font-size: 11px; font-weight: 850; text-transform: uppercase; }
    .note-metric strong { display: block; margin-top: 4px; overflow-wrap: anywhere; }
    .story-block { display: grid; gap: 5px; }
    .story-block h4 { margin: 0; font-size: 13px; color: #667585; text-transform: uppercase; letter-spacing: 0; }
    .story-block p { margin: 0; line-height: 1.45; }
    .evidence-links { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .evidence-link {
      border: 1px solid #dce5eb; border-radius: 8px; background: white; padding: 10px 12px; text-decoration: none; color: #25313d;
      display: grid; gap: 4px;
    }
    .table-link { color: #0f6e5d; font-weight: 850; text-decoration: none; }
    .table-link:hover { text-decoration: underline; }
    .evidence-link strong { overflow-wrap: anywhere; }
    .evidence-link span { color: #687688; font-size: 12px; }
    .pill { display: inline-flex; width: fit-content; border-radius: 999px; padding: 3px 8px; background: #edf2f6; color: #4c5b6a; font-size: 11px; font-weight: 900; }
    .pill.ready { background: #e1f3ec; color: #0b6c58; }
    .pill.needs_attention { background: #fff1ce; color: #835900; }
    .pill.not_posted, .pill.excluded { background: #eef2f5; color: #556373; }
    pre { margin: 0; overflow: auto; white-space: pre-wrap; font-size: 12px; color: #566472; }
    .small { font-size: 12px; }
    @media (max-width: 980px) {
      .shell { padding: 14px; }
      header { align-items: flex-start; flex-direction: column; }
      .grid { grid-template-columns: 1fr; }
      .summary, .steps, .milestones { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .notes-grid { grid-template-columns: 1fr; }
      .note-meta { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .evidence-links { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      .summary, .steps, .milestones { grid-template-columns: 1fr; }
      .note-meta { grid-template-columns: 1fr; }
      .row { align-items: stretch; }
      .button, button, label.upload { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div>
        <p class="eyebrow">Tenet Legacy</p>
        <h1>Hi, I’m Tessa, your AI workpaper assistant.</h1>
        <p class="muted">I’ll help prepare the financial-statement workpaper. Upload the client file pack, and I’ll prepare the Excel workbook with row stories and review notes.</p>
      </div>
      <div class="row">
        <button id="demoBtn">Load demo</button>
        <button id="refreshBtn">Refresh status</button>
        <button id="resetBtn">Start over</button>
      </div>
    </header>

    <section class="summary" id="summary"></section>

    <section class="grid">
      <aside class="panel">
        <div class="panel-title"><h2>Client files</h2><span class="status" id="fileCount">0 files</span></div>
        <div class="panel-body">
          <label class="upload" id="folderUploadLabel">
            Upload folder
            <input id="fileInput" hidden type="file" multiple webkitdirectory directory mozdirectory />
          </label>
          <p class="muted small" id="uploadStatus">No folder uploaded yet.</p>
          <div class="stack">
            <label class="small muted" for="priorFs">Prior-year financial statement</label>
            <select id="priorFs"><option value="">Auto detect</option></select>
          </div>
          <div class="stack">
            <label class="small muted">Target financial year</label>
            <div class="row" style="flex-wrap: nowrap;">
              <input id="fyStart" type="text" placeholder="Start, e.g. 2024-07-01" />
              <input id="fyEnd" type="text" placeholder="End, e.g. 2025-06-30" />
            </div>
          </div>
          <label class="check-row">
            <input id="allowCache" type="checkbox" />
            <span>Reuse previous AI reading when the same files were already read. Leave off for a fresh run.</span>
          </label>
          <button class="primary" id="startBtn" title="Upload a folder first">Prepare Excel workpaper</button>
          <p class="muted small" id="clientFolder"></p>
        </div>
      </aside>

      <main class="stack">
        <section class="steps" id="steps"></section>
        <section class="panel output">
          <div class="panel-title"><h2>Workpaper status</h2><span class="status" id="jobStatus">Idle</span></div>
          <div class="panel-body">
            <div id="outputMessage" class="callout">Upload files, then prepare the workpaper.</div>
            <div class="milestones" id="milestones"></div>
            <div class="row" id="downloadRow" style="display:none;">
              <a class="button primary" href="/download/workbook">Download Excel workbook</a>
              <a class="button" href="/download/summary">Download summary</a>
            </div>
            <div class="findings" id="findings"></div>
          </div>
        </section>
        <section class="panel" id="evidencePreviewPanel">
          <div class="panel-title"><h2 id="evidencePanelTitle">Uploaded files</h2><span class="muted small" id="uploadLabel"></span></div>
          <div class="table-wrap">
            <table>
              <thead id="filesTableHead"><tr><th>File</th><th>Size</th><th>Modified</th></tr></thead>
              <tbody id="filesTable"></tbody>
            </table>
          </div>
        </section>
      </main>

      <section class="panel full-width" id="movementNotesPanel" style="display:none;">
        <div class="panel-title">
          <h2>Movement stories</h2>
          <span class="muted small" id="movementNotesCount"></span>
        </div>
        <div class="panel-body">
          <p class="muted small">Use this beside Excel. Search the Note ID from the TB Bridge, then read the row story and open the supporting evidence.</p>
          <input id="noteSearch" type="text" placeholder="Search note ID, account, amount, column, or evidence..." />
          <div class="notes-grid">
            <div class="note-list" id="movementNotesList"></div>
            <div class="note-detail" id="movementNoteDetail"></div>
          </div>
        </div>
      </section>
    </section>
  </div>

  <script>
    const state = { polling: null, latestData: null, selectedNoteId: "" };
    const $ = (id) => document.getElementById(id);
    const money = (n) => Number(n || 0).toLocaleString("en-AU");
    function statusClass(value) { return `status ${value || ""}`; }
    async function api(path, options = {}) {
      const response = await fetch(path, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
      return data;
    }
    async function uploadFiles(files, input) {
      const uploadStatus = $("uploadStatus");
      if (!files || !files.length) {
        uploadStatus.textContent = "No files were selected. Choose the client folder that contains the source documents.";
        return;
      }
      uploadStatus.textContent = `Uploading ${files.length.toLocaleString("en-AU")} file(s)...`;
      $("startBtn").disabled = true;
      const form = new FormData();
      Array.from(files).forEach((file) => form.append("files", file, file.webkitRelativePath || file.name));
      try {
        const result = await api("/api/upload", { method: "POST", body: form });
        uploadStatus.textContent = `Uploaded ${(result.extracted_files || files.length).toLocaleString("en-AU")} file(s). Ready to prepare the workpaper.`;
        await refresh();
      } finally {
        if (input) input.value = "";
      }
    }
    async function refresh() {
      const data = await api("/api/state");
      render(data);
      if (data.job?.status === "running" && !state.polling) {
        state.polling = setInterval(refresh, 5000);
      }
      if (data.job?.status !== "running" && state.polling) {
        clearInterval(state.polling);
        state.polling = null;
      }
    }
    function render(data) {
      state.latestData = data;
      $("summary").innerHTML = [
        ["Files", data.file_count || 0],
        ["Evidence read", data.counts?.documents || 0],
        ["Workpaper rows", data.counts?.matrix_rows || 0],
        ["Row stories", data.counts?.movement_notes || 0],
      ].map(([label, value]) => `<div class="metric"><strong>${money(value)}</strong><span>${label}</span></div>`).join("");
      $("fileCount").textContent = `${data.file_count || 0} files`;
      $("clientFolder").textContent = data.client_folder || "";
      $("uploadLabel").textContent = data.upload_label || "";
      if (data.file_count > 0 && !data.job) {
        $("uploadStatus").textContent = `${data.file_count.toLocaleString("en-AU")} file(s) uploaded. Ready to prepare the workpaper.`;
      } else if (!data.client_folder && !data.job) {
        $("uploadStatus").textContent = "No folder uploaded yet.";
      }
      $("jobStatus").textContent = data.job?.status || "Idle";
      $("jobStatus").className = statusClass(data.job?.status || "");
      $("steps").innerHTML = (data.stages || []).map((step) => `
        <div class="step">
          <span class="${statusClass(step.status)}">${step.status}</span>
          <strong>${step.label}</strong>
          <span class="description">${escapeHtml(step.description || "")}</span>
        </div>`).join("");
      $("milestones").innerHTML = (data.milestones || []).map((item) => `
        <div class="milestone ${escapeAttr(item.status || "")}">
          <strong>${escapeHtml(item.label || "")}</strong>
          <span>${escapeHtml(item.description || "")}</span>
        </div>`).join("");
      const evidenceRows = data.evidence_index || [];
      if (evidenceRows.length) {
        $("evidencePanelTitle").textContent = "Evidence index";
        $("uploadLabel").textContent = `${evidenceRows.length.toLocaleString("en-AU")} document${evidenceRows.length === 1 ? "" : "s"} read`;
        $("filesTableHead").innerHTML = `<tr><th>Original file</th><th>Tessa name</th><th>Type</th><th>Status</th><th>PDF</th></tr>`;
        $("filesTable").innerHTML = evidenceRows.map((row) => `
          <tr>
            <td>${escapeHtml(row.original_file_name || "")}</td>
            <td><strong>${escapeHtml(row.display_name || "")}</strong></td>
            <td>${escapeHtml(formatLabel(row.document_type || ""))}</td>
            <td><span class="pill ${escapeAttr(row.entity_relevance || "")}">${escapeHtml(formatLabel(row.entity_relevance || "read"))}</span></td>
            <td>${row.open_url ? `<a class="table-link" target="_blank" rel="noreferrer" href="${escapeAttr(row.open_url)}">Open</a>` : ""}</td>
          </tr>`).join("");
      } else {
        $("evidencePanelTitle").textContent = "Uploaded files";
        $("uploadLabel").textContent = data.upload_label || "";
        $("filesTableHead").innerHTML = `<tr><th>File</th><th>Size</th><th>Modified</th></tr>`;
        $("filesTable").innerHTML = (data.files || []).slice(0, 80).map((file) => `
          <tr>
            <td>${escapeHtml(file.relative_path || file.name)}</td>
            <td>${((file.size || 0) / 1024).toLocaleString("en-AU", { maximumFractionDigits: 1 })} KB</td>
            <td>${file.modified_at ? new Date(file.modified_at).toLocaleString() : ""}</td>
          </tr>`).join("") || `<tr><td colspan="3" class="muted">No files selected.</td></tr>`;
      }
      const prior = $("priorFs");
      const old = prior.value;
      prior.innerHTML = `<option value="">Auto detect</option>` + (data.prior_fs_candidates || []).map((item) => `
        <option value="${escapeAttr(item.path)}">${escapeHtml(item.relative_path || item.name)}</option>`).join("");
      if (old) prior.value = old;
      const running = data.job?.status === "running";
      $("startBtn").disabled = running || !(data.client_folder);
      $("startBtn").title = running ? "Tessa is already preparing a workpaper" : (data.client_folder ? "Prepare the Excel workpaper" : "Upload a folder first");
      $("demoBtn").disabled = running || !data.demo_available;
      $("demoBtn").title = data.demo_available ? "Replay the latest completed demo workpaper" : "No demo snapshot is available yet";
      $("resetBtn").disabled = running;
      const message = $("outputMessage");
      const download = $("downloadRow");
      const turingStatus = data.turing?.status || "";
      if (data.job?.status === "completed") {
        message.className = "callout";
        const attention = data.progress?.status === "needs_attention" || (data.turing?.findings || []).length > 0;
        message.innerHTML = attention
          ? `<strong>Tessa prepared the workbook and found review notes.</strong><br/>The workbook is available. Use Movement stories and Review notes beside Excel.`
          : `<strong>Final workbook ready.</strong><br/>Review status: ${escapeHtml(turingStatus || "not available")}.`;
        download.style.display = data.artifacts?.workbook_exists ? "flex" : "none";
      } else if (data.job?.status === "failed") {
        message.className = "callout fail";
        const restored = data.progress?.last_good_restored;
        message.innerHTML = restored
          ? `<strong>Tessa could not refresh the workbook.</strong><br/>The engineering checker is reviewing it. Previous workbook kept.`
          : `<strong>Tessa could not refresh the workbook.</strong><br/>The engineering checker is reviewing it.`;
        download.style.display = data.artifacts?.workbook_exists ? "flex" : "none";
      } else if (running) {
        message.className = "callout warn";
        const draftReady = !!data.artifacts?.workbook_exists;
        const elapsed = data.elapsed_label ? ` Running for ${escapeHtml(data.elapsed_label)}.` : "";
        message.innerHTML = draftReady
          ? `<strong>Draft workbook available.</strong><br/>Senior review is still checking it.${elapsed} Full client packs often take ${escapeHtml(data.expected_duration_label || "60-90 minutes")}. You can close this page and come back later.`
          : `<strong>Tessa is preparing the workpaper.</strong><br/>${escapeHtml(data.progress_message || "This usually takes 60-90 minutes for a full client pack. You can close this page and come back later.")}`;
        download.style.display = draftReady ? "flex" : "none";
      } else {
        message.className = "callout";
        message.textContent = "Upload files, then prepare the workpaper.";
        download.style.display = data.artifacts?.workbook_exists ? "flex" : "none";
      }
      const findings = data.turing?.findings || [];
      const internalNotes = data.turing?.internal_note_count || 0;
      $("findings").innerHTML = findings.length
        ? findings.slice(0, 8).map((finding) => `
          <div class="finding">
            <strong>${escapeHtml(finding.title || finding.category || "Review note")}</strong>
            <span>${escapeHtml(finding.body || finding.message || "")}</span>
            ${finding.check ? `<span class="muted small">${escapeHtml(finding.check)}</span>` : ""}
          </div>`).join("")
        : internalNotes
          ? `<div class="finding"><strong>Review notes handled internally</strong><span>Tessa kept ${internalNotes.toLocaleString("en-AU")} low-risk review note${internalNotes === 1 ? "" : "s"} in the audit trail.</span></div>`
          : "";
      renderMovementNotes(data.movement_notes || []);
    }
    function noteSearchBlob(note) {
      return [
        note.note_id, note.tb_row, note.account_name, note.statement_section, note.statement_group, note.status,
        note.tb_column, note.opening_balance, note.closing_balance, note.main_amount, note.other_amounts,
        note.explanation, note.calculation, note.evidence_summary, note.check_hint,
        ...(note.context_stories || []),
        ...(note.evidence_docs || []).flatMap((doc) => [doc.display_name, doc.document_type, doc.period, doc.document_id]),
      ].join(" ").toLowerCase();
    }
    function renderMovementNotes(notes) {
      const panel = $("movementNotesPanel");
      const count = $("movementNotesCount");
      const list = $("movementNotesList");
      const detail = $("movementNoteDetail");
      if (!notes.length) {
        panel.style.display = "none";
        $("evidencePreviewPanel").style.display = "block";
        return;
      }
      panel.style.display = "block";
      $("evidencePreviewPanel").style.display = "block";
      count.textContent = `${notes.length.toLocaleString("en-AU")} row notes`;
      const query = ($("noteSearch").value || "").trim().toLowerCase();
      const filtered = query ? notes.filter((note) => noteSearchBlob(note).includes(query)) : notes;
      if (!filtered.some((note) => note.note_id === state.selectedNoteId)) {
        state.selectedNoteId = filtered[0]?.note_id || "";
      }
      const selected = filtered.find((note) => note.note_id === state.selectedNoteId) || filtered[0];
      list.innerHTML = filtered.slice(0, 120).map((note) => `
        <button class="note-item ${note.note_id === selected?.note_id ? "selected" : ""}" data-note-id="${escapeAttr(note.note_id)}">
          <span class="note-top">
            <span class="pill ${escapeAttr(note.status || "")}">${escapeHtml(note.status || "review")}</span>
            <span class="muted small">${escapeHtml(note.note_id || "")}${note.tb_row ? ` · row ${escapeHtml(note.tb_row)}` : ""}</span>
          </span>
          <strong>${escapeHtml(note.account_name || "Unnamed row")}</strong>
          <span class="muted small">${escapeHtml(note.tb_column || "No movement")}</span>
        </button>`).join("") || `<p class="muted">No movement notes match this search.</p>`;
      list.querySelectorAll("[data-note-id]").forEach((button) => {
        button.addEventListener("click", () => {
          state.selectedNoteId = button.getAttribute("data-note-id") || "";
          renderMovementNotes(state.latestData?.movement_notes || []);
        });
      });
      if (!selected) {
        detail.innerHTML = `<p class="muted">Search for a Note ID from Excel, e.g. R006.</p>`;
        return;
      }
      const evidenceLinks = (selected.evidence_docs || []).map((doc) => `
        <a class="evidence-link" target="_blank" rel="noreferrer" href="${escapeAttr(doc.open_url || "#")}">
          <strong>${escapeHtml(doc.display_name || doc.document_id || "Open source")}</strong>
          <span>${escapeHtml([doc.document_type, doc.period].filter(Boolean).join(" · "))}</span>
        </a>`).join("");
      const stories = (selected.context_stories || []).map((story) => `<li>${escapeHtml(story)}</li>`).join("");
      const rowTutorial = selected.row_tutorial || null;
      const movementRows = rowTutorial?.movements?.length
        ? `<div class="table-wrap" style="max-height: 300px;"><table><thead><tr><th>Movement column</th><th>Why</th><th>Amount</th></tr></thead><tbody>${rowTutorial.movements.map((movement) => `
            <tr>
              <td>${escapeHtml(movement.column || "")}</td>
              <td>${escapeHtml(movement.explanation || "")}</td>
              <td>${escapeHtml(movement.amount || "")}</td>
            </tr>`).join("")}</tbody></table></div>`
        : `<p class="muted">No FY movement was identified for this row. Tessa is carrying the opening balance forward unless the accountant adds an adjustment.</p>`;
      const bridge = selected.profit_bridge || null;
      const bridgeRows = bridge?.rows?.length
        ? `<div class="table-wrap" style="max-height: 280px;"><table><thead><tr><th>P&L row</th><th>Effect</th><th>Amount</th></tr></thead><tbody>${bridge.rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.account_name || "")}</td>
              <td>${escapeHtml(row.effect || "")}</td>
              <td>${escapeHtml(row.amount || "")}</td>
            </tr>`).join("")}</tbody></table></div>`
        : "";
      detail.innerHTML = `
        <div>
          <span class="pill ${escapeAttr(selected.status || "")}">${escapeHtml(selected.status || "review")}</span>
          <h3>${escapeHtml(selected.account_name || "Movement note")}</h3>
          <p class="muted small">${escapeHtml(selected.note_id || "")}${selected.tb_row ? ` · Excel row ${escapeHtml(selected.tb_row)}` : ""}${selected.statement_group ? ` · ${escapeHtml(selected.statement_group)}` : ""}</p>
        </div>
        <div class="note-meta">
          <div class="note-metric"><span>Opening</span><strong>${escapeHtml(selected.opening_balance || "-")}</strong></div>
          <div class="note-metric"><span>Movement</span><strong>${escapeHtml(selected.tb_column || "-")}</strong></div>
          <div class="note-metric"><span>Main amount</span><strong>${escapeHtml(selected.main_amount || "-")}</strong></div>
          <div class="note-metric"><span>Closing</span><strong>${escapeHtml(selected.closing_balance || "-")}</strong></div>
        </div>
        <div class="story-block">
          <h4>What happened</h4>
          <p>${escapeHtml(selected.explanation || "No explanation available.")}</p>
        </div>
        ${rowTutorial ? `<div class="story-block">
          <h4>${escapeHtml(rowTutorial.title || "How to read this row")}</h4>
          <p>${escapeHtml(rowTutorial.tutorial || "")}</p>
          ${rowTutorial.formula ? `<p><strong>${escapeHtml(rowTutorial.formula)}</strong></p>` : ""}
          ${movementRows}
        </div>` : ""}
        <div class="story-block">
          <h4>Calculation</h4>
          <p>${escapeHtml(selected.calculation || "No calculation note available.")}</p>
        </div>
        ${bridge ? `<div class="story-block">
          <h4>${escapeHtml(bridge.title || "Book-profit bridge")}</h4>
          <p>${escapeHtml(bridge.summary || "")}</p>
          ${bridge.calculation ? `<p><strong>${escapeHtml(bridge.calculation)}</strong></p>` : ""}
          ${bridgeRows}
        </div>` : ""}
        <div class="story-block">
          <h4>What to check</h4>
          <p>${escapeHtml(selected.check_hint || "Review linked evidence if this row is selected.")}</p>
        </div>
        ${selected.evidence_summary ? `<div class="story-block"><h4>Evidence note</h4><p>${escapeHtml(selected.evidence_summary)}</p></div>` : ""}
        ${stories ? `<div class="story-block"><h4>Supporting context</h4><ul>${stories}</ul></div>` : ""}
        <div class="story-block">
          <h4>Open evidence</h4>
          <div class="evidence-links">${evidenceLinks || `<p class="muted">No direct source link mapped to this note yet.</p>`}</div>
        </div>
        ${selected.other_amounts ? `<div class="story-block"><h4>Searchable amounts</h4><p>${escapeHtml(selected.other_amounts)}</p></div>` : ""}
      `;
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
    }
    function escapeAttr(value) { return escapeHtml(value).replace(/`/g, "&#96;"); }
    function formatLabel(value) {
      return String(value || "").replace(/_/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
    }
    $("fileInput").addEventListener("change", (event) => uploadFiles(event.target.files, event.target).catch((error) => {
      $("uploadStatus").textContent = error.message;
      alert(error);
    }));
    $("startBtn").addEventListener("click", () => api("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prior_fs_file: $("priorFs").value,
        fy_start: $("fyStart").value,
        fy_end: $("fyEnd").value,
        allow_cache: $("allowCache").checked
      })
    }).then(refresh).catch(alert));
    $("refreshBtn").addEventListener("click", () => refresh().catch(alert));
    $("demoBtn").addEventListener("click", () => api("/api/demo", { method: "POST" }).then(refresh).catch(alert));
    $("resetBtn").addEventListener("click", () => {
      if (!confirm("Clear uploaded files and generated workpaper results?")) return;
      api("/api/reset", { method: "POST" }).then(refresh).catch(alert);
    });
    $("noteSearch").addEventListener("input", () => renderMovementNotes(state.latestData?.movement_notes || []));
    refresh().catch((error) => { $("outputMessage").textContent = error.message; });
  </script>
</body>
</html>
"""
