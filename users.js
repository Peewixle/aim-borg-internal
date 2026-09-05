#!/usr/bin/env node
'use strict';
/**
 * Account management for the AIM BORG internal deployment.
 *
 *   node users.js list
 *   node users.js add <username> "<Display Name>" [password]
 *   node users.js passwd <username> [password]
 *   node users.js disable <username>
 *   node users.js enable <username>
 *   node users.js remove <username>
 *   node users.js sessions
 *   node users.js log [n]
 *
 * With no password given, a strong one is generated and printed once. It is
 * never recoverable afterwards - only the scrypt hash is stored.
 */
const path = require('path');
const crypto = require('crypto');
const auth = require('./auth');

const DB = process.env.BORG_AUTH_DB || path.join(__dirname, 'auth.db');
const db = auth.init(DB);
const [, , cmd, ...args] = process.argv;

// Ambiguous characters removed: a password read off a screen and typed by hand
// should not turn on telling O from 0.
const gen = () => {
  const A = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_';
  return Array.from(crypto.randomBytes(20)).map(b => A[b % A.length]).join('');
};

function table(rows, cols) {
  if (!rows.length) return console.log('  (none)');
  const w = cols.map(c => Math.max(c.length, ...rows.map(r => String(r[c] ?? '').length)));
  console.log('  ' + cols.map((c, i) => c.padEnd(w[i])).join('  '));
  console.log('  ' + w.map(x => '-'.repeat(x)).join('  '));
  rows.forEach(r => console.log('  ' + cols.map((c, i) =>
    String(r[c] ?? '').padEnd(w[i])).join('  ')));
}

try {
  switch (cmd) {
    case 'list':
      table(auth.listUsers().map(u => ({
        username: u.username, display: u.display, created: u.created_at,
        disabled: u.disabled ? 'yes' : '', fails: u.fails || '',
        locked: u.locked_until || ''
      })), ['username', 'display', 'created', 'disabled', 'fails', 'locked']);
      break;

    case 'add': {
      const [u, d, pw] = args;
      if (!u) throw new Error('usage: users.js add <username> "<Display Name>" [password]');
      const password = pw || gen();
      auth.addUser(u, d || u, password, 0);
      console.log(`added ${u}`);
      if (!pw) console.log(`  password: ${password}\n  (shown once - it is not stored in the clear)`);
      break;
    }

    case 'passwd': {
      const [u, pw] = args;
      if (!u) throw new Error('usage: users.js passwd <username> [password]');
      const password = pw || gen();
      auth.setPassword(u, password);
      console.log(`password changed for ${u}; existing sessions revoked`);
      if (!pw) console.log(`  password: ${password}\n  (shown once)`);
      break;
    }

    case 'disable':
    case 'enable': {
      const [u] = args;
      const off = cmd === 'disable' ? 1 : 0;
      const r = db.prepare('UPDATE account SET disabled=? WHERE username=?').run(off, u);
      if (!r.changes) throw new Error('no such account');
      // Disabling must cut existing sessions, or the account stays usable
      // until whatever it holds happens to expire.
      if (off) db.prepare('DELETE FROM session WHERE username=?').run(u);
      auth.log(u, off ? 'account.disabled' : 'account.enabled');
      console.log(`${u} ${cmd}d${off ? '; sessions revoked' : ''}`);
      break;
    }

    case 'remove': {
      const [u] = args;
      db.prepare('DELETE FROM session WHERE username=?').run(u);
      const r = db.prepare('DELETE FROM account WHERE username=?').run(u);
      if (!r.changes) throw new Error('no such account');
      auth.log(u, 'account.removed');
      console.log(`removed ${u}`);
      break;
    }

    case 'sessions':
      table(db.prepare(`SELECT username, created_at, last_seen, ip FROM session
                        ORDER BY last_seen DESC`).all(),
            ['username', 'created_at', 'last_seen', 'ip']);
      break;

    case 'log':
      table(db.prepare(`SELECT at, username, event, ip, detail FROM access_log
                        ORDER BY id DESC LIMIT ?`).all(Number(args[0] || 40)),
            ['at', 'username', 'event', 'ip', 'detail']);
      break;

    default:
      console.log(require('fs').readFileSync(__filename, 'utf8')
        .split('/**')[1].split('*/')[0].replace(/^\s*\*ic?/gm, '').replace(/^ \* ?/gm, ''));
  }
} catch (e) {
  console.error('error: ' + e.message);
  process.exit(1);
}
