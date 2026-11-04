#!/bin/sh
# Idempotent: create Moodle role + database on the shared Postgres instance.
set -eu

HOST="${POSTGRES_HOST:-midpoint_data}"
ADMIN_USER="${POSTGRES_USER:-midpoint}"
ADMIN_DB="${POSTGRES_ADMIN_DB:-midpoint}"
MOODLE_USER="${MOODLE_DATABASE_USER:-bn_moodle}"
MOODLE_PASSWORD="${MOODLE_DATABASE_PASSWORD:-moodle_db_pass}"
MOODLE_DB="${MOODLE_DATABASE_NAME:-bitnami_moodle}"

export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

echo "Waiting for Postgres at ${HOST}..."
until pg_isready -h "$HOST" -U "$ADMIN_USER" -d "$ADMIN_DB" >/dev/null 2>&1; do
  sleep 1
done

psql -v ON_ERROR_STOP=1 -h "$HOST" -U "$ADMIN_USER" -d "$ADMIN_DB" <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${MOODLE_USER}') THEN
    CREATE ROLE ${MOODLE_USER} LOGIN PASSWORD '${MOODLE_PASSWORD}';
  ELSE
    ALTER ROLE ${MOODLE_USER} WITH LOGIN PASSWORD '${MOODLE_PASSWORD}';
  END IF;
END
\$\$;

SELECT 'CREATE DATABASE ${MOODLE_DB} OWNER ${MOODLE_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${MOODLE_DB}')
\gexec
SQL

psql -v ON_ERROR_STOP=1 -h "$HOST" -U "$ADMIN_USER" -d "$MOODLE_DB" <<SQL
GRANT ALL ON SCHEMA public TO ${MOODLE_USER};
ALTER DATABASE ${MOODLE_DB} OWNER TO ${MOODLE_USER};
SQL

echo "Moodle database ${MOODLE_DB} ready (owner ${MOODLE_USER})."
