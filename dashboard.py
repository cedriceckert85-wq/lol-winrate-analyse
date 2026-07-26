"""
dashboard.py — Interaktives Streamlit-Dashboard zur LoL-Winrate-Analyse.

Liest matches.db (erzeugt von fetch_data.py), berechnet Kennzahlen und zeigt
Kontext-Analysen mit Plotly-Graphen im Dark Mode.

Statistische Regeln (gelten überall):
- Jede Prozentzahl zeigt n (Anzahl Spiele).
- Jede Winrate bekommt ein Wilson-95%-Konfidenzintervall.
- Die Auto-Insight-Karten werten nur Bedingungen mit n >= MIN_N, damit dort
  keine Zufallsausreißer mit 2 Spielen landen.

Farb-Logik: Farbe kodiert AUSSCHLIESSLICH die Winrate-Abweichung vom eigenen
Durchschnitt (farbenblind-sichere divergierende RdBu-Palette). Alles andere grau.
"""

import glob
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

import fetch_data

# API-Key aus Streamlit-Secrets (Cloud-Hosting / lokale secrets.toml) laden
# und an den Fetcher + dessen Subprozesse durchreichen
try:
    _key = st.secrets.get("RIOT_API_KEY", None)
except Exception:
    _key = None
if _key:
    fetch_data.API_KEY = _key
    os.environ["RIOT_API_KEY"] = _key

# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------
MIN_N = 15                  # Mindest-Spielzahl für die Auto-Insight-Karten
PREMADE_MIN_GAMES = 5       # Mitspieler mit >= 5 gemeinsamen Spielen gilt als Premade
SESSION_GAP_H = 2           # > 2 h Pause = neue Session
REQUEUE_MIN = 5             # < 5 min zwischen Spielen = Instant-Requeue
TZ = "Europe/Berlin"

COLOR_SCALE = "RdBu"        # divergierend, farbenblind-sicher (kein Rot-Grün)
GREY = "#5a5f6b"
BLUE = "#3b7dd8"            # Sieg / über Durchschnitt
ORANGE = "#e07b39"          # Niederlage / unter Durchschnitt

WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag",
               "Freitag", "Samstag", "Sonntag"]
WEEKDAYS_SHORT = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

QUEUE_NAMES = {
    420: "Ranked Solo/Duo", 440: "Ranked Flex", 450: "ARAM",
    400: "Normal Draft", 430: "Normal Blind", 490: "Quickplay",
    700: "Clash", 1700: "Arena", 900: "URF/ARURF",
}

ROLE_NAMES = {
    "TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid",
    "BOTTOM": "Bot (ADC)", "UTILITY": "Support",
}

st.set_page_config(page_title="LoL Winrate-Analyse", page_icon="📊",
                   layout="wide", initial_sidebar_state="expanded")


# ---------------------------------------------------------------------------
# Statistik-Helfer
# ---------------------------------------------------------------------------
def wilson_ci(wins, n, z=1.96):
    """Wilson-Score-95%-Konfidenzintervall für eine Winrate (in Prozent)."""
    if n == 0:
        return (0.0, 100.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (center - half) * 100), min(100.0, (center + half) * 100))


def winrate_stats(df, group_col, overall_wr, order=None):
    """Gruppiert nach group_col und liefert n, Winrate, Wilson-CI, Abweichung."""
    g = df.groupby(group_col, observed=True)["win"].agg(["sum", "count"]).reset_index()
    g.columns = [group_col, "wins", "n"]
    g["wr"] = g["wins"] / g["n"] * 100
    g[["ci_low", "ci_high"]] = g.apply(
        lambda r: pd.Series(wilson_ci(r["wins"], r["n"])), axis=1)
    g["abw"] = g["wr"] - overall_wr
    g["genug"] = g["n"] >= MIN_N
    if order is not None:
        g[group_col] = pd.Categorical(g[group_col], categories=order, ordered=True)
        g = g.sort_values(group_col)
    return g


