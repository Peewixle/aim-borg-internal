#!/usr/bin/env python3
"""
udbr_trace — BG-56, the Profile Mapping Trace.

WHAT IT IS
==========
One row per destination billing field, read left to right in the order a value
travels: ePCR source, tab, destination field, eight rule bands in firing order,
three outputs, then the reviewer columns.

The profile detail view reads top to bottom by tier. This reads left to right
by destination, so a reviewer can see which rule actually wins for a field
without reconstructing it across screens.

A ROW IS (tab, field). NOTHING MERGES.
=====================================
`Signature Provided` on Payers and `Signature Provided (12)` on HCFA 1500 are
two destinations, not one field written twice. The evidence: 22 profiles set
the Payers one and not the HCFA one — patient-invoice profiles that write a
signature to the invoice and not to box 12 of the CMS-1500. Merged into one row
the trace would show them setting a field they do not set.

`Emergency` needs no special case under this rule, which is the point: it
exists on both ANSI 5010 and HCFA 1500 under an identical name, and any merge
rule keyed on the name alone would have collapsed two real destinations.

WHAT IS NOT COMPUTED HERE
=========================
NEMSIS element mapping. The conditions carry source field LABELS — 'PCR CMS
Service Level' — not element identifiers. Mapping a label to eResponse.05 needs
a lookup nobody has produced. BG-56 anticipates this: an unmapped label is
displayed as unmapped rather than omitted, and the Assumptions surface records
it as outstanding rather than letting silence imply it was done.
"""
import re, sqlite3
from collections import defaultdict

from udbr_domain import TIERS, dest_label

# The three outputs, in the order BG-56 specifies. A destination reaches an
# output when its tab is that output's tab; the Patient Invoice is fed by the
# Patient and Commercial tabs, which is why tab alone cannot name the output.
OUTPUTS = ('5010', 'HCFA 1500', 'Patient Invoice')
TAB_OUTPUT = {
    'ANSI 5010':        '5010',
    'HCFA 1500':        'HCFA 1500',
    'Patient':          'Patient Invoice',
    'Commercial':       'Patient Invoice',
    # These tabs feed the pipeline rather than an output document directly, so
    # a field on them reaches no output and shows three empty columns. BG-56
    # requires the row to remain rather than be dropped.
    'Bill Information': None,
    'Transport':        None,
    'Diagnosis':        None,
    'Payers':           None,
    'Charges':          None,
    'Narrative':        None,
}

# Source labels in a condition: 'PCR <field> <operator> ...' or 'Bill <field>'.
_SRC = re.compile(r'\b(PCR|Bill)\s+([A-Za-z0-9 /\-\(\)\.]+?)\s+'
                  r'(?:is not one of|is one of|contains any of|contains all|'
                  r'starts with|does not equal|equals|is not|is|greater|less)')


def _sources(condition):
    """The source field labels a condition reads. Labels, not NEMSIS elements —
    see the module note."""
    if not condition:
        return []
    return sorted({f'{m.group(1)} {m.group(2).strip()}' for m in _SRC.finditer(condition)})


SCHEMA = """
-- Reviewer columns. BG-56: a QC verdict, a disposition and a free-text note per
-- row, persisting with the trace so they are present when it is reopened.
--
-- Keyed on (snapshot, profile, tab, field) because that is what a row IS. Keyed
-- on the field alone, two rows sharing a field name would share one verdict.
CREATE TABLE IF NOT EXISTS trace_review (
    snapshot_id  INTEGER NOT NULL,
    profile_guid TEXT    NOT NULL,
    tab          TEXT    NOT NULL,
    field        TEXT    NOT NULL,
    verdict      TEXT,
    disposition  TEXT,
    note         TEXT,
    reviewed_by  TEXT,
    reviewed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (snapshot_id, profile_guid, tab, field)
);
"""


def ensure(cx):
    cx.executescript(SCHEMA)
    cx.commit()


