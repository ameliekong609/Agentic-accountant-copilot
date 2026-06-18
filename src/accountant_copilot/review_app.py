"""Streamlit accountant review UI for the Agentic Accountant Copilot.

This app is intentionally a review front-end. It can stage uploaded source
files and download/apply review workbench decisions, but the same deterministic
CLI controls still validate and persist approvals.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # pragma: no cover - import availability is exercised by launching Streamlit.
    import streamlit as st
except ModuleNotFoundError:  # pragma: no cover
    st = None  # type: ignore[assignment]


DEFAULT_STATE = Path("outputs/raw_inputs_pdf_extraction/engagement_state.json")
DEFAULT_ARTIFACT_DIR = Path("outputs/raw_inputs_pdf_extraction")
DEFAULT_INPUT_DIR = Path("inputs")


def _app_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    parser.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    args, _unknown = parser.parse_known_args()
    return args


def _query_param(name: str, default: str) -> str:
    try:
        value = st.query_params.get(name)  # type: ignore[union-attr]
        return str(value or default)
    except Exception:
        return default


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _save_uploads(uploaded_files: list[Any], input_dir: Path) -> list[Path]:
    input_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for uploaded in uploaded_files:
        target = input_dir / uploaded.name
        target.write_bytes(uploaded.getbuffer())
        saved.append(target)
    return saved


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    return subprocess.run([sys.executable, "-m", "accountant_copilot.cli", *args], cwd=cwd, env=env, text=True, capture_output=True, check=False)


def _render_blockers(blockers: list[dict[str, Any]]) -> None:
    st.subheader("Release blockers")
    if not blockers:
        st.success("No release blockers detected by the current blocker report.")
        return
    for blocker in blockers:
        st.error(f"{blocker.get('category', 'blocker')}: {blocker.get('message', '')}")
        st.caption(f"Artifact: {blocker.get('artifact', '')}")
        st.write(f"Required action: {blocker.get('required_action', '')}")


def _decision_fields(prefix: str, include_offset: bool = False) -> dict[str, str]:
    action = st.selectbox("Action", ["", "approve", "reject"], key=f"{prefix}_action")
    offset = ""
    if include_offset:
        offset = st.text_input("Offset account ID", key=f"{prefix}_offset")
    approved_by = st.text_input("Reviewer", key=f"{prefix}_reviewer")
    rationale = st.text_area("Rationale", key=f"{prefix}_rationale")
    payload = {"action": action, "approved_by": approved_by, "rationale": rationale}
    if include_offset:
        payload["offset_account_id"] = offset
    return payload


def _render_workbench(workbench: dict[str, Any]) -> dict[str, Any]:
    edited = json.loads(json.dumps(workbench))
    sections = edited.setdefault("sections", {})

    st.subheader("CoA Review")
    coa_accounts = sections.get("coa_accounts", [])
    if not coa_accounts:
        st.info("No CoA accounts pending review.")
    for idx, account in enumerate(coa_accounts):
        with st.expander(f"{account.get('code')} — {account.get('name')} ({account.get('status')})", expanded=idx < 3):
            st.write({k: account.get(k) for k in ["account_id", "type", "presentation_group", "opening_balance"]})
            account.update(_decision_fields(f"coa_{idx}"))

    st.subheader("Journal Review")
    journals = sections.get("journal_decisions", [])
    if not journals:
        st.info("No journals pending review.")
    for idx, journal in enumerate(journals):
        with st.expander(f"{journal.get('adjustment_id')} — {journal.get('amount')}"):
            st.write({k: journal.get(k) for k in ["description", "debit_account", "credit_account", "amount", "status"]})
            journal.update(_decision_fields(f"journal_{idx}", include_offset=True))

    st.subheader("Draft Statement Review")
    draft = sections.setdefault("draft_statement_review", {})
    st.write(f"Status: `{draft.get('draft_status', 'missing')}`")
    st.write(f"Findings: `{draft.get('draft_findings', 0)}`")
    draft.setdefault("decision", {}).update(_decision_fields("draft_review"))

    st.subheader("Final Sign-off")
    st.warning("Only sign off after release candidate verification is clean.")
    sections.setdefault("final_signoff", {}).update(_decision_fields("final_signoff"))
    return edited


def main() -> None:
    if st is None:
        raise RuntimeError("Streamlit is not installed. Install with `python3.11 -m pip install -e .[ui]`.")

    st.set_page_config(page_title="Accountant Review Workbench", layout="wide")
    st.title("Accountant Review Workbench")
    st.caption("Start by uploading source documents, then review blockers and accountant decisions. Approvals still go through deterministic CLI controls.")

    app_args = _app_args()
    repo_root = Path.cwd()
    state_path = Path(_query_param("state", app_args.state))
    artifact_dir = Path(_query_param("artifact_dir", app_args.artifact_dir))
    input_dir = Path(_query_param("input_dir", app_args.input_dir))

    with st.sidebar:
        st.header("Engagement paths")
        state_path = Path(st.text_input("State path", str(state_path)))
        artifact_dir = Path(st.text_input("Artifact directory", str(artifact_dir)))
        input_dir = Path(st.text_input("Upload/input directory", str(input_dir)))
        st.caption("Use `ingest-raw-inputs` after staging new uploads.")

    upload_tab, review_tab, artifacts_tab, apply_tab = st.tabs(["1 Upload source documents", "2 Accountant Review", "3 Artifacts", "4 Apply decisions"])

    with upload_tab:
        st.header("Upload source documents")
        st.write("Upload PDFs, images, CSVs, spreadsheets, or other source files into the engagement input folder. This does not approve accounting treatment.")
        uploaded_files = st.file_uploader("Upload source documents", accept_multiple_files=True)
        if st.button("Save uploaded documents"):
            saved = _save_uploads(uploaded_files or [], input_dir)
            if saved:
                st.success(f"Saved {len(saved)} file(s).")
                for path in saved:
                    st.write(str(path))
            else:
                st.info("No files selected.")
        st.code(f"PYTHONPATH=src python3.11 -m accountant_copilot.cli ingest-raw-inputs --input-dir {input_dir} --output-dir {artifact_dir}")

    workbench = _load_json(artifact_dir / "accountant_review_workbench.json", {})
    blockers = _load_json(artifact_dir / "release_blockers.json", {"blockers": []})

    with review_tab:
        _render_blockers(blockers.get("blockers", []))
        if not workbench:
            st.warning("No accountant_review_workbench.json found. Run export-accountant-review-workbench first.")
        else:
            edited = _render_workbench(workbench)
            st.download_button("Download filled workbench JSON", json.dumps(edited, indent=2, sort_keys=True), file_name="accountant_review_workbench_filled.json", mime="application/json")

    with artifacts_tab:
        st.header("Review artifacts")
        for label, rel in {
            "Release blockers": "release_blockers.md",
            "Draft statements": "draft_statements/draft_statements.md",
            "Post-journal TB": "post_journal_trial_balance.md",
            "Statement mapping": "statement_line_mapping.md",
            "Review packet": "review_packet/README.md",
        }.items():
            path = artifact_dir / rel
            if path.exists():
                st.subheader(label)
                st.caption(str(path))
                st.text_area(label, path.read_text()[:20000], height=220, key=f"artifact_{rel}")

    with apply_tab:
        st.header("Apply decisions through controls")
        st.write("Upload/downloaded filled workbench JSON here, then run the deterministic apply command. This app does not bypass validation.")
        filled = st.file_uploader("Filled workbench JSON", type=["json"], key="filled_workbench")
        target = artifact_dir / "accountant_review_workbench_filled.json"
        if st.button("Stage filled workbench for apply") and filled is not None:
            target.write_bytes(filled.getbuffer())
            st.success(f"Staged {target}")
        st.code(f"PYTHONPATH=src python3.11 -m accountant_copilot.cli apply-accountant-review-workbench --state {state_path} --workbench {target} --artifact-dir {artifact_dir} --output {artifact_dir / 'applied_accountant_review_workbench.json'}")
        if st.button("Run apply command"):
            result = _run_cli(["apply-accountant-review-workbench", "--state", str(state_path), "--workbench", str(target), "--artifact-dir", str(artifact_dir), "--output", str(artifact_dir / "applied_accountant_review_workbench.json")], repo_root)
            st.write(f"Exit code: {result.returncode}")
            if result.stdout:
                st.code(result.stdout)
            if result.stderr:
                st.error(result.stderr)


if __name__ == "__main__":
    main()
