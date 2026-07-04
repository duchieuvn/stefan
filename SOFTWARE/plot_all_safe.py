from __future__ import annotations

import argparse
import math
import sqlite3
from pathlib import Path
from typing import Dict, Tuple, Optional, List

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# STYLE
# ============================================================

plt.ioff()
plt.rcParams.update({
    "axes.spines.top": False,
    "axes.spines.right": False,
    "lines.linewidth": 1.25,
})


# ============================================================
# HELPERS
# ============================================================

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def savefig(path: Path, dpi: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close()
    print("saved:", path)


def normalize_pair(a: int, b: int) -> tuple[int, int]:
    return (min(int(a), int(b)), max(int(a), int(b)))


def rolling_mean_hours(series: pd.Series, hours: int) -> pd.Series:
    hours = max(1, int(hours))
    return series.rolling(window=hours, min_periods=1).mean()


def configure_sqlite(con: sqlite3.Connection) -> None:
    # Weniger aggressiver RAM-Verbrauch bei großen Tabellen.
    con.execute("PRAGMA temp_store = FILE;")
    con.execute("PRAGMA cache_size = -50000;")
    con.execute("PRAGMA busy_timeout = 30000;")


def sql_time_expr(time_col: str) -> str:
    return f"replace({time_col}, 'T', ' ')"


def pandas_freq(rule: str) -> str:
    r = rule.lower()
    if r in ("1h", "h", "hour"):
        return "1h"
    if r in ("15min", "15m"):
        return "15min"
    raise ValueError(f"Unsupported rule: {rule}")


def time_bucket_sql(ts_sql: str, rule: str) -> str:
    r = rule.lower()

    if r in ("1h", "h", "hour"):
        return f"strftime('%Y-%m-%d %H:00:00', {ts_sql})"

    if r in ("15min", "15m"):
        return (
            f"strftime('%Y-%m-%d %H:', {ts_sql}) || "
            f"printf('%02d:00', "
            f"CAST(((CAST(strftime('%M', {ts_sql}) AS INTEGER) / 15) * 15) AS INTEGER)"
            f")"
        )

    raise ValueError(f"Unsupported rule: {rule}")


def build_time_where(ts_sql: str, start: Optional[str], end: Optional[str]) -> tuple[str, list[str]]:
    clauses = []
    params: list[str] = []

    if start:
        clauses.append(f"{ts_sql} >= ?")
        params.append(start.replace("T", " "))

    if end:
        clauses.append(f"{ts_sql} < ?")
        params.append(end.replace("T", " "))

    if not clauses:
        return "", params

    return " AND " + " AND ".join(clauses), params


def parse_nodes(text: str) -> list[int]:
    return [int(x.strip()) for x in str(text).split(",") if x.strip()]


def make_directed_links_from_nodes(nodes: list[int]) -> list[tuple[int, int]]:
    return [(a, b) for a in nodes for b in nodes if a != b]


def safe_float_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


# ============================================================
# DB SCHEMA DETECTION
# ============================================================

def detect_schema(con: sqlite3.Connection) -> dict:
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "measurement" in tables:
        table = "measurement"
    elif "measurements" in tables:
        table = "measurements"
    else:
        raise RuntimeError(f"Keine passende Tabelle gefunden. Verfügbare Tabellen: {sorted(tables)}")

    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}

    if "pc_ts" in cols:
        time_col = "pc_ts"
    elif "ts" in cols:
        time_col = "ts"
    else:
        raise RuntimeError(f"Keine Zeitspalte pc_ts/ts in {table} gefunden.")

    if "sender_id" in cols and "receiver_id" in cols:
        sender_col = "sender_id"
        receiver_col = "receiver_id"
    elif "slave_id" in cols and "rx_id" in cols:
        sender_col = "slave_id"
        receiver_col = "rx_id"
    else:
        raise RuntimeError("Keine sender/receiver Spalten gefunden.")

    if "rssi" not in cols:
        raise RuntimeError("Spalte 'rssi' fehlt.")

    return {
        "table": table,
        "time_col": time_col,
        "sender_col": sender_col,
        "receiver_col": receiver_col,
        "rssi_col": "rssi",
        "snr_col": "snr" if "snr" in cols else None,
        "bme_temp_col": "bme_temp" if "bme_temp" in cols else None,
        "bme_hum_col": "bme_hum" if "bme_hum" in cols else None,
        "bme_press_col": "bme_press" if "bme_press" in cols else None,
    }