# ---------------------------------------------------------------------------
# Daten laden & ableiten
# ---------------------------------------------------------------------------
@st.cache_data(ttl=120)  # alle 2 min neu laden — Fetch kann parallel laufen
def list_databases():
    """Findet alle matches*.db und liest den Account-Namen aus der meta-Tabelle."""
    result = {}
    for path in sorted(glob.glob("matches*.db")):
        label = path
        try:
            con = sqlite3.connect(path)
            row = con.execute(
                "SELECT value FROM meta WHERE key='account'").fetchone()
            con.close()
            if row:
                label = row[0]
        except sqlite3.Error:
            pass
        result[label] = path
    return result


@st.cache_data(ttl=120)  # alle 2 min neu laden — Fetch kann parallel laufen
def load_data(db_path):
    con = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM matches WHERE game_start IS NOT NULL", con)
    con.close()
    if df.empty:
        return df

    # Zeitspalten (lokale Zeit)
    dt = pd.to_datetime(df["game_start"], unit="ms", utc=True).dt.tz_convert(TZ)
    df["dt"] = dt
    df["datum"] = dt.dt.date
    df["stunde"] = dt.dt.hour
    df["wochentag_nr"] = dt.dt.weekday
    df["wochentag"] = df["wochentag_nr"].map(dict(enumerate(WEEKDAYS_DE)))
    df["monat"] = dt.dt.strftime("%Y-%m")
    df["iso_jahr"] = dt.dt.isocalendar().year.astype(int)
    df["iso_woche"] = dt.dt.isocalendar().week.astype(int)
    df["wochenende"] = df["wochentag_nr"].isin([5, 6]).map(
        {True: "Wochenende", False: "Wochentag"})
    df["nacht"] = df["stunde"].isin([23, 0, 1, 2, 3, 4]).map(
        {True: "Späte Nacht (23–4 Uhr)", False: "Tagsüber"})

    df["queue"] = df["queue_id"].map(QUEUE_NAMES).fillna(
        "Queue " + df["queue_id"].astype(str))
    df["rolle"] = df["team_position"].map(ROLE_NAMES).fillna("Unbekannt")
    df["seite"] = df["team_id"].map({100: "Blaue Seite", 200: "Rote Seite"})
    df["dauer_min"] = df["game_duration_s"] / 60
    df["dauer_bucket"] = pd.cut(
        df["dauer_min"], bins=[0, 20, 25, 30, 35, 40, 999],
        labels=["<20 min", "20–25", "25–30", "30–35", "35–40", "40+ min"])

    # ---- Sequenz-Features (chronologisch über ALLE Spiele) ----
    df = df.sort_values("game_start").reset_index(drop=True)

    # Serien VOR dem aktuellen Spiel (Niederlagen-/Siegesserie)
    loss_streak, win_streak = [], []
    ls = ws = 0
    for w in df["win"]:
        loss_streak.append(ls)
        win_streak.append(ws)
        if w == 1:
            ws += 1
            ls = 0
        else:
            ls += 1
            ws = 0
    df["niederlagen_serie_davor"] = loss_streak
    df["sieges_serie_davor"] = win_streak
    df["ns_bucket"] = pd.cut(df["niederlagen_serie_davor"], bins=[-1, 0, 1, 2, 999],
                             labels=["0", "1", "2", "3+"])
    df["ss_bucket"] = pd.cut(df["sieges_serie_davor"], bins=[-1, 0, 1, 2, 999],
                             labels=["0", "1", "2", "3+"])

    # Vorheriges Spiel schlecht? (KDA < 1)
    df["vorher_schlecht"] = (df["kda"].shift(1) < 1).map(
        {True: "Nach schlechtem Spiel (KDA<1)", False: "Nach normalem Spiel"})
    df.loc[df.index == 0, "vorher_schlecht"] = None

    # Sessions: > SESSION_GAP_H Stunden Pause = neue Session
    prev_end = (df["game_start"] + df["game_duration_s"] * 1000).shift(1)
    gap_min = (df["game_start"] - prev_end) / 60000
    df["pause_min"] = gap_min
    new_session = gap_min.isna() | (gap_min > SESSION_GAP_H * 60)
    df["session_id"] = new_session.cumsum()
    df["session_pos"] = df.groupby("session_id").cumcount() + 1
    df["session_pos_bucket"] = df["session_pos"].clip(upper=4).map(
        {1: "1. Spiel", 2: "2. Spiel", 3: "3. Spiel", 4: "4.+ Spiel"})

    # Instant-Requeue: < REQUEUE_MIN Minuten nach Ende des Vorspiels
    df["requeue"] = None
    mask = gap_min.notna() & (gap_min <= SESSION_GAP_H * 60)
    df.loc[mask & (gap_min < REQUEUE_MIN), "requeue"] = "Instant-Requeue (<5 min)"
    df.loc[mask & (gap_min >= REQUEUE_MIN), "requeue"] = "Mit Pause (≥5 min)"

    # Premade-Erkennung: Mitspieler-PUUIDs zählen
    teammate_counts = {}
    tm_lists = df["teammates"].apply(lambda s: json.loads(s) if s else [])
    for lst in tm_lists:
        for t in lst:
            teammate_counts[t] = teammate_counts.get(t, 0) + 1
    premades = {t for t, c in teammate_counts.items() if c >= PREMADE_MIN_GAMES}
    df["premade"] = tm_lists.apply(
        lambda lst: any(t in premades for t in lst)).map(
        {True: f"Mit Premade (≥{PREMADE_MIN_GAMES} gem. Spiele)", False: "Solo"})

    return df


