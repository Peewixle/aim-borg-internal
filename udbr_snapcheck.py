#!/usr/bin/env python3
"""
udbr_snapcheck — run the conflict and completeness checks against a snapshot
held in the store.

WHY THIS EXISTS
===============
BG-62 and BG-63 require every check `analyze.py` performs to run against a
design in the studio. `analyze.py` reads an .xlsx; a design has never been an
.xlsx and never will be. So the frames the checks expect are built from the
store instead.

The checks themselves are NOT reimplemented. This module reshapes rows into the
four sections `udbr_checks.run_checks` takes, and calls it. That is the whole
point: one implementation, because two implementations of these specific checks
demonstrably diverged — the same defect had to be fixed in `analyze.py` and
`analyze_design.py` separately, and was got wrong in both on the first pass.

WHAT IT ADDS OVER analyze.py
============================
Attribution. BG-62 requires a conflict to be attributed to the change that
introduced it. A finding's anchors name the rules involved; the design's save
history says which save last touched each. Because autosave collapses a burst,
attribution lands on a save rather than a single change, and where the history
cannot establish a cause the finding names the rules without asserting one.
"""
import sqlite3
import pandas as pd

from udbr_checks import run_checks

# The column names the checks expect. Built from the store rather than read
# from a file, so a design that exists only in the database can be checked.
_RULE_COLS = """
    r.rule_guid   AS RuleId,
    p.profile_guid AS ProfileId,
    p.name        AS ProfileName,
    p.group_type  AS GroupType,
    r.tier        AS Tier,
    COALESCE(org.name, '')  AS Organization,
    COALESCE(cust.name, '') AS Customer,
    COALESCE(agy.name, '')  AS Agency,
    COALESCE(r.tab, '')            AS Tab,
    COALESCE(r.target_field, '')   AS TargetField,
    COALESCE(r.priority, '')       AS Priority,
    COALESCE(r.conditions_raw, '') AS Conditions,
    COALESCE(r.outcome_type, '')   AS OutcomeType,
    COALESCE(r.outcome_value, '')  AS OutcomeValue,
    COALESCE(r.description, '')    AS RuleDescription,
    COALESCE(r.resulting_profile_name, '') AS ResultingProfile,
    COALESCE(r.payer_name, '')     AS PayerName,
    COALESCE(p.payers, '')         AS ProfilePayers,
    CASE WHEN p.has_group_localization THEN 'Yes' ELSE 'No' END AS HasGroupLocalization,
    CASE WHEN r.is_rule_localization  THEN 'Yes' ELSE 'No' END AS IsRuleLocalization
"""

# Entity resolution walks the org tree: a rule's agency comes from its profile,
# never from the rule row, because mapping rules carry no Agency at all.
_JOINS = """
    FROM rule r
    LEFT JOIN profile  p    ON p.profile_row_id = r.profile_row_id
    LEFT JOIN org_unit ou   ON ou.org_unit_id   = COALESCE(p.org_unit_id, r.org_unit_id)
    LEFT JOIN org_unit agy  ON agy.org_unit_id  = CASE WHEN ou.tier='Agency' THEN ou.org_unit_id END
    LEFT JOIN org_unit cust ON cust.org_unit_id = CASE
                                 WHEN ou.tier='Customer' THEN ou.org_unit_id
                                 WHEN ou.tier='Agency'   THEN ou.parent_id END
    LEFT JOIN org_unit org  ON org.org_unit_id  = CASE
                                 WHEN ou.tier='Organization' THEN ou.org_unit_id
                                 WHEN cust.parent_id IS NOT NULL THEN cust.parent_id END
"""


