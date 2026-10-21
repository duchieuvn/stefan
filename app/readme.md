# Stefan Dynamische RTI

Radio Tomographic Imaging (RTI) system for monitoring moisture/attenuation
changes inside a container using RSSI measurements between fixed LoRa sensor
nodes. Node-to-node signal attenuation is combined with a spatially and
temporally regularized reconstruction to produce a 2D map of change over
time.

## `compute_temporal_sequence` Function Explained

The `compute_temporal_sequence` function is a central component for generating the time-series of Radio Tomographic Imaging (RTI) maps. Its primary purpose is to calculate how attenuation (and thus, often moisture) changes over a sequence of days relative to a chosen reference day, incorporating both spatial and temporal regularization.

### Function Signature

```python
def compute_temporal_sequence(
    context: RTIModelContext,
    loader: IntervalLoader,
    ref_date: str,
    sequence_dates: list[str],
) -> RTISequenceResult:
```

### Parameters

- `context: RTIModelContext`: An object containing the pre-built RTI model components, such as the grid definition (`nx`, `ny`, `cells`), the `W` matrix (forward model relating cell attenuation to link attenuation), and the `Q` matrix (spatial Laplacian for smoothing). This context is built once and reused. Its `config: RTIConfig` holds all parameters, such as regularization weights (`alpha_ridge`, `alpha_smooth`, `alpha_time`), time window definitions (`ref_start_hhmm`, `cur_start_hhmm`), and display settings.
- `loader: IntervalLoader`: Loads the aggregated link statistics for a date and time window from the database (`DatabaseProvider`, SQLite or PostgreSQL) and memoizes them, so the same daily interval is never queried twice across reference dates.
- `ref_date: str`: The ISO-formatted date string (e.g., "YYYY-MM-DD") that serves as the reference point for all attenuation change calculations. The RTI maps will show changes relative to this day.
- `sequence_dates: list[str]`: A list of ISO-formatted date strings representing the entire temporal sequence for which RTI maps are to be computed. This list typically includes the `ref_date` itself.

### Internal Logic (Step-by-Step)

1.  **Load Reference Day Statistics**:
    It first loads the aggregated RSSI statistics for the `ref_date` using `loader.load_ref`. This retrieves data for the time window specified by `config.ref_start_hhmm` and `config.ref_end_hhmm`.
    The `ref_stats` (a dictionary mapping link pairs to their RSSI means and other metrics) and `ref_meta` (metadata about the interval) are stored.
2.  **Prepare Data for Each Day in the Sequence**:
    It initializes several empty lists: `W_list`, `y_list`, `times`, `cur_meta_list`, `details_list`, and `valid_counts`. These lists will store data for each day in the `sequence_dates`.
    It iterates through each `date_txt` in `sequence_dates`:
    - Loads the current day's statistics (`cur_stats`, `cur_meta`) using `loader.load_cur` (time window `config.cur_start_hhmm`–`config.cur_end_hhmm`).
    - Calls `build_y_vector_for_pairs` to compute the `y` vector for the current day. This vector represents the observed change in RSSI for each link, calculated as `-(current_RSSI - reference_RSSI)`. It also returns a `valid_mask` indicating which links had valid RSSI measurements for both the reference and current day, and details about each link's contribution.
    - Filters the pre-built `context.W` matrix using the `valid_mask_t` to create `W_t`, which only includes rows corresponding to valid links for the current day.
    - Appends `W_t`, `y_t`, the midpoint datetime of the current day's interval, `cur_meta`, `details_t`, and the count of valid links to their respective lists.
3.  **Solve the Temporal RTI Problem**:
    It determines the `reference_index` of `ref_date` within `sequence_dates`.
    It calls `solve_temporal`. This is the core numerical solver. It takes the `W_list`, `y_list`, `context.Q` (spatial Laplacian), `times`, `reference_index`, and regularization parameters (`alpha_ridge`, `alpha_smooth`, `alpha_time`) as input.
    This solver constructs and solves a block-tridiagonal system of equations that simultaneously estimates the attenuation change (`x`) for all cells across all time steps in the sequence. It enforces spatial smoothness (via `Q`), L2 regularization (ridge), and temporal continuity (penalizing large changes between adjacent time steps, weighted by `alpha_time` and the time difference `dt_days`). Crucially, it hard-constrains the `x` vector for the `ref_date` to be zero, as `x` represents change relative to the reference.
    The solver returns `x_all`, a NumPy array where each row is the reconstructed attenuation change for all cells for a given day.
4.  **Post-Processing and Display Preparation**:
    - `x_disp_all` is created by applying display-specific transformations to `x_all` (e.g., setting negative values to zero if `positive_only_display` is true, or applying a deadband `display_deadband_db`).
    - `vmin` and `vmax` are calculated using `global_symmetric_limits` to ensure a symmetric color scale around zero for visualization.
    - `mean_change` is computed by taking a weighted average of `x_all` over the cell areas, providing a single scalar value representing the overall change for each day.

