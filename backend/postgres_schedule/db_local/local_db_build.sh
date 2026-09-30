#!/bin/bash
set -e

# Configuration
POSTGRES_DB="sensor_data"
DUMP_FILE="../db_container/sensor_data.dump"

# Pre-check if database exists
echo "Checking if database '$POSTGRES_DB' already exists..."
DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '$POSTGRES_DB';")

if [ "$DB_EXISTS" = "1" ]; then
    echo "Error: Database '$POSTGRES_DB' already exists. Aborting." >&2
    exit 1
fi

echo "1. Creating user and database owned by holzofen..."
sudo -u postgres psql -v ON_ERROR_STOP=1 <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'holzofen') THEN
            CREATE USER holzofen WITH PASSWORD 'biofilter';
        END IF;
    END
    \$\$;

    CREATE DATABASE "$POSTGRES_DB" OWNER holzofen;
EOSQL

echo "2. Granting public schema permissions..."
sudo -u postgres psql -v ON_ERROR_STOP=1 --dbname="$POSTGRES_DB" <<-EOSQL
    GRANT ALL ON SCHEMA public TO holzofen;
EOSQL

echo "3. Restoring database from $DUMP_FILE..."
sudo -u postgres pg_restore \
    --dbname="$POSTGRES_DB" \
    --no-owner \
    --role=holzofen \
    --verbose "$DUMP_FILE"

echo "4. Ensuring final table and sequence permissions..."
sudo -u postgres psql -v ON_ERROR_STOP=1 --dbname="$POSTGRES_DB" <<-EOSQL
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO holzofen;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO holzofen;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO holzofen;
EOSQL

echo "Database restore and user creation complete."