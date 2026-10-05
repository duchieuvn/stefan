#!/usr/bin/env python3
"""Run, watch and control the backend background services.

A manager daemon owns the services: it imports each service script and calls its
run(ctx, **settings) in a child process of its own (see service_runtime.py), so it
can stop it cleanly, trigger its scheduled job early, restart it when it crashes and
show what it is doing. Everything else here is a client of that daemon:

    python manage_services.py                 # interactive terminal UI
    python manage_services.py status          # print status and exit
    python manage_services.py start-all       # start all stopped services in order (e.g. cron @reboot)
    python manage_services.py stop-all        # stop all running services
    python manage_services.py start pump sync # start / stop / run-now take service keys
    python manage_services.py stop pump
    python manage_services.py run-now pump    # run a scheduled job now
    python manage_services.py shutdown        # stop all services and the daemon
    python manage_services.py daemon          # run the daemon in the foreground

The UI and the start commands start the daemon in the background when it is not
running yet. Quitting the UI leaves the daemon and its services running. Run this
with the Python environment the services need (e.g. the `stefan` conda env); the
daemon and the services use that same interpreter.

Copies of a service started by hand (e.g. with nohup) are found by scanning running
python processes for the script name: they are shown, can be stopped, and block
starting a second copy.
"""

from __future__ import annotations

import argparse
import curses
import fcntl
import json
import locale
import multiprocessing
import os
import signal
import socket
import socketserver
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from multiprocessing.connection import Connection
from pathlib import Path

import service_runtime

BACKEND_DIR = Path(__file__).resolve().parent
PYTHON = os.environ.get("SERVICES_PYTHON", sys.executable)
SOCKET_PATH = BACKEND_DIR / "manage_services.sock"
PID_PATH = BACKEND_DIR / "manage_services.pid"
DAEMON_LOG = BACKEND_DIR / "manage_services.log"

START_GAP_S = 2.0            # pause between starts when starting several services
STOP_GRACE_S = 15.0          # after a stop request, wait this long before SIGTERM
KILL_GRACE_S = 5.0           # after SIGTERM, wait this long before SIGKILL
RESTART_DELAY_MIN_S = 5.0    # first restart after a crash; doubles per crash ...
RESTART_DELAY_MAX_S = 300.0  # ... up to this
STABLE_RUN_S = 600.0         # a crash after running this long restarts after the minimum delay again
TICK_S = 0.5                 # daemon housekeeping interval
REFRESH_MS = 1000            # UI refresh interval


@dataclass(frozen=True)
class Service:
    key: str                        # short name for the command line
    name: str
    script: str                     # relative to BACKEND_DIR; defines run(ctx, **settings)
    log: str                        # relative to BACKEND_DIR; output is appended
    settings: dict = field(default_factory=dict)   # keyword arguments for run()
    run_now: bool = False           # has a scheduled job that "run now" can start early

    @property
    def script_path(self) -> Path:
        return BACKEND_DIR / self.script

    @property
    def log_path(self) -> Path:
        return BACKEND_DIR / self.log


# Listed in start order.
SERVICES: tuple[Service, ...] = (
    Service(
        "sensor",
        "Sensor data logger",
        "master/main_senor_data.py",
        "master/main_senor_data.log",
        # Don't rewrite the nodes' LoRa settings; read them from the first node
        # (defaults if that fails) instead of asking on the terminal.
        settings={"lora": "read"},
    ),
    Service(
        "sync",
        "Postgres sync",
        "postgres_schedule/sync_scheduled_postgres.py",
        "postgres_schedule/schedule_postgres.log",
        settings={"interval_seconds": 900},
        run_now=True,
    ),
    Service(
        "reduce",
        "Reduce SQLite",
        "reduce_sqlite/scheduled_reduce_database.py",
        "reduce_sqlite/schedule_reduce.log",
        run_now=True,
    ),
    Service(
        "pump",
        "Water pump",
        "water_pumping/schedule_pump.py",
        "water_pumping/schedule_pump.log",
        run_now=True,
    ),
)
KEYS = {svc.key: idx for idx, svc in enumerate(SERVICES)}


# --- Helpers -----------------------------------------------------------------

def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# --- Copies started outside the manager ----------------------------------------

