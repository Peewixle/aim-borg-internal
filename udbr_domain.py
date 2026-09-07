#!/usr/bin/env python3
"""
udbr_domain — the concepts the whole pipeline shares.

WHY THIS EXISTS
===============
A destination billing field is (Tab, TargetField), not TargetField. That fact
was reconstructed inline at roughly twenty sites across five files, and every
site was an independent opportunity to forget it. Several did:

  * four grouping keys in analyze.py            -> 23 false duplicate findings
  * four more index keys in the same file       -> a hidden completeness gap
  * seven keys in analyze_design.py             -> the same, on the design
  * the duplicate key in build_design.py        -> 23 real rules deleted from v10
  * two schema views joining on the field name  -> both broken outright

Each was fixed where it was pointed at, three times, and reported as done. The
fix kept failing because the CONCEPT had no home. It has one now: nothing below
should touch TargetField directly, so a bare field name has nowhere to enter.

The same reasoning covers the AOH tier order, which was hand-written in six
files, and the promote/demote direction logic, which existed in both Python and
a SQL CASE that could drift from it.
"""

# --------------------------------------------------------------- AOH tiers
#
# Firing order, outermost first. A rule at an earlier tier is overridden by the
# same field at a later one. Every sort, comparison and rank in the pipeline
# derives from this tuple rather than restating it.
TIERS = ('System', 'Organization', 'Customer', 'Agency')
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

# The same ordering as a SQL expression, so a query and the Python cannot
# disagree about which direction is "up".
TIER_RANK_SQL = ("CASE tier WHEN 'System' THEN 0 WHEN 'Organization' THEN 1 "
                 "WHEN 'Customer' THEN 2 ELSE 3 END")


def tier_rank(tier):
    """0 for System through 3 for Agency. Unknown tiers sort last rather than
    raising: a dump with an unexpected tier should be reported by a check, not
    crash the loader before any check runs."""
    return TIER_RANK.get(tier, len(TIERS))


def outermost(tiers, default=None):
    """The highest (most shared) tier in a collection - the tier a profile
    reaches. Used to describe a design profile whose rules span several."""
    ts = [t for t in tiers if t]
    return min(ts, key=tier_rank) if ts else default


def direction(from_tier, to_tier):
    """'promote' toward System, 'demote' toward Agency, 'unchanged' otherwise.
    One definition, so the migration script and the SQL that verifies it cannot
    disagree."""
    a, b = tier_rank(from_tier), tier_rank(to_tier)
    return 'unchanged' if a == b else ('promote' if b < a else 'demote')


# ---------------------------------------------------------- destinations
#
# A DESTINATION IS (tab, field). Never a field name alone.
#
# 'Emergency' exists on both the ANSI 5010 tab and the HCFA 1500 tab: two
# different places on the claim that happen to share a label. Any key, index,
# comparison or grouping that omits the tab merges them.

def dest(row):
    """The destination a rule writes to, from any of the shapes the pipeline
    carries: a pandas row from a dump, a parsed design rule, or a sqlite row."""
    for tab_k, fld_k in (('Tab', 'TargetField'),          # dump row
                         ('tab', 'field'),                 # parsed design rule
                         ('tab', 'target_field')):         # sqlite row
        try:
            tab = row[tab_k] if not hasattr(row, tab_k) else getattr(row, tab_k)
            fld = row[fld_k] if not hasattr(row, fld_k) else getattr(row, fld_k)
            if fld is not None:
                return (tab or '', fld)
        except (KeyError, TypeError, IndexError, AttributeError):
            continue
    raise KeyError(f'no destination on {type(row).__name__}: expected Tab/TargetField, '
                   f'tab/field or tab/target_field')


def dest_key(row, *extra):
    """A grouping key that always begins with the destination. Anything else
    that identifies the rule - priority, conditions, outcome - follows.

        dest_key(r, r.Priority, r.Conditions, r.OutcomeValue)
    """
    return dest(row) + tuple(extra)


def dest_label(d):
    """How a destination is written for a person: 'Site Code (Charges tab)'.

    Defined once because it was written out by hand in four places, which is
    how the same finding came to be phrased three different ways."""
    tab, fld = d
    return f'{fld} ({tab} tab)' if tab else str(fld)


def dest_list(dests, limit=6):
    """A readable list of destinations for a finding's evidence line."""
    out = [dest_label(d) for d in sorted(dests)]
    return ', '.join(out[:limit]) + ('...' if len(out) > limit else '')


def dest_str(d):
    """A stable string form for JSON payloads and cross-process keys, where a
    tuple cannot survive. Pipe-separated because neither tab nor field names
    contain a pipe."""
    return f'{d[0]}|{d[1]}'


def dest_from_str(s):
    tab, _, fld = str(s).partition('|')
    return (tab, fld)


# ------------------------------------------------------- dump validation

# The columns every analysis depends on. Not the full export - just the ones
# whose absence would change a result rather than merely lose a detail.
REQUIRED_COLUMNS = (
    'Section', 'Tier', 'Organization', 'Customer', 'Agency',
    'ProfileName', 'GroupType', 'ProfileId', 'RuleId',
    'Tab', 'TargetField', 'Priority', 'Conditions',
    'OutcomeType', 'OutcomeValue',
)

REQUIRED_SECTIONS = ('Profile', 'Profile Mapping Rule')


def validate_dump(df, source=''):
    """Raise if a dump cannot be analysed correctly. Returns the columns the
    export carried, so an unexpected addition is recorded rather than ignored.

    Fails loudly and early: the alternative is a KeyError three checks deep, or
    a grouping that silently matches nothing and reports a clean estate."""
    cols = list(df.columns)
    missing = [c for c in REQUIRED_COLUMNS if c not in cols]
    if missing:
        raise SystemExit(
            f'DUMP SHAPE UNRECOGNISED{f" ({source})" if source else ""}: '
            f'missing required column(s) {missing}.\n'
            f'The export format has changed. Every check depends on these; '
            f'analysing without them produces confident wrong answers rather '
            f'than an error.')
    if 'Section' in cols:
        seen = set(df['Section'].dropna().unique())
        absent = [s for s in REQUIRED_SECTIONS if s not in seen]
        if absent:
            raise SystemExit(f'DUMP INCOMPLETE{f" ({source})" if source else ""}: '
                             f'no rows of section {absent}.')
    extra = [c for c in cols if c not in REQUIRED_COLUMNS]
    return dict(columns=cols, unexpected=extra)
