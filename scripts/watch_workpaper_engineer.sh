#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: scripts/watch_workpaper_engineer.sh <JOB_ID_OR_JOB_DIR> [interval_seconds]"
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
fi
JOB_REF="$1"
INTERVAL="${2:-${ACCOUNTANT_COPILOT_ENGINEER_INTERVAL_SECONDS:-300}}"
STALE_SECONDS="${ACCOUNTANT_COPILOT_ENGINEER_STALE_SECONDS:-900}"

case "$INTERVAL" in
  ''|*[!0-9]*) INTERVAL=300 ;;
esac
if [ "$INTERVAL" -lt 60 ]; then
  INTERVAL=60
fi
case "$STALE_SECONDS" in
  ''|*[!0-9]*) STALE_SECONDS=900 ;;
esac
if [ "$STALE_SECONDS" -lt 300 ]; then
  STALE_SECONDS=300
fi

if [ "$JOB_REF" = "latest" ]; then
  JOB_DIR="$REPO_ROOT/outputs/workpaper_jobs/latest"
elif [ -d "$JOB_REF" ]; then
  JOB_DIR="$JOB_REF"
else
  JOB_DIR="$REPO_ROOT/outputs/workpaper_jobs/$JOB_REF"
fi
if [ ! -d "$JOB_DIR" ]; then
  echo "Workpaper job not found: $JOB_REF"
  exit 2
fi
JOB_DIR="$(cd "$JOB_DIR" && pwd)"
JOB_NAME="$(basename "$JOB_DIR")"
LAST_STALE_MARK="$JOB_DIR/engineer-last-stale-check"

send_message() {
  "$REPO_ROOT/scripts/send_workpaper_telegram_message.py" "$1" || true
}

latest_progress_age() {
  REPO_ROOT="$REPO_ROOT" python3 - <<'PY'
import os
import time
from pathlib import Path

repo = Path(os.environ["REPO_ROOT"])
paths = [
    repo / "outputs" / "step4_tb_bridge_workpaper" / "prepare_workpaper_progress.json",
    repo / "outputs" / "raw_inputs_pdf_extraction" / "document_processing_progress.json",
    repo / "outputs" / "raw_inputs_pdf_extraction" / "relationship_reasoning_progress.json",
    repo / "outputs" / "step4_tb_bridge_workpaper" / "tb_bridge_generation_progress.json",
]
existing = [path.stat().st_mtime for path in paths if path.exists()]
if not existing:
    print(10**9)
else:
    print(int(time.time() - max(existing)))
PY
}

while true; do
  output="$("$REPO_ROOT/scripts/check_prepare_workpaper_job.sh" "$JOB_REF" 2>&1 || true)"
  status="$(printf '%s\n' "$output" | awk -F': ' '/^Status:/ {print $2; exit}')"
  stage="$(printf '%s\n' "$output" | awk -F': ' '/^Current stage:/ {print $2; exit}')"

  if [ "$status" = "completed" ]; then
    exit 0
  fi

  if [ "$status" = "failed" ] || [ "$status" = "stopped_without_status" ]; then
    send_message "Engineering watcher is checking failed workpaper job $JOB_NAME. Stage: ${stage:-unknown}."
    "$REPO_ROOT/scripts/run_workpaper_engineer_check.sh" "$JOB_REF" "failed" || true
    exit 0
  fi

  if [ "$status" = "running" ]; then
    age="$(latest_progress_age)"
    if [ "$age" -ge "$STALE_SECONDS" ]; then
      marker_value="${stage:-unknown}:$age"
      previous=""
      if [ -f "$LAST_STALE_MARK" ]; then
        previous="$(cat "$LAST_STALE_MARK" 2>/dev/null || true)"
      fi
      if [ "$previous" != "$marker_value" ]; then
        echo "$marker_value" >"$LAST_STALE_MARK"
        send_message "Engineering watcher is checking a possibly stuck workpaper job $JOB_NAME. No checkpoint update for about $((age / 60)) minute(s). Stage: ${stage:-unknown}."
        "$REPO_ROOT/scripts/run_workpaper_engineer_check.sh" "$JOB_REF" "stale" || true
      fi
    fi
  fi

  sleep "$INTERVAL"
done
