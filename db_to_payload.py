#!/usr/bin/env python3
"""
Emit the Borg work-build payload from the SQLite store.

WHY THIS EXISTS
===============
The overlay used to be built by a pandas block inside refresh.sh that re-read
the .xlsx and reshaped it. That meant two independent readers of the same dump —
the analyzer and the payload builder — which is how findings and profile data
came to be generated from different dumps once, with the only symptom a marking
check quietly finding one flagged row instead of two.

With the store in place there is one reader. The database is the source of
truth; this payload is a derived view of one snapshot.

WHY THE PAYLOAD IS STILL JSON RATHER THAN THE DATABASE ITSELF
=============================================================
The work build is a single local HTML file opened over file://, where a browser
cannot fetch a .db — CORS blocks it. Embedding the database would mean
base64-ing it plus a SQLite WASM build into the overlay, roughly 2MB, to gain
ad-hoc SQL in the browser that nothing currently needs.

The database's value is the analysis and the run-to-run comparison, both of
which happen before the payload is built. So the Grid gets a derived view and
the querying stays where the query engine is.

If in-browser SQL is wanted later, this is the file to replace, not the schema.
"""
import udbr_paths as P
import sys, os, json, sqlite3
from udbr_domain import TIERS, TIER_RANK, tier_rank

DB   = sys.argv[1] if len(sys.argv) > 1 else P.DB
OUT  = sys.argv[2] if len(sys.argv) > 2 else P.PAYLOAD
# Third argument selects the snapshot. It may be a snapshot id OR a source
# filename. Filename is the safer form for scripted use: defaulting to the most
# recently LOADED snapshot emitted the wrong dump's payload whenever an older
# export was re-run, because load order and the requested dump are independent.
SEL  = sys.argv[3] if len(sys.argv) > 3 else None
# ALL snapshots are emitted, not just the selected one, so the Grid can switch
# between them at runtime. SEL chooses which one is CURRENT on open.
#
# One payload is ~98KB against a work build already at 1.4MB, so ten snapshots
# cost under a megabyte. That is far cheaper than embedding the database plus a
# SQLite WASM build (~2MB) to get querying nothing has asked for.

def build(SNAP, cx=None):
    cx = cx if cx is not None else globals()["cx"]
    snap = cx.execute('SELECT * FROM snapshot WHERE snapshot_id=?', (SNAP,)).fetchone()

    def entity(row):
        """A profile's owning entity names, resolved through the org tree. Agency is
        never on the rule row, so it can only come from here."""
        ou = cx.execute('SELECT * FROM org_unit WHERE org_unit_id=?', (row['org_unit_id'],)).fetchone()
        agency = ou['name'] if ou['tier'] == 'Agency' else ''
        if ou['tier'] == 'Agency':
            par = cx.execute('SELECT * FROM org_unit WHERE org_unit_id=?', (ou['parent_id'],)).fetchone()
            cust = par['name'] if par and par['tier'] == 'Customer' else ''
            org = ''
            if par and par['parent_id']:
                gp = cx.execute('SELECT * FROM org_unit WHERE org_unit_id=?', (par['parent_id'],)).fetchone()
                org = gp['name'] if gp and gp['tier'] == 'Organization' else ''
        elif ou['tier'] == 'Customer':
            cust = ou['name']
            par = cx.execute('SELECT * FROM org_unit WHERE org_unit_id=?', (ou['parent_id'],)).fetchone()
            org = par['name'] if par and par['tier'] == 'Organization' else ''
        elif ou['tier'] == 'Organization':
            cust, org = '', ou['name']
        else:
            cust, org = '', ''
        return cust, org, agency

    profiles, defaults = [], []
    for p in cx.execute('SELECT * FROM profile WHERE snapshot_id=? ORDER BY name', (SNAP,)):
        cust, org, agency = entity(p)
        rules = [dict(f=r['target_field'], tab=r['tab'] or '',
                      p=str(r['priority']) if r['priority'] is not None else '',
                      c=r['conditions_raw'] or '(always applies)',
                      ot=r['outcome_type'] or '', ov=r['outcome_value'] or '',
                      lvl=r['tier'])
                 for r in cx.execute('''SELECT * FROM rule
                                        WHERE profile_row_id=? AND rule_kind='mapping'
                                        ORDER BY CASE WHEN priority IS NULL THEN 999 ELSE priority END,
                                                 target_field''', (p['profile_row_id'],))]
        rec = dict(id=p['profile_guid'], name=p['name'], cust=cust, org=org, agency=agency,
                   tier=p['tier'], tiers=sorted({r['lvl'] for r in rules},
                       key=tier_rank),
                   payers=p['payers'] or '',
                   parent=p['parent_profile_guid'] or '', parentTier=p['parent_tier'] or '',
                   loc='Yes' if p['has_group_localization'] else 'No', rules=rules)
        (defaults if p['group_type'] == 'Default Profile' else profiles).append(rec)

    def org_names(org_unit_id):
        ou = cx.execute('SELECT * FROM org_unit WHERE org_unit_id=?', (org_unit_id,)).fetchone()
        if ou['tier'] == 'Agency':
            par = cx.execute('SELECT * FROM org_unit WHERE org_unit_id=?', (ou['parent_id'],)).fetchone()
            return ou['name'], (par['name'] if par else '')
        return '', ou['name']

    selection = []
    for r in cx.execute("""SELECT * FROM rule WHERE snapshot_id=? AND rule_kind='profile_selection'
                           ORDER BY CASE WHEN priority IS NULL THEN 999 ELSE priority END""", (SNAP,)):
        agency, cust = org_names(r['org_unit_id'])
        selection.append(dict(agency=agency, cust=cust, tier=r['tier'],
                              p=str(r['priority']) if r['priority'] is not None else '',
                              c=r['conditions_raw'] or '', desc=r['description'] or '',
                              target=r['resulting_profile_name'] or ''))

    payer = []
    for r in cx.execute("""SELECT * FROM rule WHERE snapshot_id=? AND rule_kind='payer_selection'
                           ORDER BY CASE WHEN priority IS NULL THEN 999 ELSE priority END""", (SNAP,)):
        agency, cust = org_names(r['org_unit_id'])
        payer.append(dict(cust=cust or agency, tier=r['tier'],
                          p=str(r['priority']) if r['priority'] is not None else '',
                          c=r['conditions_raw'] or '', desc=r['description'] or '',
                          payer=r['payer_name'] or '', form=r['payer_form_type'] or '',
                          timing=r['payer_timing'] or ''))

    nmap = cx.execute("SELECT COUNT(*) FROM rule WHERE snapshot_id=? AND rule_kind='mapping'", (SNAP,)).fetchone()[0]
    data = dict(profiles=profiles, defaults=defaults, selection=selection, payer=payer,
                counts=dict(rows=snap['row_count'], profiles=len(profiles), defaults=len(defaults),
                            mapping=nmap, selection=len(selection), payer=len(payer)),
                source=snap['source_file'],
                snapshot=dict(id=SNAP, exportedAt=snap['exported_at'], loadedAt=snap['loaded_at']))

    return data


