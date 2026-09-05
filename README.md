# AIM BORG — internal deployment

Serves the internal build to named accounts. Carries production UDBR
configuration for Azalea Medical Transport and West County Paramedics.

## Deploy on Render

Web Service, Node, from this repository.

    Build command:  (none)
    Start command:  node server.js

**Node 22.5 or later is required** — the account store uses `node:sqlite`,
which is built in, so there are no dependencies and no build step.

### Environment variables

| Variable | Required | Notes |
|---|---|---|
| `BORG_USERS` | first deploy | seeds accounts when the store is empty |
| `BORG_AUTH_DB` | no | defaults to `./auth.db` |

`BORG_USERS` format, comma-separated:

    mmcintyre:Michael McIntyre:a-long-password,mcorey:Michelle Corey:another-long-one

The service **will not start** with no accounts and no seed.

### Render's filesystem is ephemeral

Without a persistent disk, `auth.db` is wiped on every redeploy — accounts added
later disappear and sessions end. Two options:

- **Attach a persistent disk** mounted at `/data` and set
  `BORG_AUTH_DB=/data/auth.db`. Accounts, sessions and the access log survive.
- **Leave it ephemeral** and rely on `BORG_USERS` to re-seed on each deploy.
  Fine for a fixed set of people; anything changed at runtime is lost.

## Managing accounts

    node users.js list
    node users.js add <username> "<Display Name>" [password]
    node users.js passwd <username> [password]
    node users.js disable <username>          # revokes sessions immediately
    node users.js remove <username>
    node users.js sessions
    node users.js log 50

With no password given, a strong one is generated and printed once. Only the
scrypt hash is stored, so it cannot be recovered afterwards.

Changes take effect immediately — the running server picks them up without a
restart, which the tests verify.

## What the security is, and is not

**Is:** TLS terminated by Render; named accounts with scrypt-hashed passwords
and per-account salts; server-side sessions that can actually be revoked;
12-hour idle and 24-hour absolute session limits; lockout after 8 failed
attempts; identical responses for unknown user and wrong password, so the login
page is not a username oracle; and a log of who opened it and when.

**Is not:** a second factor. A password is one thing you know, and whoever has
it is you. For a small internal group over TLS that is a defensible control —
but it is a decision, not a property. If this ever holds more than
configuration, put an identity provider in front of it.

## What the data is

Business configuration only: profiles, target fields, conditions, rate codes and
template remark text. **No patient data** — no names, dates of birth,
identifiers or contact details, verified by scan rather than assumed. The
exposure risk is customer confidentiality, not PHI.

## Separation

Keep this repository separate from the demo. The demo is served as static files
and carries no production data; this build carries data and is only safe behind
`server.js`. There is deliberately no `index.html` here, because a static host
would serve it without ever running the server.

## Verify before trusting

    node auth_test.js internal.html

45 checks against a real listening process: refuses to start unconfigured, seeds
from the environment, 401 on every route without a session, rejects wrong
passwords, unknown users, forged and empty tokens, logout revokes server-side,
lockout works and a password reset clears it, cookies are HttpOnly and
SameSite=Strict, write methods refused, the password never appears in the
database file, and the session token is stored only as a hash.
