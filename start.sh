#!/usr/bin/env bash
# Seed the store on first boot only.
#
# Render's disk starts empty. Without a store the studio is disabled and the
# server serves the Grid read-only — it does not fail, but PDS is absent, which
# looks like a bug rather than an empty disk.
set -e
if [ ! -f "$BORG_UDBR_DB" ]; then
  echo "no store at $BORG_UDBR_DB — seeding from udbr_seed.db"
  cp udbr_seed.db "$BORG_UDBR_DB"
fi
exec python3 udbr_server.py
