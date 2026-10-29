# slave_rotation

Rotation logger for `firmware/slave_rotation`: alternates **LoRa rounds** and **CW rounds** on the
same RS485 bus. The master decides the mode; the nodes follow.

```
cp .env.example .env     # set PORT and NODES (same as services/sensor_logger/.env)
python main_rotation_data.py
```

Started as a script it asks whether to write new radio settings; `run(ctx, lora="read")` skips the
questions (that is the form a manager service would use).

## How a round works

1. Write the wanted mode to register 420 of every node, wait until register 421 reports it.
   A node that doesn't follow (rebooted, radio error) is left out of that round; with fewer than
   two nodes in the mode the round is skipped. A rebooted node comes back in LoRa mode and is
   re-synced automatically before the next round.
2. LoRa round: every node sends one packet (CMD 10), the others report RSSI/SNR (regs 100..115).
   CW round: every node sends a carrier (CMD 30), the others measure RSSI (CMD 32, regs 130..135).
3. Wait until `MEASURE_INTERVAL_S` has passed since the round started, then the next round.

## Database

No schema change. LoRa rounds go to `setting`, `rounds`, `measurement`; CW rounds to `cw_rounds`,
`cw_measurement` (same layout the two existing loggers use). Pair a LoRa and a CW round by
`pc_start`/`pc_end`. `services/postgres_sync` only copies the LoRa tables.

## Register map (new in the rotation firmware)

| Address | Meaning |
|---|---|
| 30 / 31 / 32 (CMD, reg 200) | CW start / CW stop / RSSI measure (CW mode only); CMD 10 sends a packet (LoRa mode only) |
| 130..135 | CW result: avg, 0, max, min, count, seq (RSSI x10 in dBm) |
| 360 / 361 / 362 | CW duration ms / measure duration ms / CW state (0 idle, 1 TX, 2 measuring) |
| 420 | wanted mode: 0 = LoRa, 1 = CW (reset to 0 on reboot) |
| 421 | active mode: 0 = LoRa, 1 = CW, 0xFFFF = radio init failed |

Everything else (IDs, OTA, payload, 400..410) is unchanged from `firmware/slave`.

## Not the same bus as the sensor logger

This service and `services/sensor_logger` talk to the same nodes over the same serial port: don't
run both at once. `manage_services.py start` (no arguments) starts every non-one-shot service, so
this service is deliberately not registered there.
