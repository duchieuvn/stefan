import math
import sys

import backend.experiments.config as cfg_module

# ----------------------------------------------------------------------
# 1. READ FUNCTION
# ----------------------------------------------------------------------
def read_config():
    """Reads configuration parameters from config.py and returns a dictionary."""
    return {
        # RF parameters
        "power_dbm": cfg_module.POWER_DBM,
        "antenna_gain_dbi": cfg_module.ANTENNA_GAIN_DBI,
        "cable_loss_db": cfg_module.CABLE_LOSS_DB,
        # Modulation parameters
        "sf": cfg_module.SF,
        "bw_khz": cfg_module.BW_KHZ,
        "cr_denom": cfg_module.CR_DENOM,
        "preamble_len": cfg_module.PREAMBLE_LEN,
        "payload_bytes": cfg_module.PAYLOAD_BYTES,
        "crc_enabled": cfg_module.CRC_ENABLED,
        "explicit_header": cfg_module.EXPLICIT_HEADER,
        "ldro": cfg_module.LDRO.strip().lower(),
        # Timing parameters
        "interval_ms": cfg_module.INTERVAL_MS,
    }

# ----------------------------------------------------------------------
# 2. POWER & MW CALCULATION FUNCTION
# ----------------------------------------------------------------------
def calculate_power_mw(power_dbm, antenna_gain_dbi=2.15, cable_loss_db=0.0):
    """
    Calculates conducted power and ERP in milliwatts (mW) and dBm.
    ERP = Conducted (dBm) - Cable Loss (dB) + Antenna Gain (dBd)
    where dBd = dBi - 2.15
    """
    conducted_mw = 10.0 ** (power_dbm / 10.0)
    antenna_gain_dbd = antenna_gain_dbi - 2.15
    erp_dbm = power_dbm - cable_loss_db + antenna_gain_dbd
    erp_mw = 10.0 ** (erp_dbm / 10.0)

    return {
        "conducted_dbm": power_dbm,
        "conducted_mw": conducted_mw,
        "erp_dbm": erp_dbm,
        "erp_mw": erp_mw,
    }

# ----------------------------------------------------------------------
# 3. TIME-ON-AIR (ToA) HELPER CALCULATION
# ----------------------------------------------------------------------
def calculate_time_on_air_ms(sf, bw_khz, cr_denom, preamble_len, payload_bytes,
                             crc_enabled=True, explicit_header=True, ldro="auto"):
    """Calculates symbol time and total Time on Air (ToA) in milliseconds."""
    bw_hz = bw_khz * 1000.0
    t_sym_ms = (2.0 ** sf / bw_hz) * 1000.0

    # Determine LDRO status
    if ldro == "auto":
        ldro_active = 1 if t_sym_ms > 16.0 else 0
    elif ldro in ["true", "1", "yes"]:
        ldro_active = 1
    else:
        ldro_active = 0

    # Preamble duration
    t_preamble_ms = (preamble_len + 4.25) * t_sym_ms

    # Payload symbols calculation
    h = 0 if explicit_header else 1
    crc = 1 if crc_enabled else 0
    cr = cr_denom - 4

    payload_term = 8 * payload_bytes - 4 * sf + 28 + 16 * crc - 20 * h
    denominator = 4 * (sf - 2 * ldro_active)
    num_payload_sym = 8 + max(math.ceil(payload_term / denominator) * (cr + 4), 0)
    t_payload_ms = num_payload_sym * t_sym_ms

    return {
        "t_sym_ms": t_sym_ms,
        "ldro_active": bool(ldro_active),
        "toa_ms": t_preamble_ms + t_payload_ms,
    }

# ----------------------------------------------------------------------
# 4. DUTY CYCLE CALCULATION FUNCTION
# ----------------------------------------------------------------------
def calculate_duty_cycle(toa_ms, interval_ms):
    """Calculates duty cycle percentage given the ToA and total cycle period."""
    if interval_ms <= 0:
        raise ValueError("Interval must be greater than zero.")
    return (toa_ms / interval_ms) * 100.0

# ----------------------------------------------------------------------
# 5. WRITE / DISPLAY FUNCTION
# ----------------------------------------------------------------------
def write_console_report(power_results, timing_results, duty_cycle_pct, interval_ms):
    """Outputs the calculated metrics cleanly to the console."""
    print("=" * 48)
    print("           LORA RF CALCULATION REPORT           ")
    print("=" * 48)
    print(f"Symbol Duration    : {timing_results['t_sym_ms']:8.3f} ms")
    print(f"LDRO Enabled       : {'Yes' if timing_results['ldro_active'] else 'No'}")
    print(f"Time on Air (ToA)  : {timing_results['toa_ms']:8.2f} ms ({timing_results['toa_ms'] / 1000.0:.3f} s)")
    print(f"Tx Cycle Interval  : {interval_ms:8.0f} ms ({interval_ms / 1000.0:.2f} s)")
    print("-" * 48)
    print("-" * 48)
    print(f"Conducted Power    : {power_results['conducted_mw']:8.2f} mW ({power_results['conducted_dbm']:.1f} dBm)")
    print(f"Estimated ERP      : {power_results['erp_mw']:8.2f} mW ({power_results['erp_dbm']:.2f} dBm ERP)")
    print(f"DUTY CYCLE         : {duty_cycle_pct:8.2f} %")
    print("=" * 48)

# ----------------------------------------------------------------------
# MAIN PIPELINE
# ----------------------------------------------------------------------
def main():
    try:
        # Step 1: Read configuration
        cfg = read_config()

        # Step 2: Compute power in mW / ERP
        power_results = calculate_power_mw(
            power_dbm=cfg["power_dbm"],
            antenna_gain_dbi=cfg["antenna_gain_dbi"],
            cable_loss_db=cfg["cable_loss_db"]
        )

        # Step 3: Compute Time-on-Air
        timing_results = calculate_time_on_air_ms(
            sf=cfg["sf"],
            bw_khz=cfg["bw_khz"],
            cr_denom=cfg["cr_denom"],
            preamble_len=cfg["preamble_len"],
            payload_bytes=cfg["payload_bytes"],
            crc_enabled=cfg["crc_enabled"],
            explicit_header=cfg["explicit_header"],
            ldro=cfg["ldro"]
        )

        # Step 4: Compute Duty Cycle
        duty_cycle_pct = calculate_duty_cycle(
            toa_ms=timing_results["toa_ms"],
            interval_ms=cfg["interval_ms"]
        )

        # Step 5: Write output report
        write_console_report(power_results, timing_results, duty_cycle_pct, cfg["interval_ms"])

    except Exception as err:
        print(f"Execution Error: {err}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()