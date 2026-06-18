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

`ingest-raw-inputs` registers every file under the requested input directory as a `SourceDocument`, classifies common raw source files, records markdown convention text as evidence, extracts text-based PDFs into page-level `EvidenceRef` records, extracts image OCR evidence with local Tesseract when available, and creates high-severity `source_extraction_required` exceptions for documents that do not yield extractable text.

`run-engagement --input-dir inputs` calls raw input intake before rendering review outputs. Final output must remain blocked while extraction-required exceptions are open.

`export-distribution-tax-facts` extracts evidence-linked distribution and tax statement facts from investment statement source evidence. It writes markdown plus sibling JSON, capturing payment/record dates and parseable distribution, dividend, capital-gain, tax-offset, and withholding components; unparseable candidate documents become findings instead of guessed facts.

`export-distribution-tax-review` creates unapproved accountant review findings for extracted distribution/tax facts, covering income mapping, tax component treatment, and bank receipt matching.

`export-broker-trade-facts` extracts evidence-linked broker confirmation facts when labels are parseable and writes incomplete-extraction findings instead of guessing.

`export-broker-trade-review` creates unapproved accountant review findings for broker disposal/acquisition classification, gain/loss treatment, and bank settlement matching.

`match-source-facts` matches extracted invoice, distribution/tax, and broker source facts to bank transaction evidence by exact amount/date. Proposed matches remain `approved=false`; ambiguous or missing matches become findings rather than forced links.

`export-review-packet` includes `source_fact_layers.md` when source fact/review/matching markdown artifacts sit next to the engagement state, giving the accountant one packet section for bank transactions, invoice facts/review, distribution/tax facts/review, broker facts/review, and source-to-bank matching controls.

`suggest-coa-mappings` creates unapproved CoA mapping suggestions from extracted source facts using the imported/chart accounts in engagement state. Missing candidate accounts and all proposed mappings become accountant review findings; mappings are not relied on until approved.

`import-coa-from-prior-statements` imports candidate CoA accounts from prior-year financial statement evidence when no trial balance CSV is available. Imported accounts remain `pending_review`, link back to evidence refs, and set CoA review status to pending.

`export-coa-mapping-template` and `apply-coa-mapping-decisions` provide the accountant review round trip for source-fact-to-CoA mappings. Blank templates do not apply decisions; filled decisions require mapping IDs, action, reviewer, and rationale, and persist approved/rejected mapping decisions to engagement state.

`propose-journals` creates pending-review journal proposals only from approved CoA mapping decisions. Each proposal is evidence-linked, uses a `pending_review_offset` side where the accounting treatment still needs review, and starts with `approved=0`; missing accounts become findings instead of forced journals.

Review packets now include `journal_tb_impact.md`, summarising CoA status, linked CoA/mapping/journal artifacts, pending/approved accounts, pending journal proposals, and the accountant review requirements before TB/final-statement reliance.

`export-journal-decision-template` and `apply-journal-decisions` provide the accountant approval round trip for journal proposals. Approval requires reviewer/rationale and resolves any `pending_review_offset` to a valid CoA account before the proposal can be marked approved.

`preview-tb-impact` groups approved journal proposals by account and reports debit/credit impact, excluded unapproved journals, placeholder offsets, missing accounts, and balance status.

`export-reviewed-journals` exports only approved journals to JSON, CSV, and markdown, and fails if any approved journal still contains `pending_review_offset`.
