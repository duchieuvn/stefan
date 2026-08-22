#  psql -h 100.100.51.120 -p 5432 -U holzofen -d sensor_data
import psycopg2 # currently only work with psycopg2-2.9.10
try:
    conn = psycopg2.connect(
        host="100.100.51.120",
        port="5432",
        dbname="sensor_data",
        user="holzofen",
        password="biofilter",
        sslmode="prefer"  # Matches psql's default behavior
    )
    print("Connection successful!")
    conn.close()
except Exception as e:
    print(f"Connection failed: {e}")