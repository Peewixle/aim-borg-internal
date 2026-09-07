#!/usr/bin/env python3
"""
udbr_checks — the conflict and completeness checks, as a callable function.

WHY THIS IS A MODULE AND NOT A SCRIPT
=====================================
The checks lived inside analyze.py, which read an .xlsx, ran them, wrote six
files and persisted a run. That meant they could not be run against anything
but a dump on disk, could not be tested without side effects, and could not be
called by the studio at all.

The code review recorded this as M1 and it was the one finding left open. The
studio forces it: BG-62 and BG-63 require these exact checks against a design
held in the store, not against a file.

So the checks take frames and return findings. Nothing here reads a file,
writes a file, or touches the database. analyze.py is now one caller and the
studio is another, both running the same code — which is the whole point,
because two implementations of these checks demonstrably diverged.

A destination is (tab, field). Never a field name alone: 'Emergency' exists on
both the ANSI 5010 and HCFA 1500 tabs and they are different destinations.
"""
import re
from collections import defaultdict, Counter
import pandas as pd

from udbr_domain import dest, dest_key, dest_label, dest_list, dest_str


def run_checks(PROF, PMR, PSR, PYSR):
    """Run every conflict and completeness check.

    PROF/PMR/PSR/PYSR are the four sections of a rule set as DataFrames, from
    a dump or from the store. Returns (findings, candidates).
    """
    # Derived here rather than passed in: a caller should hand over the four
    # sections, not have to know which GroupType means what.
    SPEC = PROF[PROF.GroupType == 'Profiles']
    DFLT = PROF[PROF.GroupType == 'Default Profile']

    findings = []   # every row: check, severity, scope, subject, detail, evidence


    def add(check, sev, tier, customer, subject, detail, evidence='',
            profile_id=None, anchors=None, fields=None):
        # anchors identify the exact rules a finding is about: (field, priority)
        # pairs within profile_id. The workbook ignores them; the Borg uses them to
        # mark the offending rows in place, which is the one thing a spreadsheet
        # row cannot do.
        findings.append(dict(Check=check, Severity=sev, Tier=tier, Customer=customer,
                             Subject=subject, Finding=detail, Evidence=evidence,
                             _pid=profile_id, _anchors=anchors or [], _fields=fields or []))

    def pname(pid):
        r = PROF[PROF.ProfileId == pid]
        return r.ProfileName.iloc[0] if len(r) else '(unknown)'

    def pcust(pid):
        r = PROF[PROF.ProfileId == pid]
        return r.Customer.iloc[0] if len(r) else ''

    UNCOND = '(always applies)'

    # =========================================================================
    # CONFLICT DETECTION
    # =========================================================================

    # --- C1 / C2 : ADO 7643 (Pipeline: Profiles: Conflict Detection) and
    #               ADO 7657 (Pipeline: Default Profile: Conflict Detection)
    # Grouped by ProfileId, never by ProfileName: both customers' Default Profiles
    # are literally named "Default Mapping Rules", and grouping by name merges two
    # customers' rule sets into one phantom profile. That mistake manufactured a
    # defect that did not exist.
    for pid, g in PMR.groupby('ProfileId'):
        if not len(g): continue
        is_default = (g.GroupType.iloc[0] == 'Default Profile')
        check = 'C2 Default Profile duplicate rules (ADO 7657)' if is_default \
                else 'C1 Profile duplicate rules (ADO 7643)'
        chk_amb = 'C2 Default Profile ambiguous rules (ADO 7657)' if is_default \
                else 'C1 Profile ambiguous rules (ADO 7643)'
        tier, cust, nm = g.Tier.iloc[0], g.Customer.iloc[0], pname(pid)

        # exact duplicates: identical field, priority, condition and outcome
        for key, gg in g.groupby(['Tab','TargetField','Priority','Conditions','OutcomeValue']):
            if len(gg) > 1:
                add(check, 'Low', tier, cust, nm,
                    f'{len(gg)} identical rules on {dest_label((key[0], key[1]))}, priority {key[2]}. '
                    f'Under first-match firing the extra copies are never reached, and every '
                    f'copy sets the same value, so no bill is affected.',
                    f'sets: {str(key[4])[:60]}',
                    profile_id=pid, anchors=[(key[0], key[1], key[2])])

        # ambiguous: same field and priority, DIFFERENT outcome. Which one wins is
        # undefined by priority alone, so this is a genuine contradiction.
        for key, gg in g.groupby(['Tab','TargetField','Priority']):
            outs = gg.OutcomeValue.unique()
            if len(outs) > 1:
                add(chk_amb, 'High', tier, cust, nm,
                    f'{dest_label((key[0], key[1]))} is set at priority {key[2]} by {len(gg)} rules '
                    f'with {len(outs)} different outcomes. Priority does not decide between them.',
                    ' | '.join(str(o)[:34] for o in outs[:3]),
                    profile_id=pid, anchors=[(key[0], key[1], key[2])])

    # --- C3 : ADO 8175 (Unconditional Mapping Rule followed by other Mapping Rules
    #          targeting the same destination field)
    #
    # FIRING ORDER CONFIRMED by ADO 8159 (Replace Help Text on Each Screen):
    # within a level, the FIRST mapping rule whose conditions are satisfied
    # produces that level's value, and all lower-priority rules on the same
    # destination field are then skipped.
    #
    # So the defect is narrow: a CONDITIONAL rule sitting at a LATER priority than
    # an unconditional rule on the same field can never be reached. A conditional
    # rule EARLIER than an unconditional one is the correct pattern - specific
    # cases first, unconditional fallback last - and must not be reported.
    #
    # An earlier version of this check flagged any field holding both kinds and
    # produced 8 findings. All 8 had their conditional rules first. They were
    # false positives from not knowing the firing order, and reporting them would
    # have sent Michelle to look at correctly written rules.
    for pid, g in PMR.groupby('ProfileId'):
        tier, cust, nm = g.Tier.iloc[0], g.Customer.iloc[0], pname(pid)
        for fld, gg in g.groupby(['Tab','TargetField']):
            rows = [(int(r.Priority), r.Conditions, r.OutcomeValue)
                    for _, r in gg.iterrows() if str(r.Priority).isdigit()]
            if len(rows) < 2: continue
            rows.sort(key=lambda x: x[0])
            first_uncond = next((p for p, c, o in rows if c == UNCOND), None)
            if first_uncond is None: continue
            unreachable = [(p, o) for p, c, o in rows if p > first_uncond and c != UNCOND]
            if unreachable:
                add('C3 Conditional rule unreachable behind an unconditional rule (ADO 8175)',
                    'High', tier, cust, nm,
                    f'{dest_label(fld)} has an unconditional rule at priority {first_uncond}. '
                    f'{len(unreachable)} conditional rule(s) at a later priority can never be '
                    f'reached: the unconditional rule always matches first and the rest are skipped.',
                    ', '.join(f'pri {p} -> {str(o)[:22]}' for p, o in unreachable[:4]),
                    profile_id=pid, anchors=[(fld[0], fld[1], str(p)) for p, _ in unreachable])

    # --- C4 : substring shadowing between conditions on the same field
    # A pattern that is a prefix of another pattern on the same field matches
    # everything the longer one matches. Order then decides whether the specific
    # rule is ever reached.
    PAT = re.compile(r'contains all \[?([A-Za-z0-9]+)\]?', re.I)
    for pid, g in PMR.groupby('ProfileId'):
        tier, cust, nm = g.Tier.iloc[0], g.Customer.iloc[0], pname(pid)
        for fld, gg in g.groupby(['Tab','TargetField']):
            pats = []
            for _, r in gg.iterrows():
                m = PAT.search(str(r.Conditions))
                if m and str(r.Priority).isdigit():
                    pats.append((m.group(1), int(r.Priority), r.OutcomeValue))
            for a, pa, oa in pats:
                for b, pb, ob in pats:
                    if a != b and b.upper().startswith(a.upper()) and pa < pb:
                        add('C4 Broader pattern shadows a more specific one (ADO 7643)', 'High',
                            tier, cust, nm,
                            f'On {dest_label(fld)}: pattern "{a}" (priority {pa}) is a prefix of "{b}" '
                            f'(priority {pb}). Every value matching "{b}" also matches "{a}", and '
                            f'first match wins, so "{b}" is never reached. Those bills are '
                            f'getting "{oa}" instead of "{ob}" in production.',
                            f'{a}->{oa} shadows {b}->{ob}',
                            profile_id=pid,
                            anchors=[(fld[0], fld[1], str(pa)), (fld[0], fld[1], str(pb))])

    # --- C5 : ADO 7658 (Profile Selection Rules: Conflict Detection)
    # --- C6 : ADO 7786 (Payer Selection Rules: Conflict Detection)
    def selection_conflicts(frame, check_pref, ado, scope_col):
        for scope, g in frame.groupby(scope_col):
            if not scope: continue
            # duplicate priorities within one scope
            for pri, gg in g.groupby('Priority'):
                if len(gg) > 1:
                    add(f'{check_pref} duplicate priority ({ado})', 'High',
                        g.Tier.iloc[0], g.Customer.iloc[0], scope,
                        f'{len(gg)} rules share priority {pri}. Evaluation order between '
                        f'them is undefined.',
                        ' | '.join(str(x)[:30] for x in gg.iloc[:,:1].squeeze(axis=1).tolist()[:3])
                        if len(gg.columns) else '')
            # unsatisfiable: "contains all" over a multi-value list on a
            # single-valued field can never be true
            for _, r in g.iterrows():
                for m in re.finditer(r'([A-Za-z0-9 /()\-\.]+?) contains all \[([^\]]+)\]', str(r.Conditions)):
                    vals = [v.strip() for v in m.group(2).split(',')]
                    if len(vals) > 1:
                        add(f'{check_pref} unsatisfiable condition ({ado})', 'High',
                            r.Tier, r.Customer, scope,
                            f'Priority {r.Priority}: "{m.group(1).strip()}" must contain ALL of '
                            f'{vals}. On a single-valued field this can never be true, so the '
                            f'rule never fires.',
                            f'-> {r.ResultingProfile if "ResultingProfile" in r else r.PayerName}')

    selection_conflicts(PSR,  'C5 Profile Selection Rule', 'ADO 7658', 'Agency')
    selection_conflicts(PYSR, 'C6 Payer Selection Rule',   'ADO 7786', 'Customer')

    # --- C7 : selection rules pointing at a profile that does not exist
    names = set(PROF.ProfileName)
    for _, r in PSR.iterrows():
        if r.ResultingProfile and r.ResultingProfile not in names:
            add('C7 Selection rule targets a missing profile (ADO 7658)', 'High',
                r.Tier, r.Customer, r.Agency,
                f'Priority {r.Priority} resolves to "{r.ResultingProfile}", which is not a '
                f'profile in this dump.', '')

    # --- C8 : non-production artefacts live in production
    for _, p in SPEC.iterrows():
        if re.fullmatch(r'\s*test\s*', str(p.ProfileName), re.I):
            add('C8 Test artefact in production (ADO 7643)', 'Medium',
                p.Tier, p.Customer, p.ProfileName,
                'A profile named "test" is live in production and would be carried into '
                'any bulk promotion.', f'payers: {p.ProfilePayers}',
                profile_id=p.ProfileId)

    # =========================================================================
    # COMPLETENESS
    #
    # INTERIM DEFINITION. Completeness is normally measured against a standard,
    # and no standard exists yet: System and Organization tiers hold zero mapping
    # rules and HasGroupLocalization is No on all 520. Checks written that way
    # would return empty and read as a clean bill of health.
    #
    # Substitute used here: measure each profile against its OWN FAMILY - the
    # other profiles of the same customer. A field set by most siblings and missing
    # from one is a real signal today and is actionable without a standard.
    # =========================================================================

    FAMILY_THRESHOLD = 0.80   # a field this common among siblings is treated as expected

    # --- K1a : ADO 8137, measured within a NAME FAMILY.
    # Customer-level comparison proved too coarse: a customer holds 12-rule trip
    # profiles and 6-rule patient-invoice profiles, so common-field coverage falls
    # away and real gaps hide under the threshold. Profiles sharing a leading name
    # token (DIALYSIS, AUTO, MEDICAID POS) do the same job and should carry the
    # same fields, which makes a missing one meaningful.
    def family_of(name):
        return re.split(r'[ /\-]', str(name).upper())[0]

    fam_index = defaultdict(list)
    for _, p in SPEC.iterrows():
        fam_index[(p.Customer, family_of(p.ProfileName))].append(p.ProfileId)

    for (cust, fam), pids in sorted(fam_index.items()):
        if not cust or len(pids) < 3: continue
        # (tab, field). Keyed on the name alone, a profile setting Emergency on
        # ANSI 5010 and a sibling setting it on HCFA 1500 both read as 'has
        # Emergency', hiding a real gap.
        have = {pid: {dest(r) for r in PMR[PMR.ProfileId == pid].itertuples()} for pid in pids}
        have = {p: f for p, f in have.items() if f}
        if len(have) < 3: continue
        counts = Counter()
        for f in have.values(): counts.update(f)
        # within a family, a field carried by every sibling but one is the signal
        expected = {fld for fld, n in counts.items() if n == len(have) - 1 or n == len(have)}
        for pid, fields in have.items():
            missing = sorted(f for f in expected if f not in fields)
            if missing:
                add('K1a Profile missing a field its family carries (ADO 8137)', 'High',
                    'Customer', cust, pname(pid),
                    f'Family "{fam}" has {len(have)} profiles. {len(missing)} field(s) carried '
                    f'by every other member are not set here.',
                    dest_list(missing),
                    profile_id=pid, fields=[dest_str(d) for d in sorted(missing)])

    # --- K1b : ADO 8137, measured across the whole customer (weaker signal)
    for cust, g in SPEC.groupby('Customer'):
        if not cust: continue
        pids = list(g.ProfileId)
        fields_by_profile = {pid: {dest(r) for r in PMR[PMR.ProfileId == pid].itertuples()} for pid in pids}
        # only profiles that actually carry rules take part; a 0-rule profile is a
        # different finding, not an incomplete one
        active = {p: f for p, f in fields_by_profile.items() if f}
        if len(active) < 3: continue
        counts = Counter()
        for f in active.values():
            counts.update(f)
        expected = {fld for fld, n in counts.items() if n / len(active) >= FAMILY_THRESHOLD}
        for pid, have in active.items():
            missing = sorted(expected - have)
            if missing:
                add('K1b Profile missing customer-common fields (ADO 8137)', 'Medium',
                    'Customer', cust, pname(pid),
                    f'{len(missing)} field(s) set by at least {int(FAMILY_THRESHOLD*100)}% of this '
                    f'customer\'s other profiles are not set here.',
                    dest_list(missing),
                    profile_id=pid, fields=[dest_str(d) for d in sorted(missing)])

    # --- K2 : ADO 8177 (Profile Localization & Values Completeness Detection)
    # Named local entities embedded directly in rule values. These are the things
    # that cannot sit at System level unchanged, so each one is a localization that
    # has not been made.
    LOCAL_TOKENS = ['REGENCY','DOBLER','DOBBLER','CRANESVILLE','CRANESV','LAKE CITY','GIRARD',
                    'MCKEAN','CECPA','CENTRAL ERIE','EDINBORO','FAIRVIEW','MILLCREEK',
                    'SPRINGBORO','FELLOWS','MACON','PUCKETT','WINDER','MARIETTA',
                    'MEDICAID (PA)','DPA','UPMC']
    for _, p in SPEC.iterrows():
        rules = PMR[PMR.ProfileId == p.ProfileId]
        sel   = PSR[PSR.ResultingProfile == p.ProfileName]
        blob = ' | '.join([str(p.ProfileName), str(p.ProfilePayers)] +
                          rules.Conditions.tolist() + rules.OutcomeValue.tolist() +
                          sel.Conditions.tolist()).upper()
        hits = sorted({t for t in LOCAL_TOKENS if t in blob})
        if hits:
            add('K2 Local entity not localized (ADO 8177)', 'Medium',
                p.Tier, p.Customer, p.ProfileName,
                f'Carries {len(hits)} named local entity value(s) inline. These cannot be '
                f'promoted to System level unchanged; each needs a localization.',
                ', '.join(hits),
                profile_id=p.ProfileId,
                anchors=[dest_key(r, r.Priority) for _, r in rules.iterrows()
                         if any(t in str(r.OutcomeValue).upper() or t in str(r.Conditions).upper()
                                for t in LOCAL_TOKENS)])

    # no localization anywhere - stated once, as a fact about the estate
    n_loc = (PMR.HasGroupLocalization == 'Yes').sum() + (PMR.IsRuleLocalization == 'Yes').sum()
    # Reported as an observation, not High severity. An earlier version called this
    # a High finding on the reasoning that "localization is unused". That reasoning
    # was wrong in the design analysis and is wrong here too: AIM handles
    # agency/customer inheritance in the configuration levels, so an agency
    # inherits its customer's configuration wherever it defines nothing. Absence of
    # localization records is the expected state for single-agency customers, not a
    # gap. What remains true and worth stating is the fact itself.
    add('K2 Localization records absent estate-wide (ADO 8177)', 'Low', 'All', 'All customers',
        'Estate-wide',
        f'HasGroupLocalization and IsRuleLocalization are "No" on all {len(PMR)} mapping rules. '
        f'Stated as a fact about the estate rather than a defect: an agency inherits its '
        f'customer configuration where it defines nothing, so for single-agency customers there '
        f'is nothing to localize.',
        f'localized rules found: {n_loc}')

    # --- K3 : ADO 8189 (Initial Onboarding Config of Standard Localizations)
    # The list a new agency would have to supply. Derived from the local values
    # actually in use, grouped by the field they populate.
    loc_fields = defaultdict(set)
    for _, r in PMR.iterrows():
        v = str(r.OutcomeValue).upper()
        for t in LOCAL_TOKENS:
            if t in v:
                loc_fields[dest(r)].add(str(r.OutcomeValue))
    for fld, vals in sorted(loc_fields.items()):
        add('K3 Field requiring a per-agency localization value (ADO 8189)', 'Medium',
            'Agency', 'All customers', dest_label(fld),
            f'This field is set to a named local value in production. A new agency onboarding '
            f'onto a standard profile would have to supply its own value.',
            ', '.join(sorted(vals)[:6]))

    # --- K4 : ADO 7813 (Profile Missing Rules (MR) Detection - Candidate Rule Generation)
    # For each K1 gap, propose the sibling rule that would fill it: the modal
    # condition/outcome pair among the profiles that DO set the field.
    candidates = []
    for (cust, fam), pids in sorted(fam_index.items()):
        if not cust or len(pids) < 3: continue
        active = {pid: {dest(r) for r in PMR[PMR.ProfileId == pid].itertuples()} for pid in pids}
        active = {p: f for p, f in active.items() if f}
        if len(active) < 3: continue
        counts = Counter()
        for f in active.values(): counts.update(f)
        expected = {fld for fld, n in counts.items() if n >= len(active) - 1}
        for pid, have in active.items():
            for fld in sorted(expected - have):
                sib = PMR[(PMR.ProfileId.isin(active.keys()))
                          & (PMR.Tab == fld[0]) & (PMR.TargetField == fld[1])]
                if not len(sib): continue
                modal = Counter(zip(sib.Conditions, sib.OutcomeValue, sib.Priority)).most_common(1)[0]
                (cond, out, pri), n = modal
                candidates.append(dict(
                    # fld is a (tab, field) destination; the sheet needs a string.
                    Customer=cust, Family=fam, Profile=pname(pid),
                    Tab=fld[0], Field=fld[1],
                    **{'Candidate condition': cond, 'Candidate sets': out, 'Priority': pri,
                       'Siblings agreeing': n,
                       'Siblings setting this field': len(sib)}))

    # A DESTINATION BILLING FIELD IS (Tab, TargetField), NOT TargetField.
    #
    # 'Emergency' exists on both the ANSI 5010 tab and the HCFA 1500 tab. They are
    # two different destination fields that happen to share a name. Keying on the
    # name alone made every profile setting both look like it held a duplicate:
    # 23 false duplicates, and zero real ones.
    #
    # It also works the other way - two rules that genuinely collide on one field
    # are only comparable when they share a tab as well as a name.

    return findings, candidates
