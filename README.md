# Tenet Legacy Financial Statement Copilot

This repository contains the local financial statement preparation workflow for Tessa, the AI digital accountant used to turn a client document pack into an accountant-style Excel workpaper.

The current product is intentionally focused:

- read an uploaded client folder or zip;
- build an evidence index with practical display names;
- reason through accounting relationships from prior-year financial statements, source documents and bank statements;
- prepare a TB bridge workbook with movement notes and evidence links; and
- run a senior-review pass that can request bounded corrections without blocking on ordinary accountant judgement notes.

It is not the earlier stateful approval-queue prototype. The active code path is the financial statement workbook workflow only.

## Start The Local Portal

```bash
PYTHONPATH=src .venv/bin/python -m accountant_copilot.cli serve-workpaper-portal
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787), upload a client pack, specify the target financial year if needed, and prepare the workbook.

## Run From CLI

```bash
PYTHONPATH=src .venv/bin/python -m accountant_copilot.cli prepare-workpaper \
  --client-folder inputs \
  --fy-start 2024-07-01 \
  --fy-end 2025-06-30
```

Useful stage commands for debugging:

```bash
PYTHONPATH=src .venv/bin/python -m accountant_copilot.cli process-documents --input-dir inputs
PYTHONPATH=src .venv/bin/python -m accountant_copilot.cli match-source-facts
PYTHONPATH=src .venv/bin/python -m accountant_copilot.cli build-tb-bridge-workpaper
PYTHONPATH=src .venv/bin/python -m accountant_copilot.cli review-workpaper
```

## Outputs

Generated files are local and ignored by git:

- `outputs/raw_inputs_pdf_extraction/source_document_index.json`
- `outputs/raw_inputs_pdf_extraction/relationship_reasoning_register.json`
- `outputs/step4_tb_bridge_workpaper/tb_bridge_workpaper.json`
- `outputs/step4_tb_bridge_workpaper/step4_tb_bridge_workpaper.xlsx`
- `outputs/step4_tb_bridge_workpaper/turing_senior_review.json`
- `outputs/step4_tb_bridge_workpaper/prepared_workpaper_summary.md`

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest
```

The tests now cover the current financial statement workflow, portal status recovery, workbook quality checks, source indexing, relationship reasoning, TB bridge generation, and senior review correction behavior.
