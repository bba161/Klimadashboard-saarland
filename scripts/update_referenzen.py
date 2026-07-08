#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_referenzen.py
=====================

Lädt die HISTORISCHEN Tageswerte der saarländischen DWD-Stationen
(Klimadaten "kl", https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/daily/kl/historical/)
und berechnet daraus für jede Referenzperiode (1961-1990 und 1991-2020)
und jeden Kalendertag (01-01 ... 12-31):

  - Temperatur (TXK, Tageshöchsttemperatur): Mean / P10 / P90 über alle Jahre der Periode
  - Niederschlag (RSK) als rollierendes 30-Tage-Fenster: kumulierte Summe der letzten
    30 Tage (endend an diesem Kalendertag), davon wieder Mean / P10 / P90 über die Jahre
  - "sommertage_mittel": mittlere Anzahl Tage/Jahr mit TXK >= 25°C in der Periode

Das Ergebnis wird nach data/saarland_referenzen.json geschrieben.

Dieses Script läuft NUR 1x/Monat (siehe .github/workflows/update_referenzen.yml).
Die aktuellen 30-Tage-Werte ("HEUTE") kommen NICHT aus diesem Script, sondern
werden im Frontend live von der BrightSky-API geladen.

Benötigt: requests  (pip install requests)
Der Rest nutzt nur die Python-Standardbibliothek (kein pandas/numpy nötig).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import statistics
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

DWD_BASE_URL = (
    "https://opendata.dwd.de/climate_environment/CDC/"
    "observations_germany/climate/daily/kl/historical/"
)

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "saarland_referenzen.json"

# Referenzperioden (WMO-Standard + aktuelle Klimanormalperiode)
PERIODS: Dict[str, Tuple[int, int]] = {
    "1961-1990": (1961, 1990),
    "1991-2020": (1991, 2020),
}

# Stationsliste (Stations-ID -> Name/Koordinaten)
STATIONS: Dict[str, dict] = {
    "00460": {"name": "Berus", "lat": 49.2641, "lon": 6.6868},
    "03545": {"name": "Neunkirchen-Wellesweiler", "lat": 49.3372, "lon": 7.1667},
    "03904": {"name": "Perl-Nennig", "lat": 49.5364, "lon": 6.3711},
    "04336": {"name": "Saarbrücken-Ensheim", "lat": 49.2147, "lon": 7.1092},
    "05029": {"name": "Tholey", "lat": 49.4814, "lon": 7.1442},
    "05433": {"name": "Weiskirchen", "lat": 49.5581, "lon": 6.8083},
    "06217": {"name": "Saarbrücken-Burbach", "lat": 49.2181, "lon": 6.9525},
}

# Fehlwert-Kennung der DWD-CSV-Dateien
DWD_MISSING = {"-999", "-999.0"}

RETRIES = 4
RETRY_BACKOFF_SECONDS = 8  # verdoppelt sich bei jedem weiteren Versuch
REQUEST_TIMEOUT = 60  # Sekunden

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("update_referenzen")


# ---------------------------------------------------------------------------
# HTTP mit Retry-Logik (gegen die bekannten DWD-Timeouts)
# ---------------------------------------------------------------------------

def fetch_with_retry(url: str, binary: bool = False):
    """Lädt eine URL mit Retry + exponentiellem Backoff. Wirft am Ende eine Exception."""
    last_exc: Optional[Exception] = None
    delay = RETRY_BACKOFF_SECONDS

    for attempt in range(1, RETRIES + 1):
        try:
            log.info("GET %s (Versuch %d/%d)", url, attempt, RETRIES)
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.content if binary else resp.text
        except Exception as exc:  # noqa: BLE001 - bewusst breit, wir loggen & retryen
            last_exc = exc
            log.warning("Fehlgeschlagen (%s). Warte %ds vor erneutem Versuch...", exc, delay)
            if attempt < RETRIES:
                time.sleep(delay)
                delay *= 2

    raise RuntimeError(f"Konnte {url} nach {RETRIES} Versuchen nicht laden") from last_exc


# ---------------------------------------------------------------------------
# Station -> ZIP-Datei finden (Dateinamen enthalten variable Start/End-Daten)
# ---------------------------------------------------------------------------

def find_zip_filename(station_id: str, index_html: str) -> str:
    """
    Sucht in der DWD-Verzeichnisauflistung nach der passenden ZIP-Datei
    für eine Stations-ID, z.B. tageswerte_KL_00460_19310101_20201231_hist.zip
    """
    pattern = re.compile(
        rf'tageswerte_KL_{station_id}_\d{{8}}_\d{{8}}_hist\.zip'
    )
    match = pattern.search(index_html)
    if not match:
        raise ValueError(f"Keine ZIP-Datei für Station {station_id} im DWD-Index gefunden")
    return match.group(0)


