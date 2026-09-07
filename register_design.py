#!/usr/bin/env python3
"""
Register an existing design snapshot as a PDS design session.

WHY THIS IS NOT REVERSE ENGINEERING
===================================
v11 is already a design snapshot: kind='design', derived_from pointing at
UDBR_3, 498 rules sitting at their designed tiers. Everything a design IS, it
already has — the snapshot was never the missing part.

What is missing is the design_session row: the wrapper carrying a name, an
author, a state and the Read Me text. One insert. Nothing has to be recovered
because nothing was lost.

Once it exists, v11 behaves like any studio design. The change set derives
against its base (364 promotions, 110 demotions, 22 deletions — the figures
already verified against the generated script), the checks run on it, and the
workbook exports.

ATTRIBUTION IS THE PART THAT MATTERS
====================================
The Read Me records who did what, because the file travels to a team that will
read it as a proposal and needs to see its own decisions in it:

  * 395 promotions to System were made BY HAND in v9 by the RCM team. That is
    the design work — deciding what is shareable — and no script did it.
  * The engine then applied 133 mechanical corrections on top: config-resolved
    destinations to Agency, redundant Customer rules removed, MBI/Policy Number
    held where production has it.
  * 23 rules v10 had deleted as duplicates were restored. They were never
    duplicates: Emergency exists on both the ANSI 5010 and HCFA 1500 tabs, and
    a key without the tab merged two destinations. That correction is why v11
    exists.

Written as one paragraph rather than a credit list, but the distinction between
judgement and consistency pass stays visible.
"""
import sys, sqlite3

sys.path.insert(0, '/home/claude/udbr')
sys.path.insert(0, '/home/claude/work/pds')

from udbr_design import DesignStore

README = """This design was authored by the RCM team, not generated.

The 364 promotions to System originate in UDBR_Profile_Design_v9, where the \
team moved rules between level bands by hand. That is the design decision — \
which rules are common enough to share — and no script made it.

The engine then applied a consistency pass over that work: every \
config-resolved destination moved to Agency (baserate, mileage, other rate, \
site code, remarks), because those values resolve against each agency's own \
AIM configuration and cannot be shared. Redundant Customer rules that set a \
value System already sets were removed. MBI/Policy Number was held at \
Organization where production holds it, and moved to Agency where production \
has it at Customer — it is a general-purpose display bucket on the patient \
invoice, so its values are agency wording rather than a code lookup.

Twenty-three rules that v10 removed as duplicates were restored. They were \
never duplicates. A destination billing field is identified by tab and field \
together, and Emergency exists on both the ANSI 5010 and the HCFA 1500 tab: \
two different places on a claim that share a name. A key without the tab \
merged them, so every profile setting both looked like it held a duplicate. \
Keyed correctly the estate has none. That correction is why this version \
exists and supersedes v10.

Four items are deliberately left open and are listed in the pre-execution \
issues note: the nine Primary Diagnosis Code priority collisions, the tier of \
Additional Claim Information (19), the Marietta site-code ordering, and the \
per-agency scope declarations that AIM cannot yet express."""


def register(db, snapshot_id, name, username, readme=README, state='open'):
    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row
    ds = DesignStore(db)

    snap = cx.execute("SELECT * FROM snapshot WHERE snapshot_id=? AND kind='design'",
                      (snapshot_id,)).fetchone()
    if snap is None:
        raise SystemExit(f'snapshot {snapshot_id} is not a design snapshot')
    if snap['derived_from'] is None:
        raise SystemExit('the design names no base dump, so no change set can be '
                         'derived from it')

    existing = cx.execute('SELECT design_id FROM design_session WHERE snapshot_id=?',
                          (snapshot_id,)).fetchone()
    if existing:
        print(f'already registered as design {existing["design_id"]}')
        cx.close()
        return existing['design_id']

    cur = cx.cursor()
    cur.execute("""INSERT INTO design_session
        (snapshot_id, base_snapshot, name, created_by, state, readme)
        VALUES (?,?,?,?,?,?)""",
        (snapshot_id, snap['derived_from'], name, username, state, readme))
    did = cur.lastrowid
    cx.commit()
    cx.close()

    s = ds.summary(did)
    print(f'registered {snap["source_file"]} as design {did} ({state})')
    print(f'  base: snapshot {snap["derived_from"]}')
    print(f'  change set: ' + ', '.join(f'{v} {k}' for k, v in sorted(s['counts'].items())))
    print(f'  total actions: {s["total"]}')
    return did


if __name__ == '__main__':
    db = sys.argv[1] if len(sys.argv) > 1 else '/home/claude/udbr/udbr.db'
    snap = int(sys.argv[2]) if len(sys.argv) > 2 else None
    if snap is None:
        cx = sqlite3.connect(db)
        rows = cx.execute("""SELECT snapshot_id, source_file FROM snapshot
                             WHERE kind='design' ORDER BY loaded_at DESC""").fetchall()
        cx.close()
        if not rows:
            raise SystemExit('no design snapshot in the store')
        snap = rows[0][0]
        print(f'using the newest design snapshot: {snap} ({rows[0][1]})')
    name = sys.argv[3] if len(sys.argv) > 3 else 'Standard convergence (v11)'
    register(db, snap, name, 'Michael McIntyre')
