# Sensor Data DB Inspection Report

This document explains the output from running:

python3 inspect_sensor_data.py

## Database Overview

The script connected to sensor_data.db and found 3 tables:

- measurement
- rounds
- sqlite_sequence

This is a normal result for SQLite when at least one table uses AUTOINCREMENT (sqlite_sequence is an internal SQLite bookkeeping table).

## Table: measurement

Purpose:
- Stores per-link measurement events, likely one row for each sender->receiver attempt inside a round.

Important columns (from your schema):
- id: Primary key
- pc_ts: PC timestamp when the row was logged
- round_id: Links this row to one round in rounds
- sender_id, receiver_id: Node pair
- trigger_ms, rx_wait_ms, e2e_ms: Timing measurements
- ok_match: Success indicator (1 means successful match)
- rssi, snr: Radio quality metrics
- bme_temp, bme_hum, bme_press: Environmental sensor values

What your preview shows:
- All first previewed rows belong to round_id = 1.
- The payload is constant in these rows (The quick brown fox jumps over the lazy dog).
- Most links in the preview are successful (ok_match = 1).
- Some bme_* values are 0.0 while others are populated. That usually means some nodes did not provide BME data for that sample (missing sensor, disabled sensor, or parsing fallback to 0).
- RSSI/SNR values vary by receiver, which is expected for different links and placement.

## Table: rounds

Purpose:
- Stores one row per measurement round, including configuration and summary quality counters.

Important columns:
- round_id: Primary key
- pc_start, pc_end, duration_s: Round timing
- nodes_csv: Participating node IDs
- ok_links, total_links, timeouts: Round-level success summary
- freq_mhz, tx_power, sf, bw_khz, cr_denom: LoRa radio settings used
- lora_applied: Flag indicating settings were applied
- measure_interval_s: Sampling interval

What your preview shows:
- Round 1 had 54/56 successful links and 2 timeouts.
- Rounds 2, 4, and 5 reached 56/56 with 0 timeouts.
- Round 3 had 55/56 with 1 timeout.
- Configuration appears stable across shown rounds (433 MHz, TX power 22, SF12, BW125, CR 4/8).

Interpretation:
- The system seems mostly healthy, with occasional timeout events in some rounds.
- Round duration is around 44 seconds in normal rounds, with some longer rounds likely caused by timeout/retry waiting.

## Table: sqlite_sequence

Purpose:
- Internal SQLite table used to track last AUTOINCREMENT value.

What your preview shows:
- measurement -> 11952360

Interpretation:
- The largest allocated AUTOINCREMENT id for measurement is 11,952,360.
- This suggests high ingestion volume over time.
- Note: This value tracks last assigned id, not guaranteed current row count if rows were deleted.

## Overall Readout

From this output alone:
- Database structure is consistent and readable.
- You have a high-volume measurement log.
- Round summaries and per-link measurements are relationally linked via round_id.
- Link quality is generally good with occasional timeouts.
- Environmental fields are partly populated (some nodes/records return zero values).

## Optional Next Checks

If you want deeper inspection, useful follow-up SQL queries are:

- Count rows per table
- Timeout rate by round and over time
- Packet success by receiver_id
- Distribution of RSSI/SNR
- Percentage of rows where bme_temp/hum/press are zero


## Sample

(.venv) holzofen@laptop-haus:~/Schreibtisch/prototype_01$ python3 inspect_sensor_data.py 
Database: sensor_data.db
Tables (3): measurement, rounds, sqlite_sequence

=== Schema: measurement ===
cid | name | type | notnull | default | pk
------------------------------------------------------------
0 | id | INTEGER | 0 | None | 1
1 | pc_ts | TEXT | 1 | None | 0
2 | round_id | INTEGER | 0 | None | 0
3 | sender_id | INTEGER | 0 | None | 0
4 | receiver_id | INTEGER | 0 | None | 0
5 | marker | INTEGER | 0 | None | 0
6 | payload_len | INTEGER | 0 | None | 0
7 | payload_text | TEXT | 0 | None | 0
8 | trigger_ms | REAL | 0 | None | 0
9 | rx_wait_ms | REAL | 0 | None | 0
10 | e2e_ms | REAL | 0 | None | 0
11 | ok_match | INTEGER | 0 | None | 0
12 | rx_id | INTEGER | 0 | None | 0
13 | node_timestamp | INTEGER | 0 | None | 0
14 | rssi | REAL | 0 | None | 0
15 | snr | REAL | 0 | None | 0
16 | bme_wait_ms | REAL | 0 | None | 0
17 | bme_temp | REAL | 0 | None | 0
18 | bme_hum | REAL | 0 | None | 0
19 | bme_press | REAL | 0 | None | 0

