#!/usr/bin/env python3
"""
Where things live.

Every script hardcoded /home/claude/udbr/... and /mnt/user-data/..., which
works only in this container. Resolving them here means one edit to move the
pipeline, and each is overridable by environment variable so a test can point
at a temporary directory without touching the real store.
"""
import os

def _p(var, default):
    return os.environ.get(var, default)

ROOT     = _p('UDBR_ROOT',    os.path.dirname(os.path.abspath(__file__)))
DB       = _p('UDBR_DB',      os.path.join(ROOT, 'udbr.db'))
SCHEMA   = _p('UDBR_SCHEMA',  os.path.join(ROOT, 'udbr_schema.sql'))
PAYLOAD  = _p('UDBR_PAYLOAD', os.path.join(ROOT, 'udbr_data.json'))
SHELL    = _p('UDBR_SHELL',   os.path.join(ROOT, 'browser_shell.html'))
OUTPUTS  = _p('UDBR_OUTPUTS', '/mnt/user-data/outputs')
UPLOADS  = _p('UDBR_UPLOADS', '/mnt/user-data/uploads')

# Intermediates. Named here rather than spelled out at six call sites, which is
# how one script came to read a stale parse written by another.
FINDINGS_JSON   = _p('UDBR_FINDINGS',   '/tmp/findings.json')
DESIGN_JSON     = _p('UDBR_DESIGN',     '/tmp/design.json')
DESIGN_FINDINGS = _p('UDBR_DESIGN_FND', '/tmp/design_findings.json')
FINDINGS_TABLE  = _p('UDBR_FND_TABLE',  '/tmp/findings_table.json')
CAND_TABLE      = _p('UDBR_CAND_TABLE', '/tmp/candidates_table.json')

def out(name):
    return os.path.join(OUTPUTS, name)
