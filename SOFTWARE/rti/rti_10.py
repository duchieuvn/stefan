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

import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from rti_temporal_core import (
    DEFAULT_CONFIG,
    RTIConfig,
    RTIModelContext,
    RTISequenceResult,
    build_model_context,
    compute_temporal_sequence,
    ensure_indexes,
    get_available_dates,
    get_connection,
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
        self.root.title("Dynamische RTI – räumlich + zeitlich regularisiert")
        self.root.geometry("1680x1080")

        self.recompute_job = None
        self.display_job = None
        self.recompute_running = False
        self.pending_recompute = False
        self.close_requested = False
        self.closed = False
        self.reconfiguring_slider = False

        self.btn_recalc = None
        self.btn_exit = None

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        if not self.config.db_path.exists():
            messagebox.showerror("Fehler", "Datenbank nicht gefunden:" + chr(10) + str(self.config.db_path))
            root.destroy()
            return

        con = get_connection(self.config)
        try:
            ensure_indexes(con)
            self.available_dates = get_available_dates(con, self.config)
        finally:
            con.close()

        if not self.available_dates:
            messagebox.showerror(
                "Fehler",
                "Keine nutzbaren vollständigen Tage in measurement.pc_ts gefunden."
                + chr(10)
                + "Falls du bewusst auch angefangene Randtage sehen willst, setze"
                + chr(10)
                + "require_full_window_coverage = False.",
            )
            root.destroy()
            return

        self.context: RTIModelContext = build_model_context(self.config)
        self.interval_cache: dict[tuple[str, str], tuple[dict, dict]] = {}
        self.sequence_cache: dict[str, RTISequenceResult] = {}

        self.ref_date_index = 0
        self.sequence_dates: list[str] = list(self.available_dates)
        self.current_seq_index = len(self.sequence_dates) - 1
        self.sequence_result: RTISequenceResult | None = None

        self.ref_label_var = tk.StringVar(value="")
        self.cur_label_var = tk.StringVar(value="")
        self.info_var = tk.StringVar(value="Bereit")

        self.temporal_figure: TemporalFigure = create_temporal_figure()
        self.fig = self.temporal_figure.fig

        self.build_ui()
        self.update_header_labels()
        self.bind_keyboard_controls()
        self.schedule_recompute_sequence(immediate=True)

        try:
            self.root.focus_force()
        except Exception:
            pass

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
            text="Zeitfolge neu rechnen",
            command=lambda: self.schedule_recompute_sequence(immediate=True, force=True),
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

        self.update_header_labels()
        self.schedule_recompute_sequence()

    def on_cur_slider_changed(self, value: str) -> None:
        if self.close_requested or self.reconfiguring_slider:
            return

        idx = int(float(value))
        idx = max(0, min(idx, len(self.sequence_dates) - 1))
        self.current_seq_index = idx
        self.update_header_labels()
        self.schedule_display_update()

    def step_current_slider(self, delta: int) -> None:
        if self.close_requested:
            return
        idx = self.current_seq_index + int(delta)
        idx = max(0, min(idx, len(self.sequence_dates) - 1))
        if idx != self.current_seq_index:
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

    def schedule_recompute_sequence(
        self,
        delay_ms: int = 160,
        immediate: bool = False,
        force: bool = False,
    ) -> None:
        if self.close_requested or not self.root.winfo_exists():
            return

        if force:
            self.sequence_cache.pop(self.current_ref_date(), None)

        if self.recompute_job is not None:
            try:
                self.root.after_cancel(self.recompute_job)
            except Exception:
                pass
            self.recompute_job = None

        if immediate:
            self.recompute_job = self.root.after_idle(self.safe_recompute_sequence)
        else:
            self.recompute_job = self.root.after(delay_ms, self.safe_recompute_sequence)

    def safe_recompute_sequence(self) -> None:
        self.recompute_job = None

        if self.close_requested or not self.root.winfo_exists():
            return

        if self.recompute_running:
            self.pending_recompute = True
            return

        self.recompute_running = True
        try:
            self.recompute_sequence()
        except Exception as e:
            if not self.close_requested:
                self.info_var.set(f"Fehler: {e}")
        finally:
            self.recompute_running = False

        if self.close_requested:
            self._finalize_close()
        elif self.pending_recompute:
            self.pending_recompute = False
            self.schedule_recompute_sequence(immediate=True)

    def schedule_display_update(self, delay_ms: int = 40, immediate: bool = False) -> None:
        if self.close_requested or not self.root.winfo_exists():
            return

        if self.display_job is not None:
            try:
                self.root.after_cancel(self.display_job)
            except Exception:
                pass
            self.display_job = None

        if immediate:
            self.display_job = self.root.after_idle(self.safe_display_update)
        else:
            self.display_job = self.root.after(delay_ms, self.safe_display_update)

    def safe_display_update(self) -> None:
        self.display_job = None

        if self.close_requested or not self.root.winfo_exists():
            return

        try:
            self.update_display_view()
        except Exception as e:
            if not self.close_requested:
                self.info_var.set(f"Fehler: {e}")

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def on_close(self) -> None:
        if self.closed or self.close_requested:
            return

        self.close_requested = True
        self.disable_ui()

        try:
            self.root.unbind_all("<Key>")
        except Exception:
            pass

        for job_name in ("recompute_job", "display_job"):
            job = getattr(self, job_name)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, job_name, None)

        if self.recompute_running:
            self.info_var.set("Schließe nach laufender Berechnung ...")
            return

        self._finalize_close()

    def _finalize_close(self) -> None:
        if self.closed:
            return

        self.closed = True

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

    # ------------------------------------------------------------------
    # Data / solve
    # ------------------------------------------------------------------

    def recompute_sequence(self) -> None:
        if self.close_requested:
            return

        ref_date = self.current_ref_date()
        cached = self.sequence_cache.get(ref_date)
        if cached is not None:
            self.apply_sequence_solution(cached)
            self.schedule_display_update(immediate=True)
            return

        self.info_var.set(
            f"Rechne gemeinsame Zeitfolge relativ zu Referenz {ref_date} "
            f"({len(self.sequence_dates)} Karten) ..."
        )
        try:
            self.root.update_idletasks()
        except Exception:
            pass

        result = compute_temporal_sequence(
            context=self.context,
            ref_date=ref_date,
            sequence_dates=self.sequence_dates,
            interval_cache=self.interval_cache,
            config=self.config,
        )
        self.sequence_cache[ref_date] = result
        self.apply_sequence_solution(result)
        self.schedule_display_update(immediate=True)

    def apply_sequence_solution(self, result: RTISequenceResult) -> None:
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


def main() -> None:
    root = tk.Tk()
    app = RTITemporalSliderApp(root)
    if root.winfo_exists():
        root.mainloop()


if __name__ == "__main__":
    main()