### Return Value

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

### Windowed Temporal Smoothing for Long Sequences

To enhance performance and robustness when dealing with very long time sequences (e.g., many months of data), the system employs a **windowed temporal smoothing** strategy. Instead of solving the entire sequence as one massive problem, it breaks the sequence into smaller, manageable chunks and solves each one independently.

This behavior is controlled by the `temporal_smoothing_window_days` parameter in `config.py`.

#### How It Works

The `compute_temporal_sequence` function implements the following logic:

1.  **Chunk Iteration**: It no longer loads data for the entire `sequence_dates` list at once. Instead, it iterates through the list in chunks, with the size of each chunk determined by `temporal_smoothing_window_days` (e.g., 14 days).

2.  **Just-in-Time Data Loading**: For each chunk, it loads measurement data via the `IntervalLoader` _only for the dates within that specific chunk_. This "just-in-time" approach avoids the initial large, slow database query and significantly reduces peak memory usage.

3.  **Independent Chunk Solving**: Each chunk is treated as a self-contained temporal RTI problem.
    - The `y` vectors (observed RSSI changes) for each day in the chunk are still calculated relative to the single, global `ref_date`.
    - Temporal smoothing (`alpha_time`) is applied _only between the days within the chunk_. There is no temporal link between the last day of one chunk and the first day of the next.

4.  **Handling the Reference Date**:
    - **If a chunk contains the global `ref_date`**: `solve_temporal` is called with the reference day's position within that chunk and applies the hard `x_ref = 0` constraint there.
    - **If a chunk does _not_ contain the `ref_date`**: `solve_temporal` is called with `reference_index=None` and solves the chunk as a single "floating" segment. Since the `y` vectors are already relative to the global reference, the resulting `x` (attenuation change) vectors are correctly scaled without needing a local zero anchor.

5.  **Assembling the Final Result**: The solved `x_chunk` from each iteration is placed into its corresponding position in the final `x_all` array, creating the complete time-series solution.

#### Benefits of the Windowed Approach

- **Performance**: By loading and processing data in small, fixed-size chunks, the system avoids the performance degradation associated with very long time sequences. Database queries are small and fast, and memory usage is kept low.
- **Scalability**: The application can now process sequences of arbitrary length (e.g., years of data) without running into memory or performance bottlenecks.
- **Robustness**: The impact of data anomalies or measurement gaps is localized to their specific window. A problem in one chunk does not affect the solutions in other chunks, preventing errors from propagating across the entire time-series.

#### Caching and Incremental Updates

It is important to understand that the `sequence_cache/` directory, populated by `precompute_sequences.py`, stores **final, complete reports** for a static historical period. These cached sequences are **not incrementally updatable**.

When a new day of data becomes available, you cannot simply calculate the solution for that new day and append it to a cached sequence. This is due to the nature of the temporal coupling in the solver:

- **Two-Way Dependency**: The temporal smoothing term `|| x_t - x_{t-1} ||^2` creates a two-way dependency. The solution for day `t` (`x_t`) is influenced by the solution from the previous day (`x_{t-1}`), but it also influences the solution for the next day (`x_{t+1}`) because `x_t` appears in the penalty term `|| x_{t+1} - x_t ||^2`.

- **Chain Reaction**: When a new day `D+1` is added to the sequence, it changes the optimization problem for the previous day `D`, as `x_D` is now pulled by both `x_{D-1}` and `x_{D+1}`. This effect ripples backward through the entire sliding window.

- **Re-solving is Required**: Consequently, to incorporate a new day's data, the entire final window (e.g., the last 14 days) must be re-solved from scratch. The previously calculated solution for that window is no longer valid.

To get updated sequences that include new data, the `precompute_sequences.py` script must be re-run. This will generate new, complete `RTISequenceResult` files that incorporate the latest available data.

## Module layout (`app/`)

Run everything from inside `app/` (paths such as `.env`, `sequence_cache/` and
`reduced_data_*.db` are relative to the working directory).

- `main.py` — Tk viewer (reference-day and sequence sliders). Runs the
  pre-computation behind a splash screen when the cache is not from today.
- `precompute_sequences.py` — CLI (`--db sqlite|postgres|pg-tailscale`,
  default `RTI_DB_BACKEND` from `.env`) that computes one sequence per
  reference day into `sequence_cache/`. With PostgreSQL, all per-day link
  statistics come from one server-side aggregate (`LINK_DAY_STATS`, ~2.5k rows
  for 3 months) that uses the covering index
  `measurement_pc_ts_idx ON measurement (pc_ts) INCLUDE (sender_id, receiver_id, ok_match, rssi, round_id)`.
- `config.py` — `RTIConfig`, all tunable parameters (grid resolution,
  regularization weights, node positions, time windows, history length).
