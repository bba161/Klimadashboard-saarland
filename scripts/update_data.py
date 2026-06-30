#!/usr/bin/env python3
"""
update_data.py
================
Lädt für die im Saarland-Klimadashboard definierten DWD-Stationen die
relevanten Klimadaten herunter und aggregiert sie in einer einzigen
JSON-Datei (data/saarland.json), die vom statischen Frontend gelesen wird.

Aufgerufen wird dieses Skript stündlich durch die GitHub Action
(.github/workflows/update.yml). Es ist bewusst so geschrieben, dass ein
Fehler bei EINER Station nicht den gesamten Lauf abbricht: jede Station
wird einzeln verarbeitet, Fehler werden geloggt und die betroffene Station
behält im Ergebnis den letzten bekannten Stand (siehe `load_previous_result`).

DWD-Datenquellen (https://opendata.dwd.de/climate_environment/CDC/):
  - observations_germany/climate/daily/kl/historical/  (Tageswerte, geprüft)
  - observations_germany/climate/daily/kl/recent/      (Tageswerte, ungeprüft, ~tagesaktuell)
  - observations_germany/climate/hourly/air_temperature/recent/ (Stundenwerte Temperatur)
  - observations_germany/climate/hourly/precipitation/recent/   (Stundenwerte Niederschlag)
  - observations_germany/climate/multi_annual/                  (vieljährige Mittelwerte)

Wichtige Spalten in den KL-Tageswerten (produkt_klima_tag_*.txt):
  MESS_DATUM : Datum (YYYYMMDD)
  TXK        : Tagesmaximum der Lufttemperatur in 2m Höhe (°C)
  RSK        : Tagesniederschlagshöhe (mm)
  Fehlwerte werden vom DWD als -999 codiert.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

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

session = requests.Session()
session.headers.update({"User-Agent": "saarland-klimadashboard/1.0 (Zeitungsprojekt)"})


# ---------------------------------------------------------------------------
# Hilfsfunktionen: HTTP, Parsing, Geometrie
# ---------------------------------------------------------------------------

def fetch(url: str) -> bytes:
    log.info("GET %s", url)
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def fetch_text(url: str) -> str:
    raw = fetch(url)
    # DWD-Stationslisten sind latin-1 kodiert
    return raw.decode("latin-1", errors="replace")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import radians, sin, cos, sqrt, atan2
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def parse_dwd_station_list(text: str) -> dict[str, dict]:
    """
    Parst eine DWD-"Beschreibung_Stationen.txt"-Datei in ein Dict
    {station_id: {name, lat, lon, von, bis, bundesland}}.

    Format (fixed-width, durch Leerzeichen getrennt, Header mit Bindestrichen
    als zweite Zeile):
    Stations_id von_datum bis_datum Stationshoehe geoBreite geoLaenge Stationsname Bundesland Abgabe
    """
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
            "lat": lat,
            "lon": lon,
            "hoehe": hoehe,
            "von_datum": von,
            "bis_datum": bis,
        }
    return result


def find_zip_links(html: str, prefix: str) -> list[str]:
    """Extrahiert Dateinamen aus einem DWD-Verzeichnislisting (simples HTML)."""
    return re.findall(rf'href="({re.escape(prefix)}[^"]+\.zip)"', html)


def read_produkt_file_from_zip(zip_bytes: bytes) -> list[dict]:
    """
    Liest die "produkt_*.txt" aus einer DWD-ZIP-Datei und gibt eine Liste
    von Dicts (eine pro Zeile/Tag bzw. Stunde) zurück, mit gestripten Keys.
    """
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
# Schritt 1: Stationsmetadaten (Koordinaten) ermitteln + Stundenstation matchen
# ---------------------------------------------------------------------------

def load_or_build_station_meta() -> dict[str, dict]:
    """
    Liefert für jede der 11 Saarland-Stationen: lat, lon, hoehe, sowie die
    ID der nächstgelegenen Stundenwert-Station für Temperatur und
    Niederschlag (falls innerhalb von MAX_HOURLY_STATION_DISTANCE_KM).

    Ergebnis wird in data/stations_meta.json gecacht (24h), damit nicht bei
    jedem stündlichen Lauf die kompletten Stationslisten neu geladen werden.
    """
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
            log.error(
                "Station %s (%s) nicht in DWD-Tageswerte-Stationsliste gefunden! Bitte ID prüfen.",
                station_id, display_name,
            )
            continue

        lat, lon = kl_info["lat"], kl_info["lon"]
        nearest_tu = _nearest_station(lat, lon, tu_list)
        nearest_rr = _nearest_station(lat, lon, rr_list)

        meta[station_id] = {
            "name": display_name,
            "lat": lat,
            "lon": lon,
            "hoehe": kl_info["hoehe"],
            "hourly_temp_station_id": nearest_tu[0] if nearest_tu and nearest_tu[1] <= MAX_HOURLY_STATION_DISTANCE_KM else None,
            "hourly_temp_distance_km": round(nearest_tu[1], 1) if nearest_tu else None,
            "hourly_precip_station_id": nearest_rr[0] if nearest_rr and nearest_rr[1] <= MAX_HOURLY_STATION_DISTANCE_KM else None,
            "hourly_precip_distance_km": round(nearest_rr[1], 1) if nearest_rr else None,
        }
        log.info(
            "Station %s (%s): TU=%s (%.1f km), RR=%s (%.1f km)",
            station_id, display_name,
            meta[station_id]["hourly_temp_station_id"], nearest_tu[1] if nearest_tu else -1,
            meta[station_id]["hourly_precip_station_id"], nearest_rr[1] if nearest_rr else -1,
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIONS_META_CACHE.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _nearest_station(lat: float, lon: float, candidates: dict[str, dict]) -> Optional[tuple[str, float]]:
    best_id, best_dist = None, float("inf")
    for sid, info in candidates.items():
        d = haversine_km(lat, lon, info["lat"], info["lon"])
        if d < best_dist:
            best_id, best_dist = sid, d
    if best_id is None:
        return None
    return best_id, best_dist


# ---------------------------------------------------------------------------
# Schritt 2: Tageswerte laden (historical + recent kombiniert) -> Sommertage,
# Niederschlag, Tagesmaximum-Zeitreihe
# ---------------------------------------------------------------------------

def load_daily_series(station_id: str) -> list[dict]:
    """
    Lädt alle verfügbaren Tageswerte (historical + recent) für eine Station
    und gibt eine sortierte Liste von {datum, txk, rsk} zurück.
    """
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
            log.info("Keine Datei für %s unter %s gefunden (%s)", station_id, base_url, suffix)
            continue

        zip_url = base_url + matches[0]
        try:
            zip_bytes = fetch(zip_url)
            rows = read_produkt_file_from_zip(zip_bytes)
        except (requests.RequestException, ValueError, zipfile.BadZipFile) as e:
            log.error("Fehler beim Laden/Parsen von %s: %s", zip_url, e)
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

    # Duplikate (Überlappung historical/recent) entfernen: recent gewinnt,
    # da es zuletzt in rows_all eingefügt wird.
    by_date: dict[str, dict] = {}
    for row in rows_all:
        by_date[row["datum"]] = row

    return [by_date[d] for d in sorted(by_date.keys())]


def compute_sommertage_pro_jahr(daily: list[dict]) -> dict[int, int]:
    """Zählt pro Jahr die Tage mit TXK >= SOMMERTAG_SCHWELLE_C."""
    counts: dict[int, int] = {}
    for row in daily:
        if row["txk"] is None:
            continue
        jahr = int(row["datum"][:4])
        if row["txk"] >= SOMMERTAG_SCHWELLE_C:
            counts[jahr] = counts.get(jahr, 0) + 1
        else:
            counts.setdefault(jahr, counts.get(jahr, 0))
    return counts


def compute_niederschlag_letzte_30_tage(daily: list[dict]) -> Optional[float]:
    if not daily:
        return None
    heute = datetime.now(timezone.utc).date()
    grenze = heute - timedelta(days=30)
    summe = 0.0
    gefunden = False
    for row in daily:
        d = datetime.strptime(row["datum"], "%Y%m%d").date()
        if grenze <= d <= heute and row["rsk"] is not None:
            summe += row["rsk"]
            gefunden = True
    return round(summe, 1) if gefunden else None


def compute_sommertage_laufendes_jahr(daily: list[dict]) -> int:
    jetzt_jahr = datetime.now(timezone.utc).year
    return sum(
        1 for row in daily
        if row["txk"] is not None
        and row["txk"] >= SOMMERTAG_SCHWELLE_C
        and int(row["datum"][:4]) == jetzt_jahr
    )


def tageshoechstwert_heute(daily: list[dict]) -> Optional[float]:
    if not daily:
        return None
    return daily[-1]["txk"]


# ---------------------------------------------------------------------------
# Schritt 3: Stundenwerte für "aktuelle Temperatur" (jüngster verfügbarer Wert)
# ---------------------------------------------------------------------------

def load_latest_hourly_temperature(hourly_station_id: Optional[str]) -> Optional[dict]:
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
        ts_raw = last.get("MESS_DATUM", "")  # Format YYYYMMDDHH
        return {"wert": temp, "zeitpunkt": ts_raw}
    except (requests.RequestException, ValueError, zipfile.BadZipFile) as e:
        log.error("Fehler beim Laden der Stundentemperatur für %s: %s", hourly_station_id, e)
        return None


# ---------------------------------------------------------------------------
# Schritt 4: Referenzmittelwerte für Klimavergleich
# ---------------------------------------------------------------------------
# Hinweis: Der DWD multi_annual-Datensatz nutzt eigene Stations-IDs, die
# nicht 1:1 den kl-Stations-IDs entsprechen. Für ein robustes erstes Release
# wird das Referenzmittel daher direkt aus der historischen Tageswerte-Serie
# der jeweiligen Station für die gewünschte Periode berechnet (sofern genug
# Jahre mit Daten vorhanden sind). Das ist meteorologisch korrekt und macht
# das System unabhängig von einer zusätzlichen Stationszuordnung.

def compute_referenzmittel(daily: list[dict], jahr_von: int, jahr_bis: int) -> dict:
    relevante = [r for r in daily if jahr_von <= int(r["datum"][:4]) <= jahr_bis]
    sommertage_by_jahr = compute_sommertage_pro_jahr(relevante)
    jahre_mit_daten = [j for j in sommertage_by_jahr if jahr_von <= j <= jahr_bis]
    if not jahre_mit_daten:
        return {"sommertage_mittel": None, "niederschlag_jahresmittel_mm": None, "jahre_verfuegbar": 0}

    mittel = sum(sommertage_by_jahr[j] for j in jahre_mit_daten) / len(jahre_mit_daten)

    niederschlag_werte = [r["rsk"] for r in relevante if r["rsk"] is not None]
    nied_mittel_jahr = None
    if niederschlag_werte and jahre_mit_daten:
        nied_mittel_jahr = round(sum(niederschlag_werte) / len(jahre_mit_daten), 1)

    return {
        "sommertage_mittel": round(mittel, 1),
        "niederschlag_jahresmittel_mm": nied_mittel_jahr,
        "jahre_verfuegbar": len(jahre_mit_daten),
    }


# ---------------------------------------------------------------------------
# Vorheriges Ergebnis laden (Fallback bei Fehlern einzelner Stationen)
# ---------------------------------------------------------------------------

def load_previous_result() -> dict:
    if OUTPUT_FILE.exists():
        try:
            return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Bestehende %s ist beschädigt, ignoriere sie.", OUTPUT_FILE)
    return {"generated_at": None, "stations": {}}


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------

def main() -> None:
    previous = load_previous_result()
    station_meta = load_or_build_station_meta()

    result_stations: dict[str, dict] = dict(previous.get("stations", {}))
    any_success = False

    for station_id, display_name in STATIONS.items():
        log.info("=== Verarbeite Station %s (%s) ===", station_id, display_name)
        meta = station_meta.get(station_id)
        if meta is None:
            log.error("Keine Metadaten für %s, überspringe (Fallback: alter Stand bleibt).", station_id)
            continue

        try:
            daily = load_daily_series(station_id)
            if not daily:
                raise ValueError("Keine Tageswerte erhalten")

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

            station_result = {
                "name": display_name,
                "lat": meta["lat"],
                "lon": meta["lon"],
                "hoehe_m": meta["hoehe"],
                "sommertage_pro_jahr": sommertage_serie,
                "sommertage_laufendes_jahr": compute_sommertage_laufendes_jahr(daily),
                "tageshoechstwert_heute_c": tageshoechstwert_heute(daily),
                "niederschlag_letzte_30_tage_mm": compute_niederschlag_letzte_30_tage(daily),
                "aktuelle_temperatur": hourly_temp,
                "hourly_temp_distance_km": meta.get("hourly_temp_distance_km"),
                "referenzperioden": referenz,
                "letztes_tageswert_datum": daily[-1]["datum"] if daily else None,
                "status": "ok",
                "fehler": None,
            }
            result_stations[station_id] = station_result
            any_success = True
            log.info("Station %s erfolgreich aktualisiert.", station_id)

        except Exception as e:  # noqa: BLE001 - einzelne Station darf den Lauf nicht killen
            log.exception("Fehler bei Station %s (%s): %s", station_id, display_name, e)
            if station_id in result_stations:
                result_stations[station_id]["status"] = "stale"
                result_stations[station_id]["fehler"] = str(e)
            else:
                result_stations[station_id] = {
                    "name": display_name,
                    "status": "error",
                    "fehler": str(e),
                }

    if not any_success:
        log.error("Kein einziger Stationsupdate war erfolgreich. Datei wird NICHT überschrieben.")
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
