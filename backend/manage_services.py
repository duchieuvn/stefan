#!/usr/bin/env python3
"""Show, start and stop the backend background services from a terminal UI.

Run it with the Python environment the services need (e.g. the `stefan` conda env);
services are started with that same interpreter:

    python manage_services.py             # interactive terminal UI
    python manage_services.py status      # print status and exit
    python manage_services.py start-all   # start all stopped services in order (e.g. cron @reboot)
    python manage_services.py stop-all    # stop all running services

Services are detected by scanning running python processes for the script name, so
services started by hand with nohup are shown (and can be stopped) too. Started
services run in their own session, keep running after this UI exits, and append
their output to the log file listed below.
"""

from __future__ import annotations

import argparse
import curses
import locale
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PYTHON = os.environ.get("SERVICES_PYTHON", sys.executable)

START_GAP_S = 2.0      # pause between starts when starting several services
STOP_GRACE_S = 15.0    # after Ctrl+C (SIGINT), wait this long before SIGTERM
KILL_GRACE_S = 5.0     # after SIGTERM, wait this long before SIGKILL
REFRESH_MS = 1000


@dataclass(frozen=True)
class Service:
    name: str
    script: str                     # relative to BACKEND_DIR
    log: str                        # relative to BACKEND_DIR; output is appended
    args: tuple[str, ...] = ()
    stdin_text: str | None = None   # pre-typed answers for interactive prompts

    @property
    def script_path(self) -> Path:
        return BACKEND_DIR / self.script

    @property
    def log_path(self) -> Path:
        return BACKEND_DIR / self.log


# Listed in start order.
SERVICES: tuple[Service, ...] = (
    Service(
        "Sensor data logger",
        "master/main_senor_data.py",
        "master/main_senor_data.log",
        # Answers the LoRa start-up questions: "n" = don't rewrite the nodes' LoRa
        # settings, read them from the first node instead. The blank lines accept
        # the defaults if that read fails.
        stdin_text="n\n" + "\n" * 5,
    ),
    Service(
        "Postgres sync",
        "postgres_schedule/sync_scheduled_postgres.py",
        "postgres_schedule/schedule_postgres.log",
        args=("--interval-seconds", "900"),
    ),
    Service(
        "Reduce SQLite",
        "reduce_sqlite/scheduled_reduce_database.py",
        "reduce_sqlite/schedule_reduce.log",
    ),
    Service(
        "Water pump",
        "water_pumping/schedule_pump.py",
        "water_pumping/schedule_pump.log",
    ),
)


# --- Process discovery -------------------------------------------------------

def _clock_ticks() -> int:
    try:
        return os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return 100


CLK_TCK = _clock_ticks()


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


