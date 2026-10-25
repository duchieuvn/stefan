# backend

The services that run on the server: sensor data logger, Postgres sync, SQLite reduction
and water pump, run and watched by `manager/manage_services.py` (`python manager/manage_services.py` opens
its terminal UI).

## Layout

```
setup.sh, environment.yml, requirements.txt, Dockerfile.test   machine setup and packaging
manager/        manage_services.py (run and watch the services), service_runtime.py
services/
  sensor_logger/   Modbus/LoRa sensor logger, CW logger, OTA firmware update, .env.prod/.env.test
  postgres_sync/   SQLite -> PostgreSQL sync (see its README)
  sqlite_reduce/   reduced copy of the SQLite database
  water_pump/      scheduled water pump
experiments/    scripts/, notebooks/, docs/, results/ (LoRa sweep tests and their CSVs)
data/, logs/    runtime SQLite files and one log per service (not in git)
```

## Setting up a machine

On Ubuntu, as the user the services run as:

```bash
git clone https://github.com/duchieuvn/stefan.git
bash stefan/backend/setup.sh
```

The first run asks for the name of this machine's new database (e.g. `sensor_data_site2`)
and the password of the PostgreSQL user `holzofen`, then:

1. installs sqlite3, git and curl, and adds you to the `dialout` group (serial ports);
2. installs tailscale if needed and logs the machine in (it prints a link to open);
3. installs Miniforge if there is no conda, and creates the `stefan` env from
   `environment.yml`;
4. creates the database on the PostgreSQL server (`100.100.51.120:5432`) and writes the
   config files that don't exist yet: `services/sensor_logger/.env` (a link to `.env.prod`) and
   `services/postgres_sync/.env`;
5. adds the `@reboot` crontab line and starts the services. The sync then copies the
   SQLite data into the new database every 15 minutes.

Re-running it is safe: it only adds what is missing and never overwrites config or data.

| Option | Use |
|---|---|
| `--test` | Link `services/sensor_logger/.env` to `.env.test` (test hardware) |
| `--import-sqlite DIR` | Copy `sensor_data.db` / `reduced_data.db` from `DIR` first, e.g. from an old machine; the sync then copies them into PostgreSQL |
| `DB_NAME=... DB_PASSWORD=... bash setup.sh` | No questions |
| `DB_SERVER=host:port bash setup.sh` | Another PostgreSQL server |

Afterwards, `sudo reboot` and check that `python manager/manage_services.py status` shows the
services again.
