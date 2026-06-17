from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity
from accountant_copilot.state.preferences import PreferenceRule, PreferenceScope, PreferenceStatus

ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "accountant_copilot.cli", *args],
        cwd=ROOT,
        env={"PYTHONPATH": "src"},
        text=True,
        capture_output=True,
        check=False,
    )


def _base_state(path: Path, *, signed_off: bool = False) -> None:
    decisions = []
    if signed_off:
        decisions.append(
            AccountantDecision(
                decision_id="decision_final_signoff_0001",
                question="May the final financial statement workpaper pack be released?",
                selected_option="final_signoff",
                rationale="All review gates cleared.",
                status=DecisionStatus.APPROVED,
                approved_by="Amelie",
            )
        )
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        documents_ref="outputs/documents.json",
        coa_ref="outputs/coa.json",
        statements_ref="outputs/statements.xlsx",
        decisions=decisions,
    )
    path.write_text(state.model_dump_json())


def test_validate_state_and_schema_docs(tmp_path: Path) -> None:
    valid_state = tmp_path / "engagement_state.json"
    _base_state(valid_state)
    invalid_state = tmp_path / "bad_state.json"
    invalid_state.write_text(json.dumps({"engagement_id": "missing required fields"}))

    ok = _run_cli("validate-state", "--state", str(valid_state))
    bad = _run_cli("validate-state", "--state", str(invalid_state))

    assert ok.returncode == 0
    assert "Engagement state is valid" in ok.stdout
    assert bad.returncode == 2
    assert "missing required field" in bad.stderr.lower()
    assert (ROOT / "docs" / "SCHEMAS.md").exists()


def test_record_evidence_and_audit_trail_renders_structured_details(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    _base_state(state_path)

    recorded = _run_cli(
        "record-evidence",
        "--state",
        str(state_path),
        "--evidence-id",
        "ev_bank_row_7",
        "--source-type",
        "bank_statement",
        "--file-path",
        "bank.csv",
        "--row",
        "7",
        "--quote",
        "Dividend receipt 1000.00",
        "--amount",
        "1000.00",
        "--date",
        "2025-01-10",
        "--confidence",
        "0.98",
    )
    assert recorded.returncode == 0
    assert "Recorded evidence ev_bank_row_7" in recorded.stdout

    loaded = json.loads(state_path.read_text())
    assert loaded["evidence"][0]["evidence_id"] == "ev_bank_row_7"

    audit = _run_cli("export-audit-trail", "--state", str(state_path))
    assert audit.returncode == 0
    assert "## Evidence registry" in audit.stdout
    assert "ev_bank_row_7" in audit.stdout
    assert "Dividend receipt 1000.00" in audit.stdout


def test_export_review_template_from_open_exceptions(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    output = tmp_path / "review_template.json"
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        exceptions=[
            ExceptionItem(
                exception_id="exc_open",
                source="matching_agent",
                severity=ExceptionSeverity.HIGH,
                category="unmatched_bank_transaction",
                description="Needs classification.",
                evidence_refs=["ev_bank_row_7"],
                recommended_action="Review support.",
                requires_human_approval=True,
            )
        ],
    )
    state_path.write_text(state.model_dump_json())

    result = _run_cli("export-review-template", "--state", str(state_path), "--output", str(output))

    assert result.returncode == 0
    assert "Exported review template" in result.stdout
    template = json.loads(output.read_text())
    assert template["engagement_id"] == "xyz_fy2025"
    assert template["decisions"][0]["exception_id"] == "exc_open"
    assert template["decisions"][0]["action"] == ""
    assert template["decisions"][0]["rationale"] == ""
    assert template["decisions"][0]["approved_by"] == ""
    assert template["decisions"][0]["recommended_action"] == "Review support."


def test_recommend_and_apply_preferences(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        preferences=[
            PreferenceRule(
                preference_id="pref_client_income",
                scope=PreferenceScope.CLIENT,
                subject="XYZ Trust",
                rule="Present investment income by fund manager.",
                status=PreferenceStatus.APPROVED,
                approved_by="Amelie",
            ),
            PreferenceRule(
                preference_id="pref_suggested",
                scope=PreferenceScope.CLIENT,
                subject="XYZ Trust",
                rule="Suggested only.",
                status=PreferenceStatus.SUGGESTED,
            ),
        ],
    )
    state_path.write_text(state.model_dump_json())

    recommended = _run_cli("recommend-preferences", "--state", str(state_path))
    assert recommended.returncode == 0
    assert "pref_client_income" in recommended.stdout
    assert "pref_suggested" not in recommended.stdout

    applied = _run_cli(
        "apply-preferences",
        "--state",
        str(state_path),
        "--preference-id",
        "pref_client_income",
        "--approved-by",
        "Amelie",
        "--rationale",
        "Matches client convention.",
    )
    assert applied.returncode == 0
    assert "Applied preference pref_client_income" in applied.stdout
    loaded = EngagementState.model_validate_json(state_path.read_text())
    assert loaded.decisions[-1].selected_option == "apply_preference"
    assert loaded.decisions[-1].evidence_refs == ["pref_client_income"]


def test_release_manifest_requires_signoff_and_exports_manifest(tmp_path: Path) -> None:
    state_path = tmp_path / "engagement_state.json"
    output = tmp_path / "release_manifest.json"
    _base_state(state_path, signed_off=False)

    blocked = _run_cli("export-release-manifest", "--state", str(state_path), "--output", str(output))
    assert blocked.returncode == 1
    assert "Cannot export release manifest before final sign-off" in blocked.stderr

    _base_state(state_path, signed_off=True)
    ok = _run_cli(
        "export-release-manifest",
        "--state",
        str(state_path),
        "--output",
        str(output),
        "--workpaper-pack",
        "outputs/workpaper_pack",
        "--audit-trail",
        "outputs/audit_trail.md",
    )
    assert ok.returncode == 0
    manifest = json.loads(output.read_text())
    assert manifest["engagement_id"] == "xyz_fy2025"
    assert manifest["final_output_allowed"] is True
    assert manifest["signoff_decision_id"] == "decision_final_signoff_0001"
    assert manifest["workpaper_pack"] == "outputs/workpaper_pack"
    assert manifest["audit_trail"] == "outputs/audit_trail.md"
