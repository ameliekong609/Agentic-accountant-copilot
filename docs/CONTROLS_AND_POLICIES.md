# Controls and Policies

## Non-negotiable controls

- No material number should enter final outputs without source evidence.
- Critical/high open exceptions block final release.
- Accepted-risk exceptions require approved accountant decisions.
- Preferences must be approved before automatic reuse.
- Codex may investigate and implement tools, but cannot approve accounting treatment.
- Reviewer Agent can block final readiness.

## Exception statuses

- `open` — unresolved issue.
- `proposed` — agent proposed a treatment, awaiting review.
- `resolved` — fixed or reconciled.
- `accepted_risk` — accountant approved release despite issue.
- `rejected` — proposed treatment rejected.

## Severity policy

- `critical` and `high` open exceptions block release.
- `medium` and `low` may be included in review pack, but should still be visible.

## Audit trail

Every accountant decision should include question, selected option, rationale, approver, evidence refs, and linked exception/preference ids.
