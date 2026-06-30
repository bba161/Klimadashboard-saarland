#!/usr/bin/env python3
"""
update_data.py (v2)
=====================
Erweitert um tägliche Zeitreihen (letzte 30 Tage) für Temperatur (TXK)
und Niederschlag (RSK), plus historische Referenzbänder für diese
30-Tage-Fenster, damit das Frontend Charts im Stil der Stuttgarter
Klimazentrale zeichnen kann.

Neue Felder pro Station in saarland.json:
  - tagesreihe_30d: [{datum, txk, rsk}, ...]  (letzte 30 Tage, sortiert)
  - referenzband_temperatur: {
      "1961-1990": [{tag, mean, p10, p90}, ...],
      "1991-2020": [{tag, mean, p10, p90}, ...]
    }
  - referenzband_niederschlag: {
      "1961-1990": [{tag, kum_mean, kum_p10, kum_p90}, ...],
      "1991-2020": [{tag, kum_mean, kum_p10, kum_p90}, ...]
    }
  Die Referenzbänder decken denselben Kalendertag-Bereich ab wie die
  tagesreihe_30d (z.B. 1. Juni bis 30. Juni), berechnet aus allen
  verfügbaren Jahren der jeweiligen Referenzperiode.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone, date
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

session = requests.Session()
session.headers.update({"User-Agent": "saarland-klimadashboard/1.0 (Zeitungsprojekt)"})


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def fetch(url: str) -> bytes:
    log.info("GET %s", url)
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


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
            von = parts[1].strip()
            bis = parts[2].strip()
            hoehe = float(parts[3])
            lat = float(parts[4])
            lon = float(parts[5])
        except ValueError:
            continue
        rest = parts[6] if len(parts) > 6 else ""
        if len(parts) > 7:
            rest = parts[6] + " " + parts[7]
        result[station_id] = {
            "name_raw": rest.strip(),
            "lat": lat, "lon": lon, "hoehe": hoehe,
            "von_datum": von, "bis_datum": bis,
        }
    return result


def find_zip_links(html: str, prefix: str) -> list[str]:
    return re.findall(rf'href="({re.escape(prefix)}[^"]+\.zip)"', html)


def read_produkt_file_from_zip(zip_bytes: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        produkt_name = next((n for n in zf.namelist() if n.lower().startswith("produkt_")), None)
        if produkt_name is None:
            raise ValueError("Keine produkt_*.txt in ZIP gefunden")
        with zf.open(produkt_name) as f:
            text = f.read().decode("latin-1", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    rows = []
    for row in reader:
        clean = {k.strip(): v.strip() for k, v in row.items() if k}
        rows.append(clean)
    return rows


def to_float_or_none(value: str) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f <= -999:
        return None
    return f


# ---------------------------------------------------------------------------
# Stationsmetadaten
# ---------------------------------------------------------------------------

def load_or_build_station_meta() -> dict[str, dict]:
    if STATIONS_META_CACHE.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            STATIONS_META_CACHE.stat().st_mtime, tz=timezone.utc
        )
        if age < timedelta(hours=24):
            log.info("Verwende gecachte Stationsmetadaten (%s)", STATIONS_META_CACHE)
            return json.loads(STATIONS_META_CACHE.read_text(encoding="utf-8"))

    log.info("Baue Stationsmetadaten neu auf...")
    kl_list = parse_dwd_station_list(fetch_text(KL_STATIONS_LIST_URL))
    tu_list = parse_dwd_station_list(fetch_text(TU_STATIONS_LIST_URL))
    rr_list = parse_dwd_station_list(fetch_text(RR_STATIONS_LIST_URL))

    meta: dict[str, dict] = {}
    for station_id, display_name in STATIONS.items():
        kl_info = kl_list.get(station_id)
        if kl_info is None:
            log.error("Station %s (%s) nicht in DWD-Stationsliste gefunden!", station_id, display_name)
            continue
        lat, lon = kl_info["lat"], kl_info["lon"]
        nearest_tu = _nearest_station(lat, lon, tu_list)
        nearest_rr = _nearest_station(lat, lon, rr_list)
        meta[station_id] = {
            "name": display_name, "lat": lat, "lon": lon, "hoehe": kl_info["hoehe"],
            "hourly_temp_station_id": nearest_tu[0] if nearest_tu and nearest_tu[1] <= MAX_HOURLY_STATION_DISTANCE_KM else None,
            "hourly_temp_distance_km": round(nearest_tu[1], 1) if nearest_tu else None,
            "hourly_precip_station_id": nearest_rr[0] if nearest_rr and nearest_rr[1] <= MAX_HOURLY_STATION_DISTANCE_KM else None,
            "hourly_precip_distance_km": round(nearest_rr[1], 1) if nearest_rr else None,
        }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIONS_META_CACHE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _nearest_station(lat, lon, candidates):
    best_id, best_dist = None, float("inf")
    for sid, info in candidates.items():
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
# Aggregationen (bestehend)
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
# NEU: Tägliche Zeitreihe + Referenzbänder für die letzten 30 Tage
# ---------------------------------------------------------------------------

def extract_tagesreihe_30d(daily: list[dict]) -> list[dict]:
    """Letzte TAGESREIHE_TAGE Tage als Liste [{datum, txk, rsk}, ...]."""
    heute = datetime.now(timezone.utc).date()
    grenze = heute - timedelta(days=TAGESREIHE_TAGE)
    result = []
    for row in daily:
        d = datetime.strptime(row["datum"], "%Y%m%d").date()
        if grenze <= d <= heute:
            result.append({
                "datum": row["datum"],
                "txk": row["txk"],
                "rsk": row["rsk"],
            })
    return result


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Einfache Perzentil-Berechnung (linearer Interpolation)."""
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
    """
    Für jeden (Monat, Tag) in tage_fenster: berechne Mean, P10, P90 von TXK
    über alle Jahre der Referenzperiode.
    tage_fenster = [(6,1), (6,2), ..., (6,30)] z.B.
    """
    # Index: (monat, tag) -> [txk-Werte über Jahre]
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
    for md in tage_fenster:
        vals = sorted(by_md.get(md, []))
        if not vals:
            result.append({"tag": f"{md[0]:02d}-{md[1]:02d}", "mean": None, "p10": None, "p90": None})
        else:
            result.append({
                "tag": f"{md[0]:02d}-{md[1]:02d}",
                "mean": round(stat_mean(vals), 1),
                "p10": round(_percentile(vals, 0.10), 1),
                "p90": round(_percentile(vals, 0.90), 1),
            })
    return result


