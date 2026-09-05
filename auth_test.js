#!/usr/bin/env node
'use strict';
/**
 * Tests for the internal deployment server.
 *
 * Every check runs against a real listening process over real HTTP. Reading
 * server.js and asserting the auth code is present is what produced the state
 * BG-33 existed to fix: the code was there, the environment was not set, and
 * it served anyway.
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 8137;
const FILE = process.argv[2] || path.join(__dirname, 'grid_work.html');
const DB   = '/tmp/authtest.db';
const U = 'tester', PW = 'correct-horse-battery';

let pass = 0; const fail = [];
const chk = (c, m) => { if (c) { pass++; console.log('  ok   ' + m); }
                        else { fail.push(m); console.log('  FAIL ' + m); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));

function req(opts = {}, body) {
  return new Promise(resolve => {
    const r = http.request({ host: '127.0.0.1', port: PORT, path: '/', method: 'GET', ...opts },
      res => { let b = ''; res.on('data', d => b += d);
               res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: b })); });
    r.on('error', e => resolve({ status: 0, error: e.message }));
    if (body) r.write(body);
    r.end();
  });
}
const form = o => new URLSearchParams(o).toString();
const POST = (p, o) => req({ method: 'POST', path: p,
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' } }, form(o));
const cookieFrom = r => (r.headers['set-cookie'] || [])
  .map(c => c.split(';')[0]).find(c => c.startsWith('borg_session=')) || '';

function start(env) {
  const p = spawn('node', [path.join(__dirname, 'server.js')],
    { env: { ...process.env, PORT, BORG_FILE: FILE, BORG_AUTH_DB: DB,
             BORG_INSECURE_COOKIE: '1', ...env } });
  let out = '';
  p.stdout.on('data', d => out += d); p.stderr.on('data', d => out += d);
  return { proc: p, log: () => out };
}

(async () => {
  console.log('--- fails closed when there is no way to authenticate ---');
  for (const [label, env, file] of [
    ['no accounts, no seed', { BORG_AUTH_DB: '/tmp/empty-' + Date.now() + '.db' }, FILE],
    ['build file missing', { BORG_AUTH_DB: DB }, '/nonexistent.html'],
  ]) {
    const s = start({ ...env, BORG_FILE: file, BORG_USERS: '' });
    const code = await new Promise(r => s.proc.on('exit', c => r(c)));
    chk(code === 1, `${label}: refuses to start (exit ${code})`);
    chk(/REFUSING TO START/.test(s.log()), `${label}: says why`);
  }

  console.log('\n--- seeds accounts from the environment ---');
  const seedDb = '/tmp/seed-' + Date.now() + '.db';
  let s = start({ BORG_AUTH_DB: seedDb, BORG_USERS: `${U}:Test User:${PW}` });
  await sleep(900);
  chk(/seeded 1 account/.test(s.log()), 'seeds when the store is empty');
  let r = await POST('/login', { username: U, password: PW });
  chk(r.status === 302, `seeded account can sign in (got ${r.status})`);
  s.proc.kill(); await sleep(200);

  console.log('\n--- authentication ---');
  fs.rmSync(DB, { force: true });
  spawnSync();
  function spawnSync() {
    const { execFileSync } = require('child_process');
    execFileSync('node', [path.join(__dirname, 'users.js'), 'add', U, 'Test User', PW],
      { env: { ...process.env, BORG_AUTH_DB: DB } });
  }
  s = start({});
  await sleep(900);

  r = await req();
  chk(r.status === 401, `no session -> 401 (got ${r.status})`);
  chk(/Sign in/.test(r.body), 'unauthenticated request gets the login page');
  chk(!/AIM_UDBR|Azalea|West County/.test(r.body), 'the login page leaks no data');

  r = await POST('/login', { username: U, password: 'wrong-password-here' });
  chk(r.status === 401, 'wrong password -> 401');
  chk(!cookieFrom(r), 'no session cookie issued on failure');
  const wrongUser = await POST('/login', { username: 'nobody', password: PW });
  chk(wrongUser.status === 401, 'unknown user -> 401');
  chk(wrongUser.body === r.body.replace(/value="nobody"/, `value="${U}"`) ||
      /Sign-in failed/.test(wrongUser.body),
      'unknown user and wrong password give the same message (no enumeration)');

  r = await POST('/login', { username: U, password: PW });
  chk(r.status === 302, 'correct credentials -> 302');
  const cookie = cookieFrom(r);
  chk(!!cookie, 'session cookie issued');
  const setc = (r.headers['set-cookie'] || []).join(';');
  chk(/HttpOnly/i.test(setc), 'cookie is HttpOnly');
  chk(/SameSite=Strict/i.test(setc), 'cookie is SameSite=Strict');

  r = await req({ headers: { Cookie: cookie } });
  chk(r.status === 200, `signed in -> 200 (got ${r.status})`);
  chk(Number(r.headers['content-length']) === fs.statSync(FILE).size,
      'serves the whole build');
  chk(/AIM_UDBR/.test(r.body), 'the served build carries the UDBR payload');

  console.log('\n--- forged and stale sessions ---');
  r = await req({ headers: { Cookie: 'borg_session=' + 'A'.repeat(43) } });
  chk(r.status === 401, 'forged token -> 401');
  r = await req({ headers: { Cookie: 'borg_session=' } });
  chk(r.status === 401, 'empty token -> 401');

  console.log('\n--- logout revokes server-side ---');
  const lo = await req({ path: '/logout', headers: { Cookie: cookie } });
  chk(lo.status === 302, 'logout redirects');
  r = await req({ headers: { Cookie: cookie } });
  chk(r.status === 401, 'the same cookie no longer works after logout');

  console.log('\n--- lockout after repeated failures ---');
  for (let i = 0; i < 9; i++) await POST('/login', { username: U, password: 'nope-nope-nope' });
  r = await POST('/login', { username: U, password: PW });
  chk(r.status === 401, 'correct password is refused while locked out');

  // The reset is applied by a SEPARATE PROCESS while the server runs, which
  // also proves the running server picks up account changes without a restart -
  // otherwise disabling an account would not take effect until redeploy.
  const { execFileSync } = require('child_process');
  const NEWPW = 'a-second-known-password';
  execFileSync('node', [path.join(__dirname, 'users.js'), 'passwd', U, NEWPW],
    { env: { ...process.env, BORG_AUTH_DB: DB }, stdio: 'ignore' });
  r = await POST('/login', { username: U, password: PW });
  chk(r.status === 401, 'the OLD password stops working immediately, no restart');
  r = await POST('/login', { username: U, password: NEWPW });
  chk(r.status === 302, 'the new password works immediately, and clears the lockout');
  const cookie2 = cookieFrom(r);
  chk(!!cookie2, 'a session is issued after the reset');

  console.log('\n--- no unauthenticated route anywhere ---');
  for (const p of ['/', '/index.html', '/health', '/../server.js', '/auth.db', '/favicon.ico']) {
    const u = await req({ path: p });
    chk(u.status === 401, `${p} unauthenticated -> 401 (got ${u.status})`);
  }

  console.log('\n--- response hygiene ---');
  r = await req({ headers: { Cookie: cookie2 } });
  chk(/no-store/.test(r.headers['cache-control'] || ''), 'no-store set');
  chk(r.headers['x-content-type-options'] === 'nosniff', 'nosniff set');
  chk(r.headers['x-frame-options'] === 'DENY', 'framing denied');
  for (const m of ['POST', 'PUT', 'DELETE']) {
    const u = await req({ method: m, headers: { Cookie: cookie2 } });
    chk(u.status === 405, `${m} -> 405 (got ${u.status})`);
  }

  console.log('\n--- passwords are not recoverable from the store ---');
  const raw = fs.readFileSync(DB);
  chk(!raw.includes(Buffer.from(PW)), 'the password does not appear in the database file');
  const sess = require('node:sqlite');
  const d = new sess.DatabaseSync(DB);
  // The raw token must not be findable in the store: only its SHA-256 is kept,
  // so reading the database cannot mint a working cookie.
  const tok = (cookie2.split('=')[1] || '');
  const stored = tok
    ? d.prepare('SELECT COUNT(*) c FROM session WHERE token_hash=?').get(tok).c : 0;
  chk(tok && stored === 0, 'the session token itself is not stored, only its hash');
  chk(d.prepare('SELECT COUNT(*) c FROM access_log WHERE event=?').get('login.ok').c > 0,
      'successful logins are recorded against the account');
  chk(d.prepare('SELECT COUNT(*) c FROM access_log WHERE event=?').get('view').c > 0,
      'views are recorded against the account');
  chk(d.prepare('SELECT COUNT(*) c FROM access_log WHERE event=?').get('login.fail').c > 0,
      'failed attempts are recorded');

  s.proc.kill();
  console.log(`\n${pass} passed, ${fail.length} failed`);
  process.exit(fail.length ? 1 : 0);
})();
