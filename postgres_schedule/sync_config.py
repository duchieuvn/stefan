"""Source/target database configuration for scheduled syncs.

Each profile pairs a source SQLite database with target PostgreSQL DSNs
(one per connection type). Fill in the placeholders below before running
against a profile that isn't configured yet.
"""

from pathlib import Path


SQLITE_PATHS = {
    "main": "/home/holzofen/Schreibtisch/prototype_01/sensor_data.db",
    "reduced": "/home/holzofen/Schreibtisch/prototype_01/reduce_sqlite.db", 
}

POSTGRES_DSNS = {
    ("main", "tailscale"): "postgresql://holzofen:biofilter@100.100.51.120:5432/sensor_data",
    ("main", "local"): "postgresql://holzofen:biofilter@localhost:5432/sensor_data",
    ("reduced", "tailscale"): "postgresql://TODO:TODO@TODO:5432/TODO",  # TODO: fill in
    ("reduced", "local"): "postgresql://TODO:TODO@TODO:5432/TODO",  # TODO: fill in
}


def sqlite_path(source: str) -> Path:
    """Return the source SQLite path for the given profile."""
    if source not in SQLITE_PATHS:
        raise ValueError(f"Unknown source profile: {source}")
    return Path(SQLITE_PATHS[source])


def postgres_dsn(source: str, connection: str) -> str:
    """Return the destination DSN for the given source/connection profile."""
    key = (source, connection)
    if key not in POSTGRES_DSNS:
        raise ValueError(f"Unknown source/connection profile: {key}")
    dsn = POSTGRES_DSNS[key]
    if not dsn:
        raise RuntimeError(
            f"DSN for source={source!r} connection={connection!r} is not "
            "configured in sync_config.py"
        )
    return dsn
