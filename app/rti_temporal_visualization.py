"""
Matplotlib drawing for the temporal RTI viewer.

Tk-free: draws into a figure supplied by the caller, while the Tk app owns
the canvas embedding.
"""

from __future__ import annotations

from dataclasses import dataclass

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle

from config import RTIConfig
from rti_temporal_core import RTIModelContext, RTISequenceResult

CBAR_LABEL = "Δ Dämpfung [dB]\nrot < 0 = weniger, blau > 0 = mehr"
DELTA_YLABEL = "negativ = weniger / trockener, positiv = mehr / feuchter"


@dataclass
class TemporalFigure:
    fig: plt.Figure
    ax_raster: plt.Axes
    ax_smooth: plt.Axes
    ax_total: plt.Axes
    ax_links: plt.Axes
    cbar_raster: object | None = None
    cbar_smooth: object | None = None

    @property
    def axes(self) -> tuple[plt.Axes, ...]:
        return (self.ax_raster, self.ax_smooth, self.ax_total, self.ax_links)


def create_temporal_figure() -> TemporalFigure:
    fig = plt.figure(figsize=(15.5, 10.2))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.55, 0.72], width_ratios=[1.08, 0.92], hspace=0.30, wspace=0.22)
    return TemporalFigure(
        fig=fig,
        ax_raster=fig.add_subplot(gs[0, 0]),
        ax_smooth=fig.add_subplot(gs[0, 1]),
        ax_total=fig.add_subplot(gs[1, 0]),
        ax_links=fig.add_subplot(gs[1, 1]),
    )


def clear_figure(figure: TemporalFigure) -> None:
    for attr in ("cbar_raster", "cbar_smooth"):
        cbar = getattr(figure, attr)
        if cbar is not None:
            cbar.remove()
            setattr(figure, attr, None)
    for ax in figure.axes:
        ax.clear()


def draw_container_and_nodes(ax: plt.Axes, config: RTIConfig) -> None:
    ax.add_patch(Rectangle((0, 0), config.container_w_m, config.container_h_m, fill=False, linewidth=2.0, edgecolor="black", zorder=10))

    for node_id, (x, y) in config.node_pos_m.items():
        ax.scatter(x, y, s=110, color="#1565c0", edgecolors="white", linewidths=1.2, zorder=12)
        ax.text(x, y, str(node_id), color="white", weight="bold", ha="center", va="center", fontsize=8, zorder=13)

    ax.set_xlim(-0.05, config.container_w_m + 0.05)
    ax.set_ylim(-0.05, config.container_h_m + 0.05)
    ax.set_aspect("equal")
    ax.set_xlabel("x [m]")
    ax.set_ylabel("y [m]")


def _draw_map_panel(figure: TemporalFigure, ax: plt.Axes, mappable, config: RTIConfig, title: str) -> object:
    draw_container_and_nodes(ax, config)
    ax.set_title(title, fontsize=12, weight="bold")
    cbar = figure.fig.colorbar(mappable, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label(CBAR_LABEL, fontsize=9)
    return cbar


def draw_temporal_sequence_view(
    figure: TemporalFigure,
    context: RTIModelContext,
    result: RTISequenceResult,
    sequence_index: int,
) -> None:
    config = context.config
    clear_figure(figure)

    cur_date = result.sequence_dates[sequence_index]
    is_ref = cur_date == result.ref_date
    x_disp = result.x_disp_all[sequence_index]
    levels = np.linspace(result.vmin, result.vmax, config.contour_levels)

    # Raster map
    im = figure.ax_raster.imshow(
        x_disp.reshape((context.ny, context.nx)),
        origin="lower",
        extent=[0, config.container_w_m, 0, config.container_h_m],
        aspect="equal",
        interpolation="nearest",
        cmap=config.rti_cmap,
        vmin=result.vmin,
        vmax=result.vmax,
    )
    figure.cbar_raster = _draw_map_panel(
        figure, figure.ax_raster, im, config,
        "RTI auf dem Raster – Referenz gegen sich selbst = Nullkarte" if is_ref else "RTI-Karte der Dämpfungsänderung",
    )

    # IDW-smoothed map
    Zf = (context.idw_H @ x_disp).reshape(context.idw_X.shape)
    ax = figure.ax_smooth
    cf = ax.contourf(context.idw_X, context.idw_Y, Zf, levels=levels, cmap=config.rti_cmap, extend="both")
    ax.contour(context.idw_X, context.idw_Y, Zf, levels=levels, colors="black", linewidths=0.35, alpha=0.25)
    figure.cbar_smooth = _draw_map_panel(
        figure, ax, cf, config,
        "Geglättete Anzeige – Referenz gegen sich selbst = Nullkarte" if is_ref else "Räumlich + zeitlich regularisierte RTI-Karte",
    )

    # Mean change over time
    ax = figure.ax_total
    t_now = result.times[sequence_index]
    ax.plot(result.times, result.mean_change, linewidth=2.0, label="mittlere rekonstruierte Δ-Dämpfung")
    ax.axhline(0.0, linewidth=1.0, color="black")
    ax.axvline(t_now, linewidth=1.0, color="black", alpha=0.45)
    ax.scatter([t_now], [result.mean_change[sequence_index]], s=50, zorder=5, label=f"gezeigt: {cur_date}")
    ax.set_title("Mittlere rekonstruierte Δ-Dämpfung über die Zeit", fontsize=12, weight="bold")
    ax.set_ylabel(f"Δ-Dämpfung [dB]\n{DELTA_YLABEL}")
    ax.grid(alpha=0.2)
    ax.legend(loc="best", fontsize=9)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))

    # Raw change per link
    ax = figure.ax_links
    valid = [d for d in result.details_list[sequence_index] if d["valid"]]
    ax.bar(
        [f"{d['pair'][0]}-{d['pair'][1]}" for d in valid],
        [float(d["y"]) for d in valid],
        color=["#9e9e9e" if d["is_outer"] else "#4e79a7" for d in valid],
    )
    ax.axhline(0.0, linewidth=1.0, color="black")
    ax.set_title("Rohänderung je Verbindung", fontsize=12, weight="bold")
    ax.set_ylabel(f"Δ-Dämpfung je Link [dB]\n{DELTA_YLABEL}")
    ax.tick_params(axis="x", rotation=60)
    ax.grid(alpha=0.2)

    figure.fig.suptitle(
        "Dynamische RTI – räumlich und zeitlich regularisierte Dämpfungsänderung",
        fontsize=15,
        weight="bold",
        y=0.98,
    )
    figure.fig.tight_layout(rect=[0, 0, 1, 0.965])
