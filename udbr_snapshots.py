#!/usr/bin/env python3
"""
udbr_snapshots — BG-71 (Snapshot Management from the Store).

THE STORE IS THE SOURCE OF TRUTH
================================
The snapshot list and every snapshot's data were baked into the page at build
time and the store was never read. A QA export loaded into the deployed store on
17 September 2026 did not appear: the store held five snapshots, the browser
showed three.

This module is the store side of the fix. The list is read when it is asked
for — not built into the page, not read once at startup — so a snapshot present
in the store appears without a rebuild and without a restart.

ONE SNAPSHOT'S DATA, NOT EVERY SNAPSHOT'S
=========================================
`data()` returns a single snapshot. The baked payload carried all of them at all
times, which is most of a 1.8 MB page for four snapshots of which one is being
looked at.

It also removes the cause of the v218 promote defect rather than guarding it.
The browser rendered baked production data while the studio edited a design in
the store, so a control computed from one snapshot's tiers was applied to
another's. Reading both from the same store makes the tier displayed the tier an
action applies to by construction.

DELETING IS NOT RETIRING
========================
A retired snapshot stays in the list, marked superseded — v9 is kept for
lineage, not for anyone to switch to. A snapshot loaded by mistake wants
removing. Two verbs, and collapsing them would mean either keeping rubbish or
losing lineage.

LOADING DOES NOT HOLD THE INTERFACE
===================================
A load is 400-600 rows parsed, reconciled and written across four tables. It
runs on a thread and reports progress, and what the loader says reaches the
screen: a refused shape names its missing columns, a refused banner date says
so, and the reconciliation count is carried through. A control that swallowed
those would be worse than the shell command it replaces.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

# Loads in flight, by id. A load outlives the request that started it, so its
# progress has to live somewhere the next request can read.
_LOADS = {}
_LOCK = threading.Lock()


def _cx(db):
    cx = sqlite3.connect(db)
    cx.row_factory = sqlite3.Row
    # REQUIRED for delete. SQLite has foreign keys OFF per connection, so
    # without this a deleted snapshot leaves its rules, profiles and findings
    # behind as orphans rather than cascading.
    cx.execute('PRAGMA foreign_keys = ON')
    return cx


def _retired(notes):
    return bool(notes and 'HISTORICAL' in str(notes).upper())


def snapshot_list(db):
    """Every snapshot in the store, with what is needed to choose between them.

    Read on demand. The counts come from the store rather than a cached payload
    so a snapshot loaded a moment ago is complete and correct here.
    """
    cx = _cx(db)
    out = []
    for s in cx.execute('''SELECT snapshot_id, kind, source_file, exported_at,
                                  loaded_at, derived_from, notes
                           FROM snapshot ORDER BY snapshot_id'''):
        sid = s['snapshot_id']
        prof = cx.execute('SELECT COUNT(*) FROM profile WHERE snapshot_id=?',
                          (sid,)).fetchone()[0]
        rules = cx.execute('SELECT COUNT(*) FROM rule WHERE snapshot_id=?',
                           (sid,)).fetchone()[0]
        # A design that edits or derives from this snapshot blocks its deletion.
        # Collected here so the surface can say WHY rather than only refusing.
        held = [dict(design=r['design_id'], name=r['name'])
                for r in cx.execute('''SELECT design_id, name FROM design_session
                                       WHERE snapshot_id=? OR base_snapshot=?''',
                                    (sid, sid))]
        out.append(dict(id=sid, kind=s['kind'], source=s['source_file'],
                        exportedAt=s['exported_at'], loadedAt=s['loaded_at'],
                        derivedFrom=s['derived_from'],
                        retired=_retired(s['notes']),
                        profiles=prof, rules=rules,
                        heldBy=held, deletable=not held))
    cx.close()
    return out


def available_files(db, data_dir):
    """The exports sitting on the data drive, ready to load.

    The file is placed there outside the Borg, so this lists rather than
    receives. Already-loaded files are MARKED, not hidden: loading one twice is
    a mistake worth preventing and occasionally a thing someone means to do.
    """
    cx = _cx(db)
    loaded = {os.path.basename(r[0]) for r in
              cx.execute('SELECT source_file FROM snapshot')}
    cx.close()
    out = []
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return out
    for n in names:
        if not n.lower().endswith(('.xlsx', '.xls')):
            continue
        p = os.path.join(data_dir, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append(dict(name=n, bytes=st.st_size,
                        modified=time.strftime('%Y-%m-%d %H:%M',
                                               time.localtime(st.st_mtime)),
                        alreadyLoaded=n in loaded))
    return out


def start_load(db, data_dir, filename, loader_dir):
    """Begin a load on a thread and return its id.

    Does not hold the interface: the caller polls load_status(). Everything the
    loader prints is kept, because its refusals are the most useful thing it
    produces and none of them reached a screen before.
    """
    if os.path.basename(filename) != filename:
        raise ValueError('filename must not contain a path')
    src = os.path.join(data_dir, filename)
    if not os.path.exists(src):
        raise FileNotFoundError(filename)

    lid = f'load-{int(time.time() * 1000)}'
    with _LOCK:
        _LOADS[lid] = dict(id=lid, file=filename, state='running',
                           started=time.time(), output=[], snapshot=None,
                           error=None)

    def run():
        try:
            p = subprocess.run(
                [sys.executable, os.path.join(loader_dir, 'udbr_load.py'), src, db],
                capture_output=True, text=True, timeout=900)
            lines = [l for l in (p.stdout + p.stderr).splitlines() if l.strip()]
            with _LOCK:
                rec = _LOADS[lid]
                rec['output'] = lines
                if p.returncode == 0:
                    rec['state'] = 'done'
                    m = re.search(r'snapshot (\d+):', p.stdout)
                    rec['snapshot'] = int(m.group(1)) if m else None
                else:
                    # A refusal is a RESULT, not a crash. The loader's own words
                    # go to the screen: the missing columns it named, or the
                    # banner date it could not read.
                    rec['state'] = 'failed'
                    rec['error'] = lines[0] if lines else f'exit {p.returncode}'
        except Exception as e:                       # noqa: BLE001
            with _LOCK:
                _LOADS[lid].update(state='failed', error=f'{type(e).__name__}: {e}')

    threading.Thread(target=run, daemon=True).start()
    return lid


def load_status(lid):
    with _LOCK:
        rec = _LOADS.get(lid)
        return dict(rec) if rec else None


def delete(db, snapshot_id):
    """Remove a snapshot, its profiles, rules and the findings computed on it.

    Refuses when a design edits or derives from it, naming the design. That is
    the base lock one step further in: BG-61 stops a base being superseded while
    a design derives from it, and deleting it outright is the same hazard.
    """
    cx = _cx(db)
    s = cx.execute('SELECT * FROM snapshot WHERE snapshot_id=?',
                   (snapshot_id,)).fetchone()
    if s is None:
        cx.close()
        raise ValueError(f'no snapshot {snapshot_id}')

    held = [dict(design=r['design_id'], name=r['name'])
            for r in cx.execute('''SELECT design_id, name FROM design_session
                                   WHERE snapshot_id=? OR base_snapshot=?''',
                                (snapshot_id, snapshot_id))]
    if held:
        cx.close()
        names = ', '.join(f"{h['name']} (design {h['design']})" for h in held)
        raise PermissionError(
            f'snapshot {snapshot_id} cannot be deleted: {names} '
            f'{"edits it or derives from it" if len(held) == 1 else "edit it or derive from it"}')

    counts = dict(
        profiles=cx.execute('SELECT COUNT(*) FROM profile WHERE snapshot_id=?',
                            (snapshot_id,)).fetchone()[0],
        rules=cx.execute('SELECT COUNT(*) FROM rule WHERE snapshot_id=?',
                         (snapshot_id,)).fetchone()[0],
        findings=cx.execute('''SELECT COUNT(*) FROM finding f
                               JOIN analysis_run a ON a.run_id = f.run_id
                               WHERE a.snapshot_id=?''',
                            (snapshot_id,)).fetchone()[0])
    with cx:
        cx.execute('DELETE FROM snapshot WHERE snapshot_id=?', (snapshot_id,))
    cx.close()
    return dict(deleted=snapshot_id, source=s['source_file'], **counts)


def retire(db, snapshot_id, on=True):
    """Mark a snapshot superseded, or un-mark it. NOT a delete.

    It stays in the list and stays selectable; what changes is that it is
    labelled, so the picker does not offer a superseded input as a peer of the
    design that replaced it.
    """
    cx = _cx(db)
    s = cx.execute('SELECT notes FROM snapshot WHERE snapshot_id=?',
                   (snapshot_id,)).fetchone()
    if s is None:
        cx.close()
        raise ValueError(f'no snapshot {snapshot_id}')
    notes = (s['notes'] or '').strip()
    has = _retired(notes)
    if on and not has:
        notes = (notes + ' HISTORICAL').strip()
    elif not on and has:
        notes = re.sub(r'\s*HISTORICAL\s*', ' ', notes, flags=re.I).strip()
    with cx:
        cx.execute('UPDATE snapshot SET notes=? WHERE snapshot_id=?',
                   (notes or None, snapshot_id))
    cx.close()
    return dict(snapshot=snapshot_id, retired=bool(on))


def snapshot_payload(db, snapshot_id):
    """One snapshot's profiles, rules, defaults and selection rules.

    Read when that snapshot is SELECTED. The baked payload carried every
    snapshot's data at all times — most of a 1.8 MB page for four snapshots of
    which one is being looked at.

    Reuses the payload builder rather than reimplementing it: a second
    implementation of the same shaping is how the trace and the batch analyzer
    came to disagree about what a destination was.
    """
    # Imported from this module's own directory. sys.path order is not ours
    # to rely on: a stale copy elsewhere on the path was picked up instead, and
    # it ran its script body on import against a database that did not exist.
    import importlib.util
    _here = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db_to_payload.py')
    _spec = importlib.util.spec_from_file_location('_udbr_payload_builder', _here)
    B = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(B)
    cx = _cx(db)
    try:
        data = B.build(snapshot_id, cx)
    finally:
        cx.close()
    return data
