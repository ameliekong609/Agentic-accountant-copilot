"""Portal constants, config and JSON/time helpers."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
