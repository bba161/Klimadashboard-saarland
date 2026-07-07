#!/usr/bin/env python3
"""
build_plz_mapping.py - FINALE KORRIGIERTE VERSION
======================
Erzeugt data/plz_mapping.json: für jede Saarland-Postleitzahl die
nächstgelegene der DWD-Stationen (per Luftlinie).
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

ZAUBERWARE_PRIMARY_URL = (
    "https://raw.githubusercontent.com/zauberware/postal-codes-json-xml-csv/"
    "master/data/DE.zip"
)

OPENDATASOFT_FALLBACK_URL = (
    "https://data.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
    "georef-germany-postleitzahl@public/records"
    "?where=lan_name%3D%22Saarland%22&limit=100"
)

REQUEST_TIMEOUT = 60
session = requests.Session()
session.headers.update({"User-Agent": "saarland-klimadashboard/1.0"})

# BLACKLIST: Diese PLZ werden IMMER entfernt
PLZ_BLACKLIST = {
    "50424", "66087", "66088", "66090", "66094", "66097", "66098", "66099",
    "66100", "66101", "66102", "66103", "66104", "66106", "66108", "66109", "66150"
}

# Firmen-Keywords
FIRMEN_KEYWORDS = [
    "AG", "GmbH", "KG", "Media", "IHK", "Landesamt", "AOK", "Versicherung",
    "Agentur", "Bank", "Post", "Direkt", "Service", "Regio", "Deutsche",
    "Universität", "Universitätskliniken", "Klinik", "Rundfunk", "Lotterie",
    "reha", "Rentenversicherung", "HUK", "Innungskrankenkasse", "UKV",
    "Saarländischer", "Praktiker", "Bundeszentralamt", "Assist", "Cosmos"
]


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
    
    for d in data:
        if d.get("state") != "Saarland":
            continue
        
        plz = str(d["zipcode"]).zfill(5)
        ort = d.get("place", "")
        
        # 1. Blacklist-Check
        if plz in PLZ_BLACKLIST:
            log.info("PLZ %s (%s) durch Blacklist gefiltert", plz, ort)
            continue
        
        # 2. Firmen-Check
        if any(keyword in ort for keyword in FIRMEN_KEYWORDS):
            log.info("PLZ %s (%s) durch Firmen-Keyword gefiltert", plz, ort)
            continue
        
        # 3. Nur Saarland-PLZ (66xxx)
        if not plz.startswith("66"):
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
    """Fallback: Opendatasoft-Dataset."""
    resp = session.get(OPENDATASOFT_FALLBACK_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    
    results = []
    for rec in payload.get("results", []):
        plz = rec.get("plz") or rec.get("name")
        geo = rec.get("geo_point_2d") or {}
        lat, lon = geo.get("lat"), geo.get("lon")
        ort = rec.get("name") or rec.get("plz_name") or ""
        
        if not plz or lat is None or lon is None:
            continue
        
        plz = str(plz).zfill(5)
        
        # Blacklist
        if plz in PLZ_BLACKLIST:
            continue
        
        # Firmen
        if any(keyword in ort for keyword in FIRMEN_KEYWORDS):
            continue
        
        # Nur 66xxx
        if not plz.startswith("66"):
            continue
        
        results.append({"plz": plz, "ort": ort, "lat": float(lat), "lon": float(lon)})
    
    if not results:
        raise ValueError("Opendatasoft-Antwort enthielt keine verwertbaren PLZ-Datensätze")
    return results


def load_station_coords() -> dict[str, dict]:
    if not STATIONS_META_CACHE.exists():
        raise FileNotFoundError(f"{STATIONS_META_CACHE} fehlt.")
    return json.loads(STATIONS_META_CACHE.read_text(encoding="utf-8"))


def main() -> None:
    station_coords = load_station_coords()
    if not station_coords:
        log.error("Keine Stationskoordinaten verfügbar.")
        sys.exit(1)

    try:
        plz_list = load_saarland_plz_zauberware()
        quelle = "zauberware_zip"
    except Exception as e:
        log.warning("zauberware fehlgeschlagen (%s), Fallback.", e)
        try:
            plz_list = load_saarland_plz_opendatasoft()
            quelle = "opendatasoft_fallback"
        except Exception as e2:
            log.error("Auch Fallback fehlgeschlagen (%s).", e2)
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

    # SICHERHEITS-CHECK: Blacklist nochmal durchgehen
    for plz in list(mapping.keys()):
        if plz in PLZ_BLACKLIST:
            log.warning("PLZ %s war im Mapping, wird jetzt entfernt!", plz)
            del mapping[plz]

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
