from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.artifacts import AdjustmentProposal, ChartAccount
from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.engagement import EngagementState

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str):
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=ROOT,
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )


def _write_clean_statement_chain(tmp_path: Path) -> Path:
    state = EngagementState(engagement_id="release_flow", entity_name="Release Flow Trust", entity_type="trust", fy_start="2024-07-01", fy_end="2025-06-30")
    state.chart_accounts.extend([
        ChartAccount(account_id="acct_100", code="100", name="Cash", type="asset", presentation_group="Cash", opening_balance="100.00", status="approved"),
        ChartAccount(account_id="acct_400", code="400", name="Income", type="income", presentation_group="Revenue", opening_balance="0.00", status="approved"),
    ])
    state.coa_review_status = "approved"
    state.adjustment_proposals.append(AdjustmentProposal(adjustment_id="journal_income", description="Income", debit_account="acct_100", credit_account="acct_400", amount="25.00", date="2025-06-30", status="approved", decision_id="decision_j1"))
    state.adjustment_review_status = "approved"
    state_path = tmp_path / "engagement_state.json"
    state_path.write_text(state.model_dump_json())
    reviewed_dir = tmp_path / "reviewed_journals"
    assert run_cli("export-reviewed-journals", "--state", str(state_path), "--output-dir", str(reviewed_dir)).returncode == 0
    assert run_cli("build-post-journal-tb", "--state", str(state_path), "--reviewed-journals", str(reviewed_dir / "reviewed_journals.json"), "--output", str(tmp_path / "post_journal_trial_balance.md")).returncode == 0
    assert run_cli("preview-statement-line-mapping", "--post-journal-tb", str(tmp_path / "post_journal_trial_balance.json"), "--output", str(tmp_path / "statement_line_mapping.md")).returncode == 0
    assert run_cli("render-draft-statements-from-tb", "--post-journal-tb", str(tmp_path / "post_journal_trial_balance.json"), "--mapping", str(tmp_path / "statement_line_mapping.json"), "--output-dir", str(tmp_path / "draft_statements")).returncode == 0
    return state_path


def test_draft_statement_review_template_and_approval(tmp_path: Path):
    state_path = _write_clean_statement_chain(tmp_path)
    template = tmp_path / "draft_statement_review_template.json"

    exported = run_cli("export-draft-statement-review-template", "--state", str(state_path), "--draft", str(tmp_path / "draft_statements" / "draft_statements.json"), "--output", str(template))

    assert exported.returncode == 0
    payload = json.loads(template.read_text())
    assert payload["draft_status"] == "internal_review_only"
    assert payload["decision"]["action"] == ""
    payload["decision"].update({"action": "approve", "approved_by": "Reviewer", "rationale": "Draft agrees to reviewed TB."})
    decisions = tmp_path / "draft_statement_review.json"
    decisions.write_text(json.dumps(payload))

    applied = run_cli("apply-draft-statement-review", "--state", str(state_path), "--decision", str(decisions), "--draft", str(tmp_path / "draft_statements" / "draft_statements.json"), "--output", str(tmp_path / "applied_draft_statement_review.json"))

    assert applied.returncode == 0
    updated = json.loads(state_path.read_text())
    assert updated["decisions"][-1]["selected_option"] == "approve_draft_statements"
    applied_payload = json.loads((tmp_path / "applied_draft_statement_review.json").read_text())
    assert applied_payload["draft_status"] == "accountant_approved_draft"


def test_release_candidate_hash_verify_and_final_release(tmp_path: Path):
    state_path = _write_clean_statement_chain(tmp_path)
    state = EngagementState.model_validate_json(state_path.read_text())
    state.decisions.append(AccountantDecision(decision_id="decision_approve_draft_statements_0001", question="Approve draft statements?", selected_option="approve_draft_statements", rationale="Approved.", status=DecisionStatus.APPROVED, approved_by="Reviewer", evidence_refs=["draft_statements/draft_statements.json"]))
    state.decisions.append(AccountantDecision(decision_id="decision_final_signoff_0001", question="Final release?", selected_option="final_signoff", rationale="Final approved.", status=DecisionStatus.APPROVED, approved_by="Reviewer"))
    state_path.write_text(state.model_dump_json())

    built = run_cli("build-release-candidate-package", "--state", str(state_path), "--artifact-dir", str(tmp_path), "--output-dir", str(tmp_path / "release_candidate"))

    assert built.returncode == 0
    manifest = json.loads((tmp_path / "release_candidate" / "release_candidate_manifest.json").read_text())
    assert manifest["status"] == "release_candidate"
    assert "draft_statements/draft_statements.json" in manifest["artifacts"]

    verified = run_cli("verify-release-candidate", "--manifest", str(tmp_path / "release_candidate" / "release_candidate_manifest.json"))
    assert verified.returncode == 0

    tampered = tmp_path / "draft_statements" / "draft_statements.md"
    tampered.write_text(tampered.read_text() + "\nTamper")
    failed = run_cli("verify-release-candidate", "--manifest", str(tmp_path / "release_candidate" / "release_candidate_manifest.json"))
    assert failed.returncode == 1
    assert "hash_mismatch" in failed.stdout

    tampered.write_text(tampered.read_text().replace("\nTamper", ""))
    final = run_cli("export-final-release-manifest", "--state", str(state_path), "--release-candidate", str(tmp_path / "release_candidate" / "release_candidate_manifest.json"), "--output", str(tmp_path / "final_release_manifest.json"))
    assert final.returncode == 0
    final_payload = json.loads((tmp_path / "final_release_manifest.json").read_text())
    assert final_payload["release_candidate_manifest"].endswith("release_candidate_manifest.json")
    assert final_payload["status"] == "final_release_manifest"
