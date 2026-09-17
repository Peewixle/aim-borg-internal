# Loading a New Snapshot Without Losing the Design

Prepared by Michael McIntyre  |  September 17, 2026

**Version 1**

---

## What this is for

Adding a UDBR export — QA, a later production pull, anything — to the deployed
Borg **as an additional snapshot**, leaving design 1 (Standard convergence
(v11)) and its attributed history intact.

**Do not use `BORG_RESEED`.** That replaces the entire store with the seed in
the repo, which would destroy the design and the 539-row save history.

---

## Why appending is safe

Verified on a copy before proposing it, not inferred from reading the code.

A store was built with production, the v11 design workbook, a design session and
four saves including four promotions to System. A second dump was then loaded.
Every pre-existing table was compared before and after:

| | before | after | lost |
|---|---|---|---|
| `design_session` | 1 | 1 | 0 |
| `design_save` | 4 | 4 | 0 |
| design snapshot rules | 545 | 545 | 0 |
| production rules | 545 | 545 | 0 |
| snapshots 1–3 | 3 | 3 | 0 |
| `org_unit` | 18 | 24 | 0 |

`org_unit` is the only table that changed, and it only **grew** — six entities
the first dump did not contain. Everything else is byte-identical.

The mechanism: `udbr_load.py` applies the schema **only when the database file
does not exist**, the schema contains no `DROP` statements, and a new snapshot
is an `INSERT` that takes the next identifier. Nothing existing is addressed.

---

## Files to add to the repo

Four. The loader's other dependencies are already deployed and are
byte-identical to these copies, so nothing the server uses gets overwritten.

- `udbr_load.py` — loads a production-shaped UDBR export
- `reconcile.py` — verifies every source identifier arrived, none invented
- `udbr_load_design.py` — only if a QA export ever arrives as a design workbook
- `parse_design.py` — required by the above, invoked as a subprocess

Already present and unchanged: `udbr_domain.py`, `udbr_orgtree.py`,
`udbr_paths.py`, `udbr_schema.sql`.

`pandas` and `openpyxl` are already in `requirements.txt`, so there is no
dependency change.

---

## Procedure

**1. Export the UDBR from AIM QA** in the same shape as the production export —
one row per profile and per mapping rule, with `Section`, `Tab`, `TargetField`,
`Priority`, `Conditions`, `OutcomeType` and `OutcomeValue` present. The loader
validates this and fails loudly rather than loading something it half
understands.

**2. Get the file onto Render.** The shell cannot read a local machine. Commit
the xlsx to the repo, or fetch it from a URL in the shell.

**3. Back up the store first.**

```
cp /data/udbr.db /data/udbr.db.before-qa-$(date +%Y%m%d-%H%M)
```

Not because the load is expected to fail, but because the design is not
reproducible if it does.

**4. Load it.**

```
python3 udbr_load.py <the-qa-file>.xlsx /data/udbr.db
```

It prints the snapshot number, the profile and rule counts, and a reconciliation
line. **Read the reconciliation line.** It states how many source identifiers
arrived and whether any were invented; anything other than a clean match means
stop.

**5. Restart the service.** The payload is built at startup, so the new snapshot
appears in the picker after a restart, not before.

**6. Confirm the design survived** — open the browser, check the picker lists
the new snapshot, and check the History tab still shows design 1.

---

## What this does not get you

**A design against QA is possible; testing the bulk export is not.** BG-70
(Design Change Script Export) is specified but not built, so there is nothing
for the API calls to consume. It also carries an unresolved contradiction with
ADO 8213 about what the second call references in a multi-agency demotion, which
is a question for Sam Wheat.

So a QA snapshot gets a design that can be built and completed. The export step
at the end of it does not exist yet.

---

## One thing found along the way

**`udbr_schema.sql` in the project is stale.** It contains no design tables —
`design_session` and `design_save` are created by `udbr_design.py` at runtime.
Anyone building a store from that schema and expecting it to be complete will
find the design side missing. It is not a problem for this procedure, because
the deployed store already has those tables.
