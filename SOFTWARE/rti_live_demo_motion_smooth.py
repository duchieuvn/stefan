#!/usr/bin/env python3
"""
RTI Live Demo / Motion-Kiosk-Ansicht
====================================

Ziel dieser Version:
- große Live-Karte für externen Bildschirm
- sichtbare Bewegung über die Zeit: automatische Animation der letzten Zeitbins
- weniger "verwaschen": glatte Karte + leicht sichtbare Blockstruktur
- verständliche Anzeige für Erstbesucher

Start:
    python rti_live_demo_motion_smooth.py

Tasten:
    ESC / q  = beenden
    f        = Vollbild umschalten
    r        = Referenz neu wählen
    s        = Screenshot speichern
    space    = Animation pausieren/fortsetzen
"""

from __future__ import annotations

import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import math
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patheffects as pe
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.patches import Rectangle

# =============================================================================
# 1) DEMO-KONFIGURATION
# =============================================================================

DB_PATH = Path(__file__).with_name("sensor_data.db")

# Aktuelle Live-Karte rechts im Status
# Fester Referenztag
REFERENCE_DAY = "2026-03-03"

# Ganze Zeitspanne seit Referenztag zeigen
# Smooth-Variante: Historie wird einmal vorbereitet, Animation spielt nur noch fertige Arrays ab.
SHOW_LAST_WEEKS = 4  # bleibt nur aus Kompatibilitaet im Code stehen
ANIMATION_RECALC_EVERY_N_REFRESHES = 999999  # im Kiosk praktisch nicht neu rechnen
STATIC_KIOSK_MODE = True      # stabil: Frames einmal laden, danach nur noch abspielen
DATA_REFRESH_SECONDS = 300   # nur relevant, wenn STATIC_KIOSK_MODE = False
ANIMATION_FRAME_SECONDS_MIN = 0.16  # zu schnell erzeugt Tk/Matplotlib-Ruckler
ANIMATION_FRAME_SECONDS_MAX = 0.85  # Ende sichtbar langsamer, aber nicht zaeh
ANIMATION_RESTART_PAUSE_SECONDS = 1.6  # kurze Pause am letzten Tag
STATUS_UPDATE_EVERY_N_FRAMES = 4       # Text nicht in jedem Frame neu setzen
FULLSCREEN_START = True

# Referenzfenster: kompletter Referenztag
REF_WINDOW_MIN = 60  # hier nicht genutzt, bleibt nur der Kompatibilitaet halber stehen
REF_START_TS = "2026-03-03T00:00:00"
REF_END_TS = "2026-03-03T23:59:59"

# Anzeige
COLOR_LIMIT_DB = 1.2
AUTO_EXPAND_COLOR_LIMIT = True
RTI_CMAP = "RdBu"              # weniger Dämpfung = rot, mehr Dämpfung = blau
SMOOTH_INTERPOLATION = "bilinear"  # geglättet, aber noch klar
FINE_NX = 105   # bewusst etwas kleiner: deutlich fluessiger auf schwacher Hardware
FINE_NY = 176
DISPLAY_GAMMA = 0.55            # stärkere Kontrastanhebung, damit die Karte nicht blass wirkt
SCREENSHOT_DIR = Path(__file__).with_name("rti_screenshots")

# RTI-Modell
DX = 0.4
DY = 0.4
LAMBDA_M = 0.45
WEIGHT_MODE = "paper"
ALPHA_RIDGE = 0.03
ALPHA_SMOOTH = 0.9   # moderat geglättet, noch klar erkennbar
USE_MEDIAN_RSSI = True
DISPLAY_DEADBAND_DB = 0.03
POSITIVE_ONLY_DISPLAY = False

# Link-Politik
# Ziel: außen↔außen nicht als Bildinformation nutzen, weil diese Links stark Luft/Rand/Wetter sehen.
# Standard hier: außen↔außen ausblenden, außen↔innen behalten, innen↔innen behalten.
OUTER_NODES = {1, 2, 3, 4, 5, 6}
INNER_NODES = {7, 8}

# Rand-/Luftlinks außen↔außen. Diese sind fuer Feuchte im Bett meist die schlechtesten Bildgeber.
OUTER_OUTER_LINKS = {
    # gleiche Seitenwand
    (1, 2), (2, 3), (1, 3),
    (4, 5), (5, 6), (4, 6),
    # quer von Außenwand zu Außenwand
    (1, 4), (1, 5), (1, 6),
    (2, 4), (2, 5), (2, 6),
    (3, 4), (3, 5), (3, 6),
}

# getrennte Faktoren machen das Testen viel einfacher
OUTER_OUTER_MODE = "exclude"      # Empfehlung Demo: "exclude"; Diagnose: "downweight" mit 0.10..0.25
OUTER_OUTER_FACTOR = 0.15
OUTER_INNER_MODE = "keep"         # außen↔innen tragen die Tomographie, also behalten
OUTER_INNER_FACTOR = 1.00
INNER_INNER_MODE = "keep"         # 7↔8 ist wichtig fuer Zentrum/Vertikaltrend
INNER_INNER_FACTOR = 1.15

# Mindestdatenqualitaet pro Link/Tag
MIN_OK_PER_LINK_PER_DAY = 3
MIN_OK_RATE_PER_LINK = 0.02

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

# =============================================================================
# 2) BASISFUNKTIONEN
# =============================================================================

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


def parse_pc_ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value)[:19])


def fmt_ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def pair_key(a: int, b: int) -> tuple[int, int]:
    return tuple(sorted((int(a), int(b))))


def dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def all_pairs() -> list[tuple[int, int]]:
    nodes = sorted(NODE_POS_M.keys())
    return [(nodes[i], nodes[j]) for i in range(len(nodes)) for j in range(i + 1, len(nodes))]


def link_class(pair: tuple[int, int]) -> str:
    a, b = pair
    if a in OUTER_NODES and b in OUTER_NODES:
        return "outer_outer"
    if a in INNER_NODES and b in INNER_NODES:
        return "inner_inner"
    return "outer_inner"


def _mode_for_link(pair: tuple[int, int]) -> str:
    cls = link_class(pair)
    if cls == "outer_outer":
        return OUTER_OUTER_MODE
    if cls == "inner_inner":
        return INNER_INNER_MODE
    return OUTER_INNER_MODE


def filter_pairs_by_mode(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = []
    for p in pairs:
        mode = _mode_for_link(p)
        if mode == "exclude":
            continue
        if mode not in ("keep", "downweight"):
            raise ValueError(f"Unbekannter Link-Modus fuer {p}: {mode}")
        out.append(p)
    return out


def pair_factor(pair: tuple[int, int]) -> float:
    cls = link_class(pair)
    if cls == "outer_outer":
        mode, factor = OUTER_OUTER_MODE, OUTER_OUTER_FACTOR
    elif cls == "inner_inner":
        mode, factor = INNER_INNER_MODE, INNER_INNER_FACTOR
    else:
        mode, factor = OUTER_INNER_MODE, OUTER_INNER_FACTOR

    if mode == "keep":
        return 1.0
    if mode == "downweight":
        return float(factor)
    if mode == "exclude":
        return 0.0
    raise ValueError(f"Unbekannter Link-Modus fuer {pair}: {mode}")


def robust_location(values: list[float]) -> float | None:
    if not values:
        return None
    arr = np.asarray(values, dtype=np.float32)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return None
    if USE_MEDIAN_RSSI:
        return float(np.median(arr))
    return float(np.mean(arr))


def prepare_display_values(x_hat: np.ndarray) -> np.ndarray:
    x = np.asarray(x_hat, dtype=np.float32).copy()
    if POSITIVE_ONLY_DISPLAY:
        x[x < 0.0] = 0.0
    if DISPLAY_DEADBAND_DB > 0:
        x[np.abs(x) < DISPLAY_DEADBAND_DB] = 0.0
    return x


def enhance_for_display(arr: np.ndarray, color_limit: float) -> np.ndarray:
    """Nichtlineare Kontrastverstärkung: kleine Signale werden sichtbarer,
    große Werte bleiben durch color_limit begrenzt. """
    a = np.asarray(arr, dtype=np.float32)
    limit = max(float(color_limit), 1e-6)
    norm = np.clip(np.abs(a) / limit, 0.0, 1.0)
    boosted = np.sign(a) * (norm ** DISPLAY_GAMMA) * limit
    return boosted.astype(np.float32, copy=False)


def region_label(x: float, y: float) -> str:
    if x < CONTAINER_W_M / 3:
        xs = "links"
    elif x > 2 * CONTAINER_W_M / 3:
        xs = "rechts"
    else:
        xs = "mittig"

    if y < CONTAINER_H_M / 3:
        ys = "unten"
    elif y > 2 * CONTAINER_H_M / 3:
        ys = "oben"
    else:
        ys = "mittig"

    if xs == "mittig" and ys == "mittig":
        return "im Zentrum"
    if xs == "mittig":
        return ys
    if ys == "mittig":
        return xs
    return f"{ys} {xs}"


# =============================================================================
# 3) DB
# =============================================================================

class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA cache_size=-200000")
        con.execute("PRAGMA busy_timeout=5000")
        return con

    def ensure_indexes(self) -> None:
        with self.connect() as con:
            con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_pc_ts ON measurement(pc_ts)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_link_time ON measurement(sender_id, receiver_id, pc_ts)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_measurement_ok_time ON measurement(ok_match, pc_ts)")
            con.commit()

    def min_max_ts(self) -> tuple[datetime, datetime]:
        with self.connect() as con:
            row = con.execute(
                """
                SELECT MIN(pc_ts), MAX(pc_ts)
                FROM measurement
                WHERE pc_ts IS NOT NULL
                """
            ).fetchone()
        if not row or row[0] is None or row[1] is None:
            raise RuntimeError("Keine Zeitstempel in measurement.pc_ts gefunden.")
        return parse_pc_ts(row[0]), parse_pc_ts(row[1])

    def load_interval_stats(self, start_dt: datetime, end_dt: datetime) -> tuple[dict, dict]:
        start_ts = fmt_ts(start_dt)
        end_ts = fmt_ts(end_dt)

        tmp = defaultdict(lambda: {"n_total": 0, "n_ok": 0, "rssi_ok": []})
        first_ts = None
        last_ts = None
        n_rows = 0
        n_ok_rows = 0

        with self.connect() as con:
            cur = con.execute(
                """
                SELECT sender_id, receiver_id, ok_match, rssi, pc_ts
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
            for sender_id, receiver_id, ok_match, rssi, pc_ts in cur:
                n_rows += 1
                pair = pair_key(sender_id, receiver_id)
                tmp[pair]["n_total"] += 1
                if first_ts is None:
                    first_ts = str(pc_ts)
                last_ts = str(pc_ts)

                if int(ok_match or 0) == 1 and rssi is not None:
                    n_ok_rows += 1
                    tmp[pair]["n_ok"] += 1
                    tmp[pair]["rssi_ok"].append(float(rssi))

        stats = {}
        for pair, d in tmp.items():
            stats[pair] = {
                "n_total": d["n_total"],
                "n_ok": d["n_ok"],
                "ok_rate": d["n_ok"] / d["n_total"] if d["n_total"] else None,
                "rssi_mean": robust_location(d["rssi_ok"]),
            }

        meta = {
            "start_ts_requested": start_ts,
            "end_ts_requested": end_ts,
            "first_ts_found": first_ts,
            "last_ts_found": last_ts,
            "n_measurements": n_rows,
            "n_ok_measurements": n_ok_rows,
        }
        return stats, meta

    def load_daily_stats(self, start_day: datetime, end_dt: datetime) -> list[tuple[datetime, datetime, datetime, dict, dict]]:
        """Lädt alle Tages-Frames in einem einzigen SQLite-Durchlauf.
        Das ist deutlich fluessiger als 80-120 einzelne Tagesabfragen beim Start.
        """
        start_day = datetime(start_day.year, start_day.month, start_day.day)
        end_day = datetime(end_dt.year, end_dt.month, end_dt.day)
        n_days = max(1, (end_day.date() - start_day.date()).days + 1)

        tmp_by_day = [defaultdict(lambda: {"n_total": 0, "n_ok": 0, "rssi_ok": []}) for _ in range(n_days)]
        meta_by_day = [
            {"n_measurements": 0, "n_ok_measurements": 0, "first_ts_found": None, "last_ts_found": None}
            for _ in range(n_days)
        ]

        with self.connect() as con:
            cur = con.execute(
                """
                SELECT sender_id, receiver_id, ok_match, rssi, pc_ts
                FROM measurement
                WHERE pc_ts IS NOT NULL
                  AND pc_ts >= ?
                  AND pc_ts <= ?
                  AND sender_id IS NOT NULL
                  AND receiver_id IS NOT NULL
                  AND sender_id != receiver_id
                ORDER BY pc_ts ASC
                """,
                (fmt_ts(start_day), fmt_ts(end_dt)),
            )
            for sender_id, receiver_id, ok_match, rssi, pc_ts in cur:
                dt = parse_pc_ts(str(pc_ts))
                idx = (dt.date() - start_day.date()).days
                if idx < 0 or idx >= n_days:
                    continue

                meta = meta_by_day[idx]
                meta["n_measurements"] += 1
                if meta["first_ts_found"] is None:
                    meta["first_ts_found"] = str(pc_ts)
                meta["last_ts_found"] = str(pc_ts)

                pair = pair_key(sender_id, receiver_id)
                d = tmp_by_day[idx][pair]
                d["n_total"] += 1
                if int(ok_match or 0) == 1 and rssi is not None:
                    meta["n_ok_measurements"] += 1
                    d["n_ok"] += 1
                    d["rssi_ok"].append(float(rssi))

        out = []
        for idx, tmp in enumerate(tmp_by_day):
            day_start = start_day + timedelta(days=idx)
            day_end = min(end_dt, day_start + timedelta(days=1) - timedelta(seconds=1))
            day_mid = day_start + (day_end - day_start) / 2
            stats = {}
            for pair, d in tmp.items():
                ok_rate = d["n_ok"] / d["n_total"] if d["n_total"] else None
                rssi_mean = None
                if d["n_ok"] >= MIN_OK_PER_LINK_PER_DAY and (ok_rate is None or ok_rate >= MIN_OK_RATE_PER_LINK):
                    rssi_mean = robust_location(d["rssi_ok"])
                stats[pair] = {
                    "n_total": d["n_total"],
                    "n_ok": d["n_ok"],
                    "ok_rate": ok_rate,
                    "rssi_mean": rssi_mean,
                }
            out.append((day_start, day_end, day_mid, stats, meta_by_day[idx]))
        return out

    def load_binned_stats(self, start_dt: datetime, end_dt: datetime, bin_minutes: int) -> list[tuple[datetime, datetime, datetime, dict, dict]]:
        start_ts = fmt_ts(start_dt)
        end_ts = fmt_ts(end_dt)
        bin_s = int(bin_minutes * 60)
        n_bins = max(1, int(math.ceil((end_dt - start_dt).total_seconds() / bin_s)))

        tmp_by_bin = [defaultdict(lambda: {"n_total": 0, "n_ok": 0, "rssi_ok": []}) for _ in range(n_bins)]
        meta_by_bin = [
            {"n_measurements": 0, "n_ok_measurements": 0, "first_ts_found": None, "last_ts_found": None}
            for _ in range(n_bins)
        ]

        with self.connect() as con:
            cur = con.execute(
                """
                SELECT sender_id, receiver_id, ok_match, rssi, pc_ts
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
            for sender_id, receiver_id, ok_match, rssi, pc_ts in cur:
                dt = parse_pc_ts(str(pc_ts))
                idx = int((dt - start_dt).total_seconds() // bin_s)
                if idx < 0 or idx >= n_bins:
                    continue

                meta = meta_by_bin[idx]
                meta["n_measurements"] += 1
                if meta["first_ts_found"] is None:
                    meta["first_ts_found"] = str(pc_ts)
                meta["last_ts_found"] = str(pc_ts)

                pair = pair_key(sender_id, receiver_id)
                tmp_by_bin[idx][pair]["n_total"] += 1
                if int(ok_match or 0) == 1 and rssi is not None:
                    meta["n_ok_measurements"] += 1
                    tmp_by_bin[idx][pair]["n_ok"] += 1
                    tmp_by_bin[idx][pair]["rssi_ok"].append(float(rssi))

        out = []
        for idx, tmp in enumerate(tmp_by_bin):
            bin_start = start_dt + timedelta(seconds=idx * bin_s)
            bin_end = min(end_dt, bin_start + timedelta(seconds=bin_s))
            bin_mid = bin_start + timedelta(seconds=(bin_end - bin_start).total_seconds() / 2)
            stats = {}
            for pair, d in tmp.items():
                stats[pair] = {
                    "n_total": d["n_total"],
                    "n_ok": d["n_ok"],
                    "ok_rate": d["n_ok"] / d["n_total"] if d["n_total"] else None,
                    "rssi_mean": robust_location(d["rssi_ok"]),
                }
            out.append((bin_start, bin_end, bin_mid, stats, meta_by_bin[idx]))
        return out

# =============================================================================
# 4) RTI MODELL
# =============================================================================

class RTIModel:
    def __init__(self) -> None:
        self.pairs = filter_pairs_by_mode(all_pairs())
        self.nx, self.ny, self.cells, self.ij_to_k = self.build_grid(CONTAINER_W_M, CONTAINER_H_M, DX, DY)
        self.W_full = self.build_w_matrix(self.cells, self.pairs)
        self.Q = self.build_grid_laplacian(self.ij_to_k)
        self.n_cells = len(self.cells)
        self.I = np.eye(self.n_cells, dtype=np.float64)
        self.reg_base = ALPHA_RIDGE * self.I + ALPHA_SMOOTH * self.Q
        self.cell_areas = np.asarray([c.dx * c.dy for c in self.cells], dtype=np.float64)
        self.total_area = float(np.sum(self.cell_areas))
        self.Xf, self.Yf, self.H_idw = self.build_idw_operator(self.cells, FINE_NX, FINE_NY)

    @staticmethod
    def build_grid(width: float, height: float, dx: float, dy: float):
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
                cells.append(Cell(i, j, k, x0, y0, xc, yc, cell_w, cell_h))
                ij_to_k[(i, j)] = k
        return nx, ny, cells, ij_to_k

    @staticmethod
    def cell_in_link_ellipse(sender_xy, receiver_xy, cell_xy, lambda_m: float) -> bool:
        ds = dist(sender_xy, cell_xy)
        dr = dist(receiver_xy, cell_xy)
        d_link = dist(sender_xy, receiver_xy)
        return (ds + dr) <= (d_link + lambda_m)

    @staticmethod
    def build_link_row(cells, sender_xy, receiver_xy, lambda_m, weight_mode, pair_scale):
        inside = []
        for cell in cells:
            if RTIModel.cell_in_link_ellipse(sender_xy, receiver_xy, (cell.xc, cell.yc), lambda_m):
                inside.append(cell.k)

        row = np.zeros(len(cells), dtype=np.float64)
        if not inside:
            return row

        d_link = dist(sender_xy, receiver_xy)
        if weight_mode == "uniform_normalized":
            fill_value = 1.0 / len(inside)
        elif weight_mode == "uniform_binary":
            fill_value = 1.0
        elif weight_mode == "paper":
            fill_value = 1.0 / math.sqrt(d_link)
        else:
            raise ValueError(f"Unbekannter WEIGHT_MODE: {weight_mode}")

        fill_value *= pair_scale
        row[inside] = fill_value
        return row

    def build_w_matrix(self, cells, pairs):
        rows = []
        for a, b in pairs:
            rows.append(
                self.build_link_row(
                    cells=cells,
                    sender_xy=NODE_POS_M[a],
                    receiver_xy=NODE_POS_M[b],
                    lambda_m=LAMBDA_M,
                    weight_mode=WEIGHT_MODE,
                    pair_scale=pair_factor((a, b)),
                )
            )
        return np.vstack(rows)

    @staticmethod
    def build_grid_laplacian(ij_to_k):
        n_cells = len(ij_to_k)
        Q = np.zeros((n_cells, n_cells), dtype=np.float64)
        for (i, j), k in ij_to_k.items():
            neighbors = []
            for ni, nj in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)):
                if (ni, nj) in ij_to_k:
                    neighbors.append(ij_to_k[(ni, nj)])
            Q[k, k] += len(neighbors)
            for n in neighbors:
                Q[k, n] -= 1.0
        return Q

    @staticmethod
    def build_idw_operator(cells, nx_fine: int, ny_fine: int, power: float = 2.0):
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

    def build_y(self, ref_stats: dict, cur_stats: dict):
        y_list = []
        valid = []
        details = []
        for pair in self.pairs:
            ref_rssi = ref_stats.get(pair, {}).get("rssi_mean")
            cur_rssi = cur_stats.get(pair, {}).get("rssi_mean")
            ok_rate = cur_stats.get(pair, {}).get("ok_rate")
            if ref_rssi is not None and cur_rssi is not None:
                delta_rssi = float(cur_rssi) - float(ref_rssi)
                y = -delta_rssi
                valid.append(True)
                y_list.append(y)
            else:
                delta_rssi = None
                y = None
                valid.append(False)
            details.append({
                "pair": pair,
                "ref_rssi": ref_rssi,
                "cur_rssi": cur_rssi,
                "delta_rssi": delta_rssi,
                "y": y,
                "ok_rate": ok_rate,
                "link_class": link_class(pair),
            })
        return np.asarray(y_list, dtype=np.float64), np.asarray(valid, dtype=bool), details

    def solve(self, ref_stats: dict, cur_stats: dict):
        y, valid, details = self.build_y(ref_stats, cur_stats)
        valid_count = int(np.sum(valid))

        if valid_count == 0:
            x = np.zeros(self.n_cells, dtype=np.float64)
        else:
            W = self.W_full[valid, :]
            A = W.T @ W + self.reg_base
            b = W.T @ y
            try:
                x = np.linalg.solve(A, b)
            except np.linalg.LinAlgError:
                x = np.linalg.lstsq(A, b, rcond=None)[0]

        x_disp = prepare_display_values(x)
        X_block = x_disp.reshape((self.ny, self.nx))
        Z = (self.H_idw @ x_disp.astype(np.float32)).reshape((FINE_NY, FINE_NX))
        mean_change = float((x @ self.cell_areas) / self.total_area)

        idx_max = int(np.argmax(x))
        idx_min = int(np.argmin(x))
        wet_cell = self.cells[idx_max]
        dry_cell = self.cells[idx_min]

        return {
            "x": x,
            "X_block": X_block,
            "Z": Z,
            "mean_change": mean_change,
            "valid_count": valid_count,
            "details": details,
            "wet_pos": (wet_cell.xc, wet_cell.yc),
            "wet_desc": region_label(wet_cell.xc, wet_cell.yc),
            "wet_val": float(x[idx_max]),
            "dry_pos": (dry_cell.xc, dry_cell.yc),
            "dry_desc": region_label(dry_cell.xc, dry_cell.yc),
            "dry_val": float(x[idx_min]),
        }

# =============================================================================
# 5) LIVE-DEMO-APP
# =============================================================================

class LiveDemoApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("RF Moisture Imaging – Live Motion Demo")
        self.fullscreen = bool(FULLSCREEN_START)
        self.animation_paused = False
        self.refresh_counter = 0
        self.playback_index = 0
        self.completed_cycle = False
        self.color_limit = float(COLOR_LIMIT_DB)
        self.last_solution = None
        self.ref_start_dt = None
        self.ref_end_dt = None
        self.ref_stats = None
        self.ref_meta = None
        self.history_frames = []
        self.latest_ts = None
        self.last_cur_meta = {}
        self.startup_loaded = False
        self.trend_x = np.asarray([], dtype=float)
        self.trend_y = np.asarray([], dtype=float)
        self.trend_ymax = 0.2
        self.frame_cache_ready = False
        self.last_status_frame = -9999

        if not DB_PATH.exists():
            messagebox.showerror("Fehler", f"Datenbank nicht gefunden:\n{DB_PATH}")
            root.destroy()
            return

        self.db = Database(DB_PATH)
        self.db.ensure_indexes()
        self.model = RTIModel()
        self.choose_reference(force=True)
        self.build_ui()
        self.bind_keys()
        self.apply_fullscreen()
        self.update_once()
        self.root.after(int(ANIMATION_FRAME_SECONDS_MIN * 1000), self.animate_step)

    def choose_reference(self, force: bool = False) -> None:
        if self.ref_stats is not None and not force:
            return
        if REF_START_TS.strip() and REF_END_TS.strip():
            start = parse_pc_ts(REF_START_TS.strip())
            end = parse_pc_ts(REF_END_TS.strip())
        else:
            first_ts, _ = self.db.min_max_ts()
            start = first_ts
            end = first_ts + timedelta(minutes=REF_WINDOW_MIN)
        self.ref_start_dt = start
        self.ref_end_dt = end
        self.ref_stats, self.ref_meta = self.db.load_interval_stats(start, end)

    def build_ui(self) -> None:
        self.fig = plt.figure(figsize=(16, 9), facecolor="#0b0f14")
        gs = self.fig.add_gridspec(2, 2, width_ratios=[3.45, 1.35], height_ratios=[3.0, 1.0], hspace=0.16, wspace=0.08)
        self.ax_map = self.fig.add_subplot(gs[:, 0])
        self.ax_status = self.fig.add_subplot(gs[0, 1])
        self.ax_trend = self.fig.add_subplot(gs[1, 1])

        for ax in (self.ax_map, self.ax_status, self.ax_trend):
            ax.set_facecolor("#0b0f14")

        empty_Z = np.zeros((FINE_NY, FINE_NX), dtype=np.float32)
        empty_block = np.zeros((self.model.ny, self.model.nx), dtype=np.float32)

        # Glatte Anzeige
        self.im = self.ax_map.imshow(
            empty_Z,
            origin="lower",
            extent=[0, CONTAINER_W_M, 0, CONTAINER_H_M],
            interpolation=SMOOTH_INTERPOLATION,
            cmap=RTI_CMAP,
            vmin=-self.color_limit,
            vmax=self.color_limit,
            aspect="equal",
            alpha=1.00,
            zorder=1,
        )

        # Blockkarte als Hauptanzeige, damit klar erkennbar bleibt WO etwas passiert
        self.im_blocks = self.ax_map.imshow(
            empty_block,
            origin="lower",
            extent=[0, CONTAINER_W_M, 0, CONTAINER_H_M],
            interpolation="nearest",
            cmap=RTI_CMAP,
            vmin=-self.color_limit,
            vmax=self.color_limit,
            aspect="equal",
            alpha=0.18,
            zorder=2,
        )

        self.draw_container_and_nodes(self.ax_map)
        self.ax_map.set_title("LIVE RF MOISTURE MAP  |  SICHTBARER ZEITVERLAUF IM BIOFILTER", fontsize=20, weight="bold", color="white", pad=14)
        self.ax_map.set_xlabel("Breite [m]", color="#d7dde8")
        self.ax_map.set_ylabel("Höhe [m]", color="#d7dde8")
        self.ax_map.tick_params(colors="#d7dde8")

        # Große Tagesanzeige direkt auf der Karte: keine Box, keine Animation-Legende.
        self.day_text = self.ax_map.text(
            0.045, 0.965, "",
            transform=self.ax_map.transAxes,
            ha="left", va="top",
            color="white",
            fontsize=54,
            weight="bold",
            zorder=30,
        )
        self.day_text.set_path_effects([
            pe.Stroke(linewidth=5.5, foreground="#05070a"),
            pe.Normal(),
        ])

        self.date_text = self.ax_map.text(
            0.050, 0.855, "",
            transform=self.ax_map.transAxes,
            ha="left", va="top",
            color="#d7dde8",
            fontsize=18,
            weight="bold",
            zorder=30,
        )
        self.date_text.set_path_effects([
            pe.Stroke(linewidth=3.5, foreground="#05070a"),
            pe.Normal(),
        ])

        self.cbar = self.fig.colorbar(self.im, ax=self.ax_map, fraction=0.035, pad=0.025)
        self.cbar.set_label("Δ Dämpfung [dB]\nrot = weniger Dämpfung | blau = mehr Dämpfung", color="white", fontsize=10)
        self.cbar.ax.yaxis.set_tick_params(color="white")
        plt.setp(plt.getp(self.cbar.ax.axes, "yticklabels"), color="white")

        self.ax_status.axis("off")
        self.ax_status.text(0.02, 0.96, "BIOFILTER LIVE", transform=self.ax_status.transAxes, ha="left", va="top", color="white", fontsize=24, weight="bold")

        self.legend_text = self.ax_status.text(
            0.02, 0.86,
            "FARBLEGENDE\nBLAU = mehr Dämpfung\nROT = weniger Dämpfung",
            transform=self.ax_status.transAxes,
            ha="left",
            va="top",
            color="white",
            fontsize=14,
            weight="bold",
            linespacing=1.35,
        )

        self.status_text = self.ax_status.text(0.02, 0.64, "starte ...", transform=self.ax_status.transAxes, ha="left", va="top", color="#d7dde8", fontsize=12, linespacing=1.38)
        self.big_value = self.ax_status.text(0.02, 0.18, "0.00 dB", transform=self.ax_status.transAxes, ha="left", va="bottom", color="white", fontsize=34, weight="bold")
        self.big_label = self.ax_status.text(0.02, 0.08, "mittlere rekonstruierte Δ-Dämpfung", transform=self.ax_status.transAxes, ha="left", va="bottom", color="#9fb0c4", fontsize=11)

        self.trend_line, = self.ax_trend.plot([], [], linewidth=2.2, color="#d7dde8")
        self.trend_marker_shown, = self.ax_trend.plot([], [], marker="o", markersize=8, color="#69b7ff")
        self.trend_marker_latest, = self.ax_trend.plot([], [], marker="o", markersize=8, color="white")
        self.ax_trend.axhline(0.0, linewidth=1.0, color="#5e6b7a")
        self.ax_trend.set_title("Verlauf über die Tage seit Referenz", color="white", fontsize=14, weight="bold")
        self.ax_trend.set_ylabel("ΔD [dB]", color="#d7dde8")
        self.ax_trend.tick_params(colors="#d7dde8", labelsize=9)
        self.ax_trend.grid(alpha=0.18, color="white")
        self.ax_trend.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=5))
        self.ax_trend.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))

        self.fig.tight_layout(pad=1.0)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def draw_container_and_nodes(self, ax) -> None:
        ax.add_patch(Rectangle((0, 0), CONTAINER_W_M, CONTAINER_H_M, fill=False, linewidth=2.4, edgecolor="white", zorder=10))
        # Gitterstruktur der Blöcke andeuten
        for x in np.arange(0.0, CONTAINER_W_M + 1e-9, DX):
            ax.plot([x, x], [0.0, CONTAINER_H_M], color="white", alpha=0.07, linewidth=0.8, zorder=5)
        for y in np.arange(0.0, CONTAINER_H_M + 1e-9, DY):
            ax.plot([0.0, CONTAINER_W_M], [y, y], color="white", alpha=0.07, linewidth=0.8, zorder=5)
        for node_id, (x, y) in NODE_POS_M.items():
            ax.scatter(x, y, s=155, color="#1667c7", edgecolors="white", linewidths=1.3, zorder=12)
            ax.text(x, y, str(node_id), color="white", weight="bold", ha="center", va="center", fontsize=9, zorder=13)
        ax.set_xlim(-0.05, CONTAINER_W_M + 0.05)
        ax.set_ylim(-0.05, CONTAINER_H_M + 0.05)
        ax.set_aspect("equal")

    def bind_keys(self) -> None:
        self.root.bind_all("<Key>", self.on_key)

    def on_key(self, event) -> None:
        key = str(getattr(event, "keysym", "")).lower()
        if key in ("escape", "q"):
            self.close()
        elif key == "f":
            self.fullscreen = not self.fullscreen
            self.apply_fullscreen()
        elif key == "r":
            self.choose_reference(force=True)
            self.history_frames = []
            self.update_once()
        elif key == "s":
            self.save_screenshot()
        elif key == "space":
            self.animation_paused = not self.animation_paused

    def apply_fullscreen(self) -> None:
        try:
            self.root.attributes("-fullscreen", self.fullscreen)
        except Exception:
            pass
        if not self.fullscreen:
            self.root.geometry("1600x950")

    def compute_history_frames(self, latest_ts: datetime):
        latest_day_start = datetime(latest_ts.year, latest_ts.month, latest_ts.day)
        ref_day_start = datetime.fromisoformat(REFERENCE_DAY + "T00:00:00")

        frames = []
        skipped_days = 0

        try:
            daily = self.db.load_daily_stats(ref_day_start, latest_ts)
        except Exception:
            daily = []

        for day_start, day_end, day_mid, stats, meta in daily:
            try:
                if meta.get("n_ok_measurements", 0) <= 0:
                    skipped_days += 1
                    continue
                sol = self.model.solve(self.ref_stats, stats)
                if sol["valid_count"] <= 0:
                    skipped_days += 1
                    continue
                frames.append({
                    "bin_start": day_start,
                    "bin_end": day_end,
                    "bin_mid": day_mid,
                    "sol": sol,
                    "meta": meta,
                })
            except Exception:
                skipped_days += 1

        if frames:
            self.history_frames = frames
            self.prepare_frame_cache()
            if self.playback_index >= len(frames) or self.playback_index < 0:
                self.playback_index = 0
                self.completed_cycle = False
            self.startup_loaded = True
        elif not self.history_frames:
            self.playback_index = -1
            self.startup_loaded = False
            self.frame_cache_ready = False

        return len(frames), skipped_days

    def prepare_frame_cache(self) -> None:
        """Alles vorberechnen, was sonst jeden Animationsframe ruckeln laesst."""
        if not self.history_frames:
            self.frame_cache_ready = False
            return

        max_abs = 0.0
        for fr in self.history_frames:
            x = fr["sol"].get("x")
            if x is not None and np.asarray(x).size:
                max_abs = max(max_abs, float(np.nanpercentile(np.abs(x), 88)))
        if self.last_solution is not None and self.last_solution.get("x") is not None:
            x = self.last_solution["x"]
            if np.asarray(x).size:
                max_abs = max(max_abs, float(np.nanpercentile(np.abs(x), 88)))

        if AUTO_EXPAND_COLOR_LIMIT:
            self.color_limit = max(COLOR_LIMIT_DB, math.ceil(max_abs * 10.0) / 10.0)
        self.im.set_clim(-self.color_limit, self.color_limit)
        self.im_blocks.set_clim(-self.color_limit, self.color_limit)

        for fr in self.history_frames:
            sol = fr["sol"]
            sol["Z_disp"] = enhance_for_display(sol["Z"], self.color_limit)
            sol["X_block_disp"] = enhance_for_display(sol["X_block"], self.color_limit)

        self.trend_x = mdates.date2num([f["bin_mid"] for f in self.history_frames])
        self.trend_y = np.asarray([f["sol"]["mean_change"] for f in self.history_frames], dtype=float)
        self.trend_ymax = max(0.2, float(np.nanmax(np.abs(self.trend_y))) * 1.15) if self.trend_y.size else 0.2
        if self.last_solution is not None:
            self.trend_ymax = max(self.trend_ymax, abs(float(self.last_solution["mean_change"])) * 1.25)

        self.trend_line.set_data(self.trend_x, self.trend_y)
        if self.trend_x.size:
            x0 = self.trend_x[0]
            x1 = self.trend_x[-1] if self.trend_x[-1] > self.trend_x[0] else self.trend_x[0] + 1.0 / 24.0
            self.ax_trend.set_xlim(x0, x1)
        self.ax_trend.set_ylim(-self.trend_ymax, self.trend_ymax)
        self.frame_cache_ready = True

    def update_once(self) -> None:
        start_wall = time.perf_counter()
        try:
            self.latest_ts = self.db.min_max_ts()[1]
            live_end = self.latest_ts
            live_start = datetime(self.latest_ts.year, self.latest_ts.month, self.latest_ts.day)

            try:
                cur_stats, cur_meta = self.db.load_interval_stats(live_start, live_end)
                self.last_solution = self.model.solve(self.ref_stats, cur_stats)
                self.last_cur_meta = cur_meta
            except Exception:
                cur_meta = self.last_cur_meta if self.last_cur_meta else {}
                if self.last_solution is None and self.history_frames:
                    self.last_solution = self.history_frames[-1]["sol"]

            if self.refresh_counter == 0 or not self.history_frames:
                self.status_text.set_text("lade Tagesframes ...")
                self.canvas.draw_idle()
                frame_count, skipped_days = self.compute_history_frames(self.latest_ts)
            elif not STATIC_KIOSK_MODE and self.refresh_counter % ANIMATION_RECALC_EVERY_N_REFRESHES == 0:
                frame_count, skipped_days = self.compute_history_frames(self.latest_ts)

            if self.last_solution is None and self.history_frames:
                self.last_solution = self.history_frames[-1]["sol"]

            shown_frame = None
            shown_sol = self.last_solution
            if self.history_frames:
                if self.playback_index < 0 or self.playback_index >= len(self.history_frames):
                    self.playback_index = 0
                    self.completed_cycle = False
                shown_frame = self.history_frames[self.playback_index]
                shown_sol = shown_frame["sol"]

            if shown_sol is not None:
                self.update_map(shown_sol, shown_frame)
                self.update_status(cur_meta, self.last_solution, live_start, live_end, start_wall, shown_frame)
                self.update_trend(shown_frame)
            self.canvas.draw_idle()
            self.refresh_counter += 1
        except Exception as e:
            self.status_text.set_text(f"Fehler:\n{e}")
            self.canvas.draw_idle()

        if not STATIC_KIOSK_MODE:
            self.root.after(int(DATA_REFRESH_SECONDS * 1000), self.update_once)

    def update_map(self, sol: dict, shown_frame: dict | None) -> None:
        z = sol.get("Z_disp")
        if z is None:
            z = enhance_for_display(sol["Z"], self.color_limit)
        xb = sol.get("X_block_disp")
        if xb is None:
            xb = enhance_for_display(sol["X_block"], self.color_limit)

        self.im.set_data(z)
        self.im_blocks.set_data(xb)

        if shown_frame is not None:
            day_no = self.playback_index + 1
            period = f"{shown_frame['bin_start']:%d.%m.%Y}"
            self.day_text.set_text(f"TAG {day_no}")
            self.date_text.set_text(period)
        else:
            self.day_text.set_text("TAG")
            self.date_text.set_text("heute")

    def update_status(self, cur_meta: dict, current_sol: dict, live_start: datetime, live_end: datetime, start_wall: float, shown_frame: dict | None) -> None:
        elapsed_ms = (time.perf_counter() - start_wall) * 1000.0
        if current_sol is None:
            current_sol = shown_frame["sol"] if shown_frame is not None else {"mean_change": 0.0, "valid_count": 0}
        mean_change = float(current_sol.get("mean_change", 0.0))
        n_all = len(self.model.pairs)
        n_valid = int(current_sol.get("valid_count", 0))
        n_meas = cur_meta.get("n_measurements", 0)
        n_ok = cur_meta.get("n_ok_measurements", 0)

        if mean_change > 0.05:
            state = "mehr Dämpfung / eher feuchter"
        elif mean_change < -0.05:
            state = "weniger Dämpfung / eher trockener"
        else:
            state = "nahe Referenz / stabil"

        shown_txt = "-"
        if shown_frame is not None:
            shown_txt = f"TAG {self.playback_index + 1}  |  {shown_frame['bin_start']:%d.%m.%Y}"

        self.status_text.set_text(
            f"Aktueller Status: {state}\n"
            f"aktueller Tag: {live_start:%d.%m.%Y}\n"
            f"gezeigter Schritt: {shown_txt}\n"
            f"letzter Messpunkt: {self.latest_ts:%d.%m.%Y %H:%M:%S}\n"
            f"gültige Links jetzt: {n_valid}/{n_all}\n"
            f"Messungen jetzt: {n_ok}/{n_meas} ok\n"
            f"Zeitverlauf: 1 Tag pro Schritt\n"
            f"Referenz: 03.03.2026\n"
            f"Frames geladen: {len(self.history_frames)}"
        )
        self.big_value.set_text(f"{mean_change:+.2f} dB")

    def update_trend(self, shown_frame: dict | None) -> None:
        if not self.history_frames:
            return
        if not self.frame_cache_ready:
            self.prepare_frame_cache()

        if shown_frame is not None:
            shown_x = mdates.date2num(shown_frame["bin_mid"])
            shown_y = shown_frame["sol"]["mean_change"]
            self.trend_marker_shown.set_data([shown_x], [shown_y])
        else:
            self.trend_marker_shown.set_data([], [])

        if self.last_solution is not None and self.latest_ts is not None:
            latest_x = mdates.date2num(self.latest_ts)
            latest_y = self.last_solution["mean_change"]
            self.trend_marker_latest.set_data([latest_x], [latest_y])
        else:
            self.trend_marker_latest.set_data([], [])

    def animate_step(self) -> None:
        next_delay_s = ANIMATION_FRAME_SECONDS_MIN
        try:
            if self.history_frames and not self.animation_paused:
                n = len(self.history_frames)

                # aktuellen Frame anzeigen
                self.playback_index = max(0, min(self.playback_index, n - 1))
                shown_frame = self.history_frames[self.playback_index]
                self.update_map(shown_frame["sol"], shown_frame)
                if self.last_solution is not None and self.latest_ts is not None:
                    if abs(self.playback_index - self.last_status_frame) >= STATUS_UPDATE_EVERY_N_FRAMES or self.playback_index in (0, n - 1):
                        live_start = datetime(self.latest_ts.year, self.latest_ts.month, self.latest_ts.day)
                        live_end = self.latest_ts
                        self.update_status(shown_frame.get("meta", {}), self.last_solution, live_start, live_end, time.perf_counter(), shown_frame)
                        self.last_status_frame = self.playback_index
                self.update_trend(shown_frame)
                self.canvas.draw_idle()

                # am Anfang schnell, gegen Ende zunehmend langsamer
                if n <= 1:
                    progress = 1.0
                else:
                    progress = self.playback_index / float(n - 1)
                eased = progress ** 1.8
                next_delay_s = ANIMATION_FRAME_SECONDS_MIN + (ANIMATION_FRAME_SECONDS_MAX - ANIMATION_FRAME_SECONDS_MIN) * eased

                # bis zum letzten Frame laufen, dort kurz halten, dann neu starten
                if self.playback_index >= n - 1:
                    self.completed_cycle = True
                    self.playback_index = 0
                    next_delay_s = ANIMATION_RESTART_PAUSE_SECONDS
                else:
                    self.playback_index += 1
                    self.completed_cycle = False
        except Exception as e:
            try:
                self.status_text.set_text(f"Fehler in Animation:\n{e}")
                self.canvas.draw_idle()
            except Exception:
                pass
            next_delay_s = 1.0
        finally:
            self.root.after(max(1, int(next_delay_s * 1000)), self.animate_step)

    def save_screenshot(self) -> None:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = SCREENSHOT_DIR / f"rti_live_motion_{ts}.png"
        self.fig.savefig(out, dpi=180, facecolor=self.fig.get_facecolor())
        self.status_text.set_text(self.status_text.get_text() + f"\nScreenshot: {out.name}")
        self.canvas.draw_idle()

    def close(self) -> None:
        try:
            self.root.quit()
        finally:
            try:
                self.root.destroy()
            except Exception:
                pass

# =============================================================================
# 6) START
# =============================================================================

def main() -> None:
    root = tk.Tk()
    app = LiveDemoApp(root)
    if root.winfo_exists():
        root.mainloop()


if __name__ == "__main__":
    main()
