#!/bin/sh
# Start a Postgres read replica of the db service (Lampus Phase 1, Step 9; course topic 7).
#
# On first start the data folder is empty: copy the primary's data with pg_basebackup.
# -R writes the settings that make this server a standby: it connects to the primary,
# receives every change as it happens (streaming replication) and replays it, and it
# refuses writes. Then start Postgres as usual.
set -e
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  until pg_isready -h db -U lampus -q; do sleep 1; done
  mkdir -p "$PGDATA" && chown postgres "$PGDATA" && chmod 700 "$PGDATA"
  PGPASSWORD=lampus gosu postgres pg_basebackup -h db -U lampus -D "$PGDATA" -R -X stream -c fast
fi
exec docker-entrypoint.sh postgres
