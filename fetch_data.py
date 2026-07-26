"""
fetch_data.py — Lädt alle verfügbaren League-of-Legends-Matches eines Spielers
über die offizielle Riot-Games-API und speichert sie in SQLite (matches.db).

Eigenschaften:
- Beliebig oft neustartbar (Resume): bereits geladene Matches werden übersprungen.
- Respektiert BEIDE Dev-Key-Rate-Limits gleichzeitig (20/s UND 100/2min).
- HTTP 429: liest Retry-After und wartet, mit exponentiellem Backoff.
- HTTP 404 auf einzelne Matches (zu alt / gelöscht): überspringen, nicht abbrechen.
- Defensive Feld-Extraktion (.get() überall) — Felder können je Patch fehlen.

Hinweis: Riot deckelt jede ID-Abfrage bei ~1000 — dieses Skript umgeht das
per endTime-Zeitscheiben und lädt die volle ~2-Jahres-Historie (harte Grenze:
ältere Matches löscht Riot serverseitig).
"""

import json
import os
import re
import sqlite3
import sys
import time
from collections import deque
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# KONFIGURATION — hier eigene Daten eintragen
# ---------------------------------------------------------------------------
# Liste der zu ladenden Accounts als (game_name, tag_line) — Riot-ID: Name#Tag.
# Jeder Account bekommt seine eigene Datenbank (matches_<name>.db).
ACCOUNTS = [
    ("xgazo", "xgazo"),
    ("Kurosakis wife", "777"),
    ("BBroly", "Broly"),
    ("Rocks D Xebec", "RIG"),
]
ROUTING = "europe"        # Regionales Routing: europe | americas | asia | sea
# Key aus Umgebungsvariable (lokal & Cloud-Hosting); Fallback: hier eintragen
API_KEY = os.environ.get("RIOT_API_KEY", "TRAGE-DEINEN-KEY-EIN")

BASE_URL = f"https://{ROUTING}.api.riotgames.com"
PAGE_SIZE = 100  # Maximum der Riot-API für by-puuid/ids


def set_routing(routing):
    """Routing zur Laufzeit umstellen (für CLI-Aufruf aus dem Dashboard)."""
    global ROUTING, BASE_URL
    ROUTING = routing
    BASE_URL = f"https://{ROUTING}.api.riotgames.com"


# ---------------------------------------------------------------------------
# Rate-Limiter: 20 Anfragen/Sekunde UND 100 Anfragen/2 Minuten (Dev-Key)
# ---------------------------------------------------------------------------
class RateLimiter:
    """Sliding-Window-Limiter, der beide Riot-Dev-Key-Limits gleichzeitig einhält."""

    # Fenster minimal größer als Riots 1 s / 120 s: fährt das Limit voll aus,
    # vermeidet aber 429-Strafwartezeiten durch Messungenauigkeiten an der
    # Fenstergrenze — netto die schnellste Variante mit einem Dev-Key.
    WIN_SHORT = 1.05
    WIN_LONG = 122.0

    def __init__(self, per_second=20, per_two_minutes=100):
        self.per_second = per_second
        self.per_two_minutes = per_two_minutes
        self.window_1s = deque()    # Zeitstempel der Anfragen der letzten ~1 s
        self.window_120s = deque()  # Zeitstempel der Anfragen der letzten ~120 s

    def wait_for_slot(self):
        """Blockiert, bis eine Anfrage erlaubt ist, und reserviert den Slot."""
        while True:
            now = time.monotonic()
            while self.window_1s and now - self.window_1s[0] >= self.WIN_SHORT:
                self.window_1s.popleft()
            while self.window_120s and now - self.window_120s[0] >= self.WIN_LONG:
                self.window_120s.popleft()

            if (len(self.window_1s) < self.per_second
                    and len(self.window_120s) < self.per_two_minutes):
                self.window_1s.append(now)
                self.window_120s.append(now)
                return

            # Warten, bis der älteste Eintrag aus dem vollen Fenster fällt
            sleep_1s = (self.WIN_SHORT - (now - self.window_1s[0])
                        if len(self.window_1s) >= self.per_second else 0.0)
            sleep_120s = (self.WIN_LONG - (now - self.window_120s[0])
                          if len(self.window_120s) >= self.per_two_minutes else 0.0)
            time.sleep(max(sleep_1s, sleep_120s, 0.02))


limiter = RateLimiter()