- `rti_temporal_core.py` — all numerics, no UI: SQL queries, model (grid,
  `W`, Laplacian `Q`, IDW operator), per-link RSSI statistics
  (`IntervalLoader`), the temporal solver (`solve_temporal`),
  `compute_temporal_sequence` and the on-disk cache helpers.
- `rti_temporal_visualization.py` — Matplotlib drawing for the 4-panel figure (no Tk).
- `weather.py` — daily temperature, precipitation and relative humidity from
  the DWD Climate Data Center (opendata.dwd.de, no API key) for the station
  `weather_dwd_station` in `config.py` (default 00151 Amberg-Unterammersricht):
  hourly `recent` files (~500 days up to yesterday), `historical` files for
  older days and the 10-minute `now` files for today. DWD timestamps are UTC;
  they are converted to local time and aggregated over the map time window
  (`cur_start_hhmm`–`cur_end_hhmm`). Fetched in the background by `main.py` and
  drawn as a third figure row below the time series.
- `database.py` — `.env` settings, SQLite / PostgreSQL providers
  (`open_database()`, `default_backend()`), and the SFTP sync of the local
  `reduced_data_*.db` (used only by the `sqlite` fallback backend).

## Why `solve_temporal` Cannot Solve Independently

The `solve_temporal` function is designed to _not_ solve the attenuation maps for each day independently. This is a fundamental aspect of its design, driven by the inclusion of a **temporal regularization term** in its underlying objective function. This term explicitly links the solution of one day to the solutions of its adjacent days, ensuring a smooth evolution of the reconstructed attenuation maps over time.

### The Core Concept: Temporal Coupling

If the system were to solve for each day independently, the objective for a single day `t` would typically be to find the attenuation map `x_t` that minimizes:

```
|| W_t * x_t - y_t ||^2   +   alpha_ridge * || x_t ||^2   +   alpha_smooth * (x_t^T * Q * x_t)
```

This independent objective considers:

1.  **Data Fidelity:** How well the map `x_t` explains the measured RSSI changes `y_t` for that specific day.
2.  **Ridge Regularization:** A penalty to keep the overall magnitude of attenuation values small, preventing overfitting.
3.  **Spatial Smoothness:** A penalty (where `Q` is a spatial Laplacian matrix) to ensure the map `x_t` is smooth and avoids noisy, abrupt changes within the spatial domain.

However, `solve_temporal` introduces a crucial fourth component to the _global_ objective function, which it minimizes across _all_ days simultaneously:

```
... + alpha_time * sum_t (1 / dt_t) * || x_t - x_{t-1} ||^2
```

This is the **temporal regularization term**.

### What This Term Does

1.  **Links Consecutive Days:** This term directly penalizes the difference between the attenuation map for day `t` (`x_t`) and the map for the previous day `t-1` (`x_{t-1}`). This means the optimal solution for day `t` is no longer independent; it is influenced by and influences the solution for day `t-1` (and `t+1`).
2.  **Enforces Temporal Smoothness:** By minimizing this term, the solver is encouraged to find a sequence of maps that change smoothly and gradually over time. This is based on the assumption that the physical phenomenon being measured (e.g., moisture content) typically evolves continuously rather than jumping erratically from one day to the next. This helps to filter out day-to-day measurement noise and produce a more physically plausible time-series.
3.  **Creates a Single, Large Problem:** Because each day's solution (except the first and last in the sequence) is coupled to its neighbors, it becomes impossible to solve for them one by one. The entire sequence of maps (`x_all`) must be solved as a single, large, interconnected system of equations.

### How the Implementation Reflects This

The function's name, `solve_temporal`, directly reflects this approach:

- The problem is structured as a large system of linear equations whose matrix has a **block-tridiagonal** form.
- Each "block" on the main diagonal corresponds to the terms related to a single day's independent problem (data fidelity, ridge, spatial smoothness).
- The temporal regularization term creates smaller "off-diagonal" blocks that couple each day's equations to its immediate neighbors.
- The `solve_block_tridiagonal_segment` helper function is specifically designed to efficiently solve such a structured system, finding the global optimum that balances all regularization terms across the entire time series.

Furthermore, the function includes a **hard zero reference anchor**: the `x` vector for the `ref_date` is strictly constrained to be zero. This is because `x` represents the _change_ relative to the reference, and by definition, the change at the reference point itself is zero. This hard constraint further integrates the reference day into the global solution, affecting the solutions for all other days.

In summary, the primary purpose of `solve_temporal` is to _avoid_ independent daily solutions. By solving the entire sequence simultaneously with temporal regularization, it produces a more coherent, stable, and physically meaningful time-series of RTI reconstructions.

CREATE USER holzofen WITH PASSWORD 'biofilter';
CREATE DATABASE sensor_data OWNER holzofen;
