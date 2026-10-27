#!/usr/bin/env python3

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "manager"))  # for db_path
import db_path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"   # backend/data (kernbild_01/data on the Pi)
OUT = DATA_DIR / "reduced_data.db"

# Nur Runden behalten, deren round_id durch 5 teilbar ist:
# 5, 10, 15, 20, ...
STEP = 5


def reduce_database(src: Path | None = None, out: Path = OUT, step: int = STEP) -> None:
    # The sensor database from backend/data_path.env (asked on the first run)
    src = src or db_path.sqlite_path()
    if not src.exists():
        raise SystemExit(f"Quelldatenbank nicht gefunden: {src}")

    if out.exists():
        out.unlink()
        print(f"Alte Ausgabedatenbank gelöscht: {out}")

    # ------------------------------------------------------------
    # Neue Datenbank erstellen und Quelldatenbank anhängen
    # ------------------------------------------------------------

    con = sqlite3.connect(out)

    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA temp_store=MEMORY")

    src_path = src.resolve().as_posix()
    con.execute("ATTACH DATABASE ? AS src", (src_path,))

    # ------------------------------------------------------------
    # Tabellenstrukturen auslesen
    # ------------------------------------------------------------

    tables = con.execute(
        """
        SELECT name, sql
        FROM src.sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
          AND sql IS NOT NULL
        ORDER BY name
        """
    ).fetchall()

    if not tables:
        con.close()
        raise SystemExit("Keine Tabellen in der Quelldatenbank gefunden.")

    # ------------------------------------------------------------
    # Tabellen erstellen
    # ------------------------------------------------------------

    for table_name, create_sql in tables:
        con.execute(create_sql)

    con.commit()

    # ------------------------------------------------------------
    # Daten kopieren
    # ------------------------------------------------------------

    for table_name, _ in tables:
        quoted_table = table_name.replace('"', '""')

        columns = con.execute(
            f'PRAGMA src.table_info("{quoted_table}")'
        ).fetchall()

        column_names = [column[1] for column in columns]

        source_count = con.execute(
            f'SELECT COUNT(*) FROM src."{quoted_table}"'
        ).fetchone()[0]

        if "round_id" in column_names:
            # Nur jede fünfte Runde übernehmen:
            # round_id 5, 10, 15, 20, ...
            con.execute(
                f"""
                INSERT INTO "{quoted_table}"
                SELECT *
                FROM src."{quoted_table}"
                WHERE round_id % ? = 0
                """,
                (step,)
            )

            target_count = con.execute(
                f'SELECT COUNT(*) FROM "{quoted_table}"'
            ).fetchone()[0]

            print(
                f"{table_name}: "
                f"{target_count:,} von {source_count:,} Zeilen übernommen "
                f"(nur jede {step}. Runde)"
            )

        else:
            # Tabellen ohne round_id vollständig übernehmen
            con.execute(
                f"""
                INSERT INTO "{quoted_table}"
                SELECT *
                FROM src."{quoted_table}"
                """
            )

            print(
                f"{table_name}: "
                f"{source_count:,} Zeilen vollständig übernommen"
            )

        con.commit()

    # ------------------------------------------------------------
    # Indizes und Trigger wiederherstellen
    # ------------------------------------------------------------

    schema_objects = con.execute(
        """
        SELECT type, name, sql
        FROM src.sqlite_master
        WHERE type IN ('index', 'trigger', 'view')
          AND name NOT LIKE 'sqlite_%'
          AND sql IS NOT NULL
        ORDER BY
            CASE type
                WHEN 'index' THEN 1
                WHEN 'trigger' THEN 2
                WHEN 'view' THEN 3
            END
        """
    ).fetchall()

    for object_type, object_name, create_sql in schema_objects:
        try:
            con.execute(create_sql)
            print(f"{object_type.capitalize()} erstellt: {object_name}")
        except sqlite3.Error as error:
            print(
                f"{object_type.capitalize()} konnte nicht erstellt werden: "
                f"{object_name} – {error}"
            )

    con.commit()

    # ------------------------------------------------------------
    # Abschluss
    # ------------------------------------------------------------

    con.execute("DETACH DATABASE src")
    con.execute("VACUUM")
    con.close()

    source_size_mb = src.stat().st_size / 1024 / 1024
    output_size_mb = out.stat().st_size / 1024 / 1024

    print()
    print("Fertig.")
    print(f"Quelle:  {src} – {source_size_mb:.1f} MB")
    print(f"Ausgabe: {out} – {output_size_mb:.1f} MB")
    print(f"Behalten wurden die Runden {step}, {2 * step}, {3 * step}, {4 * step} usw.")


if __name__ == "__main__":
    reduce_database()