def scan_processes() -> dict[int, list[int]]:
    """Map service index -> PIDs of live python processes running its script."""
    by_name = {svc.script_path.name: i for i, svc in enumerate(SERVICES)}
    found: dict[int, list[int]] = {i: [] for i in range(len(SERVICES))}
    me = os.getpid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit() or int(entry.name) == me:
            continue
        try:
            raw = Path(entry.path, "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
        if len(argv) < 2 or not os.path.basename(argv[0]).startswith("python"):
            continue
        for arg in argv[1:]:
            idx = by_name.get(os.path.basename(arg))
            if idx is not None:
                found[idx].append(int(entry.name))
                break
    return found


class Stopper:
    """Stop processes gently: Ctrl+C first, then SIGTERM, then SIGKILL."""

    def __init__(self) -> None:
        self.pending: dict[int, tuple[int, float]] = {}  # pid -> (last signal, sent at)

    def request(self, pid: int) -> bool:
        """Begin stopping pid; False if we are not allowed to signal it."""
        if pid not in self.pending:
            return self._send(pid, signal.SIGINT)
        return True

    def _send(self, pid: int, sig: int) -> bool:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            self.pending.pop(pid, None)
            return True
        except PermissionError:  # e.g. started by another user
            self.pending.pop(pid, None)
            return False
        self.pending[pid] = (sig, time.monotonic())
        return True

    def tick(self, alive: set[int]) -> None:
        now = time.monotonic()
        for pid, (sig, sent) in list(self.pending.items()):
            if pid not in alive:
                del self.pending[pid]
            elif sig == signal.SIGINT and now - sent > STOP_GRACE_S:
                self._send(pid, signal.SIGTERM)
            elif sig == signal.SIGTERM and now - sent > KILL_GRACE_S:
                self._send(pid, signal.SIGKILL)


def all_pids(procs: dict[int, list[int]]) -> set[int]:
    return {pid for pids in procs.values() for pid in pids}


# --- Daemon ------------------------------------------------------------------

class Unit:
    """The daemon's view of one service."""

    def __init__(self, svc: Service) -> None:
        self.svc = svc
        self.state = "stopped"          # stopped | starting | running | stopping | restarting
        self.proc: multiprocessing.process.BaseProcess | None = None
        self.conn: Connection | None = None
        self.start_at = 0.0             # monotonic time of a queued start or restart
        self.started_at = 0.0           # wall-clock time the current process started
        self.stop_sent_at = 0.0
        self.forced = ""                # signal used when a stop had to be forced
        self.restarts = 0
        self.restart_delay = RESTART_DELAY_MIN_S
        self.last_exit = ""
        self.last_error = ""
        self.info: dict = {}            # status fields reported by the service


class Daemon:
    def __init__(self) -> None:
        self.units = [Unit(svc) for svc in SERVICES]
        # spawn: each service starts in a fresh interpreter, holding no copies of the
        # daemon's sockets, pipes or locks.
        self.mp = multiprocessing.get_context("spawn")
        self.external: dict[int, list[int]] = {i: [] for i in range(len(SERVICES))}
        self.stopper = Stopper()
        self.next_start = 0.0
        self.started = time.time()
        self.shutting_down = False
        self.signalled = False

    @property
    def finished(self) -> bool:
        return self.shutting_down and all(unit.proc is None for unit in self.units)

    # -- requests from clients --

    def handle(self, request: dict) -> dict:
        command = request.get("cmd")
        unknown = [key for key in request.get("services", []) if key not in KEYS]
        if unknown:
            return {"ok": False, "error": f"unknown service {', '.join(unknown)}"}
        indices = [KEYS[key] for key in request.get("services", [])]
        if command == "status":
            return self.status()
        if command == "start":
            return self.start(indices)
        if command == "stop":
            return self.stop(indices)
        if command == "run-now":
            return self.run_now(indices)
        if command == "shutdown":
            self.request_shutdown()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {command!r}"}

    def status(self) -> dict:
        self.scan()
        now, wall = time.monotonic(), time.time()
        services = []
        for idx, unit in enumerate(self.units):
            waiting = unit.state in ("starting", "restarting")
            services.append({
                "key": unit.svc.key,
                "state": unit.state,
                "pid": unit.proc.pid if unit.proc else None,
                "uptime": wall - unit.started_at if unit.proc else None,
                "start_in": max(0.0, unit.start_at - now) if waiting else None,
                "restarts": unit.restarts,
                "last_exit": unit.last_exit,
                "last_error": unit.last_error,
                "info": unit.info,
                "external_pids": self.external[idx],
                "external_stopping": any(pid in self.stopper.pending for pid in self.external[idx]),
            })
        daemon = {"pid": os.getpid(), "uptime": wall - self.started, "python": sys.executable,
                  "shutting_down": self.shutting_down}
        return {"ok": True, "daemon": daemon, "services": services}

    def start(self, indices: list[int]) -> dict:
        if self.shutting_down:
            return {"ok": False, "error": "the manager is shutting down"}
        self.scan()
        started, skipped, refused = [], [], []
        now = time.monotonic()
        for idx in indices:
            unit = self.units[idx]
            if unit.state not in ("stopped", "restarting"):
                skipped.append(unit.svc.name)
            elif self.external[idx]:
                refused.append(f"{unit.svc.name} (already running outside the manager, "
                               f"pid {', '.join(map(str, self.external[idx]))})")
            else:
                self.next_start = max(now, self.next_start)
                unit.state = "starting"
                unit.start_at = self.next_start
                unit.restart_delay = RESTART_DELAY_MIN_S
                self.next_start += START_GAP_S
                started.append(unit.svc.name)
        if started:
            log(f"Start requested: {', '.join(started)}")
        return {"ok": True, "started": started, "skipped": skipped, "refused": refused}

    def stop(self, indices: list[int]) -> dict:
        self.scan()
        stopping, denied = [], []
        for idx in indices:
            unit = self.units[idx]
            if self.stop_unit(unit):
                stopping.append(unit.svc.name)
            for pid in self.external[idx]:
                if self.stopper.request(pid):
                    stopping.append(f"{unit.svc.name} (pid {pid}, outside the manager)")
                else:
                    denied.append(pid)
        if stopping:
            log(f"Stop requested: {', '.join(stopping)}")
        return {"ok": True, "stopping": stopping, "denied": denied}

    def run_now(self, indices: list[int]) -> dict:
        sent, errors = [], []
        for idx in indices:
            unit = self.units[idx]
            if not unit.svc.run_now:
                errors.append(f"{unit.svc.name} has no scheduled job to run now")
            elif unit.state != "running":
                errors.append(f"{unit.svc.name} is not running")
            else:
                self.send(unit, "run-now")
                sent.append(unit.svc.name)
        if sent:
            log(f"Run now requested: {', '.join(sent)}")
        return {"ok": True, "sent": sent, "errors": errors}

    def request_shutdown(self) -> None:
        if not self.shutting_down:
            log("Shutting down: stopping all services")
        self.shutting_down = True
        for unit in reversed(self.units):
            self.stop_unit(unit)

    # -- housekeeping --

    def tick(self) -> None:
        if self.signalled and not self.shutting_down:
            self.request_shutdown()
        if self.stopper.pending:
            self.scan()
        now = time.monotonic()
        for idx, unit in enumerate(self.units):
            self.receive(unit)
            if unit.proc is not None and unit.proc.exitcode is not None:
                self.reap(unit)
            elif unit.state == "stopping":
                self.escalate(unit, now)
            elif unit.state in ("starting", "restarting") and now >= unit.start_at:
                self.launch(idx, unit)

    def scan(self) -> None:
        self.external = scan_processes()
        self.stopper.tick(all_pids(self.external))

    def stop_unit(self, unit: Unit) -> bool:
        if unit.state in ("starting", "restarting"):
            unit.state = "stopped"      # cancel the queued start
            return True
        if unit.state == "running":
            self.send(unit, "stop")
            unit.state = "stopping"
            unit.stop_sent_at = time.monotonic()
            unit.forced = ""
            return True
        return False

    def launch(self, idx: int, unit: Unit) -> None:
        self.scan()
        if self.external[idx]:
            unit.state = "stopped"
            unit.last_error = (f"not started: already running outside the manager "
                               f"(pid {', '.join(map(str, self.external[idx]))})")
            log(f"{unit.svc.name}: {unit.last_error}")
            return
        parent, child = self.mp.Pipe()
        proc = self.mp.Process(
            target=service_runtime.child_main,
            args=(str(unit.svc.script_path), str(unit.svc.log_path), dict(unit.svc.settings), child),
            name=f"service-{unit.svc.key}",
        )
        try:
            proc.start()
        except Exception as error:
            parent.close()
            unit.last_error = f"could not start: {error}"
            self.crashed(unit, f"failed to start {stamp()}")
            return
        finally:
            child.close()
        unit.proc, unit.conn = proc, parent
        unit.state = "running"
        unit.started_at = time.time()
        unit.info = {}
        log(f"{unit.svc.name} started (pid {proc.pid})")

    def receive(self, unit: Unit) -> None:
        if unit.conn is None:
            return
        try:
            while unit.conn.poll():
                kind, payload = unit.conn.recv()
                if kind == "info":
                    unit.info.update(payload)
                elif kind == "error":
                    unit.last_error = payload
        except (EOFError, OSError):
            unit.conn.close()
            unit.conn = None

    def send(self, unit: Unit, message: str) -> None:
        if unit.conn is not None:
            try:
                unit.conn.send(message)
            except OSError:
                pass

    def reap(self, unit: Unit) -> None:
        self.receive(unit)
        code = unit.proc.exitcode
        ran_for = time.time() - unit.started_at
        unit.proc.close()
        unit.proc = None
        if unit.conn is not None:
            unit.conn.close()
            unit.conn = None
        unit.info.pop("activity", None)
        unit.info.pop("next_run", None)
        if unit.state == "stopping":
            unit.state = "stopped"
            unit.last_exit = f"stopped {stamp()}" + (f" (forced with {unit.forced})" if unit.forced else "")
            log(f"{unit.svc.name} {unit.last_exit}")
        elif code == 0:
            unit.state = "stopped"
            unit.last_exit = f"finished on its own {stamp()}"
            log(f"{unit.svc.name} {unit.last_exit}")
        else:
            reason = f"exit code {code}" if code > 0 else f"killed by {signal.Signals(-code).name}"
            if ran_for >= STABLE_RUN_S:
                unit.restart_delay = RESTART_DELAY_MIN_S
            self.crashed(unit, f"crashed {stamp()} ({reason})")

    def crashed(self, unit: Unit, why: str) -> None:
        unit.last_exit = why
        if self.shutting_down:
            unit.state = "stopped"
            log(f"{unit.svc.name} {why}")
            return
        unit.restarts += 1
        unit.state = "restarting"
        unit.start_at = time.monotonic() + unit.restart_delay
        log(f"{unit.svc.name} {why}; restart #{unit.restarts} in {unit.restart_delay:.0f}s"
            + (f" (last error: {unit.last_error})" if unit.last_error else ""))
        unit.restart_delay = min(unit.restart_delay * 2, RESTART_DELAY_MAX_S)

    def escalate(self, unit: Unit, now: float) -> None:
        waited = now - unit.stop_sent_at
        if not unit.forced and waited > STOP_GRACE_S:
            log(f"{unit.svc.name} did not stop within {STOP_GRACE_S:.0f}s; sending SIGTERM")
            unit.proc.terminate()
            unit.forced = "SIGTERM"
        elif unit.forced == "SIGTERM" and waited > STOP_GRACE_S + KILL_GRACE_S:
            log(f"{unit.svc.name} still running; sending SIGKILL")
            unit.proc.kill()
            unit.forced = "SIGKILL"


class RequestHandler(socketserver.StreamRequestHandler):
    """One JSON request line in, one JSON response line out."""

    timeout = 5.0

    def handle(self) -> None:
        try:
            request = json.loads(self.rfile.readline())
        except (OSError, ValueError):
            return
        try:
            response = self.server.manager.handle(request)
        except Exception as error:
            log(f"Request failed:\n{traceback.format_exc()}")
            response = {"ok": False, "error": f"manager error: {error}"}
        self.wfile.write(json.dumps(response).encode() + b"\n")


def run_daemon() -> int:
    lock = open(PID_PATH, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("The manager daemon is already running.", file=sys.stderr)
        return 1
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()

    daemon = Daemon()

    def on_signal(signum, frame):
        daemon.signalled = True   # handled in the loop, not in the middle of a tick

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    SOCKET_PATH.unlink(missing_ok=True)   # left behind by a daemon that was killed
    server = socketserver.UnixStreamServer(str(SOCKET_PATH), RequestHandler)
    server.manager = daemon
    server.timeout = TICK_S
    log(f"Manager daemon started (pid {os.getpid()}, python {sys.executable})")
    try:
        while not daemon.finished:
            server.handle_request()
            try:
                daemon.tick()
            except Exception:
                log(f"Housekeeping failed:\n{traceback.format_exc()}")
    finally:
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)
        PID_PATH.unlink(missing_ok=True)
        log("Manager daemon stopped")
    return 0


# --- Client ------------------------------------------------------------------

def call(command: str, services: list[str] | tuple[str, ...] = ()) -> dict:
    """Send one request to the daemon; raises OSError if it is not running."""
    payload = json.dumps({"cmd": command, "services": list(services)}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("the manager closed the connection")
            data += chunk
    return json.loads(data)


def ensure_daemon() -> None:
    """Start the daemon in the background unless it is running already."""
    try:
        call("status")
        return
    except OSError:
        pass
    with open(DAEMON_LOG, "ab") as daemon_log:
        subprocess.Popen(
            [PYTHON, "-u", str(Path(__file__).resolve()), "daemon"],
            cwd=BACKEND_DIR,
            stdin=subprocess.DEVNULL,
            stdout=daemon_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survives closing this terminal / quitting the UI
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            call("status")
            return
        except OSError:
            pass
    raise RuntimeError(f"The manager daemon did not start; see {DAEMON_LOG}")


def service_states(status: dict | None) -> list[dict | None]:
    """The daemon's status entries in SERVICES order (None where missing)."""
    by_key = {entry["key"]: entry for entry in status["services"]} if status else {}
    return [by_key.get(svc.key) for svc in SERVICES]


def describe(st: dict) -> tuple[str, str]:
    """One-line state of a service and its color key (ok / warn / bad)."""
    external = ", ".join(map(str, st["external_pids"]))
    if external:
        if st["external_stopping"]:
            return f"◌ stopping   pid {external} (outside manager)", "warn"
        return f"● running    pid {external} (outside manager)", "warn"
    state = st["state"]
    if state == "running":
        return f"● running    pid {st['pid']}  up {format_duration(st['uptime'])}", "ok"
    if state == "starting":
        return "◌ starting", "warn"
    if state == "stopping":
        return f"◌ stopping   pid {st['pid']}", "warn"
    if state == "restarting":
        return f"✖ crashed    restart #{st['restarts']} in {format_duration(st['start_in'])}", "bad"
    return "○ stopped", "bad"


INFO_LABELS = {"activity": "now", "last_run": "last run", "last_result": "result", "next_run": "next run"}


def detail_lines(st: dict) -> list[tuple[str, str, str]]:
    """(label, value, color key) lines describing one service in detail."""
    info = dict(st["info"])
    lines = [(label, str(info.pop(key)), "") for key, label in INFO_LABELS.items() if key in info]
    lines += [(key.replace("_", " "), str(value), "") for key, value in info.items()]
    if st["restarts"]:
        lines.append(("restarts", str(st["restarts"]), ""))
    if st["last_exit"]:
        lines.append(("last exit", st["last_exit"], ""))
    if st["last_error"]:
        lines.append(("last error", st["last_error"], "bad"))
    return lines


# --- Log tail ----------------------------------------------------------------

def tail_log(path: Path, count: int) -> list[str]:
    if count <= 0:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 16384))
            data = f.read().decode(errors="replace")
    except OSError:
        return ["(no log file yet)"]
    lines = []
    for line in data.split("\n"):
        # Countdown output rewrites the line with \r; keep only the last version.
        parts = [p for p in line.split("\r") if p.strip()]
        text = parts[-1] if parts else ""
        lines.append("".join(ch if ch.isprintable() else " " for ch in text.replace("\t", "    ")))
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-count:]