# ---------------------------------------------------------------------------
# Grafik-Helfer
# ---------------------------------------------------------------------------
def style_fig(fig, height=420, title=None):
    fig.update_layout(template="plotly_dark", height=height,
                      margin=dict(l=40, r=20, t=50 if title else 25, b=40),
                      title=title, paper_bgcolor="rgba(0,0,0,0)")
    return fig


def deviation_range(values):
    """Symmetrischer Farbbereich um 0 für Abweichungs-Farbskalen."""
    m = max(5.0, float(pd.Series(values).abs().max() or 5.0))
    return -m, m


def rgba(hex_color, alpha):
    """#rrggbb → rgba(...) mit Deckkraft — für blasse n<MIN_N-Darstellung."""
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


def scale_color(abw, lo, hi, alpha=1.0):
    """Abweichung → Farbe aus der divergierenden Skala, optional blass."""
    t = (abw - lo) / (hi - lo) if hi > lo else 0.5
    c = px.colors.sample_colorscale(COLOR_SCALE, max(0.0, min(1.0, t)))[0]
    if alpha >= 1.0:
        return c
    nums = c[c.index("(") + 1:c.index(")")]  # "r, g, b"
    return f"rgba({nums},{alpha})"


# ===========================================================================
# App
# ===========================================================================
def db_status(db_path):
    """'running' solange der Fetcher für diese DB läuft, sonst 'done'."""
    try:
        con = sqlite3.connect(db_path)
        row = con.execute("SELECT value FROM meta WHERE key='status'").fetchone()
        con.close()
        return row[0] if row else "done"
    except sqlite3.Error:
        return "done"


# ---------------- Account-Suche: beliebigen Account laden ----------------
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

with st.sidebar.expander("Account suchen / hinzufügen", expanded=False):
    riot_id = st.text_input("Riot-ID (Name#Tag)", placeholder="Name#EUW")
    region = st.selectbox("Region", ["europe", "americas", "asia", "sea"])
    if st.button("Suchen & laden"):
        if "#" not in riot_id:
            st.error("Format: Name#Tag (z.B. Faker#KR1)")
        else:
            name, tag = (s.strip() for s in riot_id.split("#", 1))
            try:
                resp = requests.get(
                    f"https://{region}.api.riotgames.com/riot/account/v1/"
                    f"accounts/by-riot-id/{quote(name)}/{quote(tag)}",
                    headers={"X-Riot-Token": fetch_data.API_KEY}, timeout=15)
            except requests.RequestException:
                resp = None
            if resp is None:
                st.error("Netzwerkfehler — bitte erneut versuchen.")
            elif resp.status_code == 404:
                st.error(f"Account {name}#{tag} in Region '{region}' "
                         "nicht gefunden.")
            elif resp.status_code != 200:
                st.error(f"Riot-API-Fehler ({resp.status_code}) — "
                         "später erneut versuchen.")
            else:
                db = os.path.join(PROJECT_DIR, fetch_data.db_path_for(name, tag))
                if os.path.exists(db) and db_status(db) == "running":
                    st.info("Dieser Account wird bereits geladen.")
                else:
                    # Fetch als eigener Prozess: blockiert die Seite nicht,
                    # überlebt Seiten-Reloads, schreibt in die Account-DB
                    log = open(os.path.join(
                        PROJECT_DIR, "fetch_dashboard.log"), "a")
                    subprocess.Popen(
                        [sys.executable, "fetch_data.py", name, tag, region],
                        cwd=PROJECT_DIR, stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=True)
                    st.success(f"{name}#{tag} gefunden — Daten werden geladen.")
                    list_databases.clear()
                    time.sleep(2)  # kurz warten, bis die DB-Datei existiert
                    st.rerun()

