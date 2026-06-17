# Local data layout

Sensitive client/source files are kept out of git and should live under the local ignored folder:

```text
/Users/ameliekong/Documents/Projects/Agentic-accountant-copilot/inputs
```

For this internal project, this folder is the working source-data path. It is populated from the original raw files previously held at:

```text
/Users/ameliekong/Documents/Projects/financial_statement_automation/inputs
```

Do not use prior pipeline `outputs/` as the default source of truth for new internal workflow runs. Use prior outputs only for comparison, regression checks, or temporary bridge imports when raw intake support is not yet available.

Current local inventory after copy:

- PDFs: 46
- PNGs: 1
- Markdown convention file: 1
- CSV support file: 1

The `inputs/` folder is intentionally ignored by `.gitignore` to avoid committing client data.
