#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_referenzen.py
=====================

Läuft 1x/Monat (statt stündlich) und erzeugt data/saarland_referenzen.json:

  - referenzperioden["1961-1990"/"1991-2020"]:
      - temperatur_pro_tag["MM-DD"]        = {mean, p10, p90}   (TXK, alle Jahre der Periode)
      - niederschlag_kumuliert_pro_tag["MM-DD"] = {mean, p10, p90}  (30-Tage-Fenster, alle Jahre)
      - sommertage_mittel                  (mittlere Sommertage/Jahr in der Periode)
      - niederschlag_jahresmittel_mm       (mittlerer Jahresniederschlag in der Periode)
  - sommertage_pro_jahr["2000".."<akt. Jahr>"] = Anzahl Tage mit TXK >= SOMMERTAG_SCHWELLE_C
      (ganzes Kalenderjahr, wie in update_data.py::compute_sommertage_pro_jahr - NICHT nur
       März-Oktober! Braucht dafür sowohl DWD "historical" als auch "recent"-Daten, da
       "historical" bei ca. 2020 endet.)

WICHTIG: Dieses Script ersetzt NICHT update_data.py / saarland.json. Beide Dateien
existieren parallel:
  - saarland.json            -> weiterhin von update_data.py erzeugt, dient nur noch
                                 als Fallback falls BrightSky im Frontend down ist
  - saarland_referenzen.json -> NEU, von diesem Script, 1x/Monat

Die "heute"-Werte (tagesreihe_30d, tageshoechstwert_heute_c, sommertage_laufendes_jahr,
niederschlag_letzte_30_tage_mm) kommen NICHT aus diesem Script, sondern werden im
Frontend live per BrightSky-API nachgeladen (siehe index.html).

Benötigt: requests (pip install requests). Sonst nur Standardbibliothek.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

sys.path.insert(0, str(Path(__file__).parent))
from stations import STATIONS, SOMMERTAG_SCHWELLE_C, STATS_START_JAHR  # gleiche Quelle wie update_data.py!

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

BASE = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate"
DAILY_HIST_URL = f"{BASE}/daily/kl/historical/"
DAILY_RECENT_URL = f"{BASE}/daily/kl/recent/"
KL_STATIONS_LIST_URL = f"{BASE}/daily/kl/recent/KL_Tageswerte_Beschreibung_Stationen.txt"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "saarland_referenzen.json"
STATIONS_META_CACHE = DATA_DIR / "stations_meta.json"  # von update_data.py bereits gepflegt

PERIODS: Dict[str, Tuple[int, int]] = {
    "1961-1990": (1961, 1990),
    "1991-2020": (1991, 2020),
}

REQUEST_TIMEOUT = 60
RETRIES = 4
RETRY_BACKOFF_SECONDS = 8

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("update_referenzen")

session = requests.Session()
session.headers.update({"User-Agent": "saarland-klimadashboard/1.0 (Zeitungsprojekt)"})


# ---------------------------------------------------------------------------
# HTTP mit Retry-Logik
# ---------------------------------------------------------------------------

def fetch_with_retry(url: str, binary: bool = False):
    last_exc: Optional[Exception] = None
    delay = RETRY_BACKOFF_SECONDS
    for attempt in range(1, RETRIES + 1):
        try:
            log.info("GET %s (Versuch %d/%d)", url, attempt, RETRIES)
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.content if binary else resp.content.decode("latin-1", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("Fehlgeschlagen (%s). Warte %ds vor erneutem Versuch...", exc, delay)
            if attempt < RETRIES:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"Konnte {url} nach {RETRIES} Versuchen nicht laden") from last_exc


# ---------------------------------------------------------------------------
# Stationsmetadaten (lat/lon) - wiederverwendet aus dem Cache von update_data.py,
# baut ihn bei Bedarf aber auch selbst neu auf (kein harter Dependency).
# ---------------------------------------------------------------------------

