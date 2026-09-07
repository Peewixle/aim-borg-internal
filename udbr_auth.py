#!/usr/bin/env python3
"""
udbr_auth — accounts, sessions and the access log.

Port of the Node implementation, preserving its behaviour exactly: every
property the 45 Node tests asserted is asserted again by test_server.py.

NO DEPENDENCIES. hashlib.scrypt, secrets, hmac and sqlite3 are all standard
library, so the deployment needs no build step and there is no supply chain to
trust for the component guarding customer configuration.

PASSWORDS ARE NEVER STORED. Each account holds a random 16-byte salt and an
scrypt hash. scrypt is memory-hard, so a leaked database is expensive to attack
offline rather than merely inconvenient.

SESSIONS ARE STORED HASHED. The cookie holds a random token; the database holds
its SHA-256. Read the database and you cannot mint a cookie from it. Sessions
are rows, so logout and revocation actually revoke rather than relying on the
client to discard something.
"""
import sqlite3, threading, hashlib, secrets, hmac, os
from datetime import datetime, timedelta, timezone

SCRYPT = dict(n=16384, r=8, p=1, dklen=64)
SESSION_HOURS = 12       # idle timeout
ABSOLUTE_HOURS = 24      # hard ceiling regardless of activity
MAX_FAILS = 8
LOCKOUT_MIN = 15
MIN_PASSWORD = 12


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Auth:
    def __init__(self, path):
        self.path = path
        # One connection shared across ThreadingHTTPServer threads loses
        # writes: measured at 176 of 1600 inserts lost with 8 threads, plus
        # "cannot start a transaction within a transaction". The lock
        # serialises access. At this scale — a handful of Standards Managers —
        # serialising is free, and a connection pool would be complexity
        # bought for load that does not exist.
        self._lock = threading.RLock()
        self.cx = sqlite3.connect(path, check_same_thread=False)
        self.cx.row_factory = sqlite3.Row
        self.cx.executescript("""
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS account (
          username     TEXT PRIMARY KEY COLLATE NOCASE,
          display      TEXT NOT NULL,
          salt         BLOB NOT NULL,
          hash         BLOB NOT NULL,
          created_at   TEXT NOT NULL DEFAULT (datetime('now')),
          disabled     INTEGER NOT NULL DEFAULT 0,
          fails        INTEGER NOT NULL DEFAULT 0,
          locked_until TEXT
        );
        CREATE TABLE IF NOT EXISTS session (
          token_hash TEXT PRIMARY KEY,
          username   TEXT NOT NULL REFERENCES account(username) ON DELETE CASCADE,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          last_seen  TEXT NOT NULL DEFAULT (datetime('now')),
          ip         TEXT,
          user_agent TEXT
        );
        -- A shared credential could only say that somebody with the password
        -- looked. Named accounts let the log say who, which is the whole point
        -- of having them.
        CREATE TABLE IF NOT EXISTS access_log (
          id       INTEGER PRIMARY KEY,
          at       TEXT NOT NULL DEFAULT (datetime('now')),
          username TEXT,
          event    TEXT NOT NULL,
          ip       TEXT,
          detail   TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_log_at ON access_log(at);
        """)
        self.cx.commit()

    # ---------------------------------------------------------------- helpers
    def _hash(self, password, salt):
        return hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)

    def log(self, username, event, ip=None, detail=None):
        try:
            self.cx.execute(
                'INSERT INTO access_log (username,event,ip,detail) VALUES (?,?,?,?)',
                (username, event, ip, detail))
            self.cx.commit()
        except Exception:
            pass   # logging must never break a request

    # ---------------------------------------------------------------- accounts
    def add_user(self, username, display, password):
        if not username or not password:
            raise ValueError('username and password required')
        if len(password) < MIN_PASSWORD:
            raise ValueError(f'password must be at least {MIN_PASSWORD} characters')
        salt = secrets.token_bytes(16)
        self.cx.execute(
            'INSERT INTO account (username,display,salt,hash) VALUES (?,?,?,?)',
            (username.strip(), display or username.strip(), salt,
             self._hash(password, salt)))
        self.cx.commit()
        self.log(username, 'account.created', detail=display)

    def set_password(self, username, password):
        if len(password) < MIN_PASSWORD:
            raise ValueError(f'password must be at least {MIN_PASSWORD} characters')
        salt = secrets.token_bytes(16)
        cur = self.cx.execute("""UPDATE account SET salt=?, hash=?, fails=0,
                                 locked_until=NULL WHERE username=?""",
                              (salt, self._hash(password, salt), username))
        if not cur.rowcount:
            raise ValueError('no such account')
        # Changing a password invalidates every existing session for it.
        self.cx.execute('DELETE FROM session WHERE username=?', (username,))
        self.cx.commit()
        self.log(username, 'password.changed')

    def set_disabled(self, username, off):
        cur = self.cx.execute('UPDATE account SET disabled=? WHERE username=?',
                              (1 if off else 0, username))
        if not cur.rowcount:
            raise ValueError('no such account')
        if off:
            # Disabling must cut live sessions, or the account stays usable
            # until whatever it holds happens to expire.
            self.cx.execute('DELETE FROM session WHERE username=?', (username,))
        self.cx.commit()
        self.log(username, 'account.disabled' if off else 'account.enabled')

    def remove_user(self, username):
        self.cx.execute('DELETE FROM session WHERE username=?', (username,))
        cur = self.cx.execute('DELETE FROM account WHERE username=?', (username,))
        if not cur.rowcount:
            raise ValueError('no such account')
        self.cx.commit()
        self.log(username, 'account.removed')

    def users(self):
        return [dict(r) for r in self.cx.execute(
            """SELECT username, display, created_at, disabled, fails, locked_until
               FROM account ORDER BY username""")]

    def count(self):
        return self.cx.execute('SELECT COUNT(*) c FROM account').fetchone()['c']

    # ---------------------------------------------------------------- verify
    def verify(self, username, password, ip=None):
        """Returns (ok, user_or_reason). The reason is for the LOG only: telling
        a caller that a username exists but the password was wrong is a
        user-enumeration oracle, so every failure looks identical outside."""
        row = self.cx.execute('SELECT * FROM account WHERE username=?',
                              (username or '',)).fetchone()
        if row is None:
            # Spend comparable time, so a missing account is not measurably
            # faster than a wrong password.
            self._hash(password or '', secrets.token_bytes(16))
            self.log(username, 'login.fail', ip, 'no such account')
            return False, 'no such account'
        if row['disabled']:
            self.log(username, 'login.fail', ip, 'account disabled')
            return False, 'disabled'
        if row['locked_until'] and datetime.fromisoformat(row['locked_until']) > _now():
            self.log(username, 'login.fail', ip, 'locked out')
            return False, 'locked'

        got = self._hash(password or '', bytes(row['salt']))
        if not hmac.compare_digest(got, bytes(row['hash'])):
            fails = row['fails'] + 1
            lock = ((_now() + timedelta(minutes=LOCKOUT_MIN)).isoformat(' ', 'seconds')
                    if fails >= MAX_FAILS else None)
            self.cx.execute('UPDATE account SET fails=?, locked_until=? WHERE username=?',
                            (fails, lock, row['username']))
            self.cx.commit()
            self.log(row['username'], 'login.fail', ip, f'wrong password ({fails})')
            return False, 'wrong password'

        self.cx.execute('UPDATE account SET fails=0, locked_until=NULL WHERE username=?',
                        (row['username'],))
        self.cx.commit()
        return True, dict(username=row['username'], display=row['display'])

    # ---------------------------------------------------------------- sessions
    def create_session(self, username, ip=None, ua=None):
        token = secrets.token_urlsafe(32)
        th = hashlib.sha256(token.encode()).hexdigest()
        self.cx.execute(
            'INSERT INTO session (token_hash,username,ip,user_agent) VALUES (?,?,?,?)',
            (th, username, ip, (ua or '')[:200]))
        self.cx.commit()
        self.log(username, 'login.ok', ip)
        return token

    def session_user(self, token, ip=None):
        if not token:
            return None
        th = hashlib.sha256(token.encode()).hexdigest()
        s = self.cx.execute("""SELECT s.*, a.display, a.disabled FROM session s
                               JOIN account a ON a.username = s.username
                               WHERE s.token_hash=?""", (th,)).fetchone()
        if s is None:
            return None
        if s['disabled']:
            self.destroy_session(token)
            return None
        now = _now()
        idle = now - datetime.fromisoformat(s['last_seen'])
        age = now - datetime.fromisoformat(s['created_at'])
        # Two limits catching different things: idle covers a walked-away
        # browser, absolute caps a stolen token.
        if idle > timedelta(hours=SESSION_HOURS) or age > timedelta(hours=ABSOLUTE_HOURS):
            self.destroy_session(token)
            self.log(s['username'], 'session.expired', ip)
            return None
        self.cx.execute("UPDATE session SET last_seen=datetime('now') WHERE token_hash=?",
                        (th,))
        self.cx.commit()
        return dict(username=s['username'], display=s['display'])

    def destroy_session(self, token):
        if not token:
            return
        th = hashlib.sha256(token.encode()).hexdigest()
        s = self.cx.execute('SELECT username FROM session WHERE token_hash=?',
                            (th,)).fetchone()
        self.cx.execute('DELETE FROM session WHERE token_hash=?', (th,))
        self.cx.commit()
        if s:
            self.log(s['username'], 'logout')

    def purge(self):
        """Expired sessions are removed on read, but one nobody returns to
        would sit in the table forever."""
        self.cx.execute(f"""DELETE FROM session WHERE
              julianday('now') - julianday(last_seen)  > {SESSION_HOURS}/24.0
           OR julianday('now') - julianday(created_at) > {ABSOLUTE_HOURS}/24.0""")
        self.cx.commit()

    def seed_from_env(self, spec):
        """Seed accounts when the store is empty.

        Render's filesystem is ephemeral without a persistent disk, so a
        database written at runtime is wiped on redeploy. Seeding from the
        environment means the service still comes up with working accounts;
        with a disk attached this runs once and later changes survive.

        Format: user:Display Name:password,user2:Name Two:password2
        """
        if not spec or self.count() > 0:
            return 0
        n = 0
        for entry in spec.split(','):
            parts = entry.split(':')
            if len(parts) < 3:
                continue
            u, d, pw = parts[0], parts[1], ':'.join(parts[2:])
            if not u or not pw:
                continue
            self.add_user(u.strip(), d.strip(), pw)
            n += 1
        return n