dbs = list_databases()
if not dbs:
    st.error("Keine Datenbank gefunden. Bitte zuerst `python fetch_data.py` ausführen.")
    st.stop()

st.sidebar.title("Filter")
account = st.sidebar.selectbox("Account", list(dbs.keys()))

if db_status(dbs[account]) == "running":
    st.info("Dieser Account wird gerade geladen — die Zahlen unten wachsen "
            "noch. Die Daten aktualisieren sich automatisch alle 2 Minuten "
            "(bei ~1000 Spielen dauert der komplette Abruf ca. 20 Minuten).")

df_all = load_data(dbs[account])
if df_all.empty:
    st.error(f"Noch keine Matches für {account} in der Datenbank — "
             "`python fetch_data.py` läuft evtl. noch. Seite später neu laden.")
    st.stop()

# ---------------- Sidebar-Filter ----------------
queues = sorted(df_all["queue"].unique())
default_q = [q for q in queues if q == "Ranked Solo/Duo"] or queues
f_queue = st.sidebar.multiselect("Queue-Typ", queues, default=default_q)
f_champ = st.sidebar.multiselect("Champion", sorted(df_all["champion"].dropna().unique()))
f_rolle = st.sidebar.multiselect("Rolle", [r for r in ROLE_NAMES.values()
                                           if r in df_all["rolle"].unique()])
dmin, dmax = df_all["datum"].min(), df_all["datum"].max()
f_zeit = st.sidebar.date_input("Zeitraum", (dmin, dmax), min_value=dmin, max_value=dmax)

df = df_all.copy()
if f_queue:
    df = df[df["queue"].isin(f_queue)]
if f_champ:
    df = df[df["champion"].isin(f_champ)]
if f_rolle:
    df = df[df["rolle"].isin(f_rolle)]
if isinstance(f_zeit, tuple) and len(f_zeit) == 2:
    df = df[(df["datum"] >= f_zeit[0]) & (df["datum"] <= f_zeit[1])]

st.sidebar.caption(f"{len(df)} von {len(df_all)} Spielen im Filter · "
                   f"Fehlerbalken = Wilson-95%-CI")

if df.empty:
    st.warning("Der aktuelle Filter enthält keine Spiele — Filter lockern.")
    st.stop()

N = len(df)
WINS = int(df["win"].sum())
WR = WINS / N * 100
CI_LO, CI_HI = wilson_ci(WINS, N)

# ---------------- Kopf: KPIs (Inverted Pyramid — Status zuerst) ----------------
st.title(f"LoL Winrate-Analyse — {account}")

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Spiele", f"{N}", f"{df['dauer_min'].sum()/60:.0f} h Spielzeit",
          delta_color="off")
k2.metric("Winrate", f"{WR:.1f} %", f"CI: {CI_LO:.0f}–{CI_HI:.0f} % (n={N})",
          delta_color="off")
k3.metric("Ø KDA", f"{df['kda'].mean():.2f}",
          f"{df['kills'].mean():.1f}/{df['deaths'].mean():.1f}/{df['assists'].mean():.1f}",
          delta_color="off")
last = df.sort_values("game_start").tail(1).iloc[0]
cur = 0
for w in reversed(df.sort_values("game_start")["win"].tolist()):
    if w == last["win"]:
        cur += 1
    else:
        break
k4.metric("Aktuelle Serie",
          f"{cur} {'Siege' if last['win'] else 'Niederlagen'}")
recent = df.sort_values("game_start").tail(20)
k5.metric("Letzte 20 Spiele", f"{recent['win'].mean()*100:.0f} %",
          f"{recent['win'].mean()*100 - WR:+.0f} pp vs. Ø")

