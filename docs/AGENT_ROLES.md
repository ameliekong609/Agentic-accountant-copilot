# Agent Roles

## Engagement Orchestrator
Owns the engagement plan, task graph, and release readiness gate.

## Document Agent
Classifies source documents, periods, entities, and downstream evidence roles.

## CoA Agent
Drafts chart of accounts from prior-year statements, source evidence, and approved preferences.

## Bank Reconciliation Agent
Extracts and reconciles bank statement transactions; raises gaps and variance exceptions.

## Source Matching Agent
Matches bank movements to supporting events; escalates unmatched or ambiguous items.

## Journal Agent
Creates balanced journal drafts from approved matches and treatments.

## Tax / Distribution Agent
Handles entity-type-specific tax and distribution logic, especially trusts.

## Preference Agent
Retrieves and proposes firm/accountant/client/entity conventions. Only approved preferences may be applied automatically.

## Reviewer / Skeptic Agent
Challenges outputs and can block final release when controls or evidence are insufficient.

## Codex Engineering / Investigation Agent
Inspects code/data, traces failures, writes tests, fixes tools, and improves verifiers. Codex does not approve accounting judgment or final release.

## Human Accountant
Final authority for professional judgment, accepted risk, and client-ready financial statements.