def trace(db, snapshot_id, profile_guid):
    """Build the trace for one profile."""
    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row
    ensure(cx)

    prof = cx.execute("""SELECT p.*, ou.name AS entity, ou.tier AS entity_tier
                         FROM profile p LEFT JOIN org_unit ou ON ou.org_unit_id=p.org_unit_id
                         WHERE p.snapshot_id=? AND p.profile_guid=?""",
                      (snapshot_id, profile_guid)).fetchone()
    if prof is None:
        cx.close()
        raise ValueError('no such profile in this snapshot')
    snap = cx.execute('SELECT * FROM snapshot WHERE snapshot_id=?',
                      (snapshot_id,)).fetchone()

    # The row set is the union of what this profile writes and what the Default
    # Profile writes — the reviewer needs both to see a field's real value.
    rules = cx.execute("""
        SELECT r.rule_guid, r.tier, COALESCE(r.tab,'') AS tab, r.target_field,
               COALESCE(r.priority,'') AS priority,
               COALESCE(r.conditions_raw,'') AS cond,
               COALESCE(r.outcome_type,'') AS otype,
               COALESCE(r.outcome_value,'') AS ovalue,
               COALESCE(r.outcome_raw,'') AS oraw,
               COALESCE(r.description,'') AS descr,
               p.group_type, p.name AS profile, p.profile_guid,
               COALESCE(ou.name,'') AS entity
        FROM rule r
        JOIN profile p ON p.profile_row_id = r.profile_row_id
        LEFT JOIN org_unit ou ON ou.org_unit_id = p.org_unit_id
        WHERE r.snapshot_id=? AND r.rule_kind='mapping'
          AND (p.profile_guid=? OR p.group_type='Default Profile')
    """, (snapshot_id, profile_guid)).fetchall()

    reviews = {(r['tab'], r['field']): dict(verdict=r['verdict'],
                                            disposition=r['disposition'],
                                            note=r['note'], by=r['reviewed_by'],
                                            at=r['reviewed_at'])
               for r in cx.execute("""SELECT * FROM trace_review
                                      WHERE snapshot_id=? AND profile_guid=?""",
                                   (snapshot_id, profile_guid))}
    cx.close()

    # Group into rows. THE KEY IS (tab, field).
    rows = defaultdict(lambda: dict(bands=defaultdict(list), sources=set(),
                                    copied_from=set()))
    for r in rules:
        key = (r['tab'], r['target_field'])
        row = rows[key]
        band = ('default' if r['group_type'] == 'Default Profile' else 'specific',
                r['tier'])
        row['bands'][band].append(dict(
            rule=r['rule_guid'], priority=r['priority'], condition=r['cond'],
            outcome_type=r['otype'], value=r['ovalue'], raw=r['oraw'],
            description=r['descr'], entity=r['entity'], profile=r['profile']))
        row['sources'].update(_sources(r['cond']))
        # A value copied from an ePCR field is shown separately from the
        # elements a condition reads: they are different relationships.
        if 'PCR' in r['otype'] and r['ovalue']:
            row['copied_from'].add(r['ovalue'])

    out = []
    # FIELD, then TAB — the same order the browser uses, and for the same
    # reason: it puts the two Emergency destinations on adjacent rows so the
    # Tab column shows they differ. Sorted (tab, field), they would sit pages
    # apart and a reader could see one without ever noticing the other.
    for (tab, field), row in sorted(rows.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        # Eight bands, always all eight. BG-56: empty columns are displayed,
        # not suppressed — an absent band is information.
        bands = []
        for kind in ('default', 'specific'):
            for tier in TIERS:
                rs = sorted(row['bands'].get((kind, tier), []),
                            key=lambda x: int(x['priority']) if str(x['priority']).isdigit() else 999)
                bands.append(dict(set=kind, tier=tier, rules=rs, count=len(rs)))

        reaches = TAB_OUTPUT.get(tab, None)
        outputs = []
        for o in OUTPUTS:
            outputs.append(dict(output=o,
                                lands=(field if reaches == o else None),
                                # The tab-to-output map is derived, not read
                                # from the product, so it is marked unconfirmed
                                # rather than presented as fact.
                                confirmed=False))

        rv = reviews.get((tab, field), {})
        out.append(dict(
            tab=tab, field=field, label=dest_label((tab, field)),
            sources=sorted(row['sources']) or [],
            sources_unmapped=True,     # no NEMSIS lookup exists yet
            copied_from=sorted(row['copied_from']),
            bands=bands, outputs=outputs,
            verdict=rv.get('verdict'), disposition=rv.get('disposition'),
            note=rv.get('note'),
        ))

    return dict(
        profile=prof['name'], profile_guid=profile_guid,
        entity=prof['entity'], tier=prof['tier'],
        snapshot=snapshot_id, source=snap['source_file'], kind=snap['kind'],
        rows=out, row_count=len(out),
        outputs=list(OUTPUTS),
        assumptions=_assumptions(out),
    )


def _assumptions(rows):
    """BG-56's Assumptions surface. Every inference the trace applied, with its
    basis and whether it is settled — so an inference is never mistaken for a
    fact the product confirmed."""
    a = [
        dict(subject='Destination identity',
             assumed='A row is (tab, field). Nothing merges, including '
                     'Signature Provided and Signature Provided (12).',
             basis='22 profiles set the Payers variant and not the HCFA one. '
                   'Merging would show them setting a field they do not set.',
             status='settled', owner=None),
        dict(subject='NEMSIS element mapping',
             assumed='Source fields are shown as labels, not NEMSIS elements.',
             basis='Conditions carry labels such as "PCR CMS Service Level". No '
                   'label-to-element lookup exists.',
             status='awaiting confirmation', owner='Product'),
        dict(subject='Tab to output',
             assumed='ANSI 5010 reaches 5010; HCFA 1500 reaches HCFA 1500; '
                     'Patient and Commercial reach the Patient Invoice; other '
                     'tabs reach no output.',
             basis='Derived from tab names, not read from the product. Shown '
                   'unconfirmed throughout.',
             status='awaiting confirmation', owner='Product'),
    ]
    n = sum(1 for r in rows if r['tab'] not in TAB_OUTPUT)
    if n:
        a.append(dict(subject='Unknown tabs',
                      assumed=f'{n} row(s) sit on a tab with no output mapping.',
                      basis='The tab was not in the derived map.',
                      status='awaiting confirmation', owner='Product'))
    return a


def set_review(db, snapshot_id, profile_guid, tab, field, username,
               verdict=None, disposition=None, note=None):
    cx = sqlite3.connect(db)
    ensure(cx)
    cx.execute("""INSERT INTO trace_review
        (snapshot_id,profile_guid,tab,field,verdict,disposition,note,reviewed_by,
         reviewed_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(snapshot_id,profile_guid,tab,field) DO UPDATE SET
          verdict=excluded.verdict, disposition=excluded.disposition,
          note=excluded.note, reviewed_by=excluded.reviewed_by,
          reviewed_at=excluded.reviewed_at""",
        (snapshot_id, profile_guid, tab, field, verdict, disposition, note, username))
    cx.commit()
    cx.close()


def rule_detail(db, snapshot_id, profile_guid):
    """BG-56's Rule Detail surface: every rule behind the grid, readable across
    all destinations at once rather than one cell at a time."""
    t = trace(db, snapshot_id, profile_guid)
    out = []
    for row in t['rows']:
        for band in row['bands']:
            for r in band['rules']:
                out.append(dict(tab=row['tab'], field=row['field'],
                                rule_set=band['set'], tier=band['tier'],
                                entity=r['entity'], priority=r['priority'],
                                outcome_type=r['outcome_type'], value=r['value'],
                                raw=r['raw'], condition=r['condition'],
                                description=r['description'], rule=r['rule']))
    # Sorted by tab, then field, then band, then priority — the order BG-56 asks
    # for, which is the order a reviewer reads a bill in.
    order = {('default', t): i for i, t in enumerate(TIERS)}
    order.update({('specific', t): 4 + i for i, t in enumerate(TIERS)})
    out.sort(key=lambda r: (r['tab'], r['field'],
                            order.get((r['rule_set'], r['tier']), 9),
                            int(r['priority']) if str(r['priority']).isdigit() else 999))
    return dict(profile=t['profile'], snapshot=snapshot_id, rules=out, count=len(out))


def source_map(db, snapshot_id, profile_guid):
    """BG-56's ePCR Source Map: every source field the profile reads, whether
    it is used as a condition or a value, and which destinations it feeds."""
    t = trace(db, snapshot_id, profile_guid)
    m = defaultdict(lambda: dict(as_condition=False, as_value=False, feeds=set()))
    for row in t['rows']:
        for s in row['sources']:
            m[s]['as_condition'] = True
            m[s]['feeds'].add(row['label'])
        for s in row['copied_from']:
            m[s]['as_value'] = True
            m[s]['feeds'].add(row['label'])
    return dict(profile=t['profile'], snapshot=snapshot_id,
                nemsis_version='v3.5.0 Build 251001 Critical Patch 6',
                verified=False,
                sources=[dict(source=k, element=None, element_name=None,
                              mapping='unmapped',
                              as_condition=v['as_condition'], as_value=v['as_value'],
                              feeds=sorted(v['feeds']))
                         for k, v in sorted(m.items())],
                count=len(m))
