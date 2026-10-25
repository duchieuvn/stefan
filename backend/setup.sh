#!/bin/bash
# Set up the backend on this machine in one go: system packages, tailscale, the conda env,
# config files, a new database on the PostgreSQL server, the cron autostart, then start the
# services. Idempotent: each step only adds what is missing and never overwrites existing
# config or data, so it is also safe to re-run on a machine that is already set up.
#
#   bash backend/setup.sh                       # production hardware config (services/sensor_logger/.env.prod)
#   bash backend/setup.sh --test                # test hardware config (services/sensor_logger/.env.test)
#   bash backend/setup.sh --import-sqlite DIR   # first copy sensor_data.db / reduced_data.db from DIR
#
# Run it as the user the services run as (it uses sudo itself). The first run asks for the
# name of this machine's new database (e.g. sensor_data_site2) and the password of the
# PostgreSQL user holzofen; DB_NAME=... and DB_PASSWORD=... in the environment skip that.
# DB_SERVER=host:port changes the server (default: 100.100.51.120:5432, over tailscale).
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANAGER="$BACKEND_DIR/manager/manage_services.py"
SYNC_DIR="$BACKEND_DIR/services/postgres_sync"
SENSOR_DIR="$BACKEND_DIR/services/sensor_logger"
SYNC_SCRIPT="$SYNC_DIR/sync_scheduled_postgres.py"
SYNC_ENV="$SYNC_DIR/.env"
MASTER_ENV="$SENSOR_DIR/.env"
ENV_NAME="stefan"
SERVICE_IMPORTS="pymodbus, serial, psycopg2, schedule, dotenv"
DB_SERVER="${DB_SERVER:-100.100.51.120:5432}"
DB_USER="holzofen"
USER="${USER:-$(id -un)}"

hardware="prod"
import_dir=""
while [ $# -gt 0 ]; do
    case "$1" in
        --test) hardware="test" ;;
        --import-sqlite) import_dir="${2:?--import-sqlite needs a directory}"; shift ;;
        -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

step() { echo; echo "== $*"; }
ok()   { echo "   ok:   $*"; }
did()  { echo "   done: $*"; }
warn() { echo "   WARN: $*"; }
die()  { echo "Error: $*" >&2; exit 1; }

# Value of KEY in a dotenv file, parsed by python-dotenv like the services do.
env_value() { "$PY" -c 'import sys; from dotenv import dotenv_values; print(dotenv_values(sys.argv[1]).get(sys.argv[2]) or "")' "$1" "$2"; }

# --- 1. Preflight ---------------------------------------------------------------------

step "1/8 Preflight"
[ "$(id -u)" -ne 0 ] || die "run this as the user the services run as, not as root (it uses sudo itself)"
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] && ok "$PRETTY_NAME" || warn "only tested on Ubuntu, this is ${PRETTY_NAME:-unknown}"
sudo -v || die "this script needs sudo"

# --- 2. System packages and groups ----------------------------------------------------

step "2/8 System packages and groups"
packages=()
for cmd in sqlite3 git curl; do
    command -v "$cmd" >/dev/null || packages+=("$cmd")
