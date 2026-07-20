"""Public progress-stage wording and status-card helpers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from accountant_copilot.portal_config import SOURCE_INDEX_PATH, TURING_REVIEW_PATH, WORKBOOK_PATH

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
