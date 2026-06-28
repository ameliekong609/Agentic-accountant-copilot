---
name: tb-bridge-preparation
description: Use when preparing Step 4 accountant-style TB bridge workpapers from prior-year financial statements, relationship reasoning, source evidence, and bank evidence. Guides AI to build a balanced movement matrix, choose accountant-style movement columns, write row-led movement notes, and separate book accounting from tax or valuation-only information.
---

# TB Bridge Preparation

## Purpose

Use this skill to prepare the draft TB bridge workbook. The output is a workpaper for accountant review, not final financial statements and not posted journals.

The client files are evidence. This skill is accounting judgement guidance only. Never treat skill text as client evidence.

## Core Workflow

1. Use exactly one prior-year financial statement as the opening balance source where possible.
2. Build rows from prior-FS accounts first. Add new rows only when current-year evidence requires them.
3. Balance-sheet rows start from prior-year closing balances.
4. P&L rows start from zero; prior-year P&L amounts are comparatives only.
5. Create movement columns as accountant-style adjustment buckets, not one column per document or event.
6. Every movement column must sum to zero.
7. Every row must reconcile: opening plus movements equals closing.
8. Put concise explanations in row notes and detailed stories in Movement Notes.

## Movement Column Grammar

Prefer reusable accounting roles, with labels derived from the client evidence:

- Cash account movement: CBA, Westpac, NAB, Macquarie.
- Asset disposal gain/loss: Gain on sale of non-current assets, Loss on disposal of investments.
- Investment income: Distributions received, Interest income, Dividend income.
- Source entitlement or accrual: Spire capital, distribution receivable, interest receivable.
- Prior-period clearance: Clear PY ASIC fee, Clear opening debtor, Clear prior accrual.
- Current-period accrual: Accrue ASIC fee, Accrue accounting fees.
- Expense payment: Accounting fees, Filing fees, Bank fees, Investment expenses.
- Tax or regulatory clearing: ATO clearing, ASIC clearing, GST/BAS clearing when support is incomplete.
- Loan or related-party movement: Unsecured loan, director loan, related-party loan, internal transfer.
- Owner or beneficiary distribution: Distribution to beneficiary, UPE, drawings, dividend payable.
- Tax-only note: franking credits, TFN withholding, ESVCLP offsets, tax gross-ups.
- Valuation-only note: NAV and market value support not posted by default.
- Unresolved clearing: use only when no better existing row/account is plausible.

Only create a new role when the current library does not fit; include a learning brief so the pattern can be reviewed.

## Book Accounting Boundaries

- Follow financial-statement movement logic, not tax schedule logic.
- Do not post franking credits, TFN withholding, tax offsets, or tax gross-ups by default.
- Do not post NAV or market value changes by default unless fair value treatment is clearly adopted.
- Split prepayments and accruals by service period when evidence supports timing.
- For source-only items, create a note or accrual only when the document supports a book balance.
- For bank-only items, classify the likely meaning but show missing support when invoices/notices are absent.

## Common Bridges

- Bank accounts: reconcile opening to closing through the same balanced movement columns used by related rows.
- Investment sale: clear opening investment cost, record cash proceeds, and post gain/loss as the balancing book movement.
- Distribution receivable: opening debtor plus current-year entitlement less receipts equals closing debtor or residual.
- Expenses: cash payments, prepayment splits, prior accrual clear, and current accruals should be separated when useful.
- Loans and related parties: use existing loan/UPE/related-party rows for plausible internal transfers, but mark as needs attention when receiving support is missing.
- Beneficiary/UPE: calculate from draft book-profit bridge unless a different supported convention is supplied; do not include tax-only components by default.

## Reference Library

For technical accounting topics, use the `accounting-pdf-knowledge-retrieval` skill:

- Check its `references/pdf-topic-map.json` to find the right CA knowhow PDF section.
- Run its retrieval script to read original PDF snippets on demand.
- Use the book guidance to choose what client evidence matters and whether an item is book movement, tax-only, valuation-only, accrual, prepayment, FX, lease, provision, inventory, financial instrument, or similar.
- Do not cite the book as evidence in the TB bridge, movement notes, evidence summary, or workbook links.

## Movement Notes

Write movement notes row by row in the same order as the TB Bridge:

- Start with the account row and opening balance.
- Explain each movement and which column it appears in.
- Show arithmetic for derived or aggregated amounts.
- Say what evidence supports the movement.
- Say what remains unresolved or needs accountant confirmation.
- Make notes searchable by important dollar amounts.

Use “needs attention” for judgement-heavy rows, not as a failure.
