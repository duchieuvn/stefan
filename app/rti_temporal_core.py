#!/usr/bin/env python3
"""
Core logic for the dynamic temporal RTI view (no Tk / Matplotlib).

Sections:
    SQL          - queries against the `measurement` table
    Model        - reconstruction grid, link weights W, Laplacian Q, IDW operator
    Measurements - time windows, available dates, per-link RSSI statistics
    Solver       - block-tridiagonal temporal solver
    Sequence     - full sequence relative to one reference day
    Cache        - on-disk `sequence_cache/<date>.joblib` files
"""

from __future__ import annotations

# Must happen before NumPy/OpenBLAS import.
import os
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from dateutil.relativedelta import relativedelta

from config import RTIConfig
from database import DatabaseProvider


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ==========================================================================
# SQL (psycopg2 `%s` placeholders; the SQLite provider translates them)
# ==========================================================================

DATA_RANGE = "SELECT MIN(pc_ts), MAX(pc_ts) FROM measurement"

# Params: (since_day,)
DISTINCT_DAYS = """
    SELECT DISTINCT substr(pc_ts, 1, 10) AS day
    FROM measurement
    WHERE pc_ts IS NOT NULL
      AND pc_ts >= %s
    ORDER BY day ASC
"""

# Params: (since_day, start_hhmm, end_hhmm)
DAY_WINDOW_COVERAGE = """
    SELECT
        substr(pc_ts, 1, 10) AS day,
        MIN(pc_ts) AS first_ts,
        MAX(pc_ts) AS last_ts
    FROM measurement
    WHERE pc_ts IS NOT NULL
      AND pc_ts >= %s
      AND substr(pc_ts, 12, 5) >= %s
      AND substr(pc_ts, 12, 5) <= %s
    GROUP BY substr(pc_ts, 1, 10)
    ORDER BY day ASC
"""

# Params: (start_ts, end_ts)
MEASUREMENT_IN_RANGE = """
    SELECT sender_id, receiver_id, ok_match, rssi, round_id, pc_ts
    FROM measurement
    WHERE pc_ts IS NOT NULL
        AND pc_ts >= %s
        AND pc_ts <= %s
        AND sender_id IS NOT NULL
        AND receiver_id IS NOT NULL
        AND sender_id != receiver_id
    ORDER BY pc_ts ASC
"""

# PostgreSQL only (percentile_cont, FILTER, GROUPING SETS). Aggregates
# MEASUREMENT_IN_RANGE for every day of one HH:MM window on the server, in a
# single scan of measurement_pc_ts_idx. Rows with is_day_total = 1 carry the
# per-day IntervalMeta, the others the per-link LinkStats.
# Params: (since_day, start_hhmm, end_hhmm)
LINK_DAY_STATS = """
    SELECT
        substr(pc_ts, 1, 10) AS day,
        LEAST(sender_id, receiver_id) AS a,
        GREATEST(sender_id, receiver_id) AS b,
        GROUPING(LEAST(sender_id, receiver_id)) AS is_day_total,
        COUNT(*) AS n_total,
        COUNT(*) FILTER (WHERE ok_match = 1 AND rssi IS NOT NULL) AS n_ok,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi) FILTER (WHERE ok_match = 1) AS rssi_median,
        AVG(rssi) FILTER (WHERE ok_match = 1) AS rssi_avg,
        MIN(pc_ts) AS first_ts,
        MAX(pc_ts) AS last_ts,
        COUNT(DISTINCT round_id) AS n_rounds
    FROM measurement
    WHERE pc_ts >= %s
        AND substr(pc_ts, 12, 5) >= %s
        AND substr(pc_ts, 12, 5) <= %s
        AND sender_id IS NOT NULL
        AND receiver_id IS NOT NULL
        AND sender_id != receiver_id
    GROUP BY GROUPING SETS (
        (substr(pc_ts, 1, 10), LEAST(sender_id, receiver_id), GREATEST(sender_id, receiver_id)),
        (substr(pc_ts, 1, 10))
    )
"""


