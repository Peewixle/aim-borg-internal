#!/usr/bin/env python3
"""
udbr_selection — BG-66 and BG-67, the selection rule surfaces.

TWO SURFACES, NOT ONE
=====================
Payer Selection Rules choose who gets billed. Profile Selection Rules choose
which profile fires. Similar shape, different decision, and a payer rule
additionally carries a timing attribute placing it before or after Insurance
Discovery and Eligibility, which has no equivalent on the profile side.
Combining them would imply they are the same kind of decision.

BANDED BY AOH LEVEL, EMPTY BANDS INCLUDED
=========================================
Every level is returned whether or not it holds rules. In the current estate
every payer rule sits at Customer and every profile rule at Agency, so three
of four bands are empty — and that absence is the information. Suppressing
empty bands would make a Customer-only construct out of something that can
exist at any level.

PRIORITY IS SCOPED TO THE ENTITY
================================
Not shared across bands. Azalea's twenty profile rules and West County's eight
both number from 1 and do not compete, so the rules are grouped by owning
entity within a band before being ordered by priority. Ordering the whole band
by priority would interleave two independent sequences.

A TARGET IS A NAME, AND NAMES ARE NOT UNIQUE
============================================
`ResultingProfile` holds a profile NAME. 'Default Mapping Rules' appears six
times in the estate, 'NON-BILLABLE' twice, 'test' twice. All twenty rules
resolve to exactly one profile today, and that will not stay true.

Where a target matches more than one profile the resolution is REFUSED and the
candidates are listed. Opening the first match would silently pick one of
several, which is the same defect as a destination keyed on field name without
its tab: plausible, quiet, and wrong.
"""
import sqlite3

from udbr_domain import TIERS

# Every level, always, in resolution order.
BANDS = TIERS


def _entity_of(cx, org_unit_id):
    if org_unit_id is None:
        return '', ''
    r = cx.execute('SELECT name, tier FROM org_unit WHERE org_unit_id=?',
                   (org_unit_id,)).fetchone()
    return (r['name'], r['tier']) if r else ('', '')


def _rules(cx, snapshot_id, kind):
    return cx.execute("""
        SELECT r.rule_guid, r.tier, r.org_unit_id,
               COALESCE(r.priority, '')        AS priority,
               COALESCE(r.conditions_raw, '')  AS cond,
               COALESCE(r.description, '')     AS descr,
               COALESCE(r.resulting_profile_name, '') AS target,
               COALESCE(r.payer_name, '')      AS payer,
               COALESCE(r.payer_form_type, '') AS form_type,
               COALESCE(r.payer_timing, '')    AS timing,
               COALESCE(r.outcome_value, '')   AS outcome
        FROM rule r
        WHERE r.snapshot_id=? AND r.rule_kind=?
    """, (snapshot_id, kind)).fetchall()


def _band(cx, rows, render):
    """Group into all four bands, then by entity, then by priority."""
    out = []
    for tier in BANDS:
        here = [r for r in rows if r['tier'] == tier]
        entities = {}
        for r in here:
            name, _ = _entity_of(cx, r['org_unit_id'])
            entities.setdefault(name or '(unattributed)', []).append(r)
        groups = []
        for ent in sorted(entities):
            rs = sorted(entities[ent],
                        key=lambda x: int(x['priority'])
                        if str(x['priority']).isdigit() else 999)
            groups.append(dict(entity=ent, count=len(rs),
                               rules=[render(cx, r) for r in rs]))
        out.append(dict(tier=tier, count=len(here), entities=groups))
    return out


