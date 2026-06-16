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
  tools/             Deterministic tools and legacy/V2 adapters
  state/             Engagement state, exceptions, decisions, preferences
```

## Development

Run tests from the repo root:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

If using pytest in a local venv:

```bash
python -m pip install -e '.[dev]'
pytest
```

## Design docs

- `docs/PRODUCT_VISION.md`
- `docs/ARCHITECTURE.md`
- `docs/AGENT_ROLES.md`
- `docs/CONTROLS_AND_POLICIES.md`
- `docs/MIGRATION_FROM_V2.md`
