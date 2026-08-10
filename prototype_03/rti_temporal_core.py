#!/usr/bin/env python3
"""
Core logic for the dynamic temporal RTI view.

This module intentionally has no Tk or Matplotlib canvas dependencies. It owns
configuration, database aggregation, RTI model construction, and sequence
solving so the UI can stay focused on interaction.
"""

from __future__ import annotations

# Must happen before NumPy/OpenBLAS import.
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import logging
import sql

from config import DEFAULT_CONFIG, RTIConfig
from database import DatabaseProvider


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
    all_pairs: list[tuple[int, int]]
    pairs: list[tuple[int, int]]
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


def pair_key(a: int, b: int) -> tuple[int, int]:
    return tuple(sorted((int(a), int(b))))


def all_pairs(config: RTIConfig = DEFAULT_CONFIG) -> list[tuple[int, int]]:
    nodes = sorted(config.node_pos_m.keys())
    pairs: list[tuple[int, int]] = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            pairs.append((nodes[i], nodes[j]))
    return pairs


def filter_pairs_by_mode(
    pairs: list[tuple[int, int]],
    outer_links: set[tuple[int, int]] | frozenset[tuple[int, int]],
    mode: str,
) -> list[tuple[int, int]]:
    if mode == "exclude":
        return [p for p in pairs if p not in outer_links]
    if mode in ("keep", "downweight"):
        return pairs
    raise ValueError(f"Unbekannter OUTER_LINK_MODE: {mode}")


def pair_factor(pair: tuple[int, int], config: RTIConfig = DEFAULT_CONFIG) -> float:
    if pair not in config.outer_links:
        return 1.0

    if config.outer_link_mode == "keep":
        return 1.0
    if config.outer_link_mode == "downweight":
        return float(config.outer_link_factor)
    if config.outer_link_mode == "exclude":
        return 0.0

    raise ValueError(f"Unbekannter OUTER_LINK_MODE: {config.outer_link_mode}")


def robust_location(values: list[float], config: RTIConfig = DEFAULT_CONFIG) -> float | None:
    if not values:
        return None
    arr = np.asarray(values, dtype=float)
    if config.use_median_rssi:
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
    dt_start, dt_end = build_interval_datetimes(date_txt, start_txt, end_txt)
    return dt_start.isoformat(timespec="seconds"), dt_end.isoformat(timespec="seconds")


def interval_midpoint(date_txt: str, start_txt: str, end_txt: str) -> datetime:
    dt_start, dt_end = build_interval_datetimes(date_txt, start_txt, end_txt)
    return dt_start + (dt_end - dt_start) / 2


def interval_label(date_txt: str, start_txt: str, end_txt: str) -> str:
    return f"{date_txt} | {start_txt}–{end_txt}"


def elapsed_days(t0: datetime, t1: datetime) -> float:
    return max((t1 - t0).total_seconds() / 86400.0, 1e-9)


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
            cells.append(Cell(i=i, j=j, k=k, x0=x0, y0=y0, xc=xc, yc=yc, dx=cell_w, dy=cell_h))
            ij_to_k[(i, j)] = k

    return nx, ny, cells, ij_to_k


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
    config: RTIConfig = DEFAULT_CONFIG,
) -> np.ndarray:
    rows: list[np.ndarray] = []
    for a, b in pairs:
        row = build_link_row(
            cells=cells,
            sender_xy=config.node_pos_m[a],
            receiver_xy=config.node_pos_m[b],
            lambda_m=lambda_m,
            weight_mode=weight_mode,
            pair_scale=pair_factor((a, b), config),
        )
        rows.append(row)
    return np.vstack(rows)


def build_grid_laplacian(ij_to_k: dict[tuple[int, int], int]) -> np.ndarray:
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


