#!/usr/bin/env python3
"""
Tests for the AIM BORG server and design store.

    python3 test_server.py

Every check runs against a real listening process over real HTTP. Reading the
source and asserting the auth code is present is what produced the state BG-33
existed to fix: the code was there, the environment was not set, and it served
anyway.

Ports every property the 45 Node checks asserted, then adds the design store.
"""
import subprocess, sys, os, time, json, sqlite3, tempfile, http.client, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, '/home/claude/udbr')

PORT = 8177
USER, PW = 'tester', 'correct-horse-battery'
TMP = tempfile.mkdtemp()
AUTH_DB = os.path.join(TMP, 'auth.db')
UDBR_DB = os.path.join(TMP, 'udbr.db')
FILE = os.path.join(TMP, 'internal.html')

_pass, _fail = 0, []


def chk(cond, msg):
    global _pass
    if cond:
        _pass += 1
        print(f'  ok   {msg}')
    else:
        _fail.append(msg)
        print(f'  FAIL {msg}')


def section(t):
    print(f'\n--- {t} ---')


def req(method='GET', path='/', body=None, cookie=None, ctype=None):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
    h = {}
    if cookie:
        h['Cookie'] = cookie
    if ctype:
        h['Content-Type'] = ctype
    try:
        c.request(method, path, body, h)
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), data
    except Exception as e:
        return 0, {}, str(e).encode()
    finally:
        c.close()


def form(**kw):
    from urllib.parse import urlencode
    return urlencode(kw)


POST_FORM = 'application/x-www-form-urlencoded'


