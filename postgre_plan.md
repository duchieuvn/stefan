# Manual SQLite-to-PostgreSQL Sync

## Summary

Create `SOFTWARE/sync_to_postgres.py`, a manually executed incremental sync script for `sensor_data.db`. Each invocation copies at most 1,000 new `rounds` rows and 1,000 new `measurement` rows, then exits.

## Implementation Changes

- Add small modular functions for configuration loading, SQLite/PostgreSQL connections, destination schema creation, latest-key lookup, batch reading, batch insertion, and orchestration.
- Load `POSTGRES_DSN` from `SOFTWARE/.env` with `python-dotenv`; resolve the SQLite database relative to the script so it works from any current directory.
- Add `psycopg[binary]` and `python-dotenv` to `requirements.txt`; add `.env` to `.gitignore` and provide a credential-free `.env.example`.
- Automatically create PostgreSQL `rounds` and `measurement` tables matching the inspected SQLite columns. Use PostgreSQL primary keys on `round_id` and `id`, nullable columns matching SQLite, `BIGINT` for integer values, `DOUBLE PRECISION` for real values, and text fields unchanged.
- Add a nullable foreign key from `measurement.round_id` to `rounds.round_id`.
- Sync `rounds` first:
  - Read `MAX(round_id)` from PostgreSQL, defaulting to `0`.
  - Select SQLite rows with a larger `round_id`, ordered ascending, limited to 1,000.
- Sync `measurement` second:
  - Read `MAX(id)` from PostgreSQL, defaulting to `0`.
  - Select SQLite rows with a larger `id`, ordered ascending, limited to 1,000.
  - Include only rows whose nullable `round_id` is already available in PostgreSQL after the rounds batch.
- Insert batches with parameterized bulk operations and `ON CONFLICT DO NOTHING`.
- Commit each table batch atomically, roll back and exit non-zero on failure, and always close both connections.
- Print concise progress showing previous maximum key, rows selected, rows inserted, and whether more source rows remain. No argument parser or continuous scheduling is added.

## Test Plan

- Empty PostgreSQL database: tables are created and the first batches are copied with original IDs preserved.
- Repeated invocation: only rows above PostgreSQL’s current maximum keys are considered and existing rows are not duplicated.
- Backlog over 1,000 rows: exactly 1,000 maximum are selected per table per invocation.
- Partial backlog: all available new rows below the limit are copied.
- Relationship ordering: rounds are inserted before measurements; measurements referencing unavailable rounds are deferred.
- Empty SQLite tables and fully synchronized databases exit successfully with zero inserted rows.
- Missing `.env`, invalid DSN, unavailable PostgreSQL, or missing SQLite database produces a clear error and no partial batch commit.
- Verify representative source and destination row counts/values for both tables after several manual runs.

## Assumptions

- `SOFTWARE/sensor_data.db` and the schema documented in `sensor_data_inspection_report.md` are authoritative; the older plural `measurements` schema in `auto_collect.py` is not used.
- SQLite is the source of truth and data is append-only for synchronization purposes; already copied PostgreSQL rows are never updated.
- The 1,000-row limit applies independently to each table, so one invocation can insert up to 2,000 total rows.
- Timestamps remain text to preserve the SQLite values exactly rather than introducing timezone conversion.
- `sqlite_sequence` is internal SQLite metadata and is not copied.