# ---------------- Auto-Insight-Karten ----------------
insights = []  # (Label, Winrate, n)

def collect(g, col, prefix):
    for _, r in g[g["genug"]].iterrows():
        insights.append((f"{prefix}: {r[col]}", r["wr"], int(r["n"])))

CONTEXTS = [
    ("wochentag", "Wochentag", WEEKDAYS_DE),
    ("stunde", "Stunde", list(range(24))),
    ("wochenende", "Wochenende/Wochentag", None),
    ("nacht", "Tageszeit", None),
    ("ns_bucket", "Niederlagen-Serie davor", ["0", "1", "2", "3+"]),
    ("ss_bucket", "Sieges-Serie davor", ["0", "1", "2", "3+"]),
    ("vorher_schlecht", "Vorheriges Spiel", None),
    ("session_pos_bucket", "Position in Session",
     ["1. Spiel", "2. Spiel", "3. Spiel", "4.+ Spiel"]),
    ("requeue", "Requeue-Verhalten", None),
    ("queue", "Queue", None),
    ("champion", "Champion", None),
    ("rolle", "Rolle", None),
    ("seite", "Seite", None),
    ("dauer_bucket", "Spieldauer", None),
    ("monat", "Monat", None),
    ("premade", "Premade/Solo", None),
]

context_tables = {}
for col, label, order in CONTEXTS:
    sub = df.dropna(subset=[col])
    if sub.empty:
        continue
    g = winrate_stats(sub, col, WR, order)
    context_tables[(col, label)] = g
    collect(g, col, label)

# Wochentag×Stunde-Zellen zusätzlich als Insight-Kandidaten
wh = df.groupby(["wochentag_nr", "stunde"])["win"].agg(["sum", "count"]).reset_index()
for _, r in wh[wh["count"] >= MIN_N].iterrows():
    insights.append((
        f"{WEEKDAYS_SHORT[int(r['wochentag_nr'])]} {int(r['stunde'])} Uhr",
        r["sum"] / r["count"] * 100, int(r["count"])))

if insights:
    best = max(insights, key=lambda x: x[1])
    worst = min(insights, key=lambda x: x[1])
    c1, c2 = st.columns(2)
    c1.success(f"**Beste Bedingung** — {best[0]}: "
               f"**{best[1]:.0f} %** Winrate (n={best[2]})")
    c2.error(f"**Schlechteste Bedingung** — {worst[0]}: "
             f"**{worst[1]:.0f} %** Winrate (n={worst[2]})")
else:
    st.info(f"Noch keine Bedingung mit n≥{MIN_N} — mehr Spiele nötig oder Filter lockern.")

st.divider()

# ===========================================================================
# 1) ZENTRALES ELEMENT: Wochentag×Stunde-Heatmap
# ===========================================================================
st.subheader("Winrate nach Wochentag × Stunde")
st.caption(f"Farbe = Abweichung von deiner Ø-Winrate ({WR:.1f} %) in Prozentpunkten. "
           f"Zahl in der Zelle = Anzahl Spiele.")

z = [[None] * 24 for _ in range(7)]      # Winrate-Abweichung je Zelle
txt = [[""] * 24 for _ in range(7)]
hover = [[""] * 24 for _ in range(7)]
for _, r in wh.iterrows():
    d, h = int(r["wochentag_nr"]), int(r["stunde"])
    n_, w_ = int(r["count"]), int(r["sum"])
    wr_ = w_ / n_ * 100
    lo, hi = wilson_ci(w_, n_)
    txt[d][h] = str(n_)
    hover[d][h] = (f"{WEEKDAYS_DE[d]}, {h} Uhr<br>Winrate: {wr_:.0f} % "
                   f"(CI {lo:.0f}–{hi:.0f})<br>n = {n_}")
    z[d][h] = wr_ - WR

lo_r, hi_r = deviation_range([v for row in z for v in row if v is not None])
fig = go.Figure(go.Heatmap(
    z=z, x=list(range(24)), y=WEEKDAYS_SHORT,
    colorscale=COLOR_SCALE, zmin=lo_r, zmax=hi_r, zmid=0,
    text=txt, texttemplate="%{text}", textfont=dict(size=10),
    customdata=hover, hovertemplate="%{customdata}<extra></extra>",
    colorbar=dict(title="Abw. (pp)"), xgap=2, ygap=2))
