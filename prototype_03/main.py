#!/usr/bin/env python3
"""
Dynamische RTI aus sensor_data.db mit räumlicher + zeitlicher Regularisierung.

This file is now the Tk interaction shell. Numeric/data work lives in
`rti_temporal_core.py`; Matplotlib drawing lives in
`rti_temporal_visualization.py`.
"""

from __future__ import annotations

# Must happen before NumPy/OpenBLAS import through the core module.
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import logging
import threading
import tkinter as tk
from datetime import date
from tkinter import ttk, messagebox

import joblib
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import precompute_sequences
from config import DEFAULT_CONFIG, RTIConfig
from rti_temporal_core import (
    RTIModelContext,
    RTISequenceResult,
    build_model_context,
    interval_label,
)
from rti_temporal_visualization import (
    TemporalFigure,
    clear_colorbars,
    create_temporal_figure,
    draw_temporal_sequence_view,
)


CONFIG = DEFAULT_CONFIG


class RTITemporalSliderApp:
    def __init__(self, root: tk.Tk, config: RTIConfig = CONFIG) -> None:
        self.root = root
        self.config = config
        self.root.title("RTI Sequenz-Betrachter (aus vorber. Cache)")
        self.root.geometry("1680x1080")

        self.load_job = None
        self.display_job = None
        self.load_running = False
        self.pending_load = False
        self.close_requested = False
        self.closed = False
        self.reconfiguring_slider = False

        self.btn_recalc = None
        self.btn_exit = None

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Ensure the on-disk cache directory exists and find available dates from it.
        self.config.sequence_cache_dir.mkdir(exist_ok=True)
        logging.info("Scanning for pre-computed sequence files in: %s", self.config.sequence_cache_dir)

        try:
            # Find files like 'YYYY-MM-DD.joblib' and extract the date string.
            date_files = sorted(self.config.sequence_cache_dir.glob("????-??-??.joblib"))
            self.available_dates = [p.stem for p in date_files]
        except Exception as e:
            logging.exception("Error scanning cache directory.")
            messagebox.showerror("Fehler", f"Fehler beim Durchsuchen des Cache-Verzeichnisses:\n{e}")
            root.destroy()
            return

        if not self.available_dates:
            logging.error("No pre-computed sequence files found in %s", self.config.sequence_cache_dir)
            messagebox.showerror(
                "Fehler",
                "Keine vorberechneten Sequenzdateien (*.joblib) gefunden in:"
                + chr(10)
                + str(self.config.sequence_cache_dir)
                + chr(10)
                + "Bitte zuerst 'precompute_sequences.py' ausführen.",
            )
            root.destroy()
            return

        logging.info("Found %d pre-computed reference dates.", len(self.available_dates))
        logging.info("Building RTI model context...")
        self.context: RTIModelContext = build_model_context(self.config)
        # This is now an in-memory cache for disk files
        self.sequence_cache: dict[str, RTISequenceResult] = {}

        self.ref_date_index = 0
        self.sequence_dates: list[str] = list(self.available_dates)
        self.current_seq_index = len(self.sequence_dates) - 1
        self.sequence_result: RTISequenceResult | None = None

        self.ref_label_var = tk.StringVar(value="")
        self.cur_label_var = tk.StringVar(value="")
        self.info_var = tk.StringVar(value="Bereit")

        logging.info("Creating Matplotlib figure...")
        self.temporal_figure: TemporalFigure = create_temporal_figure()
        self.fig = self.temporal_figure.fig

        logging.info("Building UI...")
        self.build_ui()
        self.update_header_labels()
        self.bind_keyboard_controls()
        self.schedule_load_sequence(immediate=True)

        try:
            self.root.focus_force()
        except Exception:
            pass
        logging.info("Initialization complete.")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        ttk.Label(top, text="Referenz:").pack(side="left")
        ttk.Label(top, textvariable=self.ref_label_var, font=("", 11, "bold")).pack(side="left", padx=(6, 18))

        ttk.Label(top, text="Gezeigte Karte:").pack(side="left")
        ttk.Label(top, textvariable=self.cur_label_var, font=("", 11, "bold")).pack(side="left", padx=(6, 18))

        self.btn_recalc = ttk.Button(
            top,
            text="Neu laden",
            command=lambda: self.schedule_load_sequence(immediate=True, force=True),
        )
        self.btn_recalc.pack(side="left")

        self.btn_exit = ttk.Button(top, text="Beenden", command=self.on_close)
        self.btn_exit.pack(side="right")

        slider_row_ref = ttk.Frame(self.root, padding=(10, 0, 10, 6))
        slider_row_ref.pack(fill="x")

        ttk.Label(slider_row_ref, text="Referenztag").pack(side="left", padx=(0, 8))
        ttk.Label(slider_row_ref, text=self.available_dates[0]).pack(side="left")

        self.ref_scale = tk.Scale(
            slider_row_ref,
            from_=0,
            to=max(0, len(self.available_dates) - 1),
            orient="horizontal",
            showvalue=False,
            resolution=1,
            command=self.on_ref_slider_changed,
            length=900,
        )
        self.ref_scale.set(self.ref_date_index)
        self.ref_scale.pack(side="left", fill="x", expand=True, padx=10)

        ttk.Label(slider_row_ref, text=self.available_dates[-1]).pack(side="left")

        slider_row_cur = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        slider_row_cur.pack(fill="x")

        ttk.Label(slider_row_cur, text="Karte in der Zeitfolge").pack(side="left", padx=(0, 14))
        self.cur_left_label = ttk.Label(slider_row_cur, text=self.sequence_dates[0])
        self.cur_left_label.pack(side="left")

        self.cur_scale = tk.Scale(
            slider_row_cur,
            from_=0,
            to=max(0, len(self.sequence_dates) - 1),
            orient="horizontal",
            showvalue=False,
            resolution=1,
            command=self.on_cur_slider_changed,
            length=900,
        )
        self.cur_scale.set(self.current_seq_index)
        self.cur_scale.pack(side="left", fill="x", expand=True, padx=10)

        self.cur_right_label = ttk.Label(slider_row_cur, text=self.sequence_dates[-1])
        self.cur_right_label.pack(side="left")

        ttk.Label(self.root, textvariable=self.info_var, padding=(10, 0, 10, 8)).pack(anchor="w")

        canvas_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        canvas_frame.pack(fill="both", expand=True)

        self.canvas = FigureCanvasTkAgg(self.fig, master=canvas_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def bind_keyboard_controls(self) -> None:
        self.root.bind_all("<Key>", self.on_global_key)

    def on_global_key(self, event: tk.Event) -> None:
        keysym = getattr(event, "keysym", "")

        if keysym == "Left":
            self.step_current_slider(-1)
        elif keysym == "Right":
            self.step_current_slider(1)
        elif keysym == "Escape":
            self.on_close()
        elif keysym.lower() == "q":
            self.on_close()

    def current_ref_date(self) -> str:
        return self.available_dates[self.ref_date_index]

    def current_cur_date(self) -> str:
        return self.sequence_dates[self.current_seq_index]

    def update_header_labels(self) -> None:
        self.ref_label_var.set(
            interval_label(self.current_ref_date(), self.config.ref_start_hhmm, self.config.ref_end_hhmm)
        )
        self.cur_label_var.set(
            interval_label(self.current_cur_date(), self.config.cur_start_hhmm, self.config.cur_end_hhmm)
        )

    def refresh_current_slider_range(self) -> None:
        self.reconfiguring_slider = True
        try:
            self.cur_scale.configure(to=max(0, len(self.sequence_dates) - 1))
            self.cur_scale.set(self.current_seq_index)
            self.cur_left_label.configure(text=self.sequence_dates[0])
            self.cur_right_label.configure(text=self.sequence_dates[-1])
        finally:
            self.reconfiguring_slider = False

    def on_ref_slider_changed(self, value: str) -> None:
        if self.close_requested or self.reconfiguring_slider:
            return

        idx = int(float(value))
        idx = max(0, min(idx, len(self.available_dates) - 1))
        self.ref_date_index = idx
        logging.info("Reference date slider changed to index %d (%s).", idx, self.current_ref_date())

        self.update_header_labels()
        self.schedule_load_sequence()

    def on_cur_slider_changed(self, value: str) -> None:
        if self.close_requested or self.reconfiguring_slider:
            return

        idx = int(float(value))
        idx = max(0, min(idx, len(self.sequence_dates) - 1))
        self.current_seq_index = idx
        logging.info("Current sequence slider changed to index %d (%s).", idx, self.current_cur_date())
        self.update_header_labels()
        self.schedule_display_update()

    def step_current_slider(self, delta: int) -> None:
        if self.close_requested:
            return
        idx = self.current_seq_index + int(delta)
        idx = max(0, min(idx, len(self.sequence_dates) - 1))
        if idx != self.current_seq_index:
            logging.debug("Stepping current slider by %d to index %d.", delta, idx)
            self.cur_scale.set(idx)

    def disable_ui(self) -> None:
        for widget in (self.ref_scale, self.cur_scale, self.btn_recalc, self.btn_exit):
            if widget is None:
                continue
            try:
                widget.configure(state="disabled")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def schedule_load_sequence(
        self,
        delay_ms: int = 160,
        immediate: bool = False,
        force: bool = False,
    ) -> None:
        if self.close_requested or not self.root.winfo_exists():
            return

        if force:
            logging.info("Forcing reload. Clearing in-memory cache for ref_date %s.", self.current_ref_date())
            self.sequence_cache.pop(self.current_ref_date(), None)

        if self.load_job is not None:
            try:
                self.root.after_cancel(self.load_job)
            except Exception:
                pass
            self.load_job = None

        log_msg = f"Scheduling sequence load (immediate={immediate}, force={force})."
        if immediate:
            self.load_job = self.root.after_idle(self.safe_load_sequence)
            logging.debug("%s (idle)", log_msg)
        else:
            self.load_job = self.root.after(delay_ms, self.safe_load_sequence)

    def safe_load_sequence(self) -> None:
        self.load_job = None

        if self.close_requested or not self.root.winfo_exists():
            return

        if self.load_running:
            logging.debug("Load is already running. Pending load set to True.")
            self.pending_load = True
            return

        self.load_running = True
        logging.info("Starting sequence load from disk.")
        try:
            self.load_sequence()
        except Exception as e:
            logging.exception("Error during sequence load.")
            if not self.close_requested:
                self.info_var.set(f"Fehler: {e}")
        finally:
            self.load_running = False
            logging.info("Finished sequence load.")

        if self.close_requested:
            self._finalize_close()
        elif self.pending_load:
            self.pending_load = False
            logging.info("Executing pending load.")
            self.schedule_load_sequence(immediate=True)

    def schedule_display_update(self, delay_ms: int = 40, immediate: bool = False) -> None:
        if self.close_requested or not self.root.winfo_exists():
            return

        if self.display_job is not None:
            try:
                self.root.after_cancel(self.display_job)
            except Exception:
                pass
            self.display_job = None

        log_msg = f"Scheduling display update (immediate={immediate})."
        if immediate:
            self.display_job = self.root.after_idle(self.safe_display_update)
            logging.debug("%s (idle)", log_msg)
        else:
            self.display_job = self.root.after(delay_ms, self.safe_display_update)

    def safe_display_update(self) -> None:
        self.display_job = None

        if self.close_requested or not self.root.winfo_exists():
            return

        try:
            logging.debug("Updating display view.")
            self.update_display_view()
        except Exception as e:
            logging.exception("Error during display update.")
            if not self.close_requested:
                self.info_var.set(f"Fehler: {e}")

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def on_close(self) -> None:
        if self.closed or self.close_requested:
            return

        logging.info("Close requested.")
        self.close_requested = True
        self.disable_ui()

        try:
            self.root.unbind_all("<Key>")
        except Exception:
            pass

        for job_name in ("load_job", "display_job"):
            job = getattr(self, job_name)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, job_name, None)

        if self.load_running:
            self.info_var.set("Schließe nach Ladevorgang ...")
            logging.info("Load is running. Deferring close until it finishes.")
            return

        self._finalize_close()

    def _finalize_close(self) -> None:
        if self.closed:
            return

        self.closed = True
        logging.info("Finalizing close.")

        try:
            self.root.withdraw()
        except Exception:
            pass

        try:
            clear_colorbars(self.temporal_figure)
        except Exception:
            pass

        try:
            self.fig.clf()
        except Exception:
            pass

        try:
            plt.close(self.fig)
        except Exception:
            pass

        try:
            widget = self.canvas.get_tk_widget()
            if widget.winfo_exists():
                widget.destroy()
        except Exception:
            pass

        try:
            self.root.quit()
        except Exception:
            pass

        try:
            self.root.destroy()
        except Exception:
            pass
        logging.info("Application closed.")

    # ------------------------------------------------------------------
    # Data / solve
    # ------------------------------------------------------------------

    def load_sequence(self) -> None:
        if self.close_requested:
            return

        ref_date = self.current_ref_date()

        # 1. Check in-memory cache first to avoid re-reading from disk
        cached = self.sequence_cache.get(ref_date)
        if cached is not None:
            logging.info("Using in-memory cached sequence for ref_date %s.", ref_date)
            self.apply_sequence_solution(cached)
            self.schedule_display_update(immediate=True)
            return

        # 2. If not in memory, try to load from the on-disk cache file
        cache_file = self.config.sequence_cache_dir / f"{ref_date}.joblib"
        logging.info("Attempting to load sequence from disk: %s", cache_file)

        if not cache_file.exists():
            logging.error("Pre-computed sequence file not found: %s", cache_file)
            self.info_var.set(
                f"FEHLER: Vorberechnete Datei nicht gefunden: {cache_file.name}"
                + chr(10)
                + "Bitte 'precompute_sequences.py' ausführen, um den Cache zu erstellen."
            )
            self.sequence_result = None
            clear_colorbars(self.temporal_figure)
            for ax in (
                self.temporal_figure.ax_raster,
                self.temporal_figure.ax_smooth,
                self.temporal_figure.ax_total,
                self.temporal_figure.ax_links,
            ):
                ax.clear()
            if not self.close_requested:
                self.canvas.draw_idle()
            return

        self.info_var.set(f"Lade Sequenz für Referenz {ref_date} von der Festplatte...")
        try:
            self.root.update_idletasks()
        except Exception:
            pass

        try:
            result = joblib.load(cache_file)
            logging.info("Successfully loaded sequence from %s.", cache_file)
            self.sequence_cache[ref_date] = result
            self.apply_sequence_solution(result)
            self.schedule_display_update(immediate=True)
        except Exception as e:
            logging.exception("Failed to load or process sequence file %s.", cache_file)
            self.info_var.set(f"Fehler beim Laden der Cache-Datei: {e}")

    def apply_sequence_solution(self, result: RTISequenceResult) -> None:
        logging.debug("Applying new sequence solution.")
        self.sequence_result = result
        self.sequence_dates = list(result.sequence_dates)
        self.current_seq_index = max(0, min(self.current_seq_index, len(self.sequence_dates) - 1))
        self.refresh_current_slider_range()
        self.update_header_labels()

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def update_display_view(self) -> None:
        if self.close_requested or self.sequence_result is None:
            return

        result = self.sequence_result
        idx = self.current_seq_index
        ref_date = self.current_ref_date()
        cur_date = result.sequence_dates[idx]
        mean_now = float(result.mean_change[idx])

        self.info_var.set(
            f"Zeitfolge relativ zu Referenz {ref_date} | "
            f"zeige {cur_date} ({idx + 1}/{len(result.sequence_dates)}) | "
            f"{'Referenz selbst: Nullkarte | ' if cur_date == ref_date else ''}"
            f"gültige Links: {result.valid_counts[idx]}/{len(self.context.pairs)} | "
            f"α_time={self.config.alpha_time:.3f} | "
            f"mittlere rekonstruierte Δ-Dämpfung={mean_now:.3f} dB | "
            f"Kartenwerte ΔDämpfung [{result.raw_min:.3f}, {result.raw_max:.3f}] dB"
        )
        logging.debug("Drawing view for sequence index %d.", idx)

        draw_temporal_sequence_view(
            figure=self.temporal_figure,
            context=self.context,
            result=result,
            sequence_index=idx,
            ref_date=ref_date,
            config=self.config,
        )

        if not self.close_requested:
            self.canvas.draw_idle()


