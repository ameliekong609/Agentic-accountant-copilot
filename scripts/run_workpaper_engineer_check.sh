#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: scripts/run_workpaper_engineer_check.sh <JOB_ID_OR_JOB_DIR> <failed|stale|manual>"
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
REASON="$2"

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
LOCK_DIR="$JOB_DIR/engineer.lock"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "Engineer check already running for $JOB_NAME"
  exit 0
fi
trap 'rm -rf "$LOCK_DIR"' EXIT

max_interventions="${ACCOUNTANT_COPILOT_ENGINEER_MAX_INTERVENTIONS:-2}"
case "$max_interventions" in
  ''|*[!0-9]*) max_interventions=2 ;;
esac
existing_count="$(find "$JOB_DIR" -maxdepth 1 -name 'engineer-diagnosis-*.md' -type f | wc -l | tr -d ' ')"
if [ "$existing_count" -ge "$max_interventions" ]; then
  echo "Engineer intervention limit reached for $JOB_NAME ($existing_count/$max_interventions)"
  exit 0
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
context_path="$JOB_DIR/engineer-context-$timestamp.txt"
diagnosis_path="$JOB_DIR/engineer-diagnosis-$timestamp.md"
command="${ACCOUNTANT_COPILOT_ENGINEER_CODEX_COMMAND:-codex exec}"
timeout_seconds="${ACCOUNTANT_COPILOT_ENGINEER_TIMEOUT_SECONDS:-1800}"
case "$timeout_seconds" in
  ''|*[!0-9]*) timeout_seconds=1800 ;;
esac

effective_autofix="${ACCOUNTANT_COPILOT_ENGINEER_AUTOFIX:-0}"
if [ "$REASON" = "stale" ] && [ "${ACCOUNTANT_COPILOT_ENGINEER_AUTOFIX_STALE:-0}" != "1" ]; then
  effective_autofix=0
fi

{
  echo "# Accountant workpaper engineering context"
  echo
  echo "Repo: $REPO_ROOT"
  echo "Job: $JOB_NAME"
  echo "Reason: $REASON"
  echo "Autofix enabled: $effective_autofix"
  echo
  echo "## Job status"
  "$REPO_ROOT/scripts/check_prepare_workpaper_job.sh" "$JOB_REF" || true
  echo
  echo "## Progress checkpoints"
  for path in \
    "$REPO_ROOT/outputs/step4_tb_bridge_workpaper/prepare_workpaper_progress.json" \
    "$REPO_ROOT/outputs/raw_inputs_pdf_extraction/document_processing_progress.json" \
    "$REPO_ROOT/outputs/raw_inputs_pdf_extraction/relationship_reasoning_progress.json" \
    "$REPO_ROOT/outputs/step4_tb_bridge_workpaper/tb_bridge_generation_progress.json" \
    "$REPO_ROOT/outputs/step4_tb_bridge_workpaper/last_good_workpaper_restored.json"
  do
    echo
    echo "### $path"
    if [ -f "$path" ]; then
      tail -200 "$path"
    else
      echo "missing"
    fi
  done
  echo
  echo "## Latest job log tail"
  if [ -f "$JOB_DIR/prepare-workpaper.log" ]; then
    tail -180 "$JOB_DIR/prepare-workpaper.log"
  else
    echo "missing"
  fi
  echo
  echo "## Git status"
  git -C "$REPO_ROOT" status --short || true
} >"$context_path"

prompt_path="$JOB_DIR/engineer-prompt-$timestamp.md"
cat >"$prompt_path" <<EOF
You are the product engineering support agent for the Agentic Accountant Copilot.

You are checking a workpaper job because: $REASON.

Read this context file first:
$context_path

Product intent:
- The accountant-facing product should not expose raw technical failure when it can preserve or explain a usable workbook.
- Live progress should be visible through checkpoint JSON files.
- A failed new run must not delete the last good workbook.
- The final user-facing output should be a useful Excel workbook plus concise summary, not logs.

Guardrails:
- Do not change client source documents or uploaded files.
- Do not delete outputs unless the fix explicitly requires regenerating a temporary broken artifact.
- Do not hide genuine accounting judgement issues as success.
- If autofix is disabled, do not edit files. Diagnose only.
- If autofix is enabled, you may edit product code, scripts, docs, and tests in this repo.
- If you edit code, run focused compile/tests before you finish.
- Keep the fix bounded. Do not redesign the whole product from this watcher.
- Do not run an expensive full client workpaper job unless the context strongly shows it is necessary.

Autofix enabled: $effective_autofix

Write your final answer as Markdown with:
1. Status: fixed, diagnosed, or blocked.
2. User impact in plain accountant-facing language.
3. Engineering root cause.
4. Files changed, if any.
5. Verification run, if any.
6. Whether the job should be rerun.
EOF

{
  echo "# Workpaper Engineer Check"
  echo
  echo "- Job: $JOB_NAME"
  echo "- Reason: $REASON"
  echo "- Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "- Autofix enabled: $effective_autofix"
  echo
  echo "## Codex Engineer Output"
  echo
} >"$diagnosis_path"

set +e
ENGINEER_COMMAND="$command" ENGINEER_TIMEOUT="$timeout_seconds" PROMPT_PATH="$prompt_path" DIAGNOSIS_PATH="$diagnosis_path" python3 - <<'PY'
import os
import shlex
import subprocess
import sys
from pathlib import Path

command = shlex.split(os.environ["ENGINEER_COMMAND"])
timeout = int(os.environ["ENGINEER_TIMEOUT"])
prompt_path = Path(os.environ["PROMPT_PATH"])
diagnosis_path = Path(os.environ["DIAGNOSIS_PATH"])

with prompt_path.open("r", encoding="utf-8") as prompt_handle, diagnosis_path.open("a", encoding="utf-8") as diagnosis_handle:
    try:
        result = subprocess.run(
            command,
            stdin=prompt_handle,
            stdout=diagnosis_handle,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        sys.exit(result.returncode)
    except subprocess.TimeoutExpired:
        diagnosis_handle.write(f"\n\nEngineer Codex command timed out after {timeout} seconds.\n")
        sys.exit(124)
    except FileNotFoundError:
        diagnosis_handle.write(f"\n\nEngineer Codex command was not found: {' '.join(command)}\n")
        sys.exit(127)
PY
exit_code=$?
set -e

{
  echo
  echo "## Engineer Runner"
  echo
  echo "- Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "- Exit code: $exit_code"
  echo "- Context: $context_path"
  echo "- Prompt: $prompt_path"
} >>"$diagnosis_path"

"$REPO_ROOT/scripts/send_workpaper_telegram_message.py" "Engineering watcher checked workpaper job $JOB_NAME ($REASON). Diagnosis: $diagnosis_path" || true

exit 0
