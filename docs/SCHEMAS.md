# Schemas

This document describes the stable JSON shapes used by the CLI control loop.

## Engagement state

Required fields:

- `engagement_id`
- `entity_name`
- `fy_start`
- `fy_end`

Common optional fields:

- `entity_type`
- `documents_ref`
- `coa_ref`
- `bank_txns_ref`
- `events_ref`
- `matches_ref`
- `journals_ref`
- `statements_ref`
- `exceptions`
- `decisions`
- `preferences`
- `evidence`
- `source_documents`
- `chart_accounts`
- `adjustment_proposals`
- `output_artifacts`
- `coa_review_required`
- `coa_review_status`
- `adjustment_review_status`
- `lifecycle_status`

Lifecycle status values currently used by CLI outputs:

- `intake`
- `evidence_imported`
- `exceptions_open`
- `signed_off`
- `released`

## Batch review decisions

`review-exceptions --decisions decisions.json` expects:

```json
{
  "decisions": [
    {
      "exception_id": "exc_...",
      "action": "resolved",
      "rationale": "Why the accountant accepted this treatment.",
      "approved_by": "Reviewer Name"
    }
  ]
}
```

Allowed `action` values:

- `resolved`
- `accepted_risk`
- `rejected`

The batch is validated before state mutation. Unknown exception IDs or missing required fields fail the command.

## Evidence registry

Structured evidence records use:

```json
{
  "evidence_id": "ev_bank_row_7",
  "source_type": "bank_statement",
  "file_path": "bank.csv",
  "page": null,
  "row": "7",
  "quote": "Source quote text",
  "amount": "1000.00",
  "date": "2025-01-10",
  "confidence": "0.98"
}
```

## Source documents

Source document records use:

```json
{
  "document_id": "doc_bank_001",
  "file_path": "source/bank.csv",
  "document_type": "bank_statement",
  "entity": "XYZ Trust",
  "period_start": "2025-01-01",
  "period_end": "2025-01-31",
  "source_hash": "sha256...",
  "status": "recorded",
  "notes": null
}
```

## Structured CoA accounts

```json
{
  "account_id": "acct_cash",
  "code": "1000",
  "name": "Cash at Bank",
  "type": "asset",
  "presentation_group": "Current assets",
  "opening_balance": "1000.00",
  "source_evidence_refs": ["ev_bank_row_1"],
  "status": "pending_review",
  "decision_id": null
}
```

## Adjustment proposals

```json
{
  "adjustment_id": "adj_dist",
  "description": "Year-end distribution accrual",
  "debit_account": "Distribution expense",
  "credit_account": "Distribution payable",
  "amount": "5000.00",
  "date": "2025-06-30",
  "source_evidence_refs": ["ev_distribution"],
  "status": "pending_review",
  "decision_id": null
}
```

## Output artifacts

```json
{
  "output_id": "out_fs",
  "file_path": "outputs/fs.xlsx",
  "artifact_type": "financial_statements",
  "verifier_status": "passed",
  "created_at": "2026-06-17T00:00:00+00:00",
  "source_state_hash": "sha256...",
  "release_manifest_id": null
}
```
