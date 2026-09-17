#!/usr/bin/env python3
"""
Load a design workbook (UDBR_Profile_Design_vN.xlsx) into the store as a
'design' snapshot.

WHY A SEPARATE LOADER
=====================
A design workbook is not a dump. Its shape is one sheet per profile with rules
grouped into bands by the AOH level the design places them at, rather than a
flat export with a Section column. So the parse differs entirely, even though
the rows land in the same tables.

WHY A SEPARATE SNAPSHOT KIND
============================
A production snapshot records what AIM held. A design records what someone
proposes it should hold, and has NOT been applied. Both belong in the history —
UDBR_2, UDBR_3, then the v10 design is the actual trajectory — but a diff that
cannot tell them apart would report design decisions as production changes, and
the reverification cycle exists precisely to say what the RCM team fixed.

WHAT MAKES THE DIFF WORK
========================
Every design rule carries the Rule Id of the production rule it came from, and
those match one-for-one. So the same rule_guid appears in both snapshots at
different tiers, and the migration is derivable by SQL rather than by a
separate script:

    production tier -> design tier, per rule = promote / demote / unchanged
    in production, absent from design         = delete
"""
import udbr_paths as P
from udbr_domain import TIERS, TIER_RANK, tier_rank
import sys, os, json, sqlite3, hashlib, datetime, subprocess

SRC  = sys.argv[1] if len(sys.argv) > 1 else '/mnt/user-data/uploads/UDBR_Profile_Design_v10.xlsx'
DB   = sys.argv[2] if len(sys.argv) > 2 else P.DB
BASE = sys.argv[3] if len(sys.argv) > 3 else 'UDBR_3.xlsx'   # the dump it derives from

# Reuse the existing design parser rather than reimplementing the band walk.
subprocess.run(['python3', os.path.join(P.ROOT,'parse_design.py'), SRC],
               check=True, capture_output=True)
design = json.load(open('/tmp/design.json'))
RULES = design['rules']
PROFS = design['profiles']

sha = hashlib.sha256(open(SRC, 'rb').read()).hexdigest()
exported = datetime.datetime.fromtimestamp(os.path.getmtime(SRC)).date().isoformat()

cx = sqlite3.connect(DB)
cx.execute('PRAGMA foreign_keys = ON')
cur = cx.cursor()

row = cur.execute('SELECT snapshot_id FROM snapshot WHERE source_file=? AND sha256=?',
                  (os.path.basename(SRC), sha)).fetchone()
if row:
    print(f'already loaded as snapshot {row[0]}')
    from reconcile import reconcile
    reconcile(DB, row[0], {r['rid'] for r in RULES}, None, os.path.basename(SRC))
    sys.exit(0)

base = cur.execute('''SELECT snapshot_id FROM snapshot
                      WHERE source_file=? AND kind='production'
                      ORDER BY loaded_at DESC LIMIT 1''', (BASE,)).fetchone()
if base is None:
    raise SystemExit(f'base dump {BASE!r} is not loaded — load it first, or the design '
                     f'has nothing to diff against')
base_id = base[0]

# Source counts taken from the PARSE, then re-derived from the workbook itself
# below, so a parser that silently drops rows cannot also define the target.
SRC_RULES = len(RULES)
SRC_PROFS = len({r['sheet'] for r in RULES})
cur.execute('''INSERT INTO snapshot (kind, derived_from, source_file, exported_at,
                                     src_rows, src_rules, src_profiles, row_count, sha256, notes)
               VALUES ('design',?,?,?,?,?,?,?,?,?)''',
            (base_id, os.path.basename(SRC), exported,
             SRC_RULES, SRC_RULES, SRC_PROFS, SRC_RULES, sha,
             f'design workbook, derived from {BASE}; mapping rules only, '
             f'no profile or payer selection rules'))
snap = cur.lastrowid

# --- org units. Mirror the base snapshot's tree rather than re-deriving it:
# a design describes the same estate as the dump it comes from, and the design
# workbook carries no Agency column to derive parentage from.
from udbr_orgtree import OrgTree
tree = OrgTree(cur, snap).mirror(cur, base_id)
sys_id = tree.system_id
ou = tree._seen

TEST = {p['sheet'] for p in PROFS if p['complete'] != 'Y'}

# --- profiles.
#
# ONE DESIGN PROFILE PER PRODUCTION PROFILE, not per sheet.
#
# A sheet is not a profile. The Default Profile sheet holds the rules of SIX
# production profiles - System, Organization, and a Customer and Agency profile
# for each alpha - because the design presents them together for editing.
#
# Taking the first rule's profile and applying it to the whole sheet collapsed
# all of them into one, and attributed 40 West County rules to Azalea. Row
# counts reconciled perfectly while the attribution was wrong, which is the
# failure mode reconciliation is supposed to prevent.
#
# Identity therefore comes from the PRODUCTION profile each rule belongs to,
# resolved by Rule Id. That is exact, not inferred.
ORDER = TIERS
prof_row = {}          # production profile_guid -> design profile_row_id
rule_owner = {}        # rule_guid -> production profile_guid

for r in RULES:
    got = cur.execute("""SELECT pr.profile_guid FROM rule ru
                         JOIN profile pr ON pr.profile_row_id = ru.profile_row_id
                         WHERE ru.snapshot_id=? AND ru.rule_guid=?""",
                      (base_id, r['rid'])).fetchone()
    if got:
        rule_owner[r['rid']] = got[0]

