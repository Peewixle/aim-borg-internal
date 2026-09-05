#!/usr/bin/env node
'use strict';
/**
 * AIM BORG — internal deployment server.
 *
 * Serves the internal build, which carries production UDBR configuration for
 * Azalea Medical Transport and West County Paramedics. Named accounts, hashed
 * passwords, server-side sessions, per-user access log.
 *
 * FAILS CLOSED. With no accounts and no seed, the process exits rather than
 * serving. The state this replaces was worse than no authentication: the code
 * supported it, the environment was never set, and it served anyway.
 *
 * WHAT THIS PROVIDES
 *   TLS (terminated by Render), named accounts with scrypt-hashed passwords,
 *   server-side sessions that can actually be revoked, lockout after repeated
 *   failures, and a log of WHO opened it and when.
 *
 * WHAT IT DOES NOT
 *   A second factor. A password is one thing you know, and anyone who obtains
 *   it is you. For a small internal group over TLS that is a defensible
 *   control - but it is a decision, not a property. If this ever holds more
 *   than configuration, put an identity provider in front of it.
 *
 * The payload is business configuration: profiles, fields, conditions, rate
 * codes, template remark text. No patient data - no names, dates of birth,
 * identifiers or contact details, verified by scan rather than assumed. The
 * exposure risk is customer confidentiality, not PHI.
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const auth = require('./auth');

const PORT    = process.env.PORT || 8080;
const FILE    = process.env.BORG_FILE || path.join(__dirname, 'internal.html');
const AUTH_DB = process.env.BORG_AUTH_DB || path.join(__dirname, 'auth.db');
const SEED    = process.env.BORG_USERS;
const COOKIE  = 'borg_session';
const SECURE  = process.env.BORG_INSECURE_COOKIE !== '1';   // unset only for local tests

if (!fs.existsSync(FILE)) {
  console.error(`REFUSING TO START: build not found at ${FILE}`);
  process.exit(1);
}
auth.init(AUTH_DB);
const seeded = auth.seedFromEnv(SEED);
if (seeded) console.log(`seeded ${seeded} account(s) from BORG_USERS`);
if (auth.countUsers() === 0) {
  console.error('REFUSING TO START: no accounts exist and BORG_USERS is not set.');
  console.error('This build carries production customer configuration and will not be');
  console.error('served without authentication. Set BORG_USERS, or run: node users.js add');
  process.exit(1);
}

const BODY = fs.readFileSync(FILE);
const ETAG = '"' + crypto.createHash('sha256').update(BODY).digest('hex').slice(0, 32) + '"';

const esc = s => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const ipOf = req => (req.headers['x-forwarded-for'] || '').split(',')[0].trim()
                 || req.socket.remoteAddress || '';

function cookies(req) {
  const out = {};
  (req.headers.cookie || '').split(';').forEach(part => {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  });
  return out;
}

function loginPage(msg, username) {
  return `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIM BORG</title><style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#070a0e;
  color:#c9d6e2;font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",sans-serif}
form{width:min(360px,92vw);background:#0d1117;border:1px solid #223044;
  border-radius:8px;padding:26px 24px;box-shadow:0 20px 60px rgba(0,0,0,.6)}
h1{margin:0 0 4px;font:600 18px/1.3 ui-monospace,Menlo,monospace;color:#7fd8e8;
  letter-spacing:.04em}
p.sub{margin:0 0 20px;color:#71818f;font-size:12.5px}
label{display:block;margin:12px 0 5px;font-size:12.5px;color:#9fb0c0}
input{width:100%;box-sizing:border-box;background:#070a0e;border:1px solid #2a3542;
  color:#eaf2fa;border-radius:4px;padding:9px 11px;font:14px inherit}
input:focus{outline:none;border-color:#2f9fb0}
button{width:100%;margin-top:18px;background:#0b1a24;border:1px solid #2f9fb0;
  color:#7fd8e8;padding:10px;border-radius:6px;cursor:pointer;
  font:14px ui-monospace,Menlo,monospace}
button:hover{background:#12313f;color:#d6f4fb}
.err{margin-top:14px;padding:8px 10px;border-radius:4px;font-size:12.5px;
  background:rgba(200,69,47,.14);border:1px solid rgba(200,69,47,.5);color:#f08a72}
.note{margin-top:18px;font-size:11px;color:#5d6b78;line-height:1.5}
</style></head><body>
<form method="POST" action="/login">
  <h1>AIM BORG</h1>
  <p class="sub">Internal. Production customer configuration.</p>
  <label for="u">Username</label>
  <input id="u" name="username" autocomplete="username" autofocus value="${esc(username)}">
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password">
  <button type="submit">Sign in</button>
  ${msg ? `<div class="err">${esc(msg)}</div>` : ''}
  <div class="note">Access is logged against your account.</div>
</form></body></html>`;
}

const HEADERS = {
  'Content-Type': 'text/html; charset=utf-8',
  'Cache-Control': 'private, no-store, max-age=0',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY'
};

function send(res, status, body, extra) {
  res.writeHead(status, Object.assign({}, HEADERS, extra || {}));
  res.end(body);
}

function readBody(req, cb) {
  let b = '';
  req.on('data', d => { b += d; if (b.length > 4096) req.destroy(); });
  req.on('end', () => cb(b));
}

// Expired sessions are removed on read, but a session nobody returns to would
// sit in the table forever.
setInterval(() => { try { auth.purge(); } catch {} }, 15 * 60 * 1000).unref();

const server = http.createServer((req, res) => {
  const ip = ipOf(req);
  const p = new URL(req.url, 'http://x').pathname;
  const clear = `${COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0`
              + (SECURE ? '; Secure' : '');

  if (p === '/logout') {
    auth.destroySession(cookies(req)[COOKIE]);
    return send(res, 302, '', { Location: '/', 'Set-Cookie': clear });
  }

  if (p === '/login' && req.method === 'POST') {
    return readBody(req, raw => {
      const f = new URLSearchParams(raw);
      const u = (f.get('username') || '').trim();
      const r = auth.verify(u, f.get('password') || '', ip);
      if (!r.ok) {
        // One message for every failure. Distinguishing "no such user" from
        // "wrong password" hands an attacker a list of real usernames.
        return send(res, 401,
          loginPage('Sign-in failed. Check your details and try again.', u));
      }
      const token = auth.createSession(r.user.username, ip, req.headers['user-agent']);
      return send(res, 302, '', { Location: '/',
        'Set-Cookie': `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict; `
                    + `Max-Age=${auth.ABSOLUTE_HOURS * 3600}` + (SECURE ? '; Secure' : '') });
    });
  }

  // Authenticate BEFORE routing. Routing first is how an unauthenticated 404
  // ends up confirming which paths exist.
  const user = auth.sessionUser(cookies(req)[COOKIE], ip);
  if (!user) return send(res, 401, loginPage(), { 'Set-Cookie': clear });

  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.writeHead(405, { Allow: 'GET, HEAD', 'Cache-Control': 'no-store' });
    return res.end();
  }

  auth.log(user.username, 'view', ip, p);
  if (req.headers['if-none-match'] === ETAG) {
    res.writeHead(304, { ETag: ETAG, 'Cache-Control': 'private, no-store' });
    return res.end();
  }
  // One artifact at every path: no directory listing, no traversal surface,
  // and no second file to forget about.
  res.writeHead(200, Object.assign({}, HEADERS, {
    'Content-Length': BODY.length, ETag: ETAG
  }));
  res.end(req.method === 'HEAD' ? undefined : BODY);
});

server.listen(PORT, () => {
  console.log(`AIM BORG internal server on :${PORT}`);
  console.log(`  serving ${path.basename(FILE)} (${(BODY.length / 1048576).toFixed(2)} MB)`);
  console.log(`  accounts: ${auth.listUsers().map(u => u.username).join(', ')}`);
  console.log(`  sessions: ${auth.SESSION_HOURS}h idle, ${auth.ABSOLUTE_HOURS}h absolute`);
  console.log('  every route requires a signed-in account; no public path');
});