# --- Terminal UI -------------------------------------------------------------

class Tui:
    def __init__(self, screen: curses.window) -> None:
        self.screen = screen
        self.cursor = 0
        self.selected: set[int] = set()
        self.message = ""
        self.status: dict | None = None

        curses.curs_set(0)
        screen.keypad(True)
        screen.timeout(REFRESH_MS)
        self.colors = {"": 0, "ok": 0, "bad": 0, "warn": 0, "dim": curses.A_DIM}
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            for n, (key, color) in enumerate(
                (("ok", curses.COLOR_GREEN), ("bad", curses.COLOR_RED), ("warn", curses.COLOR_YELLOW)), 1
            ):
                curses.init_pair(n, color, background)
                self.colors[key] = curses.color_pair(n)

    # -- helpers --

    def call(self, command: str, indices: list[int] = ()) -> dict | None:
        try:
            response = call(command, [SERVICES[i].key for i in indices])
        except OSError:
            self.status = None
            self.message = f"The manager daemon is not reachable; see {DAEMON_LOG.name}."
            return None
        if not response.get("ok"):
            self.message = response.get("error", "The manager refused the request.")
            return None
        return response

    def refresh_state(self) -> None:
        try:
            self.status = call("status")
        except OSError:
            self.status = None

    def targets(self) -> list[int]:
        return sorted(self.selected) if self.selected else [self.cursor]

    def put(self, row: int, col: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= row < height and col < width - 1:
            try:
                self.screen.addnstr(row, col, text, width - 1 - col, attr)
            except curses.error:
                pass

    def rule(self, row: int, title: str) -> None:
        width = self.screen.getmaxyx()[1]
        self.put(row, 1, f"─ {title} ".ljust(max(0, width - 3), "─"), self.colors["dim"])

    # -- drawing --

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        bold, dim = curses.A_BOLD, self.colors["dim"]
        self.put(0, 1, "Backend services", bold)
        clock = datetime.now().strftime("%H:%M:%S")
        self.put(0, max(18, width - len(clock) - 2), clock)
        if self.status:
            daemon = self.status["daemon"]
            self.put(1, 1, f"manager pid {daemon['pid']}  up {format_duration(daemon['uptime'])}  "
                           f"python: {daemon['python']}", dim)
        else:
            self.put(1, 1, f"manager daemon not reachable (log: {DAEMON_LOG.name})", self.colors["bad"])

        states = service_states(self.status)
        self.put(3, 1, "    Sel  Service               Status", bold)
        for idx, svc in enumerate(SERVICES):
            row = 4 + idx
            pointer = ">" if idx == self.cursor else " "
            box = "[x]" if idx in self.selected else "[ ]"
            line_attr = curses.A_REVERSE if idx == self.cursor else 0
            self.put(row, 1, f" {pointer}  {box}  {idx + 1}. {svc.name:<18}", line_attr)
            st = states[idx]
            if st is None:
                self.put(row, 32, "? unknown", dim)
                continue
            text, color = describe(st)
            self.put(row, 32, text, self.colors[color])
            activity = st["info"].get("activity") if st["state"] == "running" else None
            if activity:
                self.put(row, 32 + max(38, len(text) + 2), str(activity), dim)

        svc, st = SERVICES[self.cursor], states[self.cursor]
        top = 5 + len(SERVICES)
        self.rule(top, svc.name)
        details = (detail_lines(st) if st else []) or [("", "no status yet", "")]
        for i, (label, value, color) in enumerate(details):
            self.put(top + 1 + i, 3, f"{label:<11} {value}", self.colors[color])

        log_top = top + 2 + len(details)
        self.rule(log_top, f"Log: {svc.log}")
        footer_rows = 3
        log_rows = height - log_top - 1 - footer_rows
        for i, line in enumerate(tail_log(svc.log_path, log_rows)):
            self.put(log_top + 1 + i, 1, line)

        if self.message:
            self.put(height - 3, 1, self.message, self.colors["warn"] | bold)
        self.put(height - 2, 1, f"↑/↓ move   space select   a select all/none   1-{len(SERVICES)} jump", dim)
        self.put(height - 1, 1, "s start   x stop   n run now   q quit (services keep running)", dim)
        self.screen.refresh()

    def show_message(self, text: str) -> None:
        self.message = text
        self.draw()

    def confirm(self, question: str) -> bool:
        self.show_message(f"{question} [y/N]")
        self.screen.timeout(-1)
        try:
            key = self.screen.getch()
        finally:
            self.screen.timeout(REFRESH_MS)
        return key in (ord("y"), ord("Y"))

    # -- actions --

    def start_selected(self) -> None:
        try:
            ensure_daemon()
        except RuntimeError as error:
            self.message = str(error)
            return
        response = self.call("start", self.targets())
        self.selected.clear()
        if response is None:
            return
        parts = []
        if response["started"]:
            parts.append(f"Starting: {', '.join(response['started'])}.")
        if response["skipped"]:
            parts.append(f"Already running: {', '.join(response['skipped'])}.")
        if response["refused"]:
            parts.append(f"Not started: {'; '.join(response['refused'])}.")
        self.message = " ".join(parts)

    def stop_selected(self) -> None:
        self.refresh_state()
        states = service_states(self.status)
        running = [i for i in self.targets()
                   if states[i] and (states[i]["state"] != "stopped" or states[i]["external_pids"])]
        if not running:
            self.message = "Nothing to stop: the chosen services are not running."
            return
        names = ", ".join(SERVICES[i].name for i in running)
        if not self.confirm(f"Stop {names}?"):
            self.message = "Stop cancelled."
            return
        response = self.call("stop", running)
        self.selected.clear()
        if response is None:
            return
        self.message = f"Stopping {names} (forced after {STOP_GRACE_S:.0f}s)."
        if response["denied"]:
            self.message += (f" Permission denied for pid {', '.join(map(str, response['denied']))}"
                             " (another user's process?).")

    def run_now_selected(self) -> None:
        chosen = [i for i in self.targets() if SERVICES[i].run_now]
        if not chosen:
            self.message = "Nothing to run now: the chosen services have no scheduled job."
            return
        names = ", ".join(SERVICES[i].name for i in chosen)
        if not self.confirm(f"Run {names} now?"):
            self.message = "Run now cancelled."
            return
        response = self.call("run-now", chosen)
        self.selected.clear()
        if response is None:
            return
        parts = []
        if response["sent"]:
            parts.append(f"Run now sent to: {', '.join(response['sent'])}.")
        if response["errors"]:
            parts.append(f"{'; '.join(response['errors'])}.")
        self.message = " ".join(parts)

    def run(self) -> None:
        while True:
            self.refresh_state()
            self.draw()
            key = self.screen.getch()
            if key == -1:
                continue
            if key not in (ord("s"), ord("x"), ord("n")):
                self.message = ""
            if key in (ord("q"), ord("Q")):
                return
            if key in (curses.KEY_UP, ord("k")):
                self.cursor = (self.cursor - 1) % len(SERVICES)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.cursor = (self.cursor + 1) % len(SERVICES)
            elif ord("1") <= key < ord("1") + len(SERVICES):
                self.cursor = key - ord("1")
            elif key == ord(" "):
                self.selected ^= {self.cursor}
            elif key == ord("a"):
                everything = set(range(len(SERVICES)))
                self.selected = set() if self.selected == everything else everything
            elif key == ord("s"):
                self.start_selected()
            elif key == ord("x"):
                self.stop_selected()
            elif key == ord("n"):
                self.run_now_selected()


# --- Plain command-line modes ------------------------------------------------

def print_status() -> None:
    try:
        status = call("status")
    except OSError:
        print("Manager daemon: not running")
        procs = scan_processes()
        for idx, svc in enumerate(SERVICES):
            pids = procs[idx]
            state = f"running  pid {', '.join(map(str, pids))} (outside manager)" if pids else "stopped"
            print(f"{idx + 1}. {svc.name:<20} {state}")
        return
    daemon = status["daemon"]
    print(f"Manager daemon: pid {daemon['pid']}, up {format_duration(daemon['uptime'])}")
    for idx, (svc, st) in enumerate(zip(SERVICES, service_states(status))):
        if st is None:
            print(f"{idx + 1}. {svc.name:<20} ? unknown")
            continue
        text, _ = describe(st)
        activity = st["info"].get("activity") if st["state"] == "running" else None
        print(f"{idx + 1}. {svc.name:<20} {text}" + (f"   {activity}" if activity else ""))
        if st["last_error"] and st["state"] != "running":
            print(f"   last error: {st['last_error']}")


def wait_until(done, timeout: float) -> None:
    """Poll the daemon's status until done(states) is true or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if done(service_states(call("status"))):
                return
        except OSError:
            return
        time.sleep(0.5)


def start_services(keys: list[str]) -> int:
    ensure_daemon()
    response = call("start", keys)
    if not response["ok"]:
        print(response["error"])
        return 1
    for label, names in (("Starting", response["started"]), ("Already running", response["skipped"]),
                         ("Not started", response["refused"])):
        if names:
            print(f"{label}: {'; '.join(names)}")
    if response["started"]:
        # Wait for the queued starts, then give the services a moment to fail.
        wait_until(lambda states: not any(st and st["state"] == "starting" for st in states),
                   START_GAP_S * len(keys) + 10)
        time.sleep(START_GAP_S)
    print_status()
    states = service_states(call("status"))
    crashed = [SERVICES[KEYS[key]].name for key in keys
               if states[KEYS[key]] and states[KEYS[key]]["state"] == "restarting"]
    if crashed:
        print(f"Crashed right after starting (will be restarted): {', '.join(crashed)}")
    return 1 if crashed or response["refused"] else 0


def stop_services(keys: list[str]) -> int:
    indices = [KEYS[key] for key in keys]
    try:
        response = call("stop", keys)
    except OSError:
        return stop_outside_manager(indices)
    for name in response["stopping"]:
        print(f"Stopping {name}")
    for pid in response["denied"]:
        print(f"  permission denied for pid {pid} (another user's process?)")
    wait_until(lambda states: all(states[i] is None
                                  or (states[i]["state"] == "stopped" and not states[i]["external_pids"])
                                  for i in indices),
               STOP_GRACE_S + KILL_GRACE_S + 10)
    print_status()
    return 0


def stop_outside_manager(indices: list[int]) -> int:
    """No daemon: stop copies started by hand (e.g. with nohup)."""
    stopper = Stopper()
    procs = scan_processes()
    for idx in reversed(indices):
        for pid in procs[idx]:
            print(f"Stopping {SERVICES[idx].name} (pid {pid})")
            if not stopper.request(pid):
                print(f"  permission denied for pid {pid} (another user's process?)")
    while stopper.pending:
        time.sleep(0.5)
        stopper.tick(all_pids(scan_processes()))
    print_status()
    return 0


def run_now(keys: list[str]) -> int:
    try:
        response = call("run-now", keys)
    except OSError:
        print("The manager daemon is not running.")
        return 1
    for name in response["sent"]:
        print(f"Run now sent to {name}")
    for error in response["errors"]:
        print(error)
    return 1 if response["errors"] else 0


def shutdown() -> int:
    try:
        call("shutdown")
    except OSError:
        print("The manager daemon is not running.")
        return 0
    print("Stopping all services and the manager daemon ...")
    deadline = time.monotonic() + STOP_GRACE_S + KILL_GRACE_S + 10
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            call("status")
        except OSError:
            print("Manager daemon stopped.")
            return 0
    print(f"The manager daemon is still running; see {DAEMON_LOG}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run, watch and control the backend background services.")
    parser.add_argument("command", nargs="?", default="tui",
                        choices=("tui", "status", "start", "stop", "run-now", "start-all", "stop-all",
                                 "shutdown", "daemon"),
                        help="default: interactive terminal UI")
    parser.add_argument("services", nargs="*", metavar="service",
                        help=f"for start, stop and run-now: {', '.join(KEYS)}")
    args = parser.parse_args()
    unknown = [key for key in args.services if key not in KEYS]
    if unknown:
        parser.error(f"unknown service {', '.join(unknown)} (choose from {', '.join(KEYS)})")
    if args.command in ("start", "stop", "run-now") and not args.services:
        parser.error(f"{args.command} needs at least one service: {', '.join(KEYS)}")

    try:
        if args.command == "daemon":
            return run_daemon()
        if args.command == "status":
            print_status()
            return 0
        if args.command in ("start", "start-all"):
            return start_services(args.services or list(KEYS))
        if args.command in ("stop", "stop-all"):
            return stop_services(args.services or list(KEYS))
        if args.command == "run-now":
            return run_now(args.services)
        if args.command == "shutdown":
            return shutdown()
        ensure_daemon()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        print(f"Lost the connection to the manager daemon: {error}; see {DAEMON_LOG}", file=sys.stderr)
        return 1
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(lambda screen: Tui(screen).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
