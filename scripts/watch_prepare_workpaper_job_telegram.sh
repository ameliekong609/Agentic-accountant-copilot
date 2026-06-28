#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: scripts/watch_prepare_workpaper_job_telegram.sh <JOB_ID> [interval_seconds]"
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
fi
JOB_ID="$1"
INTERVAL="${2:-${ACCOUNTANT_COPILOT_TELEGRAM_STATUS_INTERVAL_SECONDS:-300}}"
case "$INTERVAL" in
  ''|*[!0-9]*) INTERVAL=300 ;;
esac
if [ "$INTERVAL" -lt 30 ]; then
  INTERVAL=30
fi

send_message() {
  "$REPO_ROOT/scripts/send_workpaper_telegram_message.py" "$1" || true
}

while true; do
  output="$("$REPO_ROOT/scripts/check_prepare_workpaper_job.sh" "$JOB_ID" 2>&1 || true)"
  status="$(printf '%s\n' "$output" | awk -F': ' '/^Status:/ {print $2; exit}')"
  stage="$(printf '%s\n' "$output" | awk -F': ' '/^Current stage:/ {print $2; exit}')"
  summary_path="$REPO_ROOT/outputs/step4_tb_bridge_workpaper/prepared_workpaper_summary.md"
  workbook_path="$REPO_ROOT/outputs/step4_tb_bridge_workpaper/step4_tb_bridge_workpaper.xlsx"

  if [ "$status" = "completed" ]; then
    send_message "Workpaper job $JOB_ID completed. Current stage: ${stage:-completed}. Workbook: $workbook_path"
    exit 0
  fi
  if [ "$status" = "failed" ] || [ "$status" = "stopped_without_status" ]; then
    send_message "Workpaper job $JOB_ID failed. Current stage: ${stage:-failed}. Summary: $summary_path"
    exit 0
  fi

  send_message "Workpaper job $JOB_ID is running. Current stage: ${stage:-unknown}."
  sleep "$INTERVAL"
done
