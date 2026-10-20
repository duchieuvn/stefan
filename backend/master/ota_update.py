"""Update the slave nodes' firmware over WiFi (OTA), driven over Modbus.

For each node: write the WiFi credentials and the firmware URL into its registers,
send CMD 50 and follow its progress. The node joins the WiFi, downloads the .bin
(e.g. a GitHub Release asset), flashes it and reboots; then its last result is read
back. Every run flashes, whatever the node is running. Register map:
firmware/slave/ota_handler.h.

manage_services.py runs this as the one-shot "ota" service and asks for the settings:

    nodes      node IDs to update (default: NODES from .env)
    ssid       WiFi the nodes join for the download
    password   its password ("" for an open network); never printed or stored
    url        firmware URL, one .bin for all nodes (default: OTA_URL); each node keeps
               its Modbus ID in its own flash (NVS), so the image has no ID in it
    md5_check  download the file here first (verified TLS) and let the nodes check its
               MD5; also catches a missing file before any node is touched

Standalone: python ota_update.py --ssid MyWifi [--nodes 10,11] [--url ...] [--no-md5]
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from pymodbus.client import ModbusSerialClient
from pymodbus.exceptions import ModbusException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/, for service_runtime
from service_runtime import ServiceContext

from config import BAUDRATE, CMD_REGISTER, NODES, OTA_NODE_TIMEOUT_S, OTA_PORT, OTA_URL
from main_senor_data import _call

# Register map (firmware/slave/ota_handler.h)
STATUS_REG = 500            # 500..504 = status, progress, detail, last result, last detail
SSID_LEN_REG, SSID_MAX = 510, 32
PASS_LEN_REG, PASS_MAX = 530, 64
URL_LEN_REG, URL_MAX = 570, 160
MD5_REG = 660               # 16 registers, 32 hex characters
CMD_OTA_START = 50
CMD_OTA_ABORT = 51

ST_IDLE, ST_STARTED, ST_SUCCESS = 0, 1, 4
STATUS_TEXT = {
    1: "connecting to WiFi",
    2: "downloading",
    3: "verifying",
    4: "done, rebooting",
    20: "WiFi failed",
    21: "download failed (HTTP)",
    22: "no room for the image (OTA partition scheme?)",
    23: "download or flash write failed",
    24: "image check failed (corrupt download or MD5 mismatch)",
    25: "aborted",
    26: "bad parameters (SSID or URL)",
}
WIFI_TEXT = {1: "SSID not found", 4: "wrong password?", 6: "disconnected"}

APP_IMAGE_MAGIC = 0xE9
MAX_APP_SIZE = 4 * 1024 * 1024
NO_START_TIMEOUT_S = 10.0


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class NodeError(Exception):
    pass


# --- Firmware file ----------------------------------------------------------------

def fetch_md5(url: str) -> tuple[str, int]:
    """Download url (TLS verified, redirects followed); return its MD5 hex and size."""
    request = urllib.request.Request(url, headers={"User-Agent": "stefan-ota"})
    md5, size, first = hashlib.md5(), 0, b""
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            while chunk := response.read(65536):
                if not first:
                    first = chunk[:1]
                md5.update(chunk)
                size += len(chunk)
    except OSError as error:   # URLError/HTTPError are OSErrors
        raise NodeError(f"could not download {url}: {error}") from None
    if first != bytes([APP_IMAGE_MAGIC]) or size > MAX_APP_SIZE:
        raise NodeError(f"{url} is not an ESP32 app image ({size} bytes); upload slave.ino.bin, "
                        f"not the merged or bootloader .bin")
    return md5.hexdigest(), size


# --- Modbus -----------------------------------------------------------------------

def pack_bytes(data: bytes) -> list[int]:
    """2 bytes per register, high byte first (like PAYLOAD_DATA_BASE)."""
    return [(data[i] << 8) | (data[i + 1] if i + 1 < len(data) else 0) for i in range(0, len(data), 2)]


def read_regs(client, sid: int, address: int, count: int) -> list[int] | None:
    """Registers, or None if the node did not answer or refused."""
    try:
        res = _call(client.read_holding_registers, address=address, count=count, device_id=sid)
    except ModbusException:
        return None
    if res is None or res.isError():
        return None
    return list(res.registers)


def write_regs(client, sid: int, address: int, values: list[int]) -> None:
    try:
        res = _call(client.write_registers, address, values, device_id=sid)
    except ModbusException as error:
        raise NodeError(f"no answer writing register {address}: {error}") from None
    if res is None or res.isError():
        raise NodeError(f"writing register {address} failed: {res}")


def write_cmd(client, sid: int, cmd: int) -> None:
    try:
        res = _call(client.write_register, CMD_REGISTER, cmd, device_id=sid)
    except ModbusException as error:
        raise NodeError(f"no answer to CMD {cmd}: {error}") from None
    if res is None or res.isError():
        raise NodeError(f"CMD {cmd} failed: {res}")


def describe_status(status: int, detail: int) -> str:
    text = STATUS_TEXT.get(status, f"status {status}")
    if status == 20 and detail in WIFI_TEXT:
        return f"{text} ({WIFI_TEXT[detail]})"
    if detail:
        return f"{text} ({detail})"
    return text


# --- One node ---------------------------------------------------------------------

def update_node(client, sid: int, ssid: bytes, password: bytes, url: str, md5: str | None,
                ctx: ServiceContext, label: str) -> str:
    """Flash one node; returns a short result text, raises NodeError on failure.

    md5 is the checksum the node checks the download against, or None.
    """
    if read_regs(client, sid, STATUS_REG, 1) is None:
        # Distinguish an old firmware (registers missing) from a node that is not there.
        if read_regs(client, sid, CMD_REGISTER, 1) is None:
            raise NodeError("does not answer on Modbus")
        raise NodeError("firmware has no OTA support (flash an OTA-capable build over USB once)")
    log(f"Node {sid}: flashing")

    url_bytes = url.encode()
    write_regs(client, sid, SSID_LEN_REG, [len(ssid)] + pack_bytes(ssid))
    write_regs(client, sid, PASS_LEN_REG, [len(password)] + pack_bytes(password))
    write_regs(client, sid, URL_LEN_REG, [len(url_bytes)] + pack_bytes(url_bytes))
    write_regs(client, sid, MD5_REG, pack_bytes(md5.encode()) if md5 else [0] * 16)
    write_cmd(client, sid, CMD_OTA_START)

    started = time.monotonic()
    deadline = started + OTA_NODE_TIMEOUT_S
    seen_running = False
    last_text = ""
    while True:
        if ctx.wait(1.0):
            try:
                write_cmd(client, sid, CMD_OTA_ABORT)
                log(f"Node {sid}: abort sent")
            except NodeError:
                pass
            raise NodeError("stopped by the manager (abort sent)")
        if time.monotonic() > deadline:
            try:
                write_cmd(client, sid, CMD_OTA_ABORT)
            except NodeError:
                pass
            raise NodeError(f"no result within {OTA_NODE_TIMEOUT_S:.0f}s (abort sent)")

        regs = read_regs(client, sid, STATUS_REG, 5)
        if regs is None:
            continue    # busy writing flash, or rebooting
        status, progress, detail, last_result, last_detail = regs
        if status >= 20:
            raise NodeError(describe_status(status, detail))
        if status == ST_IDLE:
            if seen_running:
                break   # the node rebooted
            if time.monotonic() - started > NO_START_TIMEOUT_S:
                raise NodeError("did not start the update")
            continue
        seen_running = True
        text = f"{STATUS_TEXT.get(status, status)}" + (f" {progress}%" if status == 2 else "")
        if text != last_text:
            log(f"Node {sid}: {text}")
            last_text = text
        ctx.report(activity=f"{label}: {text}")

    # Rebooted (it answered again): the last result (kept in flash) says whether the update went through.
    if last_result != ST_SUCCESS:
        if last_result == ST_STARTED:
            raise NodeError("restarted in the middle of the update, still on the old firmware")
        raise NodeError(describe_status(last_result, last_detail))
    result = f"flashed in {time.monotonic() - started:.0f}s" + (f", MD5 {md5}" if md5 else "")
    log(f"Node {sid}: ✅ {result}")
    return result


# --- Service entry point -------------------------------------------------------------

def run(ctx: ServiceContext, ssid: str = "", password: str = "", nodes: list[int] | None = None,
        url: str | None = None, md5_check: bool = True) -> None:
    nodes = list(nodes or NODES)
    url = url or OTA_URL
    ssid_bytes, pass_bytes = ssid.encode(), password.encode()
    if not ssid_bytes:
        raise SystemExit("No WiFi SSID given")
    if len(ssid_bytes) > SSID_MAX:
        raise SystemExit(f"SSID longer than {SSID_MAX} bytes")
    if len(pass_bytes) > PASS_MAX:
        raise SystemExit(f"WiFi password longer than {PASS_MAX} bytes")
    if len(url.encode()) > URL_MAX:
        raise SystemExit(f"URL longer than {URL_MAX} bytes: {url}")

    print("=== firmware update (OTA) ===")
    print(f"PORT: {OTA_PORT} @ {BAUDRATE}")
    print(f"NODES: {nodes}")
    print(f"WiFi: {ssid}")
    print(f"URL: {url}")

    md5 = None
    if md5_check:
        ctx.report(activity="checking firmware file")
        try:
            md5, size = fetch_md5(url)
        except NodeError as error:
            raise SystemExit(str(error)) from None
        log(f"Firmware: {size} bytes, MD5 {md5}")

    client = ModbusSerialClient(port=OTA_PORT, baudrate=BAUDRATE, stopbits=1, bytesize=8,
                                parity="N", timeout=1.0)
    if not client.connect():
        raise RuntimeError(f"Modbus connect failed on {OTA_PORT}")

    results: dict[int, str] = {}
    failed = 0
    try:
        for i, sid in enumerate(nodes, 1):
            if ctx.stopping():
                results[sid] = "– skipped (stopped)"
                continue
            label = f"node {sid} ({i}/{len(nodes)})"

            ctx.report(activity=f"{label}: checking node")
            try:
                results[sid] = "✓ " + update_node(client, sid, ssid_bytes, pass_bytes, url, md5, ctx, label)
            except NodeError as error:
                failed += 1
                results[sid] = f"✗ {error}"
                log(f"Node {sid}: ❌ {error}")
            ctx.report(last_result=" | ".join(f"{sid} {text}" for sid, text in results.items()))
    finally:
        client.close()

    ctx.report(last_run=datetime.now())
    print("\n=== result ===")
    for sid, text in results.items():
        print(f"Node {sid}: {text}")
    if failed:
        raise SystemExit(f"{failed}/{len(nodes)} nodes not updated")


def main() -> None:
    parser = argparse.ArgumentParser(description="Update the slave nodes' firmware over WiFi.")
    parser.add_argument("--ssid", required=True, help="WiFi the nodes join for the download")
    parser.add_argument("--nodes", help=f"comma-separated node IDs (default: {','.join(map(str, NODES))})")
    parser.add_argument("--url", help=f"firmware URL, one .bin for all nodes (default: {OTA_URL})")
    parser.add_argument("--no-md5", action="store_true", help="skip the download check on this machine")
    args = parser.parse_args()
    nodes = [int(n) for n in args.nodes.split(",")] if args.nodes else None
    password = getpass.getpass("WiFi password: ")
    try:
        run(ServiceContext.standalone(), ssid=args.ssid, password=password, nodes=nodes, url=args.url,
            md5_check=not args.no_md5)
    except KeyboardInterrupt:
        print("\nStopped by user (a running node update continues; it can't be aborted from here).")


if __name__ == "__main__":
    main()
