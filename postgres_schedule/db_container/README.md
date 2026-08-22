## Export data dump from the original PostgreSQL database

sudo pg_dump -U holzofen -h localhost -F c -b -v -f "/home/holzofen/Schreibtisch/data_backup/sensor_data.dump" sensor_data
password: biofilter

## Download the dump to the destination machine

scp holzofen@10.10.64.27:/home/holzofen/Schreibtisch/data_backup/sensor_data.dump /home/duchieuvn/Code/stefan/postgres_schedule/

## Configure Tailscale

Create a `.env` file in this directory with an auth key from the Tailscale admin console:

```dotenv
TS_AUTHKEY=tskey-auth-REPLACE_WITH_YOUR_KEY
```

Do not commit the auth key. The `tailscale-state/` directory stores the node identity between restarts.

## Build and run the containers

docker compose up -d --build

PostgreSQL shares the Tailscale container's network namespace and is available from an authenticated Tailnet device at `docker-postgres:5432` or the sidecar's Tailnet IP. No Docker host port is published.

## Persist database files on the host

The compose file mounts PostgreSQL data to a host directory so your database files survive container stop/remove events:

```sh
${POSTGRES_DATA_DIR:-./postgres-data} -> /var/lib/postgresql/data
```

By default, data is stored in `./postgres-data` (next to `docker-compose.yml`).

Optional: choose another host path in a `.env` file:

```dotenv
POSTGRES_DATA_DIR=/absolute/path/on/host/postgres-data
```

If this is the first run, create the directory before starting containers:

```sh
mkdir -p ./postgres-data
```

View startup logs:

```sh
docker compose logs -f tailscale postgres
```
