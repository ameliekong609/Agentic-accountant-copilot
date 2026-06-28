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

`match-source-facts` uses Codex CLI to build the Step 3 Accounting Event Register. The register explains what happened, which source/bank evidence supports it, and what is still judgemental, without creating CoA mappings or journal postings.

`export-review-packet` includes `source_fact_layers.md` when source fact/review/matching markdown artifacts sit next to the engagement state, giving the accountant one packet section for bank transactions, invoice facts/review, distribution/tax facts/review, broker facts/review, and source-to-bank matching controls.

`build-coa-mapping-workpaper` uses Codex CLI as the Step 4 workpaper accountant. From the Step 3 Accounting Event Register and one explicit prior-year financial statement document, it writes `coa_mapping_workpaper.json`, `coa_mapping_workpaper.md`, and an Excel workbook with Summary, Draft CoA, Draft TB Bridge, Ready Events, Needs Judgement, Excluded, and Evidence Index sheets. The JSON must include Codex-authored `draft_coa_accounts` and `tb_matrix_movements`; the workbook builder only renders those into Excel. Prior-year FS balances and accountant presentation rows become the workpaper Starting point, and Codex creates dynamic accountant-style bridge columns from the evidence, such as bank columns, investment sale/gain columns, accrual/clearing columns, tax/fee columns, and beneficiary/distribution columns. Movement columns should normally net to zero across rows, with judgement/clearing rows used where evidence is incomplete.

`import-coa-from-prior-statements` imports candidate CoA accounts from prior-year financial statement evidence when no trial balance CSV is available. Imported accounts remain `pending_review`, link back to evidence refs, and set CoA review status to pending. In the raw-input workflow, Step 4 rebuilds the prior-FS import from the single selected prior-year FS in `source_document_index.json`; when more than one candidate exists, pass `--prior-fs-document-id` or `--prior-fs-file`.

Step 5 journal and trial-balance preparation should consume the reviewed Step 4 workbook rather than the removed CoA mapping approval-template flow.

Review packets now include `journal_tb_impact.md`, summarising CoA status, linked CoA/mapping/journal artifacts, pending/approved accounts, pending journal proposals, and the accountant review requirements before TB/final-statement reliance.

`export-journal-decision-template` and `apply-journal-decisions` provide the accountant approval round trip for journal proposals. Approval requires reviewer/rationale and resolves any `pending_review_offset` to a valid CoA account before the proposal can be marked approved.

`preview-tb-impact` groups approved journal proposals by account and reports debit/credit impact, excluded unapproved journals, placeholder offsets, missing accounts, and balance status.

`export-reviewed-journals` exports only approved journals to JSON, CSV, and markdown, and fails if any approved journal still contains `pending_review_offset`.

`build-post-journal-tb` builds the reviewed post-journal trial balance from approved reviewed journals and CoA opening balances. It excludes unreviewed journals, blocks placeholder offsets/missing accounts, and reports balanced debit/credit movement status.

`preview-statement-line-mapping` maps non-zero post-journal TB accounts to balance sheet or profit-and-loss lines using account type and presentation group, with unmapped accounts reported as findings.

`render-draft-statements-from-tb` renders internal-review-only draft statements from the post-journal TB and statement-line mapping. It carries control references and refuses clean output when TB or mapping findings remain.

`inspect-statement-chain-readiness` checks the reviewed-journal-to-draft-statement artifact chain and reports missing artifacts plus release blockers such as unapproved CoA, unresolved journals, or missing final sign-off.

`process-documents` processes uploaded files directly without an engagement state file. For each document, Codex CLI reads the available document text, suggests a `display_name`, classifies `document_type`, writes a short document summary and headline `primary_amounts`, writes `per_document/raw_XXX.json`, and refreshes `document_inventory.json`, `source_document_index.json`, and `source_coverage_continuity.json`. It does not create detailed accounting facts; Step 3 performs accounting-event investigation. Successful per-document results are cached under `.codex_doc_cache` by source hash, so unchanged development reruns reuse prior Codex output. Source indexing reads text PDFs, password-protected PDFs where filename passwords work, PNG/JPG OCR when local Tesseract is available, CSV/TXT/JSON/Markdown, and modern DOCX/XLSX/XLSM text; legacy binary DOC/XLS files should be converted before upload.

`export-draft-statement-review-template` and `apply-draft-statement-review` provide accountant approval/rejection for internal-review-only draft statements. Approval requires reviewer, rationale, clean draft findings, and a matching draft artifact hash.

`build-release-candidate-package` packages the reviewed journals, post-journal TB, statement mapping, and draft statements after draft approval, recording artifact hashes and source state hash in `release_candidate_manifest.json`.

`verify-release-candidate` checks release candidate hashes and reports missing artifacts or `hash_mismatch` findings when files are changed after packaging.

`export-final-release-manifest` ties final release to a verified release candidate plus final sign-off, and blocks stale-state or tampered-artifact release.

`export-accountant-review-workbench` creates one accountant-facing JSON/markdown workbench for pending CoA accounts, journal decisions, draft statement approval, and final sign-off. All decisions default blank and require reviewer/rationale before apply.

`apply-accountant-review-workbench` applies filled workbench decisions safely: blank decisions are ignored, unknown IDs fail, journal approvals require resolved offsets, and decisions are persisted to engagement state.

`explain-release-blockers` exports markdown/JSON blockers grouped by control layer: source evidence, CoA, journal, statement, release candidate, and final sign-off.

`export-review-ui-bundle` writes a read-only JSON bundle for local review UI use, including the workbench, release blockers, key artifacts, and state summary. It never applies approvals.

`export-accountant-review-ui` writes a local static HTML/JavaScript review workbench (`index.html`, `app.js`, and JSON bundle files) that lets an accountant fill CoA, journal, draft statement, and final sign-off decisions and download a filled workbench JSON. The UI has no network calls and does not mutate engagement state; approvals still go through `apply-accountant-review-workbench`.

`serve-workpaper-portal` starts the local accountant-facing workpaper portal. It lets a user upload a client zip/folder, reuse the repo `inputs` folder, or point to a local folder, then starts `prepare-workpaper` as a background job. The user can optionally confirm the target FY start/end and the single prior-year financial statement. The portal shows source indexing, relationship reasoning, TB bridge workbook generation, and Turing senior review status, then exposes the generated Excel workbook and summary for download. The portal is a local orchestration shell; the accounting reasoning still happens through Codex CLI and the senior review contract.

Document processing uses Codex CLI through `--codex-command`, `--codex-timeout`, and `--batch-size`. The default batch size is five documents, while outputs remain one `per_document/raw_XXX.json` and one cache record per source document. Successful per-document responses are cached under `.codex_doc_cache` using source hashes so unchanged development reruns do not repeat paid work.

`accounting_facts_by_document.json` carries each document's `display_name` into the grouped facts artifact. Documents without extracted accounting facts include `no_fact_reason`, distinguishing unreadable/no-evidence sources, unsupported document types, and documents where accounting facts were not found.

`source_coverage_continuity.json` is the Step 2B coverage artifact. It summarizes document/fact type counts, groups bank statement balance facts by account, detects duplicate statement periods, detects missing statement periods, and flags opening/closing balance roll-forward mismatches before source matching.

`match-source-facts --accounting-facts ...` uses Codex CLI for Step 3 source investigation. Codex receives the grouped accounting facts and optional source coverage artifact, then returns `investigative_source_matches` with `proposed_matches`, `hypotheses`, `unresolved_items`, and `validation_findings`. If Codex CLI is unavailable or the output fails validation, the command writes a `codex_failed` review artifact; it does not fall back to deterministic matching.
