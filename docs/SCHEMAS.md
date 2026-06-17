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
- `state_transitions`
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

## State transitions

State-changing orchestration can record transition hashes:

```json
{
  "transition_id": "transition_0001",
  "command": "run-engagement",
  "before_hash": "sha256...",
  "after_hash": "sha256...",
  "actor": "system",
  "timestamp": "2026-06-17T00:00:00+00:00",
  "summary": "Engagement blocked; review packet exported."
}
```

Evidence records may include `document_id` when linked to a source document register entry.

## Intake, matching, and draft output command contracts

`ingest-source-document` currently supports CSV intake. It records one `SourceDocument` and creates row-level `EvidenceRef` entries with `document_id`, row number, quote, amount, and date where columns are available.

`match-transactions` performs deterministic exact date/amount matching between bank and supporting event CSV files. It writes a JSON match artifact with `matches`, `unmatched_bank_transactions`, and `unmatched_events`; unmatched rows create idempotent review exceptions from `deterministic_matching`.

`render-draft-statements` writes a markdown draft financial statement artifact, writes a verifier result JSON, and registers `out_draft_statements` in `output_artifacts`.

`run-demo` creates safe sample blocked and clean engagement flows under the requested output directory. Do not place client data in demo fixtures.

## Next queue controls

CSV source intake now validates required `date`, `description`, and `amount` columns, normalises common date/amount formats, and raises `duplicate_source_row` exceptions when duplicate source rows are detected.

`match-transactions` supports `--amount-tolerance` and `--date-window-days`, reference/date/amount tolerance matches, composite amount matches, and evidence references in match artifacts.

`import-trial-balance` imports trial balance CSV rows into structured CoA accounts and flags duplicate account codes or suspense accounts as accountant-review exceptions.

`render-statement-package` writes a structured draft statement package folder with balance sheet, income/distribution statement, verifier detail, and output artifact registration.

## Internal workflow commands

`run-engagement` can now take internal source inputs (`--bank-csv`, `--events-csv`, `--trial-balance-csv`) and orchestrate intake, matching, trial balance import, statement package rendering, review packet export, and review UI export without bypassing accountant gates.

`apply-review-ui-decisions` applies the copyable review UI decision JSON back into engagement state for exceptions, CoA approvals, adjustment decisions, preferences, and output verifier decisions.

`render-xlsx-statements` writes a dependency-free XLSX financial statement workbook and verifier result, registering an `xlsx_financial_statements` output artifact.

`export-local-ui` writes a lightweight internal HTML wrapper linking local review artifacts. It is for internal workflow convenience and does not replace review controls.

## Raw input intake controls

`ingest-raw-inputs` registers every file under the requested input directory as a `SourceDocument`, classifies common raw source files, records markdown convention text as evidence, extracts text-based PDFs into page-level `EvidenceRef` records, and creates high-severity `source_extraction_required` exceptions for images/scanned PDFs that do not yield text.

`run-engagement --input-dir inputs` calls raw input intake before rendering review outputs. Final output must remain blocked while extraction-required exceptions are open.
