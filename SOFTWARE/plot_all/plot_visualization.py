import math
from pathlib import Path
from typing import Dict, Tuple, Optional

import numpy as np
import pandas as pd

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


plt.ioff()
plt.rcParams.update({
    "axes.spines.top": False,
    "axes.spines.right": False,
    "lines.linewidth": 1.25,
})


def savefig(path: Path, dpi: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close()
    print("saved:", path)


def rolling_mean_hours(series: pd.Series, hours: int) -> pd.Series:
    hours = max(1, int(hours))
    return series.rolling(window=hours, min_periods=1).mean()


def _save_legend_only(handles, labels, out_path: Path, dpi: int = 140, ncol: int = 4, fontsize: int = 8):
    if not labels:
        return

    n = len(labels)
    ncol = max(1, int(ncol))
    nrow = int(math.ceil(n / ncol))

    fig_h = max(2.0, 0.30 * nrow)
    fig_w = 14.0

    fig = plt.figure(figsize=(fig_w, fig_h))
    fig.legend(
        handles,
        labels,
        loc="center",
        ncol=ncol,
        frameon=False,
        fontsize=fontsize,
        handlelength=2.6,
        columnspacing=1.2,
        labelspacing=0.7,
    )
    plt.axis("off")
    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print("saved:", out_path)


def plot_overview_colored(
    series_map: Dict[Tuple[int, int], pd.Series],
    title: str,
    out_path: Path,
    dpi: int,
    label_fmt,
    ylabel: str,
    legend_cols: int = 4,
):
    if not series_map:
        return

    legend_path = out_path.with_name(out_path.stem + "_legend.png")

    fig = plt.figure(figsize=(14, 7))
    ax = plt.gca()

    colors = plt.cm.tab20(np.linspace(0, 1, 20))
    keys = sorted(series_map.keys())

    for i, k in enumerate(keys):
        s = series_map[k]
        if s is None or s.empty:
            continue

        ax.plot(
            s.index,
            s.values,
            color=colors[i % len(colors)],
            linewidth=1.1,
            alpha=0.90,
            label=label_fmt(k),
        )

    ax.set_title(title)
    ax.set_xlabel("Zeit")
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.20)

    handles, labels = ax.get_legend_handles_labels()

    if len(labels) <= 18:
        ax.legend(
            fontsize=8,
            loc="center left",
            bbox_to_anchor=(1.02, 0.5),
            frameon=False,
            ncol=1,
        )
    else:
        ax.text(
            0.01,
            0.01,
            f"{len(labels)} Kurven – Legende separat: {legend_path.name}",
            transform=ax.transAxes,
            fontsize=9,
            color="0.3",
        )

    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print("saved:", out_path)

    _save_legend_only(handles, labels, legend_path, dpi=dpi, ncol=legend_cols, fontsize=8)


def plot_directed_link(
    sender_id: int,
    receiver_id: int,
    df_1h: pd.DataFrame,
    df_15m: Optional[pd.DataFrame],
    out_path: Path,
    dpi: int,
):
    if df_1h is None or df_1h.empty:
        return

    fig, ax1 = plt.subplots(figsize=(12, 4.8))

    if df_15m is not None and not df_15m.empty and "rssi_mean" in df_15m.columns:
        ax1.plot(
            df_15m.index,
            df_15m["rssi_mean"],
            color="0.55",
            linestyle="--",
            linewidth=0.9,
            alpha=0.75,
            label="RSSI mean 15min",
        )

    ax1.plot(
        df_1h.index,
        df_1h["rssi_mean"],
        color="black",
        linewidth=2.0,
        marker="o",
        markersize=2.5,
        label="RSSI mean 1h",
    )

    if "rssi_std" in df_1h.columns:
        m = df_1h["rssi_mean"]
        s = df_1h["rssi_std"].fillna(0.0)
        ax1.fill_between(
            df_1h.index,
            (m - s).to_numpy(),
            (m + s).to_numpy(),
            color="0.5",
            alpha=0.10,
            label="RSSI ±1σ 1h",
        )

    ax1.set_title(f"Directed link {sender_id} → {receiver_id}")
    ax1.set_xlabel("Zeit")
    ax1.set_ylabel("RSSI [dBm]")
    ax1.grid(True, alpha=0.18)

    ax2 = None
    if "n" in df_1h.columns:
        ax2 = ax1.twinx()
        n_values = pd.to_numeric(df_1h["n"], errors="coerce").fillna(0.0)
        ax2.bar(
            df_1h.index,
            n_values,
            width=0.035,
            alpha=0.12,
            color="0.35",
            label="n Messwerte/h",
        )
        ax2.set_ylabel("n Messwerte/h")
        ax2.tick_params(axis="y", labelsize=8)

    handles1, labels1 = ax1.get_legend_handles_labels()
    if ax2 is not None:
        handles2, labels2 = ax2.get_legend_handles_labels()
        ax1.legend(handles1 + handles2, labels1 + labels2, fontsize=8, frameon=False, ncol=2, loc="best")
    else:
        ax1.legend(fontsize=8, frameon=False, ncol=2, loc="best")

    fig.tight_layout()
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print("saved:", out_path)


