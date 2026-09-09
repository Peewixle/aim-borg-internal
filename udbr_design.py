#!/usr/bin/env python3
"""
udbr_design — the durable design store behind BG-61.

WHAT A DESIGN IS
================
A design is a snapshot with kind='design' and derived_from naming its base
dump. That is the model the store already had before PDS existed, and v11 is in
it now. PDS replaces the editing surface, not the artifact.

THE CHANGE SET IS DERIVED, NOT RECORDED
=======================================
BG-61: "The change set is derived by comparing the design to its base dump. It
is not maintained as a separate record."

That is right, and it is the same reasoning that removed the JSON run history
this morning: a change set stored alongside the design is a second source of
truth for something computable, and the two drift. Comparing design to base is
one query, and it is the same derivation the migration script already uses and
that was verified against the generated SQL.

WHAT IS RECORDED IS THE SAVE HISTORY
====================================
Distinct from the change set. A save entry records that a burst of changes was
written at a time by a person. BG-62 attributes a conflict to "the change that
introduced it, using the design's history" — that history is this, and because
autosave collapses a burst, attribution lands on a save rather than on a single
change. BG-62 accounts for that: where history cannot establish a cause, the
finding names the rules without asserting one.

LOCKING
=======
BG-61: a base dump cannot be superseded, replaced or deleted while any design
derives from it, and a design is edited by one person at a time. Both are
enforced here rather than by convention.
"""
import sqlite3, json, secrets, threading
from datetime import datetime, timedelta, timezone

from udbr_domain import TIERS, tier_rank, direction, dest

# How long an editing claim survives without a heartbeat. Long enough that a
# slow save or a tab left open over lunch does not lose the claim; short enough
# that a crashed browser does not lock a design out for a day.
CLAIM_MINUTES = 30


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


SCHEMA = """
-- A design being edited. One row per design; the rules themselves live in the
-- rule table under this design's snapshot_id, exactly as a loaded design does.
CREATE TABLE IF NOT EXISTS design_session (
    design_id     INTEGER PRIMARY KEY,
    snapshot_id   INTEGER NOT NULL REFERENCES snapshot(snapshot_id) ON DELETE CASCADE,
    base_snapshot INTEGER NOT NULL REFERENCES snapshot(snapshot_id),
    name          TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- 'open' while being designed, 'complete' once marked finished. Completing
    -- releases the lock on the base dump.
    state         TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','complete')),
    completed_by  TEXT,
    completed_at  TEXT,
    -- Authored Read Me text (BG-65). Persists with the design and autosaves
    -- like any other change, so regenerating the workbook does not lose it.
    readme        TEXT NOT NULL DEFAULT ''
);

-- Who currently holds the single-writer claim. A row exists only while a
-- claim is held; a second person opening the design gets it read-only.
CREATE TABLE IF NOT EXISTS design_claim (
    design_id   INTEGER PRIMARY KEY REFERENCES design_session(design_id) ON DELETE CASCADE,
    username    TEXT NOT NULL,
    claimed_at  TEXT NOT NULL DEFAULT (datetime('now')),
    heartbeat   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Save history. NOT the change set — the change set is derived. This records
-- that a burst was written, by whom, and what it touched, so a finding can be
-- attributed to a save.
CREATE TABLE IF NOT EXISTS design_save (
    save_id    INTEGER PRIMARY KEY,
    design_id  INTEGER NOT NULL REFERENCES design_session(design_id) ON DELETE CASCADE,
    saved_by   TEXT NOT NULL,
    saved_at   TEXT NOT NULL DEFAULT (datetime('now')),
    n_actions  INTEGER NOT NULL,
    summary    TEXT
);

-- Which rules a save touched, so attribution can name them.
CREATE TABLE IF NOT EXISTS design_save_rule (
    save_id    INTEGER NOT NULL REFERENCES design_save(save_id) ON DELETE CASCADE,
    rule_guid  TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('promote','demote','delete','revert')),
    from_tier  TEXT,
    to_tier    TEXT,
    PRIMARY KEY (save_id, rule_guid)
);

CREATE INDEX IF NOT EXISTS ix_save_design ON design_save (design_id, saved_at);
"""


