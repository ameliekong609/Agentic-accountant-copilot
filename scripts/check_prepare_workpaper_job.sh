#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOB_REF="${1:-latest}"

if [ "$JOB_REF" = "latest" ]; then
  JOB_DIR="$REPO_ROOT/outputs/workpaper_jobs/latest"
elif [ -d "$JOB_REF" ]; then
  JOB_DIR="$JOB_REF"
else
  JOB_DIR="$REPO_ROOT/outputs/workpaper_jobs/$JOB_REF"
fi

if [ ! -e "$JOB_DIR" ]; then
  echo "Workpaper job not found: $JOB_REF"
  exit 2
fi

JOB_DIR="$(cd "$JOB_DIR" && pwd)"
STATUS_PATH="$JOB_DIR/status.json"
PID_PATH="$JOB_DIR/pid"
LOG_PATH="$JOB_DIR/prepare-workpaper.log"

if [ ! -f "$STATUS_PATH" ]; then
  echo "Status file missing: $STATUS_PATH"
  exit 2
fi

runtime_status="$(python3 - "$STATUS_PATH" "$PID_PATH" "$REPO_ROOT" <<'PY'
import json
import os
import sys
from pathlib import Path

status_path, pid_path, repo_root = sys.argv[1:4]
repo = Path(repo_root)
payload = json.load(open(status_path, encoding="utf-8"))
status = payload.get("status")
pid = None
if os.path.exists(pid_path):
    try:
        pid = int(open(pid_path, encoding="utf-8").read().strip())
    except ValueError:
        pid = None

def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def completed_workpaper_progress():
    progress = load(repo / "outputs" / "step4_tb_bridge_workpaper" / "prepare_workpaper_progress.json")
    if not isinstance(progress, dict):
        return False
    if progress.get("stage") != "completed" or progress.get("status") != "completed":
        return False
    workbook_path = Path(str(progress.get("workbook_path") or "outputs/step4_tb_bridge_workpaper/step4_tb_bridge_workpaper.xlsx"))
    if not workbook_path.is_absolute():
        workbook_path = repo / workbook_path
    return workbook_path.exists()

if status == "running" and completed_workpaper_progress():
    status = "completed"
elif status == "running" and pid:
    try:
        os.kill(pid, 0)
    except OSError:
        status = "stopped_without_status"
print(status or "unknown")
PY
)"

echo "Job: $(basename "$JOB_DIR")"
echo "Status: $runtime_status"
echo "Status file: $STATUS_PATH"
echo "Log file: $LOG_PATH"
current_stage="$(python3 - "$REPO_ROOT" "$runtime_status" <<'PY'
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
runtime_status = sys.argv[2]
raw_dir = repo / "outputs" / "raw_inputs_pdf_extraction"
bridge_dir = repo / "outputs" / "step4_tb_bridge_workpaper"

def load(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

progress = load(raw_dir / "document_processing_progress.json")
prepare_progress = load(bridge_dir / "prepare_workpaper_progress.json")
relationship_progress = load(raw_dir / "relationship_reasoning_progress.json")
bridge_progress = load(bridge_dir / "tb_bridge_generation_progress.json")
source_index = raw_dir / "source_document_index.json"
event_register = raw_dir / "accounting_event_register.json"
tb_json = bridge_dir / "tb_bridge_workpaper.json"
workbook = bridge_dir / "step4_tb_bridge_workpaper.xlsx"
review_json = bridge_dir / "turing_senior_review.json"
summary = bridge_dir / "prepared_workpaper_summary.md"

def prepare_message():
    stage = prepare_progress.get("stage") or "running"
    message = prepare_progress.get("message") or ""
    label = {
        "indexing": "Step 1 read files",
        "relationships": "Step 2 understand movements",
        "bridge": "Step 3 prepare TB bridge",
        "turing": "Step 4 senior review",
        "correction": "Step 4 correction loop",
        "completed": "completed",
    }.get(stage, stage)
    return f"{label}: {message}".strip(": ")

if runtime_status == "completed":
    review = load(review_json)
    review_status = review.get("status") or "not reviewed"
    if prepare_progress:
        print(f"{prepare_message()}; final senior review status {review_status}")
    else:
        print(f"completed; final senior review status {review_status}")
elif runtime_status == "failed":
    if prepare_progress:
        print(f"failed; {prepare_message()}")
    elif summary.exists():
        print("failed; see prepared_workpaper_summary.md")
    elif tb_json.exists() and not workbook.exists():
        print("failed during Step 3 workbook validation/render")
    elif event_register.exists() and not tb_json.exists():
        print("failed during Step 3 TB bridge generation")
    elif source_index.exists() and not event_register.exists():
        print("failed during Step 2 movement reasoning")
    else:
        print("failed during Step 1 read files")
elif prepare_progress and prepare_progress.get("stage") == "relationships" and relationship_progress:
    print(relationship_progress.get("message") or prepare_message())
elif prepare_progress and prepare_progress.get("stage") in {"bridge", "turing", "correction"} and bridge_progress:
    print(bridge_progress.get("message") or prepare_message())
elif progress and progress.get("status") != "complete":
    print(
        "Step 1 read files "
        f"{progress.get('processed_items', 0)}/{progress.get('total_items', 0)} "
        f"(batch {progress.get('current_batch', 0)}/{progress.get('total_batches', 0)}, "
        f"cache hits {progress.get('cache_hits', 0)})"
    )
elif prepare_progress:
    print(prepare_message())
elif not source_index.exists():
    print("Step 1 read files is starting")
elif not event_register.exists():
    print("Step 2 understand movements is running")
elif not tb_json.exists():
    event = load(event_register)
    relationships = len(event.get("relationships", []) if isinstance(event.get("relationships"), list) else [])
    print(f"Step 3 prepare TB bridge is running (relationships: {relationships})")
elif not workbook.exists():
    tb = load(tb_json)
    tb_status = tb.get("status") or "draft"
    findings = len(tb.get("validation_findings", []) if isinstance(tb.get("validation_findings"), list) else [])
    print(f"Step 3 workbook render/validation is running (TB JSON status {tb_status}, validation findings {findings})")
elif not review_json.exists():
    print("Step 4 senior review is running")
else:
    review = load(review_json)
    review_status = review.get("status") or "review created"
    if review_status == "needs_corrections":
        print("Step 4 correction loop is running")
    else:
        print(f"finalising after senior review ({review_status})")
PY
)"
echo "Current stage: $current_stage"

echo
echo "Artifacts:"
for path in \
  "$REPO_ROOT/outputs/raw_inputs_pdf_extraction/source_document_index.json" \
  "$REPO_ROOT/outputs/raw_inputs_pdf_extraction/accounting_event_register.json" \
  "$REPO_ROOT/outputs/step4_tb_bridge_workpaper/tb_bridge_workpaper.json" \
  "$REPO_ROOT/outputs/step4_tb_bridge_workpaper/step4_tb_bridge_workpaper.xlsx" \
  "$REPO_ROOT/outputs/step4_tb_bridge_workpaper/turing_senior_review.json" \
  "$REPO_ROOT/outputs/step4_tb_bridge_workpaper/prepared_workpaper_summary.md"
do
  if [ -f "$path" ]; then
    stat -f "  %Sm  %N" -t "%Y-%m-%d %H:%M:%S" "$path"
  else
    echo "  missing  $path"
  fi
done

if [ -f "$LOG_PATH" ]; then
  echo
  echo "Last log lines:"
  tail -40 "$LOG_PATH"
fi