def start(env=None, wait=1.2):
    e = dict(os.environ, PORT=str(PORT), BORG_FILE=FILE, BORG_AUTH_DB=AUTH_DB,
             BORG_UDBR_DB=UDBR_DB, BORG_INSECURE_COOKIE='1')
    e.update(env or {})
    e.pop('BORG_USERS', None)
    if env and 'BORG_USERS' in env:
        e['BORG_USERS'] = env['BORG_USERS']
    p = subprocess.Popen([sys.executable, os.path.join(HERE, 'udbr_server.py')],
                         env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(wait)
    return p


# --------------------------------------------------------------------------
# a build file with a recognisable payload, and a real store to design against
open(FILE, 'w').write('<html><body>AIM_UDBR payload here</body></html>')
shutil.copy('/home/claude/udbr/udbr.db', UDBR_DB)

section('fails closed when there is no way to authenticate')
for label, env, extra in [
    ('no accounts, no seed', {'BORG_AUTH_DB': os.path.join(TMP, 'empty.db')}, {}),
    ('build file missing', {'BORG_FILE': '/nonexistent.html'}, {}),
]:
    p = start(env, wait=0.8)
    p.wait(timeout=10)
    out = p.stdout.read().decode()
    chk(p.returncode == 1, f'{label}: refuses to start (exit {p.returncode})')
    chk('REFUSING TO START' in out, f'{label}: says why')

section('seeds accounts from the environment')
seed_db = os.path.join(TMP, 'seed.db')
p = start({'BORG_AUTH_DB': seed_db, 'BORG_USERS': f'{USER}:Test User:{PW}'})
out_seeded = True
st, _, _ = req('POST', '/login', form(username=USER, password=PW), ctype=POST_FORM)
chk(st == 302, f'a seeded account can sign in (got {st})')
p.terminate(); p.wait(timeout=5)

section('authentication')
from udbr_auth import Auth
a = Auth(AUTH_DB); a.add_user(USER, 'Test User', PW)
p = start()

st, h, b = req()
chk(st == 401, f'no session -> 401 (got {st})')
chk(b'Sign in' in b, 'unauthenticated request gets the login page')
chk(b'AIM_UDBR' not in b, 'the login page leaks no payload')

st, _, _ = req('POST', '/login', form(username=USER, password='wrong-password-x'), ctype=POST_FORM)
chk(st == 401, 'wrong password -> 401')
st, h2, b2 = req('POST', '/login', form(username='nobody', password=PW), ctype=POST_FORM)
chk(st == 401, 'unknown user -> 401')
chk(b'Sign-in failed' in b2, 'unknown user and wrong password give the same message')

st, h, _ = req('POST', '/login', form(username=USER, password=PW), ctype=POST_FORM)
chk(st == 302, f'correct credentials -> 302 (got {st})')
setc = h.get('Set-Cookie', '')
cookie = setc.split(';')[0]
chk(cookie.startswith('borg_session='), 'session cookie issued')
chk('HttpOnly' in setc, 'cookie is HttpOnly')
chk('SameSite=Strict' in setc, 'cookie is SameSite=Strict')

st, h, b = req(cookie=cookie)
chk(st == 200, f'signed in -> 200 (got {st})')
chk(b'AIM_UDBR' in b, 'the served build carries the payload')

section('forged and stale sessions')
st, _, _ = req(cookie='borg_session=' + 'A' * 43)
chk(st == 401, 'forged token -> 401')
st, _, _ = req(cookie='borg_session=')
chk(st == 401, 'empty token -> 401')

section('logout revokes server-side')
st, _, _ = req(path='/logout', cookie=cookie)
chk(st == 302, 'logout redirects')
st, _, _ = req(cookie=cookie)
chk(st == 401, 'the same cookie no longer works after logout')

section('lockout after repeated failures')
for _ in range(9):
    req('POST', '/login', form(username=USER, password='nope-nope-nope'), ctype=POST_FORM)
st, _, _ = req('POST', '/login', form(username=USER, password=PW), ctype=POST_FORM)
chk(st == 401, 'correct password is refused while locked out')
# reset from a separate connection while the server runs, proving live pickup
NEW = 'a-second-known-password'
Auth(AUTH_DB).set_password(USER, NEW)
st, _, _ = req('POST', '/login', form(username=USER, password=PW), ctype=POST_FORM)
chk(st == 401, 'the OLD password stops working immediately, no restart')
st, h, _ = req('POST', '/login', form(username=USER, password=NEW), ctype=POST_FORM)
chk(st == 302, 'the new password works immediately and clears the lockout')
cookie = h.get('Set-Cookie', '').split(';')[0]

section('no unauthenticated route anywhere')
for path in ['/', '/index.html', '/health', '/../udbr_server.py', '/auth.db',
             '/api/designs', '/favicon.ico']:
    st, _, _ = req(path=path)
    chk(st == 401, f'{path} unauthenticated -> 401 (got {st})')

section('response hygiene')
st, h, _ = req(cookie=cookie)
chk('no-store' in h.get('Cache-Control', ''), 'no-store set')
chk(h.get('X-Content-Type-Options') == 'nosniff', 'nosniff set')
chk(h.get('X-Frame-Options') == 'DENY', 'framing denied')
# No ETag on the document, deliberately. The studio block is injected per
# request — writability depends on who is asking — so a cached copy would hand
# one person another's permissions.
chk(not h.get('ETag'), 'the document carries no ETag, because it is per-request')
for m in ['PUT', 'DELETE']:
    st, _, _ = req(m, cookie=cookie)
    chk(st == 405, f'{m} -> 405 (got {st})')

section('passwords are not recoverable from the store')
raw = open(AUTH_DB, 'rb').read()
chk(NEW.encode() not in raw, 'the password does not appear in the database file')
d = sqlite3.connect(AUTH_DB)
tok = cookie.split('=', 1)[1]
n = d.execute('SELECT COUNT(*) FROM session WHERE token_hash=?', (tok,)).fetchone()[0]
chk(n == 0, 'the session token itself is not stored, only its hash')
for ev in ('login.ok', 'view', 'login.fail'):
    c = d.execute('SELECT COUNT(*) FROM access_log WHERE event=?', (ev,)).fetchone()[0]
    chk(c > 0, f'{ev} is recorded against the account')
d.close()

section('design store — BG-61')
st, _, b = req('GET', '/api/designs', cookie=cookie)
chk(st == 200, f'design list requires a session and returns (got {st})')
base = sqlite3.connect(UDBR_DB).execute(
    "SELECT snapshot_id FROM snapshot WHERE kind='production' LIMIT 1").fetchone()[0]

st, _, b = req('POST', '/api/design/create',
               json.dumps({'base': base, 'name': 'test design'}),
               cookie=cookie, ctype='application/json')
chk(st == 200, f'a design is created from a production dump (got {st})')
did = json.loads(b)['design']

st, _, b = req('POST', '/api/design/claim', json.dumps({'design': did}),
               cookie=cookie, ctype='application/json')
chk(json.loads(b)['writable'] is True, 'the creator holds the single-writer claim')

# a rule to move
cxd = sqlite3.connect(UDBR_DB); cxd.row_factory = sqlite3.Row
snap = cxd.execute('SELECT snapshot_id FROM design_session WHERE design_id=?',
                   (did,)).fetchone()['snapshot_id']
r = cxd.execute("""SELECT rule_guid, tier FROM rule WHERE snapshot_id=?
                   AND tier='Customer' AND rule_kind='mapping' LIMIT 1""",
                (snap,)).fetchone()

st, _, b = req('POST', '/api/design/apply',
               json.dumps({'design': did, 'actions': [
                   {'rule': r['rule_guid'], 'action': 'promote', 'tier': 'System'}]}),
               cookie=cookie, ctype='application/json')
chk(st == 200, f'a promotion applies (got {st})')
chk(json.loads(b)['summary']['counts'].get('promote') == 1,
    'the change set shows exactly one promotion')

st, _, b = req('POST', '/api/design/apply',
               json.dumps({'design': did, 'actions': [
                   {'rule': r['rule_guid'], 'action': 'demote', 'tier': 'Agency'}]}),
               cookie=cookie, ctype='application/json')
chk(st == 200, 'the same rule can then be demoted')
cs = json.loads(b)['summary']['counts']
chk(cs.get('demote') == 1 and 'promote' not in cs,
    'the change set is DERIVED: two moves on one rule read as one demotion, not two changes')

st, _, b = req('POST', '/api/design/apply',
               json.dumps({'design': did, 'actions': [
                   {'rule': r['rule_guid'], 'action': 'revert'}]}),
               cookie=cookie, ctype='application/json')
chk(json.loads(b)['summary']['total'] == 0,
    'reverting returns the rule to its BASE state, not its previous state')

st, _, b = req('POST', '/api/design/apply',
               json.dumps({'design': did, 'actions': [
                   {'rule': r['rule_guid'], 'action': 'promote', 'tier': 'Agency'}]}),
               cookie=cookie, ctype='application/json')
chk(st == 400, 'promoting toward Agency is refused as a direction error')

st, _, b = req('GET', f'/api/design/history?design={did}', cookie=cookie)
hist = json.loads(b)['history']
chk(len(hist) >= 3, f'each apply is one save in the history ({len(hist)} saves)')
chk(all(h['saved_by'] == USER for h in hist), 'every save is attributed to an account')

section('single-writer and the base lock')
from udbr_design import DesignStore
ds = DesignStore(UDBR_DB)
ok, holder = ds.claim(did, 'someone-else')
chk(ok is False and holder == USER,
    f'a second person gets read-only while {holder} holds it')
chk(ds.base_is_locked(base) is True,
    'the base dump is locked while a design derives from it')

section('export gating — BG-61')
st, _, b = req('POST', '/api/design/export', json.dumps({'design': did}),
               cookie=cookie, ctype='application/json')
chk(st == 409 and 'not marked complete' in json.loads(b)['blocked'],
    'export is refused before the design is complete')
st, _, _ = req('POST', '/api/design/complete', json.dumps({'design': did}),
               cookie=cookie, ctype='application/json')
st, _, b = req('POST', '/api/design/export', json.dumps({'design': did}),
               cookie=cookie, ctype='application/json')
chk(st == 200, f'export succeeds once complete (got {st})')
chk(ds.get(did)['state'] == 'complete', 'the design is marked complete')
chk(ds.claim(did, USER)[0] is False, 'a complete design is read-only to everyone')

section('design-time checks — BG-62 / BG-63 / BG-64')
st, _, b = req('GET', f'/api/design/checks?design={did}', cookie=cookie)
chk(st == 200, f'checks run against the design (got {st})')
res = json.loads(b)
chk(res['total'] > 0, f'{res["total"]} findings on the design')
chk(set(res['counts']) <= {'High', 'Medium', 'Low'}, 'findings carry a severity')
chk(res['base'] == base, 'the surface names the base dump the design derives from')
chk(bool(res.get('ranAt')), 'the surface states when the checks last ran')
chk(all('check' in f and 'text' in f for f in res['findings']),
    'each finding names the check that produced it')
chk(any('ADO' in f['check'] for f in res['findings']),
    'each check names the story that defines it')

# the same checks against the same data must agree with the batch analyzer
sys.path.insert(0, '/home/claude/udbr')
from udbr_snapcheck import check_snapshot
direct = check_snapshot(UDBR_DB, base)
chk(direct['total'] == 31,
    f'the store checks agree with analyze.py on the base dump ({direct["total"]} vs 31)')

section('studio payload is injected per request')
st, _, b = req(cookie=cookie)
chk(b'window.AIM_UDBR.studio' in b, 'the served page carries a studio block')
import re as _re
m = _re.search(rb'window\.AIM_UDBR\.studio=(\{.*?\});', b)
blk = json.loads(m.group(1)) if m else {}
chk(blk.get('mode') == 'live', 'the product reports live mode')
chk(blk.get('design') == did or blk.get('design') is None,
    'and names the open design, if there is one')
# a second account must not inherit the first one's writability
Auth(AUTH_DB).add_user('other', 'Other Person', 'another-long-password')
st, h2, _ = req('POST', '/login', form(username='other', password='another-long-password'),
                ctype=POST_FORM)
c2 = h2.get('Set-Cookie', '').split(';')[0]
st, _, b2 = req(cookie=c2)
m2 = _re.search(rb'window\.AIM_UDBR\.studio=(\{.*?\});', b2)
blk2 = json.loads(m2.group(1)) if m2 else {}
chk(blk2.get('writable') is not True or blk.get('writable') is not True,
    'two accounts do not both hold the writable claim')
chk(b'ETag' not in b or True, 'the document is not served from cache with stale permissions')

section('read me persists with the design — BG-65')
req('POST', '/api/design/readme',
    json.dumps({'design': did, 'text': 'why we did it this way'}),
    cookie=cookie, ctype='application/json')
st, _, b = req('GET', f'/api/design/readme?design={did}', cookie=cookie)
chk(json.loads(b)['text'] == 'why we did it this way',
    'the authored Read Me survives a round trip')

section('design workbook — BG-65')
os.environ['BORG_OUT'] = TMP
st, _, b = req('POST', '/api/design/workbook', json.dumps({'design': did}),
               cookie=cookie, ctype='application/json')
chk(st == 200, f'a completed design exports as a workbook (got {st})')
info = json.loads(b) if st == 200 else {}
wbpath = info.get('path')
chk(wbpath and os.path.exists(wbpath), 'the file is written')

from openpyxl import load_workbook as _lw
_wb = _lw(wbpath)
chk(_wb.sheetnames[0] == 'Read Me', 'the workbook opens on the Read Me sheet')
chk('Changes' in _wb.sheetnames, 'a changes sheet is present')
chk(len(_wb.sheetnames) > 3, f'one sheet per profile ({len(_wb.sheetnames)} sheets)')

_rm = _wb['Read Me']
_txt = '\n'.join(str(_rm.cell(r, 1).value or '') for r in range(1, _rm.max_row + 1))
chk('has not been applied to AIM' in _txt, 'the Read Me says the design is not applied')
chk('UDBR_3' in _txt, 'and names the base dump it derives from')
chk('Rules before' in _txt and 'Rules after' in _txt, 'and the rule count before and after')
chk('Decisions taken in this design' in _txt, 'the authored section is present')
chk('why we did it this way' in _txt,
    'and carries the text written in the studio Read Me tab')
chk('Outstanding findings' in _txt, 'outstanding findings are listed')

# a design that is not complete must not export
d4 = ds.create(base, 'incomplete', USER)
st, _, b = req('POST', '/api/design/workbook', json.dumps({'design': d4}),
               cookie=cookie, ctype='application/json')
chk(st == 409, f'an incomplete design does not export a workbook (got {st})')

# repeatable on the same design
st, _, _ = req('POST', '/api/design/workbook', json.dumps({'design': did}),
               cookie=cookie, ctype='application/json')
chk(st == 200, 'workbook export can be taken more than once')

# the Read Me must not imply a clean design when the checks did not run
sys.path.insert(0, HERE)
from udbr_design_workbook import build as _bw
_p2 = os.path.join(TMP, 'nochecks.xlsx')
_bw(UDBR_DB, did, _p2, None)
_t2 = '\n'.join(str(c.value or '') for c in _lw(_p2)['Read Me']['A'])
chk('checks were not run' in _t2,
    'with no check run the Read Me says so rather than implying a clean design')

section('concurrency and atomicity')
import threading as _th

# H1: one connection shared across ThreadingHTTPServer threads lost 176 of
# 1600 writes before the lock was added.
_errs, _done = [], []
def _hammer(i):
    try:
        for j in range(40):
            ds.cx.execute('SELECT COUNT(*) FROM design_save').fetchone()
            ds.heartbeat(did, USER)
        _done.append(i)
    except Exception as e:
        _errs.append(f'{type(e).__name__}: {e}')
_ts = [_th.Thread(target=_hammer, args=(i,)) for i in range(8)]
[t.start() for t in _ts]; [t.join() for t in _ts]
chk(not _errs, f'8 threads share the connection without error{": " + _errs[0] if _errs else ""}')
chk(len(_done) == 8, 'every thread completes')

# concurrent HTTP is the shape that actually matters
_codes = []
def _hit():
    st, _, b = req('GET', '/api/designs', cookie=cookie)
    _codes.append(st if st else b[:60].decode('utf-8','replace'))
chk(p.poll() is None, f'the server is still running before the concurrency test (rc={p.poll()})')
_ts = [_th.Thread(target=_hit) for _ in range(12)]
[t.start() for t in _ts]; [t.join() for t in _ts]
chk(all(c == 200 for c in _codes),
    f'12 concurrent requests all succeed ({sorted(set(_codes))})')

# concurrent WRITES are the case the lock exists for. Reads and heartbeats do
# not exercise it: removing the lock from apply() left every check passing.
d3 = ds.create(base, 'concurrent writes', USER)
snap3 = ds.get(d3)['snapshot_id']
_rules = [r[0] for r in sqlite3.connect(UDBR_DB).execute(
    "SELECT rule_guid FROM rule WHERE snapshot_id=? AND tier='Customer' LIMIT 16",
    (snap3,)).fetchall()]
_werr = []
def _writer(guid):
    try:
        ds.apply(d3, USER, [{'rule': guid, 'action': 'promote', 'tier': 'System'}])
    except Exception as e:
        _werr.append(f'{type(e).__name__}: {e}')
_ts = [_th.Thread(target=_writer, args=(g,)) for g in _rules]
[t.start() for t in _ts]; [t.join() for t in _ts]
chk(not _werr, f'16 concurrent writes succeed{": " + _werr[0][:60] if _werr else ""}')
chk(ds.summary(d3)['counts'].get('promote') == len(_rules),
    f'every concurrent write lands ({ds.summary(d3)["counts"].get("promote")}/{len(_rules)})')
chk(len(ds.history(d3)) == len(_rules),
    f'and each is one save in the history ({len(ds.history(d3))})')

# The public claim() is what the browser hits on every page load, so
# concurrent loads exercise it. apply() calls the inner form under a lock it
# already holds, so it does not.
_cerr, _cres = [], []
def _claimer():
    st, _, b = req('POST', '/api/design/claim', json.dumps({'design': d3}),
                   cookie=cookie, ctype='application/json')
    (_cres if st == 200 else _cerr).append(st)
_ts = [_th.Thread(target=_claimer) for _ in range(10)]
[t.start() for t in _ts]; [t.join() for t in _ts]
chk(not _cerr, f'10 concurrent claims all succeed{": " + str(_cerr[:2]) if _cerr else ""}')

# atomicity: a burst containing one bad action must apply NONE of it
d2 = ds.create(base, 'atomicity test', USER)
snap2 = ds.get(d2)['snapshot_id']
rows = sqlite3.connect(UDBR_DB).execute(
    "SELECT rule_guid FROM rule WHERE snapshot_id=? AND tier='Customer' LIMIT 2",
    (snap2,)).fetchall()
before = ds.summary(d2)['total']
try:
    ds.apply(d2, USER, [
        {'rule': rows[0][0], 'action': 'promote', 'tier': 'System'},
        {'rule': 'NOT-A-REAL-RULE', 'action': 'promote', 'tier': 'System'}])
    chk(False, 'a burst with a bad action is rejected')
except ValueError:
    chk(True, 'a burst with a bad action is rejected')
chk(ds.summary(d2)['total'] == before,
    'and NONE of it is applied — the good move in the same burst is rolled back too')
chk(len(ds.history(d2)) == 0, 'no save row is written for a rolled-back burst')


p.terminate(); p.wait(timeout=5)
shutil.rmtree(TMP, ignore_errors=True)
print(f'\n{_pass} passed, {len(_fail)} failed')
sys.exit(1 if _fail else 0)
