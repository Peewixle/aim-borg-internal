#!/usr/bin/env python3
"""
udbr_orgtree — building the AOH entity tree for a snapshot.

`org_id()` and the surrounding tree construction were duplicated verbatim
between udbr_load.py and udbr_load_design.py. Duplicated tree-building is
worse than most duplication, because the two copies can produce trees that
differ in parentage while both loading without error — and every per-agency
query then answers differently depending on which loader ran.
"""


class OrgTree:
    """Creates org_unit rows for one snapshot and remembers what it created, so
    the same entity is never inserted twice and parentage stays consistent."""

    def __init__(self, cur, snapshot_id):
        self.cur = cur
        self.snap = snapshot_id
        self._seen = {}
        # System has no name in the export. It is created explicitly so the
        # tree has a single root and no organization is left parentless.
        self.system_id = self.add('System', 'AIM (System)')

    def add(self, tier, name, parent=None):
        if not name:
            return None
        key = (tier, name)
        if key not in self._seen:
            self.cur.execute(
                'INSERT INTO org_unit (snapshot_id,tier,name,parent_id) VALUES (?,?,?,?)',
                (self.snap, tier, name, parent))
            self._seen[key] = self.cur.lastrowid
        return self._seen[key]

    def get(self, tier, name):
        return self._seen.get((tier, name))

    def owner_of(self, organization='', customer='', agency=''):
        """The org_unit a profile or rule belongs to: the innermost entity named
        on the row, falling back outward. A rule with no agency belongs to its
        customer, not to nothing."""
        return (self.get('Agency', (agency or '').strip())
                or self.get('Customer', (customer or '').strip())
                or self.get('Organization', (organization or '').strip())
                or self.system_id)

    def build_from_rows(self, rows):
        """rows: an iterable of objects carrying Organization / Customer /
        Agency. Builds the whole tree in one pass, parenting each level to the
        one above it as it goes."""
        for r in rows:
            o = self.add('Organization', str(getattr(r, 'Organization', '')).strip(),
                         self.system_id)
            c = self.add('Customer', str(getattr(r, 'Customer', '')).strip(), o)
            self.add('Agency', str(getattr(r, 'Agency', '')).strip(), c)
        return self

    def mirror(self, cur, base_snapshot_id):
        """Recreate the tree of an existing snapshot, preserving parentage.

        A design snapshot describes the same estate as the dump it derives
        from, so it must not invent a second, differently-shaped tree.
        Parentage is read back from the base rather than re-derived, because
        re-deriving it from a workbook that does not carry Agency columns
        produced agencies hanging off the wrong customer."""
        rows = cur.execute(
            """SELECT tier, name FROM org_unit WHERE snapshot_id=?
               ORDER BY CASE tier WHEN 'System' THEN 0 WHEN 'Organization' THEN 1
                                  WHEN 'Customer' THEN 2 ELSE 3 END""",
            (base_snapshot_id,)).fetchall()
        for tier, name in rows:
            if tier == 'System':
                continue
            if tier == 'Organization':
                self.add(tier, name, self.system_id)
            elif tier == 'Customer':
                org = next((self.get('Organization', n) for t, n in rows
                            if t == 'Organization'), self.system_id)
                self.add(tier, name, org)
            else:
                par = cur.execute(
                    """SELECT p.tier, p.name FROM org_unit a
                       JOIN org_unit p ON p.org_unit_id = a.parent_id
                       WHERE a.snapshot_id=? AND a.tier='Agency' AND a.name=?""",
                    (base_snapshot_id, name)).fetchone()
                self.add(tier, name, self.get(par[0], par[1]) if par else None)
        return self
