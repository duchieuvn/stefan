#!/usr/bin/env python3
"""
Dynamische RTI aus sensor_data.db mit räumlicher + zeitlicher Regularisierung.

Tk viewer for the sequences pre-computed by `precompute_sequences.py`
(numerics in `rti_temporal_core`, drawing in `rti_temporal_visualization`).
The "Daten" menu selects the data source and history period and re-runs
the pre-computation in the background.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import replace
from tkinter import messagebox, ttk

# rti_temporal_core must be imported before Matplotlib/NumPy (sets BLAS thread limits).
from rti_temporal_core import (
    RTISequenceResult,
    build_model_context,
    cache_is_current,
    cache_path,
    cached_dates,
    interval_label,
    load_cache_info,
    load_cached_sequence,
    setup_logging,
)

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import precompute_sequences
from config import DEFAULT_CONFIG, RTIConfig
from database import BACKENDS, SettingsError, default_backend
from rti_temporal_visualization import clear_figure, create_temporal_figure, draw_temporal_sequence_view

LOAD_DELAY_MS = 160
DISPLAY_DELAY_MS = 40
PRECOMPUTE_POLL_MS = 200

# "Daten" menu choices: label -> backend / history_months (None = all data).
SOURCE_LABELS = {
    "PostgreSQL (Tailscale)": "pg-tailscale",
    "PostgreSQL (SSH-Tunnel)": "postgres",
    "SQLite (Download)": "sqlite",
}
assert set(SOURCE_LABELS.values()) == set(BACKENDS)
PERIOD_MONTHS = (3, 6, None)


def _source_label(backend: str) -> str:
    return next(label for label, value in SOURCE_LABELS.items() if value == backend)


def _period_label(months: int | None) -> str:
    return "Alle Daten" if months is None else f"{months} Monate"


def _clamp(index: int | float | str, n: int) -> int:
    return max(0, min(int(float(index)), n - 1))


class RTITemporalSliderApp:
    def __init__(self, root: tk.Tk, config: RTIConfig, backend: str) -> None:
        self.root = root
        self.config = config
        self.backend = backend
        self.root.title("RTI Sequenz-Betrachter (aus vorber. Cache)")
        self.root.geometry("1680x1080")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.ref_dates: list[str] = []
        self.ref_index = 0
        self.sequence_dates: list[str] = []
        self.seq_index = 0
        self.result: RTISequenceResult | None = None
        self.loaded: dict[str, RTISequenceResult] = {}  # in-memory cache of loaded files
        self.jobs: dict[str, str] = {}  # pending Tk `after` jobs by name
        self.closing = False
        self.precompute_thread: threading.Thread | None = None
        self.precompute_errors: list[Exception] = []

        logging.info("Building RTI model context...")
        self.context = build_model_context(config)
        self.figure = create_temporal_figure()

        self.ref_label_var = tk.StringVar()
        self.cur_label_var = tk.StringVar()
        self.info_var = tk.StringVar(value="Bereit")
        self.source_var = tk.StringVar(value=backend)
        self.period_var = tk.StringVar(value=_period_label(config.history_months))
        self.cache_info_var = tk.StringVar()

        self._build_ui()
        self.root.bind_all("<Key>", self._on_key)
        self._set_ref_dates(cached_dates(config))
        self.root.focus_force()

        if not cache_is_current(config, backend):
            logging.info("Sequence cache is missing, not from today or built with other settings.")
            self._start_precompute()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_menu()

        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        for text, var in (("Referenz:", self.ref_label_var), ("Gezeigte Karte:", self.cur_label_var)):
            ttk.Label(top, text=text).pack(side="left")
            ttk.Label(top, textvariable=var, font=("", 11, "bold")).pack(side="left", padx=(6, 18))
        ttk.Button(top, text="Neu laden", command=self._reload).pack(side="left")
        ttk.Button(top, text="Beenden", command=self.on_close).pack(side="right")
        ttk.Label(top, textvariable=self.cache_info_var).pack(side="right", padx=(8, 18))
        self.progress = ttk.Progressbar(top, mode="indeterminate", length=160)
        self.progress.pack(side="right")

        self.ref_scale, self.ref_lo_label, self.ref_hi_label = self._slider_row(
            "Referenztag", self._on_ref_slider_changed
        )
        self.cur_scale, self.cur_lo_label, self.cur_hi_label = self._slider_row(
            "Karte in der Zeitfolge", self._on_cur_slider_changed
        )

        ttk.Label(self.root, textvariable=self.info_var, padding=(10, 0, 10, 8)).pack(anchor="w")

        canvas_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        canvas_frame.pack(fill="both", expand=True)
        self.canvas = FigureCanvasTkAgg(self.figure.fig, master=canvas_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def _build_menu(self) -> None:
        """Menu bar with "Daten" → Datenquelle / Zeitraum / Neu berechnen."""
        menubar = tk.Menu(self.root)
        self.data_menu = tk.Menu(menubar, tearoff=False)

        source_menu = tk.Menu(self.data_menu, tearoff=False)
        for label, backend in SOURCE_LABELS.items():
            source_menu.add_radiobutton(
                label=label, value=backend, variable=self.source_var, command=self._on_data_settings_changed
            )

        period_menu = tk.Menu(self.data_menu, tearoff=False)
        periods = list(PERIOD_MONTHS)
        if self.config.history_months not in periods:  # a custom history_months from config.py
            periods.insert(0, self.config.history_months)
        for months in periods:
            period_menu.add_radiobutton(
                label=_period_label(months), value=_period_label(months), variable=self.period_var,
                command=self._on_data_settings_changed,
            )

        self.data_menu.add_cascade(label="Datenquelle", menu=source_menu)
        self.data_menu.add_cascade(label="Zeitraum", menu=period_menu)
        self.data_menu.add_separator()
        self.data_menu.add_command(label="Neu berechnen", command=self._start_precompute)
        menubar.add_cascade(label="Daten", menu=self.data_menu)
        self.root.configure(menu=menubar)

    def _selected_settings(self) -> tuple[str, int | None]:
        months = next(
            m for m in (*PERIOD_MONTHS, self.config.history_months) if _period_label(m) == self.period_var.get()
        )
        return self.source_var.get(), months

    def _on_data_settings_changed(self) -> None:
        if self._selected_settings() != (self.backend, self.config.history_months):
            self._start_precompute()

    def _set_data_menu_state(self, state: str) -> None:
        for label in ("Datenquelle", "Zeitraum", "Neu berechnen"):
            self.data_menu.entryconfigure(label, state=state)

    def _slider_row(self, title: str, command: Callable[[str], None]) -> tuple[tk.Scale, ttk.Label, ttk.Label]:
        row = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        row.pack(fill="x")
        ttk.Label(row, text=title).pack(side="left", padx=(0, 8))
        lo_label = ttk.Label(row, text="–")
        lo_label.pack(side="left")
        scale = tk.Scale(
            row,
            from_=0,
            to=0,
            orient="horizontal",
            showvalue=False,
            resolution=1,
            command=command,
            length=900,
        )
        scale.pack(side="left", fill="x", expand=True, padx=10)
        hi_label = ttk.Label(row, text="–")
        hi_label.pack(side="left")
        return scale, lo_label, hi_label

    @staticmethod
    def _configure_slider(scale: tk.Scale, lo_label: ttk.Label, hi_label: ttk.Label, dates: list[str], index: int) -> None:
        scale.configure(to=max(0, len(dates) - 1))
        scale.set(index)
        lo_label.configure(text=dates[0] if dates else "–")
        hi_label.configure(text=dates[-1] if dates else "–")

    def _update_header_labels(self) -> None:
        cfg = self.config
        if not self.ref_dates:
            self.ref_label_var.set("–")
            self.cur_label_var.set("–")
            return
        self.ref_label_var.set(interval_label(self.ref_dates[self.ref_index], cfg.ref_start_hhmm, cfg.ref_end_hhmm))
        self.cur_label_var.set(interval_label(self.sequence_dates[self.seq_index], cfg.cur_start_hhmm, cfg.cur_end_hhmm))

    def _update_cache_info(self) -> None:
        info = load_cache_info(self.config)
        if not self.ref_dates or info is None:
            self.cache_info_var.set("Kein Cache vorhanden")
            return
        source = _source_label(info["backend"]) if info.get("backend") in BACKENDS else info.get("backend")
        self.cache_info_var.set(
            f"Cache: {source}, {_period_label(info.get('history_months'))}, "
            f"{len(self.ref_dates)} Tage bis {self.ref_dates[-1]}"
        )

    def _set_ref_dates(self, ref_dates: list[str]) -> None:
        """Shows a new set of cached reference dates, keeping the current one if it still exists."""
        current = self.ref_dates[self.ref_index] if self.ref_dates else None
        self.ref_dates = ref_dates
        self.ref_index = ref_dates.index(current) if current in ref_dates else 0
        self.sequence_dates = list(ref_dates)
        self.seq_index = max(0, len(ref_dates) - 1)
        self.loaded.clear()
        self.result = None

        self._configure_slider(self.ref_scale, self.ref_lo_label, self.ref_hi_label, ref_dates, self.ref_index)
        self._configure_slider(self.cur_scale, self.cur_lo_label, self.cur_hi_label, ref_dates, self.seq_index)
        self._update_header_labels()
        self._update_cache_info()

        if ref_dates:
            self._debounce("load", 0, self._load_sequence)
        else:
            clear_figure(self.figure)
            self.canvas.draw_idle()
            self.info_var.set("Keine vorberechneten Sequenzen. Bitte Datenquelle und Zeitraum wählen und 'Neu berechnen' drücken.")

    def _on_key(self, event: tk.Event) -> None:
        keysym = getattr(event, "keysym", "")
        if keysym in ("Left", "Right") and self.sequence_dates:
            step = 1 if keysym == "Right" else -1
            self.cur_scale.set(_clamp(self.cur_scale.get() + step, len(self.sequence_dates)))
        elif keysym == "Escape" or keysym.lower() == "q":
            self.on_close()

    # Tk invokes a Scale's command at idle time, also after programmatic `set`
    # calls, so ignore callbacks that don't change the index.

    def _on_ref_slider_changed(self, value: str) -> None:
        if not self.ref_dates:
            return
        idx = _clamp(value, len(self.ref_dates))
        if idx == self.ref_index:
            return
        self.ref_index = idx
        logging.info("Reference date changed to %s.", self.ref_dates[idx])
        self._update_header_labels()
        self._debounce("load", LOAD_DELAY_MS, self._load_sequence)

    def _on_cur_slider_changed(self, value: str) -> None:
        if not self.sequence_dates:
            return
        idx = _clamp(value, len(self.sequence_dates))
        if idx == self.seq_index:
            return
        self.seq_index = idx
        self._update_header_labels()
        self._debounce("display", DISPLAY_DELAY_MS, self._draw)

    def _reload(self) -> None:
        if not self.ref_dates:
            return
        ref_date = self.ref_dates[self.ref_index]
        logging.info("Forcing reload of %s.", ref_date)
        self.loaded.pop(ref_date, None)
        self._debounce("load", 0, self._load_sequence)

    # ------------------------------------------------------------------
    # Pre-computation ("Daten" menu)
    # ------------------------------------------------------------------

    def _start_precompute(self) -> None:
        """Re-runs precompute_sequences for the selected source and period in a background thread."""
        if self.precompute_thread is not None:
            return
        backend, months = self._selected_settings()
        config = replace(self.config, history_months=months)
        logging.info("Pre-computing sequences (backend=%s, history_months=%s).", backend, months)

        self._set_data_menu_state("disabled")
        self.progress.start(12)
        self.info_var.set(f"Berechne Sequenzen aus {_source_label(backend)} ({self.period_var.get()}) ...")
        self.precompute_errors = []

        def worker() -> None:
            try:
                precompute_sequences.run(config, backend=backend)
            except Exception as error:
                logging.exception("Failed to precompute sequences.")
                self.precompute_errors.append(error)

        self.precompute_thread = threading.Thread(target=worker, daemon=True)
        self.precompute_thread.start()
        self.config, self.backend = config, backend
        self.root.after(PRECOMPUTE_POLL_MS, self._poll_precompute)

    def _poll_precompute(self) -> None:
        if self.closing:
            return
        if self.precompute_thread is not None and self.precompute_thread.is_alive():
            self.root.after(PRECOMPUTE_POLL_MS, self._poll_precompute)
            return

        self.precompute_thread = None
        self.progress.stop()
        self._set_data_menu_state("normal")

        self._set_ref_dates(cached_dates(self.config))
        if self.precompute_errors:
            error = self.precompute_errors[0]
            self.info_var.set(f"Vorberechnung fehlgeschlagen: {error}")
            messagebox.showerror("Fehler", f"Vorberechnung fehlgeschlagen:\n{error}", parent=self.root)

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
        # Loading and drawing run on the Tk thread; a running pre-computation is a
        # daemon thread and is simply abandoned.
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
                    "Bitte 'precompute_sequences.py' ausführen, um den Cache zu erstellen."
                )
                return
            self.loaded[ref_date] = result

        self.result = result
        self.sequence_dates = list(result.sequence_dates)
        self.seq_index = _clamp(self.seq_index, len(self.sequence_dates))
        self._configure_slider(
            self.cur_scale, self.cur_lo_label, self.cur_hi_label, self.sequence_dates, self.seq_index
        )
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

def _initial_settings(config: RTIConfig) -> tuple[RTIConfig, str]:
    """Source and period the cache was last built with; otherwise RTI_DB_BACKEND and config.py."""
    info = load_cache_info(config)
    if info and info.get("backend") in BACKENDS:
        return replace(config, history_months=info.get("history_months")), info["backend"]
    try:
        return config, default_backend()
    except SettingsError:
        logging.exception("Invalid RTI_DB_BACKEND, using sqlite.")
        return config, "sqlite"


def main(config: RTIConfig = DEFAULT_CONFIG) -> None:
    setup_logging()
    logging.info("Starting RTI Temporal Slider App.")
    config, backend = _initial_settings(config)

    root = tk.Tk()
    RTITemporalSliderApp(root, config, backend)
    root.mainloop()
    logging.info("RTI Temporal Slider App finished.")


if __name__ == "__main__":
    main()
