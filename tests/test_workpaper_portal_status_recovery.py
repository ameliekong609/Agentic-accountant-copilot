import json
from pathlib import Path

from accountant_copilot import workpaper_portal as portal


def _write_progress(repo: Path, payload: dict) -> None:
    progress_path = repo / portal.PREPARE_PROGRESS_PATH
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(json.dumps(payload), encoding="utf-8")


def test_reconcile_running_job_uses_completed_workpaper_checkpoint(tmp_path: Path) -> None:
    workbook_path = tmp_path / portal.WORKBOOK_PATH
    workbook_path.parent.mkdir(parents=True, exist_ok=True)
    workbook_path.write_bytes(b"workbook")
    _write_progress(
        tmp_path,
        {
            "stage": "completed",
            "status": "completed",
            "message": "Workbook ready. Senior review passed.",
            "workbook_path": str(portal.WORKBOOK_PATH),
        },
    )
    job = {"job_id": "20260626T054755Z", "status": "running", "pid": 999999}

    updated, changed = portal._reconcile_running_job_status(tmp_path, job, None)

    assert changed is True
    assert updated["status"] == "completed"
    assert updated["exit_code"] == 0
    assert updated["message"] == "Workbook ready. Senior review passed."


def test_reconcile_running_job_does_not_convert_judgement_checkpoint_to_completed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workbook_path = tmp_path / portal.WORKBOOK_PATH
    workbook_path.parent.mkdir(parents=True, exist_ok=True)
    workbook_path.write_bytes(b"workbook")
    _write_progress(
        tmp_path,
        {
            "stage": "turing",
            "status": "needs_attention",
            "message": "Workbook was created, but senior review still has correction notes.",
            "workbook_path": str(portal.WORKBOOK_PATH),
        },
    )
    monkeypatch.setattr(portal, "_process_is_running", lambda pid: True)
    job = {"job_id": "needs-attention", "status": "running", "pid": 123}

    updated, changed = portal._reconcile_running_job_status(tmp_path, job, None)

    assert changed is False
    assert updated == job


def test_reconcile_running_job_marks_dead_process_failed_without_completed_checkpoint(tmp_path: Path) -> None:
    job = {"job_id": "dead-process", "status": "running", "pid": 999999}

    updated, changed = portal._reconcile_running_job_status(tmp_path, job, None)

    assert changed is True
    assert updated["status"] == "failed"
    assert "stopped before writing final status" in updated["message"]