fig.update_yaxes(autorange="reversed")
fig.update_xaxes(title="Uhrzeit", dtick=1)
st.plotly_chart(style_fig(fig, 380), width="stretch")

# ===========================================================================
# 2) Die "Journey": Verlauf, Rolling-Winrate, Streak-Barcode
# ===========================================================================
st.subheader("Verlauf")
dfc = df.sort_values("game_start").reset_index(drop=True)
dfc["journey"] = (dfc["win"] * 2 - 1).cumsum()  # +1 Sieg, −1 Niederlage
dfc["spiel_nr"] = dfc.index + 1

c1, c2 = st.columns(2)

with c1:  # Kumulative Sieg-minus-Niederlage-Linie
    fig = go.Figure()
    fig.add_hline(y=0, line_color=GREY, line_dash="dot")
    fig.add_trace(go.Scatter(
        x=dfc["spiel_nr"], y=dfc["journey"], mode="lines",
        line=dict(color=BLUE, width=2), fill="tozeroy",
        fillcolor="rgba(59,125,216,0.15)", name="Bilanz",
        customdata=dfc["dt"].dt.strftime("%d.%m.%Y %H:%M"),
        hovertemplate="Spiel %{x} (%{customdata})<br>Bilanz: %{y:+d}<extra></extra>"))
    fig.update_xaxes(title="Spiel Nr.")
    fig.update_yaxes(title="Siege − Niederlagen (kumuliert)")
    st.plotly_chart(style_fig(fig, 340, "Kumulative Bilanz (Siege − Niederlagen)"),
                    width="stretch")

with c2:  # Rolling-Winrate mit Wilson-Band
    max_w = max(10, min(50, N))
    window = st.slider("Fenstergröße (Spiele)", 10, max_w,
                       min(20, max_w), key="rollwin")
    roll = dfc["win"].rolling(window).mean() * 100
    band = dfc["win"].rolling(window).sum().apply(
        lambda w: wilson_ci(w, window) if pd.notna(w) else (None, None))
    lo_b = band.apply(lambda t: t[0])
    hi_b = band.apply(lambda t: t[1])
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=dfc["spiel_nr"], y=hi_b, mode="lines",
                             line=dict(width=0), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=dfc["spiel_nr"], y=lo_b, mode="lines",
                             line=dict(width=0), fill="tonexty",
                             fillcolor="rgba(59,125,216,0.18)",
                             name="Wilson-95%-Band", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=dfc["spiel_nr"], y=roll, mode="lines",
                             line=dict(color=BLUE, width=2),
                             name=f"Winrate (letzte {window})",
                             hovertemplate="Spiel %{x}<br>%{y:.0f} %<extra></extra>"))
    fig.add_hline(y=WR, line_color=ORANGE, line_dash="dash",
                  annotation_text=f"Ø {WR:.1f} %")
    fig.update_yaxes(title="Winrate (%)", range=[0, 100])
    fig.update_xaxes(title="Spiel Nr.")
    st.plotly_chart(style_fig(fig, 280, "Rollende Winrate"), width="stretch")

# Streak-Barcode (Form-Guide)
barcode = go.Figure(go.Heatmap(
    z=[dfc["win"].tolist()],
    colorscale=[[0, ORANGE], [1, BLUE]], showscale=False,
    customdata=[[f"Spiel {i+1}: {'Sieg' if w else 'Niederlage'}"
                 for i, w in enumerate(dfc['win'])]],
    hovertemplate="%{customdata}<extra></extra>", xgap=1))
barcode.update_yaxes(visible=False)
barcode.update_xaxes(title="Spiel Nr. (blau = Sieg, orange = Niederlage)")
st.plotly_chart(style_fig(barcode, 130, "Streak-Barcode"), width="stretch")

st.divider()

# ===========================================================================
# 3) Vergleiche & Aufschlüsselungen
# ===========================================================================
st.subheader("Abweichung vom eigenen Durchschnitt")