def frames(db, snapshot_id):
    """The four sections the checks take, built from one snapshot."""
    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row

    def q(sql, *a):
        rows = [dict(r) for r in cx.execute(sql, a)]
        return pd.DataFrame(rows) if rows else pd.DataFrame(columns=_empty_cols())

    prof = q("""SELECT p.profile_guid AS ProfileId, p.name AS ProfileName,
                       p.group_type AS GroupType, p.tier AS Tier,
                       COALESCE(p.payers,'') AS ProfilePayers,
                       COALESCE(p.description,'') AS ProfileDescription,
                       CASE WHEN p.has_group_localization THEN 'Yes' ELSE 'No' END
                         AS HasGroupLocalization,
                       COALESCE(org.name,'')  AS Organization,
                       COALESCE(cust.name,'') AS Customer,
                       COALESCE(agy.name,'')  AS Agency
                FROM profile p
                LEFT JOIN org_unit ou   ON ou.org_unit_id = p.org_unit_id
                LEFT JOIN org_unit agy  ON agy.org_unit_id = CASE WHEN ou.tier='Agency'
                                            THEN ou.org_unit_id END
                LEFT JOIN org_unit cust ON cust.org_unit_id = CASE
                                            WHEN ou.tier='Customer' THEN ou.org_unit_id
                                            WHEN ou.tier='Agency' THEN ou.parent_id END
                LEFT JOIN org_unit org  ON org.org_unit_id = CASE
                                            WHEN ou.tier='Organization' THEN ou.org_unit_id
                                            WHEN cust.parent_id IS NOT NULL THEN cust.parent_id END
                WHERE p.snapshot_id=?""", snapshot_id)

    def rules(kind):
        return q(f"SELECT {_RULE_COLS} {_JOINS} WHERE r.snapshot_id=? AND r.rule_kind=?",
                 snapshot_id, kind)

    # Built before closing: rules() captures cx, and closing first left the
    # inner call operating on a shut connection.
    out = (prof, rules('mapping'), rules('profile_selection'), rules('payer_selection'))
    cx.close()
    return out


def _empty_cols():
    return ['RuleId', 'ProfileId', 'ProfileName', 'GroupType', 'Tier', 'Organization',
            'Customer', 'Agency', 'Tab', 'TargetField', 'Priority', 'Conditions',
            'OutcomeType', 'OutcomeValue', 'RuleDescription', 'ResultingProfile',
            'PayerName', 'ProfilePayers', 'HasGroupLocalization', 'IsRuleLocalization']


def check_snapshot(db, snapshot_id, design_store=None, design_id=None):
    """Run every check against a snapshot. Returns a dict ready for the
    findings surface (BG-64).

    When a design store and id are given, findings are attributed to the save
    that last touched the rules they name.
    """
    prof, pmr, psr, pysr = frames(db, snapshot_id)
    findings, candidates = run_checks(prof, pmr, psr, pysr)

    out = []
    for f in findings:
        anchors = f.get('_anchors') or []
        rec = dict(check=f['Check'], severity=f['Severity'], tier=f['Tier'],
                   entity=f['Customer'], subject=str(f['Subject']),
                   text=f['Finding'], evidence=f['Evidence'],
                   profile=f.get('_pid'),
                   rules=[dict(tab=a[0], field=a[1], priority=a[2]) for a in anchors
                          if len(a) == 3])
        out.append(rec)

    if design_store is not None and design_id is not None:
        _attribute(design_store, design_id, out, pmr)

    sev = {}
    for f in out:
        sev[f['severity']] = sev.get(f['severity'], 0) + 1
    return dict(findings=out, candidates=len(candidates), counts=sev,
                total=len(out))


def _attribute(store, design_id, findings, pmr):
    """Name the save that last touched each rule a finding points at.

    BG-62: where the history cannot establish which change is responsible, the
    finding names the rules involved without asserting a cause. That is what
    happens here when a rule was never touched in this design — the finding is
    real, it just did not come from a change.
    """
    by_dest = {}
    for r in pmr.itertuples():
        by_dest[(r.ProfileId, r.Tab, r.TargetField, str(r.Priority))] = r.RuleId

    for f in findings:
        guids = []
        for a in f['rules']:
            g = by_dest.get((f['profile'], a['tab'], a['field'], str(a['priority'])))
            if g:
                guids.append(g)
        if not guids:
            f['attributed'] = None
            continue
        who = store.attribute(design_id, guids)
        saves = {v['save'] for v in who.values()}
        if len(saves) == 1:
            v = next(iter(who.values()))
            f['attributed'] = dict(save=v['save'], by=v['by'], at=v['at'])
        elif saves:
            # More than one save is implicated: name the rules, assert no cause.
            f['attributed'] = None
            f['involves'] = sorted(guids)
        else:
            f['attributed'] = None
