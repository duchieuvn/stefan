from dataclasses import dataclass, field
from pathlib import Path

db_path = Path("reduced_data.db")


@dataclass(frozen=True)
class RTIConfig:
    db_path: Path = db_path

    dx: float = 0.4
    dy: float = 0.4
    sequence_cache_dir: Path = Path("sequence_cache")
    lambda_m: float = 0.5
    weight_mode: str = "paper"

    alpha_ridge: float = 0.01
    alpha_smooth: float = 4.0
    alpha_time: float = 0.05
    temporal_smoothing_window_days: int = 14

    display_deadband_db: float = 0.0
    fine_nx: int = 120
    fine_ny: int = 190
    contour_levels: int = 14
    positive_only_display: bool = False
    rti_cmap: str = "RdBu"

    use_median_rssi: bool = True

    ref_start_hhmm: str = "00:00"
    ref_end_hhmm: str = "23:59"
    cur_start_hhmm: str = "00:00"
    cur_end_hhmm: str = "23:59"

    require_full_window_coverage: bool = False
    coverage_edge_tolerance_min: int = 30

    outer_links: frozenset[tuple[int, int]] = frozenset({
        (1, 2), (2, 3), (1, 3),
        (4, 5), (5, 6), (4, 6),
        (1, 4), (1, 5), (1, 6),
        (2, 4), (2, 5), (2, 6),
        (3, 4), (3, 5), (3, 6),
    })
    outer_link_mode: str = "exclude"
    # outer_link_mode: str = "downweight"
    outer_link_factor: float = 0.3

    container_w_m: float = 2.390
    container_h_m: float = 4.020
    node_pos_m: dict[int, tuple[float, float]] = field(default_factory=lambda: {
        1: (2.390, 1.075),
        2: (2.390, 2.205),
        3: (2.390, 3.335),
        4: (0.000, 3.335),
        5: (0.000, 2.205),
        6: (0.000, 1.075),
        7: (1.180, 1.300),
        8: (1.245, 2.720),
    })


@dataclass(frozen=True)
class PlotAllConfig:
    db_path: Path = db_path
    out_dir: Path = Path("out_plots")
    dpi: int = 140

    use_15min: bool = False
    nodes: str = "1,2,3,4,5,6,7,8"
    discover_links: bool = False
    create_index: bool = False

    bme_nodes: str = "5,6,7,8"
    bme_smooth_hours: int = 6
    bme_hide_raw1h: bool = False

    start: str | None = None
    end: str | None = None


DEFAULT_CONFIG = RTIConfig()
DEFAULT_PLOT_CONFIG = PlotAllConfig()

__all__ = ["DEFAULT_CONFIG", "DEFAULT_PLOT_CONFIG", "PlotAllConfig", "RTIConfig"]
