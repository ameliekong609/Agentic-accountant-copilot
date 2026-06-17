# Document Intake Plan

## Current implemented path

1. `ingest-raw-inputs --input-dir inputs`
   - registers raw files as source documents
   - classifies document type from filename/type hints
   - extracts text-based PDFs into page-level `EvidenceRef` records
   - extracts image text with local Tesseract OCR when available
   - records readable markdown conventions as evidence
   - creates `source_extraction_required` exceptions for PDFs/images/scans without extractable text

2. `export-document-inventory`
   - groups evidence by source document
   - lists page-level snippets
   - extracts high-level dates and currency amounts
   - applies content tags such as bank, distribution, tax, broker, interest, balance, and fees
   - writes both markdown and JSON inventory outputs

3. `export-bank-statement-facts`
   - runs only against bank statement evidence
   - extracts statement period, opening balance, closing balance, and statement-level total credits/debits from source snippets
   - links every extracted fact back to its `EvidenceRef`
   - reports missing period/opening/closing balance findings instead of guessing

4. `export-bank-transactions`
   - extracts evidence-linked transaction rows from bank statement page text
   - captures transaction date, description, debit/credit amount, running balance when visible, page, evidence ID, and confidence
   - excludes statement opening/closing balance summary rows from transaction rows
   - reports documents where transaction rows are not extractable instead of guessing

5. `export-invoice-facts`
   - extracts evidence-linked invoice facts from OCR/PDF source evidence
   - captures invoice number, invoice date, due date, supplier, description, service period, subtotal, GST, amount due, page, evidence ID, and confidence
   - reports incomplete invoice-like source evidence instead of guessing missing fields

6. `export-invoice-review`
   - turns extracted invoice facts into accountant review findings
   - proposes candidate treatment such as portfolio management fee/service expense, GST review, period allocation, and payment/matching handling
   - explicitly sets `approved=false`; no accounting treatment is auto-approved from OCR or parsed invoice text

7. `export-distribution-tax-facts`
   - extracts evidence-linked distribution and tax statement facts from investment statement evidence
   - captures payment date, record date, cash/net distribution, interest/dividend/capital-gain-style components, tax offsets, and withholding when labels are parseable
   - reports distribution/tax candidate documents where numeric component lines are not parseable instead of guessing

8. `export-distribution-tax-review`
   - turns extracted distribution/tax facts into accountant review findings
   - asks for income mapping, tax component treatment, and bank receipt matching review
   - explicitly sets `approved=false`; no distribution/tax accounting treatment is auto-approved

9. `export-broker-trade-facts`
   - extracts evidence-linked broker confirmation facts when parseable
   - captures buy/sell side, transaction/settlement dates, settlement amount, consideration, quantity, price, fees, GST, company/security, and transaction identifiers where labels are available
   - reports incomplete broker confirmation extraction instead of guessing

10. `export-broker-trade-review`
   - turns extracted broker trade facts into accountant review findings
   - asks for disposal/acquisition classification, gain/loss treatment, and bank settlement matching review
   - explicitly sets `approved=false`; no realised gain/loss or investment treatment is auto-approved

11. `export-bank-continuity`
   - compares sequential bank statement closing balances to next opening balances
   - groups statements by inferred account key before comparing
   - accepts same-day or next-day period bridges, because different banks label statement boundaries differently
   - reports continuity breaks, missing opening/closing balances, duplicate periods, and period gaps instead of guessing

## Current boundary

This is high-level document/content discovery only. It is not final accounting treatment, reconciliation, journal generation, or financial statement sign-off. The source quote contract still applies: no accounting number should enter later workflows without file/page/row evidence and verifier checks.

## Next extraction layer

Add domain-specific fact extractors for:

- bank statement opening/closing balances and statement periods
- distribution/tax statement accounting review layer
- broker trade confirmations and settlement amounts
- prior-year financial statement comparative balances
- Excel workbook/sheet/row/cell evidence when source workbooks are supplied
- scanned image/OCR evidence