def parse_dwd_station_list(text: str) -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    lines = text.splitlines()
    data_started = False
    for line in lines:
        if not data_started:
            if re.match(r"^-{5,}", line.strip()):
                data_started = True
            continue
        if not line.strip():
            continue
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        station_id = parts[0].strip().zfill(5)
        try:
            hoehe = float(parts[3])
            lat = float(parts[4])
            lon = float(parts[5])
        except ValueError:
            continue
        result[station_id] = {"lat": lat, "lon": lon, "hoehe": hoehe}
    return result


def load_station_meta() -> Dict[str, dict]:
    if STATIONS_META_CACHE.exists():
        log.info("Verwende vorhandenen Stationsmeta-Cache: %s", STATIONS_META_CACHE)
        return json.loads(STATIONS_META_CACHE.read_text(encoding="utf-8"))

    log.info("Kein Stationsmeta-Cache gefunden, baue ihn neu auf...")
    kl_list = parse_dwd_station_list(fetch_with_retry(KL_STATIONS_LIST_URL))
    meta: Dict[str, dict] = {}
    for station_id, display_name in STATIONS.items():
        info = kl_list.get(station_id)
        if info is None:
            log.error("Station %s (%s) nicht in DWD-Stationsliste gefunden!", station_id, display_name)
            continue
        meta[station_id] = {"name": display_name, "lat": info["lat"], "lon": info["lon"], "hoehe": info["hoehe"]}
    return meta


# ---------------------------------------------------------------------------
# Tageswerte laden (historical + recent kombiniert, wie in update_data.py)
# ---------------------------------------------------------------------------

def find_zip_links(html: str, prefix: str) -> List[str]:
    return re.findall(rf'href="({re.escape(prefix)}[^"]+\.zip)"', html)


