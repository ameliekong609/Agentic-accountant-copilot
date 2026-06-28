# Agentic Accountant Copilot

Agentic Accountant Copilot is a clean-slate product foundation for preparing source-traceable financial statement workpapers with agentic orchestration, deterministic controls, accountant approval, and reusable preference memory.

## Product promise

The system is not positioned as a fully autonomous accountant. It is an accountant copilot that:

- plans the engagement work;
- delegates to specialist agents;
- preserves source evidence for every material number;
- maintains an exception queue;
- asks the accountant for judgment decisions;
- learns approved client/accountant/firm preferences; and
- blocks final release when critical controls are unresolved.

## Core idea

```text
Agentic planning + deterministic controls + accountant approval + preference memory
```

## Initial architecture

```text
src/accountant_copilot/
  orchestrator/      Engagement planning and readiness gates
  agents/            Agent role interfaces and future implementations
  tools/             Deterministic tools and source pipeline adapters
  state/             Engagement state, exceptions, decisions, preferences
```

## CLI MVP

Inspect an engagement state and see readiness, blockers, approvals needed, and the recommended next task:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli inspect-engagement \
  --state examples/sample_engagement_state.json
```

Machine-readable output:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli inspect-engagement \
  --state examples/sample_engagement_state.json \
  --json
```

Export a markdown audit trail of readiness, exceptions, evidence, and accountant decisions:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli export-audit-trail \
  --state outputs/engagement_state.json \
  --output outputs/audit_trail.md
```

Validate state, run orchestration, and record structured evidence:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli validate-state \
  --state outputs/engagement_state.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli run-engagement \
  --state outputs/engagement_state.json \
  --review-packet-dir outputs/review_packet \
  --release-manifest outputs/release_manifest.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli ingest-source-document \
  --state outputs/engagement_state.json \
  --document-id doc_bank_001 \
  --file-path source/bank.csv \
  --document-type bank_statement \
  --entity "XYZ Trust" \
  --period-start 2025-01-01 \
  --period-end 2025-01-31

PYTHONPATH=src python3.11 -m accountant_copilot.cli match-transactions \
  --state outputs/engagement_state.json \
  --bank-csv source/bank.csv \
  --events-csv source/events.csv \
  --output outputs/matches.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli record-document \
  --state outputs/engagement_state.json \
  --document-id doc_bank_001 \
  --file-path source/bank.csv \
  --document-type bank_statement \
  --entity "XYZ Trust" \
  --period-start 2025-01-01 \
  --period-end 2025-01-31

PYTHONPATH=src python3.11 -m accountant_copilot.cli record-evidence \
  --state outputs/engagement_state.json \
  --evidence-id ev_bank_row_7 \
  --source-type bank_statement \
  --file-path bank.csv \
  --row 7 \
  --document-id doc_bank_001 \
  --quote "Source quote text"
```

For the current upload workflow, use `process-documents` after upload. It is the single Codex path for display names, document type classification, accounting facts, per-document JSON, and cache reuse:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli process-documents \
  --input-dir inputs \
  --artifact-dir outputs/raw_inputs_pdf_extraction \
  --codex-command "codex exec" \
  --codex-timeout 120 \
  --codex-max-attempts 3 \
  --batch-size 5
```

The processing metadata is written to `per_document/raw_XXX.json`, `document_inventory.json`, `accounting_facts_by_document.json`, and `source_coverage_continuity.json`. Bank statement names include the detected bank and account when supported by content, for example `2025-01-31 - Commonwealth Bank Statement - Account 027.pdf`. Source files are not physically renamed by this step.

Rebuild the local Turing financial statement automation review workspace from raw `inputs/`:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli setup-turing-workspace \
  --input-dir inputs \
  --output-dir outputs/turing_financial_statement_setup
```

This creates a fresh engagement state, review packet, review UI, statement package, document inventory, bank facts, bank continuity checks, bank transactions, invoice facts/review, distribution tax facts, and `SETUP_RESULTS.md`. It is a review setup workflow only; it does not approve accounting treatment or release final statements.

Export a batch review template:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli export-review-template \
  --state outputs/engagement_state.json \
  --output outputs/review_decisions_template.json