def get_all_dates(con: Any) -> list[str]:
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
    con: Any,
    start_hhmm: str,
    end_hhmm: str,
    edge_tolerance_min: int,
) -> list[str]:
    rows = con.execute(
        """
        SELECT
            substr(pc_ts, 1, 10) AS day,
            MIN(pc_ts) AS first_ts,
            MAX(pc_ts) AS last_ts
        FROM measurement
        WHERE pc_ts IS NOT NULL
          AND substr(pc_ts, 12, 5) >= %s
          AND substr(pc_ts, 12, 5) <= %s
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


def get_available_dates(con: Any, config: RTIConfig = DEFAULT_CONFIG) -> list[str]:
    if not config.require_full_window_coverage:
        return get_all_dates(con)

    ref_days = set(
        get_dates_with_full_window_coverage(
            con,
            config.ref_start_hhmm,
            config.ref_end_hhmm,
            config.coverage_edge_tolerance_min,
        )
    )
    cur_days = set(
        get_dates_with_full_window_coverage(
            con,
            config.cur_start_hhmm,
            config.cur_end_hhmm,
            config.coverage_edge_tolerance_min,
        )
    )

    return sorted(ref_days & cur_days)


def finalize_day_tmp(
    day_tmp: dict[str, Any],
    start_ts: str,
    end_ts: str,
    config: RTIConfig = DEFAULT_CONFIG,
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[str, Any]]:
    out: dict[tuple[int, int], dict[str, Any]] = {}
    for pair, d in day_tmp["pairs"].items():
        out[pair] = {
            "n_total": d["n_total"],
            "n_ok": d["n_ok"],
            "ok_rate": d["n_ok"] / d["n_total"] if d["n_total"] else None,
            "rssi_mean": robust_location(d["rssi_ok"], config),
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


def _empty_day_tmp() -> dict[str, Any]:
    return {
        "pairs": defaultdict(lambda: {"n_total": 0, "n_ok": 0, "rssi_ok": []}),
        "round_ids": set(),
        "n_rows": 0,
        "n_ok_rows": 0,
        "first_ts": None,
        "last_ts": None,
    }


def load_stats_for_interval(
    con: Any,
    start_ts: str,
    end_ts: str,
    config: RTIConfig = DEFAULT_CONFIG,
) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[str, Any]]:
    query = sql.measurement_in_range(start_ts, end_ts)
    cursor = con.execute(query)

    day_tmp = _empty_day_tmp()

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

    return finalize_day_tmp(day_tmp, start_ts, end_ts, config)


def load_stats_for_many_days(
    con: Any,
    dates: list[str],
    start_hhmm: str,
    end_hhmm: str,
    config: RTIConfig = DEFAULT_CONFIG,
) -> dict[str, tuple[dict[tuple[int, int], dict[str, Any]], dict[str, Any]]]:
    if not dates:
        return {}

    first_start_ts, _ = build_interval_timestamps(min(dates), start_hhmm, end_hhmm)
    _, last_end_ts = build_interval_timestamps(max(dates), start_hhmm, end_hhmm)

    query = sql.measurement_for_many_days(first_start_ts, last_end_ts, start_hhmm, end_hhmm)
    logging.info("Executing query for %d days: %s", len(dates), query)
    cursor = con.execute(query)

    requested_dates = set(dates)
    tmp_by_day = {day: _empty_day_tmp() for day in dates}

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

    result: dict[str, tuple[dict[tuple[int, int], dict[str, Any]], dict[str, Any]]] = {}
    for day in dates:
        start_ts, end_ts = build_interval_timestamps(day, start_hhmm, end_hhmm)
        result[day] = finalize_day_tmp(tmp_by_day[day], start_ts, end_ts, config)

    return result


def build_y_vector_for_pairs(
    ref_stats: dict[tuple[int, int], dict[str, Any]],
    cur_stats: dict[tuple[int, int], dict[str, Any]],
    pairs: list[tuple[int, int]],
    config: RTIConfig = DEFAULT_CONFIG,
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    y_list: list[float] = []
    valid_mask: list[bool] = []
    details: list[dict[str, Any]] = []

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
                "pair_factor": pair_factor(pair, config),
                "is_outer": pair in config.outer_links,
                "valid": valid,
            }
        )

        if valid:
            y_list.append(float(y_val))
        valid_mask.append(valid)

    return np.asarray(y_list, dtype=float), np.asarray(valid_mask, dtype=bool), details


def solve_block_tridiagonal_segment(
    diag_blocks: list[np.ndarray],
    rhs_blocks: list[np.ndarray],
    betas_between: list[float],
) -> np.ndarray:
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
    Solve temporally regularized Real-Time Inverse (RTI) problems with hard zero-anchored reference.

    Formulates and solves the inverse problem across a time sequence where the reference 
    state is constrained to zero, enforcing spatial smoothness, ridge regularization, and 
    temporal continuity across non-reference time steps.

    Objective Function
    ------------------
    Minimizes the following objective for all non-reference time steps t:

        sum_t || W_t * x_t - y_t ||^2
      + alpha_ridge * sum_t || x_t ||^2
      + alpha_smooth * sum_t (x_t^T * Q * x_t)
      + alpha_time   * sum_t (1 / dt_t) * || x_t - x_{t-1} ||^2

    Parameters
    ----------
    y : list or ndarray
        Measured data/observation vectors y_t for each time step t.
    W : list or ndarray
        Weighting/system matrices W_t for each time step t.
    Q : ndarray
        Spatial regularization matrix (e.g., Laplacian) for spatial smoothness.
    ref_index : int
        Index corresponding to the reference time step t_ref.
    dt : list or ndarray
        Time intervals dt_t between consecutive time steps.
    alpha_ridge : float
        Regularization parameter for L2 ridge norm penalization.
    alpha_smooth : float
        Regularization parameter for spatial smoothing.
    alpha_time : float
        Regularization parameter for temporal continuity penalization.

    Returns
    -------
    x : ndarray
        Reconstructed time-series states x_t, with x[ref_index] strictly equal to zero.

    Notes
    -----
    Hard Zero Reference Anchor:
        The reference state is strictly constrained rather than penalised softly:
        
            x_ref = 0

        Because x_t represents change relative to the reference state, the change at the 
        reference point itself is zero by definition. The temporal sequence on both sides 
        (left and right) of the reference index is solved subject to this hard zero boundary condition.
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

        diag_blocks[t - 1] = diag_blocks[t - 1] + beta * I
        diag_blocks[t] = diag_blocks[t] + beta * I

    x_all = np.zeros((T, n_cells), dtype=float)

    if reference_index > 0:
        x_all[:reference_index, :] = solve_block_tridiagonal_segment(
            diag_blocks[:reference_index],
            rhs_blocks[:reference_index],
            betas[:max(0, reference_index - 1)],
        )

    x_all[reference_index, :] = 0.0

    if reference_index < T - 1:
        x_all[reference_index + 1:, :] = solve_block_tridiagonal_segment(
            diag_blocks[reference_index + 1:],
            rhs_blocks[reference_index + 1:],
            betas[reference_index + 1:],
        )

    return x_all, dt_days_list


def build_idw_operator(
    cells: list[Cell],
    nx_fine: int,
    ny_fine: int,
    width: float,
    height: float,
    power: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    pts = np.array([(c.xc, c.yc) for c in cells], dtype=np.float32)

    gx = np.linspace(0.0, width, nx_fine, dtype=np.float32)
    gy = np.linspace(0.0, height, ny_fine, dtype=np.float32)
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


def prepare_display_values(x_hat: np.ndarray, config: RTIConfig = DEFAULT_CONFIG) -> np.ndarray:
    x = x_hat.copy()

    if config.positive_only_display:
        x[x < 0.0] = 0.0

    x[np.abs(x) < config.display_deadband_db] = 0.0
    return x


def global_symmetric_limits(sequence_x_raw: np.ndarray) -> tuple[float, float]:
    vmax = max(abs(float(np.min(sequence_x_raw))), abs(float(np.max(sequence_x_raw))))
    vmax = max(vmax, 0.1)
    return -vmax, vmax


def build_model_context(config: RTIConfig = DEFAULT_CONFIG) -> RTIModelContext:
    pair_all = all_pairs(config)
    pairs = filter_pairs_by_mode(pair_all, config.outer_links, config.outer_link_mode)
    nx, ny, cells, ij_to_k = build_grid(config.container_w_m, config.container_h_m, config.dx, config.dy)
    W = build_w_matrix(cells, pairs, config.lambda_m, weight_mode=config.weight_mode, config=config)
    Q = build_grid_laplacian(ij_to_k)
    cell_areas = np.asarray([c.dx * c.dy for c in cells], dtype=float)
    total_area = float(np.sum(cell_areas))
    idw_X, idw_Y, idw_H = build_idw_operator(
        cells,
        config.fine_nx,
        config.fine_ny,
        config.container_w_m,
        config.container_h_m,
    )

    return RTIModelContext(
        config=config,
        all_pairs=pair_all,
        pairs=pairs,
        nx=nx,
        ny=ny,
        cells=cells,
        ij_to_k=ij_to_k,
        W=W,
        Q=Q,
        cell_areas=cell_areas,
        total_area=total_area,
        idw_X=idw_X,
        idw_Y=idw_Y,
        idw_H=idw_H,
    )


def load_interval_cached(
    date_txt: str,
    start_hhmm: str,
    end_hhmm: str,
    interval_cache: dict[tuple[str, str], tuple[dict, dict]],
    database: DatabaseProvider,
    config: RTIConfig,
) -> tuple[dict, dict]:
    start_ts, end_ts = build_interval_timestamps(date_txt, start_hhmm, end_hhmm)
    key = (start_ts, end_ts)

    if key in interval_cache:
        return interval_cache[key]

    con = database.connect()
    try:
        stats, meta = load_stats_for_interval(con, start_ts, end_ts, config)
    finally:
        con.close()

    interval_cache[key] = (stats, meta)
    return stats, meta


def ensure_many_intervals_cached(
    dates: list[str],
    start_hhmm: str,
    end_hhmm: str,
    interval_cache: dict[tuple[str, str], tuple[dict, dict]],
    database: DatabaseProvider,
    config: RTIConfig,
) -> None:
    missing_dates: list[str] = []
    for day in dates:
        start_ts, end_ts = build_interval_timestamps(day, start_hhmm, end_hhmm)
        if (start_ts, end_ts) not in interval_cache:
            missing_dates.append(day)

    if not missing_dates:
        return

    con = database.connect()
    try:
        loaded = load_stats_for_many_days(con, missing_dates, start_hhmm, end_hhmm, config)
    finally:
        con.close()

    for day, value in loaded.items():
        start_ts, end_ts = build_interval_timestamps(day, start_hhmm, end_hhmm)
        interval_cache[(start_ts, end_ts)] = value


def compute_temporal_sequence(
    context: RTIModelContext,
    ref_date: str,
    sequence_dates: list[str],
    interval_cache: dict[tuple[str, str], tuple[dict, dict]],
    database: DatabaseProvider,
    config: RTIConfig,
) -> RTISequenceResult:
    logging.info("Loading reference interval for %s", ref_date)
    ref_stats, ref_meta = load_interval_cached(
        ref_date,
        config.ref_start_hhmm,
        config.ref_end_hhmm,
        interval_cache,
        database,
        config,
    )
    logging.info("Checking interval cached %s", ref_date)
    ensure_many_intervals_cached(
        sequence_dates,
        config.cur_start_hhmm,
        config.cur_end_hhmm,
        interval_cache,
        database,
        config,
    )

    W_list: list[np.ndarray] = []
    y_list: list[np.ndarray] = []
    times: list[datetime] = []
    cur_meta_list: list[dict[str, Any]] = []
    details_list: list[list[dict[str, Any]]] = []
    valid_counts: list[int] = []

    for date_txt in sequence_dates:
        cur_stats, cur_meta = load_interval_cached(
            date_txt,
            config.cur_start_hhmm,
            config.cur_end_hhmm,
            interval_cache,
            database,
            config,
        )
        y_t, valid_mask_t, details_t = build_y_vector_for_pairs(ref_stats, cur_stats, context.pairs, config)
        W_t = context.W[valid_mask_t, :]

        W_list.append(W_t)
        y_list.append(y_t)
        times.append(interval_midpoint(date_txt, config.cur_start_hhmm, config.cur_end_hhmm))
        cur_meta_list.append(cur_meta)
        details_list.append(details_t)
        valid_counts.append(int(np.sum(valid_mask_t)))

    reference_index = sequence_dates.index(ref_date)
    x_all, dt_days_list = solve_temporal_smooth_rti_block_tridiagonal(
        W_list=W_list,
        y_list=y_list,
        Q=context.Q,
        times=times,
        reference_index=reference_index,
        alpha_ridge=config.alpha_ridge,
        alpha_smooth=config.alpha_smooth,
        alpha_time=config.alpha_time,
    )

    x_disp_all = np.vstack([prepare_display_values(row, config) for row in x_all])
    vmin, vmax = global_symmetric_limits(x_all)
    mean_change = (x_all @ context.cell_areas) / context.total_area

    return RTISequenceResult(
        ref_date=ref_date,
        ref_meta=ref_meta,
        sequence_dates=list(sequence_dates),
        times=times,
        cur_meta_list=cur_meta_list,
        details_list=details_list,
        valid_counts=valid_counts,
        dt_days_list=dt_days_list,
        x_all=x_all,
        x_disp_all=x_disp_all,
        mean_change=mean_change,
        vmin=vmin,
        vmax=vmax,
    )
