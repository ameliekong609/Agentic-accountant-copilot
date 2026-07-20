"""Portal job, demo snapshot and backend process helpers."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from accountant_copilot.portal_config import (
    DEMO_DURATION_SECONDS,
    DEMO_REVIEW_READY_SECONDS,
    DEMO_SNAPSHOT_PATH,
    DEMO_SOURCE_READY_SECONDS,
    DEMO_WORKBOOK_READY_SECONDS,
    PORTAL_ROOT,
    PREPARE_PROGRESS_PATH,
    STATE_FILE,
    WORKBOOK_PATH,
    _iso_now,
    _read_json,
)

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