class DesignStore:
    def __init__(self, path):
        self.path = path
        # ONE CONNECTION SHARED ACROSS THREADS LOSES WRITES.
        #
        # The server is a ThreadingHTTPServer. Measured on this exact shape:
        # 8 threads, 1600 inserts, 176 lost, plus "cannot start a transaction
        # within a transaction". The lock serialises access.
        #
        # At this scale - a handful of Standards Managers - serialising costs
        # nothing, and a connection pool would be complexity bought for load
        # that does not exist.
        self._lock = threading.RLock()
        self.cx = sqlite3.connect(path, check_same_thread=False)
        self.cx.row_factory = sqlite3.Row
        self.cx.execute('PRAGMA foreign_keys = ON')
        self.cx.executescript(SCHEMA)
        self.cx.commit()

    # ------------------------------------------------------------ lifecycle
    def create(self, base_snapshot, name, username):
        """Start a design from a base dump: copy its rules and profiles into a
        new snapshot, which is then edited in place.

        ATOMIC. Roughly 600 inserts across four tables. A failure partway
        through would leave a snapshot holding part of a design, which is the
        same hazard the loaders guard against - an incomplete artifact that
        looks usable.
        """
        with self._lock:
            base = self.cx.execute(
                "SELECT * FROM snapshot WHERE snapshot_id=? AND kind='production'",
                (base_snapshot,)).fetchone()
            if base is None:
                raise ValueError(f'snapshot {base_snapshot} is not a production dump')
            try:
                cur = self.cx.cursor()
                cur.execute("""INSERT INTO snapshot
                      (kind, derived_from, source_file, exported_at, src_rows,
                       src_rules, src_profiles, row_count, sha256, notes)
                      VALUES ('design',?,?,?,?,?,?,?,?,?)""",
                    (base_snapshot, f'PDS: {name}', base['exported_at'],
                     base['src_rows'], base['src_rules'], base['src_profiles'],
                     base['row_count'], secrets.token_hex(16),
                     f'authored in the studio from {base["source_file"]}'))
                snap = cur.lastrowid

                # A design describes the same estate, so the org tree is
                # mirrored rather than re-derived.
                idmap = {}
                for o in self.cx.execute(
                        'SELECT * FROM org_unit WHERE snapshot_id=? ORDER BY org_unit_id',
                        (base_snapshot,)).fetchall():
                    cur.execute('INSERT INTO org_unit (snapshot_id,tier,name,parent_id) '
                                'VALUES (?,?,?,?)',
                                (snap, o['tier'], o['name'], idmap.get(o['parent_id'])))
                    idmap[o['org_unit_id']] = cur.lastrowid

                pmap = {}
                for pr in self.cx.execute('SELECT * FROM profile WHERE snapshot_id=?',
                                          (base_snapshot,)).fetchall():
                    cur.execute("""INSERT INTO profile (snapshot_id,profile_guid,name,
                          description,group_type,tier,org_unit_id,payers,
                          has_group_localization,parent_profile_guid,parent_tier)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (snap, pr['profile_guid'], pr['name'], pr['description'],
                         pr['group_type'], pr['tier'], idmap.get(pr['org_unit_id']),
                         pr['payers'], pr['has_group_localization'],
                         pr['parent_profile_guid'], pr['parent_tier']))
                    pmap[pr['profile_row_id']] = cur.lastrowid

                for r in self.cx.execute('SELECT * FROM rule WHERE snapshot_id=?',
                                         (base_snapshot,)).fetchall():
                    cur.execute("""INSERT INTO rule (snapshot_id,rule_guid,rule_kind,
                          profile_row_id,org_unit_id,tier,tab,target_field,
                          target_field_source,target_field_value_type,priority,
                          description,conditions_raw,is_unconditional,outcome_type,
                          outcome_value,outcome_raw,resulting_profile_name,payer_name,
                          payer_form_type,payer_timing,is_rule_localization)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (snap, r['rule_guid'], r['rule_kind'],
                         pmap.get(r['profile_row_id']), idmap.get(r['org_unit_id']),
                         r['tier'], r['tab'], r['target_field'],
                         r['target_field_source'], r['target_field_value_type'],
                         r['priority'], r['description'], r['conditions_raw'],
                         r['is_unconditional'], r['outcome_type'], r['outcome_value'],
                         r['outcome_raw'], r['resulting_profile_name'], r['payer_name'],
                         r['payer_form_type'], r['payer_timing'],
                         r['is_rule_localization']))

                cur.execute("""INSERT INTO design_session
                      (snapshot_id, base_snapshot, name, created_by) VALUES (?,?,?,?)""",
                    (snap, base_snapshot, name, username))
                did = cur.lastrowid
                self.cx.commit()
                return did
            except Exception:
                self.cx.rollback()
                raise

    def get(self, design_id):
        return self.cx.execute('SELECT * FROM design_session WHERE design_id=?',
                               (design_id,)).fetchone()

    def list(self):
        return [dict(r) for r in self.cx.execute("""
            SELECT d.*, s.source_file AS base_file,
                   (SELECT COUNT(*) FROM rule WHERE snapshot_id=d.snapshot_id) AS rules,
                   (SELECT username FROM design_claim c WHERE c.design_id=d.design_id) AS held_by
            FROM design_session d JOIN snapshot s ON s.snapshot_id=d.base_snapshot
            ORDER BY d.created_at DESC""")]

    # ------------------------------------------------------------ locking
    def claim(self, design_id, username):
        """Single-writer. Returns (writable, holder). An expired claim is taken
        over rather than left holding the design forever after a crash.

        Under the lock: this reads and writes on the shared connection, and
        concurrent claims interleaved badly enough that get() returned None and
        a caller saw 'no such design'. Found by hammering apply() from 16
        threads; reads and heartbeats alone never exposed it.
        """
        with self._lock:
            return self._claim(design_id, username)

    def _claim(self, design_id, username):
        d = self.get(design_id)
        if d is None:
            raise ValueError('no such design')
        if d['state'] == 'complete':
            return False, None          # complete designs are read-only to everyone
        row = self.cx.execute('SELECT * FROM design_claim WHERE design_id=?',
                              (design_id,)).fetchone()
        if row:
            stale = _now() - datetime.fromisoformat(row['heartbeat']) > timedelta(minutes=CLAIM_MINUTES)
            if row['username'] == username:
                self.heartbeat(design_id, username)
                return True, username
            if not stale:
                return False, row['username']
            self.cx.execute('DELETE FROM design_claim WHERE design_id=?', (design_id,))
        self.cx.execute('INSERT INTO design_claim (design_id,username) VALUES (?,?)',
                        (design_id, username))
        self.cx.commit()
        return True, username

    def heartbeat(self, design_id, username):
      with self._lock:
        self.cx.execute("""UPDATE design_claim SET heartbeat=datetime('now')
                           WHERE design_id=? AND username=?""", (design_id, username))
        self.cx.commit()

    def release(self, design_id, username):
      with self._lock:
        self.cx.execute('DELETE FROM design_claim WHERE design_id=? AND username=?',
                        (design_id, username))
        self.cx.commit()

    def base_is_locked(self, snapshot_id):
        """A base dump cannot be superseded, replaced or deleted while any
        design derives from it. Reading it is unaffected."""
        n = self.cx.execute("""SELECT COUNT(*) c FROM design_session
                               WHERE base_snapshot=?""", (snapshot_id,)).fetchone()['c']
        return n > 0

    # ------------------------------------------------------------ editing
    def _rule(self, design_id, rule_guid):
        d = self.get(design_id)
        return self.cx.execute('SELECT * FROM rule WHERE snapshot_id=? AND rule_guid=?',
                               (d['snapshot_id'], rule_guid)).fetchone()

    def apply(self, design_id, username, actions):
        """Apply a burst of actions as ONE save.

        A burst is one save because autosave collapses consecutive changes:
        twenty rules dragged to System is one history entry, not twenty a
        second apart. BG-61 requires that explicitly.

        And because it is one save it is also ONE transaction: a failure on the
        fifth of twenty moves must not leave four applied with no save row to
        say so.
        """
        with self._lock:
            ok, holder = self._claim(design_id, username)
            if not ok:
                raise PermissionError(
                    f'design is held by {holder or "another session"}')
            try:
                d = self.get(design_id)
                # A DESIGN IS THE ONLY THING THE STUDIO EDITS.
                #
                # Belt as well as braces: the client refuses authoring off the
                # design, but a rule identifier posted directly must not be
                # able to touch a production snapshot. Production is what AIM
                # holds; changing it is the migration script's job, not the
                # studio's.
                kind = self.cx.execute('SELECT kind FROM snapshot WHERE snapshot_id=?',
                                       (d['snapshot_id'],)).fetchone()
                if kind and kind['kind'] != 'design':
                    raise PermissionError(
                        f'design {design_id} points at a {kind["kind"]} snapshot; '
                        f'the studio only edits designs')
                touched = []
                for a in actions:
                    guid, act = a['rule'], a['action']
                    r = self._rule(design_id, guid)
                    if r is None:
                        raise ValueError(f'rule {guid} is not in this design')
                    frm = r['tier']

                    if act == 'delete':
                        self.cx.execute('DELETE FROM rule WHERE rule_row_id=?',
                                        (r['rule_row_id'],))
                        touched.append((guid, 'delete', frm, None))
                    elif act in ('promote', 'demote'):
                        to = a['tier']
                        if to not in TIERS:
                            raise ValueError(f'unknown tier {to}')
                        want = direction(frm, to)
                        if want != act:
                            raise ValueError(f'{act} from {frm} to {to} is a {want}')
                        self.cx.execute('UPDATE rule SET tier=? WHERE rule_row_id=?',
                                        (to, r['rule_row_id']))
                        touched.append((guid, act, frm, to))
                    elif act == 'revert':
                        # Not sequential undo. A rule changed more than once
                        # returns to its BASE state, not its previous state.
                        # BG-61 says so, and it falls out of a derived change
                        # set: there is no per-change history to step back
                        # through.
                        self._revert(design_id, guid)
                        touched.append((guid, 'revert', frm, None))
                    else:
                        raise ValueError(f'unknown action {act}')

                cur = self.cx.cursor()
                summary = ', '.join(sorted({t[1] for t in touched})) or 'no change'
                cur.execute("""INSERT INTO design_save
                               (design_id,saved_by,n_actions,summary) VALUES (?,?,?,?)""",
                            (design_id, username, len(touched), summary))
                sid = cur.lastrowid
                for guid, act, frm, to in touched:
                    cur.execute("""INSERT OR REPLACE INTO design_save_rule
                                   (save_id,rule_guid,action,from_tier,to_tier)
                                   VALUES (?,?,?,?,?)""", (sid, guid, act, frm, to))
                self.cx.commit()
                return sid, len(touched)
            except Exception:
                self.cx.rollback()
                raise

    def _revert(self, design_id, rule_guid):
        """Return one rule to its state in the base dump."""
        d = self.get(design_id)
        base = self.cx.execute('SELECT * FROM rule WHERE snapshot_id=? AND rule_guid=?',
                               (d['base_snapshot'], rule_guid)).fetchone()
        if base is None:
            # Not in the base: it was created in the design, so reverting removes it.
            self.cx.execute('DELETE FROM rule WHERE snapshot_id=? AND rule_guid=?',
                            (d['snapshot_id'], rule_guid))
            return
        cur = self.cx.execute('SELECT rule_row_id FROM rule WHERE snapshot_id=? AND rule_guid=?',
                              (d['snapshot_id'], rule_guid)).fetchone()
        if cur:
            self.cx.execute('UPDATE rule SET tier=? WHERE rule_row_id=?',
                            (base['tier'], cur['rule_row_id']))
        else:
            # Deleted in the design: put it back by copying the base row.
            self._copy_rule_from_base(d, base)

    def _copy_rule_from_base(self, d, base):
        prof = self.cx.execute("""SELECT p2.profile_row_id FROM profile p1
              JOIN profile p2 ON p2.profile_guid = p1.profile_guid AND p2.snapshot_id=?
              WHERE p1.profile_row_id=?""",
              (d['snapshot_id'], base['profile_row_id'])).fetchone()
        self.cx.execute("""INSERT INTO rule (snapshot_id,rule_guid,rule_kind,profile_row_id,
              org_unit_id,tier,tab,target_field,priority,description,conditions_raw,
              is_unconditional,outcome_type,outcome_value,is_rule_localization)
              VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,0)""",
            (d['snapshot_id'], base['rule_guid'], base['rule_kind'],
             prof['profile_row_id'] if prof else None, base['tier'], base['tab'],
             base['target_field'], base['priority'], base['description'],
             base['conditions_raw'], base['is_unconditional'], base['outcome_type'],
             base['outcome_value']))

    # ------------------------------------------------------------ change set
    def change_set(self, design_id):
        """Derived by comparing the design to its base. Not a stored record.

        The same derivation the migration script uses, and it matched the
        generated SQL exactly when checked against v11.
        """
        d = self.get(design_id)
        rows = self.cx.execute("""
            SELECT b.rule_guid, b.tier AS from_tier, x.tier AS to_tier,
                   b.tab, b.target_field, b.priority, p.name AS profile
            FROM rule b
            JOIN profile p ON p.profile_row_id = b.profile_row_id
            LEFT JOIN rule x ON x.rule_guid = b.rule_guid AND x.snapshot_id = ?
            WHERE b.snapshot_id = ? AND b.rule_kind='mapping'
        """, (d['snapshot_id'], d['base_snapshot'])).fetchall()
        out = []
        for r in rows:
            if r['to_tier'] is None:
                act = 'delete'
            else:
                act = direction(r['from_tier'], r['to_tier'])
                if act == 'unchanged':
                    continue
            out.append(dict(rule=r['rule_guid'], action=act, profile=r['profile'],
                            tab=r['tab'], field=r['target_field'], priority=r['priority'],
                            from_tier=r['from_tier'], to_tier=r['to_tier']))
        return out

    def summary(self, design_id):
        cs = self.change_set(design_id)
        counts = {}
        for c in cs:
            counts[c['action']] = counts.get(c['action'], 0) + 1
        d = self.get(design_id)
        return dict(design_id=design_id, name=d['name'], state=d['state'],
                    base=d['base_snapshot'], counts=counts, total=len(cs))

    # ------------------------------------------------------------ history
    def history_rules(self, design_id, save_id=None):
        """The rules a save touched, with their destination so a reviewer can
        read them rather than a list of GUIDs.

        `contested` marks a rule touched by more than one save. That is the
        number worth seeing: 42 of the RCM team's promotions were moved back
        down by the consistency pass, and the net change set cannot show it —
        it reports where a rule ended up, not that a decision was reversed.
        """
        d = self.get(design_id)
        with self._lock:
            rows = self.cx.execute("""
                SELECT sr.save_id, sr.rule_guid, sr.action, sr.from_tier, sr.to_tier,
                       s.saved_by, s.saved_at,
                       COALESCE(b.tab, x.tab, '')                   AS tab,
                       COALESCE(b.target_field, x.target_field, '')  AS field,
                       COALESCE(bp.name, xp.name, '')                AS profile,
                       (SELECT COUNT(DISTINCT o.save_id) FROM design_save_rule o
                        JOIN design_save os ON os.save_id = o.save_id
                        WHERE o.rule_guid = sr.rule_guid AND os.design_id = ?) AS touches
                FROM design_save_rule sr
                JOIN design_save s ON s.save_id = sr.save_id
                LEFT JOIN rule b  ON b.rule_guid = sr.rule_guid AND b.snapshot_id = ?
                LEFT JOIN profile bp ON bp.profile_row_id = b.profile_row_id
                LEFT JOIN rule x  ON x.rule_guid = sr.rule_guid AND x.snapshot_id = ?
                LEFT JOIN profile xp ON xp.profile_row_id = x.profile_row_id
                WHERE s.design_id = ?
                  AND (? IS NULL OR sr.save_id = ?)
                ORDER BY sr.save_id, COALESCE(bp.name, xp.name), tab, field
            """, (design_id, d['base_snapshot'], d['snapshot_id'], design_id,
                  save_id, save_id)).fetchall()
        return [dict(save=r['save_id'], rule=r['rule_guid'], action=r['action'],
                     from_tier=r['from_tier'], to_tier=r['to_tier'],
                     by=r['saved_by'], at=r['saved_at'], tab=r['tab'],
                     field=r['field'], profile=r['profile'],
                     contested=r['touches'] > 1) for r in rows]

    def history(self, design_id, limit=50):
        return [dict(r) for r in self.cx.execute("""
            SELECT s.save_id, s.saved_by, s.saved_at, s.n_actions, s.summary
            FROM design_save s WHERE s.design_id=? ORDER BY s.saved_at DESC LIMIT ?""",
            (design_id, limit))]

    def attribute(self, design_id, rule_guids):
        """Which save last touched each of these rules. BG-62 attributes a
        conflict to the change that introduced it; because a save can hold a
        burst, this names a save, not a single change."""
        if not rule_guids:
            return {}
        q = ','.join('?' * len(rule_guids))
        rows = self.cx.execute(f"""
            SELECT r.rule_guid, MAX(s.save_id) AS save_id, s.saved_by, s.saved_at
            FROM design_save_rule r JOIN design_save s ON s.save_id = r.save_id
            WHERE s.design_id=? AND r.rule_guid IN ({q})
            GROUP BY r.rule_guid""", (design_id, *rule_guids)).fetchall()
        return {r['rule_guid']: dict(save=r['save_id'], by=r['saved_by'],
                                     at=r['saved_at']) for r in rows}

    # ------------------------------------------------------------ complete
    def complete(self, design_id, username):
        self.cx.execute("""UPDATE design_session SET state='complete', completed_by=?,
                           completed_at=datetime('now') WHERE design_id=?""",
                        (username, design_id))
        self.cx.execute('DELETE FROM design_claim WHERE design_id=?', (design_id,))
        self.cx.commit()

    def set_readme(self, design_id, text):
        self.cx.execute('UPDATE design_session SET readme=? WHERE design_id=?',
                        (text, design_id))
        self.cx.commit()

    # ------------------------------------------------------------ export gate
    def export_blocked_reason(self, design_id):
        """BG-61: a design containing a demotion that created rules at more than
        one agency cannot be exported, because expressing those rules needs a
        create verb AIM does not provide. The design itself is unaffected.

        Returns a reason string, or None when export is allowed.
        """
        d = self.get(design_id)
        if d['state'] != 'complete':
            return 'the design is not marked complete'
        multi = self.cx.execute("""
            SELECT COUNT(*) c FROM (
              SELECT x.rule_guid FROM rule x
              WHERE x.snapshot_id=? AND x.tier='Agency'
              GROUP BY x.rule_guid HAVING COUNT(*) > 1)""",
            (d['snapshot_id'],)).fetchone()['c']
        if multi:
            return (f'{multi} rule(s) were copied down to more than one agency. '
                    f'Expressing those needs a create verb AIM does not yet provide.')
        return None
