#!/usr/bin/env python3
"""
Dynamische RTI aus sensor_data.db mit räumlicher + zeitlicher Regularisierung
=============================================================================

Diese Version ist für deinen Trocknungsprozess gebaut:

    min_{x_1,...,x_T}
        Σ_t ||W_t x_t - y_t||²
      + α_ridge Σ_t ||x_t||²
      + α_space Σ_t x_tᵀ Q x_t
      + α_time Σ_t (1/Δt_t) ||x_t - x_{t-1}||²

Der letzte Term ist die diskrete Form einer zeitlichen Ableitungsstrafe. Er
verhindert, dass Karten nur wegen Messrauschen von Tag zu Tag springen, lässt
aber bei größerem realem Zeitabstand auch mehr Änderung zu.

Wichtige Optimierungen
----------------------
1. Kein riesiges Gesamtsystem:
   Die Zeitkopplung ergibt ein block-tridiagonales System, das direkt gelöst wird.

2. Aktuelle Tagesfenster werden gesammelt geladen:
   Nicht ein kompletter DB-Scan pro Tag, sondern ein gemeinsamer Ladevorgang.

3. IDW wird vorkompiliert:
   Die teure Geometrie für die glatte Anzeige wird nur einmal berechnet.

Darstellung
-----------
- oben links: RTI auf dem groben Raster
- oben rechts: geglättete Anzeige derselben Rekonstruktion
- unten links: mittlere rekonstruierte Δ-Dämpfung über die Zeit
- unten rechts: Rohänderung je Verbindung der aktuell gezeigten Karte

Bedienung
---------
- Referenztag oben wählen.
- Karten-Schieber frei über alle nutzbaren Tage.
- Wenn Referenz = gezeigte Karte, ist die Karte per Definition die Nullkarte.
- Links/Rechts: durch die gezeigten Karten gehen
- ESC oder Q: schließen

Einordnung
----------
Das Ergebnis ist eine Karte der Änderung der Dämpfung gegenüber der gewählten
Referenz, nicht direkt absolute Feuchte.
"""

from __future__ import annotations

# Muss vor NumPy/OpenBLAS passieren, sonst kann OpenBLAS bereits beim Import
# zu viele Thread-Puffer anfordern und mit MemoryError abbrechen.
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.patches import Rectangle


# ============================================================================
# 1) PARAMETER
# ============================================================================

DB_PATH = Path(__file__).with_name("sensor_data.db")

# Rastergröße [m]
DX = 0.4
DY = 0.4

# Ellipsenbreite für RTI-Linkmodell
LAMBDA_M = 0.5

# Gewichtungsmodus für W
# "uniform_normalized" = 1/n innerhalb der Ellipse
# "uniform_binary"     = 1 innerhalb der Ellipse
# "paper"              = 1/sqrt(d_link) innerhalb der Ellipse
WEIGHT_MODE = "paper"

# Regularisierung
ALPHA_RIDGE = 0.01
ALPHA_SMOOTH = 4.0

# Zeitliche Regularisierung über erste zeitliche Ableitung
# Startwert bewusst schwach, damit nichts totgebügelt wird.
# Wenn Karten noch springen: erhöhen.
# Wenn reale Entwicklung zu stark geglättet wird: senken.
ALPHA_TIME = 0.05

# Anzeige
# Fürs Abstimmen zunächst 0.0 lassen. Sonst können kleine, aber reale Werte
# komplett auf weiß gekappt werden.
DISPLAY_DEADBAND_DB = 0.0
FINE_NX = 120
FINE_NY = 190
CONTOUR_LEVELS = 14
POSITIVE_ONLY_DISPLAY = False
RTI_CMAP = "RdBu"   # rot = weniger Dämpfung, blau = mehr Dämpfung

# robuste RSSI-Schätzung
USE_MEDIAN_RSSI = True

# Zeitfenster innerhalb jedes Tages
# Für faire Vergleiche später besser gleiche Tageszeiten wählen,
# z.B. 10:00-12:00 gegen 10:00-12:00.
REF_START_HHMM = "00:00"
REF_END_HHMM   = "23:59"

CUR_START_HHMM = "00:00"
CUR_END_HHMM   = "23:59"

# Nur Tage verwenden, die das gewählte Zeitfenster wirklich vollständig abdecken.
# Das verhindert schiefe Randtage:
# - erster Messtag begann evtl. erst mittags
# - heutiger letzter Tag ist noch nicht fertig
REQUIRE_FULL_WINDOW_COVERAGE = False
COVERAGE_EDGE_TOLERANCE_MIN = 30

# Außenlinks, die vermutlich überwiegend "durch Luft" laufen
OUTER_LINKS = {
    (1, 2), (2, 3), (1, 3),
    (4, 5), (5, 6), (4, 6),

    (1, 4), (1, 5), (1, 6),
    (2, 4), (2, 5), (2, 6),
    (3, 4), (3, 5), (3, 6),

    #(1, 7), (1, 8),
    #(2, 7), (2, 8), 
    #(3, 7), (3, 8), 
    #(4, 7), (4, 8), 
    #(5, 7), (5, 8), 
    #(6, 7), (6, 8), 
    #(7, 8),  
}

# Behandlung der Außenlinks:
# "keep"       -> normal drinlassen
# "downweight" -> geringere Sensitivität in W modellieren
# "exclude"    -> komplett aus der RTI entfernen
OUTER_LINK_MODE = "downweight" 
OUTER_LINK_FACTOR = 0.3

# Geometrie
CONTAINER_W_M = 2.390
CONTAINER_H_M = 4.020

NODE_POS_M = {
    1: (2.390, 1.075),
    2: (2.390, 2.205),
    3: (2.390, 3.335),
    4: (0.000, 3.335),
    5: (0.000, 2.205),
    6: (0.000, 1.075),
    7: (1.180, 1.300),
    8: (1.245, 2.720),
}


# ============================================================================
# 2) HILFSFUNKTIONEN
# ============================================================================

def pair_key(a: int, b: int) -> tuple[int, int]:
    return tuple(sorted((int(a), int(b))))


def all_pairs() -> list[tuple[int, int]]:
    nodes = sorted(NODE_POS_M.keys())
    pairs: list[tuple[int, int]] = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            pairs.append((nodes[i], nodes[j]))
    return pairs