def compute_referenzband_niederschlag(daily: list[dict], jahr_von: int, jahr_bis: int, tage_fenster: list[tuple[int, int]]) -> list[dict]:
    """
    Kumulierter Niederschlag über das 30-Tage-Fenster, pro Referenzjahr,
    dann Mean/P10/P90 der kumulierten Werte pro Tag-Position.
    """
    # Pro Jahr: kumulierte Niederschlagsreihe über das Fenster
    jahre_range = range(jahr_von, jahr_bis + 1)
    # Index: jahr -> {(m,d): rsk}
    by_year_md: dict[int, dict[tuple[int,int], float]] = {}
    for row in daily:
        if row["rsk"] is None:
            continue
        j = int(row["datum"][:4])
        if j not in jahre_range:
            continue
        m = int(row["datum"][4:6])
        d = int(row["datum"][6:8])
        key = (m, d)
        if key in set(tage_fenster):
            by_year_md.setdefault(j, {})[key] = row["rsk"]

    # Pro Jahr kumulierte Reihe
    kum_by_year: dict[int, list[float]] = {}
    for j in jahre_range:
        year_data = by_year_md.get(j, {})
        kum = []
        running = 0.0
        for md in tage_fenster:
            running += year_data.get(md, 0.0)
            kum.append(running)
        if any(md in year_data for md in tage_fenster):
            kum_by_year[j] = kum

    result = []
    for i, md in enumerate(tage_fenster):
        vals = sorted([kum_by_year[j][i] for j in kum_by_year])
        if not vals:
            result.append({"tag": f"{md[0]:02d}-{md[1]:02d}", "kum_mean": None, "kum_p10": None, "kum_p90": None})
        else:
            result.append({
                "tag": f"{md[0]:02d}-{md[1]:02d}",
                "kum_mean": round(stat_mean(vals), 1),
                "kum_p10": round(_percentile(vals, 0.10), 1),
                "kum_p90": round(_percentile(vals, 0.90), 1),
            })
    return result


def get_tage_fenster() -> list[tuple[int, int]]:
    """Gibt die (Monat, Tag)-Tupel für die letzten TAGESREIHE_TAGE Tage zurück."""
    heute = datetime.now(timezone.utc).date()
    result = []
    for i in range(TAGESREIHE_TAGE + 1):
        d = heute - timedelta(days=TAGESREIHE_TAGE - i)
        result.append((d.month, d.day))
    return result

# ---------------------------------------------------------------------------
# Stundenwerte
# ---------------------------------------------------------------------------

def load_latest_hourly_temperature(hourly_station_id):
    if hourly_station_id is None:
        return None
    try:
        html = fetch_text(HOURLY_TEMP_URL)
        matches = find_zip_links(html, f"stundenwerte_TU_{hourly_station_id}")
        if not matches:
            return None
        zip_bytes = fetch(HOURLY_TEMP_URL + matches[0])
        rows = read_produkt_file_from_zip(zip_bytes)
        if not rows:
            return None
        last = rows[-1]
        temp = to_float_or_none(last.get("TT_TU", ""))
        if temp is None:
            return None
        return {"wert": temp, "zeitpunkt": last.get("MESS_DATUM", "")}
    except (requests.RequestException, ValueError, zipfile.BadZipFile) as e:
        log.error("Stundentemp %s: %s", hourly_station_id, e)
        return None


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

            # NEU: Tagesreihe + Referenzbänder
            tagesreihe = extract_tagesreihe_30d(daily)

            ref_temp = {}
            ref_nied = {}
            for periode, (jv, jb) in [("1961-1990", (1961, 1990)), ("1991-2020", (1991, 2020))]:
                ref_temp[periode] = compute_referenzband_temperatur(daily, jv, jb, tage_fenster)
                ref_nied[periode] = compute_referenzband_niederschlag(daily, jv, jb, tage_fenster)

            station_result = {
                "name": display_name,
                "lat": meta["lat"], "lon": meta["lon"], "hoehe_m": meta["hoehe"],
                "sommertage_pro_jahr": sommertage_serie,
                "sommertage_laufendes_jahr": compute_sommertage_laufendes_jahr(daily),
                "tageshoechstwert_heute_c": tageshoechstwert_heute(daily),
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
    log.info("Geschrieben: %s", OUTPUT_FILE)


if __name__ == "__main__":
    main()
