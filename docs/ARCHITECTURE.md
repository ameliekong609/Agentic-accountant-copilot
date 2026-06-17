# Architecture

## Principle

```text
Agentic planning + deterministic controls + accountant approval + preference memory
```

## Main layers

1. **Engagement State** — central state for documents, CoA, transactions, exceptions, decisions, and preferences.
2. **Orchestrator** — plans next work, builds task graph, applies readiness gates.
3. **Specialist Agents** — document, CoA, bank, matching, journal, tax/distribution, preference, reviewer, Codex investigation.
4. **Tool Layer** — deterministic extraction, matching, journal, verifier, and workbook tools.
5. **Controls** — source evidence, exception queue, approval records, release readiness gate.

## Clean-slate decision

This repository is the product foundation for the accountant copilot workflow. Product language should stay focused on source documents, controls, exceptions, accountant decisions, and final readiness.