done
if [ ${#packages[@]} -gt 0 ]; then
    sudo apt-get update -q
    sudo apt-get install -y -q "${packages[@]}"
    did "installed ${packages[*]}"
else
    ok "sqlite3, git and curl are installed"
fi
# The sensor and pump services talk to /dev/ttyUSB* serial ports.
if id -nG "$USER" | grep -qw dialout; then
    ok "$USER is in group dialout"
else
    sudo usermod -aG dialout "$USER"
    did "added $USER to group dialout (takes effect at the next login; this run works around it)"
fi

# --- 3. Tailscale ---------------------------------------------------------------------

step "3/8 Tailscale (the way to the PostgreSQL server)"
if command -v tailscale >/dev/null; then
    ok "tailscale is installed"
else
    curl -fsSL https://tailscale.com/install.sh | sh
    did "installed tailscale"
fi
if ! tailscale status >/dev/null 2>&1; then
    echo "   Log this machine in to the tailnet: open the link below in a browser."
    sudo tailscale up
fi
ok "tailscale is connected ($(tailscale ip -4 | head -1))"

# --- 4. Conda env ---------------------------------------------------------------------

step "4/8 Conda env '$ENV_NAME'"
conda=""
for candidate in "${CONDA_EXE:-}" "$(type -P conda || true)" \
                 "$HOME/miniconda3/bin/conda" "$HOME/miniforge3/bin/conda" "$HOME/anaconda3/bin/conda"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then conda="$candidate"; break; fi
done
if [ -n "$conda" ]; then
    ok "conda found at $conda"
else
    # Miniforge: conda with the conda-forge channel, installs without a terms-of-service prompt.
    installer="$(mktemp --suffix=.sh)"
    curl -fsSL -o "$installer" \
        "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-$(uname -m).sh"
    bash "$installer" -b -p "$HOME/miniforge3"
    rm -f "$installer"
    conda="$HOME/miniforge3/bin/conda"
    "$conda" init bash >/dev/null
    did "installed Miniforge to ~/miniforge3 (conda activate $ENV_NAME works in new shells)"
fi

PY="$("$conda" info --base)/envs/$ENV_NAME/bin/python"
if [ ! -x "$PY" ]; then
    CONDA_ALWAYS_YES=true "$conda" env create -n "$ENV_NAME" -f "$BACKEND_DIR/environment.yml"
    did "created env $ENV_NAME from environment.yml"
elif ! "$PY" -c "import $SERVICE_IMPORTS" 2>/dev/null; then
    CONDA_ALWAYS_YES=true "$conda" env update -n "$ENV_NAME" -f "$BACKEND_DIR/environment.yml"
    did "updated env $ENV_NAME from environment.yml (service packages were missing)"
else
    ok "env $ENV_NAME exists"
fi
"$PY" -c "import $SERVICE_IMPORTS" || die "the services' packages don't import in $PY"
ok "service packages import ($PY)"

# --- 5. Config and database -----------------------------------------------------------

step "5/8 Config and database"
# Layout before the folders moved into services/: the untracked .env files stay behind on a git pull.
for old_new in "master/.env:$MASTER_ENV" "postgres_schedule/.env:$SYNC_ENV"; do
    old="$BACKEND_DIR/${old_new%%:*}"; new="${old_new#*:}"
    if { [ -e "$old" ] || [ -L "$old" ]; } && [ ! -e "$new" ] && [ ! -L "$new" ]; then
        cp -P "$old" "$new"
        did "${old_new%%:*} copied to ${new#"$BACKEND_DIR"/} (the old file is left in place)"
    fi
done
if [ -e "$MASTER_ENV" ] || [ -L "$MASTER_ENV" ]; then
    ok "services/sensor_logger/.env exists$( [ -L "$MASTER_ENV" ] && echo " (-> $(readlink "$MASTER_ENV"))")"
else
    ln -s ".env.$hardware" "$MASTER_ENV"
    did "services/sensor_logger/.env -> .env.$hardware"
fi

new_config=0
if [ -f "$SYNC_ENV" ]; then
    ok "services/postgres_sync/.env exists"
else
    new_config=1
    db_name="${DB_NAME:-}"
    db_password="${DB_PASSWORD:-}"
    until [[ "$db_name" =~ ^[a-z_][a-z0-9_]*$ ]]; do
        [ -z "$db_name" ] || warn "'$db_name' is not a valid name: use lowercase letters, digits and _"
        [ -t 0 ] || die "no terminal to ask for the database name; set DB_NAME=... instead"
        read -rp "   Name of this machine's new database on $DB_SERVER (e.g. sensor_data_site2): " db_name
    done
    if [ -z "$db_password" ]; then
        [ -t 0 ] || die "no terminal to ask for the password; set DB_PASSWORD=... instead"
        read -rsp "   Password of the PostgreSQL user $DB_USER: " db_password; echo
    fi
    # Used for this run (they win over the .env file), written to .env once the login works.
    data_dir="$(env_value "$MASTER_ENV" DATA_DIR)"
    case "$data_dir" in /*) ;; *) data_dir="$BACKEND_DIR/$data_dir" ;; esac   # like sensor_logger/config.py
    export MAIN_SQLITE_PATH="$data_dir/$(env_value "$MASTER_ENV" DB_FILENAME)"
    MAIN_TAILSCALE_DSN="$(DB_PASSWORD="$db_password" "$PY" -c '
import os, sys
from urllib.parse import quote
user, server, name = sys.argv[1:]
password = quote(os.environ["DB_PASSWORD"], safe="")
print(f"postgresql://{user}:{password}@{server}/{name}")
' "$DB_USER" "$DB_SERVER" "$db_name")"
    export MAIN_TAILSCALE_DSN
fi

if ! db_out="$("$PY" "$SYNC_SCRIPT" --create-database 2>&1)"; then
    die "couldn't log in to the PostgreSQL server and create the database:
       $(grep -v '^[[:space:]]' <<<"$db_out" | tail -1)"   # the error, without the traceback
fi
db_result="$(tail -1 <<<"$db_out" | sed 's/^\[[^]]*\] //')"
case "$db_result" in
    Created*) did "$db_result" ;;
    *)
        if [ "$new_config" = 1 ]; then
            # Two machines syncing into one database would skip each other's rows (same ids).
            warn "$db_result on the server. If another machine syncs into it, pick another name."
            if [ -t 0 ]; then
                read -rp "   Use it anyway? [y/N] " answer
                [[ "$answer" =~ ^[yY] ]] || die "nothing was written; re-run and pick another name"
            fi
        else
            ok "$db_result"
        fi
        ;;
esac

if [ "$new_config" = 1 ]; then
    (
        umask 077
        cat > "$SYNC_ENV" <<EOF
# Configuration for sync_scheduled_postgres.py (source SQLite paths and target DSNs).
# Written by setup.sh.

# main profile
MAIN_SQLITE_PATH=$MAIN_SQLITE_PATH
MAIN_TAILSCALE_DSN=$MAIN_TAILSCALE_DSN

# reduced profile (not synced automatically; fill in a DSN to sync it by hand)
REDUCED_SQLITE_PATH=$BACKEND_DIR/data/reduced_data.db
REDUCED_TAILSCALE_DSN=
EOF
    )
    did "wrote services/postgres_sync/.env"
fi
"$PY" "$SYNC_SCRIPT" --check >/dev/null 2>&1 || die "the sync can't log in; run: $PY $SYNC_SCRIPT --check"
ok "the sync can log in to its database"

# --- 6. Import existing SQLite data ---------------------------------------------------

step "6/8 SQLite data"
if [ -z "$import_dir" ]; then
    ok "nothing to import (the sensor service creates an empty database on first start)"
else
    for key in MAIN_SQLITE_PATH REDUCED_SQLITE_PATH; do
        target="$(env_value "$SYNC_ENV" "$key")"
        source_db="$import_dir/$(basename "$target")"
        if [ -e "$target" ]; then
            ok "$target exists, not overwritten"
        elif [ ! -f "$source_db" ]; then
            warn "$source_db not found, skipped"
        else
            mkdir -p "$(dirname "$target")"
            sqlite3 "$source_db" ".backup '$target'"   # consistent copy, even of a database in use
            did "imported $source_db -> $target"
        fi
    done
fi

# --- 7. Autostart ---------------------------------------------------------------------

step "7/8 Start at boot"
crontab_now="$(crontab -l 2>/dev/null || true)"
reboot_line="@reboot cd \"$BACKEND_DIR\" && \"$PY\" manager/manage_services.py start-all >> logs/manage_services.log 2>&1"
if grep -q "manager/manage_services.py start-all" <<<"$crontab_now"; then
    ok "crontab already starts the services at boot"
elif grep -q "[[:space:]]manage_services.py start-all" <<<"$crontab_now"; then
    # Line from before manage_services.py moved into manager/: replace it.
    { grep -v "[[:space:]]manage_services.py start-all" <<<"$crontab_now"; echo "$reboot_line"; } | crontab -
    did "crontab line updated: $reboot_line"
else
    { [ -n "$crontab_now" ] && printf '%s\n' "$crontab_now"; echo "$reboot_line"; } | crontab -
    did "added to crontab: $reboot_line"
fi
mkdir -p "$BACKEND_DIR/logs"

# --- 8. Start the services ------------------------------------------------------------

step "8/8 Start the services"
cd "$BACKEND_DIR"
start_all=("$PY" "$MANAGER" start-all)
# A fresh sudo login picks up the dialout group added above, without logging out.
id -nG | grep -qw dialout || start_all=(sudo -u "$USER" -H "${start_all[@]}")
# A service that crashes (e.g. its serial port isn't plugged in) is reported, not fatal:
# the manager keeps restarting it.
"${start_all[@]}" | sed 's/^/   /' || warn "not every service started cleanly; see the status above and its log"
for key in PORT OTA_PORT; do
    port="$(env_value "$MASTER_ENV" "$key")"
    [ -z "$port" ] || [ -e "$port" ] || warn "serial port $port ($key in services/sensor_logger/.env) is not connected"
done
pump_port="$(sed -n 's/^PORT = "\(.*\)"/\1/p' services/water_pump/pump_water.py)"
[ -z "$pump_port" ] || [ -e "$pump_port" ] || warn "pump serial port $pump_port (services/water_pump/pump_water.py) is not connected"

echo
echo "Setup complete. The sync copies the SQLite data into the new database every 15 minutes."
echo "Check that everything comes back after: sudo reboot"
