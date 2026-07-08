#!/usr/bin/env python3
"""
update_data.py (v3 - MIT LIVE-DATEN FÜR HEUTE)
================================================
Erweitert um:
1. Stündliche Daten für HEUTE (vom hourly endpoint)
2. Nur überschreiben wenn HÖHERE Temperatur
3. Heute als letzten Tag in tagesreihe_30d ergänzen
4. Retry-Logik gegen Timeouts
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sys
import zipfile
import time
from datetime import datetime, timedelta, timezone, date
import zoneinfo
from pathlib import Path
from typing import Optional
from statistics import mean as stat_mean

import requests

sys.path.insert(0, str(Path(__file__).parent))
from stations import STATIONS, SOMMERTAG_SCHWELLE_C, STATS_START_JAHR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("update_data")

BASE = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate"
DAILY_HIST_URL = f"{BASE}/daily/kl/historical/"
DAILY_RECENT_URL = f"{BASE}/daily/kl/recent/"
HOURLY_TEMP_URL = f"{BASE}/hourly/air_temperature/recent/"
HOURLY_PRECIP_URL = f"{BASE}/hourly/precipitation/recent/"
KL_STATIONS_LIST_URL = f"{BASE}/daily/kl/recent/KL_Tageswerte_Beschreibung_Stationen.txt"
TU_STATIONS_LIST_URL = f"{BASE}/hourly/air_temperature/recent/TU_Stundenwerte_Beschreibung_Stationen.txt"
RR_STATIONS_LIST_URL = f"{BASE}/hourly/precipitation/recent/RR_Stundenwerte_Beschreibung_Stationen.txt"

DATA_DIR = Path(__file__).parent.parent / "data"
OUTPUT_FILE = DATA_DIR / "saarland.json"
STATIONS_META_CACHE = DATA_DIR / "stations_meta.json"

REQUEST_TIMEOUT = 60
MAX_HOURLY_STATION_DISTANCE_KM = 15.0
TAGESREIHE_TAGE = 30
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

session = requests.Session()
session.headers.update({"User-Agent": "saarland-klimadashboard/1.0 (Zeitungsprojekt)"})


# ---------------------------------------------------------------------------
# Hilfsfunktionen mit Retry-Logik
# ---------------------------------------------------------------------------

def fetch(url: str, retries: int = MAX_RETRIES) -> bytes:
    """Fetch mit exponential backoff retry."""
    for attempt in range(retries):
        try:
            log.info("GET %s (Versuch %d/%d)", url, attempt + 1, retries)
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < retries - 1:
                wait = RETRY_DELAY_SECONDS * (2 ** attempt)
                log.warning("Timeout/Connection Error: %s. Warte %ds...", e, wait)
                time.sleep(wait)
            else:
                log.error("Alle Versuche fehlgeschlagen: %s", e)
                raise
        except requests.RequestException as e:
            log.error("Request Error: %s", e)
            raise
    raise RuntimeError(f"Konnte {url} nicht laden nach {retries} Versuchen")


def fetch_text(url: str) -> str:
    raw = fetch(url)
    return raw.decode("latin-1", errors="replace")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import radians, sin, cos, sqrt, atan2
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def parse_dwd_station_list(text: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    lines = text.splitlines()
    data_started = False
    for line in lines:
        if line.startswith("-" * 20):
            data_started = True
            continue
        if not data_started or not line.strip():
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        sid = parts[0].strip()
        try:
            lat, lon, hoehe = float(parts[2]), float(parts[3]), float(parts[4])
            result[sid] = {"lat": lat, "lon": lon, "hoehe": hoehe}
        except ValueError:
            continue
    return result


def find_zip_links(html: str, prefix: str = "") -> list[str]:
    pattern = r'href="([^"]+\.zip)"'
    all_links = re.findall(pattern, html, re.IGNORECASE)
    return [lnk for lnk in all_links if lnk.lower().startswith(prefix.lower())]


def to_float_or_none(s: str) -> Optional[float]:
    s = s.strip()
    if not s or s == "-999":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_produkt_file_from_zip(zip_bytes: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        produkt = [n for n in z.namelist() if "produkt" in n.lower() and n.endswith(".txt")]
        if not produkt:
            raise ValueError("Keine produkt_*.txt gefunden")
        with z.open(produkt[0]) as f:
            txt = f.read().decode("latin-1", errors="replace")
    reader = csv.DictReader(io.StringIO(txt), delimiter=";")
    return [r for r in reader]


# ---------------------------------------------------------------------------
# Stationsmetadaten
# ---------------------------------------------------------------------------

def load_or_build_station_meta() -> dict[str, dict]:
    if STATIONS_META_CACHE.exists():
        log.info("Verwende gecachte Stationsmetadaten (%s)", STATIONS_META_CACHE)
        return json.loads(STATIONS_META_CACHE.read_text(encoding="utf-8"))
    log.info("Lade Stationsmetadaten vom DWD...")
    kl_meta = parse_dwd_station_list(fetch_text(KL_STATIONS_LIST_URL))
    tu_meta = parse_dwd_station_list(fetch_text(TU_STATIONS_LIST_URL))
    result: dict[str, dict] = {}
    for sid in STATIONS.keys():
        if sid not in kl_meta:
            log.warning("Station %s nicht in KL-Liste", sid)
            continue
        info = kl_meta[sid].copy()
        hourly = find_nearest_station(info["lat"], info["lon"], tu_meta)
        if hourly:
            info["hourly_temp_station_id"], info["hourly_temp_distance_km"] = hourly
        result[sid] = info
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIONS_META_CACHE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def find_nearest_station(lat: float, lon: float, pool: dict[str, dict]) -> Optional[tuple[str, float]]:
    best_id, best_dist = None, float("inf")
    for sid, info in pool.items():
        d = haversine_km(lat, lon, info["lat"], info["lon"])
        if d < best_dist:
            best_id, best_dist = sid, d
    return (best_id, best_dist) if best_id else None


# ---------------------------------------------------------------------------
# Tageswerte
# ---------------------------------------------------------------------------

def load_daily_series(station_id: str) -> list[dict]:
    rows_all: list[dict] = []
    for base_url, suffix in [(DAILY_HIST_URL, "_hist.zip"), (DAILY_RECENT_URL, "_akt.zip")]:
        try:
            html = fetch_text(base_url)
        except requests.RequestException as e:
            log.warning("Konnte Verzeichnis %s nicht laden: %s", base_url, e)
            continue
        pattern = f"tageswerte_KL_{station_id}"
        matches = [
            link for link in find_zip_links(html, "tageswerte_KL_")
            if link.startswith(pattern) and link.endswith(suffix)
        ]
        if not matches:
            continue
        zip_url = base_url + matches[0]
        try:
            zip_bytes = fetch(zip_url)
            rows = read_produkt_file_from_zip(zip_bytes)
        except (requests.RequestException, ValueError, zipfile.BadZipFile) as e:
            log.error("Fehler: %s: %s", zip_url, e)
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
    by_date: dict[str, dict] = {}
    for row in rows_all:
        by_date[row["datum"]] = row
    return [by_date[d] for d in sorted(by_date.keys())]


# ---------------------------------------------------------------------------
# NEU: Stündliche Daten für HEUTE
# ---------------------------------------------------------------------------

def load_hourly_temp_today(station_id: str) -> Optional[dict]:
    """
    Lädt stündliche Temperaturdaten für HEUTE.
    Gibt zurück: {"max_temp": float, "timestamp": str} oder None
    """
    if not station_id:
        return None
    
    try:
        html = fetch_text(HOURLY_TEMP_URL)
    except requests.RequestException as e:
        log.warning("Konnte Hourly-Temp-Verzeichnis nicht laden: %s", e)
        return None
    
    pattern = f"stundenwerte_TU_{station_id}"
    matches = [
        link for link in find_zip_links(html, "stundenwerte_TU_")
        if link.startswith(pattern) and link.endswith("_akt.zip")
    ]
    
    if not matches:
        log.warning("Keine Hourly-Temp-Datei für Station %s", station_id)
        return None
    
    zip_url = HOURLY_TEMP_URL + matches[0]
    try:
        zip_bytes = fetch(zip_url)
        rows = read_produkt_file_from_zip(zip_bytes)
    except (requests.RequestException, ValueError, zipfile.BadZipFile) as e:
        log.error("Fehler bei Hourly-Temp: %s: %s", zip_url, e)
        return None
    
    # Filtere nur HEUTE
    heute_str = datetime.now(zoneinfo.ZoneInfo("Europe/Berlin")).strftime("%Y%m%d")
    temps_heute = []
    
    for row in rows:
        datum_str = row.get("MESS_DATUM", "")
        if not datum_str.startswith(heute_str):
            continue
        
        temp = to_float_or_none(row.get("TT_TU", ""))
        if temp is not None:
            temps_heute.append({
                "temp": temp,
                "timestamp": datum_str
            })
    
    if not temps_heute:
        return None
    
    # Maximum finden
    max_entry = max(temps_heute, key=lambda x: x["temp"])
    
    return {
        "max_temp": round(max_entry["temp"], 1),
        "timestamp": max_entry["timestamp"]
    }


def load_hourly_precip_today(station_id: str) -> Optional[float]:
    """
    Lädt stündliche Niederschlagsdaten für HEUTE.
    Gibt Summe in mm zurück.
    """
    if not station_id:
        return None
    
    try:
        html = fetch_text(HOURLY_PRECIP_URL)
    except requests.RequestException as e:
        log.warning("Konnte Hourly-Precip-Verzeichnis nicht laden: %s", e)
        return None
    
    pattern = f"stundenwerte_RR_{station_id}"
    matches = [
        link for link in find_zip_links(html, "stundenwerte_RR_")
        if link.startswith(pattern) and link.endswith("_akt.zip")
    ]
    
    if not matches:
        log.warning("Keine Hourly-Precip-Datei für Station %s", station_id)
        return None
    
    zip_url = HOURLY_PRECIP_URL + matches[0]
    try:
        zip_bytes = fetch(zip_url)
        rows = read_produkt_file_from_zip(zip_bytes)
    except (requests.RequestException, ValueError, zipfile.BadZipFile) as e:
        log.error("Fehler bei Hourly-Precip: %s: %s", zip_url, e)
        return None
    
    # Filtere nur HEUTE
    heute_str = datetime.now(zoneinfo.ZoneInfo("Europe/Berlin")).strftime("%Y%m%d")
    precip_sum = 0.0
    
    for row in rows:
        datum_str = row.get("MESS_DATUM", "")
        if not datum_str.startswith(heute_str):
            continue
        
        precip = to_float_or_none(row.get("R1", ""))
        if precip is not None and precip >= 0:
            precip_sum += precip
    
    return round(precip_sum, 1) if precip_sum > 0 else 0.0


# ---------------------------------------------------------------------------
# Aggregationen
# ---------------------------------------------------------------------------

def compute_sommertage_pro_jahr(daily):
    counts = {}
    for row in daily:
        if row["txk"] is None:
            continue
        j = int(row["datum"][:4])
        if row["txk"] >= SOMMERTAG_SCHWELLE_C:
            counts[j] = counts.get(j, 0) + 1
        else:
            counts.setdefault(j, counts.get(j, 0))
    return counts


def compute_niederschlag_letzte_30_tage(daily):
    if not daily:
        return None
    heute = datetime.now(timezone.utc).date()
    grenze = heute - timedelta(days=30)
    summe, gefunden = 0.0, False
    for row in daily:
        d = datetime.strptime(row["datum"], "%Y%m%d").date()
        if grenze <= d <= heute and row["rsk"] is not None:
            summe += row["rsk"]
            gefunden = True
    return round(summe, 1) if gefunden else None


def compute_sommertage_laufendes_jahr(daily):
    jetzt_jahr = datetime.now(timezone.utc).year
    return sum(1 for r in daily if r["txk"] is not None and r["txk"] >= SOMMERTAG_SCHWELLE_C and int(r["datum"][:4]) == jetzt_jahr)


def tageshoechstwert_heute(daily):
    return daily[-1]["txk"] if daily else None


def compute_referenzmittel(daily, jahr_von, jahr_bis):
    relevante = [r for r in daily if jahr_von <= int(r["datum"][:4]) <= jahr_bis]
    sommertage_by_jahr = compute_sommertage_pro_jahr(relevante)
    jahre = [j for j in sommertage_by_jahr if jahr_von <= j <= jahr_bis]
    if not jahre:
        return {"sommertage_mittel": None, "niederschlag_jahresmittel_mm": None, "jahre_verfuegbar": 0}
    mittel = sum(sommertage_by_jahr[j] for j in jahre) / len(jahre)
    nied = [r["rsk"] for r in relevante if r["rsk"] is not None]
    nied_mittel = round(sum(nied) / len(jahre), 1) if nied and jahre else None
    return {"sommertage_mittel": round(mittel, 1), "niederschlag_jahresmittel_mm": nied_mittel, "jahre_verfuegbar": len(jahre)}


# ---------------------------------------------------------------------------
# NEU: Tagesreihe mit HEUTE ergänzen
# ---------------------------------------------------------------------------

def extract_tagesreihe_30d(daily: list[dict], heute_temp: Optional[dict], heute_precip: Optional[float]) -> list[dict]:
    """
    Letzte TAGESREIHE_TAGE Tage als Liste [{datum, txk, rsk}, ...].
    HEUTE wird ergänzt mit stündlichen Daten (nur wenn höhere Temperatur!).
    """
    heute = datetime.now(timezone.utc).date()
    grenze = heute - timedelta(days=TAGESREIHE_TAGE)
    result = []
    
    for row in daily:
        d = datetime.strptime(row["datum"], "%Y%m%d").date()
        if grenze <= d < heute:  # Bis GESTERN (nicht heute!)
            result.append({
                "datum": row["datum"],
                "txk": row["txk"],
                "rsk": row["rsk"],
            })
    
    # HEUTE ergänzen (aus stündlichen Daten)
    heute_str = heute.strftime("%Y%m%d")
    
    # Prüfe ob HEUTE schon in daily existiert
    existing_today = next((r for r in daily if r["datum"] == heute_str), None)
    
    if heute_temp and heute_temp["max_temp"] is not None:
        # Nur überschreiben wenn HÖHER!
        if existing_today and existing_today["txk"] is not None:
            txk_final = max(existing_today["txk"], heute_temp["max_temp"])
            log.info("HEUTE (%s): Tageswert %.1f°C vs. Stündlich %.1f°C → %.1f°C", 
                     heute_str, existing_today["txk"], heute_temp["max_temp"], txk_final)
        else:
            txk_final = heute_temp["max_temp"]
            log.info("HEUTE (%s): Stündlich %.1f°C (kein Tageswert)", heute_str, txk_final)
    else:
        txk_final = existing_today["txk"] if existing_today else None
    
    if heute_precip is not None:
        rsk_final = heute_precip
        if existing_today and existing_today["rsk"] is not None:
            rsk_final = max(existing_today["rsk"], heute_precip)
    else:
        rsk_final = existing_today["rsk"] if existing_today else None
    
    result.append({
        "datum": heute_str,
        "txk": txk_final,
        "rsk": rsk_final,
    })
    
    return result


def get_tage_fenster() -> list[tuple[int, int]]:
    """Erstellt Liste der letzten 30 Kalendertage."""
    heute = datetime.now(timezone.utc).date()
    grenze = heute - timedelta(days=TAGESREIHE_TAGE)
    fenster = []
    d = grenze
    while d <= heute:
        fenster.append((d.month, d.day))
        d += timedelta(days=1)
    return fenster


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    k = (n - 1) * p
    f = int(k)
    c = f + 1
    if c >= n:
        return sorted_vals[-1]
    return sorted_vals[f] + (k - f) * (sorted_vals[c] - sorted_vals[f])


def compute_referenzband_temperatur(daily: list[dict], jahr_von: int, jahr_bis: int, tage_fenster: list[tuple[int, int]]) -> list[dict]:
    by_md: dict[tuple[int,int], list[float]] = {md: [] for md in tage_fenster}
    for row in daily:
        if row["txk"] is None:
            continue
        j = int(row["datum"][:4])
        if not (jahr_von <= j <= jahr_bis):
            continue
        m = int(row["datum"][4:6])
        d = int(row["datum"][6:8])
        key = (m, d)
        if key in by_md:
            by_md[key].append(row["txk"])
    
    result = []
    for (m, d) in tage_fenster:
        vals = sorted(by_md[(m, d)])
        if vals:
            result.append({
                "tag": f"{m:02d}-{d:02d}",
                "mean": round(stat_mean(vals), 1),
                "p10": round(_percentile(vals, 0.10), 1),
                "p90": round(_percentile(vals, 0.90), 1),
            })
        else:
            result.append({"tag": f"{m:02d}-{d:02d}", "mean": None, "p10": None, "p90": None})
    return result


def compute_referenzband_niederschlag(daily: list[dict], jahr_von: int, jahr_bis: int, tage_fenster: list[tuple[int, int]]) -> list[dict]:
    jahre_dict: dict[int, dict[tuple[int,int], float]] = {}
    for row in daily:
        j = int(row["datum"][:4])
        if not (jahr_von <= j <= jahr_bis):
            continue
        m = int(row["datum"][4:6])
        d = int(row["datum"][6:8])
        r = row["rsk"] if row["rsk"] is not None else 0.0
        if j not in jahre_dict:
            jahre_dict[j] = {}
        jahre_dict[j][(m, d)] = jahre_dict[j].get((m, d), 0.0) + r
    
    jahre_keys = sorted(jahre_dict.keys())
    kum_by_jahr: dict[int, list[float]] = {j: [] for j in jahre_keys}
    
    for j in jahre_keys:
        kum = 0.0
        for (m, d) in tage_fenster:
            kum += jahre_dict[j].get((m, d), 0.0)
            kum_by_jahr[j].append(kum)
    
    n_tage = len(tage_fenster)
    result = []
    for i, (m, d) in enumerate(tage_fenster):
        vals = [kum_by_jahr[j][i] for j in jahre_keys if i < len(kum_by_jahr[j])]
        if vals:
            vals_sorted = sorted(vals)
            result.append({
                "tag": f"{m:02d}-{d:02d}",
                "kum_mean": round(stat_mean(vals), 1),
                "kum_p10": round(_percentile(vals_sorted, 0.10), 1),
                "kum_p90": round(_percentile(vals_sorted, 0.90), 1),
            })
        else:
            result.append({"tag": f"{m:02d}-{d:02d}", "kum_mean": None, "kum_p10": None, "kum_p90": None})
    return result


def load_latest_hourly_temperature(hourly_station_id: Optional[str]) -> dict:
    if not hourly_station_id:
        return {"wert": None, "zeitpunkt": None}
    try:
        html = fetch_text(HOURLY_TEMP_URL)
        pattern = f"stundenwerte_TU_{hourly_station_id}"
        matches = [l for l in find_zip_links(html, "stundenwerte_TU_") if l.startswith(pattern)]
        if not matches:
            return {"wert": None, "zeitpunkt": None}
        zip_url = HOURLY_TEMP_URL + matches[0]
        rows = read_produkt_file_from_zip(fetch(zip_url))
        if not rows:
            return {"wert": None, "zeitpunkt": None}
        valid = [(r.get("MESS_DATUM"), to_float_or_none(r.get("TT_TU", ""))) for r in rows]
        valid = [(dt, t) for (dt, t) in valid if dt and t is not None]
        if not valid:
            return {"wert": None, "zeitpunkt": None}
        latest = max(valid, key=lambda x: x[0])
        return {"wert": round(latest[1], 1), "zeitpunkt": latest[0]}
    except Exception as e:
        log.warning("Fehler beim Laden stündlicher Temperatur: %s", e)
        return {"wert": None, "zeitpunkt": None}


# ---------------------------------------------------------------------------
# Vorheriges Ergebnis
# ---------------------------------------------------------------------------

def load_previous_result():
    if OUTPUT_FILE.exists():
        try:
            return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"generated_at": None, "stations": {}}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    previous = load_previous_result()
    station_meta = load_or_build_station_meta()
    result_stations = dict(previous.get("stations", {}))
    any_success = False
    tage_fenster = get_tage_fenster()

    for station_id, display_name in STATIONS.items():
        log.info("=== Station %s (%s) ===", station_id, display_name)
        meta = station_meta.get(station_id)
        if meta is None:
            continue

        try:
            daily = load_daily_series(station_id)
            if not daily:
                raise ValueError("Keine Tageswerte")

            # HEUTE: Stündliche Daten laden
            hourly_temp_station = meta.get("hourly_temp_station_id", station_id)
            heute_temp = load_hourly_temp_today(hourly_temp_station)
            heute_precip = load_hourly_precip_today(hourly_temp_station)
            
            if heute_temp:
                log.info("HEUTE Temperatur (stündlich): %.1f°C um %s", 
                        heute_temp["max_temp"], heute_temp["timestamp"])
            if heute_precip is not None:
                log.info("HEUTE Niederschlag (stündlich): %.1f mm", heute_precip)

            sommertage_jahr = compute_sommertage_pro_jahr(daily)
            verfuegbar_ab = min(sommertage_jahr.keys()) if sommertage_jahr else None
            start_jahr = max(STATS_START_JAHR, verfuegbar_ab) if verfuegbar_ab else STATS_START_JAHR
            sommertage_serie = {
                str(j): sommertage_jahr.get(j, 0)
                for j in range(start_jahr, datetime.now(timezone.utc).year + 1)
                if j in sommertage_jahr
            }

            referenz = {
                "1961-1990": compute_referenzmittel(daily, 1961, 1990),
                "1991-2020": compute_referenzmittel(daily, 1991, 2020),
            }

            hourly_temp = load_latest_hourly_temperature(meta.get("hourly_temp_station_id"))

            # NEU: Tagesreihe MIT HEUTE (stündliche Daten)
            tagesreihe = extract_tagesreihe_30d(daily, heute_temp, heute_precip)

            ref_temp = {}
            ref_nied = {}
            for periode, (jv, jb) in [("1961-1990", (1961, 1990)), ("1991-2020", (1991, 2020))]:
                ref_temp[periode] = compute_referenzband_temperatur(daily, jv, jb, tage_fenster)
                ref_nied[periode] = compute_referenzband_niederschlag(daily, jv, jb, tage_fenster)

            # Tageshoechstwert: aus tagesreihe (inkl. HEUTE)
            tageshoechstwert = tagesreihe[-1]["txk"] if tagesreihe else None

            station_result = {
                "name": display_name,
                "lat": meta["lat"], "lon": meta["lon"], "hoehe_m": meta["hoehe"],
                "sommertage_pro_jahr": sommertage_serie,
                "sommertage_laufendes_jahr": compute_sommertage_laufendes_jahr(daily),
                "tageshoechstwert_heute_c": tageshoechstwert,
                "niederschlag_letzte_30_tage_mm": compute_niederschlag_letzte_30_tage(daily),
                "aktuelle_temperatur": hourly_temp,
                "hourly_temp_distance_km": meta.get("hourly_temp_distance_km"),
                "referenzperioden": referenz,
                "tagesreihe_30d": tagesreihe,
                "referenzband_temperatur": ref_temp,
                "referenzband_niederschlag": ref_nied,
                "letztes_tageswert_datum": daily[-1]["datum"] if daily else None,
                "status": "ok",
                "fehler": None,
            }
            result_stations[station_id] = station_result
            any_success = True
            log.info("Station %s OK.", station_id)

        except Exception as e:
            log.exception("Fehler bei %s: %s", station_id, e)
            if station_id in result_stations:
                result_stations[station_id]["status"] = "stale"
                result_stations[station_id]["fehler"] = str(e)
            else:
                result_stations[station_id] = {"name": display_name, "status": "error", "fehler": str(e)}

    if not any_success:
        log.error("Kein Update erfolgreich. Datei wird NICHT ueberschrieben.")
        sys.exit(1)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sommertag_schwelle_c": SOMMERTAG_SCHWELLE_C,
        "stations": result_stations,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Fertig. %d Stationen in %s", len(result_stations), OUTPUT_FILE)


if __name__ == "__main__":
    main()
