#!/bin/bash
set -e

# Configuration
POSTGRES_DB="sensor_data"

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

echo "3. Creating tables and indexes..."
# Mirrors ROUNDS_SCHEMA / MEASUREMENT_SCHEMA in sync_scheduled_postgres.py; keep both in step.
sudo -u postgres psql -v ON_ERROR_STOP=1 --dbname="$POSTGRES_DB" <<-EOSQL
    SET ROLE holzofen;

    CREATE TABLE setting (
        setting_id BIGINT PRIMARY KEY,
        created_at TEXT NOT NULL,
        freq_mhz DOUBLE PRECISION,
        tx_power BIGINT,
        sf BIGINT,
        bw_khz BIGINT,
        cr_denom BIGINT,
        lora_applied BIGINT,
        port TEXT,
        baudrate BIGINT,
        measure_interval_s DOUBLE PRECISION,
        notes TEXT
    );

    CREATE TABLE rounds (
        round_id BIGINT PRIMARY KEY,
        pc_start TEXT,
        pc_end TEXT,
        duration_s DOUBLE PRECISION,
        nodes_csv TEXT,
        ok_links BIGINT,
        total_links BIGINT,
        timeouts BIGINT,
        setting_id BIGINT REFERENCES setting(setting_id)
    );

    CREATE TABLE measurement (
        id BIGINT PRIMARY KEY,
        pc_ts TEXT NOT NULL,
        round_id BIGINT REFERENCES rounds(round_id),
        sender_id BIGINT,
        receiver_id BIGINT,
        marker BIGINT,
        ok_match BIGINT,
        rx_id BIGINT,
        node_timestamp BIGINT,
        rssi DOUBLE PRECISION,
        snr DOUBLE PRECISION
    );

    CREATE INDEX measurement_round_id_idx ON measurement(round_id);
    -- Covering index for the RTI app's time-range aggregates (index-only scans).
    CREATE INDEX measurement_pc_ts_idx ON measurement(pc_ts)
        INCLUDE (sender_id, receiver_id, ok_match, rssi, round_id);
EOSQL

echo "4. Ensuring final table and sequence permissions..."
sudo -u postgres psql -v ON_ERROR_STOP=1 --dbname="$POSTGRES_DB" <<-EOSQL
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO holzofen;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO holzofen;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO holzofen;
EOSQL

echo "Database creation complete."