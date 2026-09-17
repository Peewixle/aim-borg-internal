#!/usr/bin/env python3
"""
Load an AIM UDBR export into the SQLite store.

Idempotent by file content: the same bytes cannot be loaded twice, because
snapshot has UNIQUE (source_file, sha256). Re-running is safe.

Nothing is deduplicated, collapsed or corrected on load. Duplicate rules are a
FINDING, not noise — silently removing them on ingest would hide a real defect
and make the dump unreconcilable against AIM.
"""
import udbr_paths as P
import sys, os, re, sqlite3, hashlib, datetime
import pandas as pd

DB     = sys.argv[2] if len(sys.argv) > 2 else P.DB
SRC    = sys.argv[1] if len(sys.argv) > 1 else '/mnt/user-data/uploads/UDBR_3.xlsx'
SCHEMA = P.SCHEMA

KIND = {'Profile Mapping Rule': 'mapping',
        'Profile Selection Rule': 'profile_selection',
        'Payer Selection Rule': 'payer_selection'}
UNCOND = '(always applies)'
OPS = ('is not one of', 'is one of', 'contains any of', 'contains all',
       'starts with', 'does not equal', 'equals',
       'greater than or equal to', 'less than or equal to',
       'greater than', 'less than', 'is not', 'is')

def yn(v):
    return 1 if str(v).strip().lower() in ('yes', 'y', 'true', '1') else 0

def parse_conditions(text):
    """Split a condition expression into clauses and parse each.

    Returns [(conjunction, source, field, operator, values, parse_ok)].
    A failed parse is RECORDED, not discarded: an unparsed clause that vanished
    would make a conditional rule look unconditional, which inverts every
    shadowing and reachability check.
    """
    t = (text or '').strip()
    if not t or t == UNCOND:
        return []
    parts = re.split(r'\s+(AND|OR)\s+', t)
    out, conj = [], None
    for i, part in enumerate(parts):
        if part in ('AND', 'OR'):
            conj = part
            continue
        p = part.strip()
        src = None
        m = re.match(r'^(PCR|Bill)\s+(.*)$', p)
        if m:
            src, p = m.group(1), m.group(2)
        op = next((o for o in OPS if f' {o} ' in f' {p} '), None)
        if not op:
            out.append((conj, src, p, '?', [], 0))
            conj = None
            continue
        field, _, rest = p.partition(op)
        rest = rest.strip().strip('[]')
        vals = [v.strip() for v in rest.split(',') if v.strip()] if rest else []
        out.append((conj, src, field.strip(), op, vals, 1))
        conj = None
    return out

# ---------------------------------------------------------------- ingest
df = pd.read_excel(SRC, dtype=str).fillna('')
# Validate the shape before anything reads it. AIM's export has changed once
# already; a rename should stop the load, not surface three checks deep.
from udbr_domain import validate_dump
_shape = validate_dump(df, os.path.basename(SRC))
if _shape['unexpected']:
    print(f"  note: {len(_shape['unexpected'])} column(s) not used by any check: "
          f"{', '.join(_shape['unexpected'][:6])}")
sha = hashlib.sha256(open(SRC, 'rb').read()).hexdigest()
exported = datetime.datetime.fromtimestamp(os.path.getmtime(SRC)).date().isoformat()

fresh = not os.path.exists(DB)
cx = sqlite3.connect(DB)
cx.execute('PRAGMA foreign_keys = ON')
if fresh:
    cx.executescript(open(SCHEMA).read())
    print(f'created {DB}')

cur = cx.cursor()
row = cur.execute('SELECT snapshot_id FROM snapshot WHERE source_file=? AND sha256=?',
                  (os.path.basename(SRC), sha)).fetchone()
if row:
    # Reconcile the EXISTING snapshot rather than exiting silently. A stored
    # snapshot can be short - a lossy load before this check existed, or rows
    # deleted since - and skipping the check on the common path means the
    # verification only ever runs on first load.
    print(f'already loaded as snapshot {row[0]}')
    cx.close()
    from reconcile import reconcile
    reconcile(DB, row[0],
              set(df[df.Section != 'Profile'].RuleId),
              set(df[df.Section == 'Profile'].ProfileId),
              os.path.basename(SRC))
    sys.exit(0)