# Importable. BG-71 (Snapshot Management from the Store) calls build() per
# snapshot from the server; running the script body on import would connect to
# whatever database the environment happened to name and fail before the caller
# could pass its own.
if __name__ == '__main__':
    cx = sqlite3.connect(DB)
    cx.row_factory = sqlite3.Row

    # Default to the most recently LOADED snapshot, not the highest id and not the
    # newest exported_at. Load order is the only ordering that matches the sequence
    # of analysis runs; keying on the dump's own date let an older export look like
    # the current state.
    if SEL is None:
        # Newest PRODUCTION snapshot, never a design. Opening the Grid on an
        # unapplied proposal is the worst available default: it looks like live
        # configuration and is not.
        row = cx.execute('''SELECT snapshot_id FROM snapshot WHERE kind='production'
                            ORDER BY loaded_at DESC, snapshot_id DESC LIMIT 1''').fetchone()
        if row is None:
            raise SystemExit('no production snapshot loaded — refusing to default to a design')
        SNAP = row[0]
    elif str(SEL).isdigit():
        SNAP = int(SEL)
    else:
        import os as _os
        row = cx.execute('''SELECT snapshot_id FROM snapshot WHERE source_file=?
                            ORDER BY loaded_at DESC LIMIT 1''',
                         (_os.path.basename(SEL),)).fetchone()
        if row is None:
            raise SystemExit(f'no snapshot loaded from {_os.path.basename(SEL)!r} — '
                             f'run udbr_load.py on it first')
        SNAP = row[0]

    snaps = [r['snapshot_id'] for r in
             cx.execute('SELECT snapshot_id FROM snapshot ORDER BY loaded_at, snapshot_id')]
    payloads = {sid: build(sid) for sid in snaps}
    cur = payloads[SNAP]

    out = dict(cur)                      # current snapshot at the top level, so
                                         # every existing reader keeps working
    # kind and derivedFrom travel with every snapshot. Without them the Grid shows
    # a live dump and an unapplied proposal side by side with nothing to tell them
    # apart - the exact conflation the schema's kind column exists to prevent,
    # reintroduced at the UI layer.
    meta = {r[0]: r for r in cx.execute(
        'SELECT snapshot_id, kind, derived_from FROM snapshot').fetchall()}
    out['snapshots'] = [dict(id=sid,
                             kind=meta[sid][1],
                             derivedFrom=meta[sid][2],
                             source=payloads[sid]['source'],
                             exportedAt=payloads[sid]['snapshot']['exportedAt'],
                             loadedAt=payloads[sid]['snapshot']['loadedAt'],
                             counts=payloads[sid]['counts'],
                             # A design workbook covers MAPPING rules only. Reporting
                             # 0 profile-selection and 0 payer-selection rules without
                             # saying so reads as though the design deletes them.
                             covers=('mapping' if meta[sid][1] == 'design' else 'all'))
                        for sid in snaps]
    out['bySnapshot'] = {str(sid): dict(profiles=payloads[sid]['profiles'],
                                        defaults=payloads[sid]['defaults'],
                                        selection=payloads[sid]['selection'],
                                        payer=payloads[sid]['payer'],
                                        counts=payloads[sid]['counts'],
                                        source=payloads[sid]['source'],
                                        snapshot=payloads[sid]['snapshot'])
                         for sid in snaps}
    data = out

    json.dump(data, open(OUT, 'w'), separators=(',', ':'))
    print(f"current snapshot {SNAP} ({cur['source']}, exported {cur['snapshot']['exportedAt']})")
    for sid in snaps:
        c = payloads[sid]['counts']
        mark = '  <-- current' if sid == SNAP else ''
        print(f"  snapshot {sid}: {payloads[sid]['source']:16} "
              f"{c['profiles']}+{c['defaults']} profiles, {c['mapping']} rules{mark}")
    print(f"  wrote {OUT} ({os.path.getsize(OUT)} bytes, {len(snaps)} snapshots)")
    cx.close()
