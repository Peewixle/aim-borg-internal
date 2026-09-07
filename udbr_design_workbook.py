#!/usr/bin/env python3
"""
udbr_design_workbook — BG-65. A completed design, as a workbook.

WHY A WORKBOOK AT ALL
=====================
The script output (BG-61) is for AIM. It is not readable by a person reviewing
a ruleset, and every design pass so far — v9, v10, v11 — was reviewed as a
spreadsheet, because that is what the RCM team works from.

WHAT IS GENERATED AND WHAT IS NOT
=================================
Everything on the Read Me is generated from the design and its findings, except
the decisions taken and the reasoning behind them, which cannot be. That
section is authored in the studio, stored with the design, and written in
beneath the generated content.

UDBR_Profile_Design_v11.xlsx demonstrates why it is needed: the workbook
travels to a team that was not in the room when the decisions were made.

STYLING IS SHARED
=================
Fonts, fills, borders and the table writer come from udbr_xlsx. The two
existing workbook builders each had their own copy and they had already
drifted — one wrapped long text and the other did not, so the same finding
rendered readably in one and clipped in the other.
"""
import os, sys, sqlite3, datetime

sys.path.insert(0, '/home/claude/udbr')

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from udbr_xlsx import H1, H2, BODY, BOLD, NOTE, HFILL, THIN, header, finish
from udbr_domain import TIERS, tier_rank, dest_label

# Band colours match the design workbook the RCM team already reads, so a sheet
# generated here and a sheet from v11 look like the same document.
BAND = {'System':       PatternFill('solid', fgColor='D9E2F3'),
        'Organization': PatternFill('solid', fgColor='DEEBD9'),
        'Customer':     PatternFill('solid', fgColor='FFF2CC'),
        'Agency':       PatternFill('solid', fgColor='F2E4EF')}

# A changed rule has to be findable by eye on a sheet of eighty. Colour alone
# would not survive printing, so the action is also written in a column.
CHANGED = PatternFill('solid', fgColor='FFF9E0')
ACTION_FONT = {'promote': Font(name='Arial', size=10, bold=True, color='1F4E79'),
               'demote':  Font(name='Arial', size=10, bold=True, color='7B3F7B'),
               'delete':  Font(name='Arial', size=10, bold=True, color='9C2A1A')}


def _rows(cx, snapshot_id):
    return cx.execute("""
        SELECT p.profile_guid, p.name AS profile, p.group_type, r.rule_guid,
               r.tier, COALESCE(r.tab,'') AS tab, r.target_field,
               COALESCE(r.priority,'') AS priority,
               COALESCE(r.conditions_raw,'') AS cond,
               COALESCE(r.outcome_type,'') AS otype,
               COALESCE(r.outcome_value,'') AS ovalue,
               COALESCE(ou.name,'') AS entity, ou.tier AS entity_tier
        FROM rule r
        JOIN profile p ON p.profile_row_id = r.profile_row_id
        LEFT JOIN org_unit ou ON ou.org_unit_id = p.org_unit_id
        WHERE r.snapshot_id=? AND r.rule_kind='mapping'
        ORDER BY p.name,
                 CASE r.tier WHEN 'System' THEN 0 WHEN 'Organization' THEN 1
                             WHEN 'Customer' THEN 2 ELSE 3 END,
                 CASE WHEN r.priority IS NULL THEN 999 ELSE r.priority END,
                 r.target_field""", (snapshot_id,)).fetchall()


