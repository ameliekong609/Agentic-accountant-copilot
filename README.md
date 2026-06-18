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
  --bank-transactions outputs/bank_transactions.json \
  --invoice-facts outputs/invoice_facts.json \
  --distribution-tax-facts outputs/distribution_tax_facts.json \
  --broker-trade-facts outputs/broker_trade_facts.json \
  --output outputs/source_fact_matches.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli suggest-coa-mappings \
  --state outputs/engagement_state.json \
  --invoice-facts outputs/invoice_facts.json \
  --distribution-tax-facts outputs/distribution_tax_facts.json \
  --broker-trade-facts outputs/broker_trade_facts.json \
  --output outputs/coa_mapping_suggestions.md

PYTHONPATH=src python3.11 -m accountant_copilot.cli export-bank-continuity \
  --facts outputs/bank_statement_facts.json \
  --output outputs/bank_continuity.md
```