def filter_pairs_by_mode(
    pairs: list[tuple[int, int]],
    outer_links: set[tuple[int, int]],
    mode: str,
) -> list[tuple[int, int]]:
    if mode == "exclude":
        return [p for p in pairs if p not in outer_links]
    if mode in ("keep", "downweight"):
        return pairs
    raise ValueError(f"Unbekannter OUTER_LINK_MODE: {mode}")


def pair_factor(pair: tuple[int, int]) -> float:
    if pair not in OUTER_LINKS:
        return 1.0

    if OUTER_LINK_MODE == "keep":
        return 1.0
    if OUTER_LINK_MODE == "downweight":
        return float(OUTER_LINK_FACTOR)
    if OUTER_LINK_MODE == "exclude":
        return 0.0

    raise ValueError(f"Unbekannter OUTER_LINK_MODE: {OUTER_LINK_MODE}")


def robust_location(values: list[float]) -> float | None:
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    if USE_MEDIAN_RSSI:
        return float(np.median(arr))
    return float(np.mean(arr))


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def parse_hhmm(txt: str) -> tuple[int, int]:
    raw = txt.strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"Ungültige Zeit '{txt}'. Erwartet: HH:MM")

    hh = int(parts[0])
    mm = int(parts[1])

    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"Ungültige Zeit '{txt}'. Erwartet: HH:MM")

    return hh, mm


def parse_pc_ts(value: str) -> datetime:
    """
    Unterstützt beide Formate:
      2026-02-27T17:23:36
      2026-02-27 17:23:36
    """
    return datetime.fromisoformat(str(value)[:19])


def build_interval_datetimes(date_txt: str, start_txt: str, end_txt: str) -> tuple[datetime, datetime]:
    hh0, mm0 = parse_hhmm(start_txt)
    hh1, mm1 = parse_hhmm(end_txt)

    dt_start = datetime.strptime(f"{date_txt} {hh0:02d}:{mm0:02d}:00", "%Y-%m-%d %H:%M:%S")
    dt_end = datetime.strptime(f"{date_txt} {hh1:02d}:{mm1:02d}:59", "%Y-%m-%d %H:%M:%S")

    if dt_end < dt_start:
        raise ValueError("Endzeit muss größer oder gleich Startzeit sein.")

    return dt_start, dt_end


def build_interval_timestamps(date_txt: str, start_txt: str, end_txt: str) -> tuple[str, str]:
    # Deine DB-Zeitstempel enthalten ein 'T', z.B. 2026-02-27T17:23:36.
    # Deshalb müssen die Vergleichsgrenzen dasselbe ISO-Format benutzen.
    dt_start, dt_end = build_interval_datetimes(date_txt, start_txt, end_txt)
    return dt_start.isoformat(timespec="seconds"), dt_end.isoformat(timespec="seconds")


def interval_midpoint(date_txt: str, start_txt: str, end_txt: str) -> datetime:
    dt_start, dt_end = build_interval_datetimes(date_txt, start_txt, end_txt)
    return dt_start + (dt_end - dt_start) / 2


def interval_label(date_txt: str, start_txt: str, end_txt: str) -> str:
    return f"{date_txt} | {start_txt}–{end_txt}"


def elapsed_days(t0: datetime, t1: datetime) -> float:
    return max((t1 - t0).total_seconds() / 86400.0, 1e-9)


# ============================================================================
# 3) RASTER
# ============================================================================

@dataclass
class Cell:
    i: int
    j: int
    k: int
    x0: float
    y0: float
    xc: float
    yc: float
    dx: float
    dy: float


def build_grid(
    width: float,
    height: float,
    dx: float,
    dy: float,
) -> tuple[int, int, list[Cell], dict[tuple[int, int], int]]:
    nx = math.ceil(width / dx)
    ny = math.ceil(height / dy)

    cells: list[Cell] = []
    ij_to_k: dict[tuple[int, int], int] = {}

    for j in range(ny):
        for i in range(nx):
            x0 = i * dx
            y0 = j * dy

            cell_w = min(dx, width - x0)
            cell_h = min(dy, height - y0)

            if cell_w <= 0 or cell_h <= 0:
                continue

            xc = x0 + 0.5 * cell_w
            yc = y0 + 0.5 * cell_h
            k = j * nx + i

            cells.append(
                Cell(
                    i=i, j=j, k=k,
                    x0=x0, y0=y0,
                    xc=xc, yc=yc,
                    dx=cell_w, dy=cell_h,
                )
            )
            ij_to_k[(i, j)] = k

    return nx, ny, cells, ij_to_k


# ============================================================================
# 4) W-MATRIX
# ============================================================================

def cell_in_link_ellipse(
    sender_xy: tuple[float, float],
    receiver_xy: tuple[float, float],
    cell_xy: tuple[float, float],
    lambda_m: float,
) -> bool:
    ds = dist(sender_xy, cell_xy)
    dr = dist(receiver_xy, cell_xy)
    d_link = dist(sender_xy, receiver_xy)
    return (ds + dr) <= (d_link + lambda_m)


def build_link_row(
    cells: list[Cell],
    sender_xy: tuple[float, float],
    receiver_xy: tuple[float, float],
    lambda_m: float,
    weight_mode: str = "uniform_normalized",
    pair_scale: float = 1.0,
) -> np.ndarray:
    inside_indices: list[int] = []

    for cell in cells:
        if cell_in_link_ellipse(sender_xy, receiver_xy, (cell.xc, cell.yc), lambda_m):
            inside_indices.append(cell.k)

    row = np.zeros(len(cells), dtype=float)
    if not inside_indices:
        return row

    d_link = dist(sender_xy, receiver_xy)

    if weight_mode == "uniform_normalized":
        fill_value = 1.0 / len(inside_indices)
    elif weight_mode == "uniform_binary":
        fill_value = 1.0
    elif weight_mode == "paper":
        fill_value = 1.0 / math.sqrt(d_link)
    else:
        raise ValueError(f"Unbekannter weight_mode: {weight_mode}")

    fill_value *= pair_scale
    row[inside_indices] = fill_value
    return row