def process_uptime(pid: int) -> float | None:
    """Seconds since the process started, or None if unknown."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        system_uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, IndexError, ValueError):
        return None
    return max(0.0, system_uptime - start_ticks / CLK_TCK)


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


# --- Start / stop ------------------------------------------------------------

def start_service(svc: Service) -> subprocess.Popen:
    """Launch a service detached from this terminal, appending output to its log."""
    svc.log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().isoformat(timespec="seconds")
    with open(svc.log_path, "ab") as log:
        log.write(f"\n===== started by manage_services.py at {stamp} =====\n".encode())
        log.flush()
        proc = subprocess.Popen(
            [PYTHON, "-u", str(svc.script_path), *svc.args],
            cwd=svc.script_path.parent,
            stdin=subprocess.PIPE if svc.stdin_text else subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # survives closing this terminal / quitting the UI
        )
    if proc.stdin is not None:
        try:
            proc.stdin.write((svc.stdin_text or "").encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass
    return proc


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
        self.stopper = Stopper()
        self.launched: dict[int, subprocess.Popen] = {}  # service index -> process we started
        self.procs: dict[int, list[int]] = {}

        curses.curs_set(0)
        screen.keypad(True)
        screen.timeout(REFRESH_MS)
        self.colors = {"ok": 0, "bad": 0, "warn": 0, "dim": curses.A_DIM}
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

    def refresh_state(self) -> None:
        self.procs = scan_processes()
        for idx, proc in list(self.launched.items()):
            code = proc.poll()
            if code is not None:
                del self.launched[idx]
                stopped_by_us = proc.pid in self.stopper.pending
                if code != 0 and not stopped_by_us and not self.procs[idx]:
                    self.message = f"{SERVICES[idx].name} exited with code {code}. Check its log below."
        self.stopper.tick(all_pids(self.procs))

    def targets(self) -> list[int]:
        return sorted(self.selected) if self.selected else [self.cursor]

    def put(self, row: int, col: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= row < height and col < width - 1:
            try:
                self.screen.addnstr(row, col, text, width - 1 - col, attr)
            except curses.error:
                pass

    def status_of(self, idx: int) -> tuple[str, int]:
        pids = self.procs.get(idx, [])
        if not pids:
            return "○ stopped", self.colors["bad"]
        if any(pid in self.stopper.pending for pid in pids):
            return f"◌ stopping   pid {', '.join(map(str, pids))}", self.colors["warn"]
        uptime = format_duration(process_uptime(pids[0]))
        extra = f"  ({len(pids)} processes!)" if len(pids) > 1 else ""
        return f"● running    pid {pids[0]}  up {uptime}{extra}", self.colors["ok"]

    # -- drawing --

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        bold = curses.A_BOLD
        self.put(0, 1, "Backend services", bold)
        clock = datetime.now().strftime("%H:%M:%S")
        self.put(0, max(18, width - len(clock) - 2), clock)
        self.put(1, 1, f"python: {PYTHON}", self.colors["dim"])

        self.put(3, 1, "    Sel  Service               Status", bold)
        for idx, svc in enumerate(SERVICES):
            row = 4 + idx
            pointer = ">" if idx == self.cursor else " "
            box = "[x]" if idx in self.selected else "[ ]"
            line_attr = curses.A_REVERSE if idx == self.cursor else 0
            self.put(row, 1, f" {pointer}  {box}  {idx + 1}. {svc.name:<18}", line_attr)
            text, attr = self.status_of(idx)
            self.put(row, 32, text, attr)

        svc = SERVICES[self.cursor]
        log_top = 5 + len(SERVICES)
        self.put(log_top, 1, f"─ Log: {svc.log} ".ljust(max(0, width - 3), "─"), self.colors["dim"])
        footer_rows = 3
        log_rows = height - log_top - 1 - footer_rows
        for i, line in enumerate(tail_log(svc.log_path, log_rows)):
            self.put(log_top + 1 + i, 1, line)

        if self.message:
            self.put(height - 3, 1, self.message, self.colors["warn"] | bold)
        self.put(height - 2, 1, "↑/↓ move   space select   a select all/none   1-4 jump", self.colors["dim"])
        self.put(height - 1, 1, "s start selected   x stop selected   r refresh   q quit", self.colors["dim"])
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
        self.refresh_state()
        todo = [i for i in self.targets() if not self.procs[i]]
        skipped = [SERVICES[i].name for i in self.targets() if self.procs[i]]
        started, failed = [], []
        for n, idx in enumerate(todo):
            svc = SERVICES[idx]
            self.show_message(f"Starting {svc.name} ...")
            try:
                self.launched[idx] = start_service(svc)
                started.append(svc.name)
            except OSError as error:
                failed.append(f"{svc.name} ({error})")
            if n < len(todo) - 1:
                time.sleep(START_GAP_S)
        self.selected.clear()
        parts = []
        if started:
            parts.append(f"Started: {', '.join(started)}.")
        if skipped:
            parts.append(f"Already running: {', '.join(skipped)}.")
        if failed:
            parts.append(f"Failed: {', '.join(failed)}.")
        self.message = " ".join(parts)

    def stop_selected(self) -> None:
        self.refresh_state()
        running = [i for i in self.targets() if self.procs[i]]
        if not running:
            self.message = "Nothing to stop: the chosen services are not running."
            return
        names = ", ".join(SERVICES[i].name for i in running)
        if not self.confirm(f"Stop {names}?"):
            self.message = "Stop cancelled."
            return
        denied = []
        for idx in reversed(running):
            for pid in self.procs[idx]:
                if not self.stopper.request(pid):
                    denied.append(pid)
        self.selected.clear()
        self.message = f"Stopping {names} (Ctrl+C sent; forced after {STOP_GRACE_S:.0f}s)."
        if denied:
            self.message += f" Permission denied for pid {', '.join(map(str, denied))} (another user's process?)."

    def run(self) -> None:
        while True:
            self.refresh_state()
            self.draw()
            key = self.screen.getch()
            if key == -1:
                continue
            if key not in (ord("s"), ord("x")):
                self.message = ""
            if key in (ord("q"), ord("Q")):
                if self.stopper.pending:
                    self.show_message("Services are still stopping; they will finish on their own.")
                    time.sleep(1.5)
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


# --- Plain command-line modes ------------------------------------------------

def print_status() -> None:
    procs = scan_processes()
    for idx, svc in enumerate(SERVICES):
        pids = procs[idx]
        if pids:
            state = f"running  pid {', '.join(map(str, pids))}  up {format_duration(process_uptime(pids[0]))}"
        else:
            state = "stopped"
        print(f"{idx + 1}. {svc.name:<20} {state}")


def start_all() -> int:
    procs = scan_processes()
    todo = [i for i in range(len(SERVICES)) if not procs[i]]
    launched = {}
    for n, idx in enumerate(todo):
        svc = SERVICES[idx]
        print(f"Starting {svc.name} (log: {svc.log})")
        launched[idx] = start_service(svc)
        if n < len(todo) - 1:
            time.sleep(START_GAP_S)
    if launched:
        time.sleep(START_GAP_S)
    failures = 0
    for idx, proc in launched.items():
        code = proc.poll()
        if code is not None:
            failures += 1
            print(f"{SERVICES[idx].name} exited with code {code}; see {SERVICES[idx].log}")
    print_status()
    return 1 if failures else 0


def stop_all() -> int:
    stopper = Stopper()
    procs = scan_processes()
    for idx in reversed(range(len(SERVICES))):
        for pid in procs[idx]:
            print(f"Stopping {SERVICES[idx].name} (pid {pid})")
            if not stopper.request(pid):
                print(f"  permission denied for pid {pid} (another user's process?)")
    while stopper.pending:
        time.sleep(0.5)
        stopper.tick(all_pids(scan_processes()))
    print_status()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Show, start and stop the backend background services.")
    parser.add_argument("command", nargs="?", default="tui", choices=("tui", "status", "start-all", "stop-all"),
                        help="default: interactive terminal UI")
    command = parser.parse_args().command
    if command == "status":
        print_status()
        return 0
    if command == "start-all":
        return start_all()
    if command == "stop-all":
        return stop_all()
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(lambda screen: Tui(screen).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