=== Preview: measurement (up to 5 rows) ===
id | pc_ts | round_id | sender_id | receiver_id | marker | payload_len | payload_text | trigger_ms | rx_wait_ms | e2e_ms | ok_match | rx_id | node_timestamp | rssi | snr | bme_wait_ms | bme_temp | bme_hum | bme_press
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
1 | 2026-02-27T17:23:36 | 1 | 1 | 2 | 1772209409 | 43 | The quick brown fox jumps over the lazy dog | 25.870681000014883 | 4033.6308299993107 | 4033.6308299993107 | 1 | 1 | 1772209409 | -71.0 | 4.7 | 29.982074000145076 | 0.0 | 0.0 | 0.0
2 | 2026-02-27T17:23:36 | 1 | 1 | 3 | 1772209409 | 43 | The quick brown fox jumps over the lazy dog | 25.870681000014883 | 149.84116699997685 | 149.84116699997685 | 1 | 1 | 1772209409 | -83.0 | 5.2 | 29.940210999484407 | 0.0 | 0.0 | 0.0
3 | 2026-02-27T17:23:36 | 1 | 1 | 4 | 1772209409 | 43 | The quick brown fox jumps over the lazy dog | 25.870681000014883 | 149.93319699897256 | 149.93319699897256 | 1 | 1 | 1772209409 | -104.0 | 4.7 | 29.969437999170623 | 0.0 | 0.0 | 0.0
4 | 2026-02-27T17:23:36 | 1 | 1 | 5 | 1772209409 | 43 | The quick brown fox jumps over the lazy dog | 25.870681000014883 | 149.91397999983747 | 149.91397999983747 | 1 | 1 | 1772209409 | -74.0 | 5.7 | 29.912070000136737 | 12.97 | 62.89 | 975.6
5 | 2026-02-27T17:23:36 | 1 | 1 | 6 | 1772209409 | 43 | The quick brown fox jumps over the lazy dog | 25.870681000014883 | 149.80940100031148 | 149.80940100031148 | 1 | 1 | 1772209409 | -108.0 | 3.2 | 29.9733389983885 | 13.72 | 59.55 | 976.0

=== Schema: rounds ===
cid | name | type | notnull | default | pk
------------------------------------------------------------
0 | round_id | INTEGER | 0 | None | 1
1 | pc_start | TEXT | 0 | None | 0
2 | pc_end | TEXT | 0 | None | 0
3 | duration_s | REAL | 0 | None | 0
4 | nodes_csv | TEXT | 0 | None | 0
5 | payload_len | INTEGER | 0 | None | 0
6 | payload_text | TEXT | 0 | None | 0
7 | ok_links | INTEGER | 0 | None | 0
8 | total_links | INTEGER | 0 | None | 0
9 | timeouts | INTEGER | 0 | None | 0
10 | freq_mhz | REAL | 0 | None | 0
11 | tx_power | INTEGER | 0 | None | 0
12 | sf | INTEGER | 0 | None | 0
13 | bw_khz | INTEGER | 0 | None | 0
14 | cr_denom | INTEGER | 0 | None | 0
15 | lora_applied | INTEGER | 0 | None | 0
16 | port | TEXT | 0 | None | 0
17 | baudrate | INTEGER | 0 | None | 0
18 | measure_interval_s | REAL | 0 | None | 0
19 | notes | TEXT | 0 | None | 0

=== Preview: rounds (up to 5 rows) ===
round_id | pc_start | pc_end | duration_s | nodes_csv | payload_len | payload_text | ok_links | total_links | timeouts | freq_mhz | tx_power | sf | bw_khz | cr_denom | lora_applied | port | baudrate | measure_interval_s | notes
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
1 | 2026-02-27T17:23:31 | 2026-02-27T17:24:54 | 83.80275969300055 | 1,2,3,4,5,6,7,8 | 43 | The quick brown fox jumps over the lazy dog | 54 | 56 | 2 | 433.0 | 22 | 12 | 125 | 8 | 1 | /dev/ttyUSB0 | 9600 | 10.0 | 
2 | 2026-02-27T17:24:54 | 2026-02-27T17:25:38 | 43.895328484000856 | 1,2,3,4,5,6,7,8 | 43 | The quick brown fox jumps over the lazy dog | 56 | 56 | 0 | 433.0 | 22 | 12 | 125 | 8 | 1 | /dev/ttyUSB0 | 9600 | 10.0 | 
3 | 2026-02-27T17:25:38 | 2026-02-27T17:26:42 | 63.78351161700084 | 1,2,3,4,5,6,7,8 | 43 | The quick brown fox jumps over the lazy dog | 55 | 56 | 1 | 433.0 | 22 | 12 | 125 | 8 | 1 | /dev/ttyUSB0 | 9600 | 10.0 | 
4 | 2026-02-27T17:26:42 | 2026-02-27T17:27:26 | 43.906213819000186 | 1,2,3,4,5,6,7,8 | 43 | The quick brown fox jumps over the lazy dog | 56 | 56 | 0 | 433.0 | 22 | 12 | 125 | 8 | 1 | /dev/ttyUSB0 | 9600 | 10.0 | 
5 | 2026-02-27T17:27:26 | 2026-02-27T17:28:10 | 43.88109959800022 | 1,2,3,4,5,6,7,8 | 43 | The quick brown fox jumps over the lazy dog | 56 | 56 | 0 | 433.0 | 22 | 12 | 125 | 8 | 1 | /dev/ttyUSB0 | 9600 | 10.0 | 

=== Schema: sqlite_sequence ===
cid | name | type | notnull | default | pk
------------------------------------------------------------
0 | name |  | 0 | None | 0
1 | seq |  | 0 | None | 0

=== Preview: sqlite_sequence (up to 5 rows) ===
name | seq
--------------------
measurement | 11952360