def build_w_matrix(
    cells: list[Cell],
    pairs: list[tuple[int, int]],
    lambda_m: float,
    weight_mode: str = "uniform_normalized",
) -> np.ndarray:
    rows: list[np.ndarray] = []
    for a, b in pairs:
        row = build_link_row(
            cells=cells,
            sender_xy=NODE_POS_M[a],
            receiver_xy=NODE_POS_M[b],
            lambda_m=lambda_m,
            weight_mode=weight_mode,
            pair_scale=pair_factor((a, b)),
        )
        rows.append(row)
    return np.vstack(rows)


# ============================================================================
# 5) Q-MATRIX
# ============================================================================

def build_grid_laplacian(
    ij_to_k: dict[tuple[int, int], int],
) -> np.ndarray:
    n_cells = len(ij_to_k)
    Q = np.zeros((n_cells, n_cells), dtype=float)

    for (i, j), k in ij_to_k.items():
        neighbors = []
        for ni, nj in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
            if (ni, nj) in ij_to_k:
                neighbors.append(ij_to_k[(ni, nj)])

        Q[k, k] += len(neighbors)
        for l in neighbors:
            Q[k, l] -= 1.0

    return Q


# ============================================================================
# 6) DB-ZUGRIFF
# ============================================================================

def get_connection() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    # Weniger Locking-Stress bei großer laufender Messdatenbank.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def ensure_indexes(con: sqlite3.Connection) -> None:
    # Beschleunigt die Tages-/Zeitfenster-Filter deutlich.
    con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_pc_ts ON measurement(pc_ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_link_time ON measurement(sender_id, receiver_id, pc_ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_ok_time ON measurement(ok_match, pc_ts)")
    con.commit()


def get_all_dates(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        """
        SELECT DISTINCT substr(pc_ts, 1, 10) AS day
        FROM measurement
        WHERE pc_ts IS NOT NULL
        ORDER BY day ASC
        """
    ).fetchall()
    return [str(r[0]) for r in rows if r[0] is not None]


def get_dates_with_full_window_coverage(
    con: sqlite3.Connection,
    start_hhmm: str,
    end_hhmm: str,
    edge_tolerance_min: int,
) -> list[str]:
    """
    Gib nur Tage zurück, bei denen das angeforderte Tagesfenster an beiden
    Rändern wirklich durch Messungen abgedeckt ist.

    Beispiel bei 00:00-23:59:
    - erster Messpunkt spätestens 00:30
    - letzter Messpunkt frühestens 23:29
    """
    rows = con.execute(
        """
        SELECT
            substr(pc_ts, 1, 10) AS day,
            MIN(pc_ts) AS first_ts,
            MAX(pc_ts) AS last_ts
        FROM measurement
        WHERE pc_ts IS NOT NULL
          AND substr(pc_ts, 12, 5) >= ?
          AND substr(pc_ts, 12, 5) <= ?
        GROUP BY substr(pc_ts, 1, 10)
        ORDER BY day ASC
        """,
        (start_hhmm, end_hhmm),
    ).fetchall()

    usable: list[str] = []
    tol_s = edge_tolerance_min * 60

    for day, first_ts, last_ts in rows:
        if day is None or first_ts is None or last_ts is None:
            continue

        dt_start, dt_end = build_interval_datetimes(str(day), start_hhmm, end_hhmm)
        dt_first = parse_pc_ts(str(first_ts))
        dt_last = parse_pc_ts(str(last_ts))

        left_ok = (dt_first - dt_start).total_seconds() <= tol_s
        right_ok = (dt_end - dt_last).total_seconds() <= tol_s

        if left_ok and right_ok:
            usable.append(str(day))

    return usable


def get_available_dates(con: sqlite3.Connection) -> list[str]:
    if not REQUIRE_FULL_WINDOW_COVERAGE:
        return get_all_dates(con)

    ref_days = set(
        get_dates_with_full_window_coverage(
            con,
            REF_START_HHMM,
            REF_END_HHMM,
            COVERAGE_EDGE_TOLERANCE_MIN,
        )
    )
    cur_days = set(
        get_dates_with_full_window_coverage(
            con,
            CUR_START_HHMM,
            CUR_END_HHMM,
            COVERAGE_EDGE_TOLERANCE_MIN,
        )
    )

    return sorted(ref_days & cur_days)


def finalize_day_tmp(day_tmp: dict, start_ts: str, end_ts: str) -> tuple[dict, dict]:
    out: dict[tuple[int, int], dict] = {}
    for pair, d in day_tmp["pairs"].items():
        out[pair] = {
            "n_total": d["n_total"],
            "n_ok": d["n_ok"],
            "ok_rate": d["n_ok"] / d["n_total"] if d["n_total"] else None,
            "rssi_mean": robust_location(d["rssi_ok"]),
        }

    meta = {
        "start_ts_requested": start_ts,
        "end_ts_requested": end_ts,
        "first_ts_found": day_tmp["first_ts"],
        "last_ts_found": day_tmp["last_ts"],
        "n_measurements": day_tmp["n_rows"],
        "n_ok_measurements": day_tmp["n_ok_rows"],
        "n_rounds": len(day_tmp["round_ids"]),
    }
    return out, meta


def load_stats_for_interval(
    con: sqlite3.Connection,
    start_ts: str,
    end_ts: str,
) -> tuple[dict[tuple[int, int], dict], dict]:
    # pc_ts liegt als ISO-Text vor. Direkter Textvergleich ist schneller als
    # datetime(pc_ts), weil SQLite nicht jede Zeile erst umwandeln muss.
    cursor = con.execute(
        """
        SELECT sender_id, receiver_id, ok_match, rssi, round_id, pc_ts
        FROM measurement
        WHERE pc_ts IS NOT NULL
          AND pc_ts >= ?
          AND pc_ts <= ?
          AND sender_id IS NOT NULL
          AND receiver_id IS NOT NULL
          AND sender_id != receiver_id
        ORDER BY pc_ts ASC
        """,
        (start_ts, end_ts),
    )

    day_tmp = {
        "pairs": defaultdict(lambda: {"n_total": 0, "n_ok": 0, "rssi_ok": []}),
        "round_ids": set(),
        "n_rows": 0,
        "n_ok_rows": 0,
        "first_ts": None,
        "last_ts": None,
    }

    # Streaming statt fetchall(): hält große Tagesfenster RAM-schonend.
    for sender_id, receiver_id, ok_match, rssi, round_id, pc_ts in cursor:
        day_tmp["n_rows"] += 1
        pair = pair_key(sender_id, receiver_id)
        day_tmp["pairs"][pair]["n_total"] += 1

        if round_id is not None:
            day_tmp["round_ids"].add(int(round_id))

        if day_tmp["first_ts"] is None:
            day_tmp["first_ts"] = str(pc_ts)
        day_tmp["last_ts"] = str(pc_ts)

        if int(ok_match or 0) == 1 and rssi is not None:
            day_tmp["n_ok_rows"] += 1
            day_tmp["pairs"][pair]["n_ok"] += 1
            day_tmp["pairs"][pair]["rssi_ok"].append(float(rssi))

    return finalize_day_tmp(day_tmp, start_ts, end_ts)


def load_stats_for_many_days(
    con: sqlite3.Connection,
    dates: list[str],
    start_hhmm: str,
    end_hhmm: str,
) -> dict[str, tuple[dict, dict]]:
    """
    Lade mehrere Tage in einem einzigen DB-Zugriff und aggregiere danach in Python.
    Das ist bei großen DBs deutlich schneller als ein kompletter Scan pro Tag.
    """
    if not dates:
        return {}

    first_start_ts, _ = build_interval_timestamps(min(dates), start_hhmm, end_hhmm)
    _, last_end_ts = build_interval_timestamps(max(dates), start_hhmm, end_hhmm)

    cursor = con.execute(
        """
        SELECT sender_id, receiver_id, ok_match, rssi, round_id, pc_ts
        FROM measurement
        WHERE pc_ts IS NOT NULL
          AND pc_ts >= ?
          AND pc_ts <= ?
          AND substr(pc_ts, 12, 5) >= ?
          AND substr(pc_ts, 12, 5) <= ?
          AND sender_id IS NOT NULL
          AND receiver_id IS NOT NULL
          AND sender_id != receiver_id
        ORDER BY pc_ts ASC
        """,
        (first_start_ts, last_end_ts, start_hhmm, end_hhmm),
    )

    requested_dates = set(dates)
    tmp_by_day = {
        day: {
            "pairs": defaultdict(lambda: {"n_total": 0, "n_ok": 0, "rssi_ok": []}),
            "round_ids": set(),
            "n_rows": 0,
            "n_ok_rows": 0,
            "first_ts": None,
            "last_ts": None,
        }
        for day in dates
    }

    # Streaming statt fetchall(): mehrere Tage können sonst sehr viel RAM belegen.
    for sender_id, receiver_id, ok_match, rssi, round_id, pc_ts in cursor:
        pc_ts_s = str(pc_ts)
        day = pc_ts_s[:10]
        if day not in requested_dates:
            continue

        tmp = tmp_by_day[day]
        tmp["n_rows"] += 1
        pair = pair_key(sender_id, receiver_id)
        tmp["pairs"][pair]["n_total"] += 1

        if round_id is not None:
            tmp["round_ids"].add(int(round_id))

        if tmp["first_ts"] is None:
            tmp["first_ts"] = pc_ts_s
        tmp["last_ts"] = pc_ts_s

        if int(ok_match or 0) == 1 and rssi is not None:
            tmp["n_ok_rows"] += 1
            tmp["pairs"][pair]["n_ok"] += 1
            tmp["pairs"][pair]["rssi_ok"].append(float(rssi))

    result: dict[str, tuple[dict, dict]] = {}
    for day in dates:
        start_ts, end_ts = build_interval_timestamps(day, start_hhmm, end_hhmm)
        result[day] = finalize_day_tmp(tmp_by_day[day], start_ts, end_ts)

    return result


# ============================================================================
# 7) MESSVEKTOR y
# ============================================================================

def build_y_vector_for_pairs(
    ref_stats: dict[tuple[int, int], dict],
    cur_stats: dict[tuple[int, int], dict],
    pairs: list[tuple[int, int]],
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    y_list: list[float] = []
    valid_mask: list[bool] = []
    details: list[dict] = []

    for pair in pairs:
        ref_rssi = ref_stats.get(pair, {}).get("rssi_mean")
        cur_rssi = cur_stats.get(pair, {}).get("rssi_mean")
        ok_rate = cur_stats.get(pair, {}).get("ok_rate")

        if ref_rssi is not None and cur_rssi is not None:
            delta = cur_rssi - ref_rssi
            y_val = -delta
            valid = True
        else:
            delta = None
            y_val = None
            valid = False

        details.append(
            {
                "pair": pair,
                "ref_rssi": ref_rssi,
                "cur_rssi": cur_rssi,
                "delta": delta,
                "y": y_val,
                "ok_rate": ok_rate,
                "pair_factor": pair_factor(pair),
                "is_outer": pair in OUTER_LINKS,
                "valid": valid,
            }
        )

        if valid:
            y_list.append(float(y_val))
        valid_mask.append(valid)

    return np.asarray(y_list, dtype=float), np.asarray(valid_mask, dtype=bool), details


# ============================================================================
# 8) RTI-LÖSUNG MIT ZEITLICHER KOPPLUNG
# ============================================================================

def solve_block_tridiagonal_segment(
    diag_blocks: list[np.ndarray],
    rhs_blocks: list[np.ndarray],
    betas_between: list[float],
) -> np.ndarray:
    """
    Löse ein block-tridiagonales Segment.

    diag_blocks:      A_0 ... A_{T-1}
    rhs_blocks:       b_0 ... b_{T-1}
    betas_between:    beta_i zwischen Block i und i+1
                      Off-Diagonalblock = -beta_i * I
    """
    n_blocks = len(diag_blocks)
    if n_blocks == 0:
        return np.empty((0, 0), dtype=float)

    n_cells = diag_blocks[0].shape[0]
    I = np.eye(n_cells, dtype=float)

    if n_blocks == 1:
        return np.linalg.solve(diag_blocks[0], rhs_blocks[0]).reshape(1, -1)

    if len(betas_between) != n_blocks - 1:
        raise ValueError("Für ein Segment müssen genau n_blocks-1 beta-Werte vorliegen.")

    c_blocks: list[np.ndarray] = []
    d_blocks: list[np.ndarray] = []

    upper_0 = -betas_between[0] * I
    c_blocks.append(np.linalg.solve(diag_blocks[0], upper_0))
    d_blocks.append(np.linalg.solve(diag_blocks[0], rhs_blocks[0]))

    for t in range(1, n_blocks):
        lower_t = -betas_between[t - 1] * I
        denom = diag_blocks[t] - lower_t @ c_blocks[t - 1]
        rhs_mod = rhs_blocks[t] - lower_t @ d_blocks[t - 1]

        if t < n_blocks - 1:
            upper_t = -betas_between[t] * I
            c_blocks.append(np.linalg.solve(denom, upper_t))

        d_blocks.append(np.linalg.solve(denom, rhs_mod))

    x_blocks: list[np.ndarray] = [np.zeros(n_cells, dtype=float) for _ in range(n_blocks)]
    x_blocks[-1] = d_blocks[-1]

    for t in range(n_blocks - 2, -1, -1):
        x_blocks[t] = d_blocks[t] - c_blocks[t] @ x_blocks[t + 1]

    return np.vstack(x_blocks)


def solve_temporal_smooth_rti_block_tridiagonal(
    W_list: list[np.ndarray],
    y_list: list[np.ndarray],
    Q: np.ndarray,
    times: list[datetime],
    reference_index: int,
    alpha_ridge: float,
    alpha_smooth: float,
    alpha_time: float,
) -> tuple[np.ndarray, list[float]]:
    """
    Zeitlich regularisierte RTI mit harter Null-Verankerung der Referenz.

    Zielfunktion für alle nicht-referenziellen Zeitpunkte:
        Σ_t ||W_t x_t - y_t||²
      + α_ridge Σ_t ||x_t||²
      + α_smooth Σ_t x_tᵀ Q x_t
      + α_time Σ_t (1/Δt_t) ||x_t - x_{t-1}||²

    Wichtig:
    x_ref wird nicht nur "ungefähr", sondern exakt auf 0 gesetzt, weil es
    per Definition die Änderung gegenüber sich selbst ist. Die Zeitfolge links
    und rechts der Referenz wird mit dieser Nullbedingung gelöst.
    """
    if not W_list or not y_list:
        raise ValueError("Keine Zeitpunkte für die zeitliche RTI vorhanden.")
    if len(W_list) != len(y_list) or len(W_list) != len(times):
        raise ValueError("W_list, y_list und times müssen gleich lang sein.")

    T = len(W_list)
    if not (0 <= reference_index < T):
        raise ValueError("reference_index liegt außerhalb der Zeitfolge.")

    n_cells = Q.shape[0]
    I = np.eye(n_cells, dtype=float)

    diag_blocks: list[np.ndarray] = []
    rhs_blocks: list[np.ndarray] = []

    for W_t, y_t in zip(W_list, y_list):
        if W_t.shape[0] > 0:
            A_data = W_t.T @ W_t
            b_data = W_t.T @ y_t
        else:
            A_data = np.zeros((n_cells, n_cells), dtype=float)
            b_data = np.zeros(n_cells, dtype=float)

        diag_blocks.append(A_data + alpha_ridge * I + alpha_smooth * Q)
        rhs_blocks.append(b_data)

    dt_days_list: list[float] = []
    betas: list[float] = []

    for t in range(1, T):
        dt_days = elapsed_days(times[t - 1], times[t])
        beta = alpha_time / dt_days
        dt_days_list.append(dt_days)
        betas.append(beta)

        # Der Zeitterm beta * ||x_t - x_{t-1}||² trägt beta*I zu beiden
        # Diagonalblöcken bei. Ist einer der beiden die Referenz, bleibt diese
        # Diagonalwirkung beim Nachbarn trotzdem korrekt erhalten; x_ref=0 ist
        # dann die feste Randbedingung.
        diag_blocks[t - 1] = diag_blocks[t - 1] + beta * I
        diag_blocks[t] = diag_blocks[t] + beta * I

    x_all = np.zeros((T, n_cells), dtype=float)

    # Links der Referenz lösen, falls vorhanden.
    if reference_index > 0:
        left_diag = diag_blocks[:reference_index]
        left_rhs = rhs_blocks[:reference_index]
        left_betas = betas[:max(0, reference_index - 1)]
        x_all[:reference_index, :] = solve_block_tridiagonal_segment(
            left_diag,
            left_rhs,
            left_betas,
        )

    # Referenz exakt Null.
    x_all[reference_index, :] = 0.0

    # Rechts der Referenz lösen, falls vorhanden.
    if reference_index < T - 1:
        right_diag = diag_blocks[reference_index + 1:]
        right_rhs = rhs_blocks[reference_index + 1:]
        right_betas = betas[reference_index + 1:]
        x_all[reference_index + 1:, :] = solve_block_tridiagonal_segment(
            right_diag,
            right_rhs,
            right_betas,
        )

    return x_all, dt_days_list

# ============================================================================
# 9) DARSTELLUNG
# ============================================================================

def build_idw_operator(
    cells: list[Cell],
    nx_fine: int,
    ny_fine: int,
    power: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Baue einmalig die IDW-Interpolationsmatrix H, so dass später nur noch gilt:
        Z_flat = H @ x

    RAM-schonend in Blöcken berechnet: vermeidet große temporäre float64-
    Matrizen, falls das Raster später wieder feiner gestellt wird.
    """
    pts = np.array([(c.xc, c.yc) for c in cells], dtype=np.float32)

    gx = np.linspace(0.0, CONTAINER_W_M, nx_fine, dtype=np.float32)
    gy = np.linspace(0.0, CONTAINER_H_M, ny_fine, dtype=np.float32)
    X, Y = np.meshgrid(gx, gy)
    grid = np.column_stack((X.ravel(), Y.ravel())).astype(np.float32, copy=False)

    n_grid = grid.shape[0]
    n_cells = pts.shape[0]
    H = np.empty((n_grid, n_cells), dtype=np.float32)

    chunk = 4096
    for start in range(0, n_grid, chunk):
        stop = min(start + chunk, n_grid)
        g = grid[start:stop]

        dx = g[:, 0][:, None] - pts[:, 0][None, :]
        dy = g[:, 1][:, None] - pts[:, 1][None, :]
        d2 = dx * dx + dy * dy

        exact = d2 < np.float32(1e-20)
        d = np.sqrt(np.maximum(d2, np.float32(1e-20)))
        block = 1.0 / np.power(d, power)
        block = block / np.sum(block, axis=1, keepdims=True)

        exact_rows = np.where(np.any(exact, axis=1))[0]
        for r in exact_rows:
            c = int(np.argmax(exact[r]))
            block[r, :] = 0.0
            block[r, c] = 1.0

        H[start:stop, :] = block.astype(np.float32, copy=False)

    return X, Y, H

def prepare_display_values(x_hat: np.ndarray) -> np.ndarray:
    x = x_hat.copy()

    if POSITIVE_ONLY_DISPLAY:
        x[x < 0.0] = 0.0

    x[np.abs(x) < DISPLAY_DEADBAND_DB] = 0.0
    return x


def global_symmetric_limits(sequence_x_raw: np.ndarray) -> tuple[float, float]:
    # gemeinsame Farbskala für die ganze Folge, damit Karten wirklich vergleichbar sind
    vmax = max(abs(float(np.min(sequence_x_raw))), abs(float(np.max(sequence_x_raw))))
    vmax = max(vmax, 0.1)
    return -vmax, vmax


# ============================================================================
# 10) GUI-APP
# ============================================================================

class RTITemporalSliderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
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

        if not DB_PATH.exists():
            messagebox.showerror("Fehler", "Datenbank nicht gefunden:" + chr(10) + str(DB_PATH))
            root.destroy()
            return

        con = get_connection()
        try:
            ensure_indexes(con)
            self.available_dates = get_available_dates(con)
        finally:
            con.close()

        if not self.available_dates:
            messagebox.showerror(
                "Fehler",
                "Keine nutzbaren vollständigen Tage in measurement.pc_ts gefunden."
                + chr(10)
                + "Falls du bewusst auch angefangene Randtage sehen willst, setze"
                + chr(10)
                + "REQUIRE_FULL_WINDOW_COVERAGE = False.",
            )
            root.destroy()
            return

        self.all_pairs = all_pairs()
        self.pairs = filter_pairs_by_mode(self.all_pairs, OUTER_LINKS, OUTER_LINK_MODE)

        self.nx, self.ny, self.cells, self.ij_to_k = build_grid(CONTAINER_W_M, CONTAINER_H_M, DX, DY)
        self.W = build_w_matrix(self.cells, self.pairs, LAMBDA_M, weight_mode=WEIGHT_MODE)
        self.Q = build_grid_laplacian(self.ij_to_k)
        self.cell_areas = np.asarray([c.dx * c.dy for c in self.cells], dtype=float)
        self.total_area = float(np.sum(self.cell_areas))

        # IDW-Geometrie nur einmal vorbereiten
        self.idw_X, self.idw_Y, self.idw_H = build_idw_operator(self.cells, FINE_NX, FINE_NY)

        self.interval_cache: dict[tuple[str, str], tuple[dict, dict]] = {}
        self.sequence_cache: dict[str, dict] = {}

        # Beide Schieber frei über alle nutzbaren Tage
        self.ref_date_index = 0
        self.sequence_dates: list[str] = list(self.available_dates)
        self.current_seq_index = len(self.sequence_dates) - 1

        self.sequence_times: list[datetime] = []
        self.sequence_x: np.ndarray | None = None
        self.sequence_x_disp: np.ndarray | None = None
        self.sequence_cur_meta: list[dict] = []
        self.sequence_details: list[list[dict]] = []
        self.sequence_valid_counts: list[int] = []
        self.sequence_dt_days: list[float] = []
        self.sequence_mean_change: np.ndarray | None = None
        self.sequence_vmin = -1.0
        self.sequence_vmax = 1.0
        self.sequence_raw_min = 0.0
        self.sequence_raw_max = 0.0

        self.ref_label_var = tk.StringVar(value="")
        self.cur_label_var = tk.StringVar(value="")
        self.info_var = tk.StringVar(value="Bereit")

        self.fig = plt.figure(figsize=(15.5, 10.2))
        gs = self.fig.add_gridspec(
            2, 2,
            height_ratios=[1.55, 0.72],
            width_ratios=[1.08, 0.92],
            hspace=0.30,
            wspace=0.22,
        )

        self.ax_raster = self.fig.add_subplot(gs[0, 0])
        self.ax_smooth = self.fig.add_subplot(gs[0, 1])
        self.ax_total = self.fig.add_subplot(gs[1, 0])
        self.ax_links = self.fig.add_subplot(gs[1, 1])

        self.cbar_raster = None
        self.cbar_smooth = None

        self.build_ui()
        self.update_header_labels()
        self.bind_keyboard_controls()
        self.schedule_recompute_sequence(immediate=True)

        try:
            self.root.focus_force()
        except Exception:
            pass

    # ----------------------------------------------------------------------
    # UI
    # ----------------------------------------------------------------------

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

        self.btn_exit = ttk.Button(
            top,
            text="Beenden",
            command=self.on_close,
        )
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
        self.ref_label_var.set(interval_label(self.current_ref_date(), REF_START_HHMM, REF_END_HHMM))
        self.cur_label_var.set(interval_label(self.current_cur_date(), CUR_START_HHMM, CUR_END_HHMM))

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

        # Der gezeigte Tag bleibt stehen.
        # Nur die Referenz ändert sich; die Zeitfolge wird relativ dazu neu gelöst.
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

    # ----------------------------------------------------------------------
    # Scheduling
    # ----------------------------------------------------------------------

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

    # ----------------------------------------------------------------------
    # Schließen
    # ----------------------------------------------------------------------

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
            self.clear_colorbars()
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

    # ----------------------------------------------------------------------
    # Daten laden / Zeitfolge lösen
    # ----------------------------------------------------------------------

    def load_interval_cached(self, date_txt: str, start_hhmm: str, end_hhmm: str) -> tuple[dict, dict]:
        start_ts, end_ts = build_interval_timestamps(date_txt, start_hhmm, end_hhmm)
        key = (start_ts, end_ts)

        if key in self.interval_cache:
            return self.interval_cache[key]

        con = get_connection()
        try:
            stats, meta = load_stats_for_interval(con, start_ts, end_ts)
        finally:
            con.close()

        self.interval_cache[key] = (stats, meta)
        return stats, meta

    def ensure_many_intervals_cached(self, dates: list[str], start_hhmm: str, end_hhmm: str) -> None:
        missing_dates: list[str] = []
        for day in dates:
            start_ts, end_ts = build_interval_timestamps(day, start_hhmm, end_hhmm)
            if (start_ts, end_ts) not in self.interval_cache:
                missing_dates.append(day)

        if not missing_dates:
            return

        con = get_connection()
        try:
            loaded = load_stats_for_many_days(con, missing_dates, start_hhmm, end_hhmm)
        finally:
            con.close()

        for day, value in loaded.items():
            start_ts, end_ts = build_interval_timestamps(day, start_hhmm, end_hhmm)
            self.interval_cache[(start_ts, end_ts)] = value

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

        # Referenz einmal laden
        ref_stats, ref_meta = self.load_interval_cached(ref_date, REF_START_HHMM, REF_END_HHMM)

        # Alle aktuellen Tage in einem Rutsch laden, nicht Tag für Tag scannen
        self.ensure_many_intervals_cached(self.sequence_dates, CUR_START_HHMM, CUR_END_HHMM)

        W_list: list[np.ndarray] = []
        y_list: list[np.ndarray] = []
        times: list[datetime] = []
        cur_meta_list: list[dict] = []
        details_list: list[list[dict]] = []
        valid_counts: list[int] = []

        for date_txt in self.sequence_dates:
            cur_stats, cur_meta = self.load_interval_cached(date_txt, CUR_START_HHMM, CUR_END_HHMM)
            y_t, valid_mask_t, details_t = build_y_vector_for_pairs(ref_stats, cur_stats, self.pairs)
            W_t = self.W[valid_mask_t, :]

            W_list.append(W_t)
            y_list.append(y_t)
            times.append(interval_midpoint(date_txt, CUR_START_HHMM, CUR_END_HHMM))
            cur_meta_list.append(cur_meta)
            details_list.append(details_t)
            valid_counts.append(int(np.sum(valid_mask_t)))

        reference_index = self.sequence_dates.index(ref_date)

        x_all, dt_days_list = solve_temporal_smooth_rti_block_tridiagonal(
            W_list=W_list,
            y_list=y_list,
            Q=self.Q,
            times=times,
            reference_index=reference_index,
            alpha_ridge=ALPHA_RIDGE,
            alpha_smooth=ALPHA_SMOOTH,
            alpha_time=ALPHA_TIME,
        )

        x_disp_all = np.vstack([prepare_display_values(row) for row in x_all])
        vmin, vmax = global_symmetric_limits(x_all)

        # Physikalisch klare Gesamtkennzahl:
        # flächengewichtete mittlere rekonstruierte Δ-Dämpfung.
        # Negativ = weniger Dämpfung als Referenz = eher trockener.
        # Positiv = mehr Dämpfung als Referenz = eher feuchter.
        mean_change = (x_all @ self.cell_areas) / self.total_area

        solution = {
            "ref_date": ref_date,
            "ref_meta": ref_meta,
            "sequence_dates": list(self.sequence_dates),
            "times": times,
            "cur_meta_list": cur_meta_list,
            "details_list": details_list,
            "valid_counts": valid_counts,
            "dt_days_list": dt_days_list,
            "x_all": x_all,
            "x_disp_all": x_disp_all,
            "mean_change": mean_change,
            "vmin": vmin,
            "vmax": vmax,
        }
        self.sequence_cache[ref_date] = solution

        self.apply_sequence_solution(solution)
        self.schedule_display_update(immediate=True)

    def apply_sequence_solution(self, solution: dict) -> None:
        self.sequence_dates = list(solution["sequence_dates"])
        self.sequence_times = list(solution["times"])
        self.sequence_cur_meta = list(solution["cur_meta_list"])
        self.sequence_details = list(solution["details_list"])
        self.sequence_valid_counts = list(solution["valid_counts"])
        self.sequence_dt_days = list(solution["dt_days_list"])
        self.sequence_x = np.asarray(solution["x_all"], dtype=float)
        self.sequence_x_disp = np.asarray(solution["x_disp_all"], dtype=float)
        self.sequence_mean_change = np.asarray(solution["mean_change"], dtype=float)
        self.sequence_vmin = float(solution["vmin"])
        self.sequence_vmax = float(solution["vmax"])
        self.sequence_raw_min = float(np.min(self.sequence_x))
        self.sequence_raw_max = float(np.max(self.sequence_x))
        self.ref_meta_current = solution["ref_meta"]

        self.current_seq_index = max(0, min(self.current_seq_index, len(self.sequence_dates) - 1))
        self.refresh_current_slider_range()
        self.update_header_labels()

    # ----------------------------------------------------------------------
    # Plot
    # ----------------------------------------------------------------------

    def clear_colorbars(self) -> None:
        if self.cbar_raster is not None:
            try:
                self.cbar_raster.remove()
            except Exception:
                pass
            self.cbar_raster = None

        if self.cbar_smooth is not None:
            try:
                self.cbar_smooth.remove()
            except Exception:
                pass
            self.cbar_smooth = None

    def draw_container_and_nodes(self, ax) -> None:
        ax.add_patch(
            Rectangle(
                (0, 0),
                CONTAINER_W_M,
                CONTAINER_H_M,
                fill=False,
                linewidth=2.0,
                edgecolor="black",
                zorder=10,
            )
        )

        for node_id, (x, y0) in NODE_POS_M.items():
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

        ax.set_xlim(-0.05, CONTAINER_W_M + 0.05)
        ax.set_ylim(-0.05, CONTAINER_H_M + 0.05)
        ax.set_aspect("equal")
        ax.set_xlabel("x [m]")
        ax.set_ylabel("y [m]")

    def update_display_view(self) -> None:
        if self.close_requested:
            return
        if self.sequence_x is None or self.sequence_x_disp is None:
            return

        idx = self.current_seq_index
        ref_date = self.current_ref_date()
        cur_date = self.sequence_dates[idx]
        x_disp = self.sequence_x_disp[idx]

        mean_now = (
            float(self.sequence_mean_change[idx])
            if self.sequence_mean_change is not None
            else float("nan")
        )

        self.info_var.set(
            f"Zeitfolge relativ zu Referenz {ref_date} | "
            f"zeige {cur_date} ({idx + 1}/{len(self.sequence_dates)}) | "
            f"{'Referenz selbst: Nullkarte | ' if cur_date == ref_date else ''}"
            f"gültige Links: {self.sequence_valid_counts[idx]}/{len(self.pairs)} | "
            f"α_time={ALPHA_TIME:.3f} | "
            f"mittlere rekonstruierte Δ-Dämpfung={mean_now:.3f} dB | "
            f"Kartenwerte ΔDämpfung [{self.sequence_raw_min:.3f}, {self.sequence_raw_max:.3f}] dB"
        )

        self.plot_results(
            cur_date=cur_date,
            x_disp=x_disp,
            details=self.sequence_details[idx],
            sequence_index=idx,
        )

    def plot_results(
        self,
        cur_date: str,
        x_disp: np.ndarray,
        details: list[dict],
        sequence_index: int,
    ) -> None:
        if self.close_requested:
            return

        self.clear_colorbars()

        X_img = x_disp.reshape((self.ny, self.nx))
        Zf = (self.idw_H @ x_disp).reshape(self.idw_X.shape)

        vmin = self.sequence_vmin
        vmax = self.sequence_vmax
        levels = np.linspace(vmin, vmax, CONTOUR_LEVELS)

        ax1 = self.ax_raster
        ax2 = self.ax_smooth
        ax3 = self.ax_total
        ax4 = self.ax_links

        for ax in (ax1, ax2, ax3, ax4):
            ax.clear()

        im = ax1.imshow(
            X_img,
            origin="lower",
            extent=[0, CONTAINER_W_M, 0, CONTAINER_H_M],
            aspect="equal",
            interpolation="nearest",
            cmap=RTI_CMAP,
            vmin=vmin,
            vmax=vmax,
        )
        self.draw_container_and_nodes(ax1)
        if cur_date == self.current_ref_date():
            ax1.set_title("RTI auf dem Raster – Referenz gegen sich selbst = Nullkarte", fontsize=12, weight="bold")
        else:
            ax1.set_title("RTI-Karte der Dämpfungsänderung", fontsize=12, weight="bold")
        self.cbar_raster = self.fig.colorbar(im, ax=ax1, fraction=0.035, pad=0.02)
        self.cbar_raster.set_label("Δ Dämpfung [dB]" + chr(10) + "rot < 0 = weniger, blau > 0 = mehr", fontsize=9)

        cf = ax2.contourf(
            self.idw_X, self.idw_Y, Zf,
            levels=levels,
            cmap=RTI_CMAP,
            extend="both",
        )
        ax2.contour(
            self.idw_X, self.idw_Y, Zf,
            levels=levels,
            colors="black",
            linewidths=0.35,
            alpha=0.25,
        )
        self.draw_container_and_nodes(ax2)
        if cur_date == self.current_ref_date():
            ax2.set_title("Geglättete Anzeige – Referenz gegen sich selbst = Nullkarte", fontsize=12, weight="bold")
        else:
            ax2.set_title("Räumlich + zeitlich regularisierte RTI-Karte", fontsize=12, weight="bold")
        self.cbar_smooth = self.fig.colorbar(cf, ax=ax2, fraction=0.035, pad=0.02)
        self.cbar_smooth.set_label("Δ Dämpfung [dB]" + chr(10) + "rot < 0 = weniger, blau > 0 = mehr", fontsize=9)

        # Gleiche physikalische Größe wie die Karte:
        # negativ = weniger Dämpfung = eher trockener,
        # positiv = mehr Dämpfung = eher feuchter.
        ax3.plot(
            self.sequence_times,
            self.sequence_mean_change,
            linewidth=2.0,
            label="mittlere rekonstruierte Δ-Dämpfung",
        )
        ax3.axhline(0.0, linewidth=1.0, color="black")
        ax3.axvline(self.sequence_times[sequence_index], linewidth=1.0, color="black", alpha=0.45)
        ax3.scatter(
            [self.sequence_times[sequence_index]],
            [self.sequence_mean_change[sequence_index]],
            s=50,
            zorder=5,
            label=f"gezeigt: {cur_date}",
        )
        ax3.set_title("Mittlere rekonstruierte Δ-Dämpfung über die Zeit", fontsize=12, weight="bold")
        ax3.set_ylabel("Δ-Dämpfung [dB]" + chr(10) + "negativ = weniger / trockener, positiv = mehr / feuchter")
        ax3.grid(alpha=0.2)
        ax3.legend(loc="best", fontsize=9)
        ax3.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))
        ax3.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))

        # Rohänderung je Verbindung in derselben Vorzeichenlogik wie die Karte:
        # y = -ΔRSSI = ΔDämpfung.
        valid_details = [d for d in details if d["valid"]]
        labels = [f"{d['pair'][0]}-{d['pair'][1]}" for d in valid_details]
        y_values = [float(d["y"]) for d in valid_details]
        bar_colors = ["#9e9e9e" if d["is_outer"] else "#4e79a7" for d in valid_details]

        ax4.bar(labels, y_values, color=bar_colors)
        ax4.axhline(0.0, linewidth=1.0, color="black")
        ax4.set_title("Rohänderung je Verbindung", fontsize=12, weight="bold")
        ax4.set_ylabel("Δ-Dämpfung je Link [dB]" + chr(10) + "negativ = weniger / trockener, positiv = mehr / feuchter")
        ax4.tick_params(axis="x", rotation=60)
        ax4.grid(alpha=0.2)

        self.fig.suptitle(
            "Dynamische RTI – räumlich und zeitlich regularisierte Dämpfungsänderung",
            fontsize=15,
            weight="bold",
            y=0.98,
        )
        self.fig.tight_layout(rect=[0, 0, 1, 0.965])

        if not self.close_requested:
            self.canvas.draw_idle()


# ============================================================================
# 11) MAIN
# ============================================================================

def main() -> None:
    root = tk.Tk()
    app = RTITemporalSliderApp(root)
    if root.winfo_exists():
        root.mainloop()


if __name__ == "__main__":
    main()
