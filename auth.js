'use strict';
/**
 * User accounts and sessions for the AIM BORG internal deployment.
 *
 * NO DEPENDENCIES. Uses node:sqlite and node:crypto, both built in. Render
 * therefore needs no build step and there is no supply chain to trust for the
 * one component that guards customer configuration.
 *
 * PASSWORDS ARE NEVER STORED. Each account holds a random 16-byte salt and an
 * scrypt hash. scrypt is memory-hard, so a leaked database is expensive to
 * attack offline rather than merely inconvenient.
 *
 * SESSIONS ARE STORED HASHED. The cookie holds a random 32-byte token; the
 * database holds its SHA-256. Read the database and you cannot mint a cookie
 * from it. Sessions are server-side rows, so logout and revocation actually
 * revoke rather than relying on the client discarding something.
 */
const { DatabaseSync } = require('node:sqlite');
const crypto = require('crypto');

const SCRYPT = { N: 16384, r: 8, p: 1, keylen: 64 };
const SESSION_HOURS = 12;      // idle timeout
const ABSOLUTE_HOURS = 24;     // hard ceiling regardless of activity
const MAX_FAILS = 8;           // per account, before a lockout window
const LOCKOUT_MIN = 15;

let db;

function init(path) {
  db = new DatabaseSync(path);
  db.exec(`
    PRAGMA journal_mode = WAL;
    CREATE TABLE IF NOT EXISTS account (
      username    TEXT PRIMARY KEY COLLATE NOCASE,
      display     TEXT NOT NULL,
      salt        BLOB NOT NULL,
      hash        BLOB NOT NULL,
      created_at  TEXT NOT NULL DEFAULT (datetime('now')),
      disabled    INTEGER NOT NULL DEFAULT 0,
      must_change INTEGER NOT NULL DEFAULT 0,
      fails       INTEGER NOT NULL DEFAULT 0,
      locked_until TEXT
    );
    CREATE TABLE IF NOT EXISTS session (
      token_hash  TEXT PRIMARY KEY,
      username    TEXT NOT NULL REFERENCES account(username) ON DELETE CASCADE,
      created_at  TEXT NOT NULL DEFAULT (datetime('now')),
      last_seen   TEXT NOT NULL DEFAULT (datetime('now')),
      ip          TEXT,
      user_agent  TEXT
    );
    -- Access log. Basic auth could only say that somebody holding the password
    -- looked; with named accounts the log can say who, which is the whole
    -- reason for accounts rather than a shared credential.
    CREATE TABLE IF NOT EXISTS access_log (
      id        INTEGER PRIMARY KEY,
      at        TEXT NOT NULL DEFAULT (datetime('now')),
      username  TEXT,
      event     TEXT NOT NULL,
      ip        TEXT,
      detail    TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_log_at ON access_log(at);
  `);
  return db;
}

const hash = (pw, salt) => crypto.scryptSync(pw, salt, SCRYPT.keylen,
  { N: SCRYPT.N, r: SCRYPT.r, p: SCRYPT.p });

function log(username, event, ip, detail) {
  try {
    db.prepare('INSERT INTO access_log (username,event,ip,detail) VALUES (?,?,?,?)')
      .run(username || null, event, ip || null, detail || null);
  } catch { /* logging must never break a request */ }
}

function addUser(username, display, password, mustChange = 1) {
  if (!username || !password) throw new Error('username and password required');
  if (password.length < 12) throw new Error('password must be at least 12 characters');
  const salt = crypto.randomBytes(16);
  db.prepare(`INSERT INTO account (username,display,salt,hash,must_change)
              VALUES (?,?,?,?,?)`)
    .run(username.trim(), display || username.trim(), salt, hash(password, salt), mustChange ? 1 : 0);
  log(username, 'account.created', null, display);
}

function setPassword(username, password) {
  if (password.length < 12) throw new Error('password must be at least 12 characters');
  const salt = crypto.randomBytes(16);
  const r = db.prepare(`UPDATE account SET salt=?, hash=?, must_change=0, fails=0,
                        locked_until=NULL WHERE username=?`)
              .run(salt, hash(password, salt), username);
  if (!r.changes) throw new Error('no such account');
  // Changing a password invalidates every existing session for that account.
  db.prepare('DELETE FROM session WHERE username=?').run(username);
  log(username, 'password.changed');
}

/**
 * Returns {ok, user} or {ok:false, reason}.
 *
 * The reason is for the LOG, never for the response. Telling a caller that a
 * username exists but the password was wrong is a user-enumeration oracle, so
 * every failure looks identical from outside.
 */