```

Review CoA, adjustments, and export accountant review packet:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli record-coa-account \
  --state outputs/engagement_state.json \
  --account-id acct_cash \
  --code 1000 \
  --name "Cash at Bank" \
  --type asset \
  --presentation-group "Current assets" \
  --opening-balance 1000.00

PYTHONPATH=src python3.11 -m accountant_copilot.cli review-coa \
  --state outputs/engagement_state.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli approve-coa \
  --state outputs/engagement_state.json \
  --account-id acct_cash \
  --approved-by "Reviewer Name" \
  --rationale "CoA presentation and opening balances approved."

PYTHONPATH=src python3.11 -m accountant_copilot.cli record-adjustment \
  --state outputs/engagement_state.json \
  --adjustment-id adj_dist \
  --description "Year-end distribution accrual" \
  --debit-account "Distribution expense" \
  --credit-account "Distribution payable" \
  --amount 5000.00 \
  --date 2025-06-30

PYTHONPATH=src python3.11 -m accountant_copilot.cli review-adjustments \
  --state outputs/engagement_state.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli approve-adjustment \
  --state outputs/engagement_state.json \
  --adjustment-id adj_dist \
  --approved-by "Reviewer Name" \
  --rationale "Adjustment ties to support."

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-review-packet \
  --state outputs/engagement_state.json \
  --output-dir outputs/review_packet
```

Exit code policy:

- `0` — final output is allowed by current readiness gate.
- `1` — final output is blocked by open critical/high exceptions or missing approvals.

Review open exceptions and record accountant decisions:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli review-exceptions \
  --state outputs/engagement_state.json
```

Resolve or accept an exception with an approved accountant decision:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli review-exceptions \
  --state outputs/engagement_state.json \
  --exception-id exc_high_bank \
  --action resolved \
  --rationale "Matched to approved supporting evidence." \
  --approved-by "Reviewer Name"
```

Apply a batch of exception decisions:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli review-exceptions \
  --state outputs/engagement_state.json \
  --decisions decisions.json
```

Record final sign-off after readiness passes:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli sign-off-engagement \
  --state outputs/engagement_state.json \
  --approved-by "Reviewer Name" \
  --rationale "All review gates cleared."
```

Export a workpaper pack folder:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli export-workpaper-pack \
  --state outputs/engagement_state.json \
  --output-dir outputs/workpaper_pack
```

Record and list approved preference rules:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli record-preference \
  --state outputs/engagement_state.json \
  --scope client \
  --subject "XYZ Trust" \
  --rule "Present investment income by fund manager." \
  --approved-by "Reviewer Name"

PYTHONPATH=src python3.11 -m accountant_copilot.cli list-preferences \
  --state outputs/engagement_state.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli recommend-preferences \
  --state outputs/engagement_state.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli apply-preferences \
  --state outputs/engagement_state.json \
  --preference-id pref_client_income \
  --approved-by "Reviewer Name" \
  --rationale "Matches approved client convention."
```

Export a final release manifest after sign-off:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli record-output \
  --state outputs/engagement_state.json \
  --output-id out_fs \
  --file-path outputs/fs.xlsx \
  --artifact-type financial_statements \
  --verifier-status passed

PYTHONPATH=src python3.11 -m accountant_copilot.cli render-draft-statements \
  --state outputs/engagement_state.json \
  --output outputs/draft_financial_statements.md \
  --verifier-result outputs/verifier_result.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli import-verifier-result \
  --state outputs/engagement_state.json \
  --verifier-result outputs/verifier_result.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli recommend-templates \
  --state outputs/engagement_state.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-review-ui \
  --state outputs/engagement_state.json \
  --output outputs/review_packet/index.html

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-release-manifest \
  --state outputs/engagement_state.json \
  --output outputs/release_manifest.json \
  --workpaper-pack outputs/workpaper_pack \
  --audit-trail outputs/audit_trail.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli run-demo \
  --output-dir outputs/demo
```

Import source pipeline control issues into a new engagement state exception queue:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli import-source-exceptions \
  --matching /path/to/matching.json \
  --journal /path/to/journal.json \
  --output outputs/engagement_state.json \
  --engagement-id xyz_fy2025 \
  --entity-name "XYZ Australia Financial Trust" \
  --entity-type discretionary_trust \
  --fy-start 2024-07-01 \
  --fy-end 2025-06-30
```

## Development

Run tests from the repo root:

```bash
PYTHONPATH=src python3.11 -m pytest -q
```

Compile check:

```bash
python3.11 -m compileall -q src tests
```

If using an installed local venv:

```bash
python -m pip install -e '.[dev]'
pytest
```

## Design docs

