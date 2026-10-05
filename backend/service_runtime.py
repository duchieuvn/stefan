"""What a backend service gets from whoever runs it, and how manage_services.py runs one.

A service is a script that defines

    def run(ctx: ServiceContext, **settings) -> None

and loops until ctx says to stop. manage_services.py imports the script in a child
process of its manager daemon and calls run() with a context wired to the manager:

    ctx.stopping()      True once a stop was requested; cheap, call it often
    ctx.wait(seconds)   sleep, cut short by a stop request; returns ctx.stopping()
    ctx.idle(seconds)   sleep between jobs, cut short by a stop request or a "run now"
                        trigger; returns True if "run now" ended it
    ctx.report(**info)  publish status fields (activity, last_run, last_result,
                        next_run, ...) that manage_services.py shows

Run as a plain script, a service uses ServiceContext.standalone() instead: nothing asks
it to stop (Ctrl+C raises KeyboardInterrupt as usual) and reports go nowhere.
"""

from __future__ import annotations

import importlib
import os
import signal
import sys
import time
import traceback
from datetime import datetime
from multiprocessing.connection import Connection
from pathlib import Path


class ServiceContext:
    def __init__(self, conn: Connection | None = None) -> None:
        self._conn = conn           # None when run standalone
        self._closed = False        # the manager went away
        self._stop = False
        self._run_now = False
        self._idle = False

    @classmethod
    def standalone(cls) -> ServiceContext:
        return cls()

    def stopping(self) -> bool:
        self._receive(0)
        return self._stop

    def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return early (True) if a stop was requested."""
        self._sleep(seconds)
        return self._stop

    def idle(self, seconds: float) -> bool:
        """Sleep up to `seconds` between jobs; return True if a "run now" trigger ended it.

        "Run now" is only accepted here: one that arrives while a job is running is
        dropped, so pressing it twice cannot queue a second run.
        """
        self._idle = True
        try:
            self._sleep(seconds)
        finally:
            self._idle = False
        run_now, self._run_now = self._run_now, False
        return run_now and not self._stop

    def report(self, **info: object) -> None:
        """Publish status fields; datetimes are sent as local 'YYYY-mm-dd HH:MM:SS', other
        values that are not plain str/int/float/bool/None as str()."""
        if self._conn is None or self._closed:
            return
        for key, value in info.items():
            if isinstance(value, datetime):
                info[key] = value.strftime("%Y-%m-%d %H:%M:%S")
            elif value is not None and not isinstance(value, (str, int, float, bool)):
                info[key] = str(value)
        self._send(("info", info))

    # -- internals --

    def _sleep(self, seconds: float) -> None:
        if self._conn is None:
            time.sleep(max(0.0, seconds))
            return
        deadline = time.monotonic() + seconds
        while not self._stop and not self._run_now:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._receive(remaining)

    def _receive(self, timeout: float) -> None:
        if self._conn is None or self._closed:
            return
        try:
            ready = self._conn.poll(timeout)
            while ready:
                message = self._conn.recv()
                if message == "stop":
                    self._stop = True
                elif message == "run-now":
                    if self._idle:
                        self._run_now = True
                    else:
                        # Leading newline: the job may be mid-way through a \r progress line.
                        print("\n[manager] 'run now' ignored: a run is already in progress", flush=True)
                ready = self._conn.poll(0)
        except (EOFError, OSError):
            self._manager_gone()

    def _send(self, message: object) -> None:
        try:
            self._conn.send(message)
        except OSError:
            self._manager_gone()

    def _manager_gone(self) -> None:
        # Stop rather than keep running unmanaged: a new manager could not see this
        # process and might start a second copy on the same serial port.
        if not self._closed:
            print("[manager] lost connection to the manager; stopping", flush=True)
        self._closed = True
        self._stop = True


# --- Child process entry point (used by manage_services.py) -------------------

def _interrupt(signum, frame):
    raise KeyboardInterrupt


def child_main(script: str, log_path: str, settings: dict, conn: Connection) -> None:
    """Import `script` like `python script` would, and call its run(ctx, **settings)."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)    # Ctrl+C in the manager's terminal is for the manager
    signal.signal(signal.SIGTERM, _interrupt)       # forced stop: unwind like Ctrl+C so cleanup runs
    _redirect_output(Path(log_path))

    script_path = Path(script)
    os.chdir(script_path.parent)
    sys.path.insert(0, str(script_path.parent))
    ctx = ServiceContext(conn)
    try:
        module = importlib.import_module(script_path.stem)
        module.run(ctx, **settings)
    except KeyboardInterrupt:
        print("Stopped by the manager (SIGTERM).", flush=True)
    except SystemExit as error:
        if error.code not in (None, 0):
            _fail(ctx, error)
    except Exception as error:
        _fail(ctx, error)


def _redirect_output(log_path: Path) -> None:
    """Append stdout/stderr to the service log, line-buffered (like `python -u`)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    sys.stdout = open(1, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
    sys.stderr = open(2, "w", buffering=1, encoding="utf-8", errors="replace", closefd=False)
    stamp = datetime.now().isoformat(timespec="seconds")
    print(f"\n===== started by manage_services.py at {stamp} (pid {os.getpid()}) =====", flush=True)


def _fail(ctx: ServiceContext, error: BaseException) -> None:
    traceback.print_exc()
    summary = str(error) if isinstance(error, SystemExit) else f"{type(error).__name__}: {error}"
    if ctx._conn is not None and not ctx._closed:
        ctx._send(("error", summary))
    sys.exit(1)
