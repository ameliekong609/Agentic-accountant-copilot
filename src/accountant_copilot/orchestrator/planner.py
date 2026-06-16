"""Rule-based planning and readiness gates for the first copilot MVP."""
from __future__ import annotations

from dataclasses import dataclass

from accountant_copilot.orchestrator.task_graph import AgentTask, TaskStatus
from accountant_copilot.state.engagement import EngagementState
from accountant_copilot.state.exceptions import ExceptionSeverity


@dataclass
class ReadinessReport:
    final_output_allowed: bool
    blocking_exception_count: int
    human_approval_exception_count: int
    summary: str


def build_readiness_report(state: EngagementState) -> ReadinessReport:
    """Decide whether final output may be released.

    Critical/high open exceptions block release. Accepted-risk exceptions only
    stop release when they require approval but do not reference an approved
    accountant decision.
    """
    blocking = state.blocking_exceptions()
    human_approval = state.unresolved_human_approval_exceptions()
    allowed = not blocking and not human_approval

    if allowed:
        summary = "Final output allowed: no open critical/high exceptions and all required approvals are recorded."
    else:
        severity_words = sorted({item.severity.value for item in blocking})
        if severity_words:
            summary = (
                "Final output blocked by open "
                + "/".join(severity_words)
                + " exception(s)."
            )
        else:
            summary = "Final output blocked pending accountant approval."

    return ReadinessReport(
        final_output_allowed=allowed,
        blocking_exception_count=len(blocking),
        human_approval_exception_count=len(human_approval),
        summary=summary,
    )


def plan_next_tasks(state: EngagementState) -> list[AgentTask]:
    """Return the next recommended tasks for the engagement.

    This first version is intentionally deterministic. Later versions can let an
    LLM planner propose a richer task graph, but the release gate stays hard.
    """
    open_exceptions = state.open_exceptions()
    high_or_critical = [
        item
        for item in open_exceptions
        if item.severity in {ExceptionSeverity.CRITICAL, ExceptionSeverity.HIGH}
    ]
    if high_or_critical:
        return [
            AgentTask(
                agent_type="reviewer_agent",
                goal="Review and resolve open exception queue before final workbook release.",
                acceptance_criteria=[
                    "Each high/critical exception has evidence reviewed.",
                    "Each exception is resolved, rejected, or accepted as risk with an approved accountant decision.",
                    "No final FS release is allowed while critical/high exceptions remain open.",
                ],
                status=TaskStatus.READY,
                input_refs=[item.exception_id for item in high_or_critical],
                requires_human_approval=True,
            )
        ]

    if not state.documents_ref:
        return [
            AgentTask(
                agent_type="document_agent",
                goal="Classify source documents and identify engagement evidence coverage.",
                acceptance_criteria=[
                    "Every source document has type, period, entity, and role classified.",
                    "Missing or wrong-entity documents are raised as exceptions.",
                ],
                status=TaskStatus.READY,
            )
        ]

    if not state.coa_ref:
        return [
            AgentTask(
                agent_type="coa_agent",
                goal="Draft chart of accounts from prior-year statements and approved preferences.",
                acceptance_criteria=[
                    "Opening balances tie to source evidence.",
                    "CoA presentation preferences are applied only when approved.",
                    "Accountant review is requested for unusual accounts.",
                ],
                status=TaskStatus.READY,
                requires_human_approval=True,
            )
        ]

    readiness = build_readiness_report(state)
    if readiness.final_output_allowed:
        return [
            AgentTask(
                agent_type="financial_statement_agent",
                goal="Render review-ready trial balance and financial statement workbook.",
                acceptance_criteria=[
                    "Final workbook verifier passes.",
                    "Audit trail references source evidence and accountant decisions.",
                ],
                status=TaskStatus.READY,
            )
        ]

    return [
        AgentTask(
            agent_type="orchestrator",
            goal="Inspect engagement state and identify the next unblocked accounting task.",
            acceptance_criteria=["A concrete next task or blocker is recorded."],
            status=TaskStatus.READY,
        )
    ]
