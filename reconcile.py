#!/usr/bin/env python3
"""
Load reconciliation. Every source row must land in the destination.

WHY IDENTIFIER MATCHING, NOT COUNTS
===================================
Counts are necessary and not sufficient. A loader that drops one row and
duplicates another gives the right total and the wrong data, and every derived
figure downstream - migration totals, findings, the Grid - is then internally
consistent and wrong. So this compares the actual SET of identifiers, both ways:

    source IDs missing from the destination  -> rows lost
    destination IDs absent from the source   -> rows invented or duplicated

Both are fatal. An incomplete snapshot is worse than no snapshot, because it
looks usable.

Runs on EVERY load, including a re-load of a file already in the store. The
early-exit path used to skip it, so verification only ever ran the first time a
file was seen.
"""
import sqlite3


def reconcile(db, snapshot_id, src_rule_ids, src_profile_ids=None, label=''):
    """src_rule_ids / src_profile_ids are SETS of identifiers read from the
    source file, not counts."""
    cx = sqlite3.connect(db)
    got_rules = {r[0] for r in cx.execute(
        'SELECT rule_guid FROM rule WHERE snapshot_id=?', (snapshot_id,))}
    got_profs = {r[0] for r in cx.execute(
        'SELECT profile_guid FROM profile WHERE snapshot_id=?', (snapshot_id,))}
    n_rules = cx.execute('SELECT COUNT(*) FROM rule WHERE snapshot_id=?',
                         (snapshot_id,)).fetchone()[0]
    n_profs = cx.execute('SELECT COUNT(*) FROM profile WHERE snapshot_id=?',
                         (snapshot_id,)).fetchone()[0]
    cx.close()

    src_rule_ids = set(src_rule_ids)
    problems = []

    # Duplicates make the count right and the set wrong, so check the row count
    # against the distinct-identifier count as well as against the source.
    if n_rules != len(got_rules):
        problems.append(f'rules: {n_rules} rows but {len(got_rules)} distinct '
                        f'identifiers - {n_rules - len(got_rules)} duplicated')
    if n_rules != len(src_rule_ids):
        problems.append(f'rules: source {len(src_rule_ids)}, loaded {n_rules}, '
                        f'{abs(len(src_rule_ids) - n_rules)} '
                        f'{"lost" if n_rules < len(src_rule_ids) else "extra"}')
    # Both directions, always. Checking only that every source id reached the
    # destination misses rows the destination holds that the source never had,
    # and 520-of-520 passing in one direction is what let a whole class of
    # error through unnoticed.
    lost = src_rule_ids - got_rules
    inv = got_rules - src_rule_ids
    if lost:
        problems.append(f'rules: {len(lost)} source identifiers absent from the '
                        f'destination, e.g. {sorted(lost)[:3]}')
    if inv:
        problems.append(f'rules: {len(inv)} destination identifiers not in the '
                        f'source, e.g. {sorted(inv)[:3]}')

    if src_profile_ids is not None:
        src_profile_ids = set(src_profile_ids)
        if n_profs != len(src_profile_ids):
            problems.append(f'profiles: source {len(src_profile_ids)}, loaded {n_profs}')
        plost = src_profile_ids - got_profs
        if plost:
            problems.append(f'profiles: {len(plost)} source identifiers absent, '
                            f'e.g. {sorted(plost)[:3]}')

    if problems:
        raise SystemExit(
            'LOAD RECONCILIATION FAILED' + (f' ({label})' if label else '') + ':\n  '
            + '\n  '.join(problems)
            + '\n\nThe snapshot is INCOMPLETE and must not be used. Delete it and fix '
              'the loader before anything reads from this store.')

    print(f'  reconciled: {n_rules}/{len(src_rule_ids)} rules'
          + (f', {n_profs}/{len(src_profile_ids)} profiles'
             if src_profile_ids is not None else '')
          + ' - every source identifier present, none invented')
    return True
