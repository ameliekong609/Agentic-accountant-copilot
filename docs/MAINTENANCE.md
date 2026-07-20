# Maintenance And Repo Hygiene

This repo should stay aligned to the current financial statement workflow.

## Keep

- `src/accountant_copilot/workpaper_portal.py` for the local portal.
- `src/accountant_copilot/cli.py` for the current CLI orchestration and stage commands.
- `src/accountant_copilot/tb_bridge_workflow.py` for relationship, TB bridge, workbook and review contracts.
- `knowhow/skills/**` for accounting knowhow and retrieval instructions.
- `scripts/*prepare_workpaper*`, `scripts/check_workpaper_quality.py` and engineer/Telegram watcher scripts.
- Tests that exercise source indexing, relationship reasoning, TB bridge output, senior review, quality checks and portal behavior.

## Remove Instead Of Archiving

Do not keep stale product surfaces as archive folders. If a feature is no longer part of the active workflow, delete its docs, tests and public CLI command.

Removed stale surfaces include:

- earlier stateful-prototype documentation and sample state;
- old specialist-role and release-gate documentation;
- old browser decision surfaces;
- old fact-extractor command tests for the previous Step 2/Step 3 split;
- old orchestrator, state, adapter, agent and tool placeholder packages.

## Guardrails

- `accountant-copilot --help` should list only the current product commands.
- Docs should describe only features that exist in the active workflow unless a new feature is rebuilt and tested.
- Judgement issues should be recorded as notes, not treated as technical workflow failures.
- Technical failures should write progress artifacts and invoke retry or engineering checks where possible.