def plot_pair(
    a: int,
    b: int,
    ab_1h: Optional[pd.Series],
    ba_1h: Optional[pd.Series],
    out_path: Path,
    dpi: int,
):
    if ab_1h is None:
        ab_1h = pd.Series(dtype=float)
    if ba_1h is None:
        ba_1h = pd.Series(dtype=float)
    if ab_1h.empty and ba_1h.empty:
        return None

    df = pd.concat([ab_1h.rename("ab"), ba_1h.rename("ba")], axis=1)
    df["mean"] = df[["ab", "ba"]].mean(axis=1, skipna=True)

    plt.figure(figsize=(12, 4.8))

    if df["ab"].notna().any():
        plt.plot(df.index, df["ab"], color="tab:blue", linewidth=1.5, label=f"{a}→{b} mean 1h")

    if df["ba"].notna().any():
        plt.plot(df.index, df["ba"], color="tab:orange", linewidth=1.5, label=f"{b}→{a} mean 1h")

    plt.plot(df.index, df["mean"], color="tab:red", linewidth=2.4, label="Paar-Mittelwert 1h")

    plt.title(f"Pair {a} ↔ {b}")
    plt.xlabel("Zeit")
    plt.ylabel("RSSI [dBm]")
    plt.grid(True, alpha=0.18)
    plt.legend(fontsize=8, frameon=False, ncol=2)
    plt.tight_layout()

    savefig(out_path, dpi)
    return df["mean"]


def plot_bme_1h_smooth(
    df_bme_1h: pd.DataFrame,
    out_dir: Path,
    dpi: int,
    smooth_hours: int,
    show_raw_1h: bool,
):
    if df_bme_1h.empty:
        print("No BME data found.")
        return

    label_map = {
        "bme_hum": "Humidity [%]",
        "bme_temp": "Temperature [°C]",
        "bme_press": "Pressure [hPa]",
    }

    nodes = sorted(df_bme_1h["node_id"].dropna().unique().tolist())
    colors = plt.cm.tab10(np.linspace(0, 1, 10))

    for var in ["bme_hum", "bme_temp", "bme_press"]:
        if var not in df_bme_1h.columns:
            continue
        if not df_bme_1h[var].notna().any():
            continue

        plt.figure(figsize=(14, 5))

        for i, n in enumerate(nodes):
            g = df_bme_1h[df_bme_1h["node_id"] == n][["t_bucket", var]].copy()
            if g.empty:
                continue
            g = g.set_index("t_bucket").sort_index()
            g_smooth = rolling_mean_hours(g[var], smooth_hours)

            if show_raw_1h:
                plt.plot(g.index, g[var], color=colors[i % len(colors)], linewidth=0.8, alpha=0.22)

            plt.plot(g_smooth.index, g_smooth.values, color=colors[i % len(colors)], linewidth=2.0, label=f"K{int(n)}")

        plt.title(f"{var} – receiver nodes {nodes} | 1h mean + smooth {smooth_hours}h")
        plt.xlabel("Zeit")
        plt.ylabel(label_map.get(var, var))
        plt.grid(True, alpha=0.20)
        plt.legend(frameon=False)
        plt.tight_layout()
        savefig(out_dir / f"overview_{var}_1h_smooth.png", dpi)

        for i, n in enumerate(nodes):
            g = df_bme_1h[df_bme_1h["node_id"] == n][["t_bucket", var]].copy()
            if g.empty:
                continue
            g = g.set_index("t_bucket").sort_index()
            g_smooth = rolling_mean_hours(g[var], smooth_hours)

            plt.figure(figsize=(12, 4.5))

            if show_raw_1h:
                plt.plot(g.index, g[var], color="0.75", linewidth=0.8, alpha=0.30, label="1h mean")

            plt.plot(g_smooth.index, g_smooth.values, color="black", linewidth=2.0, label=f"geglättet {smooth_hours}h")

            plt.title(f"K{int(n)} – {var}")
            plt.xlabel("Zeit")
            plt.ylabel(label_map.get(var, var))
            plt.grid(True, alpha=0.20)
            plt.legend(frameon=False)
            plt.tight_layout()
            savefig(out_dir / f"node_{int(n)}_{var}_1h_smooth.png", dpi)


def write_index_html(out_root: Path) -> None:
    def imgs_in(folder: Path) -> list[str]:
        if not folder.exists():
            return []
        return sorted([p.name for p in folder.glob("*.png")])

    directed = imgs_in(out_root / "directed")
    pairs = imgs_in(out_root / "pairs")
    bme = imgs_in(out_root / "bme")

    html = []
    html.append("<html><head><meta charset='utf-8'><title>Sensor DB Plots</title>")
    html.append(
        "<style>"
        "body{font-family:Arial;margin:20px}"
        "img{max-width:100%;height:auto;border:1px solid #ddd}"
        ".sec{margin-bottom:50px}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}"
        "</style>"
    )
    html.append("</head><body>")
    html.append("<h1>Sensor DB Plots</h1>")
    html.append("<p>Link-für-Link SQL-Aggregation gegen MemoryError. Fehlende Zeitpunkte werden als NaN eingefügt. Keine Interpolation.</p>")
    html.append("<p>Graue Balken in Directed-Plots zeigen n Messwerte pro Stunde.</p>")

    def section(title: str, folder: str, items: list[str]):
        html.append(f"<div class='sec'><h2>{title}</h2><div class='grid'>")
        for fn in items:
            path = f"{folder}/{fn}"
            html.append(f"<div><a href='{path}'><img src='{path}'></a><div style='font-size:12px;color:#333'>{fn}</div></div>")
        html.append("</div></div>")

    section("Directed", "directed", directed)
    section("Pairs", "pairs", pairs)
    section("BME", "bme", bme)

    html.append("</body></html>")
    (out_root / "index.html").write_text("\n".join(html), encoding="utf-8")
    print("saved:", out_root / "index.html")