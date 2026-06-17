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
  tools/             Deterministic tools and source pipeline adapters
  state/             Engagement state, exceptions, decisions, preferences
```

## CLI MVP

Inspect an engagement state and see readiness, blockers, approvals needed, and the recommended next task:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli inspect-engagement \
  --state examples/sample_engagement_state.json
```

Machine-readable output:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli inspect-engagement \
  --state examples/sample_engagement_state.json \
  --json
```

Export a markdown audit trail of readiness, exceptions, evidence, and accountant decisions:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli export-audit-trail \
  --state outputs/engagement_state.json \
  --output outputs/audit_trail.md
```

Exit code policy:

- `0` — final output is allowed by current readiness gate.
- `1` — final output is blocked by open critical/high exceptions or missing approvals.

Review open exceptions and record accountant decisions:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli review-exceptions \
  --state outputs/engagement_state.json
```

Resolve or accept an exception with an approved accountant decision:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli review-exceptions \
  --state outputs/engagement_state.json \
  --exception-id exc_high_bank \
  --action resolved \
  --rationale "Matched to approved supporting evidence." \
  --approved-by "Reviewer Name"
```

Import source pipeline control issues into a new engagement state exception queue:

```bash
PYTHONPATH=src python3.11 -m accountant_copilot.cli import-source-exceptions \
  --matching /path/to/matching.json \
  --journal /path/to/journal.json \
  --output outputs/engagement_state.json \
  --engagement-id xyz_fy2025 \
  --entity-name "XYZ Australia Financial Trust" \
  --entity-type discretionary_trust \
  --fy-start 2024-07-01 \
  --fy-end 2025-06-30
```

## Development

Run tests from the repo root:

```bash
PYTHONPATH=src python3.11 -m pytest -q
```

Compile check:

```bash
python3.11 -m compileall -q src tests
```

If using an installed local venv:

```bash
python -m pip install -e '.[dev]'
pytest
```

## Design docs

- `docs/PRODUCT_VISION.md`
- `docs/ARCHITECTURE.md`
- `docs/AGENT_ROLES.md`
- `docs/CONTROLS_AND_POLICIES.md`
