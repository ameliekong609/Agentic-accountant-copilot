# Current Financial Statement Workflow

The active product is a local, accountant-facing workflow. Tessa acts like a digital junior accountant and prepares an Excel financial statement workpaper from a client document pack.

## Workflow

1. **Upload client files**
   - The portal accepts a folder or zip.
   - The accountant can provide target FY start/end and a specific prior-year financial statement when needed.

2. **Build the evidence index**
   - AI reads every file and writes a source index.
   - This stage classifies documents, suggests clean display names, records entity relevance, captures short summaries and preserves page quotes for later investigation.
   - It does not extract final accounting facts for posting.

3. **Reason through accounting relationships**
   - AI investigates relationships from prior-year FS rows, source documents and bank movements.
   - The register explains what happened: source + bank, bank-only, source-only, excluded, unresolved and needs-attention items.
   - This is relationship reasoning, not debit/credit posting.

4. **Prepare the TB bridge workbook**
   - AI builds structured accounting movements from the relationship register.
   - A deterministic workbook builder turns the structured movements into Excel tabs.
   - Movement columns are selected from accounting role logic and client evidence, not a hardcoded client-specific list.

5. **Senior review and bounded correction**
   - A senior-review pass checks controls, evidence lineage, balance, presentation and judgement areas.
   - Technical failures can trigger retry/correction rounds.
   - Accountant judgement notes do not block workbook delivery; they are surfaced in the workbook and companion UI.

## Output Workbook

The workbook is designed for a junior accountant who wants a clear starting point, not a massive UI:

- `TB Bridge` shows prior-year opening balances, accountant-style movement columns and closing balances.
- `Movement Notes` explains the story behind each account row and key movement.
- `Evidence Index` maps original files to clean names and source links.

## Knowledge Use

Accounting knowhow lives in `knowhow/skills`. It guides judgement, role selection, review checks and reference retrieval. It is not client evidence. Client files and prior-year financial statements remain the source of truth.