def build(db, design_id, out_path, findings=None):
    """Generate the workbook. `findings` is the result of a check run; when
    absent the Read Me says the checks were not run rather than implying a
    clean design."""
    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row

    d = cx.execute('SELECT * FROM design_session WHERE design_id=?', (design_id,)).fetchone()
    if d is None:
        raise ValueError(f'no design {design_id}')
    base = cx.execute('SELECT * FROM snapshot WHERE snapshot_id=?',
                      (d['base_snapshot'],)).fetchone()

    design_rows = _rows(cx, d['snapshot_id'])
    base_rows = _rows(cx, d['base_snapshot'])
    base_by_guid = {r['rule_guid']: r for r in base_rows}
    design_guids = {r['rule_guid'] for r in design_rows}

    # The change set, derived — the same comparison the migration uses.
    changes = []
    for b in base_rows:
        cur = next((r for r in design_rows if r['rule_guid'] == b['rule_guid']), None)
        if cur is None:
            changes.append(dict(action='delete', rule=b['rule_guid'], profile=b['profile'],
                                tab=b['tab'], field=b['target_field'],
                                frm=b['tier'], to=''))
        elif cur['tier'] != b['tier']:
            act = 'promote' if tier_rank(cur['tier']) < tier_rank(b['tier']) else 'demote'
            changes.append(dict(action=act, rule=b['rule_guid'], profile=b['profile'],
                                tab=b['tab'], field=b['target_field'],
                                frm=b['tier'], to=cur['tier']))
    changed_guids = {c['rule'] for c in changes}
    by_action = {}
    for c in changes:
        by_action[c['action']] = by_action.get(c['action'], 0) + 1

    wb = Workbook()
    _read_me(wb, d, base, design_rows, base_rows, by_action, findings)
    _changes_sheet(wb, changes)
    _profile_sheets(wb, design_rows, changed_guids, changes)

    # The Read Me opens the workbook: it is created first and stays sheet one,
    # so the file opens on the context rather than on a profile.
    wb.active = 0
    finish(wb)
    wb.save(out_path)
    cx.close()
    return dict(path=out_path, rules_before=len(base_rows), rules_after=len(design_rows),
                changes=len(changes), by_action=by_action,
                sheets=len(wb.sheetnames))


def _read_me(wb, d, base, design_rows, base_rows, by_action, findings):
    ws = wb.active
    ws.title = 'Read Me'
    header(ws, f'Profile Design — {d["name"]}',
           'What this design proposes, and why.',
           f'Design {d["design_id"]}  |  base dump {base["source_file"]} '
           f'(exported {base["exported_at"]})  |  generated '
           f'{datetime.date.today().strftime("%B %d, %Y")}')

    r = 5
    ws.cell(r, 1, 'This is a DESIGN. It has not been applied to AIM.').font = BOLD
    r += 2

    for label, value in [
        ('State', d['state']),
        ('Created by', d['created_by']),
        ('Completed by', d['completed_by'] or '—'),
        ('Rules before', len(base_rows)),
        ('Rules after', len(design_rows)),
        ('Rules changed', sum(by_action.values())),
    ]:
        ws.cell(r, 1, label).font = BOLD
        ws.cell(r, 2, value).font = BODY
        r += 1

    r += 1
    ws.cell(r, 1, 'Changes by action').font = H2
    ws.cell(r, 1).fill = HFILL
    r += 1
    for act in ('promote', 'demote', 'delete'):
        ws.cell(r, 1, act).font = BODY
        ws.cell(r, 2, by_action.get(act, 0)).font = BODY
        r += 1

    r += 1
    ws.cell(r, 1, 'Outstanding findings').font = H2
    ws.cell(r, 1).fill = HFILL
    r += 1
    if findings is None:
        # Silence would read as a clean design. Say the checks did not run.
        ws.cell(r, 1, 'The checks were not run for this workbook. '
                      'No statement is made about the design being clean.').font = NOTE
        r += 2
    else:
        for sev in ('High', 'Medium', 'Low'):
            ws.cell(r, 1, sev).font = BODY
            ws.cell(r, 2, (findings.get('counts') or {}).get(sev, 0)).font = BODY
            r += 1
        r += 1
        for f in (findings.get('findings') or [])[:40]:
            ws.cell(r, 1, f['severity']).font = BODY
            ws.cell(r, 2, f['check']).font = BODY
            ws.cell(r, 3, str(f.get('subject', ''))).font = BODY
            ws.cell(r, 4, f.get('text', '')).font = BODY
            ws.cell(r, 4).alignment = Alignment(wrap_text=True, vertical='top')
            r += 1
        notrun = findings.get('notRun') or []
        r += 1
        ws.cell(r, 1, 'Checks not run').font = BOLD
        r += 1
        if notrun:
            for n in notrun:
                ws.cell(r, 1, n).font = NOTE
                r += 1
        else:
            ws.cell(r, 1, 'None recorded.').font = NOTE
            r += 1

    # The authored section, beneath the generated content. Not generated,
    # because the decisions taken and the reasoning behind them cannot be.
    r += 2
    ws.cell(r, 1, 'Decisions taken in this design').font = H2
    ws.cell(r, 1).fill = HFILL
    r += 1
    text = (d['readme'] or '').strip()
    if not text:
        ws.cell(r, 1, 'Nothing was written in the studio\'s Read Me tab for this design.'
                ).font = NOTE
    else:
        for line in text.split('\n'):
            c = ws.cell(r, 1, line)
            c.font = BODY
            c.alignment = Alignment(wrap_text=True, vertical='top')
            r += 1

    for col, w in zip('ABCD', (26, 46, 34, 78)):
        ws.column_dimensions[col].width = w


