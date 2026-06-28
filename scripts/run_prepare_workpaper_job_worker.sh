#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "Usage: scripts/run_prepare_workpaper_job_worker.sh <JOB_ID> <STATUS_PATH> <LOG_PATH> <CLIENT_FOLDER> [prepare-workpaper options...]"
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
JOB_ID="$1"
STATUS_PATH="$2"
LOG_PATH="$3"
CLIENT_FOLDER="$4"
shift 4

write_status() {
  local status="$1"
  local exit_code="${2:-}"
  local message="${3:-}"
  JOB_ID="$JOB_ID" STATUS="$status" EXIT_CODE="$exit_code" MESSAGE="$message" CLIENT_FOLDER="$CLIENT_FOLDER" STATUS_PATH="$STATUS_PATH" LOG_PATH="$LOG_PATH" python3 - <<'PY'
import json
import os
from datetime import datetime, timezone

payload = {
    "job_id": os.environ["JOB_ID"],
    "status": os.environ["STATUS"],
    "client_folder": os.environ["CLIENT_FOLDER"],
    "log_path": os.environ["LOG_PATH"],
    "updated_at": datetime.now(timezone.utc).isoformat(),
}
if os.environ.get("EXIT_CODE"):
    payload["exit_code"] = int(os.environ["EXIT_CODE"])
if os.environ.get("MESSAGE"):
    payload["message"] = os.environ["MESSAGE"]
with open(os.environ["STATUS_PATH"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
}

write_status "running" "" "prepare-workpaper is running in the background"

cd "$REPO_ROOT"
max_attempts="${ACCOUNTANT_COPILOT_WORKFLOW_MAX_ATTEMPTS:-2}"
case "$max_attempts" in
  ''|*[!0-9]*) max_attempts=2 ;;
esac
if [ "$max_attempts" -lt 1 ]; then
  max_attempts=1
fi

: >"$LOG_PATH"
attempt=1
exit_code=1
while [ "$attempt" -le "$max_attempts" ]; do
  attempt_log="${LOG_PATH%.log}.attempt_${attempt}.log"
  write_status "running" "" "prepare-workpaper attempt $attempt/$max_attempts is running"
  {
    echo "===== prepare-workpaper attempt $attempt/$max_attempts started $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
  } >>"$LOG_PATH"
  set +e
  PYTHONUNBUFFERED=1 PYTHONPATH=src .venv/bin/python -u -m accountant_copilot.cli prepare-workpaper \
    --client-folder "$CLIENT_FOLDER" \
    "$@" \
    >"$attempt_log" 2>&1
  exit_code=$?
  set -e
  cat "$attempt_log" >>"$LOG_PATH"
  {
    echo "===== prepare-workpaper attempt $attempt/$max_attempts exited $exit_code $(date -u +%Y-%m-%dT%H:%M:%SZ) ====="
    echo
  } >>"$LOG_PATH"
  if [ "$exit_code" -eq 0 ]; then
    break
  fi
  if [ "$attempt" -lt "$max_attempts" ]; then
    write_status "running" "$exit_code" "prepare-workpaper attempt $attempt/$max_attempts failed; rerunning workflow"
  fi
  attempt=$((attempt + 1))
done

if [ "$exit_code" -eq 0 ]; then
  write_status "completed" "$exit_code" "prepare-workpaper completed on attempt $attempt/$max_attempts"
else
  write_status "failed" "$exit_code" "prepare-workpaper failed after $max_attempts attempt(s); see log"
  "$REPO_ROOT/scripts/run_workpaper_engineer_check.sh" "$JOB_ID" "failed" >/dev/null 2>&1 || true
fi

exit "$exit_code"
