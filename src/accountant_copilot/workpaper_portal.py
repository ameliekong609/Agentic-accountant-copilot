"""Compatibility entrypoint for the local accountant-facing workpaper portal."""
from __future__ import annotations

from accountant_copilot.portal_companion import (
    _artifact_counts,
    _evidence_index_preview,
    _movement_notes_preview,
    _turing_summary,
)
from accountant_copilot.portal_config import (
    DOCUMENT_PROGRESS_PATH,
    EVENT_REGISTER_PATH,
    PORTAL_ROOT,
    PREPARE_PROGRESS_PATH,
    RELATIONSHIP_PROGRESS_PATH,
    SOURCE_INDEX_PATH,
    STATE_FILE,
    SUMMARY_PATH,
    TB_BRIDGE_JSON_PATH,
    TB_BRIDGE_PROGRESS_PATH,
    TURING_REVIEW_PATH,
    WORKBOOK_PATH,
    WorkpaperPortalConfig,
    _read_json,
    _write_json,
)
from accountant_copilot.portal_jobs import (
    _completed_workpaper_progress,
    _process_is_running,
)
from accountant_copilot import portal_jobs as _portal_jobs
from accountant_copilot.portal_progress import (
    _artifact_milestones,
    _public_progress,
    _public_status_cards,
)
from accountant_copilot.portal_server import WorkpaperPortalHandler, WorkpaperPortalServer, serve_workpaper_portal


def _reconcile_running_job_status(*args, **kwargs):
    original = _portal_jobs._process_is_running
    _portal_jobs._process_is_running = _process_is_running
    try:
        return _portal_jobs._reconcile_running_job_status(*args, **kwargs)
    finally:
        _portal_jobs._process_is_running = original
