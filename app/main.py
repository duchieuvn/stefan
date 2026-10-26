#!/usr/bin/env python3
"""
Dynamische RTI aus einer lokalen SQLite-Datenbank mit räumlicher + zeitlicher Regularisierung.

Tk viewer for the sequences pre-computed by `precompute_sequences.py`
(numerics in `rti_temporal_core`, drawing in `rti_temporal_visualization`).
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# rti_temporal_core must be imported before Matplotlib/NumPy (sets BLAS thread limits).
from rti_temporal_core import (
    RTISequenceResult,
    build_model_context,
    cache_is_current,
    cache_path,
    cached_dates,
    config_for_database,
    interval_label,
    load_cached_sequence,
    setup_logging,
)

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import precompute_sequences
from config import DEFAULT_CONFIG, RTIConfig
from database import SQLiteDatabase
from rti_temporal_visualization import clear_figure, create_temporal_figure, draw_temporal_sequence_view

LOAD_DELAY_MS = 160
DISPLAY_DELAY_MS = 40


def _clamp(index: int | float | str, n: int) -> int:
    return max(0, min(int(float(index)), n - 1))


class RTITemporalSliderApp:
    def __init__(self, root: tk.Tk, config: RTIConfig, ref_dates: list[str]) -> None:
        self.root = root
        self.config = config
        self.root.title("RTI Sequenz-Betrachter (aus vorber. Cache)")
        self.root.geometry("1680x1080")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.ref_dates = ref_dates
        self.ref_index = 0
        self.sequence_dates = list(ref_dates)
        self.seq_index = len(self.sequence_dates) - 1
        self.result: RTISequenceResult | None = None
        self.loaded: dict[str, RTISequenceResult] = {}  # in-memory cache of loaded files
        self.jobs: dict[str, str] = {}  # pending Tk `after` jobs by name
        self.closing = False

        logging.info("Building RTI model context...")
        self.context = build_model_context(config)
        self.figure = create_temporal_figure()

        self.ref_label_var = tk.StringVar()
        self.cur_label_var = tk.StringVar()
        self.info_var = tk.StringVar(value="Bereit")

        self._build_ui()
        self._update_header_labels()
        self.root.bind_all("<Key>", self._on_key)
        self._debounce("load", 0, self._load_sequence)
        self.root.focus_force()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        for text, var in (("Referenz:", self.ref_label_var), ("Gezeigte Karte:", self.cur_label_var)):
            ttk.Label(top, text=text).pack(side="left")
            ttk.Label(top, textvariable=var, font=("", 11, "bold")).pack(side="left", padx=(6, 18))
        ttk.Button(top, text="Neu laden", command=self._reload).pack(side="left")
        ttk.Button(top, text="Beenden", command=self.on_close).pack(side="right")

        self.ref_scale, _, _ = self._slider_row(
            "Referenztag", self.ref_dates, self.ref_index, self._on_ref_slider_changed
        )
        self.cur_scale, self.cur_lo_label, self.cur_hi_label = self._slider_row(
            "Karte in der Zeitfolge", self.sequence_dates, self.seq_index, self._on_cur_slider_changed
        )

        ttk.Label(self.root, textvariable=self.info_var, padding=(10, 0, 10, 8)).pack(anchor="w")

        canvas_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        canvas_frame.pack(fill="both", expand=True)
        self.canvas = FigureCanvasTkAgg(self.figure.fig, master=canvas_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def _slider_row(
        self, title: str, dates: list[str], index: int, command: Callable[[str], None]
    ) -> tuple[tk.Scale, ttk.Label, ttk.Label]:
        row = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        row.pack(fill="x")
        ttk.Label(row, text=title).pack(side="left", padx=(0, 8))
        lo_label = ttk.Label(row, text=dates[0])
        lo_label.pack(side="left")
        scale = tk.Scale(
            row,
            from_=0,
            to=max(0, len(dates) - 1),
            orient="horizontal",
            showvalue=False,
            resolution=1,
            command=command,
            length=900,
        )
        scale.set(index)
        scale.pack(side="left", fill="x", expand=True, padx=10)
        hi_label = ttk.Label(row, text=dates[-1])
        hi_label.pack(side="left")
        return scale, lo_label, hi_label

    def _update_header_labels(self) -> None:
        cfg = self.config
        self.ref_label_var.set(interval_label(self.ref_dates[self.ref_index], cfg.ref_start_hhmm, cfg.ref_end_hhmm))
        self.cur_label_var.set(interval_label(self.sequence_dates[self.seq_index], cfg.cur_start_hhmm, cfg.cur_end_hhmm))

    def _on_key(self, event: tk.Event) -> None:
        keysym = getattr(event, "keysym", "")
        if keysym in ("Left", "Right"):
            step = 1 if keysym == "Right" else -1
            self.cur_scale.set(_clamp(self.cur_scale.get() + step, len(self.sequence_dates)))
        elif keysym == "Escape" or keysym.lower() == "q":
            self.on_close()

    # Tk invokes a Scale's command at idle time, also after programmatic `set`
    # calls, so ignore callbacks that don't change the index.

    def _on_ref_slider_changed(self, value: str) -> None:
        idx = _clamp(value, len(self.ref_dates))
        if idx == self.ref_index:
            return
        self.ref_index = idx
        logging.info("Reference date changed to %s.", self.ref_dates[idx])
        self._update_header_labels()
        self._debounce("load", LOAD_DELAY_MS, self._load_sequence)

    def _on_cur_slider_changed(self, value: str) -> None:
        idx = _clamp(value, len(self.sequence_dates))
        if idx == self.seq_index:
            return
        self.seq_index = idx
        self._update_header_labels()
        self._debounce("display", DISPLAY_DELAY_MS, self._draw)

    def _reload(self) -> None:
        ref_date = self.ref_dates[self.ref_index]
        logging.info("Forcing reload of %s.", ref_date)
        self.loaded.pop(ref_date, None)
        self._debounce("load", 0, self._load_sequence)

    # ------------------------------------------------------------------
    # Scheduling / close
    # ------------------------------------------------------------------

    def _debounce(self, name: str, delay_ms: int, fn: Callable[[], None]) -> None:
        """Run `fn` after `delay_ms` (0 = when idle), replacing a pending run with the same name."""
        if self.closing:
            return
        pending = self.jobs.pop(name, None)
        if pending is not None:
            self.root.after_cancel(pending)

        def run() -> None:
            self.jobs.pop(name, None)
            if self.closing:
                return
            try:
                fn()
            except Exception as e:
                logging.exception("Error in %s job.", name)
                self.info_var.set(f"Fehler: {e}")

        self.jobs[name] = self.root.after_idle(run) if delay_ms <= 0 else self.root.after(delay_ms, run)

    def on_close(self) -> None:
        # All work runs synchronously on the Tk thread, so closing can't interrupt a load.
        if self.closing:
            return
        logging.info("Closing.")
        self.closing = True
        for job in self.jobs.values():
            self.root.after_cancel(job)
        self.jobs.clear()
        self.root.unbind_all("<Key>")
        plt.close(self.figure.fig)
        self.root.destroy()

    # ------------------------------------------------------------------
    # Data / display
    # ------------------------------------------------------------------

    def _load_sequence(self) -> None:
        ref_date = self.ref_dates[self.ref_index]
        result = self.loaded.get(ref_date)

        if result is None:
            path = cache_path(self.config, ref_date)
            self.info_var.set(f"Lade Sequenz für Referenz {ref_date} von der Festplatte...")
            self.root.update_idletasks()
            try:
                result = load_cached_sequence(self.config, ref_date)
            except Exception as e:
                logging.exception("Failed to load %s.", path)
                self.result = None
                clear_figure(self.figure)
                self.canvas.draw_idle()
                self.info_var.set(
                    f"Fehler beim Laden von {path.name}: {e}\n"
                    "Bitte die App neu starten, um den Cache neu zu erstellen."
                )
                return
            self.loaded[ref_date] = result

        self.result = result
        self.sequence_dates = list(result.sequence_dates)
        self.seq_index = _clamp(self.seq_index, len(self.sequence_dates))
        self.cur_scale.configure(to=max(0, len(self.sequence_dates) - 1))
        self.cur_scale.set(self.seq_index)
        self.cur_lo_label.configure(text=self.sequence_dates[0])
        self.cur_hi_label.configure(text=self.sequence_dates[-1])
        self._update_header_labels()
        self._debounce("display", 0, self._draw)

    def _draw(self) -> None:
        result = self.result
        if result is None:
            return
        idx = self.seq_index
        cur_date = result.sequence_dates[idx]

        self.info_var.set(
            f"Zeitfolge relativ zu Referenz {result.ref_date} | "
            f"zeige {cur_date} ({idx + 1}/{len(result.sequence_dates)}) | "
            f"{'Referenz selbst: Nullkarte | ' if cur_date == result.ref_date else ''}"
            f"gültige Links: {result.valid_counts[idx]}/{len(self.context.pairs)} | "
            f"α_time={self.config.alpha_time:.3f} | "
            f"mittlere rekonstruierte Δ-Dämpfung={float(result.mean_change[idx]):.3f} dB | "
            f"Kartenwerte ΔDämpfung [{result.raw_min:.3f}, {result.raw_max:.3f}] dB"
        )
        draw_temporal_sequence_view(self.figure, self.context, result, idx)
        self.canvas.draw_idle()


# ----------------------------------------------------------------------
# Startup
# ----------------------------------------------------------------------

def _show_error(message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Fehler", message, parent=root)
    root.destroy()


def _ask_database_path() -> Path | None:
    """Lets the user pick the local SQLite file; None if cancelled."""
    root = tk.Tk()
    root.withdraw()
    try:
        chosen = filedialog.askopenfilename(
            parent=root,
            title="SQLite-Datenbank (.db) auswählen",
            filetypes=[("SQLite-Datenbank", "*.db *.sqlite *.sqlite3"), ("Alle Dateien", "*")],
        )
    finally:
        root.destroy()
    return Path(chosen) if chosen else None


def _run_precompute_with_progress(config: RTIConfig) -> None:
    """Runs precompute_sequences.run() in a background thread behind a splash window."""
    splash = tk.Tk()
    splash.title("Bitte warten")
    splash.geometry("440x120")
    splash.resizable(False, False)
    ttk.Label(splash, text="Berechne Sequenzen aus der Datenbank ...", wraplength=400, padding=16).pack()
    progress = ttk.Progressbar(splash, mode="indeterminate")
    progress.pack(fill="x", padx=16, pady=(0, 16))
    progress.start(12)

    errors: list[Exception] = []

    def worker() -> None:
        try:
            precompute_sequences.precompute_all(config, SQLiteDatabase(config.db_path))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def poll() -> None:
        if thread.is_alive():
            splash.after(200, poll)
        else:
            progress.stop()
            splash.destroy()

    splash.after(200, poll)
    splash.mainloop()

    if errors:
        raise errors[0]


def main(config: RTIConfig = DEFAULT_CONFIG) -> None:
    setup_logging()
    logging.info("Starting RTI Temporal Slider App.")

    db_path = _ask_database_path()
    if db_path is None:
        logging.info("No database selected. Exiting.")
        return
    try:
        config = config_for_database(config, db_path)
        SQLiteDatabase(config.db_path)  # validates the file
    except Exception as error:
        logging.exception("Invalid database %s.", db_path)
        _show_error(f"Ungültige Datenbank:\n{db_path}\n\n{error}")
        return

    if not cache_is_current(config):
        logging.info("Sequence cache is missing or older than the database. Running precompute_sequences...")
        try:
            _run_precompute_with_progress(config)
        except Exception as error:
            logging.exception("Failed to precompute sequences.")
            _show_error(f"Vorberechnung fehlgeschlagen:\n{error}")
            return

    ref_dates = cached_dates(config)
    if not ref_dates:
        _show_error(
            f"Keine vorberechneten Sequenzdateien (*.joblib) gefunden in:\n{config.sequence_cache_dir}\n"
            "In der Datenbank wurden keine nutzbaren Messdaten gefunden."
        )
        return
    logging.info("Found %d pre-computed reference dates.", len(ref_dates))

    root = tk.Tk()
    RTITemporalSliderApp(root, config, ref_dates)
    root.mainloop()
    logging.info("RTI Temporal Slider App finished.")


if __name__ == "__main__":
    main()
