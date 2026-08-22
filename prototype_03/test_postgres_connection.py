import logging
import psycopg
from pathlib import Path
from database import PostgresTunnelDatabase, PostgresTunnelSettings, DatabaseConnectionError

def test_connection():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(message)s")
    logging.info("Attempting to connect to PostgreSQL via SSH tunnel...")

    db_provider = None
    conn = None
    try:
        # Determine the path to the .env file.
        # Assuming this script is in 'prototype_03' and .env is in 'SOFTWARE'
        # which is a sibling directory to 'prototype_03'.
        # Current script: /home/duchieuvn/Code/stefan/prototype_03/test_postgres_connection.py
        # .env location:  /home/duchieuvn/Code/stefan/SOFTWARE/.env
        script_dir = Path(__file__).resolve().parent
        env_path = Path(".env")

        if not env_path.is_file():
            logging.error(f"'.env' file not found at expected path: {env_path}")
            print(f"Error: '.env' file not found. Please ensure it exists at {env_path} and contains PostgreSQL/SSH credentials.")
            return

        settings = PostgresTunnelSettings.from_environment(env_path=env_path)
        db_provider = PostgresTunnelDatabase(settings)

        logging.info("Establishing database connection...")
        conn = db_provider.connect()
        logging.info("Successfully connected to PostgreSQL.")

        # Run a simple query
        with conn.cursor() as cursor:
            query = """
                SELECT sender_id, receiver_id, ok_match, rssi, round_id, pc_ts
                FROM measurement
                ORDER BY pc_ts DESC
                LIMIT 10;
            """
            logging.info(f"Executing query:\n{query.strip()}")
            cursor.execute(query)
            results = cursor.fetchall()

            print(f"\n--- PostgreSQL Query Result (10 most recent rows from 'measurement') ---")
            if not results:
                print("No results found in 'measurement' table.")
            else:
                header = [desc[0] for desc in cursor.description]
                print(" | ".join(header))
                print("-" * (len(" | ".join(header)) + 2))
                for row in results:
                    print(" | ".join(map(str, row)))
            print(f"------------------------------------------------------------------------\n")

    except DatabaseConnectionError as e:
        logging.error(f"Database connection error: {e}")
        print(f"Error: Could not connect to PostgreSQL. Please check your .env configuration and network settings.")
        print(f"Details: {e}")
    except psycopg.Error as e:
        logging.error(f"PostgreSQL error: {e}")
        print(f"Error: An error occurred while interacting with the PostgreSQL database.")
        print(f"Details: {e}")
    except Exception as e:
        logging.exception("An unexpected error occurred:")
        print(f"Error: An unexpected error occurred: {e}")
    finally:
        if conn:
            try:
                conn.close()
                logging.info("PostgreSQL connection closed.")
            except Exception as e:
                logging.warning(f"Error closing PostgreSQL connection: {e}")
        if db_provider:
            try:
                db_provider.close()
                logging.info("SSH tunnel closed.")
            except Exception as e:
                logging.warning(f"Error closing SSH tunnel: {e}")

if __name__ == "__main__":
    test_connection()