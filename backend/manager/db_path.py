"""Where the sensor SQLite database lives: one choice shared by the sensor logger,
the Postgres sync and the reduce job.

backend/data_path.env (git-ignored) holds SQLITE_PATH, relative to backend/ or
absolute. On the first run (no file yet) the user is asked whether to use the
default backend/data/sensor_data.db; without a terminal it fails instead of guessing.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_PATH_ENV = BACKEND_DIR / "data_path.env"
DEFAULT_SQLITE_PATH = "data/sensor_data.db"


def missing() -> bool:
    return not DATA_PATH_ENV.exists()


def resolve(path: str) -> Path:
    """A path from the config: relative paths are relative to backend/."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else BACKEND_DIR / p


def ask_sqlite_path(ask) -> str | None:
    """Ask whether to use the default database path, else for another one, and save it.

    ask(label) returns the answer, or None on cancel. Returns the saved path, or
    None if the user cancelled. Raises ValueError on an unusable answer.
    """
    answer = ask(f"Use the default SQLite database backend/{DEFAULT_SQLITE_PATH}? [Y/n]")
    if answer is None:
        return None
    if answer.strip().lower() in ("", "y", "yes"):
        path = DEFAULT_SQLITE_PATH
    elif answer.strip().lower() in ("n", "no"):
        path = ask("SQLite database path (relative to backend/ or absolute)")
        if path is None:
            return None
        path = path.strip()
        if not path:
            raise ValueError("No SQLite database path given.")
    else:
        raise ValueError(f"Not an answer: {answer!r} (answer y or n)")
    DATA_PATH_ENV.write_text(
        "# Sensor SQLite database, relative to backend/ or absolute.\n"
        "# Used by the sensor logger, the Postgres sync and the reduce job.\n"
        f"SQLITE_PATH={path}\n"
    )
    return path


def sqlite_path() -> Path:
    """The sensor database path; asks on the terminal on the first run."""
    if missing():
        if not sys.stdin.isatty():
            raise RuntimeError(f"{DATA_PATH_ENV} does not exist. Choose the SQLite database path once: "
                               "run 'manage_services.py start sensor' in a terminal")
        while True:
            try:
                if ask_sqlite_path(lambda label: input(f"{label}: ")) is not None:
                    break
            except ValueError as error:
                print(error)
        print(f"{DATA_PATH_ENV} written")
    for line in DATA_PATH_ENV.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "SQLITE_PATH" and value.strip():
            return resolve(value.strip().strip("\"'"))
    raise RuntimeError(f"{DATA_PATH_ENV} has no SQLITE_PATH=...")
