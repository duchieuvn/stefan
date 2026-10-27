# postgres_sync

## sync_scheduled_postgres.py

Syncs a source SQLite database to a target PostgreSQL database. Source/target
pairs are named profiles (`main`, `reduced`) configured in `.env`:

| Key | Meaning |
|---|---|
| `<SOURCE>_SQLITE_PATH` | Source SQLite file, relative to `backend/` or absolute, e.g. `REDUCED_SQLITE_PATH`. `MAIN_SQLITE_PATH` is optional: it defaults to the sensor database in `backend/data_path.env` (asked on the first run) |
| `<SOURCE>_<CONNECTION>_DSN` | Target PostgreSQL DSN, e.g. `MAIN_TAILSCALE_DSN`, `REDUCED_LOCAL_DSN` |

`.env` is loaded from this directory, so the script works from any working
directory. Progress is printed to the console; redirect it to keep a log.

```bash
# Test the PostgreSQL connection only
python sync_scheduled_postgres.py --check
python sync_scheduled_postgres.py --check --source reduced

# Create the profile's database on its server if it doesn't exist yet (setup.sh does this)
python sync_scheduled_postgres.py --create-database

# Copy one batch (up to 1000 rounds + 1000 measurements), main profile, over tailscale (defaults)
python sync_scheduled_postgres.py --once

# Copy one batch, main profile, connecting to local postgres
python sync_scheduled_postgres.py --once --connection local

# Copy one batch, reduced profile (fill in the REDUCED_* entries in .env first)
python sync_scheduled_postgres.py --once --source reduced

# Run on a schedule (default interval: 2 hours); each run catches up fully
python sync_scheduled_postgres.py

# Run on a schedule with a custom interval (in seconds)
python sync_scheduled_postgres.py --interval-seconds 900

# Background it, keep it running after the shell exits, and log to a file
nohup python sync_scheduled_postgres.py --source reduced > reduced_schedule.log 2>&1 &
```

`--help` lists all flags.

## The target database

Each machine syncs into its own database on the PostgreSQL server (e.g.
`sensor_data_site2` on `100.100.51.120`): two machines in one database would skip each
other's rows, because their ids overlap. `backend/setup.sh` creates it with
`--create-database`.

The sync creates its tables itself (`create_postgres_schema`) and fills them from SQLite,
so a new, empty database fills itself on the next run. Schema changes go into
`create_postgres_schema` and must be additive (`ADD COLUMN IF NOT EXISTS`), because
existing databases are never recreated.
