#!/usr/bin/env python3
"""
AIM BORG — server.

Replaces the Node server. One language on the backend, because the work the
server actually does is Python: the conflict and completeness checks, reading a
UDBR dump, and generating the design workbook BG-65 specifies. Keeping Node
would have meant two runtimes with a process boundary between them, or a second
implementation of the checks — and two implementations of these specific checks
demonstrably diverged.

NO DEPENDENCIES. http.server, sqlite3, hashlib and secrets are standard
library, so there is no build step and nothing to keep patched.

FAILS CLOSED. With no accounts and no seed the process exits rather than
serving. The state this replaces was worse than no authentication: the code
supported it, the environment was never set, and it served anyway.
"""
import os, sys, json, html, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, '/home/claude/udbr')

from udbr_auth import Auth, SESSION_HOURS, ABSOLUTE_HOURS
from udbr_design import DesignStore
from udbr_snapcheck import check_snapshot
from udbr_design_workbook import build as build_workbook
from udbr_trace import trace as build_trace, rule_detail, source_map, set_review
from udbr_selection import payer_rules, profile_rules

PORT     = int(os.environ.get('PORT', 8080))
FILE     = os.environ.get('BORG_FILE', 'internal.html')
AUTH_DB  = os.environ.get('BORG_AUTH_DB', 'auth.db')
UDBR_DB  = os.environ.get('BORG_UDBR_DB', 'udbr.db')
SEED     = os.environ.get('BORG_USERS')
COOKIE   = 'borg_session'
SECURE   = os.environ.get('BORG_INSECURE_COOKIE') != '1'

if not os.path.exists(FILE):
    print(f'REFUSING TO START: build not found at {FILE}', file=sys.stderr)
    sys.exit(1)

auth = Auth(AUTH_DB)
seeded = auth.seed_from_env(SEED)
if seeded:
    print(f'seeded {seeded} account(s) from BORG_USERS')
if auth.count() == 0:
    print('REFUSING TO START: no accounts exist and BORG_USERS is not set.', file=sys.stderr)
    print('This build carries production customer configuration and will not be',
          file=sys.stderr)
    print('served without authentication. Set BORG_USERS, or run: python3 udbr_users.py add',
          file=sys.stderr)
    sys.exit(1)

designs = DesignStore(UDBR_DB) if os.path.exists(UDBR_DB) else None
BODY = open(FILE, 'rb').read()
import hashlib
ETAG = '"' + hashlib.sha256(BODY).hexdigest()[:32] + '"'