function verify(username, password, ip) {
  const row = db.prepare('SELECT * FROM account WHERE username=?').get(username || '');
  if (!row) {
    // Spend comparable time so a missing account is not faster than a wrong
    // password, which would leak which usernames exist.
    hash(password || '', crypto.randomBytes(16));
    log(username, 'login.fail', ip, 'no such account');
    return { ok: false, reason: 'no such account' };
  }
  if (row.disabled) {
    log(username, 'login.fail', ip, 'account disabled');
    return { ok: false, reason: 'disabled' };
  }
  if (row.locked_until) {
    const until = new Date(row.locked_until + 'Z');
    if (until > new Date()) {
      log(username, 'login.fail', ip, 'locked out');
      return { ok: false, reason: 'locked' };
    }
  }
  const got = hash(password || '', Buffer.from(row.salt));
  const want = Buffer.from(row.hash);
  const ok = got.length === want.length && crypto.timingSafeEqual(got, want);
  if (!ok) {
    const fails = row.fails + 1;
    const lock = fails >= MAX_FAILS
      ? new Date(Date.now() + LOCKOUT_MIN * 60000).toISOString().replace('T', ' ').slice(0, 19)
      : null;
    db.prepare('UPDATE account SET fails=?, locked_until=? WHERE username=?')
      .run(fails, lock, row.username);
    log(row.username, 'login.fail', ip, `wrong password (${fails})`);
    return { ok: false, reason: 'wrong password' };
  }
  db.prepare('UPDATE account SET fails=0, locked_until=NULL WHERE username=?').run(row.username);
  return { ok: true, user: { username: row.username, display: row.display,
                             mustChange: !!row.must_change } };
}

function createSession(username, ip, ua) {
  const token = crypto.randomBytes(32).toString('base64url');
  const th = crypto.createHash('sha256').update(token).digest('hex');
  db.prepare('INSERT INTO session (token_hash,username,ip,user_agent) VALUES (?,?,?,?)')
    .run(th, username, ip || null, (ua || '').slice(0, 200));
  log(username, 'login.ok', ip);
  return token;
}

function sessionUser(token, ip) {
  if (!token) return null;
  const th = crypto.createHash('sha256').update(token).digest('hex');
  const s = db.prepare(`SELECT s.*, a.display, a.disabled, a.must_change
                        FROM session s JOIN account a ON a.username = s.username
                        WHERE s.token_hash=?`).get(th);
  if (!s) return null;
  if (s.disabled) { destroySession(token); return null; }
  const now = Date.now();
  const idle = now - new Date(s.last_seen + 'Z').getTime();
  const age  = now - new Date(s.created_at + 'Z').getTime();
  // Two limits, because they catch different things: idle timeout covers a
  // walked-away browser, absolute lifetime caps a stolen token.
  if (idle > SESSION_HOURS * 3600e3 || age > ABSOLUTE_HOURS * 3600e3) {
    destroySession(token);
    log(s.username, 'session.expired', ip);
    return null;
  }
  db.prepare("UPDATE session SET last_seen=datetime('now') WHERE token_hash=?").run(th);
  return { username: s.username, display: s.display, mustChange: !!s.must_change };
}

function destroySession(token) {
  if (!token) return;
  const th = crypto.createHash('sha256').update(token).digest('hex');
  const s = db.prepare('SELECT username FROM session WHERE token_hash=?').get(th);
  db.prepare('DELETE FROM session WHERE token_hash=?').run(th);
  if (s) log(s.username, 'logout');
}

function purge() {
  db.prepare(`DELETE FROM session WHERE
     julianday('now') - julianday(last_seen)  > ${SESSION_HOURS}/24.0
  OR julianday('now') - julianday(created_at) > ${ABSOLUTE_HOURS}/24.0`).run();
}

const listUsers = () => db.prepare(
  `SELECT username, display, created_at, disabled, must_change, fails, locked_until
   FROM account ORDER BY username`).all();

const countUsers = () => db.prepare('SELECT COUNT(*) c FROM account').get().c;

/**
 * Seed accounts from BORG_USERS when the store is empty.
 *
 * Render's filesystem is EPHEMERAL unless a persistent disk is attached, so a
 * database written at runtime is wiped on every redeploy. Seeding from the
 * environment means the service still comes up with working accounts after a
 * deploy; with a persistent disk attached, this runs once and later changes
 * survive.
 *
 * Format: "user:Display Name:password,user2:Name Two:password2"
 */
function seedFromEnv(spec) {
  if (!spec || countUsers() > 0) return 0;
  let n = 0;
  for (const entry of spec.split(',')) {
    const [u, d, ...rest] = entry.split(':');
    const pw = rest.join(':');
    if (!u || !pw) continue;
    addUser(u.trim(), (d || u).trim(), pw, 1);
    n++;
  }
  return n;
}

module.exports = { init, addUser, setPassword, verify, createSession, sessionUser,
                   destroySession, purge, listUsers, countUsers, seedFromEnv, log,
                   SESSION_HOURS, ABSOLUTE_HOURS };
