# Document intake plan

This plan keeps internal use focused on existing data while defining safe next intake adapters.

## source quote contract

Every imported number must retain:

- source file path
- document ID
- page/sheet/row metadata where available
- raw source quote
- normalised amount/date value
- confidence score or deterministic parse status
- exception when confidence is low or required context is missing

## PDF intake

- Extract text and table candidates.
- Record page-level evidence refs before any accounting classification.
- Create low-confidence extraction exceptions instead of guessing.
- Do not auto-approve accounting treatment from OCR alone.

## Excel intake

- Preserve workbook, sheet, row, and cell references.
- Normalise dates and amounts with raw cell values retained.
- Detect hidden sheets, merged cells, formulas, and duplicate rows as review signals.
- Create exceptions for missing required columns or ambiguous headers.

## Review gate

PDF and Excel adapters should feed the same source document and evidence registries used by CSV intake. Matching, CoA import, statement rendering, and release controls should not need special cases for each document format.