for pguid in sorted(set(rule_owner.values())):
    mine = [r for r in RULES if rule_owner.get(r['rid']) == pguid]
    meta = cur.execute("""SELECT pr.name, pr.group_type, pr.tier, ou2.tier, ou2.name
                          FROM profile pr JOIN org_unit ou2 ON ou2.org_unit_id=pr.org_unit_id
                          WHERE pr.snapshot_id=? AND pr.profile_guid=?""",
                       (base_id, pguid)).fetchone()
    pname, gtype, ptier, otier, oname = meta
    owner = ou.get((otier, oname)) or sys_id
    # The profile's tier is the highest level its designed rules reach.
    dtier = min((r['level'] for r in mine if r['level']), key=ORDER.index, default=ptier)
    cur.execute("""INSERT INTO profile
        (snapshot_id,profile_guid,name,description,group_type,tier,org_unit_id,payers,
         has_group_localization,parent_profile_guid,parent_tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (snap, pguid, pname, None, gtype, dtier, owner, None, 0, None, None))
    prof_row[pguid] = cur.lastrowid

# --- rules. tier is the DESIGNED level, which is the whole point. ---
n = skipped = 0
for r in RULES:
    # Test-profile rules are INCLUDED, at whatever level the design leaves them.
    # Skipping them made them look deleted in the diff - 52 deletions instead of
    # 45 - when the design simply does not touch them. "Excluded from the design"
    # and "removed from AIM" are different facts and the diff must not conflate
    # them.
    prow = prof_row.get(rule_owner.get(r['rid']))
    if prow is None:
        skipped += 1
        continue
    cond = (r['cond'] or '').strip()
    cur.execute('''INSERT INTO rule
        (snapshot_id,rule_guid,rule_kind,profile_row_id,org_unit_id,tier,tab,target_field,
         priority,description,conditions_raw,is_unconditional,outcome_type,outcome_value,
         is_rule_localization)
        VALUES (?,?,'mapping',?,NULL,?,?,?,?,?,?,?,?,?,0)''',
        (snap, r['rid'], prow, r['level'], r['tab'] or None, r['field'],
         int(r['priority']) if str(r['priority']).isdigit() else None,
         r['desc'] or None, cond or None,
         1 if (not cond or cond == '(always applies)') else 0,
         r['otype'] or None, r['ovalue'] or None))
    n += 1

cx.commit()
print(f'snapshot {snap} (design, derived from snapshot {base_id} / {BASE})')
print(f'  {len(prof_row)} profiles, {n} rules loaded, {skipped} skipped (unmatched)')
lv = cur.execute('''SELECT tier, COUNT(*) FROM rule WHERE snapshot_id=?
                    GROUP BY tier ORDER BY 2 DESC''', (snap,)).fetchall()
print('  by designed level:', ', '.join(f'{t} {c}' for t, c in lv))
cx.close()

# Independent count straight from the workbook, not from the parser, so a
# parser bug cannot define its own target and pass.
from openpyxl import load_workbook as _lw
_wb = _lw(SRC, read_only=True)
_n = 0
for _sh in _wb.sheetnames:
    if _sh in ('Summary', 'Changes from v9'):
        continue
    _rows = list(_wb[_sh].iter_rows(values_only=True))
    _h = next((i for i, r in enumerate(_rows)
               if 'Target Field' in [str(x).strip() if x is not None else '' for x in r]
               and 'Priority' in [str(x).strip() if x is not None else '' for x in r]), None)
    if _h is None:
        continue
    _ix = [str(x).strip() if x is not None else '' for x in _rows[_h]].index('Target Field')
    for _r in _rows[_h+1:]:
        _v = str(_r[_ix]).strip() if len(_r) > _ix and _r[_ix] is not None else ''
        if _v and _v != 'Target Field':
            _n += 1
if _n != SRC_RULES:
    raise SystemExit(f'PARSER LOST ROWS: workbook has {_n} rule rows, parser produced '
                     f'{SRC_RULES}. The snapshot is incomplete.')
print(f'  workbook independently counted: {_n} rule rows == parsed')

from reconcile import reconcile
reconcile(DB, snap, {r['rid'] for r in RULES},
          set(prof_row.keys()), os.path.basename(SRC))

# ATTRIBUTION CHECK.
#
# Row counts reconciled perfectly while 40 West County rules sat under Azalea's
# profile, because counting rows says nothing about WHERE they landed. Every
# rule must hang off the same profile it hangs off in production.
_cx = sqlite3.connect(DB)
_bad = _cx.execute('''
  SELECT COUNT(*) FROM rule d
  JOIN profile dp ON dp.profile_row_id = d.profile_row_id
  JOIN rule p  ON p.rule_guid = d.rule_guid AND p.snapshot_id = ?
  JOIN profile pp ON pp.profile_row_id = p.profile_row_id
  WHERE d.snapshot_id = ? AND dp.profile_guid <> pp.profile_guid
''', (base_id, snap)).fetchone()[0]
_orphan = _cx.execute('''
  SELECT COUNT(*) FROM rule d WHERE d.snapshot_id=? AND NOT EXISTS
    (SELECT 1 FROM rule p WHERE p.rule_guid=d.rule_guid AND p.snapshot_id=?)
''', (snap, base_id)).fetchone()[0]
_cx.close()
if _bad or _orphan:
    raise SystemExit(f'ATTRIBUTION FAILED: {_bad} rules attached to a different '
                     f'profile than in production, {_orphan} with no production '
                     f'counterpart. The snapshot is unusable.')
print(f'  attribution verified: every rule sits under the same profile as in '
      f'production ({len(prof_row)} profiles)')