def _sequence_cache_is_current(config: RTIConfig) -> bool:
    """True if the newest pre-computed sequence file is for today's date."""
    date_files = sorted(config.sequence_cache_dir.glob("????-??-??.joblib"))
    if not date_files:
        return False
    return date_files[-1].stem == date.today().isoformat()


def _run_precompute_with_progress(config: RTIConfig = CONFIG) -> None:
    """Runs precompute_sequences.run() in a background thread behind a splash window."""
    splash = tk.Tk()
    splash.title("Bitte warten")
    splash.geometry("440x120")
    splash.resizable(False, False)

    status_var = tk.StringVar(value="Aktualisiere Datenbank und berechne Sequenzen ...")
    ttk.Label(splash, textvariable=status_var, wraplength=400, padding=16).pack()
    progress = ttk.Progressbar(splash, mode="indeterminate")
    progress.pack(fill="x", padx=16, pady=(0, 16))
    progress.start(12)

    outcome: dict[str, Exception | None] = {"error": None}

    def worker() -> None:
        try:
            precompute_sequences.run(config=config)
        except Exception as error:
            outcome["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def poll() -> None:
        if thread.is_alive():
            splash.after(200, poll)
        else:
            splash.destroy()

    splash.after(200, poll)
    splash.mainloop()

    if outcome["error"] is not None:
        raise outcome["error"]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.info("Starting RTI Temporal Slider App.")

    if not _sequence_cache_is_current(CONFIG):
        logging.info("Pre-computed sequence cache is missing or not from today. Running precompute_sequences...")
        try:
            _run_precompute_with_progress(CONFIG)
        except Exception as error:
            logging.exception("Failed to precompute sequences.")
            messagebox.showerror("Fehler", f"Vorberechnung fehlgeschlagen:\n{error}")
            return

    root = tk.Tk()
    app = RTITemporalSliderApp(root)
    if root.winfo_exists():
        root.mainloop()
    logging.info("RTI Temporal Slider App finished.")


if __name__ == "__main__":
    main()
