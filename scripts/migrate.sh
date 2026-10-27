#!/usr/bin/env bash
# Apply migrations in order. Usage: DATABASE_URL=... bash scripts/migrate.sh
# Idempotent: applied files are recorded in schema_migrations and skipped.
# Safe to re-run on every `docker compose up` (migrate service, restarts).
set -euo pipefail
: "${DATABASE_URL:?DATABASE_URL must be set}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
FILES="001_schema.sql 002_seed_footwear.sql 013_pilot_workspace.sql 004_requests.sql 005_extraction_status.sql 006_supplier_wiring.sql 007_memberships.sql 008_extraction_jobs.sql 009_invites.sql 010_extracting_status.sql 011_document_items.sql 012_request_notifications.sql 014_invite_hygiene.sql 015_audit_insert_only.sql 016_extraction_truncated.sql 017_invite_company.sql 018_app_role_grants.sql 019_one_verified_per_field.sql 020_universal_ingestion.sql"
# Files covered by the 000 Supabase one-shot (schema + footwear seed +
# pilot workspace row). ONLY these may be baseline-marked; 004-012 create
# tables/columns the one-shot never had, so marking them applied without
# running them leaves a half-migrated DB (the old bug this fixes).
BASELINE_FILES="001_schema.sql 002_seed_footwear.sql 013_pilot_workspace.sql"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())" >/dev/null

# Baseline: a DB that already has the workspaces table but no tracking rows was
# migrated by an older loop or the 000 Supabase one-shot — mark ONLY the
# one-shot-covered files applied. 004+ must still RUN (idempotent DDL), never
# be skipped, or tables like client_requests / memberships / invites go missing.
if [ "$(psql "$DATABASE_URL" -tAX -c "SELECT COUNT(*) FROM schema_migrations")" = "0" ] \
  && [ "$(psql "$DATABASE_URL" -tAX -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_name='workspaces'")" = "1" ]; then
  for f in $BASELINE_FILES; do
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c \
      "INSERT INTO schema_migrations (filename) VALUES ('$f') ON CONFLICT DO NOTHING" >/dev/null
  done
  echo "Baseline: workspaces table pre-exists, marked ${BASELINE_FILES} as applied."
fi

for f in $FILES; do
  if [ "$(psql "$DATABASE_URL" -tAX -c "SELECT COUNT(*) FROM schema_migrations WHERE filename='$f'")" = "1" ]; then
    echo "Skipping $f (already applied)"
    continue
  fi
  echo "Applying $f"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$DIR/migrations/$f"
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c \
    "INSERT INTO schema_migrations (filename) VALUES ('$f')" >/dev/null
done
echo "Done."
echo "NOTE: 000_pilot_bootstrap.sql is a Supabase-SQL-Editor one-shot (not in loop)."
echo "NOTE: 003_audit_grants.sql is a manual template (not in loop);"
echo "then run: python scripts/verify_audit_grants.py"