# ============================================================
# OPTIONAL INDEX
# ============================================================

def create_helpful_index(con: sqlite3.Connection, schema: dict) -> None:
    """
    Optional. Kann bei riesiger DB lange dauern, macht Link-für-Link-Abfragen danach aber deutlich schneller.
    Start mit: --create-index
    """
    table = schema["table"]
    time_col = schema["time_col"]
    sender_col = schema["sender_col"]
    receiver_col = schema["receiver_col"]

    idx_name = f"idx_{table}_{sender_col}_{receiver_col}_{time_col}"
    print("Creating/checking index:", idx_name)
    con.execute(
        f"CREATE INDEX IF NOT EXISTS {idx_name} "
        f"ON {table} ({sender_col}, {receiver_col}, {time_col})"
    )
    con.commit()
    print("Index ready.")


# ============================================================
# LINK DISCOVERY
# ============================================================

def discover_directed_links(
    con: sqlite3.Connection,
    schema: dict,
    start: Optional[str] = None,
    end: Optional[str] = None,
    fallback_nodes: Optional[list[int]] = None,
) -> list[tuple[int, int]]:
    table = schema["table"]
    time_col = schema["time_col"]
    sender_col = schema["sender_col"]
    receiver_col = schema["receiver_col"]
    rssi_col = schema["rssi_col"]

    ts_sql = sql_time_expr(time_col)
    time_where, params = build_time_where(ts_sql, start, end)

    query = f"""
    SELECT DISTINCT
        CAST({sender_col} AS INTEGER) AS sender_id,
        CAST({receiver_col} AS INTEGER) AS receiver_id
    FROM {table}
    WHERE {sender_col} IS NOT NULL
      AND {receiver_col} IS NOT NULL
      AND {rssi_col} IS NOT NULL
      AND CAST({rssi_col} AS REAL) BETWEEN -200 AND 0
      {time_where}
    ORDER BY sender_id, receiver_id;
    """

    try:
        rows = con.execute(query, params).fetchall()
        links = [(int(s), int(r)) for s, r in rows if int(s) != int(r)]
        if links:
            return links
    except MemoryError:
        print("WARN: DISTINCT-Linksuche hat MemoryError erzeugt.")
    except Exception as e:
        print("WARN: DISTINCT-Linksuche fehlgeschlagen:", repr(e))

    if fallback_nodes:
        print("Nutze Fallback-Knoten:", fallback_nodes)
        return make_directed_links_from_nodes(fallback_nodes)

    print("Nutze Default-Fallback-Knoten 1..8")
    return make_directed_links_from_nodes(list(range(1, 9)))


# ============================================================
# RSSI: LINK-BY-LINK SQL AGGREGATION
# ============================================================

