#!/usr/bin/env python3
"""Poll the Wetterstation HTTP endpoint and store readings in PostgreSQL."""

import time

import psycopg2
import requests

import sync_postgres
from log_config import logger

URL = "http://10.10.134.75:8080/"  # URL der Wetterstation


def find_str(str, search_str, direction=1, start_index=None):
    """
    Sucht nach einem String in einem anderen String.

    :param str: String IN dem gesucht wird
    :param search_str: String NACH dem gesucht wird
    :param direction: Suchrichtung: 1 oder -1, von vorne nach hinten oder von hinten nach vorne
    :param start_index: Position an dem die Suche startet
    :return: Position des gesuchten Strings ODER None
    """

    if direction == 1:
        if start_index is None:
            index = 0
        else:
            index = start_index

        while index + len(search_str) <= len(str):
            if str[index:index + len(search_str)] == search_str:
                return index
            index = index + direction
    elif direction == -1:
        if start_index is None:
            index = len(str) - len(search_str)
        else:
            index = start_index

        while index >= 0:
            if str[index:index + len(search_str)] == search_str:
                return index
            index = index + direction
    else:
        raise ValueError("Direction must be 1 or -1")
    return None


def fetch_readings(url: str) -> list[list]:
    """Fetch and parse the Wetterstation page into (name, value, unit) rows."""
    response = requests.get(url)
    response.raise_for_status()

    content = str(response.content, encoding="utf-8")
    content = content.replace("<p>", "")
    content = content.replace("</p>", "")
    content = content.split("<br>")

    data = []
    for line in content:
        pos = find_str(line, ") : ")
        try:
            i = pos + 4
            while line[i + 1].isdigit() or line[i + 1] == ".":
                i += 1
            value = float(line[pos + 4:i + 1])
            unit = line[i + 2:]
            if unit == "1":
                unit = ""
        except Exception:
            continue
        name = line[:find_str(line, " (")]
        data.append([name, value, unit])
    return data


def create_schema(connection: psycopg2.extensions.connection) -> None:
    """Create the weather_variables/weather_data tables when missing."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_variables (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                unit TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_data (
                id SERIAL PRIMARY KEY,
                value_id INTEGER NOT NULL REFERENCES weather_variables(id),
                zeit DOUBLE PRECISION NOT NULL,
                wert DOUBLE PRECISION NOT NULL
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS weather_data_value_id_idx "
            "ON weather_data(value_id)"
        )
    connection.commit()


def variable_id(connection: psycopg2.extensions.connection, name: str, unit: str) -> int:
    """Look up a weather variable's id, creating it on first sight."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM weather_variables WHERE name = %s", (name,))
        row = cursor.fetchone()
        if row is not None:
            return row[0]

        cursor.execute(
            "INSERT INTO weather_variables (name, unit) VALUES (%s, %s) RETURNING id",
            (name, unit),
        )
        connection.commit()
        return cursor.fetchone()[0]


def store_readings(connection: psycopg2.extensions.connection, zeit: float, data: list[list]) -> None:
    with connection.cursor() as cursor:
        for name, value, unit in data:
            value_id = variable_id(connection, name, unit)
            cursor.execute(
                "INSERT INTO weather_data (value_id, zeit, wert) VALUES (%s, %s, %s)",
                (value_id, zeit, value),
            )
    connection.commit()


def main() -> None:
    dsn = sync_postgres.load_postgres_dsn("tailscale")
    logger.info("Testing PostgreSQL connection")
    sync_postgres.test_postgres_connection(dsn)
    logger.info("PostgreSQL connection successful")

    logger.info("Fetching Wetterstation data from %s", URL)
    data = fetch_readings(URL)
    zeit = time.time()

    with sync_postgres.postgres_connect(dsn) as connection:
        create_schema(connection)
        store_readings(connection, zeit, data)

    logger.info("Stored %d Wetterstation readings", len(data))


if __name__ == "__main__":
    main()
