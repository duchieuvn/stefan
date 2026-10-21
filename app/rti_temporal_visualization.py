"""
Matplotlib drawing for the temporal RTI viewer.

Tk-free: draws into a figure supplied by the caller, while the Tk app owns
the canvas embedding.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from datetime import datetime

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Rectangle

from config import RTIConfig
from rti_temporal_core import RTIModelContext, RTISequenceResult, interval_midpoint
from weather import DailyWeather

CBAR_LABEL = "Δ Dämpfung [dB]\nrot < 0 = weniger, blau > 0 = mehr"
DELTA_YLABEL = "negativ = weniger / trockener\npositiv = mehr / feuchter"

TEMPERATURE_COLOR = "#eb6834"
PRECIPITATION_COLOR = "#2a78d6"
HUMIDITY_COLOR = "#1baf7a"
NOTE_COLOR = "#52514e"
NOTE_WRAP_CHARS = 36
WEATHER_TITLES = ("Lufttemperatur", "Niederschlag", "Rel. Luftfeuchte")
WEATHER_CREDIT = "Quelle: Deutscher Wetterdienst (DWD Climate Data Center)"


@dataclass
class TemporalFigure:
    fig: plt.Figure
    ax_raster: plt.Axes
    ax_smooth: plt.Axes
    ax_total: plt.Axes
    ax_links: plt.Axes
    ax_temperature: plt.Axes
    ax_precipitation: plt.Axes
    ax_humidity: plt.Axes
    cbar_raster: object | None = None
    cbar_smooth: object | None = None

    @property
    def axes(self) -> tuple[plt.Axes, ...]:
        return (
            self.ax_raster, self.ax_smooth, self.ax_total, self.ax_links,
            *self.weather_axes,
        )

    @property
    def weather_axes(self) -> tuple[plt.Axes, ...]:
        return (self.ax_temperature, self.ax_precipitation, self.ax_humidity)


def create_temporal_figure() -> TemporalFigure:
    fig = plt.figure(figsize=(15.5, 10.2))
    outer = fig.add_gridspec(2, 1, height_ratios=[1.45 + 0.72, 0.62], hspace=0.25)
    gs = outer[0].subgridspec(2, 2, height_ratios=[1.45, 0.72], width_ratios=[1.08, 0.92], hspace=0.45, wspace=0.22)
    weather_gs = outer[1].subgridspec(1, 3, wspace=0.35)
    fig.text(0.995, 0.004, WEATHER_CREDIT, ha="right", va="bottom", fontsize=8, color=NOTE_COLOR)
    return TemporalFigure(
        fig=fig,
        ax_raster=fig.add_subplot(gs[0, 0]),
        ax_smooth=fig.add_subplot(gs[0, 1]),
        ax_total=fig.add_subplot(gs[1, 0]),
        ax_links=fig.add_subplot(gs[1, 1]),
        ax_temperature=fig.add_subplot(weather_gs[0, 0]),
        ax_precipitation=fig.add_subplot(weather_gs[0, 1]),
        ax_humidity=fig.add_subplot(weather_gs[0, 2]),
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


def _format_date_axis(ax: plt.Axes) -> None:
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))


def _draw_weather_panels(
    figure: TemporalFigure,
    config: RTIConfig,
    weather: DailyWeather | None,
    weather_note: str,
    cur_date: str,
    t_now: datetime,
) -> None:
    """Temperature, precipitation and humidity per day, on the same time axis as `ax_total`."""
    if weather is None:
        note = "\n".join(textwrap.fill(line, NOTE_WRAP_CHARS) for line in weather_note.splitlines())
        for ax, title in zip(figure.weather_axes, WEATHER_TITLES):
            ax.set_title(title, fontsize=12, weight="bold")
            ax.text(0.5, 0.5, note, transform=ax.transAxes, ha="center", va="center", color=NOTE_COLOR)
            ax.set_xticks([])
            ax.set_yticks([])
        return

    temperature_title, precipitation_title, humidity_title = WEATHER_TITLES
    times = [interval_midpoint(d, config.cur_start_hhmm, config.cur_end_hhmm) for d in weather.dates]
    i_now = weather.index(cur_date)

    def title(name: str, values: np.ndarray, unit: str) -> str:
        if i_now is None or np.isnan(values[i_now]):
            return f"{name} – {cur_date}: keine Daten"
        return f"{name} – {cur_date}: {values[i_now]:.1f} {unit}"

    def mark_now(ax: plt.Axes, values: np.ndarray | None, color: str) -> None:
        ax.axvline(t_now, linewidth=1.0, color="black", alpha=0.45)
        if values is not None and i_now is not None:
            ax.scatter([t_now], [values[i_now]], s=50, color=color, edgecolors="white", linewidths=1.5, zorder=5)

    ax = figure.ax_temperature
    ax.fill_between(
        times, weather.temperature_min, weather.temperature_max,
        color=TEMPERATURE_COLOR, alpha=0.18, linewidth=0, label="Min–Max",
    )
    ax.plot(times, weather.temperature_mean, color=TEMPERATURE_COLOR, linewidth=2.0, label="Mittel")
    mark_now(ax, weather.temperature_mean, TEMPERATURE_COLOR)
    ax.set_title(title(temperature_title, weather.temperature_mean, "°C"), fontsize=12, weight="bold")
    ax.set_ylabel("Temperatur [°C]")
    ax.legend(loc="best", fontsize=9)

    ax = figure.ax_precipitation
    ax.bar(times, weather.precipitation_sum, width=0.8, color=PRECIPITATION_COLOR)
    mark_now(ax, None, PRECIPITATION_COLOR)
    ax.set_title(title(precipitation_title, weather.precipitation_sum, "mm"), fontsize=12, weight="bold")
    ax.set_ylabel("Niederschlag [mm]")
    ax.set_ylim(bottom=0)

    ax = figure.ax_humidity
    ax.plot(times, weather.humidity_mean, color=HUMIDITY_COLOR, linewidth=2.0)
    mark_now(ax, weather.humidity_mean, HUMIDITY_COLOR)
    ax.set_title(title(humidity_title, weather.humidity_mean, "%"), fontsize=12, weight="bold")
    ax.set_ylabel("Luftfeuchte [%]")

    xlim = figure.ax_total.get_xlim()
    for ax in figure.weather_axes:
        ax.set_xlim(xlim)
        ax.grid(alpha=0.2)
        _format_date_axis(ax)


def draw_temporal_sequence_view(
    figure: TemporalFigure,
    context: RTIModelContext,
    result: RTISequenceResult,
    sequence_index: int,
    weather: DailyWeather | None = None,
    weather_note: str = "",
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
    _format_date_axis(ax)

    _draw_weather_panels(figure, config, weather, weather_note, cur_date, t_now)

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
    figure.fig.tight_layout(rect=[0, 0.012, 1, 0.965])