HEADERS = {
    'Content-Type': 'text/html; charset=utf-8',
    'Cache-Control': 'private, no-store, max-age=0',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
}

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AIM BORG</title>
<style>:root{{color-scheme:dark}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#070a0e;
color:#c9d6e2;font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",sans-serif}}
form{{width:min(360px,92vw);background:#0d1117;border:1px solid #223044;border-radius:8px;
padding:26px 24px;box-shadow:0 20px 60px rgba(0,0,0,.6)}}
h1{{margin:0 0 4px;font:600 18px/1.3 ui-monospace,Menlo,monospace;color:#7fd8e8;letter-spacing:.04em}}
p.sub{{margin:0 0 20px;color:#71818f;font-size:12.5px}}
label{{display:block;margin:12px 0 5px;font-size:12.5px;color:#9fb0c0}}
input{{width:100%;box-sizing:border-box;background:#070a0e;border:1px solid #2a3542;
color:#eaf2fa;border-radius:4px;padding:9px 11px;font:14px inherit}}
input:focus{{outline:none;border-color:#2f9fb0}}
button{{width:100%;margin-top:18px;background:#0b1a24;border:1px solid #2f9fb0;color:#7fd8e8;
padding:10px;border-radius:6px;cursor:pointer;font:14px ui-monospace,Menlo,monospace}}
button:hover{{background:#12313f;color:#d6f4fb}}
.err{{margin-top:14px;padding:8px 10px;border-radius:4px;font-size:12.5px;
background:rgba(200,69,47,.14);border:1px solid rgba(200,69,47,.5);color:#f08a72}}
.note{{margin-top:18px;font-size:11px;color:#5d6b78;line-height:1.5}}</style></head><body>
<form method="POST" action="/login"><h1>AIM BORG</h1>
<p class="sub">Internal. Production customer configuration.</p>
<label for="u">Username</label>
<input id="u" name="username" autocomplete="username" autofocus value="{user}">
<label for="p">Password</label>
<input id="p" name="password" type="password" autocomplete="current-password">
<button type="submit">Sign in</button>{err}
<div class="note">Access is logged against your account.</div>
</form></body></html>"""


def login_page(msg='', user=''):
    err = f'<div class="err">{html.escape(msg)}</div>' if msg else ''
    return LOGIN_PAGE.format(user=html.escape(user), err=err).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'aim-borg'

    def log_message(self, fmt, *args):
        pass   # access is logged against the account instead

    # -------------------------------------------------------------- helpers
    def _ip(self):
        fwd = self.headers.get('X-Forwarded-For', '')
        return fwd.split(',')[0].strip() or self.client_address[0]

    def _cookies(self):
        out = {}
        for part in (self.headers.get('Cookie') or '').split(';'):
            if '=' in part:
                k, _, v = part.partition('=')
                out[k.strip()] = v.strip()
        return out

    def _send(self, status, body=b'', extra=None, ctype=None):
        hs = dict(HEADERS)
        if ctype:
            hs['Content-Type'] = ctype
        hs.update(extra or {})
        hs['Content-Length'] = str(len(body))
        self.send_response(status)
        for k, v in hs.items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode(),
                   ctype='application/json; charset=utf-8')

    def _cookie_clear(self):
        return (f'{COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'
                + ('; Secure' if SECURE else ''))

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > 1_000_000:
            return b''
        return self.rfile.read(n) if n else b''

    def _user(self):
        return auth.session_user(self._cookies().get(COOKIE), self._ip())

    # -------------------------------------------------------------- routing
    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/login':
            from urllib.parse import parse_qsl
            f = dict(parse_qsl(self._body().decode('utf-8', 'replace')))
            u = (f.get('username') or '').strip()
            ok, res = auth.verify(u, f.get('password') or '', self._ip())
            if not ok:
                # One message for every failure. Distinguishing "no such user"
                # from "wrong password" hands over a list of real usernames.
                return self._send(401, login_page(
                    'Sign-in failed. Check your details and try again.', u))
            token = auth.create_session(res['username'], self._ip(),
                                        self.headers.get('User-Agent'))
            return self._send(302, b'', {
                'Location': '/',
                'Set-Cookie': (f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; '
                               f'Max-Age={ABSOLUTE_HOURS * 3600}'
                               + ('; Secure' if SECURE else ''))})

        # Authenticate BEFORE routing. Routing first is how an unauthenticated
        # 404 ends up confirming which paths exist.
        user = self._user()
        if not user:
            return self._send(401, login_page(), {'Set-Cookie': self._cookie_clear()})
        if path.startswith('/api/'):
            return self._api(path, user, self._body())
        self.send_response(405)
        self.send_header('Allow', 'GET, HEAD')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/logout':
            auth.destroy_session(self._cookies().get(COOKIE))
            return self._send(302, b'', {'Location': '/',
                                         'Set-Cookie': self._cookie_clear()})
        user = self._user()
        if not user:
            return self._send(401, login_page(), {'Set-Cookie': self._cookie_clear()})
        if path.startswith('/api/'):
            return self._api(path, user, b'')

        auth.log(user['username'], 'view', self._ip(), path)
        # No 304 on the document: the studio block is per-request — writability
        # depends on who is asking and whether they hold the claim — so a cached
        # copy would hand one person another's permissions.
        # One artifact at every path: no directory listing, no traversal
        # surface, and no second file to forget about.
        self._send(200, self._with_studio(user))

    def _with_studio(self, user):
        """Inject the studio payload.

        Per request rather than baked into the file, because `writable` is a
        property of this person and this moment: whether the design is complete,
        and whether somebody else holds the single-writer claim. Baking it in
        would freeze one person's permissions into the artifact.
        """
        block = {'mode': 'live' if designs else 'none'}
        if designs:
            open_designs = [d for d in designs.list() if d['state'] == 'open']
            if open_designs:
                d = open_designs[0]
                writable, holder = designs.claim(d['design_id'], user['username'])
                row = designs.get(d['design_id'])
                block.update(design=d['design_id'], base=d['base_snapshot'],
                             name=d['name'], writable=writable, heldBy=holder,
                             readme=row['readme'])
            else:
                block.update(design=None, writable=False)
        # INJECTED BEFORE THE PAYLOAD, not before </body>.
        #
        # The overlay calls PDS.init(D) inline, partway down the body. A block
        # appended before </body> runs AFTER that, so the studio initialised
        # with no payload and rendered nothing — the tabs and controls were in
        # the file and invisible.
        #
        # Placing it ahead of window.AIM_UDBR means the studio block is already
        # on the object by the time anything reads it.
        # ASSIGN AFTER THE PAYLOAD, DO NOT WRAP IT.
        #
        # The first attempt wrapped the payload in Object.assign(...) and
        # closed the paren at the next ';</script>'. In the work build that
        # script also sets AIM_AUTHORING and AIM_DIRECT_START, so the paren
        # landed after those instead of after the payload: a syntax error that
        # killed the whole script. The Grid's camera still moved and nothing
        # rendered.
        #
        # A separate script that runs AFTER the payload needs no surgery on it
        # at all, and cannot break it.
        anchor = b'<script>window.AIM_UDBR='
        i = BODY.find(anchor)
        if i < 0:
            return BODY
        end = BODY.find(b'</script>', i)
        if end < 0:
            return BODY
        end += len(b'</script>')
        js = (f'\n<script>window.AIM_UDBR=window.AIM_UDBR||{{}};'
              f'window.AIM_UDBR.studio={json.dumps(block)};</script>').encode()
        return BODY[:end] + js + BODY[end:]

    def do_HEAD(self):
        self.do_GET()

    def do_PUT(self):    self._refuse()
    def do_DELETE(self): self._refuse()

    def _refuse(self):
        if not self._user():
            return self._send(401, login_page(), {'Set-Cookie': self._cookie_clear()})
        self.send_response(405)
        self.send_header('Allow', 'GET, HEAD')
        self.send_header('Content-Length', '0')
        self.end_headers()

    # -------------------------------------------------------------- api
    def _api(self, path, user, body):
        if designs is None:
            return self._json({'error': 'no UDBR store on this instance'}, 503)
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._json({'error': 'invalid JSON'}, 400)
        who = user['username']
        q = parse_qs(urlparse(self.path).query)
        did = int(q['design'][0]) if 'design' in q else payload.get('design')

        try:
            if path == '/api/designs':
                return self._json({'designs': designs.list()})
            if path == '/api/design/create':
                d = designs.create(int(payload['base']), payload['name'], who)
                auth.log(who, 'design.create', self._ip(), payload['name'])
                return self._json({'design': d})
            if path == '/api/design/claim':
                ok, holder = designs.claim(did, who)
                return self._json({'writable': ok, 'heldBy': holder})
            if path == '/api/design/apply':
                sid, n = designs.apply(did, who, payload['actions'])
                auth.log(who, 'design.apply', self._ip(), f'design {did}, {n} action(s)')
                return self._json({'save': sid, 'applied': n,
                                   'summary': designs.summary(did)})
            if path == '/api/design/changeset':
                return self._json({'changes': designs.change_set(did),
                                   'summary': designs.summary(did)})
            if path == '/api/design/history':
                # Saves, plus the rules each touched. `contested` marks a rule
                # more than one save moved — the number the net change set
                # cannot show, because it reports where a rule ended up rather
                # than that a decision was reversed.
                return self._json({'history': designs.history(did),
                                   'rules': designs.history_rules(did)})
            if path == '/api/design/readme':
                if body:
                    designs.set_readme(did, payload.get('text', ''))
                    return self._json({'ok': True})
                d = designs.get(did)
                return self._json({'text': d['readme'] if d else ''})
            if path == '/api/design/complete':
                designs.complete(did, who)
                auth.log(who, 'design.complete', self._ip(), f'design {did}')
                return self._json({'summary': designs.summary(did)})
            if path == '/api/design/checks':
                # BG-62 / BG-63. The same checks analyze.py runs, against the
                # design in the store. Not a second implementation: both call
                # udbr_checks.run_checks.
                d = designs.get(did)
                if d is None:
                    return self._json({'error': 'no such design'}, 404)
                res = check_snapshot(UDBR_DB, d['snapshot_id'], designs, did)
                res['design'] = did
                res['base'] = d['base_snapshot']
                # BG-64: the surface states when the checks last ran.
                res['ranAt'] = __import__('datetime').datetime.now().isoformat(' ', 'seconds')
                auth.log(who, 'design.checks', self._ip(),
                         f'design {did}, {res["total"]} findings')
                return self._json(res)
            # BG-66 and BG-67. The whole rule set for one rule type, read
            # from its node, independent of any profile.
            if path.startswith('/api/selection/'):
                snap = int(q['snapshot'][0]) if 'snapshot' in q else payload.get('snapshot')
                if not snap:
                    return self._json({'error': 'snapshot required'}, 400)
                which = path.rsplit('/', 1)[-1]
                if which == 'payer':
                    return self._json(payer_rules(UDBR_DB, snap))
                if which == 'profile':
                    return self._json(profile_rules(UDBR_DB, snap))
                return self._json({'error': f'unknown rule type {which}'}, 404)

            # BG-56. Four surfaces over one profile. The snapshot is taken
            # from the query, so a trace can be read on a design or on
            # production, and the view states which.
            if path.startswith('/api/trace'):
                snap = int(q['snapshot'][0]) if 'snapshot' in q else (
                    payload.get('snapshot'))
                pg = (q['profile'][0] if 'profile' in q else payload.get('profile'))
                if not snap or not pg:
                    return self._json({'error': 'snapshot and profile required'}, 400)
                if path == '/api/trace':
                    return self._json(build_trace(UDBR_DB, snap, pg))
                if path == '/api/trace/rules':
                    return self._json(rule_detail(UDBR_DB, snap, pg))
                if path == '/api/trace/sources':
                    return self._json(source_map(UDBR_DB, snap, pg))
                if path == '/api/trace/review':
                    set_review(UDBR_DB, snap, pg, payload['tab'], payload['field'],
                               who, payload.get('verdict'),
                               payload.get('disposition'), payload.get('note'))
                    auth.log(who, 'trace.review', self._ip(),
                             f'{payload["tab"]}/{payload["field"]}')
                    return self._json({'ok': True})
            if path == '/api/design/workbook':
                # BG-65. Separate from the script export and repeatable on the
                # same design: the script is for AIM, the workbook is for the
                # people reviewing the ruleset.
                d = designs.get(did)
                if d is None:
                    return self._json({'error': 'no such design'}, 404)
                if d['state'] != 'complete':
                    return self._json({'blocked': 'the design is not marked complete'}, 409)
                # Checks run first so the Read Me states outstanding findings.
                # Without them it would say nothing, which reads as clean.
                res = check_snapshot(UDBR_DB, d['snapshot_id'], designs, did)
                out = os.path.join(os.environ.get('BORG_OUT', '/tmp'),
                                   f'Design_{did}_{d["name"].replace(" ", "_")}.xlsx')
                info = build_workbook(UDBR_DB, did, out, res)
                auth.log(who, 'design.workbook', self._ip(),
                         f'design {did} -> {os.path.basename(out)}')
                return self._json(info)
            if path == '/api/design/export':
                reason = designs.export_blocked_reason(did)
                if reason:
                    return self._json({'blocked': reason}, 409)
                return self._json({'changes': designs.change_set(did)})
        except PermissionError as e:
            return self._json({'error': str(e)}, 409)
        except (ValueError, KeyError) as e:
            return self._json({'error': str(e)}, 400)
        return self._json({'error': 'unknown endpoint'}, 404)


def _purge_loop():
    import time
    while True:
        time.sleep(900)
        try:
            auth.purge()
        except Exception:
            pass


class Server(ThreadingHTTPServer):
    # The default listen backlog is 5. A browser opening a page fires several
    # requests at once, and a burst past the backlog is REFUSED at the socket
    # rather than queued - measured: 12 simultaneous requests, all refused.
    request_queue_size = 64
    daemon_threads = True
    # Without this a restart during the TIME_WAIT window fails to bind, which
    # on Render means a redeploy that will not come up.
    allow_reuse_address = True


if __name__ == '__main__':
    threading.Thread(target=_purge_loop, daemon=True).start()
    srv = Server(('', PORT), Handler)
    print(f'AIM BORG server on :{PORT}')
    print(f'  serving {os.path.basename(FILE)} ({len(BODY)/1048576:.2f} MB)')
    print('  accounts: ' + ', '.join(u['username'] for u in auth.users()))
    print(f'  store: {UDBR_DB if designs else "none — studio disabled"}')
    print(f'  sessions: {SESSION_HOURS}h idle, {ABSOLUTE_HOURS}h absolute')
    print('  every route requires a signed-in account; no public path')
    srv.serve_forever()
