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
