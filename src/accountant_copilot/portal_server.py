"""HTTP server and request handler for the local workpaper portal."""
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from accountant_copilot.portal_assets import _INDEX_HTML
from accountant_copilot.portal_companion import (
    _artifact_counts,
    _evidence_index_preview,
    _movement_notes_preview,
    _read_summary_text,
    _turing_summary,
)
from accountant_copilot.portal_config import (
    DOCUMENT_PROGRESS_PATH,
    PORTAL_ROOT,
    PREPARE_PROGRESS_PATH,
    RELATIONSHIP_PROGRESS_PATH,
    SUMMARY_PATH,
    TB_BRIDGE_PROGRESS_PATH,
    WORKBOOK_PATH,
    WorkpaperPortalConfig,
    _elapsed_seconds_since,
    _iso_now,
    _read_json,
    _write_json,
    _write_portal_job_status,
)
from accountant_copilot.portal_files import _prior_fs_candidates, _safe_extract_zip, _safe_relative_path, _scan_files
from accountant_copilot.portal_jobs import (
    _clear_generated_workpaper_state,
    _demo_snapshot_available,
    _demo_stage_from_elapsed,
    _demo_visibility,
    _portal_job_dir,
    _process_is_running,
    _reconcile_running_job_status,
    _restore_demo_snapshot,
    _start_engineer_check,
    _start_engineer_watcher,
    _workbook_download_ready,
)
from accountant_copilot.portal_progress import (
    _artifact_milestones,
    _format_elapsed,
    _progress_stage,
    _public_progress,
    _public_progress_message,
    _public_running_detail,
    _public_stage,
    _public_status_cards,
)

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
