# postgres_schedule

## sync_scheduled_postgres.py

Syncs a source SQLite database to a target PostgreSQL database. Source/target
pairs are named profiles defined in `sync_config.py` (`main`, `reduced`).

```bash
# Run once, main profile, over tailscale (defaults)
python sync_scheduled_postgres.py --once

# Run once, main profile, connecting to local postgres
python sync_scheduled_postgres.py --once --connection local

# Run once, reduced profile (fill in sync_config.py first)
python sync_scheduled_postgres.py --once --source reduced

# Run on a schedule (default interval: 2 hours), main profile over tailscale
python sync_scheduled_postgres.py

# Run on a schedule with a custom interval (in seconds)
python sync_scheduled_postgres.py --interval-seconds 900

# Background it and keep it running after the shell exits
nohup python sync_scheduled_postgres.py --source reduced > reduced_schedule.log 2>&1 &
```

`--help` lists all flags.
