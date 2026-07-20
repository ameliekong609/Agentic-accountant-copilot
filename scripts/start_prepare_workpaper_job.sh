#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "Usage: scripts/start_prepare_workpaper_job.sh <CLIENT_FOLDER> [prepare-workpaper options...]"
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$REPO_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO_ROOT/.env"
  set +a
fi
CLIENT_FOLDER="$1"
shift

JOB_ROOT="$REPO_ROOT/outputs/workpaper_jobs"
JOB_ID="$(date -u +%Y%m%dT%H%M%SZ)"
JOB_DIR="$JOB_ROOT/$JOB_ID"
STATUS_PATH="$JOB_DIR/status.json"
LOG_PATH="$JOB_DIR/prepare-workpaper.log"

mkdir -p "$JOB_DIR"
ln -sfn "$JOB_DIR" "$JOB_ROOT/latest"

JOB_ID="$JOB_ID" STATUS_PATH="$STATUS_PATH" LOG_PATH="$LOG_PATH" CLIENT_FOLDER="$CLIENT_FOLDER" python3 - <<'PY'
import json
import os
from datetime import datetime, timezone

payload = {
    "job_id": os.environ["JOB_ID"],
    "status": "starting",
    "client_folder": os.environ["CLIENT_FOLDER"],
    "log_path": os.environ["LOG_PATH"],
    "message": "prepare-workpaper worker is starting",
    "updated_at": datetime.now(timezone.utc).isoformat(),
}
with open(os.environ["STATUS_PATH"], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

pid="$(
  REPO_ROOT="$REPO_ROOT" JOB_ID="$JOB_ID" STATUS_PATH="$STATUS_PATH" LOG_PATH="$LOG_PATH" CLIENT_FOLDER="$CLIENT_FOLDER" JOB_DIR="$JOB_DIR" python3 - "$@" <<'PY'
import os
import subprocess
import sys

repo_root = os.environ["REPO_ROOT"]
job_id = os.environ["JOB_ID"]
status_path = os.environ["STATUS_PATH"]
log_path = os.environ["LOG_PATH"]
client_folder = os.environ["CLIENT_FOLDER"]
job_dir = os.environ["JOB_DIR"]
worker = os.path.join(repo_root, "scripts", "run_prepare_workpaper_job_worker.sh")
launch_log_path = os.path.join(job_dir, "worker-launch.log")
command = [worker, job_id, status_path, log_path, client_folder, *sys.argv[1:]]
launch_log = open(launch_log_path, "ab", buffering=0)
process = subprocess.Popen(
    command,
    cwd=repo_root,
    stdout=launch_log,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)
print(process.pid)
PY
)"
echo "$pid" > "$JOB_DIR/pid"

watcher_pid=""
if [ "${ACCOUNTANT_COPILOT_TELEGRAM_NOTIFY:-1}" != "0" ]; then
  telegram_chat_id="${ACCOUNTANT_COPILOT_TELEGRAM_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}"
  if [ -n "$telegram_chat_id" ]; then
    watcher_pid="$(
      REPO_ROOT="$REPO_ROOT" JOB_ID="$JOB_ID" JOB_DIR="$JOB_DIR" python3 - <<'PY'
import os
import subprocess

repo_root = os.environ["REPO_ROOT"]
job_id = os.environ["JOB_ID"]
job_dir = os.environ["JOB_DIR"]
watcher = os.path.join(repo_root, "scripts", "watch_prepare_workpaper_job_telegram.sh")
watcher_log_path = os.path.join(job_dir, "telegram-watcher.log")
watcher_log = open(watcher_log_path, "ab", buffering=0)
process = subprocess.Popen(
    [watcher, job_id],
    cwd=repo_root,
    stdout=watcher_log,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)
print(process.pid)
PY
    )"
    echo "$watcher_pid" > "$JOB_DIR/telegram-watcher.pid"
  fi
fi

engineer_watcher_pid=""
if [ "${ACCOUNTANT_COPILOT_ENGINEER_WATCHER:-1}" != "0" ]; then
  engineer_watcher_pid="$(
    REPO_ROOT="$REPO_ROOT" JOB_ID="$JOB_ID" JOB_DIR="$JOB_DIR" python3 - <<'PY'
import os
import subprocess

repo_root = os.environ["REPO_ROOT"]
job_id = os.environ["JOB_ID"]
job_dir = os.environ["JOB_DIR"]
watcher = os.path.join(repo_root, "scripts", "watch_workpaper_engineer.sh")
watcher_log_path = os.path.join(job_dir, "engineer-watcher.log")
watcher_log = open(watcher_log_path, "ab", buffering=0)
process = subprocess.Popen(
    [watcher, job_id],
    cwd=repo_root,
    stdout=watcher_log,
    stderr=subprocess.STDOUT,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)
print(process.pid)
PY
  )"
  echo "$engineer_watcher_pid" > "$JOB_DIR/engineer-watcher.pid"
fi

JOB_ID="$JOB_ID" PID="$pid" WATCHER_PID="$watcher_pid" ENGINEER_WATCHER_PID="$engineer_watcher_pid" JOB_DIR="$JOB_DIR" STATUS_PATH="$STATUS_PATH" LOG_PATH="$LOG_PATH" python3 - <<'PY'
import json
import os

payload = {
    "job_id": os.environ["JOB_ID"],
    "pid": int(os.environ["PID"]),
    "job_dir": os.environ["JOB_DIR"],
    "status_path": os.environ["STATUS_PATH"],
    "log_path": os.environ["LOG_PATH"],
}
if os.environ.get("WATCHER_PID"):
    payload["telegram_watcher_pid"] = int(os.environ["WATCHER_PID"])
if os.environ.get("ENGINEER_WATCHER_PID"):
    payload["engineer_watcher_pid"] = int(os.environ["ENGINEER_WATCHER_PID"])
print(json.dumps(payload, indent=2, sort_keys=True))
PY
