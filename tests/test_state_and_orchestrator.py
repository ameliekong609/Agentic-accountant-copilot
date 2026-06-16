from __future__ import annotations

from accountant_copilot.orchestrator.planner import build_readiness_report, plan_next_tasks
from accountant_copilot.state.decisions import AccountantDecision, DecisionStatus
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionItem, ExceptionSeverity, ExceptionStatus
from accountant_copilot.state.preferences import PreferenceRule, PreferenceScope, PreferenceStatus


def test_engagement_state_round_trips_with_exceptions_decisions_and_preferences() -> None:
    exception = ExceptionItem(
        source="bank_reconciliation_agent",
        severity=ExceptionSeverity.CRITICAL,
        category="bank_reconciliation_variance",
        description="CBA closing balance does not tie to extracted transactions.",
        evidence_refs=["outputs/step6.json#verifier_findings/3"],
        recommended_action="Investigate missing or duplicated bank transaction before final FS release.",
        requires_human_approval=True,
    )
    decision = AccountantDecision(
        decision_id="dec_001",
        question="Accept $1 rounding difference in opening balance?",
        selected_option="Post to rounding/suspense after accountant approval",
        rationale="Immaterial prior-year rounding difference.",
        status=DecisionStatus.APPROVED,
        approved_by="Amelie",
    )
    preference = PreferenceRule(
        scope=PreferenceScope.CLIENT,
        subject="managed fund distributions",
        rule="Route recurring managed fund distributions through Sundry Debtors when prior-year treatment used that model.",
        status=PreferenceStatus.APPROVED,
        evidence_refs=["prior_year_fs:note_5"],
    )

    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Australia Financial Trust",
        entity_type="discretionary_trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        exceptions=[exception],
        decisions=[decision],
        preferences=[preference],
    )

    loaded = EngagementState.model_validate_json(state.model_dump_json())

    assert loaded.engagement_id == "xyz_fy2025"
    assert loaded.open_exceptions()[0].severity == ExceptionSeverity.CRITICAL
    assert loaded.approved_preferences()[0].scope == PreferenceScope.CLIENT
    assert loaded.decisions[0].status == DecisionStatus.APPROVED


def test_final_output_is_blocked_by_open_critical_exception() -> None:
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Australia Financial Trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        exceptions=[
            ExceptionItem(
                source="journal_agent",
                severity=ExceptionSeverity.CRITICAL,
                category="unbalanced_journal",
                description="Opening journal is out by $1.",
                recommended_action="Resolve or obtain explicit accountant approval before final FS release.",
                requires_human_approval=True,
            )
        ],
    )

    report = build_readiness_report(state)

    assert report.final_output_allowed is False
    assert report.blocking_exception_count == 1
    assert "critical" in report.summary.lower()


def test_final_output_allowed_after_critical_exception_is_approved_as_accepted_risk() -> None:
    approved_decision = AccountantDecision(
        decision_id="dec_accept_rounding",
        question="Accept $1 rounding difference?",
        selected_option="Accept risk and disclose in audit trail",
        rationale="Immaterial rounding variance from prior-year source document.",
        status=DecisionStatus.APPROVED,
        approved_by="Amelie",
    )
    accepted_exception = ExceptionItem(
        source="journal_agent",
        severity=ExceptionSeverity.CRITICAL,
        category="rounding_variance",
        description="Opening journal is out by $1.",
        recommended_action="Accept as immaterial rounding after approval.",
        status=ExceptionStatus.ACCEPTED_RISK,
        requires_human_approval=True,
        decision_id="dec_accept_rounding",
    )
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Australia Financial Trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        exceptions=[accepted_exception],
        decisions=[approved_decision],
    )

    report = build_readiness_report(state)

    assert report.final_output_allowed is True
    assert report.blocking_exception_count == 0


def test_planner_recommends_exception_review_before_workbook_release() -> None:
    state = EngagementState(
        engagement_id="xyz_fy2025",
        entity_name="XYZ Australia Financial Trust",
        fy_start="2024-07-01",
        fy_end="2025-06-30",
        exceptions=[
            ExceptionItem(
                source="matching_agent",
                severity=ExceptionSeverity.HIGH,
                category="unmatched_bank_transaction",
                description="Bank receipt has no supporting event.",
                recommended_action="Ask accountant to classify or provide missing support.",
                requires_human_approval=True,
            )
        ],
    )

    tasks = plan_next_tasks(state)

    assert tasks[0].agent_type == "reviewer_agent"
    assert tasks[0].requires_human_approval is True
    assert "exception" in tasks[0].goal.lower()
