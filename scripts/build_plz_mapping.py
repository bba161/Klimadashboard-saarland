#!/usr/bin/env python3
"""
build_plz_mapping.py
======================
Erzeugt data/plz_mapping.json: für jede Saarland-Postleitzahl die
nächstgelegene der 11 DWD-Stationen (per Luftlinie).

Primärquelle ist der "postal-codes-json-xml-csv"-Datensatz von zauberware
(github.com/zauberware/postal-codes-json-xml-csv, CC BY 4.0), da dessen
URL nachweislich funktioniert (ZIP-Archiv mit JSON).

Als Fallback dient das Opendatasoft-Dataset "georef-germany-postleitzahl"
(data.opendatasoft.com, ODbL-Lizenz).

Das Skript benötigt die DWD-Stationskoordinaten, die bereits von
update_data.py in data/stations_meta.json abgelegt wurden. Es sollte
deshalb NACH update_data.py ausgeführt werden (siehe GitHub Workflow).
"""

from __future__ import annotations

import io
import json
import logging
import sys
import zipfile
from pathlib import Path
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).parent))
from stations import STATIONS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("build_plz_mapping")

DATA_DIR = Path(__file__).parent.parent / "data"
STATIONS_META_CACHE = DATA_DIR / "stations_meta.json"
OUTPUT_FILE = DATA_DIR / "plz_mapping.json"

# Primär: zauberware ZIP-Archiv (verifiziert funktionierend)
ZAUBERWARE_PRIMARY_URL = (
    "https://raw.githubusercontent.com/zauberware/postal-codes-json-xml-csv/"
    "master/data/DE.zip"
)

# Fallback: Opendatasoft (korrigierte Domain + @public-Suffix)
OPENDATASOFT_FALLBACK_URL = (
    "https://data.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
    "georef-germany-postleitzahl@public/records"
    "?where=lan_name%3D%22Saarland%22&limit=100"
)

REQUEST_TIMEOUT = 60
session = requests.Session()
session.headers.update({"User-Agent": "saarland-klimadashboard/1.0 (Zeitungsprojekt)"})


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import radians, sin, cos, sqrt, atan2
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def load_saarland_plz_zauberware() -> list[dict]:
    """Primär: zauberware ZIP-Archiv herunterladen und JSON extrahieren."""
    resp = session.get(ZAUBERWARE_PRIMARY_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        json_names = [n for n in zf.namelist() if n.endswith(".json")]
        if not json_names:
            raise ValueError("Kein JSON in DE.zip gefunden")
        data = json.loads(zf.read(json_names[0]))

    seen: dict[str, dict] = {}
    
    # Firmen-Keywords, die wir rausfiltern wollen
    firmen_keywords = [
        "AG", "GmbH", "KG", "Versicherung", "Agentur", "Bank", "Post",
        "Direkt", "Service", "Regio", "Deutsche", "Universität", "Klinik",
        "Rundfunk", "Lotterie", "reha", "Rentenversicherung", "HUK",
        "Innungskrankenkasse", "UKV", "Saarländischer", "Praktiker",
        "Bundeszentralamt", "Assist"
    ]
    
    for d in data:
        if d.get("state") != "Saarland":
            continue
        
        plz = str(d["zipcode"]).zfill(5)
        ort = d.get("place", "")
        
        # Filtere Firmennamen raus
        ist_firma = any(keyword in ort for keyword in firmen_keywords)
        if ist_firma:
            continue
        
        # Filtere PLZ < 66000 (das sind oft bundesweite Großkunden-PLZ)
        if int(plz) < 66000:
            continue
        
        if plz not in seen:
            seen[plz] = {
                "plz": plz,
                "ort": ort,
                "lat": float(d["latitude"]),
                "lon": float(d["longitude"]),
            }
    
    if not seen:
        raise ValueError("zauberware-Datensatz enthielt keine Saarland-PLZ")
    return sorted(seen.values(), key=lambda x: x["plz"])


def load_saarland_plz_opendatasoft() -> list[dict]:
    """Fallback: Opendatasoft-Dataset mit korrigierter URL."""
    resp = session.get(OPENDATASOFT_FALLBACK_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    
    firmen_keywords = [
        "AG", "GmbH", "KG", "Versicherung", "Agentur", "Bank", "Post",
        "Direkt", "Service", "Regio", "Deutsche", "Universität", "Klinik"
    ]
    
    results = []
    for rec in payload.get("results", []):
        plz = rec.get("plz") or rec.get("name")
        geo = rec.get("geo_point_2d") or {}
        lat, lon = geo.get("lat"), geo.get("lon")
        ort = rec.get("name") or rec.get("plz_name") or ""
        
        # Filtere Firmen
        ist_firma = any(keyword in ort for keyword in firmen_keywords)
        if ist_firma:
            continue
        
        # Filtere PLZ < 66000
        if plz and int(str(plz).zfill(5)) < 66000:
            continue
        
        if plz and lat is not None and lon is not None:
            results.append({"plz": str(plz).zfill(5), "ort": ort, "lat": float(lat), "lon": float(lon)})
    
    if not results:
        raise ValueError("Opendatasoft-Antwort enthielt keine verwertbaren PLZ-Datensätze")
    return results


def load_station_coords() -> dict[str, dict]:
    if not STATIONS_META_CACHE.exists():
        raise FileNotFoundError(
            f"{STATIONS_META_CACHE} fehlt. Bitte zuerst update_data.py ausführen, "
            "das die Stationskoordinaten von DWD lädt und cached."
        )
    return json.loads(STATIONS_META_CACHE.read_text(encoding="utf-8"))


def main() -> None:
    station_coords = load_station_coords()
    if not station_coords:
        log.error("Keine Stationskoordinaten verfügbar, breche ab.")
        sys.exit(1)

    # Primär: zauberware (verifiziert), Fallback: Opendatasoft
    try:
        plz_list = load_saarland_plz_zauberware()
        quelle = "zauberware_zip"
    except Exception as e:  # noqa: BLE001
        log.warning("zauberware-Quelle fehlgeschlagen (%s), nutze Opendatasoft-Fallback.", e)
        try:
            plz_list = load_saarland_plz_opendatasoft()
            quelle = "opendatasoft_fallback"
        except Exception as e2:  # noqa: BLE001
            log.error("Auch Opendatasoft-Fallback fehlgeschlagen (%s). Abbruch.", e2)
            sys.exit(1)

    log.info("%d Saarland-PLZ geladen (Quelle: %s)", len(plz_list), quelle)

    mapping: dict[str, dict] = {}
    for entry in plz_list:
        best_station_id: Optional[str] = None
        best_dist = float("inf")
        for station_id, meta in station_coords.items():
            d = haversine_km(entry["lat"], entry["lon"], meta["lat"], meta["lon"])
            if d < best_dist:
                best_dist = d
                best_station_id = station_id

        if best_station_id is None:
            continue

        mapping[entry["plz"]] = {
            "ort": entry["ort"],
            "station_id": best_station_id,
            "station_name": STATIONS.get(best_station_id, station_coords[best_station_id].get("name")),
            "distanz_km": round(best_dist, 1),
        }

    output = {
        "quelle": quelle,
        "anzahl_plz": len(mapping),
        "mapping": mapping,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Geschrieben: %s (%d PLZ)", OUTPUT_FILE, len(mapping))


if __name__ == "__main__":
    main()