dim_options = {
    "Champion": "champion", "Rolle": "rolle", "Queue": "queue",
    "Wochentag": "wochentag", "Seite": "seite", "Spieldauer": "dauer_bucket",
    "Monat": "monat",
}
dim_label = st.selectbox("Dimension", list(dim_options.keys()))
dim_col = dim_options[dim_label]
g = context_tables.get((dim_col, [l for c, l, _ in CONTEXTS if c == dim_col][0]))
if g is not None and not g.empty:
    gs = g.sort_values("abw")
    colors = [BLUE if a >= 0 else ORANGE for a in gs["abw"]]
    fig = go.Figure(go.Bar(
        x=gs["abw"], y=gs[dim_col].astype(str), orientation="h",
        marker_color=colors,
        error_x=dict(type="data", symmetric=False,
                     array=(gs["ci_high"] - gs["wr"]).clip(lower=0),
                     arrayminus=(gs["wr"] - gs["ci_low"]).clip(lower=0),
                     color="#aaaaaa", thickness=1),
        text=[f"{r.wr:.0f} % (n={r.n})" for r in gs.itertuples()],
        textposition="outside", cliponaxis=False,
        hovertemplate="%{y}<br>Abweichung: %{x:+.1f} pp<extra></extra>"))
    fig.add_vline(x=0, line_color="#ffffff", line_width=1)
    fig.update_xaxes(title=f"Abweichung von Ø-Winrate ({WR:.1f} %) in Prozentpunkten")
    st.plotly_chart(style_fig(fig, max(300, 34 * len(gs) + 80)),
                    width="stretch")

c1, c2 = st.columns(2)

with c1:  # Champion-Treemap
    st.markdown("**Champion-Treemap** — Fläche = Spiele, Farbe = Abweichung")
    tm = df.dropna(subset=["rolle", "champion"]).groupby(
        ["rolle", "champion"], observed=True)["win"].agg(["sum", "count"]).reset_index()
    tm["wr"] = tm["sum"] / tm["count"] * 100
    tm["abw"] = tm["wr"] - WR
    tm["label_n"] = "n=" + tm["count"].astype(int).astype(str)
    tm_lo, tm_hi = deviation_range(tm["abw"])
    fig = px.treemap(tm, path=["rolle", "champion"], values="count",
                     color="abw", color_continuous_scale=COLOR_SCALE,
                     range_color=[tm_lo, tm_hi], color_continuous_midpoint=0,
                     custom_data=["wr", "label_n"])
    fig.update_traces(
        hovertemplate="%{label}<br>Winrate: %{customdata[0]:.0f} % "
                      "(%{customdata[1]})<extra></extra>",
        texttemplate="%{label}<br>%{customdata[1]}")
    fig.update_coloraxes(colorbar_title="Abw. (pp)")
    st.plotly_chart(style_fig(fig, 450), width="stretch")

with c2:  # Radiales 24h-Uhr-Diagramm
    st.markdown("**24h-Uhr** — Balkenlänge = Spiele, Farbe = Abweichung")
    hr = winrate_stats(df, "stunde", WR, order=list(range(24)))
    hr_full = pd.DataFrame({"stunde": range(24)}).merge(hr, on="stunde", how="left")
    hr_full[["n", "wins"]] = hr_full[["n", "wins"]].fillna(0)
    # Eigene Farbskala der Uhr
    uhr_lo, uhr_hi = deviation_range(hr_full["abw"].dropna())
    bar_colors = [scale_color(r.abw if pd.notna(r.abw) else 0, uhr_lo, uhr_hi)
                  for r in hr_full.itertuples()]
    fig = go.Figure(go.Barpolar(
        r=hr_full["n"], theta=hr_full["stunde"] * 15,  # 360°/24 = 15° pro Stunde
        width=[13] * 24, marker_color=bar_colors,
        customdata=[[f"{int(r.stunde)} Uhr", r.wr if pd.notna(r.wr) else 0,
                     int(r.n)] for r in hr_full.itertuples()],
        hovertemplate="%{customdata[0]}<br>Winrate: %{customdata[1]:.0f} %"
                      "<br>n = %{customdata[2]}<extra></extra>"))
    fig.update_layout(polar=dict(
        angularaxis=dict(direction="clockwise", rotation=90,
                         tickmode="array", tickvals=[h * 15 for h in range(0, 24, 3)],
                         ticktext=[f"{h} Uhr" for h in range(0, 24, 3)]),
        radialaxis=dict(showticklabels=False)))
    st.plotly_chart(style_fig(fig, 450), width="stretch")
    st.caption("0 Uhr oben, im Uhrzeigersinn.")

