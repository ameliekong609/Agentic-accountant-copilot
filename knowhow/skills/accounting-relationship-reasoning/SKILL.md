---
name: accounting-relationship-reasoning
description: Use when building Step 3 accounting relationship reasoning from a client pack, prior-year financial statements, source documents, bank statements, invoices, investment statements, tax statements, and broker records before preparing a TB bridge. Guides AI to explain what happened, what evidence supports it, what is missing, and which relationships should carry forward without posting Dr/Cr yet.
---

# Accounting Relationship Reasoning

## Purpose

Use this skill to build an evidence-first relationship register before any TB bridge or CoA posting work.

The client files are evidence. This skill is accounting judgement guidance only. Never treat training material or skill text as client evidence.

## Investigation Order

1. Start from the prior-year financial statements when available.
2. Identify opening balance-sheet rows and prior-year P&L comparatives.
3. For each material opening balance-sheet row, ask what FY movement explains the change.
4. Then run source-first checks for documents that imply cash, receivables, payables, valuation, income, expenses, or exclusions.
5. Then run bank-first checks for remaining cash movements.
6. Finish with leftovers: source-only, bank-only, wrong-entity/personal, out-of-period, and unresolved traces.

## Relationship Types

Create accountant-useful relationships, not journal entries:

- Source plus bank match: source document and bank statement agree on amount/date/counterparty or a reasonable timing lag.
- Bank-only classification: bank movement has a likely meaning but no external support is attached.
- Source-only balance or accrual: source supports an entitlement, payable, valuation, or disclosure but no cash match is found.
- Bank roll-forward: bank statement chain explains opening to closing cash movement.
- Investment sale roll-forward: prior carrying value plus cash proceeds explains disposal and gain/loss.
- Distribution receivable roll-forward: opening debtor plus source entitlement less receipts explains closing debtor or residual.
- Loan or transfer bridge: internal/related-party transfers appear plausible but may need receiving account support.
- Opening balance support: prior FS line provides the opening point.
- Entity exclusion: evidence belongs to a person, wrong entity, or non-accounting item.
- Unresolved trace: material item remains unexplained.

## Accounting Principles

- Distinguish book movements from tax-only components.
- Treat franking credits, TFN withholding, tax offsets, and tax gross-ups as notes unless a book receivable/payable treatment is explicitly supported.
- Treat NAV, market value, and valuation-only information as notes unless fair value accounting is explicitly adopted.
- Do not treat a prior-year P&L comparative as an opening balance.
- For accruals and prepayments, consider the service period and year-end cut-off.
- For investment sales, separate cash proceeds, carrying value cleared, and gain/loss.
- For distributions, distinguish investment distributions received from beneficiary/UPE profit distributions.
- If source instructions name a bank/payee/reference, search that path before same-amount alternatives.

## Reference Library

For technical accounting topics, use the `accounting-pdf-knowledge-retrieval` skill:

- Check its `references/pdf-topic-map.json` to find the right CA knowhow PDF section.
- Run its retrieval script to read original PDF snippets on demand.
- Use the book guidance to decide what client evidence to inspect and how to frame judgement.
- Do not cite the book as evidence in the relationship register.

## Output Style

- Tell the story in short accountant language.
- Include useful arithmetic when it changes the conclusion.
- State what is missing without asking the junior accountant to approve every row.
- Use “needs attention” for plausible but unproven paths.
- Do not output Dr/Cr, journals, or final movement column names; Step 4 owns posting structure.
