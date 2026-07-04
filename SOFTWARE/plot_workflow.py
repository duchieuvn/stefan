import sqlite3
from pathlib import Path
from typing import Dict, Tuple, Optional

import numpy as np
import pandas as pd


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