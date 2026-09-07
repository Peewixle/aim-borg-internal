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
elif [ "$BORG_RESEED" = "1" ]; then
  # A deliberate, one-shot store replacement. Off by default and gated on an
  # environment variable, because a seed that overwrote the live store on every
  # boot would discard every design authored in the studio.
  #
  # The old store is kept beside the new one, not deleted: a reseed that turns
  # out to be wrong is otherwise unrecoverable.
  echo "BORG_RESEED=1 — replacing the store, keeping the previous copy"
  cp "$BORG_UDBR_DB" "${BORG_UDBR_DB}.replaced-$(date +%Y%m%d-%H%M%S)"
  cp udbr_seed.db "$BORG_UDBR_DB"
  echo "  remove BORG_RESEED before the next deploy, or the next boot replaces it again"
fi
exec python3 udbr_server.py
