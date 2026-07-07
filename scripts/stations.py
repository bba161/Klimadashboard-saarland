"""
Zentrale Stationsliste für das Saarland-Klimadashboard.

Die Koordinaten (lat/lon) werden NICHT hier hart codiert, sondern beim ersten
Lauf von update_data.py automatisch aus der offiziellen DWD-Stationsliste
(KL_Tageswerte_Beschreibung_Stationen.txt) nachgeschlagen und in
data/stations_meta.json zwischengespeichert. So bleiben Höhe/Koordinaten
immer mit der DWD-Quelle synchron, falls sich dort einmal etwas ändert.

Die Stations-IDs selbst sind vom Auftraggeber vorgegeben und fix.
"""

# DWD-Stations-ID (5-stellig, als String mit führenden Nullen) -> Anzeigename
STATIONS = {
    "00460": "Berus",
    "03263": "Merzig",
    "03545": "Neunkirchen-Wellesweiler",
    "03625": "Nohfelden-Gonnesweiler",
    "03904": "Perl-Nennig",
    "04336": "Saarbrücken-Ensheim",
    "05029": "Tholey",
    "05433": "Weiskirchen",
    "06217": "Saarbrücken-Burbach",
}

# Schwellenwert für die Sommertag-Zählung (DWD-Definition: Tmax >= 25.0 °C)
SOMMERTAG_SCHWELLE_C = 25.0

# Ab welchem Jahr die Sommertage-Statistik im Dashboard beginnen soll.
# (Die tatsächliche Datenverfügbarkeit pro Station kann später beginnen,
# das Skript verwendet dann automatisch das früheste verfügbare Jahr.)
STATS_START_JAHR = 2000

# Referenzperioden für den Klimavergleich (DWD multi_annual / vieljährige Mittel)
REFERENZPERIODEN = ["1961-1990", "1991-2020"]
