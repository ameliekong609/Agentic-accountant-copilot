# Document Intake Plan

## Current implemented path

1. `ingest-raw-inputs --input-dir inputs`
   - registers raw files as source documents
   - classifies document type from filename/type hints
   - extracts text-based PDFs into page-level `EvidenceRef` records
   - records readable markdown conventions as evidence
   - creates `source_extraction_required` exceptions for images/scanned PDFs without text

2. `export-document-inventory`
   - groups evidence by source document
   - lists page-level snippets
   - extracts high-level dates and currency amounts
   - applies content tags such as bank, distribution, tax, broker, interest, balance, and fees
   - writes both markdown and JSON inventory outputs

## Current boundary

This is high-level document/content discovery only. It is not final accounting treatment, reconciliation, journal generation, or financial statement sign-off. The source quote contract still applies: no accounting number should enter later workflows without file/page/row evidence and verifier checks.

## Next extraction layer

Add domain-specific fact extractors for:

- bank statement opening/closing balances and statement periods
- distribution/tax statement income components
- broker trade confirmations and settlement amounts
- prior-year financial statement comparative balances
- Excel workbook/sheet/row/cell evidence when source workbooks are supplied
- scanned image/OCR evidence
