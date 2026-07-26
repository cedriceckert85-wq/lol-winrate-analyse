# 📊 LoL Winrate-Analyse

Analysiert die komplette verfügbare Match-Historie eines League-of-Legends-Spielers
über die offizielle Riot-API und zeigt ein interaktives Dark-Mode-Dashboard:
Wann, mit wem und unter welchen Umständen gewinnst oder verlierst du?

## Installation

```bash
pip install requests pandas streamlit plotly
```

Mehr wird nicht benötigt (Python 3.10+, SQLite ist in Python enthalten).

**Auf NixOS** stattdessen (kein pip nötig — öffnet eine Shell mit allen Paketen):

```bash
nix-shell -p "python3.withPackages (p: [ p.requests p.pandas p.streamlit p.plotly ])"
```

## 1. API-Key holen

1. Auf https://developer.riotgames.com mit deinem Riot-Account einloggen.
2. Auf der Startseite den **Development API Key** kopieren (`RGAPI-...`).
3. **Wichtig:** Der Dev-Key läuft **alle 24 Stunden ab**. Einfach einen neuen
   holen und wieder eintragen — das Fetch-Skript macht dort weiter, wo es
   aufgehört hat.

## 2. Konfigurieren

In `fetch_data.py` oben die Konstanten anpassen:

```python
GAME_NAME = "DeinName"   # Riot-ID: der Teil VOR dem #
TAG_LINE  = "EUW"        # Riot-ID: der Teil NACH dem #
ROUTING   = "europe"     # europe | americas | asia | sea
API_KEY   = "RGAPI-..."  # dein aktueller Dev-Key
```

## 3. Daten laden

```bash
python fetch_data.py
```

- Lädt alle Match-IDs und dann jedes Match einzeln (Fortschritt in der Konsole).
- **Jederzeit abbrech- und neustartbar**: Bereits geladene Matches liegen in
  `matches.db` (SQLite) und werden übersprungen.
- Respektiert automatisch beide Rate-Limits des Dev-Keys
  (20 Anfragen/Sekunde und 100 Anfragen/2 Minuten) — bei ~1000 Matches dauert
  ein kompletter Durchlauf ca. 20–25 Minuten.

## 4. Dashboard starten

```bash
streamlit run dashboard.py
```

Öffnet sich im Browser (Standard: http://localhost:8501).

## Was das Dashboard zeigt

- **KPIs**: Spiele, Winrate (mit Konfidenzintervall), Ø-KDA, aktuelle Serie, Form.
- **Auto-Insights**: beste und schlechteste Bedingung (nur bei genug Daten).
- **Wochentag×Stunde-Heatmap** (Zentrum), Kalender-Heatmap, radiale 24h-Uhr,
  rollende Winrate mit Konfidenzband, kumulative Sieg/Niederlage-„Journey“,
  Streak-Barcode, divergierende Balken vs. eigenem Durchschnitt,
  Spieldauer-Verteilung, Champion-Treemap.
- **Kontext-Analysen**: Wochentag, Stunde, Wochenende, späte Nacht,
  Niederlagen-/Siegesserien, nach schlechtem Spiel, Position in der Session,
  Instant-Requeue, Queue, Champion, Rolle, Seite, Spieldauer, Monat,
  Premade vs. Solo.

### Statistik-Regeln

- Jede Winrate zeigt **n** (Spielanzahl) und das **Wilson-95%-Konfidenzintervall**.
- Werte mit **n < 15** werden ausgegraut — zu wenig Daten für eine Aussage.

## Grenzen

- Riot löscht Matches nach **~2 Jahren** — älter geht nicht, egal wie lange der
  Account existiert. (Die ~1000er-Deckelung pro ID-Abfrage umgeht das Skript
  automatisch über endTime-Zeitscheiben.)
- Einzelne sehr alte Matches können 404 liefern (gelöscht) und werden übersprungen.
- Manche Felder fehlen bei alten Matches/Patches; das Tool geht defensiv damit um.
