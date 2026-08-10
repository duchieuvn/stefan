# Stefan Dynamische RTI

Radio Tomographic Imaging (RTI) system for monitoring moisture/attenuation
changes inside a container using RSSI measurements between fixed LoRa sensor
nodes. Node-to-node signal attenuation is combined with a spatially and
temporally regularized reconstruction to produce a 2D map of change over
time.

## Issues

```
2026-08-09 22:00:54 [INFO    ] Checking interval cached 2026-02-27
2026-08-09 22:00:54 [INFO    ] Executing query for 163 days:
        SELECT sender_id, receiver_id, ok_match, rssi, round_id, pc_ts
        FROM measurement
        WHERE pc_ts IS NOT NULL
            AND pc_ts >= '2026-02-28T00:00:00'
            AND pc_ts <= '2026-08-09T23:59:59'
            AND substr(pc_ts, 12, 5) >= '00:00'
            AND substr(pc_ts, 12, 5) <= '23:59'
            AND sender_id IS NOT NULL
            AND receiver_id IS NOT NULL
            AND sender_id != receiver_id
        ORDER BY pc_ts ASC

```

## Repo layout

- **`SOFTWARE/`** — data processing, reconstruction, and visualization.
  - `app.py` — Flask web app: browser-based viewer for the dynamic RTI
    reconstruction (see below).
  - `rti_10.py` — the original Tkinter desktop viewer (same reconstruction,
    native GUI instead of a browser).
  - `rti/rti_temporal_core.py` — DB access, RTI model construction, and the
    spatial + temporal regularized solve. No UI dependencies. This module contains the core logic for the dynamic temporal RTI view.

    ### `compute_temporal_sequence` Function Explained

    The `compute_temporal_sequence` function is a central component for generating the time-series of Radio Tomographic Imaging (RTI) maps. Its primary purpose is to calculate how attenuation (and thus, often moisture) changes over a sequence of days relative to a chosen reference day, incorporating both spatial and temporal regularization.

    #### Function Signature

    ```python
    def compute_temporal_sequence(
        context: RTIModelContext,
        ref_date: str,
        sequence_dates: list[str],
        interval_cache: dict[tuple[str, str], tuple[dict, dict]],
        database: DatabaseProvider,
        config: RTIConfig,
    ) -> RTISequenceResult:
    ```

    #### Parameters
    - `context: RTIModelContext`: An object containing the pre-built RTI model components, such as the grid definition (`nx`, `ny`, `cells`), the `W` matrix (forward model relating cell attenuation to link attenuation), and the `Q` matrix (spatial Laplacian for smoothing). This context is built once and reused.
    - `ref_date: str`: The ISO-formatted date string (e.g., "YYYY-MM-DD") that serves as the reference point for all attenuation change calculations. The RTI maps will show changes relative to this day.
    - `sequence_dates: list[str]`: A list of ISO-formatted date strings representing the entire temporal sequence for which RTI maps are to be computed. This list typically includes the `ref_date` itself.
    - `interval_cache: dict[tuple[str, str], tuple[dict, dict]]`: An in-memory cache that stores pre-loaded statistics for specific time intervals (defined by `(start_timestamp, end_timestamp)`). This prevents redundant database queries for the same daily intervals.
    - `database: DatabaseProvider`: An object providing a database connection interface (e.g., SQLite or PostgreSQL).
    - `config: RTIConfig`: A configuration object containing various parameters for the RTI model, such as regularization weights (`alpha_ridge`, `alpha_smooth`, `alpha_time`), time window definitions (`ref_start_hhmm`, `cur_start_hhmm`), and display settings.

    #### Internal Logic (Step-by-Step)
    1.  **Load Reference Day Statistics**:
        It first loads the aggregated RSSI statistics for the `ref_date` using `load_interval_cached`. This function retrieves data for the time window specified by `config.ref_start_hhmm` and `config.ref_end_hhmm`.
        The `ref_stats` (a dictionary mapping link pairs to their RSSI means and other metrics) and `ref_meta` (metadata about the interval) are stored.
    2.  **Ensure All Sequence Day Statistics are Cached**:
        It then calls `ensure_many_intervals_cached` to pre-load or ensure that the statistics for all dates in `sequence_dates` are available in the `interval_cache`. This uses the time window defined by `config.cur_start_hhmm` and `config.cur_end_hhmm`. This step is crucial for efficiency, especially when `sequence_dates` is long, as it fetches data for multiple days in a single batched database query if possible.
    3.  **Prepare Data for Each Day in the Sequence**:
        It initializes several empty lists: `W_list`, `y_list`, `times`, `cur_meta_list`, `details_list`, and `valid_counts`. These lists will store data for each day in the `sequence_dates`.
        It iterates through each `date_txt` in `sequence_dates`:
        - Loads the current day's statistics (`cur_stats`, `cur_meta`) using `load_interval_cached`.
        - Calls `build_y_vector_for_pairs` to compute the `y` vector for the current day. This vector represents the observed change in RSSI for each link, calculated as `-(current_RSSI - reference_RSSI)`. It also returns a `valid_mask` indicating which links had valid RSSI measurements for both the reference and current day, and details about each link's contribution.
        - Filters the pre-built `context.W` matrix using the `valid_mask_t` to create `W_t`, which only includes rows corresponding to valid links for the current day.
        - Appends `W_t`, `y_t`, the midpoint datetime of the current day's interval, `cur_meta`, `details_t`, and the count of valid links to their respective lists.
    4.  **Solve the Temporal RTI Problem**:
        It determines the `reference_index` of `ref_date` within `sequence_dates`.
        It calls `solve_temporal_smooth_rti_block_tridiagonal`. This is the core numerical solver. It takes the `W_list`, `y_list`, `context.Q` (spatial Laplacian), `times`, `reference_index`, and regularization parameters (`alpha_ridge`, `alpha_smooth`, `alpha_time`) as input.
        This solver constructs and solves a block-tridiagonal system of equations that simultaneously estimates the attenuation change (`x`) for all cells across all time steps in the sequence. It enforces spatial smoothness (via `Q`), L2 regularization (ridge), and temporal continuity (penalizing large changes between adjacent time steps, weighted by `alpha_time` and the time difference `dt_days`). Crucially, it hard-constrains the `x` vector for the `ref_date` to be zero, as `x` represents change relative to the reference.
        The solver returns `x_all` (a NumPy array where each row is the reconstructed attenuation change for all cells for a given day) and `dt_days_list` (time differences between consecutive days).
    5.  **Post-Processing and Display Preparation**:
        - `x_disp_all` is created by applying display-specific transformations to `x_all` (e.g., setting negative values to zero if `positive_only_display` is true, or applying a deadband `display_deadband_db`).
        - `vmin` and `vmax` are calculated using `global_symmetric_limits` to ensure a symmetric color scale around zero for visualization.
        - `mean_change` is computed by taking a weighted average of `x_all` over the cell areas, providing a single scalar value representing the overall change for each day.

    #### Return Value

    The function returns an `RTISequenceResult` dataclass instance, which encapsulates all the computed results:
    - `ref_date`, `ref_meta`: Information about the reference day.
    - `sequence_dates`, `times`: The dates and corresponding datetime objects for the entire sequence.
    - `cur_meta_list`, `details_list`, `valid_counts`: Metadata and detailed link-level information for each day in the sequence.
    - `dt_days_list`: Time differences between consecutive days.
    - `x_all`: The raw, un-transformed reconstructed attenuation change for all cells across all days.
    - `x_disp_all`: The attenuation change prepared for display (e.g., with deadband or positive-only filtering).
    - `mean_change`: The average attenuation change over the entire grid for each day.
    - `vmin`, `vmax`: The symmetric minimum and maximum values for visualization.
    - `raw_min`, `raw_max`: The absolute minimum and maximum values in `x_all`.

    In essence, `compute_temporal_sequence` orchestrates the entire process from raw database measurements to a fully solved and display-ready time-series of RTI attenuation change maps, leveraging caching and a sophisticated block-tridiagonal solver for efficiency and accuracy.

  - `rti/rti_temporal_visualization.py` — Matplotlib drawing for the 4-panel
    RTI figure. No UI dependencies.
  - `config.py` — `RTIConfig` / `PlotAllConfig`, all tunable parameters
    (grid resolution, regularization weights, node positions, time windows).
  - `plot_all_safe.py`, `plot_all/` — batch plotting of raw link/BME sensor
    data to static HTML/PNG reports.
  - `rti_live_demo_motion_smooth.py` — kiosk-style animated live view for an
    external display.
  - `reduce_database.py` — downsample `sensor_data.db` by keeping every Nth
    measurement round.
  - `master/lora_logger_03.py` — logs incoming LoRa/Modbus measurements into
    `sensor_data.db`.
  - `sensor_data.db` — SQLite database of raw measurements (`measurement`
    table: `sender_id`, `receiver_id`, `rssi`, `ok_match`, `pc_ts`, ...).