# ==========================================================================
# Model
# ==========================================================================

Pair = tuple[int, int]


@dataclass(frozen=True)
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


@dataclass
class RTIModelContext:
    config: RTIConfig
    all_pairs: list[Pair]
    pairs: list[Pair]
    nx: int
    ny: int
    cells: list[Cell]
    ij_to_k: dict[tuple[int, int], int]
    W: np.ndarray
    Q: np.ndarray
    cell_areas: np.ndarray
    total_area: float
    idw_X: np.ndarray
    idw_Y: np.ndarray
    idw_H: np.ndarray


def pair_key(a: int, b: int) -> Pair:
    return tuple(sorted((int(a), int(b))))


def all_pairs(config: RTIConfig) -> list[Pair]:
    nodes = sorted(config.node_pos_m)
    return [(a, b) for i, a in enumerate(nodes) for b in nodes[i + 1:]]


def pair_factor(pair: Pair, config: RTIConfig) -> float:
    if pair not in config.outer_links:
        return 1.0
    return {"keep": 1.0, "downweight": float(config.outer_link_factor), "exclude": 0.0}[config.outer_link_mode]


def build_grid(width: float, height: float, dx: float, dy: float) -> tuple[int, int, list[Cell], dict[tuple[int, int], int]]:
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

            k = j * nx + i
            cells.append(Cell(i=i, j=j, k=k, x0=x0, y0=y0, xc=x0 + 0.5 * cell_w, yc=y0 + 0.5 * cell_h, dx=cell_w, dy=cell_h))
            ij_to_k[(i, j)] = k

    return nx, ny, cells, ij_to_k


def build_link_row(
    cells: list[Cell],
    sender_xy: tuple[float, float],
    receiver_xy: tuple[float, float],
    lambda_m: float,
    weight_mode: str,
    pair_scale: float = 1.0,
) -> np.ndarray:
    """One row of W: cells inside the link ellipse (d_s + d_r <= d_link + lambda) get a weight."""
    d_link = math.dist(sender_xy, receiver_xy)
    inside = [
        c.k for c in cells
        if math.dist(sender_xy, (c.xc, c.yc)) + math.dist(receiver_xy, (c.xc, c.yc)) <= d_link + lambda_m
    ]

    row = np.zeros(len(cells), dtype=float)
    if not inside:
        return row

    fill_value = {
        "uniform_normalized": 1.0 / len(inside),
        "uniform_binary": 1.0,
        "paper": 1.0 / math.sqrt(d_link),
    }[weight_mode]
    row[inside] = fill_value * pair_scale
    return row


def build_w_matrix(cells: list[Cell], pairs: list[Pair], config: RTIConfig) -> np.ndarray:
    return np.vstack([
        build_link_row(
            cells,
            config.node_pos_m[a],
            config.node_pos_m[b],
            config.lambda_m,
            config.weight_mode,
            pair_scale=pair_factor((a, b), config),
        )
        for a, b in pairs
    ])


def build_grid_laplacian(ij_to_k: dict[tuple[int, int], int]) -> np.ndarray:
    n_cells = len(ij_to_k)
    Q = np.zeros((n_cells, n_cells), dtype=float)

    for (i, j), k in ij_to_k.items():
        neighbors = [
            ij_to_k[n] for n in ((i - 1, j), (i + 1, j), (i, j - 1), (i, j + 1)) if n in ij_to_k
        ]
        Q[k, k] += len(neighbors)
        for l in neighbors:
            Q[k, l] -= 1.0

    return Q