- `docs/PRODUCT_VISION.md`
- `docs/ARCHITECTURE.md`
- `docs/AGENT_ROLES.md`
- `docs/CONTROLS_AND_POLICIES.md`
- `docs/SCHEMAS.md`

## Next queue controls

Additional build commands:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli match-transactions \
  --state outputs/engagement_state.json \
  --bank-csv source/bank.csv \
  --events-csv source/events.csv \
  --output outputs/matches.json \
  --amount-tolerance 0.02 \
  --date-window-days 2

PYTHONPATH=src python3.11 -m accountant_copilot.cli import-trial-balance \
  --state outputs/engagement_state.json \
  --trial-balance-csv source/trial_balance.csv

PYTHONPATH=src python3.11 -m accountant_copilot.cli render-statement-package \
  --state outputs/engagement_state.json \
  --output-dir outputs/statement_package
```

## Internal workflow commands

Run the internal engagement flow from existing source files:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli run-engagement \
  --state outputs/engagement_state.json \
  --bank-csv source/bank.csv \
  --events-csv source/events.csv \
  --trial-balance-csv source/trial_balance.csv \
  --statement-package-dir outputs/statement_package \
  --review-packet-dir outputs/review_packet \
  --review-ui outputs/review.html

PYTHONPATH=src python3.11 -m accountant_copilot.cli apply-review-ui-decisions \
  --state outputs/engagement_state.json \
  --decisions outputs/review_decisions_template.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli render-xlsx-statements \
  --state outputs/engagement_state.json \
  --output outputs/financial_statements.xlsx \
  --verifier-result outputs/xlsx_verifier_result.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-local-ui \
  --state outputs/engagement_state.json \
  --review-ui outputs/review.html \
  --output outputs/local_ui/index.html
```

Register raw input files, extract text-based PDF pages as evidence, and stop at remaining extraction gates:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli ingest-raw-inputs \
  --state outputs/engagement_state.json \
  --input-dir inputs

PYTHONPATH=src python3.11 -m accountant_copilot.cli run-engagement \
  --state outputs/engagement_state.json \
  --input-dir inputs \
  --statement-package-dir outputs/statement_package \
  --review-packet-dir outputs/review_packet \
  --review-ui outputs/review.html

PYTHONPATH=src python3.11 -m accountant_copilot.cli import-coa-from-prior-statements \
  --state outputs/engagement_state.json \
  --output outputs/prior_statement_coa_import.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-document-inventory \
  --state outputs/engagement_state.json \
  --output outputs/document_inventory.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-bank-statement-facts \
  --state outputs/engagement_state.json \
  --output outputs/bank_statement_facts.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-bank-transactions \
  --state outputs/engagement_state.json \
  --output outputs/bank_transactions.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-invoice-facts \
  --state outputs/engagement_state.json \
  --output outputs/invoice_facts.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-invoice-review \
  --facts outputs/invoice_facts.json \
  --output outputs/invoice_review.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-distribution-tax-facts \
  --state outputs/engagement_state.json \
  --output outputs/distribution_tax_facts.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-distribution-tax-review \
  --facts outputs/distribution_tax_facts.json \
  --output outputs/distribution_tax_review.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-broker-trade-facts \
  --state outputs/engagement_state.json \
  --output outputs/broker_trade_facts.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-broker-trade-review \
  --facts outputs/broker_trade_facts.json \
  --output outputs/broker_trade_review.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli match-source-facts \
  --accounting-facts outputs/accounting_facts_by_document.json \
  --source-coverage outputs/source_coverage_continuity.json \
  --codex-command "codex exec" \
  --codex-timeout 120 \
  --codex-max-attempts 3 \
  --output outputs/source_fact_matches.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli build-coa-mapping-workpaper \
  --artifact-dir outputs/raw_inputs_pdf_extraction \
  --output-dir outputs/step4_coa_mapping_workpaper \
  --codex-command "codex exec" \
  --codex-timeout 600 \
  --codex-max-attempts 3

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-journal-decision-template \
  --state outputs/engagement_state.json \
  --output outputs/journal_decisions_template.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli apply-journal-decisions \
  --state outputs/engagement_state.json \
  --decisions outputs/journal_decisions_template.json \
  --output outputs/applied_journal_decisions.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli preview-tb-impact \
  --state outputs/engagement_state.json \
  --output outputs/tb_impact_preview.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-reviewed-journals \
  --state outputs/engagement_state.json \
  --output-dir outputs/reviewed_journals