def _changes_sheet(wb, changes):
    ws = wb.create_sheet('Changes')
    header(ws, 'Changes from the base dump',
           'Every difference between this design and the dump it derives from.')
    cols = ['Action', 'Profile', 'Tab', 'Destination field', 'From', 'To', 'Rule Id']
    for j, c in enumerate(cols, 1):
        cell = ws.cell(4, j, c)
        cell.font = H2
        cell.fill = HFILL
        cell.border = THIN
    for i, c in enumerate(changes, 5):
        vals = [c['action'], c['profile'], c['tab'], c['field'], c['frm'], c['to'] or '—',
                c['rule']]
        for j, v in enumerate(vals, 1):
            cell = ws.cell(i, j, v)
            cell.font = ACTION_FONT.get(c['action'], BODY) if j == 1 else BODY
            cell.border = THIN
    for col, w in zip('ABCDEFG', (12, 44, 18, 30, 14, 14, 38)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = 'A5'


def _profile_sheets(wb, design_rows, changed_guids, changes):
    """One sheet per profile, rules banded by AOH level with the owning entity
    named on each band. Changed rules are distinguished by fill AND by an
    Action column, because a fill does not survive printing."""
    action_by_rule = {c['rule']: c['action'] for c in changes}
    by_profile = {}
    for r in design_rows:
        by_profile.setdefault(r['profile'], []).append(r)

    used = set()
    for name, rows in by_profile.items():
        # Excel sheet names: 31 characters, no []:*?/\ — and they must be
        # unique, which profile names are not.
        safe = ''.join(ch for ch in name if ch not in '[]:*?/\\')[:28]
        title, n = safe or 'profile', 2
        while title in used:
            title = f'{safe[:26]}~{n}'
            n += 1
        used.add(title)
        ws = wb.create_sheet(title)
        header(ws, name, f'{len(rows)} rules, banded by AOH level.')

        cols = ['Action', 'Field', 'Tab', 'Pri', 'Condition', 'Sets']
        for j, c in enumerate(cols, 1):
            cell = ws.cell(4, j, c)
            cell.font = H2
            cell.fill = HFILL
            cell.border = THIN

        i = 5
        for tier in TIERS:
            band = [r for r in rows if r['tier'] == tier]
            if not band:
                continue
            ent = band[0]['entity'] or 'AIM (System)'
            cell = ws.cell(i, 1, f'{tier} — {ent}')
            cell.font = Font(name='Arial', size=10, bold=True, color='0B1017')
            for j in range(1, len(cols) + 1):
                ws.cell(i, j).fill = BAND[tier]
                ws.cell(i, j).border = THIN
            i += 1
            for r in band:
                act = action_by_rule.get(r['rule_guid'], '')
                vals = [act, r['target_field'], r['tab'], r['priority'],
                        r['cond'] or '(always applies)', r['ovalue']]
                for j, v in enumerate(vals, 1):
                    cell = ws.cell(i, j, v)
                    cell.font = ACTION_FONT.get(act, BODY) if j == 1 else BODY
                    cell.border = THIN
                    cell.alignment = Alignment(vertical='top',
                                               wrap_text=(j == 5))
                    if r['rule_guid'] in changed_guids:
                        cell.fill = CHANGED
                i += 1
        for col, w in zip('ABCDEF', (12, 30, 18, 7, 52, 34)):
            ws.column_dimensions[col].width = w
        ws.freeze_panes = 'A5'


if __name__ == '__main__':
    db = sys.argv[1] if len(sys.argv) > 1 else '/home/claude/udbr/udbr.db'
    did = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    out = sys.argv[3] if len(sys.argv) > 3 else '/mnt/user-data/outputs/design.xlsx'
    print(build(db, did, out))
