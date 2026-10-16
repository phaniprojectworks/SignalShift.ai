#!/bin/sh
# Container entrypoint (Cloud Run). Chooses where the SQLite database lives.
#
# Default (LITESTREAM_REPLICA_URL not set): the original behaviour, unchanged.
#   The database file sits on the Cloud Storage bucket mounted at $STORAGE_MOUNT (default /mnt/storage)
#   and is symlinked to /app/signalshift.db. Demo data is generated on the first start.
#
# Litestream (LITESTREAM_REPLICA_URL=gcs://BUCKET/PATH): recommended.
#   The database lives on the instance's local disk and every committed write is streamed to Cloud Storage
#   within about a second. On start it is restored from the bucket. The first time, it is seeded from the
#   old mounted file if there is one, so no data is lost when you switch.
#   Requires: max-instances=1 (one writer), and memory for the database (local disk is RAM on Cloud Run).
set -u
STORAGE_MOUNT="${STORAGE_MOUNT:-/mnt/storage}"

if [ -z "${LITESTREAM_REPLICA_URL:-}" ]; then
  test -f "$STORAGE_MOUNT/signalshift.db" || (python scripts/generate_data.py --reset && mv signalshift.db "$STORAGE_MOUNT/signalshift.db")
  ln -sf "$STORAGE_MOUNT/signalshift.db" /app/signalshift.db && exec python run.py
  exit 1
fi

command -v litestream >/dev/null 2>&1 || { echo "LITESTREAM_REPLICA_URL is set but litestream is not installed" >&2; exit 1; }
LOCAL_DIR="${LOCAL_DB_DIR:-/data}"
DB="$LOCAL_DIR/signalshift.db"
mkdir -p "$LOCAL_DIR"
export DATABASE_PATH="$DB"

if [ ! -f "$DB" ]; then
  echo "litestream: restoring $DB from $LITESTREAM_REPLICA_URL (if a replica exists)"
  litestream restore -if-replica-exists -o "$DB" "$LITESTREAM_REPLICA_URL" || { echo "litestream restore failed" >&2; exit 1; }
fi
if [ ! -f "$DB" ] && [ -f "$STORAGE_MOUNT/signalshift.db" ]; then
  echo "litestream: no replica yet; seeding from $STORAGE_MOUNT/signalshift.db (one-time switch-over)"
  # SQLite online backup: a consistent copy that includes anything still in the old file's WAL
  python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
    "$STORAGE_MOUNT/signalshift.db" "$DB" || { echo "seeding from the mounted bucket failed" >&2; exit 1; }
fi
if [ ! -f "$DB" ]; then
  echo "litestream: no database anywhere; generating the demo data"
  python scripts/generate_data.py --reset || exit 1
fi

# Litestream runs the app as its child, forwards SIGTERM to it, and does a final sync before exiting.
exec litestream replicate -exec "python run.py" "$DB" "$LITESTREAM_REPLICA_URL"