SRC_RULES = int((df.Section != 'Profile').sum())
SRC_PROFS = int((df.Section == 'Profile').sum())
cur.execute('''INSERT INTO snapshot (kind, source_file, exported_at,
                                     src_rows, src_rules, src_profiles, row_count, sha256)
               VALUES ('production',?,?,?,?,?,?,?)''',
            (os.path.basename(SRC), exported, len(df), SRC_RULES, SRC_PROFS, len(df), sha))
snap = cur.lastrowid

# --- org units. Built by the shared OrgTree so both loaders produce the same
# shape; duplicated tree-building can yield trees that differ in parentage
# while both loading without error.
from udbr_orgtree import OrgTree
tree = OrgTree(cur, snap).build_from_rows(df.itertuples())
sys_id = tree.system_id

# --- profiles. Keyed on ProfileId; name is never an identifier. ---
prof = df[df.Section == 'Profile']
pid = {}
for r in prof.itertuples():
    owner = tree.owner_of(r.Organization, r.Customer, r.Agency)
    cur.execute('''INSERT INTO profile
        (snapshot_id,profile_guid,name,description,group_type,tier,org_unit_id,payers,
         has_group_localization,parent_profile_guid,parent_tier)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (snap, r.ProfileId, r.ProfileName, r.ProfileDescription or None, r.GroupType,
         r.Tier, owner, r.ProfilePayers or None, yn(r.HasGroupLocalization),
         r.ParentProfileId or None, r.ParentProfileTier or None))
    pid[r.ProfileId] = cur.lastrowid

# --- rules. Two attachment paths: mapping -> profile, selection -> org unit. ---
nrule = ncond = 0
unattached = []
for r in df[df.Section != 'Profile'].itertuples():
    kind = KIND[r.Section]
    prow = pid.get(r.ProfileId) if kind == 'mapping' else None
    if kind == 'mapping' and prow is None:
        unattached.append(r.RuleId)
        continue
    orow = None
    if kind != 'mapping':
        orow = tree.owner_of(r.Organization, r.Customer, r.Agency)
    cond = (r.Conditions or '').strip()
    cur.execute('''INSERT INTO rule
        (snapshot_id,rule_guid,rule_kind,profile_row_id,org_unit_id,tier,tab,target_field,
         target_field_source,target_field_value_type,priority,description,conditions_raw,
         is_unconditional,outcome_type,outcome_value,outcome_raw,resulting_profile_name,
         payer_name,payer_form_type,payer_timing,is_rule_localization)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (snap, r.RuleId, kind, prow, orow, r.Tier, r.Tab or None, r.TargetField,
         r.TargetFieldSource or None, r.TargetFieldValueType or None,
         int(r.Priority) if str(r.Priority).strip().isdigit() else None,
         r.RuleDescription or None, cond or None,
         1 if (not cond or cond == UNCOND) else 0,
         r.OutcomeType or None, r.OutcomeValue or None, r.OutcomeRawValue or None,
         r.ResultingProfile or None, r.PayerName or None,
         r.PayerFormType or None, r.PayerTiming or None,
         yn(r.IsRuleLocalization)))
    rrow = cur.lastrowid
    nrule += 1
    for ix, (conj, src, fld, op, vals, ok) in enumerate(parse_conditions(cond)):
        cur.execute('''INSERT INTO rule_condition
            (rule_row_id,clause_ix,conjunction,source,field,operator,parse_ok)
            VALUES (?,?,?,?,?,?,?)''', (rrow, ix, conj, src, fld, op, ok))
        cid = cur.lastrowid
        ncond += 1
        for vix, v in enumerate(vals):
            cur.execute('INSERT INTO rule_condition_value (condition_id,value_ix,value) VALUES (?,?,?)',
                        (cid, vix, v))

cx.commit()
print(f'snapshot {snap}: {len(prof)} profiles, {nrule} rules, {ncond} condition clauses')
if unattached:
    print(f'  WARNING: {len(unattached)} mapping rules had an unresolvable ProfileId and were skipped')

bad = cur.execute('SELECT COUNT(*) FROM rule_condition WHERE parse_ok=0').fetchone()[0]
print(f'  condition clauses that failed to parse: {bad}')
cx.close()

# Reconcile before declaring success. A load that silently drops rows makes
# every derived count - migration totals, findings, the Grid - internally
# consistent and wrong.
from reconcile import reconcile
reconcile(DB, snap,
          set(df[df.Section != 'Profile'].RuleId),
          set(df[df.Section == 'Profile'].ProfileId),
          os.path.basename(SRC))