def _resolve_target(cx, snapshot_id, name):
    """Which profile a Profile Selection Rule targets.

    Returns one of:
        {'status':'resolved',  'profile': guid, 'name': …}
        {'status':'missing'}
        {'status':'ambiguous', 'candidates': [ … ]}

    Ambiguity is REFUSED rather than resolved to the first match. A rule
    targeting 'NON-BILLABLE' names two profiles and there is nothing in the
    rule to say which.
    """
    if not name:
        return dict(status='missing', candidates=[])
    rows = cx.execute("""SELECT p.profile_guid, p.name, p.tier, p.group_type,
                                COALESCE(o.name,'') AS entity
                         FROM profile p
                         LEFT JOIN org_unit o ON o.org_unit_id = p.org_unit_id
                         WHERE p.snapshot_id=? AND p.name=?""",
                      (snapshot_id, name)).fetchall()
    if not rows:
        return dict(status='missing', candidates=[])
    if len(rows) == 1:
        return dict(status='resolved', profile=rows[0]['profile_guid'],
                    name=rows[0]['name'], candidates=[])
    return dict(status='ambiguous', profile=None, name=name,
                candidates=[dict(profile=r['profile_guid'], name=r['name'],
                                 tier=r['tier'], entity=r['entity'],
                                 kind=r['group_type']) for r in rows])


def payer_rules(db, snapshot_id):
    """BG-66. Every payer rule, banded, with the payer as the rule holds it."""
    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row
    snap = cx.execute('SELECT * FROM snapshot WHERE snapshot_id=?',
                      (snapshot_id,)).fetchone()
    if snap is None:
        cx.close()
        raise ValueError('no such snapshot')

    def render(cx, r):
        # The payer as RECORDED. No lookup, no resolution: a payer list is
        # coming to the dump and resolving against it is deliberately out of
        # scope until sample data exists.
        return dict(rule=r['rule_guid'], priority=r['priority'],
                    payer=r['payer'], form_type=r['form_type'],
                    # Timing as the rule holds it, never inferred. Every rule in
                    # the estate applies BEFORE discovery and eligibility, so
                    # nothing exercises the after case and a surface built to
                    # today's data could hard-code an attribute.
                    timing=r['timing'],
                    condition=r['cond'], description=r['descr'],
                    resolved=False)

    rows = _rules(cx, snapshot_id, 'payer_selection')
    bands = _band(cx, rows, render)
    cx.close()
    return dict(kind='payer', title='Payer Selection Rules',
                snapshot=snapshot_id, source=snap['source_file'],
                snapshot_kind=snap['kind'], exported=snap['exported_at'],
                total=len(rows), bands=bands,
                note='The first rule whose condition is satisfied selects the '
                     'payer. Rules are listed in the order they are evaluated.',
                payer_resolution='not performed — the surface shows what the '
                                 'rule holds')


def profile_rules(db, snapshot_id):
    """BG-67. Every profile rule, banded, with its target resolved or refused."""
    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row
    snap = cx.execute('SELECT * FROM snapshot WHERE snapshot_id=?',
                      (snapshot_id,)).fetchone()
    if snap is None:
        cx.close()
        raise ValueError('no such snapshot')

    def render(cx, r):
        return dict(rule=r['rule_guid'], priority=r['priority'],
                    target=r['target'],
                    # Resolved, missing or ambiguous — never a silent first match.
                    resolution=_resolve_target(cx, snapshot_id, r['target']),
                    condition=r['cond'],
                    # A rule with no description is shown without one rather
                    # than omitted.
                    description=r['descr'])

    rows = _rules(cx, snapshot_id, 'profile_selection')
    bands = _band(cx, rows, render)

    ambiguous = [r for b in bands for e in b['entities'] for r in e['rules']
                 if r['resolution']['status'] == 'ambiguous']
    missing = [r for b in bands for e in b['entities'] for r in e['rules']
               if r['resolution']['status'] == 'missing']
    cx.close()
    return dict(kind='profile', title='Profile Selection Rules',
                snapshot=snapshot_id, source=snap['source_file'],
                snapshot_kind=snap['kind'], exported=snap['exported_at'],
                total=len(rows), bands=bands,
                ambiguous=len(ambiguous), missing=len(missing),
                note='The first rule whose condition is satisfied selects the '
                     'profile. Rules are listed in the order they are '
                     'evaluated.',
                # BG-67: a reader may expect the Default Profile here and will
                # not find it. Stated on the surface rather than left as an
                # absence to be puzzled over.
                default_profile_note='The Default Profile is not listed. It '
                                     'fires regardless and is selected by no '
                                     'rule.')