- **`electronic_black_box/`** — embedded firmware (`heltecv3.2_code_reichweite/`
  sender/receiver) and the master-side Modbus collector (`master/`) for the
  physical sensor hardware.

  ### Why `solve_temporal_smooth_rti_block_tridiagonal` Cannot Solve Independently

  The `solve_temporal_smooth_rti_block_tridiagonal` function is designed to _not_ solve the attenuation maps for each day independently. This is a fundamental aspect of its design, driven by the inclusion of a **temporal regularization term** in its underlying objective function. This term explicitly links the solution of one day to the solutions of its adjacent days, ensuring a smooth evolution of the reconstructed attenuation maps over time.

  #### The Core Concept: Temporal Coupling

  If the system were to solve for each day independently, the objective for a single day `t` would typically be to find the attenuation map `x_t` that minimizes:

  ```
  || W_t * x_t - y_t ||^2   +   alpha_ridge * || x_t ||^2   +   alpha_smooth * (x_t^T * Q * x_t)
  ```

  This independent objective considers:
  1.  **Data Fidelity:** How well the map `x_t` explains the measured RSSI changes `y_t` for that specific day.
  2.  **Ridge Regularization:** A penalty to keep the overall magnitude of attenuation values small, preventing overfitting.
  3.  **Spatial Smoothness:** A penalty (where `Q` is a spatial Laplacian matrix) to ensure the map `x_t` is smooth and avoids noisy, abrupt changes within the spatial domain.

  However, `solve_temporal_smooth_rti_block_tridiagonal` introduces a crucial fourth component to the _global_ objective function, which it minimizes across _all_ days simultaneously:

  ```
  ... + alpha_time * sum_t (1 / dt_t) * || x_t - x_{t-1} ||^2
  ```

  This is the **temporal regularization term**.

  #### What This Term Does
  1.  **Links Consecutive Days:** This term directly penalizes the difference between the attenuation map for day `t` (`x_t`) and the map for the previous day `t-1` (`x_{t-1}`). This means the optimal solution for day `t` is no longer independent; it is influenced by and influences the solution for day `t-1` (and `t+1`).
  2.  **Enforces Temporal Smoothness:** By minimizing this term, the solver is encouraged to find a sequence of maps that change smoothly and gradually over time. This is based on the assumption that the physical phenomenon being measured (e.g., moisture content) typically evolves continuously rather than jumping erratically from one day to the next. This helps to filter out day-to-day measurement noise and produce a more physically plausible time-series.
  3.  **Creates a Single, Large Problem:** Because each day's solution (except the first and last in the sequence) is coupled to its neighbors, it becomes impossible to solve for them one by one. The entire sequence of maps (`x_all`) must be solved as a single, large, interconnected system of equations.

  #### How the Implementation Reflects This

  The function's name, `solve_temporal_smooth_rti_block_tridiagonal`, directly reflects this approach:
  - The problem is structured as a large system of linear equations whose matrix has a **block-tridiagonal** form.
  - Each "block" on the main diagonal corresponds to the terms related to a single day's independent problem (data fidelity, ridge, spatial smoothness).
  - The temporal regularization term creates smaller "off-diagonal" blocks that couple each day's equations to its immediate neighbors.
  - The `solve_block_tridiagonal_segment` helper function is specifically designed to efficiently solve such a structured system, finding the global optimum that balances all regularization terms across the entire time series.

  Furthermore, the function includes a **hard zero reference anchor**: the `x` vector for the `ref_date` is strictly constrained to be zero. This is because `x` represents the _change_ relative to the reference, and by definition, the change at the reference point itself is zero. This hard constraint further integrates the reference day into the global solution, affecting the solutions for all other days.

  In summary, the primary purpose of `solve_temporal_smooth_rti_block_tridiagonal` is to _avoid_ independent daily solutions. By solving the entire sequence simultaneously with temporal regularization, it produces a more coherent, stable, and physically meaningful time-series of RTI reconstructions.

CREATE USER holzofen WITH PASSWORD 'biofilter';
CREATE DATABASE sensor_data OWNER holzofen;
