#!/bin/bash
set -e

# 1. Create a custom user with a password
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER holzofen WITH PASSWORD 'biofilter';
    GRANT ALL PRIVILEGES ON DATABASE "$POSTGRES_DB" TO holzofen;
EOSQL

# 2. Restore the custom archive dump into the database
echo "Restoring database from sensor_data.dump..."
pg_restore --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --verbose /docker-entrypoint-initdb.d/sensor_data.dump || true

# 3. Grant table and schema permissions to the new user after restoration
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    GRANT ALL ON SCHEMA public TO holzofen;
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO holzofen;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO holzofen;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO holzofen;
EOSQL

echo "Database restore and user creation complete."

# Allow SCRAM-authenticated clients on the Tailscale address range.
grep -qxF 'host    all     all     100.64.0.0/10    scram-sha-256' "$PGDATA/pg_hba.conf" || \
    echo 'host    all     all     100.64.0.0/10    scram-sha-256' >> "$PGDATA/pg_hba.conf"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -c 'SELECT pg_reload_conf();'