def load_one_directed_link_agg(
    con: sqlite3.Connection,
    schema: dict,
    sender_id: int,
    receiver_id: int,
    rule: str = "1h",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> pd.DataFrame:
    """
    Kernfix gegen MemoryError:
    Es wird immer nur EIN Link aggregiert, nicht die komplette Tabelle mit GROUP BY sender, receiver, hour.
    """
    table = schema["table"]
    time_col = schema["time_col"]
    sender_col = schema["sender_col"]
    receiver_col = schema["receiver_col"]
    rssi_col = schema["rssi_col"]
    snr_col = schema["snr_col"]

    ts_sql = sql_time_expr(time_col)
    bucket_sql = time_bucket_sql(ts_sql, rule)
    time_where, time_params = build_time_where(ts_sql, start, end)

    snr_select = ""
    if snr_col:
        snr_select = f", AVG(CAST({snr_col} AS REAL)) AS snr_mean"

    query = f"""
    SELECT
        {bucket_sql} AS t_bucket,
        AVG(CAST({rssi_col} AS REAL)) AS rssi_mean,
        AVG(CAST({rssi_col} AS REAL) * CAST({rssi_col} AS REAL)) AS rssi_sq_mean,
        COUNT(*) AS n
        {snr_select}
    FROM {table}
    WHERE {sender_col} = ?
      AND {receiver_col} = ?
      AND {time_col} IS NOT NULL
      AND {rssi_col} IS NOT NULL
      AND CAST({rssi_col} AS REAL) BETWEEN -200 AND 0
      {time_where}
    GROUP BY t_bucket
    ORDER BY t_bucket;
    """

    params = [int(sender_id), int(receiver_id)] + time_params

    df = pd.read_sql_query(query, con, params=params)
    if df.empty:
        return pd.DataFrame()

    df["t_bucket"] = pd.to_datetime(df["t_bucket"], errors="coerce")
    df["rssi_mean"] = safe_float_series(df["rssi_mean"])
    df["rssi_sq_mean"] = safe_float_series(df["rssi_sq_mean"])
    df["n"] = safe_float_series(df["n"])

    if "snr_mean" in df.columns:
        df["snr_mean"] = safe_float_series(df["snr_mean"])

    df = df.dropna(subset=["t_bucket", "rssi_mean"])
    if df.empty:
        return pd.DataFrame()

    var = df["rssi_sq_mean"] - df["rssi_mean"] ** 2
    df["rssi_std"] = np.sqrt(np.maximum(var, 0.0))

    keep_cols = ["t_bucket", "rssi_mean", "rssi_std", "n"]
    if "snr_mean" in df.columns:
        keep_cols.append("snr_mean")

    df = df[keep_cols].copy()
    df = df.set_index("t_bucket").sort_index()
    return df


def load_all_directed_links_linkwise(
    con: sqlite3.Connection,
    schema: dict,
    links: list[tuple[int, int]],
    rule: str = "1h",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[Tuple[int, int], pd.DataFrame]:
    out: Dict[Tuple[int, int], pd.DataFrame] = {}

    total = len(links)
    for i, (s, r) in enumerate(links, start=1):
        print(f"[{i:02d}/{total:02d}] loading {s}->{r} {rule} ...")
        df = load_one_directed_link_agg(
            con=con,
            schema=schema,
            sender_id=s,
            receiver_id=r,
            rule=rule,
            start=start,
            end=end,
        )
        if not df.empty:
            out[(int(s), int(r))] = df

    return out


def insert_missing_time_bins(
    data: Dict[Tuple[int, int], pd.DataFrame],
    freq: str = "1h",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Dict[Tuple[int, int], pd.DataFrame]:
    """
    Fehlende Stunden/Slots als NaN einfügen.
    Keine Interpolation. Kein Forward Fill. Dadurch sind Stromausfälle als Lücken sichtbar.
    """
    if not data:
        return data

    all_indices = [df.index for df in data.values() if df is not None and not df.empty]
    if not all_indices:
        return data

    pfreq = pandas_freq(freq)

    if start:
        global_start = pd.to_datetime(start.replace("T", " "), errors="coerce")
    else:
        global_start = min(idx.min() for idx in all_indices)

    if end:
        global_end = pd.to_datetime(end.replace("T", " "), errors="coerce")
    else:
        global_end = max(idx.max() for idx in all_indices)

    if pd.isna(global_start) or pd.isna(global_end):
        return data

    full_index = pd.date_range(
        start=global_start.floor(pfreq),
        end=global_end.ceil(pfreq),
        freq=pfreq,
    )

    out: Dict[Tuple[int, int], pd.DataFrame] = {}
    for link, df in data.items():
        if df is None or df.empty:
            out[link] = df
            continue
        out[link] = df.copy().sort_index().reindex(full_index)

    return out


# ============================================================
# BME: NODE-BY-NODE SQL AGGREGATION
# ============================================================

def load_one_bme_node_1h(
    con: sqlite3.Connection,
    schema: dict,
    node_id: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> pd.DataFrame:
    table = schema["table"]
    time_col = schema["time_col"]
    receiver_col = schema["receiver_col"]

    bme_cols = []
    if schema["bme_temp_col"]:
        bme_cols.append(("bme_temp", schema["bme_temp_col"], -40, 85))
    if schema["bme_hum_col"]:
        bme_cols.append(("bme_hum", schema["bme_hum_col"], 0, 100))
    if schema["bme_press_col"]:
        bme_cols.append(("bme_press", schema["bme_press_col"], 300, 1100))

    if not bme_cols:
        return pd.DataFrame()

    ts_sql = sql_time_expr(time_col)
    bucket_sql = time_bucket_sql(ts_sql, "1h")
    time_where, time_params = build_time_where(ts_sql, start, end)

    available_srcs = [src for _, src, _, _ in bme_cols]
    zero_cond = " AND ".join([f"COALESCE(CAST({src} AS REAL), 0.0) = 0.0" for src in available_srcs])

    select_parts = []
    for dst, src, lo, hi in bme_cols:
        select_parts.append(
            f"""
            AVG(
                CASE
                    WHEN NOT ({zero_cond})
                     AND CAST({src} AS REAL) BETWEEN {lo} AND {hi}
                    THEN CAST({src} AS REAL)
                    ELSE NULL
                END
            ) AS {dst}
            """
        )

    select_cols = ",\n".join(select_parts)

    query = f"""
    SELECT
        {bucket_sql} AS t_bucket,
        {select_cols}
    FROM {table}
    WHERE {receiver_col} = ?
      AND {time_col} IS NOT NULL
      {time_where}
    GROUP BY t_bucket
    ORDER BY t_bucket;
    """

    params = [int(node_id)] + time_params
    df = pd.read_sql_query(query, con, params=params)
    if df.empty:
        return pd.DataFrame()

    df["t_bucket"] = pd.to_datetime(df["t_bucket"], errors="coerce")
    df["node_id"] = int(node_id)

    for dst, _, _, _ in bme_cols:
        if dst in df.columns:
            df[dst] = safe_float_series(df[dst])

    df = df.dropna(subset=["t_bucket"])
    return df


def load_bme_nodes_1h_linkwise(
    con: sqlite3.Connection,
    schema: dict,
    nodes: list[int],
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> pd.DataFrame:
    parts = []

    for i, n in enumerate(nodes, start=1):
        print(f"BME [{i}/{len(nodes)}] loading node {n} ...")
        df = load_one_bme_node_1h(con, schema, n, start=start, end=end)
        if not df.empty:
            parts.append(df)

    if not parts:
        return pd.DataFrame()

    return pd.concat(parts, ignore_index=True).sort_values(["node_id", "t_bucket"])


def insert_missing_bme_bins(
    df_bme: pd.DataFrame,
    freq: str = "1h",
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> pd.DataFrame:
    if df_bme.empty or "t_bucket" not in df_bme.columns or "node_id" not in df_bme.columns:
        return df_bme

    pfreq = pandas_freq(freq)

    if start:
        tmin = pd.to_datetime(start.replace("T", " "), errors="coerce")
    else:
        tmin = df_bme["t_bucket"].min()

    if end:
        tmax = pd.to_datetime(end.replace("T", " "), errors="coerce")
    else:
        tmax = df_bme["t_bucket"].max()

    if pd.isna(tmin) or pd.isna(tmax):
        return df_bme

    full_index = pd.date_range(start=tmin.floor(pfreq), end=tmax.ceil(pfreq), freq=pfreq)

    parts = []
    for node_id, g in df_bme.groupby("node_id"):
        gg = g.copy().set_index("t_bucket").sort_index()
        gg = gg.reindex(full_index)
        gg["node_id"] = node_id
        gg = gg.reset_index().rename(columns={"index": "t_bucket"})
        parts.append(gg)

    if not parts:
        return df_bme

    return pd.concat(parts, ignore_index=True).sort_values(["node_id", "t_bucket"])


# ============================================================
# LEGEND / OVERVIEW
# ============================================================

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


# ============================================================
# DIRECTED PLOTS
# ============================================================

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


# ============================================================
# PAIR PLOTS
# ============================================================

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


# ============================================================
# BME PLOTS
# ============================================================

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


# ============================================================
# HTML INDEX
# ============================================================

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


# ============================================================
# CSV SUMMARY
# ============================================================

def write_link_summary(dir_1h: Dict[Tuple[int, int], pd.DataFrame], out_path: Path) -> None:
    rows = []

    for (s, r), df in sorted(dir_1h.items()):
        if df is None or df.empty or "rssi_mean" not in df.columns:
            continue

        y = df["rssi_mean"].dropna()
        n = df["n"].dropna() if "n" in df.columns else pd.Series(dtype=float)

        if y.empty:
            continue

        rows.append({
            "sender_id": s,
            "receiver_id": r,
            "hours_with_data": int(y.shape[0]),
            "hours_missing_inside_range": int(df["rssi_mean"].isna().sum()),
            "total_measurements": int(n.sum()) if not n.empty else np.nan,
            "rssi_mean_total": float(y.mean()),
            "rssi_median_total": float(y.median()),
            "rssi_std_over_time": float(y.std()) if y.shape[0] > 1 else np.nan,
            "rssi_min": float(y.min()),
            "rssi_max": float(y.max()),
            "first_time": str(y.index.min()),
            "last_time": str(y.index.max()),
        })

    if not rows:
        return

    df_out = pd.DataFrame(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_out.to_csv(out_path, index=False, encoding="utf-8")
    print("saved:", out_path)


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Memory-safe Link-für-Link Auswertung für sensor_data.db")

    parser.add_argument("--db", default="sensor_data.db", help="Pfad zur SQLite DB")
    parser.add_argument("--out", default="out_plots", help="Output-Ordner")
    parser.add_argument("--dpi", type=int, default=140, help="PNG DPI")

    parser.add_argument("--use-15min", action="store_true", help="15min-Mittel zusätzlich in directed plots")
    parser.add_argument("--nodes", default="1,2,3,4,5,6,7,8", help="Fallback/Standard-Knoten, z.B. 1,2,3,4,5,6,7,8")
    parser.add_argument("--discover-links", action="store_true", help="Links per SELECT DISTINCT suchen. Sonst werden alle gerichteten Links aus --nodes verwendet.")
    parser.add_argument("--create-index", action="store_true", help="Optional Index auf sender,receiver,time erzeugen. Kann bei großer DB dauern, beschleunigt danach stark.")

    parser.add_argument("--bme-nodes", default="5,6,7,8", help="receiver_id-Knoten für BME, z.B. 5,6,7,8")
    parser.add_argument("--bme-smooth-hours", type=int, default=6, help="Glättung über 1h-Werte")
    parser.add_argument("--bme-hide-raw1h", action="store_true", help="BME 1h-Mittel im Plot ausblenden")

    parser.add_argument("--start", default=None, help="Optionaler Startzeitpunkt, z.B. 2026-04-01 00:00:00")
    parser.add_argument("--end", default=None, help="Optionaler Endzeitpunkt, z.B. 2026-05-01 00:00:00")

    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"DB nicht gefunden: {db_path}")

    out_root = Path(args.out)
    out_directed = out_root / "directed"
    out_pairs = out_root / "pairs"
    out_bme = out_root / "bme"

    ensure_dir(out_root)
    ensure_dir(out_directed)
    ensure_dir(out_pairs)
    ensure_dir(out_bme)

    nodes = parse_nodes(args.nodes)
    bme_nodes = parse_nodes(args.bme_nodes)

    with sqlite3.connect(db_path) as con:
        configure_sqlite(con)
        schema = detect_schema(con)
        print("Detected schema:", schema)

        if args.create_index:
            create_helpful_index(con, schema)

        if args.discover_links:
            print("Discovering directed links ...")
            links = discover_directed_links(con, schema, start=args.start, end=args.end, fallback_nodes=nodes)
        else:
            links = make_directed_links_from_nodes(nodes)

        print(f"Links to process: {len(links)}")

        print("Loading directed links LINK-BY-LINK as 1h SQL aggregation ...")
        dir_1h = load_all_directed_links_linkwise(
            con=con,
            schema=schema,
            links=links,
            rule="1h",
            start=args.start,
            end=args.end,
        )

        if not dir_1h:
            raise RuntimeError("Keine RSSI-Daten gefunden.")

        dir_1h = insert_missing_time_bins(dir_1h, freq="1h", start=args.start, end=args.end)
        print(f"Directed links with data: {len(dir_1h)}")

        if args.use_15min:
            print("Loading directed links LINK-BY-LINK as 15min SQL aggregation ...")
            dir_15m = load_all_directed_links_linkwise(
                con=con,
                schema=schema,
                links=links,
                rule="15min",
                start=args.start,
                end=args.end,
            )
            dir_15m = insert_missing_time_bins(dir_15m, freq="15min", start=args.start, end=args.end)
        else:
            dir_15m = {}

        print("Loading BME node-by-node as 1h SQL aggregation ...")
        df_bme_1h = load_bme_nodes_1h_linkwise(
            con=con,
            schema=schema,
            nodes=bme_nodes,
            start=args.start,
            end=args.end,
        )

        if df_bme_1h.empty:
            print("WARN: Keine BME Daten gefunden.")
        else:
            df_bme_1h = insert_missing_bme_bins(df_bme_1h, freq="1h", start=args.start, end=args.end)

    # ------------------------------------------------------------
    # Directed plots
    # ------------------------------------------------------------

    directed_overview_map: Dict[Tuple[int, int], pd.Series] = {}

    for (s, r), d1 in sorted(dir_1h.items()):
        if d1 is None or d1.empty:
            continue

        d15 = dir_15m.get((s, r)) if args.use_15min else None
        out_path = out_directed / f"directed_{s}_to_{r}_rssi.png"

        plot_directed_link(
            sender_id=s,
            receiver_id=r,
            df_1h=d1,
            df_15m=d15,
            out_path=out_path,
            dpi=args.dpi,
        )

        directed_overview_map[(s, r)] = d1["rssi_mean"]

    plot_overview_colored(
        series_map=directed_overview_map,
        title="Overview: directed links – RSSI mean 1h | gaps preserved",
        out_path=out_directed / "overview_directed_links_1h_colored.png",
        dpi=args.dpi,
        label_fmt=lambda k: f"{k[0]}→{k[1]}",
        ylabel="RSSI [dBm]",
        legend_cols=4,
    )

    # ------------------------------------------------------------
    # Pair plots
    # ------------------------------------------------------------

    pair_overview_map: Dict[Tuple[int, int], pd.Series] = {}
    pairs = sorted({normalize_pair(s, r) for (s, r) in dir_1h.keys()})

    for (a, b) in pairs:
        ab = dir_1h.get((a, b))
        ba = dir_1h.get((b, a))

        ab_s = ab["rssi_mean"] if ab is not None and "rssi_mean" in ab.columns else pd.Series(dtype=float)
        ba_s = ba["rssi_mean"] if ba is not None and "rssi_mean" in ba.columns else pd.Series(dtype=float)

        if ab_s.empty and ba_s.empty:
            continue

        out_path = out_pairs / f"pair_{a}_{b}_rssi.png"

        pair_mean = plot_pair(
            a=a,
            b=b,
            ab_1h=ab_s,
            ba_1h=ba_s,
            out_path=out_path,
            dpi=args.dpi,
        )

        if pair_mean is not None and not pair_mean.empty:
            pair_overview_map[(a, b)] = pair_mean

    plot_overview_colored(
        series_map=pair_overview_map,
        title="Overview: pairs – pair mean RSSI 1h | gaps preserved",
        out_path=out_pairs / "overview_pairs_1h_colored.png",
        dpi=args.dpi,
        label_fmt=lambda k: f"{k[0]}↔{k[1]}",
        ylabel="RSSI [dBm]",
        legend_cols=4,
    )

    # ------------------------------------------------------------
    # BME plots
    # ------------------------------------------------------------

    if not df_bme_1h.empty:
        plot_bme_1h_smooth(
            df_bme_1h=df_bme_1h,
            out_dir=out_bme,
            dpi=args.dpi,
            smooth_hours=args.bme_smooth_hours,
            show_raw_1h=not args.bme_hide_raw1h,
        )

    # ------------------------------------------------------------
    # Summary / HTML
    # ------------------------------------------------------------

    write_link_summary(dir_1h, out_root / "link_summary_1h.csv")
    write_index_html(out_root)

    print("\nDONE ✅")
    print("Output:", out_root)
    print("Open:", out_root / "index.html")


if __name__ == "__main__":
    main()