st.divider()

# ===========================================================================
# 4) Kalender-Heatmap & Spieldauer-Verteilung
# ===========================================================================
c1, c2 = st.columns([3, 2])

with c1:
    st.subheader("Kalender-Heatmap")
    st.caption("Farbe = Winrate-Abweichung des Tages · Zahl beim Hover = Spiele. "
               "Tageswerte haben kleine Stichproben — nur zur Orientierung.")
    cal = df.groupby(["iso_jahr", "iso_woche", "wochentag_nr"])["win"].agg(
        ["sum", "count"]).reset_index()
    cal["wk"] = cal["iso_jahr"].astype(str) + "-KW" + cal["iso_woche"].astype(
        str).str.zfill(2)
    weeks = sorted(cal["wk"].unique())
    zc = [[None] * len(weeks) for _ in range(7)]
    hv = [[""] * len(weeks) for _ in range(7)]
    for _, r in cal.iterrows():
        wi = weeks.index(r["wk"])
        d = int(r["wochentag_nr"])
        wr_ = r["sum"] / r["count"] * 100
        zc[d][wi] = wr_ - WR
        hv[d][wi] = (f"{WEEKDAYS_DE[d]}, {r['wk']}<br>"
                     f"Winrate: {wr_:.0f} % · n = {int(r['count'])}")
    lo_c, hi_c = deviation_range([v for row in zc for v in row if v is not None])
    fig = go.Figure(go.Heatmap(
        z=zc, x=weeks, y=WEEKDAYS_SHORT,
        colorscale=COLOR_SCALE, zmin=lo_c, zmax=hi_c, zmid=0,
        customdata=hv, hovertemplate="%{customdata}<extra></extra>",
        colorbar=dict(title="Abw. (pp)"), xgap=2, ygap=2))
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(tickangle=45, nticks=20)
    st.plotly_chart(style_fig(fig, 330), width="stretch")

with c2:
    st.subheader("Spieldauer: Sieg vs. Niederlage")
    fig = go.Figure()
    for val, name, color in [(1, "Sieg", BLUE), (0, "Niederlage", ORANGE)]:
        fig.add_trace(go.Violin(
            y=df.loc[df["win"] == val, "dauer_min"], name=name,
            line_color=color, fillcolor=color, opacity=0.55,
            box_visible=True, meanline_visible=True, points=False))
    fig.update_yaxes(title="Spieldauer (Minuten)")
    fig.update_layout(showlegend=False)
    st.plotly_chart(style_fig(fig, 330), width="stretch")
    st.caption(f"Sieg: n={WINS} · Niederlage: n={N - WINS}")

st.divider()

# ===========================================================================
# 5) Alle Kontext-Analysen als Detailtabellen (unten: Granulares)
# ===========================================================================
st.subheader("Kontext-Analysen im Detail")
st.caption("Jede Zeile: Winrate mit n und Wilson-95%-CI.")

for (col, label), g in context_tables.items():
    with st.expander(f"{label} ({len(g)} Ausprägungen)"):
        show = g.copy()
        show["Winrate"] = show["wr"].map(lambda w: f"{w:.1f} %")
        show["95%-CI"] = show.apply(
            lambda r: f"{r['ci_low']:.0f}–{r['ci_high']:.0f} %", axis=1)
        show["Abweichung"] = show["abw"].map(lambda a: f"{a:+.1f} pp")
        show = show.rename(columns={col: label, "n": "n (Spiele)",
                                    "wins": "Siege"})
        st.dataframe(
            show[[label, "n (Spiele)", "Siege", "Winrate", "95%-CI", "Abweichung"]],
            hide_index=True, width="stretch")

st.caption(
    f"Datengrundlage: {len(df_all)} Matches aus matches.db · "
    f"Riot-API-Limit: max. ~1000 Spiele / ~2 Jahre Historie · "
    f"Premade = wiederkehrender Mitspieler mit ≥{PREMADE_MIN_GAMES} gemeinsamen "
    f"Spielen · Session = Spielblock mit <{SESSION_GAP_H} h Pause dazwischen.")