def api_get(url, max_retries=8):
    """GET mit Rate-Limiting, 429-Handling (Retry-After + exponentielles Backoff).

    Rückgabe: geparstes JSON, oder None bei 404 (Match zu alt/gelöscht).
    Bricht das Programm bei 401/403 ab (Key ungültig/abgelaufen).
    """
    backoff = 2.0
    for attempt in range(max_retries):
        limiter.wait_for_slot()
        try:
            resp = requests.get(url, headers={"X-Riot-Token": API_KEY}, timeout=15)
        except requests.RequestException as exc:
            print(f"  Netzwerkfehler ({exc}), warte {backoff:.0f}s ...")
            time.sleep(backoff)
            backoff *= 2
            continue

        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", backoff))
            wait = max(retry_after, backoff)
            print(f"  Rate-Limit (429), warte {wait:.0f}s ...")
            time.sleep(wait)
            backoff *= 2
            continue
        if resp.status_code in (401, 403):
            sys.exit(
                f"FEHLER {resp.status_code}: API-Key ungültig oder abgelaufen. "
                "Neuen Dev-Key auf developer.riotgames.com holen und in "
                "fetch_data.py eintragen — das Skript macht danach dort weiter, "
                "wo es aufgehört hat."
            )
        if resp.status_code >= 500:
            print(f"  Riot-Serverfehler ({resp.status_code}), warte {backoff:.0f}s ...")
            time.sleep(backoff)
            backoff *= 2
            continue
        sys.exit(f"FEHLER: Unerwarteter Statuscode {resp.status_code} für {url}")
    sys.exit(f"FEHLER: {max_retries} Versuche fehlgeschlagen für {url}")


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------
def init_db(path):
    con = sqlite3.connect(path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id            TEXT PRIMARY KEY,
            game_start          INTEGER,  -- Unix-Millisekunden
            game_end            INTEGER,  -- kann NULL sein (alte Matches)
            game_duration_s     INTEGER,  -- auf Sekunden normalisiert
            queue_id            INTEGER,
            win                 INTEGER,  -- 1 = Sieg, 0 = Niederlage
            champion            TEXT,
            team_position       TEXT,     -- TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY
            team_id             INTEGER,  -- 100 = blau, 200 = rot
            kills               INTEGER,
            deaths              INTEGER,
            assists             INTEGER,
            kda                 REAL,     -- (kills+assists)/max(deaths,1)
            surrender           INTEGER,
            early_surrender     INTEGER,
            kill_participation  REAL,
            gold_per_minute     REAL,
            vision_score_per_minute REAL,
            teammates           TEXT      -- JSON-Liste der 9 Mitspieler-PUUIDs
        )
    """)
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    # 404er (zu alt/gelöscht) merken, damit Resume sie nicht erneut anfragt
    con.execute("CREATE TABLE IF NOT EXISTS skipped (match_id TEXT PRIMARY KEY)")
    con.commit()
    return con


def extract_match_row(match, puuid):
    """Extrahiert die relevanten Felder eines Matches — defensiv mit .get()."""
    info = match.get("info", {}) or {}
    metadata = match.get("metadata", {}) or {}

    game_start = info.get("gameStartTimestamp")
    game_end = info.get("gameEndTimestamp")  # erst ab Patch 11.20 vorhanden

    # Riot-Eigenheit: gameDuration ist in SEKUNDEN, wenn gameEndTimestamp
    # existiert, sonst in MILLISEKUNDEN → auf Sekunden normalisieren.
    duration = info.get("gameDuration") or 0
    if game_end is None:
        duration = duration // 1000

    me = None
    for p in info.get("participants", []) or []:
        if p.get("puuid") == puuid:
            me = p
            break
    if me is None:
        return None  # z.B. exotische Modi ohne eigenen Eintrag

    kills = me.get("kills", 0) or 0
    deaths = me.get("deaths", 0) or 0
    assists = me.get("assists", 0) or 0
    kda = (kills + assists) / max(deaths, 1)

    challenges = me.get("challenges", {}) or {}  # kann komplett fehlen
    teammates = [p for p in (metadata.get("participants") or []) if p != puuid]

    return (
        metadata.get("matchId"),
        game_start,
        game_end,
        int(duration),
        info.get("queueId"),
        1 if me.get("win") else 0,
        me.get("championName"),
        me.get("teamPosition"),
        me.get("teamId"),
        kills,
        deaths,
        assists,
        round(kda, 3),
        1 if me.get("gameEndedInSurrender") else 0,
        1 if me.get("gameEndedInEarlySurrender") else 0,
        challenges.get("killParticipation"),
        challenges.get("goldPerMinute"),
        challenges.get("visionScorePerMinute"),
        json.dumps(teammates),
    )


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------
def db_path_for(game_name, tag_line):
    """Dateiname pro Account, z.B. matches_kurosakis-wife_777.db."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{game_name}_{tag_line}".lower()).strip("-")
    return f"matches_{slug}.db"


def get_puuid(game_name, tag_line):
    print(f"Suche Riot-Account {game_name}#{tag_line} ({ROUTING}) ...")
    # quote(): Leerzeichen/Sonderzeichen in der Riot-ID URL-sicher kodieren
    account = api_get(
        f"{BASE_URL}/riot/account/v1/accounts/by-riot-id/"
        f"{quote(game_name)}/{quote(tag_line)}"
    )
    if account is None:
        sys.exit(f"FEHLER: Riot-ID {game_name}#{tag_line} nicht gefunden. "
                 "Name/Tag/ROUTING prüfen.")
    puuid = account.get("puuid")
    print(f"PUUID gefunden: {puuid[:12]}...")
    return puuid


