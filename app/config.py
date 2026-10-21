from dataclasses import dataclass, field
from pathlib import Path

WEIGHT_MODES = ("uniform_normalized", "uniform_binary", "paper")
OUTER_LINK_MODES = ("keep", "downweight", "exclude")


@dataclass(frozen=True)
class RTIConfig:
    db_path: Path = Path("reduced_data.db")
    db_max_age_hours: float = 8.0
    sequence_cache_dir: Path = Path("sequence_cache")
    history_months: int | None = 6  # None = all data from the first measurement

    dx: float = 0.4
    dy: float = 0.4
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
    outer_link_mode: str = "exclude"  # one of OUTER_LINK_MODES
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

    # DWD Climate Data Center station for the weather panels (5-digit Stations_ID).
    # 00151 = Amberg-Unterammersricht (3 km from Amberg). None = no weather is fetched.
    weather_dwd_station: str | None = "00151"

    def __post_init__(self) -> None:
        if self.weight_mode not in WEIGHT_MODES:
            raise ValueError(f"Unbekannter weight_mode: {self.weight_mode}")
        if self.outer_link_mode not in OUTER_LINK_MODES:
            raise ValueError(f"Unbekannter OUTER_LINK_MODE: {self.outer_link_mode}")


DEFAULT_CONFIG = RTIConfig()
