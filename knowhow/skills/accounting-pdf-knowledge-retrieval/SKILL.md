---
name: accounting-pdf-knowledge-retrieval
description: Use when Tessa/Codex needs accounting knowledge from the local CA knowhow PDFs while preparing Step 3 relationship reasoning, Step 4 TB bridge workpapers, or senior review. Provides a PDF topic map and an on-demand retrieval script so the agent can consult the original book sections for guidance without treating them as client evidence.
---

# Accounting PDF Knowledge Retrieval

## Purpose

Use this skill as Tessa's accounting book shelf.

The PDF materials are accounting guidance only. They are not client evidence and must not be cited as support for a client amount, date, account, counterparty, or posting.

## Workflow

1. Notice a technical accounting topic in the client pack, prior-year FS, relationship register, TB bridge, or senior review finding.
2. Open `references/pdf-topic-map.json` to find the relevant PDF, section, page range, and search terms.
3. Run `scripts/retrieve_pdf_topic.py` for the topic or query.
4. Use the returned PDF snippets to guide what to check in the client files.
5. Support the workbook using uploaded client documents, prior-year FS, bank statements, source page quotes, and arithmetic from client evidence.

## Retrieval Commands

List available topics:

```bash
PYTHONPATH=src .venv/bin/python knowhow/skills/accounting-pdf-knowledge-retrieval/scripts/retrieve_pdf_topic.py --list-topics
```

Retrieve a mapped topic:

```bash
PYTHONPATH=src .venv/bin/python knowhow/skills/accounting-pdf-knowledge-retrieval/scripts/retrieve_pdf_topic.py --topic foreign_currency_fx
```

Retrieve by query when no topic is obvious:

```bash
PYTHONPATH=src .venv/bin/python knowhow/skills/accounting-pdf-knowledge-retrieval/scripts/retrieve_pdf_topic.py --query "USD bank account year end FX"
```

## When To Consult The Books

Consult the PDF map when client evidence indicates:

- foreign currency, FX, overseas bank accounts, foreign loans, or foreign investments
- revenue cut-off, deferred income, receivables, WIP, or contract revenue
- inventory, COGS, stocktake, working capital, or trading-business movements
- income tax, deferred tax, GST/BAS, withholding, or tax/book boundary issues
- fair value, NAV, market value, impairment, PPE, intangibles, or investment carrying values
- leases, provisions, contingencies, financial instruments, hedging, debt, equity, funding, or consolidation topics

## Source-Of-Truth Rule

The retrieved PDF section can shape judgement, but it cannot prove the client fact.

Good:

- "The PDF guidance suggests checking whether this is monetary or non-monetary; client bank statement and prior FS determine the actual movement."

Bad:

- "Post the amount because FIN121 says so."
- "Use knowhow/fin121_csg.pdf as evidence."
- "Put the CA guide in source_documents_checked."
