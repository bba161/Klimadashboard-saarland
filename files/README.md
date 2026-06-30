# Klimadashboard Saarland

Ein statisches Dashboard, das anzeigt, ob das aktuelle Wetter an den
DWD-Wetterstationen im Saarland "normal" ist: aktuelle Temperatur,
Niederschlag der letzten 30 Tage und die Entwicklung der Sommertage
(Tage mit ≥ 25 °C) pro Jahr seit Stationsbeginn.

Datenquelle: [DWD Open Data](https://opendata.dwd.de/climate_environment/CDC/).
Die Daten werden stündlich automatisch per GitHub Action aktualisiert.

## Funktionsweise

```
.github/workflows/update.yml   stündlicher Cronjob
        │
        ▼
scripts/update_data.py         lädt DWD-Tages-/Stundenwerte für die
                                11 Saarland-Stationen, berechnet
                                Sommertage, Niederschlag, Referenzmittel
        │
        ▼
scripts/build_plz_mapping.py   ordnet jede Saarland-PLZ der nächst-
                                gelegenen der 11 Stationen zu
        │
        ▼
data/saarland.json             Klimadaten pro Station
data/plz_mapping.json          PLZ → Station
        │
        ▼
index.html                     liest beide JSON-Dateien per fetch()
                                und stellt das Dashboard dar
```

Es läuft kein Server: alles ist statisches HTML/JS, die "Aktualisierung"
passiert dadurch, dass die GitHub Action stündlich neue JSON-Dateien ins
Repository committet.

## Die 11 Stationen

| Station | DWD-ID |
|---|---|
| Berus | 00460 |
| Homburg | 02331 |
| Merzig | 03263 |
| Neunkirchen-Wellesweiler | 03545 |
| Nohfelden-Gonnesweiler | 03625 |
| Perl-Nennig | 03904 |
| Saarbrücken-Ensheim | 04336 |
| Schmelz-Hüttersdorf | 04490 |
| Tholey | 05029 |
| Weiskirchen | 05433 |
| Saarbrücken-Burbach | 06217 |

Stations-IDs sind in `scripts/stations.py` zentral gepflegt.

## Lokal testen

```bash
pip install -r scripts/requirements.txt
python scripts/update_data.py          # erzeugt data/saarland.json
python scripts/build_plz_mapping.py    # erzeugt data/plz_mapping.json
python -m http.server 8000             # im Projektordner
# dann im Browser: http://localhost:8000
```

Der erste Lauf von `update_data.py` dauert länger (lädt alle historischen
Tageswerte für 11 Stationen). Danach werden die DWD-Verzeichnislistings
und Stationsmetadaten 24 Stunden lang gecacht.

## Deployment

### Option A: GitHub Pages
1. Repository auf GitHub erstellen, dieses Projekt pushen.
2. Unter **Settings → Pages** als Quelle den Branch `main` (Root) wählen.
3. Die GitHub Action muss Schreibrechte auf das Repository haben: das ist
   mit `permissions: contents: write` im Workflow bereits vorbereitet.
4. Fertig – die Seite ist unter `https://<user>.github.io/<repo>/` erreichbar
   und aktualisiert sich stündlich automatisch.

### Option B: Netlify
1. Repository mit Netlify verbinden ("New site from Git").
2. Build-Befehl: leer lassen (keine Build-Pipeline nötig).
3. Publish-Verzeichnis: `.` (Projektwurzel).
4. Die GitHub Action committet die aktualisierten JSON-Dateien direkt ins
   Repository; Netlify erkennt den neuen Commit automatisch und deployt neu.
   (Alternativ kann das Dashboard so angepasst werden, dass es die JSON-
   Dateien direkt von `raw.githubusercontent.com` lädt – dann ist nicht
   einmal ein Netlify-Redeploy pro Update nötig. Bei Bedarf sagen, dann
   passe ich `DATA_URL`/`PLZ_MAPPING_URL` in `index.html` entsprechend an.)

## Wichtige Designentscheidungen

- **Eine Station für alle Werte:** anders als im Vorbild-Screenshot (das
  z. B. für Regen und Temperatur unterschiedliche Stationen nutzt) wird hier
  bewusst eine einzige, nächstgelegene Station für alle Kennzahlen einer PLZ
  verwendet – einfacher zu pflegen und zu erklären.
- **Klimareferenz:** Die Mittelwerte für 1961–1990 und 1991–2020 werden aus
  der eigenen historischen Tageswerte-Zeitreihe jeder Station berechnet
  (nicht aus dem DWD-`multi_annual`-Datensatz, der eigene, nicht 1:1
  passende Stations-IDs verwendet). Für Stationen mit kürzerer Messreihe
  kann der Referenzwert dadurch auf weniger Jahren beruhen –
  `jahre_verfuegbar` in `data/saarland.json` zeigt das transparent an.
- **Ausfallsicherheit:** Schlägt der Datenabruf für eine einzelne Station
  fehl, bleibt deren letzter bekannter Stand erhalten (Status `stale`) statt
  dass das ganze Dashboard ausfällt. Schlägt der Abruf für *alle* Stationen
  fehl, wird die JSON-Datei gar nicht erst überschrieben.
- **PLZ-Geodaten:** stammen primär aus dem OpenStreetMap-basierten
  Opendatasoft-Datensatz `georef-germany-postleitzahl` (ODbL-Lizenz), mit
  Fallback auf den `zauberware/postal-codes-json-xml-csv`-Datensatz
  (CC BY 4.0), falls die Primärquelle nicht erreichbar ist.

## Lizenzhinweise

- DWD-Daten: [Nutzungsbedingungen DWD Open Data](https://www.dwd.de/DE/service/copyright/copyright_node.html)
  (im Wesentlichen frei nutzbar mit Quellenangabe).
- PLZ-Geodaten: ODbL (Opendatasoft/OpenStreetMap) bzw. CC BY 4.0
  (zauberware-Fallback) – Quellenangabe bei Veröffentlichung nicht vergessen.