PYTHONPATH=src python3.11 -m accountant_copilot.cli build-post-journal-tb \
  --state outputs/engagement_state.json \
  --reviewed-journals outputs/reviewed_journals/reviewed_journals.json \
  --output outputs/post_journal_trial_balance.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli preview-statement-line-mapping \
  --post-journal-tb outputs/post_journal_trial_balance.json \
  --output outputs/statement_line_mapping.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli render-draft-statements-from-tb \
  --post-journal-tb outputs/post_journal_trial_balance.json \
  --mapping outputs/statement_line_mapping.json \
  --output-dir outputs/draft_statements

PYTHONPATH=src python3.11 -m accountant_copilot.cli inspect-statement-chain-readiness \
  --state outputs/engagement_state.json \
  --artifact-dir outputs

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-draft-statement-review-template \
  --state outputs/engagement_state.json \
  --draft outputs/draft_statements/draft_statements.json \
  --output outputs/draft_statement_review_template.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli apply-draft-statement-review \
  --state outputs/engagement_state.json \
  --decision outputs/draft_statement_review_template.json \
  --draft outputs/draft_statements/draft_statements.json \
  --output outputs/applied_draft_statement_review.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli build-release-candidate-package \
  --state outputs/engagement_state.json \
  --artifact-dir outputs \
  --output-dir outputs/release_candidate

PYTHONPATH=src python3.11 -m accountant_copilot.cli verify-release-candidate \
  --manifest outputs/release_candidate/release_candidate_manifest.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-final-release-manifest \
  --state outputs/engagement_state.json \
  --release-candidate outputs/release_candidate/release_candidate_manifest.json \
  --output outputs/final_release_manifest.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-accountant-review-workbench \
  --state outputs/engagement_state.json \
  --artifact-dir outputs \
  --output outputs/accountant_review_workbench.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli apply-accountant-review-workbench \
  --state outputs/engagement_state.json \
  --workbench outputs/accountant_review_workbench.json \
  --artifact-dir outputs \
  --output outputs/applied_accountant_review_workbench.json

PYTHONPATH=src python3.11 -m accountant_copilot.cli explain-release-blockers \
  --state outputs/engagement_state.json \
  --artifact-dir outputs \
  --output outputs/release_blockers.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-review-ui-bundle \
  --state outputs/engagement_state.json \
  --artifact-dir outputs \
  --output-dir outputs/review_ui_bundle

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-accountant-review-ui \
  --state outputs/engagement_state.json \
  --artifact-dir outputs \
  --output-dir outputs/accountant_review_ui

PYTHONPATH=src python3.11 -m accountant_copilot.cli serve-workpaper-portal \
  --host 127.0.0.1 \
  --port 8787
```

The workpaper portal is the accountant-facing local browser shell:

1. Upload a client zip/folder, point to a local client folder, or reuse the repo `inputs` folder.
2. Confirm the target financial year and the single prior-year financial statement when needed.
3. Start the background workpaper run. The portal calls `prepare-workpaper` with Codex CLI, bounded retries, Step 3 relationship reasoning, Step 4 TB bridge generation, and Turing senior review.
4. Source indexing reads text PDFs, password-protected PDFs where the filename contains the password, PNG/JPG OCR when local Tesseract is available, CSV/TXT/JSON/Markdown, and modern DOCX/XLSX/XLSM text. Legacy binary DOC/XLS files should be converted to PDF/DOCX/XLSX before upload.
5. Watch status for source indexing, relationship reasoning, TB bridge workbook creation, and Turing review.
6. Download the Excel workbook and summary when complete.
7. Keep the portal local for first trials. To share inside the team, run it on Amelie's laptop and expose the URL only through a trusted local network, Tailscale, or a tunnel.

The background runner also starts an engineering watcher by default. It checks the same progress checkpoints every five minutes, diagnoses failed or stale jobs, and writes a Markdown diagnosis into the job folder. It only changes product code when explicitly enabled:

- `ACCOUNTANT_COPILOT_ENGINEER_WATCHER=0` disables the watcher.
- `ACCOUNTANT_COPILOT_ENGINEER_AUTOFIX=1` lets the watcher patch product code for failed jobs and run focused verification.
- `ACCOUNTANT_COPILOT_ENGINEER_AUTOFIX_STALE=1` also allows patching while a job is stale but still running.
- `ACCOUNTANT_COPILOT_ENGINEER_STALE_SECONDS=900` controls when a running job is considered stale.

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli export-bank-continuity \
  --facts outputs/bank_statement_facts.json \
  --output outputs/bank_continuity.md
```