def build_idw_operator(
    cells: list[Cell],
    nx_fine: int,
    ny_fine: int,
    width: float,
    height: float,
    power: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Returns the fine display grid (X, Y) and H with `H @ x` = IDW-interpolated x."""
    pts = np.array([(c.xc, c.yc) for c in cells], dtype=np.float32)

    gx = np.linspace(0.0, width, nx_fine, dtype=np.float32)
    gy = np.linspace(0.0, height, ny_fine, dtype=np.float32)
    X, Y = np.meshgrid(gx, gy)
    grid = np.column_stack((X.ravel(), Y.ravel())).astype(np.float32, copy=False)

    n_grid = grid.shape[0]
    H = np.empty((n_grid, pts.shape[0]), dtype=np.float32)

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

        # Grid points that coincide with a cell centre take that cell's value.
        for r in np.where(np.any(exact, axis=1))[0]:
            block[r, :] = 0.0
            block[r, int(np.argmax(exact[r]))] = 1.0

        H[start:stop, :] = block.astype(np.float32, copy=False)

    return X, Y, H


def build_model_context(config: RTIConfig) -> RTIModelContext:
    pair_all = all_pairs(config)
    if config.outer_link_mode == "exclude":
        pairs = [p for p in pair_all if p not in config.outer_links]
    else:
        pairs = pair_all

    nx, ny, cells, ij_to_k = build_grid(config.container_w_m, config.container_h_m, config.dx, config.dy)
    cell_areas = np.asarray([c.dx * c.dy for c in cells], dtype=float)
    idw_X, idw_Y, idw_H = build_idw_operator(
        cells, config.fine_nx, config.fine_ny, config.container_w_m, config.container_h_m
    )

    return RTIModelContext(
        config=config,
        all_pairs=pair_all,
        pairs=pairs,
        nx=nx,
        ny=ny,
        cells=cells,
        ij_to_k=ij_to_k,
        W=build_w_matrix(cells, pairs, config),
        Q=build_grid_laplacian(ij_to_k),
        cell_areas=cell_areas,
        total_area=float(np.sum(cell_areas)),
        idw_X=idw_X,
        idw_Y=idw_Y,
        idw_H=idw_H,
    )


# ==========================================================================
# Measurements
# ==========================================================================

LinkStats = dict[Pair, dict[str, Any]]
IntervalMeta = dict[str, Any]


# --- Time intervals ---

def interval_bounds(date_txt: str, start_hhmm: str, end_hhmm: str) -> tuple[datetime, datetime]:
    """[start:00, end:59] of the HH:MM window on `date_txt` (YYYY-MM-DD)."""
    dt_start = datetime.strptime(f"{date_txt} {start_hhmm.strip()}", "%Y-%m-%d %H:%M")
    dt_end = datetime.strptime(f"{date_txt} {end_hhmm.strip()}", "%Y-%m-%d %H:%M") + timedelta(seconds=59)
    if dt_end < dt_start:
        raise ValueError("Endzeit muss größer oder gleich Startzeit sein.")
    return dt_start, dt_end


def interval_midpoint(date_txt: str, start_hhmm: str, end_hhmm: str) -> datetime:
    dt_start, dt_end = interval_bounds(date_txt, start_hhmm, end_hhmm)
    return dt_start + (dt_end - dt_start) / 2


def interval_label(date_txt: str, start_hhmm: str, end_hhmm: str) -> str:
    return f"{date_txt} | {start_hhmm}–{end_hhmm}"


def elapsed_days(t0: datetime, t1: datetime) -> float:
    return max((t1 - t0).total_seconds() / 86400.0, 1e-9)


def _parse_pc_ts(value: Any) -> datetime:
    return datetime.fromisoformat(str(value)[:19])


# --- Available dates ---

def history_start(con: Any, config: RTIConfig) -> str | None:
    """First day (YYYY-MM-DD) of the last `config.history_months` months of data (all data
    if it is None); None if the table is empty."""
    earliest, latest = con.execute(DATA_RANGE).fetchone()
    if latest is None:
        return None
    if config.history_months is None:
        return str(earliest)[:10]
    return (date.fromisoformat(str(latest)[:10]) - relativedelta(months=config.history_months)).isoformat()


def _covers_window(day: str, first_ts: Any, last_ts: Any, start_hhmm: str, end_hhmm: str, tolerance_min: int) -> bool:
    """True if the measurements of `day` reach both edges of the window (within the tolerance)."""
    if first_ts is None or last_ts is None:
        return False
    tol_s = tolerance_min * 60
    dt_start, dt_end = interval_bounds(day, start_hhmm, end_hhmm)
    left_ok = (_parse_pc_ts(first_ts) - dt_start).total_seconds() <= tol_s
    right_ok = (dt_end - _parse_pc_ts(last_ts)).total_seconds() <= tol_s
    return left_ok and right_ok


def _dates_with_full_window_coverage(con: Any, since: str, start_hhmm: str, end_hhmm: str, tolerance_min: int) -> set[str]:
    return {
        str(day)
        for day, first_ts, last_ts in con.execute(DAY_WINDOW_COVERAGE, (since, start_hhmm, end_hhmm)).fetchall()
        if day is not None and _covers_window(str(day), first_ts, last_ts, start_hhmm, end_hhmm, tolerance_min)
    }


def get_available_dates(con: Any, config: RTIConfig, since: str) -> list[str]:
    """Days on or after `since` with usable measurements (row-level path, e.g. SQLite)."""
    if not config.require_full_window_coverage:
        return [str(r[0]) for r in con.execute(DISTINCT_DAYS, (since,)).fetchall() if r[0] is not None]

    tol = config.coverage_edge_tolerance_min
    ref_days = _dates_with_full_window_coverage(con, since, config.ref_start_hhmm, config.ref_end_hhmm, tol)
    cur_days = _dates_with_full_window_coverage(con, since, config.cur_start_hhmm, config.cur_end_hhmm, tol)
    return sorted(ref_days & cur_days)


# --- Per-interval link statistics ---

def _link_stat(n_total: int, n_ok: int, rssi_location: float | None) -> dict[str, Any]:
    return {
        "n_total": n_total,
        "n_ok": n_ok,
        "ok_rate": n_ok / n_total if n_total else None,
        "rssi_mean": rssi_location,
    }


def _interval_meta(
    start_ts: str, end_ts: str, first_ts: str | None, last_ts: str | None,
    n_rows: int, n_ok_rows: int, n_rounds: int,
) -> IntervalMeta:
    return {
        "start_ts_requested": start_ts,
        "end_ts_requested": end_ts,
        "first_ts_found": first_ts,
        "last_ts_found": last_ts,
        "n_measurements": n_rows,
        "n_ok_measurements": n_ok_rows,
        "n_rounds": n_rounds,
    }


def load_stats_for_interval(con: Any, start_ts: str, end_ts: str, config: RTIConfig) -> tuple[LinkStats, IntervalMeta]:
    counts: dict[Pair, dict[str, Any]] = defaultdict(lambda: {"n_total": 0, "n_ok": 0, "rssi_ok": []})
    round_ids: set[int] = set()
    n_rows = n_ok_rows = 0
    first_ts = last_ts = None

    for sender_id, receiver_id, ok_match, rssi, round_id, pc_ts in con.execute(MEASUREMENT_IN_RANGE, (start_ts, end_ts)):
        n_rows += 1
        link = counts[pair_key(sender_id, receiver_id)]
        link["n_total"] += 1

        if round_id is not None:
            round_ids.add(int(round_id))
        if first_ts is None:
            first_ts = str(pc_ts)
        last_ts = str(pc_ts)

        if int(ok_match or 0) == 1 and rssi is not None:
            n_ok_rows += 1
            link["n_ok"] += 1
            link["rssi_ok"].append(float(rssi))

    location = np.median if config.use_median_rssi else np.mean
    stats: LinkStats = {
        pair: _link_stat(d["n_total"], d["n_ok"], float(location(d["rssi_ok"])) if d["rssi_ok"] else None)
        for pair, d in counts.items()
    }
    meta = _interval_meta(start_ts, end_ts, first_ts, last_ts, n_rows, n_ok_rows, len(round_ids))
    return stats, meta


class IntervalLoader:
    """Loads link statistics per (date, HH:MM window), memoizing the results.

    Providers with `server_side_stats` get every day's statistics up front from
    one LINK_DAY_STATS query per window (see `available_dates`); otherwise each
    interval is loaded on demand with its own short-lived connection.
    """

    def __init__(self, database: DatabaseProvider, config: RTIConfig) -> None:
        self.database = database
        self.config = config
        self._cache: dict[tuple[str, str], tuple[LinkStats, IntervalMeta]] = {}

    @staticmethod
    def _key(date_txt: str, start_hhmm: str, end_hhmm: str) -> tuple[str, str]:
        dt_start, dt_end = interval_bounds(date_txt, start_hhmm, end_hhmm)
        return dt_start.isoformat(timespec="seconds"), dt_end.isoformat(timespec="seconds")

    def _windows(self) -> tuple[tuple[str, str], tuple[str, str]]:
        cfg = self.config
        return (cfg.ref_start_hhmm, cfg.ref_end_hhmm), (cfg.cur_start_hhmm, cfg.cur_end_hhmm)

    def available_dates(self) -> list[str]:
        """Days with usable measurements within the last `config.history_months` months."""
        con = self.database.connect()
        try:
            since = history_start(con, self.config)
            if since is None:
                return []
            months = self.config.history_months
            logging.info("Using data since %s (%s).", since, "all data" if months is None else f"last {months} months")
            if not self.database.server_side_stats:
                return get_available_dates(con, self.config, since)
            ref_window, cur_window = self._windows()
            metas = {window: self._prefetch(con, since, *window) for window in {ref_window, cur_window}}
        finally:
            con.close()

        if self.config.require_full_window_coverage:
            tol = self.config.coverage_edge_tolerance_min
            usable = [
                {day for day, m in metas[w].items() if _covers_window(day, m["first_ts_found"], m["last_ts_found"], *w, tol)}
                for w in (ref_window, cur_window)
            ]
            dates = sorted(usable[0] & usable[1])
        else:
            dates = sorted(set(metas[ref_window]) | set(metas[cur_window]))

        # Days without rows in one window would otherwise fall back to a per-day query.
        for date_txt in dates:
            for window in (ref_window, cur_window):
                key = self._key(date_txt, *window)
                self._cache.setdefault(key, ({}, _interval_meta(*key, None, None, 0, 0, 0)))
        return dates

    def _prefetch(self, con: Any, since: str, start_hhmm: str, end_hhmm: str) -> dict[str, IntervalMeta]:
        """Caches the statistics of every day since `since` for one window; returns the metas by day."""
        logging.info("Aggregating link statistics on the server (%s–%s since %s)...", start_hhmm, end_hhmm, since)
        stats: dict[str, LinkStats] = defaultdict(dict)
        metas: dict[str, IntervalMeta] = {}

        rows = con.execute(LINK_DAY_STATS, (since, start_hhmm, end_hhmm)).fetchall()
        for day, a, b, is_day_total, n_total, n_ok, rssi_median, rssi_avg, first_ts, last_ts, n_rounds in rows:
            day = str(day)
            if is_day_total:
                key = self._key(day, start_hhmm, end_hhmm)
                metas[day] = _interval_meta(*key, str(first_ts), str(last_ts), int(n_total), int(n_ok), int(n_rounds))
            else:
                location = rssi_median if self.config.use_median_rssi else rssi_avg
                stats[day][pair_key(a, b)] = _link_stat(
                    int(n_total), int(n_ok), float(location) if n_ok and location is not None else None
                )

        for day, meta in metas.items():
            self._cache[self._key(day, start_hhmm, end_hhmm)] = (stats[day], meta)
        logging.info("Aggregated %d rows into %d days.", len(rows), len(metas))
        return metas

    def load(self, date_txt: str, start_hhmm: str, end_hhmm: str) -> tuple[LinkStats, IntervalMeta]:
        key = self._key(date_txt, start_hhmm, end_hhmm)

        if key not in self._cache:
            logging.debug("Loading interval %s – %s", *key)
            con = self.database.connect()
            try:
                self._cache[key] = load_stats_for_interval(con, *key, self.config)
            finally:
                con.close()
        return self._cache[key]

    def load_ref(self, date_txt: str) -> tuple[LinkStats, IntervalMeta]:
        return self.load(date_txt, self.config.ref_start_hhmm, self.config.ref_end_hhmm)

    def load_cur(self, date_txt: str) -> tuple[LinkStats, IntervalMeta]:
        return self.load(date_txt, self.config.cur_start_hhmm, self.config.cur_end_hhmm)


def build_y_vector_for_pairs(
    ref_stats: LinkStats,
    cur_stats: LinkStats,
    pairs: list[Pair],
    config: RTIConfig,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    """y = -(cur_rssi - ref_rssi) for every link with data in both intervals.

    Returns (y over valid links, validity mask over `pairs`, per-link details).
    """
    y_list: list[float] = []
    valid_mask: list[bool] = []
    details: list[dict[str, Any]] = []

    for pair in pairs:
        ref_rssi = ref_stats.get(pair, {}).get("rssi_mean")
        cur_rssi = cur_stats.get(pair, {}).get("rssi_mean")
        valid = ref_rssi is not None and cur_rssi is not None
        delta = cur_rssi - ref_rssi if valid else None
        y_val = -delta if valid else None

        details.append({
            "pair": pair,
            "ref_rssi": ref_rssi,
            "cur_rssi": cur_rssi,
            "delta": delta,
            "y": y_val,
            "ok_rate": cur_stats.get(pair, {}).get("ok_rate"),
            "pair_factor": pair_factor(pair, config),
            "is_outer": pair in config.outer_links,
            "valid": valid,
        })
        if valid:
            y_list.append(float(y_val))
        valid_mask.append(valid)

    return np.asarray(y_list, dtype=float), np.asarray(valid_mask, dtype=bool), details


# ==========================================================================
# Solver
# ==========================================================================

def assemble_blocks(
    W_list: list[np.ndarray],
    y_list: list[np.ndarray],
    Q: np.ndarray,
    times: list[datetime],
    alpha_ridge: float,
    alpha_smooth: float,
    alpha_time: float,
) -> tuple[list[np.ndarray], list[np.ndarray], list[float]]:
    """Normal-equation blocks of the objective documented in `solve_temporal`.

    Returns (diagonal blocks, right-hand sides, beta_t = alpha_time / dt_t
    coupling consecutive time steps).
    """
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

    betas: list[float] = []
    for t in range(1, len(times)):
        beta = alpha_time / elapsed_days(times[t - 1], times[t])
        betas.append(beta)
        diag_blocks[t - 1] = diag_blocks[t - 1] + beta * I
        diag_blocks[t] = diag_blocks[t] + beta * I

    return diag_blocks, rhs_blocks, betas


def solve_block_tridiagonal_segment(
    diag_blocks: list[np.ndarray],
    rhs_blocks: list[np.ndarray],
    betas_between: list[float],
) -> np.ndarray:
    """Block Thomas algorithm for off-diagonal blocks -beta_t * I."""
    n_blocks = len(diag_blocks)
    if n_blocks == 0:
        return np.empty((0, 0), dtype=float)
    if n_blocks == 1:
        return np.linalg.solve(diag_blocks[0], rhs_blocks[0]).reshape(1, -1)
    if len(betas_between) != n_blocks - 1:
        raise ValueError("Für ein Segment müssen genau n_blocks-1 beta-Werte vorliegen.")

    n_cells = diag_blocks[0].shape[0]
    I = np.eye(n_cells, dtype=float)

    c_blocks = [np.linalg.solve(diag_blocks[0], -betas_between[0] * I)]
    d_blocks = [np.linalg.solve(diag_blocks[0], rhs_blocks[0])]

    for t in range(1, n_blocks):
        lower_t = -betas_between[t - 1] * I
        denom = diag_blocks[t] - lower_t @ c_blocks[t - 1]
        rhs_mod = rhs_blocks[t] - lower_t @ d_blocks[t - 1]
        if t < n_blocks - 1:
            c_blocks.append(np.linalg.solve(denom, -betas_between[t] * I))
        d_blocks.append(np.linalg.solve(denom, rhs_mod))

    x_blocks = [np.zeros(n_cells, dtype=float) for _ in range(n_blocks)]
    x_blocks[-1] = d_blocks[-1]
    for t in range(n_blocks - 2, -1, -1):
        x_blocks[t] = d_blocks[t] - c_blocks[t] @ x_blocks[t + 1]

    return np.vstack(x_blocks)


def solve_temporal(
    W_list: list[np.ndarray],
    y_list: list[np.ndarray],
    Q: np.ndarray,
    times: list[datetime],
    reference_index: int | None,
    alpha_ridge: float,
    alpha_smooth: float,
    alpha_time: float,
) -> np.ndarray:
    """
    Solve the temporally regularized RTI problem over a sequence of time steps.

    Minimizes over all (non-reference) time steps t:

        sum_t || W_t x_t - y_t ||^2
      + alpha_ridge  * sum_t || x_t ||^2
      + alpha_smooth * sum_t x_t^T Q x_t
      + alpha_time   * sum_t (1 / dt_t) * || x_t - x_{t-1} ||^2

    x_t is the change relative to the reference day, so if `reference_index`
    is given the reference state is hard-anchored to x_ref = 0 and the parts
    before and after it are solved as two independent segments. With
    `reference_index=None` the whole sequence is solved as a single segment.

    Returns x of shape (T, n_cells).
    """
    T = len(W_list)
    if T == 0:
        raise ValueError("Keine Zeitpunkte für die zeitliche RTI vorhanden.")
    if len(y_list) != T or len(times) != T:
        raise ValueError("W_list, y_list und times müssen gleich lang sein.")

    diag, rhs, betas = assemble_blocks(W_list, y_list, Q, times, alpha_ridge, alpha_smooth, alpha_time)

    if reference_index is None:
        return solve_block_tridiagonal_segment(diag, rhs, betas)

    r = reference_index
    if not 0 <= r < T:
        raise ValueError("reference_index liegt außerhalb der Zeitfolge.")

    x_all = np.zeros((T, Q.shape[0]), dtype=float)
    if r > 0:
        x_all[:r] = solve_block_tridiagonal_segment(diag[:r], rhs[:r], betas[:r - 1])
    if r < T - 1:
        x_all[r + 1:] = solve_block_tridiagonal_segment(diag[r + 1:], rhs[r + 1:], betas[r + 1:])
    return x_all


# ==========================================================================
# Sequence
# ==========================================================================

@dataclass
class RTISequenceResult:
    ref_date: str
    ref_meta: dict[str, Any]
    sequence_dates: list[str]
    times: list[datetime]
    cur_meta_list: list[dict[str, Any]]
    details_list: list[list[dict[str, Any]]]
    valid_counts: list[int]
    dt_days_list: list[float]
    x_all: np.ndarray
    x_disp_all: np.ndarray
    mean_change: np.ndarray
    vmin: float
    vmax: float

    @property
    def raw_min(self) -> float:
        return float(np.min(self.x_all))

    @property
    def raw_max(self) -> float:
        return float(np.max(self.x_all))


def prepare_display_values(x: np.ndarray, config: RTIConfig) -> np.ndarray:
    x = x.copy()
    if config.positive_only_display:
        x[x < 0.0] = 0.0
    x[np.abs(x) < config.display_deadband_db] = 0.0
    return x


def global_symmetric_limits(x: np.ndarray) -> tuple[float, float]:
    vmax = max(float(np.max(np.abs(x))), 0.1)
    return -vmax, vmax


def compute_temporal_sequence(
    context: RTIModelContext,
    loader: IntervalLoader,
    ref_date: str,
    sequence_dates: list[str],
) -> RTISequenceResult:
    """Solve every day of `sequence_dates` relative to `ref_date`.

    The sequence is solved in independent windows of
    `config.temporal_smoothing_window_days`; only the window containing the
    reference day is anchored to x_ref = 0.
    """
    config = context.config
    ref_stats, ref_meta = loader.load_ref(ref_date)

    reference_index = sequence_dates.index(ref_date)
    T = len(sequence_dates)
    times = [interval_midpoint(d, config.cur_start_hhmm, config.cur_end_hhmm) for d in sequence_dates]
    x_all = np.zeros((T, context.Q.shape[0]), dtype=float)
    cur_meta_list: list[dict[str, Any]] = []
    details_list: list[list[dict[str, Any]]] = []
    valid_counts: list[int] = []

    window = config.temporal_smoothing_window_days
    logging.info("Solving temporal sequence for %s in chunks of up to %d days.", ref_date, window)

    for start in range(0, T, window):
        stop = min(start + window, T)
        chunk_W: list[np.ndarray] = []
        chunk_y: list[np.ndarray] = []

        for date_txt in sequence_dates[start:stop]:
            cur_stats, cur_meta = loader.load_cur(date_txt)
            y_t, valid_mask, details = build_y_vector_for_pairs(ref_stats, cur_stats, context.pairs, config)
            chunk_W.append(context.W[valid_mask, :])
            chunk_y.append(y_t)
            cur_meta_list.append(cur_meta)
            details_list.append(details)
            valid_counts.append(int(np.sum(valid_mask)))

        x_all[start:stop] = solve_temporal(
            chunk_W,
            chunk_y,
            context.Q,
            times[start:stop],
            reference_index=reference_index - start if start <= reference_index < stop else None,
            alpha_ridge=config.alpha_ridge,
            alpha_smooth=config.alpha_smooth,
            alpha_time=config.alpha_time,
        )

    vmin, vmax = global_symmetric_limits(x_all)

    return RTISequenceResult(
        ref_date=ref_date,
        ref_meta=ref_meta,
        sequence_dates=list(sequence_dates),
        times=times,
        cur_meta_list=cur_meta_list,
        details_list=details_list,
        valid_counts=valid_counts,
        dt_days_list=[elapsed_days(t0, t1) for t0, t1 in zip(times, times[1:])],
        x_all=x_all,
        x_disp_all=prepare_display_values(x_all, config),
        mean_change=(x_all @ context.cell_areas) / context.total_area,
        vmin=vmin,
        vmax=vmax,
    )


# ==========================================================================
# Cache
# ==========================================================================

def cache_path(config: RTIConfig, ref_date: str) -> Path:
    return config.sequence_cache_dir / f"{ref_date}.joblib"


def cached_dates(config: RTIConfig) -> list[str]:
    """Sorted reference dates that have a cache file."""
    return sorted(p.stem for p in config.sequence_cache_dir.glob("????-??-??.joblib"))


def cache_is_current(config: RTIConfig) -> bool:
    """True if the newest cached sequence is for today's date."""
    dates = cached_dates(config)
    return bool(dates) and dates[-1] == date.today().isoformat()


def load_cached_sequence(config: RTIConfig, ref_date: str) -> RTISequenceResult:
    return joblib.load(cache_path(config, ref_date))


def save_cached_sequence(config: RTIConfig, result: RTISequenceResult) -> Path:
    config.sequence_cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_path(config, result.ref_date)
    joblib.dump(result, path, compress=3)
    return path


def prune_cache(config: RTIConfig, keep_dates: list[str]) -> list[str]:
    """Delete cache files whose date is not in `keep_dates`; returns the removed dates."""
    stale = sorted(set(cached_dates(config)) - set(keep_dates))
    for ref_date in stale:
        cache_path(config, ref_date).unlink(missing_ok=True)
    return stale
