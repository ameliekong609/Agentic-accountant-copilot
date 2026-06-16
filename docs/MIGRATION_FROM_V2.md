# Migration From V2

## Relationship to V2

V2 is not the product foundation. It is a reference library for useful controls and deterministic tools.

## Useful V2 ideas to reuse

- Source-quote contract.
- Pydantic-style schemas.
- Bank reconciliation checks.
- Match-first/map-last approach.
- Journal and workbook renderers.
- Verifier patterns.
- Known limitations and trust-specific lessons.

## What not to copy directly

- Fixed step-by-step product UX.
- Hidden dependency on cached step files.
- Any flow where later clean outputs can hide unresolved upstream findings.
- Hardcoded trust/account assumptions unless moved into explicit preferences or entity-type conventions.

## Migration pattern

1. Define clean domain model in this repo.
2. Wrap one V2 capability as a tool.
3. Convert its findings into central exceptions.
4. Add tests and readiness gate behavior.
5. Only then expose it through orchestrator/UI.