def get_match_ids_slice(puuid, end_time_s=None):
    """Eine 'Zeitscheibe' Match-IDs paginiert holen (max. ~1000 pro Abfrage).

    Riot deckelt jede ID-Abfrage bei ~1000 Ergebnissen — aber mit dem
    endTime-Filter (Unix-Sekunden) lässt sich davor weiterblättern. So kommt
    man an die volle ~2-Jahres-Historie, auch bei weit über 1000 Spielen.
    """
    ids = []
    start = 0
    suffix = f"&endTime={end_time_s}" if end_time_s else ""
    while True:
        page = api_get(
            f"{BASE_URL}/lol/match/v5/matches/by-puuid/{puuid}/ids"
            f"?start={start}&count={PAGE_SIZE}{suffix}"
        )
        if not page:
            break
        ids.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return ids


def min_game_start_of(con, ids, fetched_min_ms):
    """Ältester gameStart (ms) unter den IDs — aus DB und aktuellem Durchlauf."""
    candidates = [fetched_min_ms] if fetched_min_ms else []
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        row = con.execute(
            f"SELECT MIN(game_start) FROM matches "
            f"WHERE match_id IN ({','.join('?' * len(chunk))})", chunk).fetchone()
        if row and row[0]:
            candidates.append(row[0])
    return min(candidates) if candidates else None


def fetch_account(game_name, tag_line):
    db_path = db_path_for(game_name, tag_line)
    print(f"\n{'=' * 60}\nAccount: {game_name}#{tag_line}  →  {db_path}\n{'=' * 60}")
    con = init_db(db_path)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('account', ?)",
                (f"{game_name}#{tag_line}",))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('status', 'running')")
    con.commit()
    puuid = get_puuid(game_name, tag_line)

    known = {row[0] for row in con.execute("SELECT match_id FROM matches")}
    known |= {row[0] for row in con.execute("SELECT match_id FROM skipped")}
    print(f"{len(known)} Matches bereits in der Datenbank.")

    loaded = skipped = slice_nr = 0
    end_time_s = None  # None = neueste Spiele; danach rückwärts in Zeitscheiben

    while True:
        slice_nr += 1
        label = ("neueste" if end_time_s is None
                 else time.strftime("vor %d.%m.%Y", time.localtime(end_time_s)))
        ids = get_match_ids_slice(puuid, end_time_s)
        print(f"Zeitscheibe {slice_nr} ({label}): {len(ids)} IDs")
        if not ids:
            break  # Ende der ~2-Jahres-Historie erreicht

        todo = [mid for mid in ids if mid not in known]
        fetched_min_ms = None
        for i, match_id in enumerate(todo, 1):
            match = api_get(f"{BASE_URL}/lol/match/v5/matches/{match_id}")
            if match is None:
                con.execute("INSERT OR IGNORE INTO skipped VALUES (?)", (match_id,))
                con.commit()
                known.add(match_id)
                skipped += 1
                continue

            start_ms = (match.get("info") or {}).get("gameStartTimestamp")
            if start_ms and (fetched_min_ms is None or start_ms < fetched_min_ms):
                fetched_min_ms = start_ms

            row = extract_match_row(match, puuid)
            known.add(match_id)
            if row is None or row[0] is None:
                skipped += 1
                continue

            con.execute(
                "INSERT OR REPLACE INTO matches VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            con.commit()  # nach jedem Match speichern → jederzeit abbruchsicher
            loaded += 1
            if i % 25 == 0 or i == len(todo):
                print(f"  [{i}/{len(todo)}] dieser Scheibe geladen "
                      f"(gesamt neu: {loaded}, übersprungen: {skipped})")

        # Nächste Scheibe: alles VOR dem ältesten Spiel dieser Scheibe.
        # (+1 s, da endTime-Vergleich bei Riot nicht dokumentiert in-/exklusiv
        # ist — Duplikate fängt die known-Menge ab.)
        oldest_ms = min_game_start_of(con, ids, fetched_min_ms)
        if oldest_ms is None:
            break  # keine Zeitinfo ermittelbar (nur noch 404er) → Ende
        new_end = oldest_ms // 1000 + 1
        if end_time_s is not None and new_end >= end_time_s:
            break  # kein Fortschritt mehr → Historie vollständig
        end_time_s = new_end

    con.execute("INSERT OR REPLACE INTO meta VALUES ('status', 'done')")
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    print(f"\nFertig: {total} Matches in {db_path} "
          f"(neu: {loaded}, übersprungen: {skipped}).")
    con.close()


def main():
    # CLI-Modus: python fetch_data.py "Name" "Tag" [routing] → nur diesen Account
    # (nutzt das Dashboard für die Account-Suche)
    args = sys.argv[1:]
    if len(args) >= 2:
        if len(args) >= 3:
            set_routing(args[2])
        fetch_account(args[0], args[1])
        return
    for game_name, tag_line in ACCOUNTS:
        fetch_account(game_name, tag_line)
    print("\nAlle Accounts geladen. Dashboard starten mit:  streamlit run dashboard.py")


if __name__ == "__main__":
    main()
