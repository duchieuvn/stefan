# Stefan Dynamische RTI

Radio Tomographic Imaging (RTI) system for monitoring moisture/attenuation
changes inside a container using RSSI measurements between fixed LoRa sensor
nodes. Node-to-node signal attenuation is combined with a spatially and
temporally regularized reconstruction to produce a 2D map of change over
time.

## Repo layout

- **`SOFTWARE/`** — data processing, reconstruction, and visualization.
  - `app.py` — Flask web app: browser-based viewer for the dynamic RTI
    reconstruction (see below).
  - `rti_10.py` — the original Tkinter desktop viewer (same reconstruction,
    native GUI instead of a browser).
  - `rti/rti_temporal_core.py` — DB access, RTI model construction, and the
    spatial + temporal regularized solve. No UI dependencies.
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

## Running the RTI web viewer

```bash
pip install -r requirements.txt
cd SOFTWARE
python app.py
```

Then open `http://127.0.0.1:5000/` in a browser. Must be run from `SOFTWARE/`
so the relative `sensor_data.db` path resolves.

The page shows:

- A **3-month / 6-month** window selector — bounds how far back the
  reference/current-map sliders reach, so DB queries stay cheap regardless of
  how much history has accumulated.
- **Referenztag** slider — picks the reference day the reconstruction is
  computed relative to.
- **Karte in der Zeitfolge** slider — scrubs through the solved sequence to
  view any day's reconstructed attenuation-change map.
- **Neu berechnen** — forces a recompute for the current reference day
  (bypasses the in-memory sequence cache; use if new sensor data has been
  written since the app started).

## Notes

- `sensor_data.db` uses WAL mode, so the viewer can safely read while the
  LoRa logger is concurrently writing.
- The web viewer's in-memory caches (`INTERVAL_CACHE`, `SEQUENCE_CACHE`) and
  the list of available dates are only refreshed on restart — restart
  `app.py` to pick up newly collected sensor data.

CREATE USER holzofen WITH PASSWORD 'biofilter';
CREATE DATABASE sensor_data OWNER holzofen;
