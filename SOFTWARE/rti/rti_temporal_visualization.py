#!/usr/bin/env python3
"""
Matplotlib visualization helpers for the temporal RTI UI.

The functions here are deliberately Tk-free. They draw into Matplotlib figures
and axes supplied by the caller, while the Tk app owns the canvas embedding.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from rti_temporal_core import RTIConfig, RTIModelContext, RTISequenceResult


@dataclass
class TemporalFigure:
    fig: plt.Figure
    ax_raster: plt.Axes
    ax_smooth: plt.Axes
    ax_total: plt.Axes
    ax_links: plt.Axes
    cbar_raster: object | None = None
    cbar_smooth: object | None = None


def create_temporal_figure() -> TemporalFigure:
    fig = plt.figure(figsize=(15.5, 10.2))
    gs = fig.add_gridspec(
        2, 2,
        height_ratios=[1.55, 0.72],
        width_ratios=[1.08, 0.92],
        hspace=0.30,
        wspace=0.22,
    )

    return TemporalFigure(
        fig=fig,
        ax_raster=fig.add_subplot(gs[0, 0]),
        ax_smooth=fig.add_subplot(gs[0, 1]),
        ax_total=fig.add_subplot(gs[1, 0]),
        ax_links=fig.add_subplot(gs[1, 1]),
    )


def clear_colorbars(figure: TemporalFigure) -> None:
    if figure.cbar_raster is not None:
        try:
            figure.cbar_raster.remove()
        except Exception:
            pass
        figure.cbar_raster = None

    if figure.cbar_smooth is not None:
        try:
            figure.cbar_smooth.remove()
        except Exception:
            pass
        figure.cbar_smooth = None


def draw_container_and_nodes(ax: plt.Axes, config: RTIConfig) -> None:
    ax.add_patch(
        Rectangle(
            (0, 0),
            config.container_w_m,
            config.container_h_m,
            fill=False,
            linewidth=2.0,
            edgecolor="black",
            zorder=10,
        )
    )

    for node_id, (x, y0) in config.node_pos_m.items():
        ax.scatter(
            x, y0,
            s=110,
            color="#1565c0",
            edgecolors="white",
            linewidths=1.2,
            zorder=12,
        )
        ax.text(
            x, y0,
            str(node_id),
            color="white",
            weight="bold",
            ha="center",
            va="center",
            fontsize=8,
            zorder=13,
        )

    ax.set_xlim(-0.05, config.container_w_m + 0.05)
    ax.set_ylim(-0.05, config.container_h_m + 0.05)
    ax.set_aspect("equal")
    ax.set_xlabel("x [m]")
    ax.set_ylabel("y [m]")


def draw_temporal_sequence_view(
    figure: TemporalFigure,
    context: RTIModelContext,
    result: RTISequenceResult,
    sequence_index: int,
    ref_date: str,
    config: RTIConfig,
) -> None:
    clear_colorbars(figure)

    cur_date = result.sequence_dates[sequence_index]
    x_disp = result.x_disp_all[sequence_index]
    X_img = x_disp.reshape((context.ny, context.nx))
    Zf = (context.idw_H @ x_disp).reshape(context.idw_X.shape)

    vmin = result.vmin
    vmax = result.vmax
    levels = np.linspace(vmin, vmax, config.contour_levels)

    ax1 = figure.ax_raster
    ax2 = figure.ax_smooth
    ax3 = figure.ax_total
    ax4 = figure.ax_links

    for ax in (ax1, ax2, ax3, ax4):
        ax.clear()

    im = ax1.imshow(
        X_img,
        origin="lower",
        extent=[0, config.container_w_m, 0, config.container_h_m],
        aspect="equal",
        interpolation="nearest",
        cmap=config.rti_cmap,
        vmin=vmin,
        vmax=vmax,
    )
    draw_container_and_nodes(ax1, config)
    if cur_date == ref_date:
        ax1.set_title("RTI auf dem Raster – Referenz gegen sich selbst = Nullkarte", fontsize=12, weight="bold")
    else:
        ax1.set_title("RTI-Karte der Dämpfungsänderung", fontsize=12, weight="bold")
    figure.cbar_raster = figure.fig.colorbar(im, ax=ax1, fraction=0.035, pad=0.02)
    figure.cbar_raster.set_label("Δ Dämpfung [dB]\nrot < 0 = weniger, blau > 0 = mehr", fontsize=9)

    cf = ax2.contourf(
        context.idw_X, context.idw_Y, Zf,
        levels=levels,
        cmap=config.rti_cmap,
        extend="both",
    )
    ax2.contour(
        context.idw_X, context.idw_Y, Zf,
        levels=levels,
        colors="black",
        linewidths=0.35,
        alpha=0.25,
    )
    draw_container_and_nodes(ax2, config)
    if cur_date == ref_date:
        ax2.set_title("Geglättete Anzeige – Referenz gegen sich selbst = Nullkarte", fontsize=12, weight="bold")
    else:
        ax2.set_title("Räumlich + zeitlich regularisierte RTI-Karte", fontsize=12, weight="bold")
    figure.cbar_smooth = figure.fig.colorbar(cf, ax=ax2, fraction=0.035, pad=0.02)
    figure.cbar_smooth.set_label("Δ Dämpfung [dB]\nrot < 0 = weniger, blau > 0 = mehr", fontsize=9)

    ax3.plot(
        result.times,
        result.mean_change,
        linewidth=2.0,
        label="mittlere rekonstruierte Δ-Dämpfung",
    )
    ax3.axhline(0.0, linewidth=1.0, color="black")
    ax3.axvline(result.times[sequence_index], linewidth=1.0, color="black", alpha=0.45)
    ax3.scatter(
        [result.times[sequence_index]],
        [result.mean_change[sequence_index]],
        s=50,
        zorder=5,
        label=f"gezeigt: {cur_date}",
    )
    ax3.set_title("Mittlere rekonstruierte Δ-Dämpfung über die Zeit", fontsize=12, weight="bold")
    ax3.set_ylabel("Δ-Dämpfung [dB]\nnegativ = weniger / trockener, positiv = mehr / feuchter")
    ax3.grid(alpha=0.2)
    ax3.legend(loc="best", fontsize=9)
    ax3.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))

    valid_details = [d for d in result.details_list[sequence_index] if d["valid"]]
    labels = [f"{d['pair'][0]}-{d['pair'][1]}" for d in valid_details]
    y_values = [float(d["y"]) for d in valid_details]
    bar_colors = ["#9e9e9e" if d["is_outer"] else "#4e79a7" for d in valid_details]

    ax4.bar(labels, y_values, color=bar_colors)
    ax4.axhline(0.0, linewidth=1.0, color="black")
    ax4.set_title("Rohänderung je Verbindung", fontsize=12, weight="bold")
    ax4.set_ylabel("Δ-Dämpfung je Link [dB]\nnegativ = weniger / trockener, positiv = mehr / feuchter")
    ax4.tick_params(axis="x", rotation=60)
    ax4.grid(alpha=0.2)

    figure.fig.suptitle(
        "Dynamische RTI – räumlich und zeitlich regularisierte Dämpfungsänderung",
        fontsize=15,
        weight="bold",
        y=0.98,
    )
    figure.fig.tight_layout(rect=[0, 0, 1, 0.965])
