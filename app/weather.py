"""
Weather at the container site from the DWD Climate Data Center (opendata.dwd.de, no API key).

Hourly observations of one DWD station (`weather_dwd_station` in config.py):
temperature and relative humidity (TU) and precipitation (RR) from the
`recent` files (about the last 500 days up to yesterday), older days from
`historical`, and today from the 10-minute `now` files. DWD timestamps are
UTC; they are converted to local time and aggregated per day over the same
HH:MM window as the shown RTI maps (`cur_start_hhmm` to `cur_end_hhmm`).
"""

from __future__ import annotations

import io
import logging
import re
import urllib.error
import urllib.request
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from config import RTIConfig

CDC_URL = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate"
SITE_TIMEZONE = ZoneInfo("Europe/Berlin")
MISSING = -999.0
REQUEST_TIMEOUT_S = 60

Series = dict[datetime, float]  # UTC timestamp -> value


class WeatherError(RuntimeError):
    pass


@dataclass(frozen=True)
class DailyWeather:
    """One value per calendar day in `dates`; NaN where the window has no data."""
    dates: list[str]
    temperature_mean: np.ndarray  # °C
    temperature_min: np.ndarray  # °C
    temperature_max: np.ndarray  # °C
    precipitation_sum: np.ndarray  # mm
    humidity_mean: np.ndarray  # % relative humidity

    def index(self, date_txt: str) -> int | None:
        try:
            return self.dates.index(date_txt)
        except ValueError:
            return None


def station_is_set(config: RTIConfig) -> bool:
    return config.weather_dwd_station is not None


# --- Download / parsing ---

def _download(path: str) -> bytes:
    url = f"{CDC_URL}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise WeatherError(f"DWD: {error.code} {error.reason} für {url}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise WeatherError(f"DWD nicht erreichbar: {error}") from error


def _read_product(path: str) -> list[dict[str, str]]:
    """Rows of the `produkt_*.txt` table (`;`-separated) inside the zip at `path`."""
    with zipfile.ZipFile(io.BytesIO(_download(path))) as archive:
        name = next(n for n in archive.namelist() if n.startswith("produkt_"))
        lines = archive.read(name).decode("latin-1").splitlines()
    header = [h.strip() for h in lines[0].split(";")]
    return [dict(zip(header, (v.strip() for v in line.split(";")))) for line in lines[1:] if line.strip()]


def _utc(mess_datum: str) -> datetime:
    """MESS_DATUM is YYYYMMDDHH (hourly) or YYYYMMDDHHMM (10 minutes), in UTC."""
    fmt = "%Y%m%d%H%M" if len(mess_datum) == 12 else "%Y%m%d%H"
    return datetime.strptime(mess_datum, fmt).replace(tzinfo=timezone.utc)


def _series(rows: list[dict[str, str]], column: str) -> Series:
    return {_utc(row["MESS_DATUM"]): float(row[column]) for row in rows if float(row[column]) != MISSING}


def _load_hourly(product: str, prefix: str, station: str, columns: tuple[str, ...], since: datetime) -> dict[str, Series]:
    """Hourly `columns` from the recent file, plus the historical file(s) if recent starts after `since`."""
    rows = _read_product(f"hourly/{product}/recent/stundenwerte_{prefix}_{station}_akt.zip")
    series = {column: _series(rows, column) for column in columns}
    if rows and _utc(rows[0]["MESS_DATUM"]) <= since:
        return series

    # Historical file names contain their date range, so look them up in the directory listing.
    directory = f"hourly/{product}/historical/"
    listing = _download(directory).decode("latin-1")
    for name in sorted(set(re.findall(rf"stundenwerte_{prefix}_{station}_\d{{8}}_\d{{8}}_hist\.zip", listing))):
        old_rows = _read_product(directory + name)
        for column in columns:
            series[column] = {**_series(old_rows, column), **series[column]}  # recent wins
    return series


def _extend(series: Series, newer: Series) -> None:
    """Adds the values of `newer` that come after the last timestamp in `series`."""
    last = max(series, default=None)
    series.update({t: v for t, v in newer.items() if last is None or t > last})


# --- Daily aggregation ---

def _aggregate(values: list[float], fn) -> float:
    return float(fn(values)) if values else np.nan


def fetch_daily_weather(config: RTIConfig, start_date: str, end_date: str) -> DailyWeather:
    """Daily weather from `start_date` to `end_date` (YYYY-MM-DD, local dates, inclusive)."""
    if not station_is_set(config):
        raise WeatherError("Keine DWD-Station gesetzt (weather_dwd_station in config.py).")
    station = config.weather_dwd_station
    logging.info("Fetching weather %s..%s for DWD station %s.", start_date, end_date, station)

    first_day, last_day = date.fromisoformat(start_date), date.fromisoformat(end_date)
    since = datetime.combine(first_day, datetime.min.time(), SITE_TIMEZONE)
    until = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), SITE_TIMEZONE)

    tu = _load_hourly("air_temperature", "TU", station, ("TT_TU", "RF_TU"), since)
    rr = _load_hourly("precipitation", "RR", station, ("R1",), since)
    temperature, humidity, precipitation = tu["TT_TU"], tu["RF_TU"], rr["R1"]

    # The hourly files end yesterday; today comes from the 10-minute files. Temperature and
    # humidity are taken at full hours only, so each hour weighs the same as in the hourly data.
    if last_day >= date.today():
        try:
            now_tu = _read_product(f"10_minutes/air_temperature/now/10minutenwerte_TU_{station}_now.zip")
            now_rr = _read_product(f"10_minutes/precipitation/now/10minutenwerte_nieder_{station}_now.zip")
        except WeatherError as error:
            logging.warning("No DWD data for today: %s", error)
        else:
            _extend(temperature, {t: v for t, v in _series(now_tu, "TT_10").items() if t.minute == 0})
            _extend(humidity, {t: v for t, v in _series(now_tu, "RF_10").items() if t.minute == 0})
            _extend(precipitation, _series(now_rr, "RWS_10"))

    # Keep the values whose local time lies inside the map window.
    by_day: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for name, series in (("temperature", temperature), ("humidity", humidity), ("precipitation", precipitation)):
        for ts, value in series.items():
            if not since <= ts < until:
                continue
            local = ts.astimezone(SITE_TIMEZONE)
            if config.cur_start_hhmm <= local.strftime("%H:%M") <= config.cur_end_hhmm:
                by_day[local.date().isoformat()][name].append(value)

    dates = [(first_day + timedelta(days=i)).isoformat() for i in range((last_day - first_day).days + 1)]
    temps = [by_day[d]["temperature"] for d in dates]
    return DailyWeather(
        dates=dates,
        temperature_mean=np.array([_aggregate(t, np.mean) for t in temps]),
        temperature_min=np.array([_aggregate(t, np.min) for t in temps]),
        temperature_max=np.array([_aggregate(t, np.max) for t in temps]),
        precipitation_sum=np.array([_aggregate(by_day[d]["precipitation"], np.sum) for d in dates]),
        humidity_mean=np.array([_aggregate(by_day[d]["humidity"], np.mean) for d in dates]),
    )