# ---------------------------------------------------------------------------
# ZIP herunterladen, "produkt_klima_tag_*"-Datei extrahieren & parsen
# ---------------------------------------------------------------------------

@dataclass
class DailyRecord:
    d: date
    txk: Optional[float]  # Tageshöchsttemperatur (°C)
    rsk: Optional[float]  # Niederschlagshöhe (mm)


def parse_zip_to_records(zip_bytes: bytes) -> List[DailyRecord]:
    records: List[DailyRecord] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        product_files = [n for n in zf.namelist() if n.lower().startswith("produkt_klima_tag")]
        if not product_files:
            raise ValueError("Keine produkt_klima_tag_*.txt in ZIP gefunden")

        with zf.open(product_files[0]) as f:
            text = io.TextIOWrapper(f, encoding="latin-1")
            reader = csv.DictReader(text, delimiter=";")

            for row in reader:
                # Spaltennamen haben teils führende/nachfolgende Leerzeichen
                row = {k.strip(): v.strip() for k, v in row.items() if k}

                mess_datum = row.get("MESS_DATUM")
                if not mess_datum or len(mess_datum) != 8:
                    continue

                try:
                    d = date(int(mess_datum[0:4]), int(mess_datum[4:6]), int(mess_datum[6:8]))
                except ValueError:
                    continue

                txk_raw = row.get("TXK", "-999")
                rsk_raw = row.get("RSK", "-999")

                txk = None if txk_raw in DWD_MISSING else _safe_float(txk_raw)
                rsk = None if rsk_raw in DWD_MISSING else _safe_float(rsk_raw)

                records.append(DailyRecord(d=d, txk=txk, rsk=rsk))

    return records


def _safe_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Statistik-Hilfsfunktionen
# ---------------------------------------------------------------------------

def percentile(values: List[float], pct: float) -> Optional[float]:
    """
    Lineare Interpolation, analog numpy.percentile (Standardmethode).
    pct in [0, 100].
    """
    if not values:
        return None
    data = sorted(values)
    n = len(data)
    if n == 1:
        return data[0]

    rank = (pct / 100.0) * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return data[lo] + (data[hi] - data[lo]) * frac