def read_produkt_file_from_zip(zip_bytes: bytes) -> List[dict]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        produkt_name = next((n for n in zf.namelist() if n.lower().startswith("produkt_")), None)
        if produkt_name is None:
            raise ValueError("Keine produkt_*.txt in ZIP gefunden")
        with zf.open(produkt_name) as f:
            text = f.read().decode("latin-1", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    return [{k.strip(): v.strip() for k, v in row.items() if k} for row in reader]


def to_float_or_none(value: str) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f <= -999 else f


def load_full_daily_series(station_id: str, hist_html: str, recent_html: str) -> List[dict]:
    """
    Kombiniert DWD 'historical' + 'recent' zu einer durchgehenden Tagesreihe.
    'historical' deckt i.d.R. bis ~2020 ab, 'recent' die letzten ~500 Tage bis gestern.
    Notwendig für sommertage_pro_jahr, damit auch 2021-heute im Trend auftauchen.
    """
    rows_all: List[dict] = []
    for html, base_url, suffix in [
        (hist_html, DAILY_HIST_URL, "_hist.zip"),
        (recent_html, DAILY_RECENT_URL, "_akt.zip"),
    ]:
        pattern = f"tageswerte_KL_{station_id}"
        matches = [
            link for link in find_zip_links(html, "tageswerte_KL_")
            if link.startswith(pattern) and link.endswith(suffix)
        ]
        if not matches:
            log.warning("Keine %s-Datei für Station %s gefunden", suffix, station_id)
            continue
        zip_url = base_url + matches[0]
        try:
            zip_bytes = fetch_with_retry(zip_url, binary=True)
            rows = read_produkt_file_from_zip(zip_bytes)
        except (RuntimeError, ValueError, zipfile.BadZipFile) as e:
            log.error("Fehler bei %s: %s", zip_url, e)
            continue
        for r in rows:
            datum_str = r.get("MESS_DATUM", "")
            if not re.match(r"^\d{8}$", datum_str):
                continue
            rows_all.append({
                "datum": datum_str,
                "txk": to_float_or_none(r.get("TXK", "")),
                "rsk": to_float_or_none(r.get("RSK", "")),
            })

    by_date: Dict[str, dict] = {}
    for row in rows_all:
        by_date[row["datum"]] = row  # 'recent' überschreibt 'historical' bei Überlappung
    return [by_date[d] for d in sorted(by_date.keys())]


# ---------------------------------------------------------------------------
# Statistik-Hilfsfunktionen
# ---------------------------------------------------------------------------

def percentile(values: List[float], p: float) -> Optional[float]:
    """Lineare Interpolation - identisch zur _percentile()-Logik in update_data.py."""
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    k = (n - 1) * p
    f = int(k)
    c = f + 1
    if c >= n:
        return vals[-1]
    return vals[f] + (k - f) * (vals[c] - vals[f])


def round_or_none(v: Optional[float], digits: int = 1) -> Optional[float]:
    return None if v is None else round(v, digits)


# ---------------------------------------------------------------------------
# sommertage_pro_jahr - EXAKT wie update_data.py::compute_sommertage_pro_jahr
# (ganzes Kalenderjahr, kein Monatsfilter!)
# ---------------------------------------------------------------------------

def compute_sommertage_pro_jahr(daily: List[dict]) -> Dict[int, int]:
    counts: Dict[int, int] = {}
    for row in daily:
        if row["txk"] is None:
            continue
        j = int(row["datum"][:4])
        if row["txk"] >= SOMMERTAG_SCHWELLE_C:
            counts[j] = counts.get(j, 0) + 1
        else:
            counts.setdefault(j, counts.get(j, 0))
    return counts


def build_sommertage_serie(daily: List[dict]) -> Dict[str, int]:
    sommertage_jahr = compute_sommertage_pro_jahr(daily)
    verfuegbar_ab = min(sommertage_jahr.keys()) if sommertage_jahr else None
    start_jahr = max(STATS_START_JAHR, verfuegbar_ab) if verfuegbar_ab else STATS_START_JAHR
    aktuelles_jahr = datetime.now(timezone.utc).year
    return {
        str(j): sommertage_jahr.get(j, 0)
        for j in range(start_jahr, aktuelles_jahr + 1)
        if j in sommertage_jahr
    }


# ---------------------------------------------------------------------------
# Referenzband-Berechnung pro Periode (Temperatur pro Kalendertag,
# Niederschlag als rollierendes 30-Tage-Fenster pro Kalendertag)
# ---------------------------------------------------------------------------

def compute_reference_for_period(daily: List[dict], year_start: int, year_end: int) -> dict:
    by_date: Dict[date, dict] = {}
    for r in daily:
        d = date(int(r["datum"][:4]), int(r["datum"][4:6]), int(r["datum"][6:8]))
        by_date[d] = r

    period_rows = [r for r in daily if year_start <= int(r["datum"][:4]) <= year_end]

    # --- Temperatur pro Kalendertag ---------------------------------------
    temp_by_mmdd: Dict[str, List[float]] = {}
    for r in period_rows:
        if r["txk"] is None:
            continue
        mmdd = f"{r['datum'][4:6]}-{r['datum'][6:8]}"
        temp_by_mmdd.setdefault(mmdd, []).append(r["txk"])

    temperatur_pro_tag = {}
    for mmdd, values in sorted(temp_by_mmdd.items()):
        temperatur_pro_tag[mmdd] = {
            "mean": round_or_none(statistics.fmean(values)),
            "p10": round_or_none(percentile(values, 0.10)),
            "p90": round_or_none(percentile(values, 0.90)),
        }

    # --- Niederschlag: rollierendes 30-Tage-Fenster pro Kalendertag ------
    rain_by_mmdd: Dict[str, List[float]] = {}
    all_mmdd = sorted({f"{r['datum'][4:6]}-{r['datum'][6:8]}" for r in period_rows})

    for mmdd in all_mmdd:
        month, day = int(mmdd[:2]), int(mmdd[3:])
        for year in range(year_start, year_end + 1):
            try:
                target = date(year, month, day)
            except ValueError:
                continue
            if target not in by_date:
                continue
            window_sum, missing = 0.0, 0
            for offset in range(30):
                rec = by_date.get(target - timedelta(days=offset))
                if rec is None or rec["rsk"] is None:
                    missing += 1
                    continue
                window_sum += rec["rsk"]
            if missing > 10:
                continue
            rain_by_mmdd.setdefault(mmdd, []).append(window_sum)

    niederschlag_kumuliert_pro_tag = {}
    for mmdd, values in sorted(rain_by_mmdd.items()):
        niederschlag_kumuliert_pro_tag[mmdd] = {
            "mean": round_or_none(statistics.fmean(values)),
            "p10": round_or_none(percentile(values, 0.10)),
            "p90": round_or_none(percentile(values, 0.90)),
        }

    # --- sommertage_mittel + niederschlag_jahresmittel_mm -----------------
    # EXAKT wie update_data.py::compute_referenzmittel: Jahresniederschlag =
    # Summe ALLER Tageswerte der Periode / Anzahl Jahre mit Sommertage-Daten.
    sommertage_by_jahr = compute_sommertage_pro_jahr(period_rows)
    jahre = [j for j in sommertage_by_jahr if year_start <= j <= year_end]

    if not jahre:
        sommertage_mittel = None
        niederschlag_jahresmittel_mm = None
    else:
        sommertage_mittel = round(sum(sommertage_by_jahr[j] for j in jahre) / len(jahre), 1)
        nied_werte = [r["rsk"] for r in period_rows if r["rsk"] is not None]
        niederschlag_jahresmittel_mm = round(sum(nied_werte) / len(jahre), 1) if nied_werte else None

    return {
        "temperatur_pro_tag": temperatur_pro_tag,
        "niederschlag_kumuliert_pro_tag": niederschlag_kumuliert_pro_tag,
        "sommertage_mittel": sommertage_mittel,
        "niederschlag_jahresmittel_mm": niederschlag_jahresmittel_mm,
    }


# ---------------------------------------------------------------------------
# Pro Station
# ---------------------------------------------------------------------------

def process_station(station_id: str, meta: dict, hist_html: str, recent_html: str) -> Optional[dict]:
    log.info("=== Station %s (%s) ===", station_id, meta["name"])

    daily = load_full_daily_series(station_id, hist_html, recent_html)
    if not daily:
        log.error("Keine Tageswerte für Station %s, übersprungen.", station_id)
        return None

    log.info("  %d Tageswerte geladen (%s bis %s)", len(daily), daily[0]["datum"], daily[-1]["datum"])

    referenzperioden = {}
    for name, (y1, y2) in PERIODS.items():
        log.info("  Berechne Referenzperiode %s ...", name)
        referenzperioden[name] = compute_reference_for_period(daily, y1, y2)

    return {
        "name": meta["name"],
        "lat": meta["lat"],
        "lon": meta["lon"],
        "referenzperioden": referenzperioden,
        "sommertage_pro_jahr": build_sommertage_serie(daily),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    log.info("Starte Update der Referenzdaten (%d Stationen)", len(STATIONS))

    try:
        station_meta = load_station_meta()
    except RuntimeError as exc:
        log.error("Konnte Stationsmetadaten nicht laden: %s", exc)
        return 1

    try:
        hist_html = fetch_with_retry(DAILY_HIST_URL)
        recent_html = fetch_with_retry(DAILY_RECENT_URL)
    except RuntimeError as exc:
        log.error("Konnte DWD-Verzeichnislisten nicht laden: %s", exc)
        return 1

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sommertag_schwelle_c": SOMMERTAG_SCHWELLE_C,
        "stations": {},
    }

    failures: List[str] = []
    for station_id, display_name in STATIONS.items():
        meta = station_meta.get(station_id)
        if meta is None:
            log.error("Keine Metadaten (lat/lon) für Station %s, übersprungen.", station_id)
            failures.append(station_id)
            continue
        try:
            station_result = process_station(station_id, meta, hist_html, recent_html)
        except Exception:  # noqa: BLE001 - eine Station soll den Rest nicht blockieren
            log.exception("Unerwarteter Fehler bei Station %s", station_id)
            station_result = None

        if station_result is None:
            failures.append(station_id)
            continue
        result["stations"][station_id] = station_result

    if not result["stations"]:
        log.error("Keine einzige Station konnte verarbeitet werden. Abbruch ohne Schreiben.")
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Fertig. Geschrieben nach %s", OUTPUT_FILE)

    if failures:
        log.warning("%d von %d Stationen fehlgeschlagen: %s", len(failures), len(STATIONS), ", ".join(failures))

    return 0


if __name__ == "__main__":
    sys.exit(main())
