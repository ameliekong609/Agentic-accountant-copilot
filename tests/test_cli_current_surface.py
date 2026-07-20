from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cli_help_only_exposes_current_financial_statement_workflow() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", "--help"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    help_text = result.stdout
    for command in [
        "process-documents",
        "match-source-facts",
        "build-tb-bridge-workpaper",
        "prepare-workpaper",
        "review-workpaper",
        "serve-workpaper-portal",
    ]:
        assert command in help_text
    for stale_command in [
        "inspect-engagement",
        "run-engagement",
        "setup-turing-workspace",
        "record-preference",
        "export-accountant-review-ui",
        "render-statement-package",
        "export-final-release-manifest",
    ]:
        assert stale_command not in help_text