def round_or_none(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------------------
# Referenzberechnung pro Station & Periode
# ---------------------------------------------------------------------------

def compute_reference_for_period(
    records: List[DailyRecord], year_start: int, year_end: int
) -> dict:
    """
    Berechnet temperatur_pro_tag, niederschlag_kumuliert_pro_tag und
    sommertage_mittel für eine Referenzperiode.
    """
    # Schneller Zugriff: Datum -> Record
    by_date: Dict[date, DailyRecord] = {r.d: r for r in records}

    period_records = [r for r in records if year_start <= r.d.year <= year_end]
    if not period_records:
        log.warning(
            "Keine Daten im Zeitraum %d-%d gefunden (Station evtl. kürzer aktiv)",
            year_start, year_end,
        )

    # --- Temperatur pro Kalendertag --------------------------------------
    temp_by_mmdd: Dict[str, List[float]] = {}
    for r in period_records:
        if r.txk is None:
            continue
        key = r.d.strftime("%m-%d")
        temp_by_mmdd.setdefault(key, []).append(r.txk)

    temperatur_pro_tag = {}
    for mmdd, values in sorted(temp_by_mmdd.items()):
        temperatur_pro_tag[mmdd] = {
            "mean": round_or_none(statistics.fmean(values)),
            "p10": round_or_none(percentile(values, 10)),
            "p90": round_or_none(percentile(values, 90)),
        }

    # --- Niederschlag: rollierendes 30-Tage-Fenster pro Kalendertag ------
    # Für jeden Kalendertag (mm-dd) und jedes Jahr der Periode: Summe RSK der
    # 30 Tage bis einschließlich diesem Datum. Fehlende Einzeltage werden
    # übersprungen (nicht als 0 gewertet), fehlt der ganze Tag komplett,
    # wird das Jahr für diesen Kalendertag ausgelassen.
    rain_by_mmdd: Dict[str, List[float]] = {}

    # Alle (Monat, Tag)-Kombinationen, die in den Daten vorkommen (inkl. 29.2.)
    all_mmdd = sorted({r.d.strftime("%m-%d") for r in period_records})

    for mmdd in all_mmdd:
        month, day = int(mmdd[:2]), int(mmdd[3:])
        for year in range(year_start, year_end + 1):
            try:
                target = date(year, month, day)
            except ValueError:
                continue  # z.B. 29. Februar in Nicht-Schaltjahr

            if target not in by_date:
                continue

            window_sum = 0.0
            missing_days = 0
            for offset in range(30):
                day_d = target - timedelta(days=offset)
                rec = by_date.get(day_d)
                if rec is None or rec.rsk is None:
                    missing_days += 1
                    continue
                window_sum += rec.rsk

            # Wenn mehr als 1/3 der Fenstertage fehlen, Jahr für diesen Tag verwerfen
            if missing_days > 10:
                continue

            rain_by_mmdd.setdefault(mmdd, []).append(window_sum)

    niederschlag_kumuliert_pro_tag = {}
    for mmdd, values in sorted(rain_by_mmdd.items()):
        niederschlag_kumuliert_pro_tag[mmdd] = {
            "mean": round_or_none(statistics.fmean(values)),
            "p10": round_or_none(percentile(values, 10)),
            "p90": round_or_none(percentile(values, 90)),
        }

    # --- Sommertage (TXK >= 25°C) im Mittel pro Jahr ----------------------
    summer_days_per_year: Dict[int, int] = {}
    for r in period_records:
        if r.txk is not None and r.txk >= 25.0:
            summer_days_per_year[r.d.year] = summer_days_per_year.get(r.d.year, 0) + 1

    years_with_data = {r.d.year for r in period_records}
    sommertage_mittel = (
        round(sum(summer_days_per_year.get(y, 0) for y in years_with_data) / len(years_with_data), 1)
        if years_with_data
        else None
    )

    return {
        "temperatur_pro_tag": temperatur_pro_tag,
        "niederschlag_kumuliert_pro_tag": niederschlag_kumuliert_pro_tag,
        "sommertage_mittel": sommertage_mittel,
    }


# ---------------------------------------------------------------------------
# Hauptlogik pro Station
# ---------------------------------------------------------------------------

def process_station(station_id: str, meta: dict, dwd_index_html: str) -> Optional[dict]:
    log.info("=== Station %s (%s) ===", station_id, meta["name"])

    try:
        zip_filename = find_zip_filename(station_id, dwd_index_html)
    except ValueError as exc:
        log.error("Übersprungen: %s", exc)
        return None

    zip_url = DWD_BASE_URL + zip_filename

    try:
        zip_bytes = fetch_with_retry(zip_url, binary=True)
    except RuntimeError as exc:
        log.error("Download fehlgeschlagen für %s: %s", station_id, exc)
        return None

    try:
        records = parse_zip_to_records(zip_bytes)
    except ValueError as exc:
        log.error("Parsen fehlgeschlagen für %s: %s", station_id, exc)
        return None

    log.info("  %d Tageswerte geladen (%s bis %s)", len(records), records[0].d, records[-1].d)

    referenzperioden = {}
    for period_name, (y_start, y_end) in PERIODS.items():
        log.info("  Berechne Referenzperiode %s ...", period_name)
        referenzperioden[period_name] = compute_reference_for_period(records, y_start, y_end)

    return {
        "name": meta["name"],
        "lat": meta["lat"],
        "lon": meta["lon"],
        "referenzperioden": referenzperioden,
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    log.info("Starte Update der Referenzdaten (%d Stationen)", len(STATIONS))

    try:
        dwd_index_html = fetch_with_retry(DWD_BASE_URL)
    except RuntimeError as exc:
        log.error("Konnte DWD-Verzeichnisliste nicht laden: %s", exc)
        return 1

    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stations": {},
    }

    failures: List[str] = []

    for station_id, meta in STATIONS.items():
        station_result = process_station(station_id, meta, dwd_index_html)
        if station_result is None:
            failures.append(station_id)
            continue
        result["stations"][station_id] = station_result

    if not result["stations"]:
        log.error("Keine einzige Station konnte verarbeitet werden. Abbruch ohne Schreiben.")
        return 1

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    log.info("Fertig. Geschrieben nach %s", OUTPUT_PATH)

    if failures:
        log.warning(
            "Achtung: %d von %d Stationen konnten nicht aktualisiert werden: %s",
            len(failures), len(STATIONS), ", ".join(failures),
        )
        # Wir geben trotzdem 0 zurück, damit der Commit-Schritt in der Action
        # die erfolgreichen Stationen trotzdem committet. Bei Bedarf hier auf
        # "return 1" ändern, um bei Teilausfällen den Workflow rot zu markieren.

    return 0


if __name__ == "__main__":
    sys.exit(main())
