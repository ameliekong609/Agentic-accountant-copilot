from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "accountant_copilot" / "review_app.py"


def run_cli(*args: str):
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=ROOT,
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )


def test_streamlit_review_app_starts_with_document_upload_and_control_tabs():
    assert APP.exists()
    source = APP.read_text()
    assert "st.file_uploader" in source
    assert "Upload source documents" in source
    assert "Accountant Review" in source
    assert "Release blockers" in source
    assert "apply-accountant-review-workbench" in source
    assert "accept_multiple_files=True" in source


def test_serve_accountant_review_ui_command_is_registered():
    result = run_cli("serve-accountant-review-ui", "--help")
    assert result.returncode == 0
    assert "Streamlit" in result.stdout
    assert "--state" in result.stdout
    assert "--artifact-dir" in result.stdout
    assert "--input-dir" in result.stdout
