# =============================================================================
# ProjectScout – KI-Agent
# Datei: agent/agent.py
#
# Ablauf:
#   1. Bezahlte Suchanfragen aus Supabase laden
#   2. Passende Medienquellen nach Bundesland filtern
#   3. Artikel crawlen (mit Cache-Prüfung)
#   4. KI-Analyse mit Claude Haiku (Relevanz-Check)
#   5. Projekte in Supabase speichern (Duplikat-Check LAUFÜBERGREIFEND via Hash)
#   6. Status auf "abgeschlossen" setzen
#   7. E-Mail mit Ergebnis-Zusammenfassung versenden (FIXER Dashboard-Link)
#
# Umgebungsvariablen (GitHub Secrets):
#   SUPABASE_URL, SUPABASE_SECRET_KEY
#   ANTHROPIC_API_KEY
#   BREVO_API_KEY
# =============================================================================

import os
import sys
import json
import time
import hashlib
import requests
import re
import anthropic
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(__file__))
from medien_datenbank import get_alle_bundeslaender_kuerzel
from gemeinden_datenbank import get_gemeinden_fuer_bundeslaender, PROTOKOLL_PFADE
import crawler  # echtes Crawling der Gemeinde-Websites (zweite Säule neben web_search)
import regionalmedien  # Säule C: Regionalmedien (meinbezirk) direkt auslesen

# =============================================================================
# KONFIGURATION
# =============================================================================

SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SECRET_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# E-Mail-Versand laeuft ueber Brevo (wie die Bestaetigungs-/Rechnungsmail des
# Webhooks). Der API-Key muss als GitHub-Actions-Secret BREVO_API_KEY gesetzt sein.
BREVO_API_KEY     = os.environ.get("BREVO_API_KEY", "")
ABSENDER_EMAIL    = "office@project-scout.at"
ABSENDER_NAME     = "ProjectScout"

DASHBOARD_BASE_URL = "https://project-scout.at/dashboard.html"
ADMIN_EMAIL        = "office@project-scout.at"

# Anthropic Client für web_search-gestützte Suche
ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Modellwahl ADAPTIV nach Umfang:
#   - Wenige Bundesländer (≤3): Sonnet (höchste Qualität, Zeit reicht locker)
#   - Viele Bundesländer (≥4):  Haiku  (schnell genug für das Zeitbudget)
# Beide Strings sind erprobt. Zum Erzwingen einfach MODELL_FIX setzen.
MODELL_SONNET = "claude-sonnet-5"
MODELL_HAIKU  = "claude-haiku-4-5-20251001"
MODELL_FIX    = MODELL_SONNET  # Immer Sonnet – beste Qualität, Kosten vertretbar

def _modell_fuer_scope(anzahl_bundeslaender: int) -> str:
    if MODELL_FIX:
        return MODELL_FIX
    return MODELL_SONNET if anzahl_bundeslaender <= 3 else MODELL_HAIKU

# web_search-Tool: wie viele Einzelsuchen Claude pro API-Call durchführen darf.
# Höher = gründlicher = mehr Treffer (aber etwas langsamer/teurer).
WEB_SEARCH_MAX_USES = 12
MAX_TOKENS_SUCHE    = 8000

# Zeitbudget: nach dieser Zeit werden KEINE neuen Suchen mehr gestartet, damit
# der Agent IMMER sauber finalisiert (E-Mail + Status), bevor der Workflow hart
# abbricht. Steuerbar per Env ZEITBUDGET_MINUTEN (Default 45). WICHTIG: das
# timeout-minutes im GitHub-Workflow muss immer ~15 Min UEBER diesem Wert liegen.
ZEITBUDGET_SEKUNDEN = int(os.environ.get("ZEITBUDGET_MINUTEN", "45")) * 60

# ── Echtes Crawling der Gemeinde-Websites (zweite Säule neben web_search) ──
# Standardmäßig aktiv. Per GitHub-Secret CRAWLING_AKTIV=0 abschaltbar.
CRAWLING_AKTIV          = os.environ.get("CRAWLING_AKTIV", "1") != "0"
# Höchstzahl Gemeinden, die PRO LAUF gecrawlt werden (Rotation über mehrere Läufe
# via Cache-Tabelle 'gemeinde_crawl'). Bei regionaler Auswahl meist alle abgedeckt.
CRAWL_MAX_GEMEINDEN     = int(os.environ.get("CRAWL_MAX_GEMEINDEN", "160"))
# Anteil des Gesamt-Zeitbudgets, der maximal fürs Crawling verwendet wird.
CRAWL_ZEITBUDGET_ANTEIL = float(os.environ.get("CRAWL_ZEITBUDGET_ANTEIL", "0.78"))
# Gleichzeitige Downloads beim Crawling (I/O-gebunden).
CRAWL_WORKERS           = int(os.environ.get("CRAWL_WORKERS", "12"))

# -- Saeule C: Regionalmedien-Ernte (meinbezirk direkt auslesen) --
# DEAKTIVIERT: meinbezirk blockt die Server-IP vollstaendig (alle Abrufe HTTP-Fehler).
# Solange das so ist, wuerde Saeule C nur Laufzeit verbrauchen, die dann der
# Websuche fehlt. Die freigewordene Zeit geht jetzt komplett an Crawling + Websuche.
# Wieder aktivierbar per GitHub-Env REGIO_AKTIV=1 (curl_cffi ist im Workflow
# installiert; vor Dauerbetrieb einen Testlauf mit der Test-Suchanfrage machen).
REGIO_AKTIV             = os.environ.get("REGIO_AKTIV", "0") == "1"
# Anteil des Gesamt-Zeitbudgets, das (zuerst) fuer die Regionalmedien-Ernte gilt.
REGIO_ZEITBUDGET_ANTEIL = float(os.environ.get("REGIO_ZEITBUDGET_ANTEIL", "0.55"))
# Obergrenze geladener Artikel pro Lauf (Schutz bei Grossauftraegen).
REGIO_MAX_ARTIKEL       = int(os.environ.get("REGIO_MAX_ARTIKEL", "4000"))
# Gleichzeitige Downloads bei der Ernte (I/O-gebunden).
REGIO_WORKERS           = int(os.environ.get("REGIO_WORKERS", "16"))

# Bundesland-Kürzel → ausgeschriebener Name (für lesbare Suchanfragen)
BL_NAMEN = {
    "W": "Wien", "NOE": "Niederösterreich", "OOE": "Oberösterreich",
    "SBG": "Salzburg", "STK": "Steiermark", "KTN": "Kärnten",
    "TIR": "Tirol", "VBG": "Vorarlberg", "BGR": "Burgenland",
}

# Robuste Normalisierung egal wie Claude das Bundesland schreibt
BL_NORMALISIERUNG = {
    "W": "W", "WIEN": "W",
    "NÖ": "NOE", "NOE": "NOE", "NIEDERÖSTERREICH": "NOE", "NIEDEROESTERREICH": "NOE",
    "OÖ": "OOE", "OOE": "OOE", "OBERÖSTERREICH": "OOE", "OBEROESTERREICH": "OOE",
    "SBG": "SBG", "SLZ": "SBG", "SALZBURG": "SBG",
    "STK": "STK", "STMK": "STK", "STEIERMARK": "STK",
    "KTN": "KTN", "KÄRNTEN": "KTN", "KAERNTEN": "KTN",
    "TIR": "TIR", "TIROL": "TIR",
    "VBG": "VBG", "VORARLBERG": "VBG",
    "BGR": "BGR", "BGLD": "BGR", "BURGENLAND": "BGR",
}

def _normalisiere_bundesland(bl_roh: str) -> str:
    if not bl_roh:
        return ""
    schluessel = bl_roh.upper().strip()
    return BL_NORMALISIERUNG.get(schluessel, schluessel)


# =============================================================================
# FAKTENBASIERTE PRÜFUNGEN (Ort -> Bundesland, Jahres-Plausibilität, Geocoding)
# =============================================================================

# Ungefähre geografische Zentren je Bundesland – Fallback fürs Geocoding,
# wenn ein konkreter Ort nicht aufgelöst werden kann.
BL_ZENTREN = {
    "W":   (48.2082, 16.3738),  # Wien
    "NOE": (48.2047, 15.6256),  # St. Pölten
    "OOE": (48.3069, 14.2858),  # Linz
    "SBG": (47.8095, 13.0550),  # Salzburg
    "STK": (47.0707, 15.4395),  # Graz
    "KTN": (46.6247, 14.3050),  # Klagenfurt
    "TIR": (47.2692, 11.4041),  # Innsbruck
    "VBG": (47.5031,  9.7471),  # Bregenz
    "BGR": (47.8457, 16.5286),  # Eisenstadt
}

# Manuelle Ort->Bundesland-Zuordnungen für Schreibweisen, die in der
# Gemeinde-Datenbank anders oder gar nicht stehen (z.B. "Wien", "Klagenfurt").
_MANUELLE_ORT_BL = {
    "wien": "W",
    "klagenfurt": "KTN",
    "st. pölten": "NOE", "st.pölten": "NOE", "sankt pölten": "NOE", "st pölten": "NOE",
    "graz": "STK", "linz": "OOE", "salzburg": "SBG", "innsbruck": "TIR",
    "bregenz": "VBG", "eisenstadt": "BGR",
}

_ORT_ZU_BL = None  # wird einmalig befüllt (Lazy-Init)

def _get_ort_zu_bl() -> dict:
    """
    Baut eine Nachschlagetabelle {ort_kleingeschrieben: bundesland} aus der
    Gemeinde-Datenbank. NUR EINDEUTIGE Ortsnamen werden aufgenommen – kommt ein
    Name in mehreren Bundesländern vor, wird er bewusst weggelassen, damit der
    Filter niemals einen korrekten Treffer fälschlich verwirft.
    """
    global _ORT_ZU_BL
    if _ORT_ZU_BL is not None:
        return _ORT_ZU_BL
    zaehler: dict = {}
    for bl in ("W", "NOE", "OOE", "SBG", "STK", "KTN", "TIR", "VBG", "BGR"):
        try:
            gemeinden = get_gemeinden_fuer_bundeslaender([bl])
        except Exception:
            gemeinden = []
        for g in gemeinden:
            name = (g.get("name") or "").lower().strip()
            if name:
                zaehler.setdefault(name, set()).add(bl)
    tabelle = {ort: next(iter(bls)) for ort, bls in zaehler.items() if len(bls) == 1}
    # Manuelle Aliase haben Vorrang
    tabelle.update(_MANUELLE_ORT_BL)
    _ORT_ZU_BL = tabelle
    return _ORT_ZU_BL

def _bundesland_aus_ort(ort: str) -> str:
    """Gibt das eindeutige Bundesland-Kürzel für einen Ort zurück, sonst ''."""
    if not ort:
        return ""
    o = ort.lower().strip()
    tabelle = _get_ort_zu_bl()
    if o in tabelle:
        return tabelle[o]
    # Auch Teiltreffer erlauben: "Floridsdorf (21. Bezirk)" -> "floridsdorf"
    erstes_wort = o.split("(")[0].split(",")[0].strip()
    if erstes_wort and erstes_wort in tabelle:
        return tabelle[erstes_wort]
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# ORTS-KANONISIERUNG für die Mehrquellen-Erkennung
# Die Zusammenführung "gleiches Bauvorhaben, zweite Quelle" vergleicht Projekte
# NUR innerhalb desselben Orts (exakter Datenbank-Match). Schreiben zwei
# Quellen denselben Ort unterschiedlich ("Micheldorf" vs. "Micheldorf in
# Oberösterreich", "Gutau (Bezirk Freistadt)" vs. "Gutau"), wird dasselbe
# Projekt sonst doppelt angelegt statt als Zusatzquelle angehängt. Darum wird
# jeder KI-Ortsname hier auf den OFFIZIELLEN Gemeindenamen der Datenbank
# vereinheitlicht. Unbekannte Orte (z.B. "Oberösterreich (landesweit)")
# bleiben – bereinigt – erhalten.
# ─────────────────────────────────────────────────────────────────────────────

_GEMEINDE_NAMEN_BL: dict = {}   # bl -> {normalisierter_name: offizieller Name}


def _norm_ortsschluessel(name: str) -> str:
    s = (name or "").lower().strip()
    # "Sankt"/"St" am Anfang auf "st. " vereinheitlichen
    if s.startswith("sankt "):
        s = "st. " + s[6:]
    elif s.startswith("st ") :
        s = "st. " + s[3:]
    return s


def _get_gemeinde_namen(bl: str) -> dict:
    if bl in _GEMEINDE_NAMEN_BL:
        return _GEMEINDE_NAMEN_BL[bl]
    m = {}
    try:
        for g in get_gemeinden_fuer_bundeslaender([bl]):
            n = (g.get("name") or "").strip()
            if n:
                m[_norm_ortsschluessel(n)] = n
    except Exception:
        pass
    _GEMEINDE_NAMEN_BL[bl] = m
    return m


def _kanonischer_ort(ort_roh: str, bundesland: str) -> str:
    """
    Mappt einen von der KI gelieferten Ortsnamen auf den offiziellen
    Gemeindenamen: Klammer-/Komma-Zusätze und Mehrfachorte ("Pupping / Karling")
    werden entfernt, dann wird gegen die Gemeinde-Datenbank des Bundeslands
    abgeglichen – exakt oder als EINDEUTIGER Namensanfang ("Micheldorf" ->
    "Micheldorf in Oberösterreich"). Mehrdeutige Anfänge (z.B. "Rainbach":
    im Mühlkreis UND im Innkreis) bleiben unverändert, damit nie falsch
    zugeordnet wird.
    """
    o = (ort_roh or "").strip()
    if not o:
        return ""
    o = o.split("(")[0].split("/")[0].split(",")[0].strip()
    if not o or not bundesland:
        return o
    namen = _get_gemeinde_namen(bundesland)
    if not namen:
        return o
    key = _norm_ortsschluessel(o)
    if key in namen:
        return namen[key]
    praefix_treffer = [voll for k, voll in namen.items() if k.startswith(key + " ")]
    if len(praefix_treffer) == 1:
        return praefix_treffer[0]
    return o


_JAHR_REGEX = re.compile(r"\b(20\d{2})\b")

def _jahr_verdaechtig(text: str, cutoff_jahr: int, heute_jahr: int) -> bool:
    """
    True, wenn im Text AUSSCHLIESSLICH veraltete Jahreszahlen vorkommen (z.B. nur
    2022/2023) und kein Jahr aus dem erlaubten Zeitfenster oder der Zukunft.
    Findet sich gar keine Jahreszahl ODER mindestens ein aktuelles/zukünftiges
    Jahr, gilt der Treffer als unverdächtig (kein falsches Verwerfen).
    """
    if not text:
        return False
    jahre = [int(j) for j in _JAHR_REGEX.findall(text)]
    if not jahre:
        return False
    # erlaubt: ab Cutoff-Jahr bis zu 3 Jahre in die Zukunft (geplante Baustarts)
    aktuelle = [j for j in jahre if cutoff_jahr <= j <= heute_jahr + 3]
    return len(aktuelle) == 0

# Geocoding: Ort -> (lat, lng). Nutzt OpenStreetMap/Nominatim mit Cache und
# fällt bei Misserfolg auf das Bundesland-Zentrum zurück. Ein Fehler bricht
# den Lauf NIEMALS ab.
_GEOCODE_CACHE: dict = {}

def geocode_ort(ort: str, bundesland: str) -> tuple:
    """Gibt (lat, lng) zurück. Cache -> Nominatim -> Bundesland-Zentrum."""
    schluessel = f"{(ort or '').lower().strip()}|{bundesland}"
    if schluessel in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[schluessel]

    lat = lng = None
    ort_sauber = (ort or "").split("(")[0].strip()

    # 1) Schon einmal verortet? Vorhandene Koordinaten aus bereits gespeicherten
    #    Projekten wiederverwenden (ortsweit, kundenübergreifend) – spart die
    #    langsame Nominatim-Abfrage (sonst 1 Sek Pause je neuem Ort). Reduziert
    #    die Geocoding-Wartezeit über die Läufe hinweg auf nahezu null.
    if ort_sauber:
        try:
            bekannt = sb_get("projekte", {
                "select": "lat,lng",
                "ort":    f"eq.{ort_sauber}",
                "lat":    "not.is.null",
                "limit":  "1",
            })
            if bekannt:
                lat = bekannt[0].get("lat")
                lng = bekannt[0].get("lng")
        except Exception:
            pass  # Bei Problemen einfach unten live abfragen.

    # 2) Noch unbekannt -> einmalig live bei Nominatim abfragen.
    if (lat is None or lng is None) and ort_sauber:
        bl_name = BL_NAMEN.get(bundesland, "")
        query = f"{ort_sauber}, {bl_name}, Österreich" if bl_name else f"{ort_sauber}, Österreich"
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "json", "limit": 1, "countrycodes": "at"},
                headers={"User-Agent": "ProjectScout/1.0 (office@project-scout.at)"},
                timeout=6,
            )
            if resp.status_code == 200:
                treffer = resp.json()
                if treffer:
                    lat = float(treffer[0]["lat"])
                    lng = float(treffer[0]["lon"])
            time.sleep(1.0)  # Nominatim-Nutzungsbedingungen: max. 1 Anfrage/Sekunde
        except Exception:
            pass  # Netzfehler etc. -> Fallback

    if lat is None or lng is None:
        lat, lng = BL_ZENTREN.get(bundesland, (None, None))

    _GEOCODE_CACHE[schluessel] = (lat, lng)
    return lat, lng

# Token-Zähler für Kosten-Logging
# "searches" = TATSÄCHLICH durchgeführte Web-Suchen (von der Anthropic-API exakt
# abgerechnet, $10/1000). Ersetzt die frühere Pauschalannahme "5 Suchen je Call",
# die die Kosten im Log um ca. das 3-Fache zu hoch ausgewiesen hat.
_TOKEN_STATS = {"input": 0, "output": 0, "calls": 0, "searches": 0}

SUPABASE_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

# =============================================================================
# SUPABASE HILFSFUNKTIONEN
# =============================================================================

SB_RETRY_VERSUCHE = 3      # Gesamtzahl Versuche pro Request
SB_TIMEOUT = 20            # Sekunden pro Versuch (vorher 10 - zu knapp für Cold-Starts)
SB_RETRY_PAUSE = 5         # Sekunden Basis-Wartezeit, steigt pro Versuch (5s, 10s, ...)

def _sb_request(methode: str, tabelle: str, headers: dict,
                 params: dict = None, json_daten: dict = None):
    """
    Zentraler Supabase-REST-Request mit automatischem Retry bei Timeout/
    Verbindungsfehlern. Verhindert, dass ein einzelner kurzer Netzwerk-
    Aussetzer (z.B. Supabase Cold-Start) den kompletten Agent-Lauf mit
    Exit Code 1 abstürzen lässt (Vorfall 17.08.2026: ReadTimeoutError
    beim Laden der offenen Aufträge, timeout=10 ohne Retry).
    """
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    letzter_fehler = None
    for versuch in range(1, SB_RETRY_VERSUCHE + 1):
        try:
            resp = requests.request(
                methode, url, headers=headers, params=params,
                json=json_daten, timeout=SB_TIMEOUT,
            )
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            letzter_fehler = e
            if versuch < SB_RETRY_VERSUCHE:
                wartezeit = versuch * SB_RETRY_PAUSE
                print(f"⚠️  Supabase-Timeout bei '{tabelle}' (Versuch {versuch}/{SB_RETRY_VERSUCHE}), "
                      f"erneuter Versuch in {wartezeit}s...")
                time.sleep(wartezeit)
            else:
                print(f"❌ Supabase-Request '{tabelle}' endgültig fehlgeschlagen "
                      f"nach {SB_RETRY_VERSUCHE} Versuchen")
    raise letzter_fehler

def sb_get(tabelle: str, params: dict = None) -> list:
    resp = _sb_request("GET", tabelle, SUPABASE_HEADERS, params=params)
    return resp.json()

def sb_patch(tabelle: str, filter_params: dict, daten: dict) -> None:
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    _sb_request("PATCH", tabelle, headers, params=filter_params, json_daten=daten)

def sb_insert(tabelle: str, daten: dict) -> dict | None:
    resp = _sb_request("POST", tabelle, SUPABASE_HEADERS, json_daten=daten)
    if resp.status_code in (200, 201):
        result = resp.json()
        return result[0] if isinstance(result, list) and result else result
    return None

def sb_upsert(tabelle: str, daten: dict, on_conflict: str) -> None:
    headers = {**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    params = {"on_conflict": on_conflict}
    _sb_request("POST", tabelle, headers, params=params, json_daten=daten)

# =============================================================================
# SCHRITT 1: OFFENE AUFTRÄGE LADEN
# =============================================================================

def lade_offene_auftraege(spezifische_id: str = None) -> list:
    if spezifische_id:
        params = {"id": f"eq.{spezifische_id}"}
    else:
        # 'bezahlt'      = neue Aufträge.
        # 'agent_laeuft' = Aufträge, deren vorheriger Lauf abgestürzt ist. Da dank
        #                  der 'ein Lauf gleichzeitig'-Sperre (Workflow) garantiert
        #                  KEIN zweiter Lauf parallel arbeitet, ist jeder noch auf
        #                  'agent_laeuft' stehende Auftrag eine Leiche und wird hier
        #                  automatisch wieder aufgegriffen (Selbstheilung gegen
        #                  Hänger). Doppelte Projekte entstehen dabei nicht, weil
        #                  speichere_projekt laufübergreifend per rohdaten_hash
        #                  dedupliziert.
        params = {"status": "in.(bezahlt,agent_laeuft)"}
    auftraege = sb_get("suchanfragen", params)
    print(f"📋 {len(auftraege)} offener Auftrag/Aufträge gefunden")
    return auftraege

def lade_kundendaten(kunden_id: str) -> dict | None:
    ergebnis = sb_get("kunden", {"id": f"eq.{kunden_id}"})
    return ergebnis[0] if ergebnis else None

# =============================================================================
# SCHRITT 2: SUCHBEGRIFFE AUFBAUEN
# =============================================================================

GEWERK_KEYWORDS = {
    "Erdbau / Aushub":                      ["Erdbau", "Aushub", "Erdarbeiten", "Geländegestaltung"],
    "Spezialtiefbau":                       ["Spezialtiefbau", "Pfahlgründung", "Bohrpfahl", "Verbau", "Grundwasser"],
    "Betonbau / Stahlbeton":                ["Betonbau", "Stahlbeton", "Beton", "Betondecke", "Fundamentarbeiten"],
    "Maurerarbeiten":                       ["Maurerarbeiten", "Mauerwerk", "Ziegelmauerwerk", "Hochbau"],
    "Abbruch / Demontage":                  ["Abbruch", "Abriss", "Rückbau", "Demolierung", "Demontage"],
    "Sprengungen":                          ["Sprengung", "Sprengtechnik", "Felssprengung"],
    "Bohrpfähle / Baugrubensicherung":      ["Bohrpfahl", "Baugrubensicherung", "Verbau", "Pfahlgründung"],
    "Bodenverbesserung":                    ["Bodenverbesserung", "Bodenstabilisierung", "Untergrundverbesserung"],
    "Hangsicherungen":                      ["Hangsicherung", "Böschungssicherung", "Steinschlagschutz", "Lawinenschutz"],
    "Hochwasserschutz":                     ["Hochwasserschutz", "Hochwasserdamm", "Retentionsbecken", "Schutzdamm"],
    "Zimmerei / Holzbau":                   ["Zimmerei", "Holzbau", "Dachstuhl", "Holzkonstruktion", "Holzriegelbau"],
    "Trockenbau":                           ["Trockenbau", "Gipskarton", "Rigips", "Raumtrenner"],
    "Estrich / Boden":                      ["Estrich", "Bodenbelag", "Fußboden", "Industrieboden"],
    "Fliesen / Naturstein":                 ["Fliesen", "Naturstein", "Fliesenleger", "Keramik", "Klinker"],
    "Maler / Anstreicher":                  ["Maler", "Anstreicher", "Fassadenanstrich", "Beschichtung"],
    "Schlosser / Metallbau":                ["Schlosser", "Metallbau", "Stahlbau", "Geländer", "Metallkonstruktion"],
    "Fenster / Türen / Verglasungen":       ["Fenster", "Türen", "Verglasung", "Glasfassade", "Sonnenschutz"],
    "Innenausbau":                          ["Innenausbau", "Inneneinrichtung", "Ausbau", "Raumgestaltung"],
    "Elektriker / Elektrotechnik":          ["Elektroinstallation", "Elektrotechnik", "Elektro", "Stromversorgung"],
    "Installateur / Sanitär":               ["Sanitär", "Installateur", "Sanitärinstallation", "Rohrinstallation"],
    "Heizung / Lüftung / Klima (HVAC)":     ["Heizung", "Lüftung", "Klimaanlage", "HKLS", "HVAC", "Heizungsanlage"],
    "Aufzüge / Lifte":                      ["Aufzug", "Lift", "Personenaufzug", "Lastenaufzug", "Fahrstuhl"],
    "Brandschutz / Sprinkler":              ["Brandschutz", "Sprinkleranlage", "Brandmeldeanlage", "Feuerschutz"],
    "Gebäudeautomation / Smart Building":   ["Gebäudeautomation", "Smart Building", "Hausautomation", "GLT"],
    "Dachdecker":                           ["Dach", "Dachdeckung", "Dachabdichtung", "Dachsanierung", "Dachdecker"],
    "Spengler / Klempner":                  ["Spengler", "Klempner", "Blechdach", "Entwässerung", "Dachrinne"],
    "Fassadenbau / WDVS":                   ["Fassade", "WDVS", "Außendämmung", "Wärmedämmung", "Fassadensanierung"],
    "Gerüstbau":                            ["Gerüst", "Gerüstbau", "Fassadengerüst", "Arbeitsgerüst"],
    "PV-Anlagen / Photovoltaik":            ["Photovoltaik", "PV-Anlage", "Solaranlage", "Solarstrom", "Solardach"],
    "Wärmepumpen":                          ["Wärmepumpe", "Erdwärmepumpe", "Luftwärmepumpe", "Wärmegewinnung"],
    "Erdwärmebohrungen":                    ["Erdwärme", "Geothermie", "Erdwärmebohrung", "Tiefenbohrung"],
    "Windkraftanlagen":                     ["Windkraft", "Windrad", "Windkraftanlage", "Windpark"],
    "E-Ladeinfrastruktur":                  ["Ladestation", "E-Ladepunkt", "Elektromobilität", "Ladeinfrastruktur"],
    "Biomasse / Nahwärme":                  ["Biomasse", "Nahwärme", "Fernwärme", "Pelletsheizung", "Hackschnitzel"],
    "Energiesanierung":                     ["Energiesanierung", "Thermische Sanierung", "Gebäudesanierung", "Sanierung"],
    "Batteriespeicher":                     ["Batteriespeicher", "Energiespeicher", "Stromspeicher"],
    "Wasserkraft":                          ["Wasserkraft", "Wasserkraftwerk", "Kleinkraftwerk", "Wasserturbine"],
    "Deponie / Entsorgung":                 ["Deponie", "Entsorgung", "Abfallentsorgung", "Mülldeponie"],
    "Altlastensanierung":                   ["Altlastensanierung", "Altlast", "Bodensanierung", "Kontamination"],
    "Recycling / Kreislaufwirtschaft":      ["Recycling", "Kreislaufwirtschaft", "Wertstoffhof", "Recyclinganlage"],
    "Straßenbau":                           ["Straßenbau", "Asphalt", "Gehsteig", "Radweg", "Ortsstraße", "Gemeindestraße"],
    "Bahnbau / Gleisbau":                   ["Gleisbau", "Bahnstrecke", "Schienenverkehr", "ÖBB", "Bahnhof"],
    "Brückenbau":                           ["Brücke", "Brückenbau", "Unterführung", "Überführung", "Viadukt"],
    "Tunnelbau":                            ["Tunnel", "Tunnelbau", "Untertagebau", "Tunnelröhre"],
    "Leitungsbau":                          ["Leitungsbau", "Pipeline", "Gasleitung", "Fernwärmeleitung"],
    "Kanal / Abwasser":                     ["Kanal", "Abwasser", "Kanalisation", "Kläranlagen", "Regenwasserkanal"],
    "Wasserversorgung":                     ["Wasserleitung", "Trinkwasser", "Wasserversorgung", "Pumpwerk", "Wasserbehälter"],
    "Glasfaser / Breitband":                ["Glasfaser", "Breitband", "Glasfaserausbau", "Internet", "Netzausbau"],
    "Kraftwerksbau":                        ["Kraftwerk", "Kraftwerksbau", "Energieanlage", "Stromversorgung"],
    "Beleuchtung / Straßenbeleuchtung":     ["Straßenbeleuchtung", "LED-Beleuchtung", "Lichtanlage", "Beleuchtung"],
    "Verkehrsleitsysteme":                  ["Verkehrsleitsystem", "Ampel", "Verkehrssteuerung", "Lichtsignalanlage"],
    "Grundstückskauf / -verkauf":           ["Grundstück", "Grundstückskauf", "Grundstücksverkauf", "Liegenschaft", "Parzelle"],
    "Umwidmungen / Flächenwidmung":         ["Umwidmung", "Flächenwidmung", "Widmungsänderung", "Bebauungsplan", "Widmung"],
    "Wohnbauprojekte":                      ["Wohnbau", "Wohnanlage", "Wohnprojekt", "Mehrfamilienhaus", "Wohnhausanlage"],
    "Gewerbliche Neubauten":                ["Gewerbegebäude", "Bürogebäude", "Gewerbebau", "Betriebsgebäude"],
    "Sanierung / Revitalisierung":          ["Sanierung", "Revitalisierung", "Generalsanierung", "Gebäudesanierung"],
    "Projektentwicklung / Bauträger":       ["Bauträger", "Projektentwicklung", "Immobilienprojekt", "Entwicklung"],
    "Immobilienmakler":                     ["Immobilienmakler", "Makler", "Liegenschaftsvermittlung", "Immobilienverkauf"],
    "Liegenschaftsbewertung":               ["Bewertung", "Liegenschaftsbewertung", "Schätzung", "Verkehrswert"],
    "Zwangsversteigerungen":                ["Zwangsversteigerung", "Versteigerung", "Exekution", "Zwangsverkauf"],
    "Pachtflächen / Landwirtschaftliche Flächen": ["Pachtfläche", "Landwirtschaftsfläche", "Ackerfläche", "Pacht"],
    "Schulen / Kindergärten":               ["Schule", "Kindergarten", "Bildungseinrichtung", "Schulbau", "Volksschule"],
    "Pflegeheime / Senioreneinrichtungen":  ["Pflegeheim", "Seniorenheim", "Altenheim", "Senioreneinrichtung"],
    "Krankenhäuser / Ärztezentren":         ["Krankenhaus", "Klinik", "Ärztehaus", "Ambulanz", "Gesundheitszentrum"],
    "Sporthallen / Freizeitanlagen":        ["Sporthalle", "Sportzentrum", "Freizeitanlage", "Turnhalle", "Hallenbad"],
    "Gemeindebauten / Rathäuser":           ["Gemeindeamt", "Rathaus", "Gemeindegebäude", "Verwaltungsgebäude"],
    "Feuerwehr / Rettung":                  ["Feuerwehr", "Feuerwehrhaus", "Feuerwehrgebäude", "Rettungsstation"],
    "Sozialwohnbau":                        ["Sozialwohnbau", "Gemeindebau", "Genossenschaftswohnbau", "Sozialbau"],
    "Kultureinrichtungen":                  ["Kulturhaus", "Theater", "Museum", "Musikschule", "Veranstaltungssaal"],
    "Friedhöfe / Kapellen":                 ["Friedhof", "Kapelle", "Aufbahrungshalle", "Friedhofsanlage"],
    "Nutzfahrzeuge / LKW":                  ["Nutzfahrzeug", "LKW", "Transporter", "Fahrzeugbeschaffung"],
    "Feuerwehrfahrzeuge":                   ["Feuerwehrfahrzeug", "Löschfahrzeug", "Einsatzfahrzeug", "Feuerwehrauto"],
    "Rettungsfahrzeuge":                    ["Rettungsfahrzeug", "Krankenwagen", "Notarztwagen", "Rettungsauto"],
    "Kommunalfahrzeuge / Traktoren":        ["Kommunalfahrzeug", "Traktor", "Kommunalmaschine", "Kehrmaschine"],
    "Baumaschinen / Geräte":                ["Baumaschine", "Bagger", "Radlader", "Walze", "Kranwagen"],
    "Krantechnik / Hebetechnik":            ["Kran", "Krantechnik", "Hebetechnik", "Turmdrehkran"],
    "Fahrzeugausstattung":                  ["Fahrzeugausstattung", "Sonderausstattung", "Aufbau", "Fahrzeugumbau"],
    "Werkzeuge / Betriebsmittel":           ["Werkzeug", "Betriebsmittel", "Maschinen", "Geräteankauf"],
    "IT-Ausstattung / Hard- und Software":  ["IT-Ausstattung", "Computer", "Software", "Hardware", "IT-Beschaffung"],
    "Büroausstattung / Mobiliar":           ["Büroausstattung", "Mobiliar", "Büromöbel", "Einrichtung"],
    "Landschaftsbau / Gartengestaltung":    ["Landschaftsbau", "Gartengestaltung", "Grünanlage", "Begrünung"],
    "Parkanlagen / Grünflächen":            ["Parkanlage", "Grünfläche", "Stadtgrün", "Bepflanzung"],
    "Spielplätze / Freizeitanlagen":        ["Spielplatz", "Spielgeräte", "Freizeitanlage", "Kinderspielplatz"],
    "Sportplätze / Kunstrasen":             ["Sportplatz", "Fußballplatz", "Kunstrasen", "Sportanlage"],
    "Bewässerungsanlagen":                  ["Bewässerungsanlage", "Beregnung", "Bewässerungssystem"],
    "Forstarbeiten / Holzschlägerung":      ["Forstarbeiten", "Holzschlägerung", "Waldpflege", "Forstwirtschaft"],
    "Schädlingsbekämpfung / Pflanzenpflege":["Schädlingsbekämpfung", "Pflanzenschutz", "Baumpflege", "Pflanzenpflege"],
    "Flurbereinigung":                      ["Flurbereinigung", "Grundzusammenlegung", "Agrargemeinschaft"],
    "Hotelneubauten / Erweiterungen":       ["Hotel", "Hotelbau", "Hotelerweiterung", "Beherbergung", "Resort"],
    "Gastronomiebetriebe / Konzessionen":   ["Gastronomie", "Restaurant", "Gasthaus", "Konzession", "Gastronomiebetrieb"],
    "Tourismusinfrastruktur":               ["Tourismus", "Tourismusanlage", "Tourismusentwicklung", "Freizeitinfrastruktur"],
    "Seilbahnen / Skilifte":               ["Seilbahn", "Skilift", "Gondelbahn", "Sesselbahn", "Bergbahn"],
    "Campingplätze":                        ["Campingplatz", "Camping", "Zeltplatz", "Wohnmobilstellplatz"],
    "Veranstaltungsstätten":                ["Veranstaltungsstätte", "Messehalle", "Kongresszentrum", "Eventhalle"],
    "Küchen- / Gastronomieausstattung":     ["Küchenausstattung", "Gastronomieausstattung", "Gastrogeräte"],
    "Freizeitparks / Erlebnisanlagen":      ["Freizeitpark", "Erlebnisanlage", "Attraktionen", "Freizeiteinrichtung"],
    "Architektur / Gebäudeplanung":         ["Architekt", "Architektur", "Gebäudeplanung", "Planung", "Entwurf"],
    "Statik / Tragwerksplanung":            ["Statik", "Tragwerksplanung", "Statiker", "Tragwerksplaner"],
    "Vermessung / Geodäsie":                ["Vermessung", "Geodäsie", "Vermessungsbüro", "Kataster", "Lageplan"],
    "Umweltgutachten / UVP":                ["Umweltgutachten", "UVP", "Umweltverträglichkeit", "Gutachten"],
    "Projektmanagement / Bauleitung":       ["Projektmanagement", "Bauleitung", "Projektsteuerung", "Örtliche Bauaufsicht"],
    "Energieberatung":                      ["Energieberatung", "Energieausweis", "Energieeffizienz", "Energiekonzept"],
    "Rechtsberatung / Vergaberecht":        ["Vergaberecht", "Rechtsberatung", "Ausschreibung", "Vergabeverfahren"],
    "Finanzierung / Fördermittel":          ["Förderung", "Fördermittel", "Wohnbauförderung", "Investitionsförderung"],
    "Landwirtschaftliche Bauten / Stallbau":["Stallbau", "Landwirtschaftliches Gebäude", "Halle", "Maschinenhalle"],
    "Silos / Lagerhallen":                  ["Silo", "Lagerhalle", "Getreidesilo", "Lagergebäude"],
    "Biogasanlagen":                        ["Biogasanlage", "Biogas", "Vergärungsanlage"],
    "Bewässerung / Drainage":               ["Bewässerung", "Drainage", "Drainagesystem", "Entwässerung"],
    "Forststraßen":                         ["Forststraße", "Waldweg", "Forstweg", "Erschließung"],
    "Landmaschinen / Geräte":               ["Landmaschine", "Traktor", "Erntemaschine", "Landwirtschaftsmaschine"],
    "Weinbau / Obstbau Infrastruktur":      ["Weinbau", "Obstbau", "Weingut", "Mosterei", "Kellerei"],
    "Fischzucht / Aquakultur":              ["Fischzucht", "Aquakultur", "Fischteich", "Fischerei"],
    "Industriehallen / Werkshallen":        ["Industriehalle", "Werkshalle", "Produktionshalle", "Fabrik"],
    "Gewerbeparks / Betriebsanlagen":       ["Gewerbepark", "Betriebsanlage", "Betriebsgebäude", "Gewerbezone"],
    "Produktionsanlagen":                   ["Produktionsanlage", "Fertigungsanlage", "Produktionsstätte"],
    "Lagerhallen / Logistikzentren":        ["Lagerhalle", "Logistikzentrum", "Logistikhalle", "Distributionszentrum"],
    "Reinräume / Labore":                   ["Reinraum", "Labor", "Laborgebäude", "Reinraumanlage"],
    "Tankstellen / Waschanlagen":           ["Tankstelle", "Waschanlage", "Tankstellenbau", "Autopflegeanlage"],
    "Kälteanlagen / Kühlhäuser":            ["Kälteanlage", "Kühlhaus", "Tiefkühlanlage", "Kältetechnik"],
    "Fördertechnik / Förderanlagen":        ["Fördertechnik", "Förderanlage", "Förderband", "Materialfluss"],
    "Transportbeton / Lieferbeton":         ["Transportbeton", "Lieferbeton", "Betonlieferung", "Fertigbeton", "Betonwerk"],
    "Baustoffhandel / Baustofflieferanten": ["Baustoffhandel", "Baustoffe", "Baustofflieferant", "Baustoffhandlung", "Baumarkt"],
    "Sporthallen / Bäder":                  ["Sporthalle", "Hallenbad", "Freibad", "Schwimmbad", "Sportzentrum", "Turnhalle"],
    "Kommunalfahrzeuge":                    ["Kommunalfahrzeug", "Kehrmaschine", "Winterdienst", "Streufahrzeug", "Kommunalmaschine"],
    "Spielplätze":                          ["Spielplatz", "Spielgeräte", "Kinderspielplatz", "Spielplatzgestaltung"],
    "Gebäudereinigung / Baureinigung":      ["Gebäudereinigung", "Baureinigung", "Bauendreinigung", "Unterhaltsreinigung"],
    "Facility Management / Hausverwaltung": ["Facility Management", "Hausverwaltung", "Gebäudemanagement", "Liegenschaftsverwaltung"],
    "Sicherheitstechnik / Videoüberwachung / Zutritt": ["Sicherheitstechnik", "Videoüberwachung", "Zutrittskontrolle", "Alarmanlage"],
}

BASIS_KEYWORDS = ["Ausschreibung", "Vergabe", "Baubewilligung", "Projekt", "Vorhaben", "Planung", "Beschluss"]

def baue_suchbegriffe(auftrag: dict) -> list[str]:
    begriffe = list(BASIS_KEYWORDS)
    gewerke = auftrag.get("gewerke") or []
    if isinstance(gewerke, str):
        try: gewerke = json.loads(gewerke)
        except Exception: gewerke = [gewerke]
    for gewerk in gewerke:
        if gewerk in GEWERK_KEYWORDS:
            begriffe.extend(GEWERK_KEYWORDS[gewerk])
    zusatz = auftrag.get("zusatz_keywords") or ""
    if zusatz:
        for kw in re.split(r"[,;\n]+", zusatz):
            kw = kw.strip()
            if kw: begriffe.append(kw)
    gesehen = set()
    ergebnis = []
    for b in begriffe:
        if b.lower() not in gesehen:
            gesehen.add(b.lower())
            ergebnis.append(b)
    print(f"  🔍 Suchbegriffe ({len(ergebnis)}): {', '.join(ergebnis[:8])}{'...' if len(ergebnis) > 8 else ''}")
    return ergebnis


# =============================================================================
# SCHRITT 3 + 4 + 5: MEHRGLEISIGE KI-SUCHE MIT WEB_SEARCH-TOOL
#
# Kernidee: Claude sucht SELBST aktiv im Internet (web_search-Tool) – pro
# Bundesland aus MEHREREN unabhängigen Blickwinkeln ("Suchgleisen"):
#   1. Projektsuche  – Bau/Tiefbau/Infrastruktur (gewerk-fokussiert)
#   2. Projektsuche  – Energie/Haustechnik (nur wenn solche Gewerke gewählt)
#   3. Vergabeportale – dedizierte Ausschreibungs-/Vergabesuche (ankoe, TED, ...)
#   4. Kommunal       – Gemeinderats-/Stadtratsbeschlüsse PRO BEZIRK mit echten
#                       Ortsnamen aus der Gemeinden-Datenbank
#
# Jedes Gleis ist ein eigener API-Call mit bis zu WEB_SEARCH_MAX_USES Suchen.
# Dadurch deutlich mehr und vielfältigere Treffer als mit einer Pauschalsuche.
# =============================================================================

def berechne_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# MEHRQUELLEN: gleiches Bauvorhaben unter verschiedenen Quellen erkennen
# ─────────────────────────────────────────────────────────────────────────────
_TITEL_STOPWORTS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines",
    "einem", "einen", "und", "oder", "mit", "ohne", "fuer", "für", "von", "vom",
    "zur", "zum", "neue", "neuer", "neues", "geplant", "geplante", "plant",
    "kommt", "baut", "neubau", "sanierung", "saniert", "projekt", "projekte",
    "bauprojekt", "bauvorhaben", "vorhaben", "spatenstich", "eroeffnung",
    "eröffnung", "startet", "investiert", "investition", "millionen", "euro",
    "mio", "gemeinde", "stadt", "stadtgemeinde", "marktgemeinde", "bezirk",
    "tirol", "wien", "oberoesterreich", "niederoesterreich", "steiermark",
    "kaernten", "salzburg", "vorarlberg", "burgenland",
    "wird", "wurde", "werden", "gebaut", "errichtet", "entsteht", "entstehen",
    "soll", "sollen", "fertig", "fertiggestellt", "modernisiert", "erweitert",
    "laeuft", "läuft", "kosten", "bauarbeiten", "baustart", "fertigstellung",
}


def _titel_tokens(text: str, ort: str = "") -> set:
    """Unterscheidende Wort-Tokens eines Titels (ohne Füllwörter, Ortsname, kurze Tokens)."""
    roh = re.sub(r"[^0-9a-zäöüß ]", " ", (text or "").lower())
    ort_tokens = set(re.sub(r"[^0-9a-zäöüß ]", " ", (ort or "").lower()).split())
    out = set()
    for t in roh.split():
        if len(t) < 4 or t in _TITEL_STOPWORTS or t in ort_tokens:
            continue
        out.add(t)
    return out


def _titel_stark_aehnlich(titel1: str, titel2: str, ort: str = "") -> bool:
    """True, wenn zwei Titel praktisch dasselbe sagen – erspart einen KI-Aufruf."""
    a, b = _titel_tokens(titel1, ort), _titel_tokens(titel2, ort)
    if not a or not b:
        return False
    gemeinsam = len(a & b)
    union = len(a | b)
    if union == 0:
        return False
    return (gemeinsam / union) >= 0.7 and gemeinsam >= 1


def _parse_match_nummer(text: str, anzahl: int) -> int:
    """Erste Zahl 1..anzahl aus der KI-Antwort, sonst 0."""
    m = re.search(r"\d+", text or "")
    if not m:
        return 0
    n = int(m.group(0))
    return n if 1 <= n <= anzahl else 0


def _ki_gleiches_projekt(analyse: dict, kandidaten: list):
    """
    Fragt Haiku, ob das neue Projekt dasselbe reale Bauvorhaben ist wie eines der
    bestehenden Kandidaten (alle im SELBEN Ort). Gibt den Treffer-Datensatz oder None.
    Bei jedem Fehler: None (der Lauf läuft normal weiter).
    """
    if not kandidaten:
        return None
    zeilen = []
    for i, k in enumerate(kandidaten, 1):
        besch = (k.get("beschreibung") or "")[:160]
        zeilen.append(f"[{i}] {k.get('titel', '')} — {besch}")
    neu_besch = (analyse.get("beschreibung") or "")[:160]
    prompt = (
        "Du prüfst, ob ein NEU gefundenes Bauprojekt dasselbe reale Vorhaben ist "
        "wie ein bereits bekanntes. Alle liegen im selben Ort.\n\n"
        f"NEU:\nTitel: {analyse.get('titel', '')}\nBeschreibung: {neu_besch}\n\n"
        "BEKANNTE PROJEKTE:\n" + "\n".join(zeilen) + "\n\n"
        "Welche Nummer bezeichnet DASSELBE reale Bauvorhaben wie das NEUE? "
        "Antworte NUR mit der Nummer. Wenn keines exakt dasselbe Projekt ist, antworte 0."
    )
    try:
        response = ANTHROPIC_CLIENT.messages.create(
            model=MODELL_HAIKU,
            max_tokens=8,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            _TOKEN_STATS["input"] += response.usage.input_tokens
            _TOKEN_STATS["output"] += response.usage.output_tokens
            _TOKEN_STATS["calls"] += 1
        except Exception:
            pass
        text = " ".join(
            b.text for b in response.content
            if getattr(b, "type", None) == "text" and getattr(b, "text", "")
        ).strip()
    except Exception as e:
        print(f"    ⚠️  Quellen-Abgleich (KI) übersprungen: {str(e)[:120]}")
        return None
    nr = _parse_match_nummer(text, len(kandidaten))
    return kandidaten[nr - 1] if nr >= 1 else None


def _finde_gleiches_projekt(analyse: dict, kandidaten: list):
    """
    Sucht unter bestehenden Projekten IM SELBEN ORT dasselbe Bauvorhaben.
    Erst günstig per Titel-Ähnlichkeit, sonst per KI. Niemals über Ortsgrenzen
    hinweg (kandidaten enthalten ausschließlich denselben Ort).
    """
    if not kandidaten:
        return None
    ort = analyse.get("ort", "")
    neu_titel = analyse.get("titel", "")
    for k in kandidaten:
        if _titel_stark_aehnlich(k.get("titel", ""), neu_titel, ort):
            return k
    kand = kandidaten
    if len(kand) > 10:
        neu_tok = _titel_tokens(neu_titel, ort)
        kand = sorted(
            kand,
            key=lambda k: len(_titel_tokens(k.get("titel", ""), ort) & neu_tok),
            reverse=True,
        )[:10]
    return _ki_gleiches_projekt(analyse, kand)


def _quelle_anhaengen(treffer: dict, artikel: dict, auftrag: dict, jetzt: str) -> None:
    """
    Hängt eine neue Quelle an ein bestehendes Projekt an (ohne Duplikat).
    Kam die Quelle in einem SPÄTEREN Lauf hinzu, wird der Neu-Zähler erhöht
    (Glocke im Dashboard).
    """
    quellen = treffer.get("quellen") or []
    if isinstance(quellen, str):
        try:
            quellen = json.loads(quellen)
        except Exception:
            quellen = []
    if not isinstance(quellen, list):
        quellen = []
    # Aeltere Zeile ohne Quellenliste: bisherige Einzelquelle (artikel_url) uebernehmen,
    # damit sie beim Anhaengen einer neuen Quelle nicht verloren geht.
    # (Nur echte http-URLs – kein alter "search://"-Platzhalter.)
    alte_url = str(treffer.get("artikel_url") or "")
    if not quellen and alte_url.startswith("http"):
        quellen.append({"url": alte_url,
                        "quelle_name": treffer.get("quelle") or "Quelle",
                        "gefunden_am": treffer.get("erstmals_gefunden") or jetzt})
    neue_url = str(artikel["url"])
    # Ist die neue Quelle kein echter Link (z.B. "search://"-Platzhalter), nichts
    # anhängen – nur den Zeitstempel aktualisieren.
    if not neue_url.startswith("http"):
        sb_patch("projekte",
                 {"id": f"eq.{treffer['id']}", "kunden_id": f"eq.{auftrag['kunden_id']}"},
                 {"zuletzt_gecrawlt": jetzt})
        return
    if any(isinstance(q, dict) and q.get("url") == neue_url for q in quellen):
        sb_patch("projekte",
                 {"id": f"eq.{treffer['id']}", "kunden_id": f"eq.{auftrag['kunden_id']}"},
                 {"zuletzt_gecrawlt": jetzt})
        return
    quellen.append({"url": neue_url, "quelle_name": artikel["quelle_name"], "gefunden_am": jetzt})
    update = {
        "quellen":           quellen,
        "zuletzt_geaendert": jetzt,
        "zuletzt_gecrawlt":  jetzt,
    }
    lauf_start = auftrag.get("_lauf_start_iso") or ""
    erstmals = treffer.get("erstmals_gefunden") or ""
    if lauf_start and erstmals and erstmals < lauf_start:
        update["neue_quellen_anzahl"] = int(treffer.get("neue_quellen_anzahl") or 0) + 1
        print(f"    🔔 Neue Quelle fuer bestehendes Projekt: {artikel['quelle_name']} -> {str(treffer.get('titel',''))[:50]}")
    else:
        print(f"    ➕ Zusatzquelle: {artikel['quelle_name']} -> {str(treffer.get('titel',''))[:50]}")
    sb_patch("projekte",
             {"id": f"eq.{treffer['id']}", "kunden_id": f"eq.{auftrag['kunden_id']}"},
             update)


def waehle_quellen(auftrag: dict) -> list:
    """Alt-Funktion (Crawling) – nicht mehr verwendet, nur Abwärtskompatibilität."""
    return []


# Gewerke die zur Energie/Haustechnik-Gruppe gehören
_ENERGIE_GEWERKE = {
    "PV-Anlagen / Photovoltaik", "Wärmepumpen", "Erdwärmebohrungen",
    "Windkraftanlagen", "E-Ladeinfrastruktur", "Biomasse / Nahwärme",
    "Energiesanierung", "Batteriespeicher", "Wasserkraft",
    "Heizung / Lüftung / Klima (HVAC)", "Elektriker / Elektrotechnik",
    "Installateur / Sanitär", "Gebäudeautomation / Smart Building",
    "Recycling / Kreislaufwirtschaft",
}

def _gruppiere_gewerke_fuer_suche(gewerke: list) -> dict:
    """
    Teilt die gewählten Gewerke in max. 2 fokussierte Projekt-Suchgruppen:
      - "Bau, Tiefbau & Infrastruktur"
      - "Energie, Haustechnik & Sanierung"
    Eine eigene, fokussierte Suche je Gruppe liefert bessere Treffer als eine
    breite Pauschalsuche. Vergabe- und Kommunalsuche laufen davon unabhängig.
    """
    bau     = [g for g in gewerke if g not in _ENERGIE_GEWERKE]
    energie = [g for g in gewerke if g in _ENERGIE_GEWERKE]

    gruppen = {}
    if bau:
        gruppen["Bau, Tiefbau & Infrastruktur"] = bau
    if energie:
        gruppen["Energie, Haustechnik & Sanierung"] = energie
    if not gruppen:
        # Kunde hat keine Gewerke gewählt → breite Standardsuche
        gruppen["Bauprojekte allgemein"] = ["Neubau", "Sanierung", "Spatenstich", "Ausschreibung"]
    return gruppen


def _bezirke_mit_orten(bundesland: str, max_bezirke: int = 20,
                       orte_pro_bezirk: int = 6) -> dict:
    """
    Baut {Bezirk: [Ortsnamen]} aus der Gemeinden-Datenbank für ein Bundesland.
    Liefert echte Ortsnamen, mit denen die Kommunalsuche gezielt arbeiten kann.
    """
    try:
        gemeinden = get_gemeinden_fuer_bundeslaender([bundesland])
    except Exception:
        return {}
    bezirke: dict = {}
    for g in gemeinden:
        bez = (g.get("bezirk") or "").strip()
        name = (g.get("name") or "").strip()
        if not bez or not name:
            continue
        bezirke.setdefault(bez, [])
        if len(bezirke[bez]) < orte_pro_bezirk:
            bezirke[bez].append(name)
    # Auf max_bezirke begrenzen (größte zuerst, damit Ballungsräume dabei sind)
    sortiert = sorted(bezirke.items(), key=lambda kv: len(kv[1]), reverse=True)
    return dict(sortiert[:max_bezirke])


# -----------------------------------------------------------------------------
# Gemeinsames System-Prompt + robuster JSON-Parser + zentraler API-Aufruf
# -----------------------------------------------------------------------------

SUCHE_SYSTEM_PROMPT = """Du bist ein professioneller Projekt-Scout für ProjectScout, eine österreichische B2B-Plattform. Unternehmen BEZAHLEN dafür, relevante Bau-, Infrastruktur- und Energieprojekte sowie öffentliche Ausschreibungen frühzeitig zu finden. Je mehr brauchbare Treffer du lieferst, desto wertvoller ist dein Ergebnis.

ARBEITSWEISE (WICHTIG):
- Nutze das web_search-Tool INTENSIV und führe MEHRERE verschiedene Suchen durch – nicht nur eine einzige.
- Variiere Suchbegriffe systematisch: kombiniere Ortsnamen + Gewerk + Signalwort (Spatenstich, Baustart, Baubeginn, Ausschreibung, Vergabe, Gemeinderat, Bebauungsplan, Investition, Erweiterung, Neubau, Sanierung).
- Öffne vielversprechende Treffer und lies Details heraus.
- QUELLENDISZIPLIN: Das Feld artikel_url muss EXAKT die Seite sein, aus der du die Projektdetails entnommen hast. Eine URL, die nur zur selben Website gehört, aber etwas anderes beschreibt (Startseite, News-Übersicht, anderer Artikel), ist FALSCH. Kennst du die exakte Projekt-URL nicht sicher, lass artikel_url leer.
- Sammle ALLE konkreten Projekte, auch kleinere oder regionale. Ziel sind 15-25 Treffer, wenn die Region sie hergibt. Lieber 20 Projekte als 5.

BEVORZUGTE QUELLEN & SUCHOPERATOREN (nutze gezielt site:-Operatoren!):
- Gemeinde-/Behördenseiten: site:gv.at (z.B. "Bauprojekt site:noe.gv.at"), site:ris.bka.gv.at (Rechtsinformationssystem, Verordnungen/Flächenwidmungen)
- UVP-Verfahren der Landesregierungen (Großvorhaben wie Straßen, Industrie, Einkaufszentren, Kraftwerke): "UVP Verfahren laufend [Bundesland]", site:land-oberoesterreich.gv.at, site:noe.gv.at, site:ktn.gv.at usw.
- Landes-Amtsblätter und Kundmachungen der Länder
- Kommunalarchive: site:kommunalarchive.at
- Offizielles Amtsblatt des Bundes: site:evi.gv.at
- Bei Grundstücken/Immobilien zusätzlich: site:willhaben.at, site:immobilienscout24.at, site:immowelt.at (Grundstücke, Bauträgerprojekte, Neubauprojekte)
- Lokalmedien: site:meinbezirk.at, site:tips.at, ORF-Landesstudios (z.B. site:noe.orf.at)

WAS ZÄHLT ALS TREFFER:
- Neubau, Umbau, Sanierung, Erweiterung von Gebäuden/Anlagen
- Infrastruktur: Straße, Brücke, Kanal, Wasserleitung, Bahn, Tunnel, Radweg
- Energie: PV, Windkraft, Wärmepumpe, Nahwärme/Fernwärme, Stromnetz, Speicher
- Öffentliche Ausschreibungen/Vergaben mit Bauleistung
- Gemeinderats-/Stadtratsbeschlüsse zu Bauvorhaben (Schule, Kindergarten, Feuerwehr, Bauhof, Amtsgebäude, Kanal, Wasser)
- Gewerbe-/Industrieansiedlungen und -erweiterungen
- Grundstücksentwicklungen mit Bauabsicht, Um-/Widmungen

WAS NICHT ZÄHLT: Unfälle, Brände, Kriminalität, Meinungsartikel, Veranstaltungen, Personalnachrichten, reine Statistiken, bereits abgeschlossene Projekte ohne neue Bauphase.

AUSGABE: AUSSCHLIESSLICH ein JSON-Array. Kein Text davor/danach, kein Markdown, keine Backticks.
[
  {
    "titel": "Prägnanter Projekttitel (max 80 Zeichen)",
    "beschreibung": "2-3 Sätze: WAS wird gebaut, WO genau, WANN (Zeitplan), WER ist Bauherr/Auftraggeber, WIE GROSS (Volumen/Fläche).",
    "ort": "Gemeinde/Stadt",
    "bezirk": "politischer Bezirk oder leer",
    "bundesland": "W/NOE/OOE/SBG/STK/KTN/TIR/VBG/BGR",
    "kategorie": "Hochbau/Tiefbau/Energie/Infrastruktur/Immobilien/Öffentlich/Industrie/Sonstiges",
    "volumen": "z.B. '12 Mio. Euro' oder '3.500 m²' oder leer",
    "phase": "Planung/Ausschreibung/Vergabe/Bau/Fertigstellung",
    "relevanz": 7,
    "datum": "YYYY-MM-DD des VERÖFFENTLICHUNGS-/Berichtsdatums (NICHT die Angebotsfrist!), sonst leer",
    "artikel_url": "vollständige https-URL GENAU der Seite, die DIESES Projekt beschreibt – niemals Startseite/Übersicht/anderer Artikel; im Zweifel leer",
    "quelle_name": "z.B. meinbezirk.at, OÖN, ankoe.at"
  }
]

RELEVANZ-SKALA:
10 = laufende Ausschreibung mit Vergabesumme · 8-9 = beschlossenes Projekt mit Budget+Termin · 6-7 = konkret geplant mit Details · 4-5 = angekündigt/erste Infos · 1-3 = vage.

Gib lieber mehr Treffer zurück. Findest du wirklich nichts: []"""


def _parse_json_array(text: str) -> list:
    """
    Robuster Parser: extrahiert ein JSON-Array auch wenn die Antwort
    abgeschnitten ist (max_tokens) oder Zusatztext enthält. Fällt auf das
    Einsammeln einzelner vollständiger {...}-Objekte zurück.
    """
    if not text:
        return []
    start = text.find("[")
    if start < 0:
        return []
    ende = text.rfind("]") + 1
    if ende > start:
        try:
            ergebnis = json.loads(text[start:ende])
            if isinstance(ergebnis, list):
                return ergebnis
        except json.JSONDecodeError:
            pass
    # Fallback: einzelne Objekte herausschneiden (toleriert Abbruch/Trailing-Kommas)
    objekte = []
    tiefe = 0
    obj_start = -1
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            if tiefe == 0:
                obj_start = i
            tiefe += 1
        elif c == "}":
            if tiefe > 0:
                tiefe -= 1
                if tiefe == 0 and obj_start >= 0:
                    try:
                        objekte.append(json.loads(text[obj_start:i + 1]))
                    except json.JSONDecodeError:
                        pass
                    obj_start = -1
    return objekte



# ─────────────────────────────────────────────────────────────────────────────
# LINK-VERIFIKATION für Websuche-Treffer
# Hintergrund: Bei web_search-Treffern stammt die artikel_url vom Suchmodell.
# In Einzelfällen ordnet es eine real existierende, aber inhaltlich FALSCHE
# Seite zu (z.B. Vereins-News statt Bauprojekt-Artikel). Diese Prüfung lädt
# jede gemeldete URL einmal pro Lauf und prüft, ob der Seiteninhalt zum
# Projekt passt. Unpassende sowie tote (404/410) Links werden ENTFERNT – das
# Projekt selbst bleibt erhalten und erscheint im Dashboard mit grauem
# Herkunftshinweis statt Link.
# BEWUSST VORSICHTIG: Seiten, die sich nicht beurteilen lassen (Bot-Block 403,
# Timeout, JavaScript-Apps mit fast leerem HTML, große/gescannte PDFs),
# behalten ihren Link – lieber ein nicht prüfbarer echter Link als ein
# fälschlich entfernter. Crawling-/Regionalmedien-Treffer durchlaufen die
# Prüfung NICHT: deren URLs setzt der Agent selbst aus bereits geladenen
# Seiten, sie sind also konstruktionsbedingt korrekt.
# ─────────────────────────────────────────────────────────────────────────────

_LINK_CHECK_WORKERS = 8       # parallele Downloads bei der Prüfung
_LINK_CHECK_TIMEOUT = 8       # Sekunden je Verbindungs-/Leseschritt
_LINK_CHECK_MAX_BYTES = 3_000_000   # mehr wird nicht geladen (Schutz vor Riesendateien)
_LINK_CACHE: dict = {}        # url -> ("ok"|"tot"|"unpruefbar", normalisierter_text)

_LINK_CHECK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36")


def _norm_match_text(s: str) -> str:
    """Vergleichs-Normalisierung: Kleinschreibung, ae/oe/ue/ss, nur [0-9a-z ]."""
    s = (s or "").lower()
    s = (s.replace("ä", "ae").replace("ö", "oe")
          .replace("ü", "ue").replace("ß", "ss"))
    return re.sub(r"[^0-9a-z ]", " ", s)


def _lade_seitentext(url: str) -> tuple:
    """
    Lädt eine URL (max. _LINK_CHECK_MAX_BYTES) und gibt zurück:
      ("ok", norm_text)   – Inhalt geladen und als Text extrahiert
      ("tot", "")         – Seite existiert nicht mehr (404/410)
      ("unpruefbar", "")  – nicht beurteilbar (Block/Timeout/JS-Shell/Scan-PDF)
    Ergebnis wird pro Lauf gecacht (gleiche URL nur einmal geladen).
    """
    if url in _LINK_CACHE:
        return _LINK_CACHE[url]
    ergebnis = ("unpruefbar", "")
    try:
        resp = requests.get(
            url, timeout=(5, _LINK_CHECK_TIMEOUT), stream=True,
            allow_redirects=True, headers={"User-Agent": _LINK_CHECK_UA},
        )
        if resp.status_code in (404, 410):
            ergebnis = ("tot", "")
        elif resp.status_code == 200:
            inhalt = b""
            abgeschnitten = False
            for chunk in resp.iter_content(chunk_size=65536):
                inhalt += chunk
                if len(inhalt) > _LINK_CHECK_MAX_BYTES:
                    abgeschnitten = True
                    break
            ct = (resp.headers.get("content-type") or "").lower()
            ist_pdf = "pdf" in ct or url.lower().split("?")[0].endswith(".pdf")
            if ist_pdf:
                # Abgeschnittene PDFs sind nicht lesbar -> nicht beurteilen.
                txt = "" if abgeschnitten else _pdf_text(inhalt)
                if txt and len(txt) >= 300:
                    ergebnis = ("ok", _norm_match_text(txt))
            else:
                try:
                    html = inhalt.decode("utf-8")
                except UnicodeDecodeError:
                    html = inhalt.decode("latin-1", errors="replace")
                try:
                    from bs4 import BeautifulSoup
                    txt = BeautifulSoup(html, "html.parser").get_text(" ")
                except Exception:
                    txt = html
                if txt and len(txt.strip()) >= 300:
                    ergebnis = ("ok", _norm_match_text(txt))
                # sonst: fast leeres HTML (JS-App) -> unpruefbar, Link behalten
        # andere Statuscodes (403/401/429/5xx): Seite existiert vermutlich,
        # blockt nur unseren Server -> unpruefbar, Link behalten
        resp.close()
    except Exception:
        # Timeout/Netzfehler -> im Zweifel behalten (kein falsches Entfernen)
        ergebnis = ("unpruefbar", "")
    _LINK_CACHE[url] = ergebnis
    return ergebnis


def _link_passt_zum_projekt(projekt: dict, norm_text: str) -> bool:
    """
    Inhaltliche Passung: Die unterscheidenden Wörter des Projekttitels (ohne
    Füllwörter und Ortsname, via _titel_tokens) müssen auf der Seite vorkommen –
    mindestens 2 (bzw. alle, wenn der Titel weniger hergibt). Beispiel: Für
    "Neubau Feuerwehrhaus Alkoven (10-torig, inkl. KAT-Lager)" sind das
    "feuerwehrhaus", "torig", "lager" – ein Vereinsbericht über eine
    Flurreinigungsaktion enthält höchstens eines davon und fällt durch,
    der echte Projektartikel enthält sie und besteht.
    """
    titel = str(projekt.get("titel") or "")
    ort   = str(projekt.get("ort") or "")
    tokens = [_norm_match_text(t).strip() for t in _titel_tokens(titel, ort)]
    tokens = [t for t in tokens if t]
    if not tokens:
        # Kein unterscheidendes Titelwort -> auf Ortsnamen zurückfallen
        o = _norm_match_text(ort.split("(")[0].split("/")[0].split(",")[0]).strip()
        return (o in norm_text) if o else True
    treffer = sum(1 for t in tokens if t in norm_text)
    return treffer >= min(2, len(tokens))


def _pruefe_quell_links(projekte: list) -> list:
    """
    Verifiziert die artikel_url aller Websuche-Treffer parallel. Links, die tot
    sind oder deren Seiteninhalt nicht zum Projekt passt, werden geleert – die
    Projekte selbst bleiben erhalten (Dashboard zeigt dann den Herkunftshinweis).
    """
    kandidaten = [p for p in projekte
                  if isinstance(p, dict)
                  and str(p.get("artikel_url") or "").strip().startswith("http")]
    if not kandidaten:
        return projekte
    from concurrent.futures import ThreadPoolExecutor
    urls = list({str(p["artikel_url"]).strip() for p in kandidaten})
    with ThreadPoolExecutor(max_workers=_LINK_CHECK_WORKERS) as ex:
        list(ex.map(_lade_seitentext, urls))  # füllt den Cache parallel
    weg_tot = weg_falsch = 0
    for p in kandidaten:
        url = str(p["artikel_url"]).strip()
        status, norm_text = _LINK_CACHE.get(url, ("unpruefbar", ""))
        if status == "tot":
            p["artikel_url"] = ""
            weg_tot += 1
        elif status == "ok" and not _link_passt_zum_projekt(p, norm_text):
            p["artikel_url"] = ""
            weg_falsch += 1
    if weg_tot or weg_falsch:
        print(f"    🔗 Link-Prüfung: {len(urls)} URLs geprüft, "
              f"{weg_falsch} inhaltlich unpassende und {weg_tot} tote Links entfernt")
    return projekte


def _websearch_aufruf(prompt: str, modell: str,
                      max_searches: int = WEB_SEARCH_MAX_USES) -> list:
    """
    Zentraler web_search-gestützter API-Aufruf. Gibt die geparste Projektliste
    zurück. Fehler werden abgefangen und als leere Liste zurückgegeben, damit
    ein einzelner fehlgeschlagener Call nie den ganzen Lauf abbricht.
    """
    try:
        response = ANTHROPIC_CLIENT.messages.create(
            model=modell,
            max_tokens=MAX_TOKENS_SUCHE,
            system=SUCHE_SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_searches,
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        # Token-/Kosten-Statistik
        try:
            _TOKEN_STATS["input"]  += response.usage.input_tokens
            _TOKEN_STATS["output"] += response.usage.output_tokens
            # Tatsächliche Anzahl der von Claude durchgeführten Web-Suchen.
            # Die API meldet das exakt in usage.server_tool_use – dadurch wird die
            # Suchkosten-Berechnung präzise statt geschätzt.
            _stu = getattr(response.usage, "server_tool_use", None)
            if _stu is not None:
                _TOKEN_STATS["searches"] += int(getattr(_stu, "web_search_requests", 0) or 0)
        except Exception:
            pass
        _TOKEN_STATS["calls"] += 1

        text = " ".join(
            b.text for b in response.content
            if getattr(b, "type", None) == "text" and getattr(b, "text", "")
        ).strip()
        projekte = _parse_json_array(text)
        if not isinstance(projekte, list):
            return []
        # Von der Websuche gemeldete Links inhaltlich verifizieren (s. oben).
        return _pruefe_quell_links(projekte)
    except Exception as ex:
        print(f"    ❌ web_search Fehler: {str(ex)[:140]}")
        return []


def _analyse_aufruf(prompt: str, modell: str, max_tokens: int = 3500) -> list:
    """
    Reiner KI-Analyse-Aufruf OHNE web_search-Tool. Wird verwendet, um bereits
    gecrawlten Text (Gemeinde-Websites/Protokolle) auf relevante Vorhaben zu
    analysieren. Deutlich günstiger als ein web_search-Call.
    """
    try:
        response = ANTHROPIC_CLIENT.messages.create(
            model=modell,
            max_tokens=max_tokens,
            system=SUCHE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            _TOKEN_STATS["input"]  += response.usage.input_tokens
            _TOKEN_STATS["output"] += response.usage.output_tokens
        except Exception:
            pass
        _TOKEN_STATS["calls"] += 1
        text = " ".join(
            b.text for b in response.content
            if getattr(b, "type", None) == "text" and getattr(b, "text", "")
        ).strip()
        projekte = _parse_json_array(text)
        return projekte if isinstance(projekte, list) else []
    except Exception as ex:
        print(f"    ❌ Analyse-Fehler: {str(ex)[:140]}")
        return []


# -----------------------------------------------------------------------------
# Die vier Suchgleise
# -----------------------------------------------------------------------------

def suche_projekte(bundesland: str, kategorie_name: str, gewerke_liste: list,
                   suchbegriffe: list, cutoff: str, heute: str, modell: str) -> list:
    """Suchgleis 1/2: gewerk-fokussierte Projekt-/Bausuche in Medien."""
    bl_name     = BL_NAMEN.get(bundesland, bundesland)
    gewerke_txt = ", ".join(gewerke_liste[:18]) if gewerke_liste else kategorie_name
    signale     = ", ".join(suchbegriffe[:10])

    prompt = f"""Suche aktuelle Projekte im Bereich "{kategorie_name}" in {bl_name} (Österreich).

ZEITRAUM: Nur Projekte/Meldungen vom {cutoff} bis {heute}. Ältere Berichte ignorieren.

GESUCHTE GEWERKE DES KUNDEN: {gewerke_txt}
NÜTZLICHE SIGNALWÖRTER: {signale}

FÜHRE MEHRERE VERSCHIEDENE WEB-SUCHEN DURCH, z.B.:
- "Spatenstich {bl_name} {heute[-4:]}"
- "Baustart Neubau {bl_name}"
- "Bauprojekt {bl_name} Investition Millionen"
- "[größere Stadt in {bl_name}] Neubau Projekt"
- einzelne Gewerke aus der Liste + Ortsname in {bl_name}
- "Betriebserweiterung {bl_name}" / "Gewerbepark {bl_name}"

QUELLEN u.a.: meinbezirk.at, nachrichten.at (OÖN), tips.at, ots.at, krone.at,
diepresse.com, derstandard.at, industriemagazin.at, wirtschaftszeit,
Bezirksblätter, Landespresse.

Sammle ALLE konkreten Treffer (auch kleinere). Antworte als JSON-Array."""
    return _websearch_aufruf(prompt, modell)


def suche_vergaben(bundesland: str, suchbegriffe: list,
                   cutoff: str, heute: str, modell: str) -> list:
    """Suchgleis 3: dedizierte Suche auf österreichischen Vergabeportalen."""
    bl_name = BL_NAMEN.get(bundesland, bundesland)
    signale = ", ".join(suchbegriffe[:8])

    prompt = f"""Suche AKTUELLE ÖFFENTLICHE AUSSCHREIBUNGEN und VERGABEN für Bauleistungen in {bl_name} (Österreich).

ZEITRAUM: Ausschreibungen aktiv oder veröffentlicht vom {cutoff} bis {heute}.

DURCHSUCHE GEZIELT DIESE VERGABEPORTALE (mehrere Suchen!) – die ersten liefern auch ohne Login öffentlich auffindbare Treffer:
- ausschreibung.at (Fachportal Bau: Hochbau, Tiefbau, Haustechnik – Titel/Ort/Frist meist frei sichtbar)
- usp.gv.at Ausschreibungssuche (zentrale Bekanntmachungen aller öffentlichen Auftraggeber Österreichs)
- auftrag.at (öffentliche Aufträge je Bundesland)
- ted.europa.eu (EU-weite Ausschreibungen über der Schwelle, Land = Österreich)
- vergabeportal.at (ANKÖ), evi.gv.at (digitales Amtsblatt der Republik), bbg.gv.at, lieferanzeiger.at, offenevergaben.at, architekturwettbewerb.at
- Vergabe-/Beschaffungsplattformen der Länder und größeren Städte

TIPP: Achte besonders auf SUB-Schwellen-Aufträge (unter ~5,4 Mio. €) – die sind für KMU/Handwerk am relevantesten und stehen oft nur als Titel auf den Portalen oder auf der Gemeinde-Amtstafel. Auch CPV-Bauklassen (45 Bau/Tiefbau, 71 Planung, 09 Energie) können als zusätzlicher Suchbegriff helfen, z.B. "CPV 45 Bauauftrag {bl_name}".

SUCHE u.a. NACH: "Bauausschreibung {bl_name}", "Vergabe Bauleistung {bl_name}",
"Generalunternehmer Ausschreibung {bl_name}", "Hochbau/Tiefbau Ausschreibung {bl_name}",
sowie {signale} jeweils + "{bl_name}".

Für jeden Treffer: Auftraggeber, Gewerk, geschätzte Auftragssumme und Angebotsfrist. Die Frist gehört in die "beschreibung" (z.B. "Angebotsfrist 05.06.2026"); ins Feld "datum" kommt das Veröffentlichungsdatum, NICHT die Frist.
Diese Treffer sind besonders wertvoll → phase="Ausschreibung" oder "Vergabe",
relevanz typischerweise 8-10. Antworte als JSON-Array."""
    return _websearch_aufruf(prompt, modell, max_searches=WEB_SEARCH_MAX_USES + 3)


def suche_amtlich(bundesland: str, suchbegriffe: list,
                  cutoff: str, heute: str, modell: str) -> list:
    """Suchgleis 3b: amtliche Quellen – UVP-Verfahren, Landes-Amtsblätter,
    Kundmachungen, Flächenwidmungs-/Bebauungsplan-Auflagen, Bauverhandlungen.
    Behörden veröffentlichen Vorhaben oft Wochen bis Monate VOR den Medien."""
    bl_name = BL_NAMEN.get(bundesland, bundesland)
    prompt = f"""Suche AMTLICHE VERÖFFENTLICHUNGEN zu Bau-, Energie- und Infrastrukturvorhaben in {bl_name} (Österreich).

ZEITRAUM: Veröffentlichungen/Kundmachungen vom {cutoff} bis {heute}.

DURCHSUCHE GEZIELT BEHÖRDLICHE QUELLEN (mehrere Suchen, nutze site:-Operatoren):
- UVP-Verfahren der Landesregierung: "UVP Verfahren {bl_name}", "UVP Kundmachung {bl_name}", "UVP Genehmigung {bl_name}" (neue/laufende Verfahren = Großprojekte wie Gewerbeparks, Kraftwerke, Windparks, Straßen, Seilbahnen, Einkaufszentren)
- Landes-Amtsblatt / Verordnungs- und Kundmachungsseiten von {bl_name} sowie der Bezirkshauptmannschaften
- site:ris.bka.gv.at Verordnungen zu Flächenwidmung/Bebauungsplan in {bl_name}
- "Flächenwidmungsplan Änderung öffentliche Auflage {bl_name}" / "Bebauungsplan Auflage {bl_name}" (Auflagen sind Frühindikatoren für konkrete Bauabsichten)
- "Bauverhandlung Kundmachung {bl_name}" / "Amtstafel Bauverhandlung {bl_name}"
- "Baubewilligung erteilt {bl_name}" / "Betriebsanlagengenehmigung {bl_name}"
- Wasserrechts-/Naturschutzverfahren mit Bauprojektbezug in {bl_name}

Für jeden Treffer: WAS wird errichtet/geändert, WO genau, WER ist Projektwerber.
Setze phase="Planung" oder "Ausschreibung"; relevanz nach Konkretheit (UVP-Einreichung, erteilte Bewilligung oder öffentliche Auflage = 6-9).
Antworte als JSON-Array."""
    return _websearch_aufruf(prompt, modell, max_searches=WEB_SEARCH_MAX_USES + 2)


def suche_kommunal(bundesland: str, bezirke_orte: dict, suchbegriffe: list,
                   cutoff: str, heute: str, modell: str) -> list:
    """Suchgleis 4: kommunale Bauprojekte & Gemeinderats-/Stadtratsbeschlüsse."""
    bl_name = BL_NAMEN.get(bundesland, bundesland)
    if bezirke_orte:
        bezirke_txt = "; ".join(
            f"{bez} (z.B. {', '.join(orte[:4])})"
            for bez, orte in list(bezirke_orte.items())[:12]
        )
    else:
        bezirke_txt = f"alle Bezirke von {bl_name}"

    prompt = f"""Suche KOMMUNALE BAUPROJEKTE und GEMEINDERATS-/STADTRATSBESCHLÜSSE in {bl_name} (Österreich).

ZEITRAUM: Beschlüsse/Meldungen vom {cutoff} bis {heute}.

BEZIRKE & BEISPIELORTE (decke möglichst viele ab):
{bezirke_txt}

FÜHRE VIELE VERSCHIEDENE WEB-SUCHEN DURCH, z.B.:
- "Gemeinderat beschließt [Ort] Bau"
- "[Ort] Spatenstich Gemeinde {heute[-4:]}"
- "[Bezirk] Gemeinde Bauprojekt {heute[-4:]}"
- "Stadtrat [Stadt] Neubau Beschluss"
- "[Ort] Kindergarten Neubau" / "[Ort] Schule Erweiterung" / "[Ort] Feuerwehrhaus"
- "[Ort] Bauhof Neubau" / "[Ort] Amtsgebäude" / "[Ort] Gemeindezentrum"
- "[Ort] Kanal Wasserleitung Sanierung" / "[Ort] Ortsstraße Sanierung"

QUELLEN: meinbezirk.at (nach Bezirk gegliedert!), lokale Bezirksblätter,
Gemeinde-Websites (.gv.at), tips.at, Landespresse {bl_name}.

Kommunale Projekte sind oft kleiner – nimm sie TROTZDEM auf (relevanz 4-7).
Ziel: möglichst viele konkrete kommunale Vorhaben. Antworte als JSON-Array."""
    return _websearch_aufruf(prompt, modell, max_searches=WEB_SEARCH_MAX_USES + 3)


def suche_wien(suchbegriffe: list, cutoff: str, heute: str, modell: str) -> list:
    """
    Wien-Spezialgleis. Wien ist zugleich Stadt UND Bundesland und hat eigene
    amtliche Quellen, die fuer Flaechengemeinden nicht existieren. Dieses Gleis
    durchsucht gezielt diese Wien-Quellen und alle 23 Gemeindebezirke.
    """
    try:
        bezirke = [g.get("name", "") for g in get_gemeinden_fuer_bundeslaender(["W"]) if g.get("name")]
    except Exception:
        bezirke = []
    bezirke_txt = ", ".join(bezirke) if bezirke else "alle 23 Wiener Gemeindebezirke"
    signale = ", ".join(suchbegriffe[:8])

    prompt = f"""Suche AKTUELLE BAU-, STADTENTWICKLUNGS- UND INFRASTRUKTURPROJEKTE in WIEN (Oesterreich).

ZEITRAUM: Beschluesse/Meldungen/Auflagen vom {cutoff} bis {heute}.

WICHTIG – Wien ist Stadt UND Bundesland. Nutze gezielt diese amtlichen Wien-Quellen:
- INFODAT, die Informationsdatenbank des Wiener Landtags & Gemeinderats (wien.gv.at/infodat) – Beschluesse, Bauordnung, Flaechenwidmungen
- Flaechenwidmungs- & Bebauungsplan / Plandokumente der Stadt Wien (wien.gv.at/flaechenwidmung) – laufende oeffentliche Auflagen sind Fruehindikatoren fuer Bauvorhaben
- Vorhabenliste der Wiener Stadtentwicklung (wien.gv.at/stadtentwicklung)
- Amtsblatt der Stadt Wien (Gemeinderat, Gemeinderatsausschuesse, Vergaben)
- Wiener Wohnen, gemeinnuetzige Bautraeger, OEBB/Wiener Linien (U-Bahn-/Gleisbau)
- meinbezirk.at (nach Wiener Bezirken gegliedert), wien.orf.at, Bezirkszeitungen

DURCHSUCHE DIE 23 BEZIRKE (decke moeglichst viele ab):
{bezirke_txt}

FUEHRE VIELE VERSCHIEDENE WEB-SUCHEN DURCH, z.B.:
- "Wien [Bezirk] Neubau Projekt {heute[-4:]}" / "Wien [Bezirk] Bauprojekt"
- "Wien Flaechenwidmung Plandokument [Bezirk]" / "oeffentliche Auflage Bebauungsplan Wien"
- "Wien Stadtentwicklung Vorhaben [Bezirk]" / "Stadterweiterung Wien"
- "Wiener Wohnen Neubau" / "Wohnbau Wien Spatenstich"
- "Wien Schule Neubau" / "Wien Kindergarten" / "Wien Amtsgebaeude Sanierung"
- "Wiener Linien U-Bahn Ausbau" / "Wien Gleisbau" / "Wien Infrastruktur"
- einzelne Gewerke aus dieser Liste + "Wien": {signale}

Sammle ALLE konkreten Treffer (auch einzelne Bezirksprojekte). Setze bundesland="W".
Antworte als JSON-Array."""
    return _websearch_aufruf(prompt, modell, max_searches=WEB_SEARCH_MAX_USES + 3)


# -----------------------------------------------------------------------------
# Validierung & Normalisierung eines einzelnen Treffers
# -----------------------------------------------------------------------------

def validiere_und_normalisiere_projekt(projekt: dict, bundesland_erwartet: str,
                                        min_relevanz: int = 4,
                                        cutoff_dt=None, heute_dt=None) -> dict | None:
    """
    Prüft Plausibilität und normalisiert ein gefundenes Projekt.
    - Titel + Beschreibung vorhanden und lang genug?
    - Relevanz >= min_relevanz?
    - Datum innerhalb des erlaubten Zeitfensters (wenn cutoff_dt/heute_dt übergeben)?
    - Bundesland plausibel (robuste Normalisierung; Fallback auf bundesland_erwartet)?
    Gibt None zurück, wenn der Treffer nicht aufgenommen werden soll.
    """
    if not isinstance(projekt, dict):
        return None

    titel        = str(projekt.get("titel") or "").strip()
    beschreibung = str(projekt.get("beschreibung") or "").strip()
    if len(titel) < 5 or len(beschreibung) < 15:
        return None

    try:
        relevanz = int(projekt.get("relevanz", 5))
    except (ValueError, TypeError):
        relevanz = 5
    if relevanz < min_relevanz:
        return None

    # ── DATUMSPRÜFUNG ──────────────────────────────────────────────────────
    # Nur wenn cutoff_dt und heute_dt übergeben wurden UND das Projekt ein
    # konkretes Datum hat. Fehlendes Datum = kein Verwerfen (kein falsches Negativ).
    datum_roh = str(projekt.get("datum") or "").strip()
    if datum_roh and cutoff_dt is not None and heute_dt is not None:
        try:
            # Unterstützt YYYY-MM-DD und DD.MM.YYYY
            if "-" in datum_roh and len(datum_roh) >= 8:
                projekt_dt = datetime.strptime(datum_roh[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            elif "." in datum_roh and len(datum_roh) >= 8:
                projekt_dt = datetime.strptime(datum_roh[:10], "%d.%m.%Y").replace(tzinfo=timezone.utc)
            else:
                projekt_dt = None
            if projekt_dt is not None:
                # Kleiner Puffer von 7 Tagen für Berichtsverzögerungen
                puffer = timedelta(days=7)
                if projekt_dt < (cutoff_dt - puffer) or projekt_dt > (heute_dt + puffer):
                    return None  # Datum eindeutig außerhalb des Zeitfensters
        except (ValueError, TypeError):
            pass  # Nicht parsebar → durchlassen

    # ── JAHRES-PLAUSIBILITÄT (zweite Sicherung gegen alte Projekte) ─────────
    # Greift auch dann, wenn KEIN sauberes Datumsfeld geliefert wurde: Stehen in
    # Titel/Beschreibung/URL nur veraltete Jahreszahlen (z.B. 2023) und kein
    # aktuelles/zukünftiges Jahr, wird der Treffer verworfen.
    if cutoff_dt is not None and heute_dt is not None:
        kombi_text = f"{titel} {beschreibung} {projekt.get('artikel_url','')}"
        if _jahr_verdaechtig(kombi_text, cutoff_dt.year, heute_dt.year):
            return None

    # ── ORT/BUNDESLAND-FILTER (faktenbasiert statt vertrauensbasiert) ───────
    # Statt blind dem zu glauben, was im "bundesland"-Feld steht, wird der echte
    # Ortsname gegen die Gemeinde-Datenbank geprüft. Das verhindert z.B., dass
    # ein Wiener Projekt in einer Oberösterreich-Suche landet.
    ort_roh        = str(projekt.get("ort") or "").strip()
    bl_angegeben   = _normalisiere_bundesland(str(projekt.get("bundesland") or ""))
    bl_aus_ort     = _bundesland_aus_ort(ort_roh)  # eindeutiges BL laut DB, sonst ''

    if bundesland_erwartet:
        # 1) Ort gehört eindeutig zu einem ANDEREN Bundesland → verwerfen.
        if bl_aus_ort and bl_aus_ort != bundesland_erwartet:
            return None
        # 2) Ort unbekannt, aber Claude nennt ausdrücklich ein anderes BL → verwerfen.
        if not bl_aus_ort and bl_angegeben and bl_angegeben != bundesland_erwartet:
            return None
        bl = bundesland_erwartet
    else:
        # Ganz-Österreich-Suche: Bundesland möglichst exakt bestimmen.
        bl = bl_aus_ort or bl_angegeben or ""

    url = str(projekt.get("artikel_url") or "").strip()
    if url and not url.startswith("http"):
        url = ""

    return {
        "titel":       titel[:200],
        "beschreibung": beschreibung,
        "ort":         _kanonischer_ort(ort_roh, bl or bundesland_erwartet),
        "bezirk":      str(projekt.get("bezirk") or "").strip(),
        "bundesland":  bl or bundesland_erwartet,
        "kategorie":   str(projekt.get("kategorie") or "Sonstiges").strip(),
        "volumen":     str(projekt.get("volumen") or "").strip(),
        "phase":       str(projekt.get("phase") or "Planung").strip(),
        "relevanz":    relevanz,
        "artikel_url": url,
        "quelle_name": str(projekt.get("quelle_name") or "web_search").strip(),
    }


def _verarbeite_treffer(raw_liste: list, bundesland: str, min_relevanz: int,
                        gesehen: set, neue_projekte: list, auftrag: dict,
                        cutoff_dt=None, heute_dt=None) -> int:
    """
    Validiert, dedupliziert (innerhalb des Laufs) und speichert eine Liste roher
    Treffer in Supabase. Gibt die Anzahl NEU gespeicherter Projekte zurück.
    """
    gespeichert = 0
    for raw in raw_liste:
        projekt = validiere_und_normalisiere_projekt(
            raw, bundesland, min_relevanz,
            cutoff_dt=cutoff_dt, heute_dt=heute_dt,
        )
        if not projekt:
            continue
        # Dedup-Schlüssel: URL, sonst Titel+Ort (kleingeschrieben)
        key = projekt["artikel_url"].lower() if projekt["artikel_url"] \
              else f"{projekt['titel'].lower()}|{projekt['ort'].lower()}"
        if key in gesehen:
            continue
        gesehen.add(key)
        print(f"    ✅ ({projekt['relevanz']}/10) {projekt['titel'][:55]} · {projekt['ort']}")
        artikel = {
            "url":         projekt["artikel_url"] or f"search://{berechne_hash(projekt['titel'] + projekt['ort'])}",
            "titel":       projekt["titel"],
            "quelle_name": projekt["quelle_name"],
        }
        if speichere_projekt(projekt, artikel, auftrag):
            neue_projekte.append(projekt)
            gespeichert += 1
    return gespeichert

# SCHRITT 6: PROJEKTE IN SUPABASE SPEICHERN
# WICHTIG: Duplikat-Check LAUFÜBERGREIFEND – kein suchanfrage_id Filter!
# =============================================================================

def speichere_projekt(analyse: dict, artikel: dict, auftrag: dict) -> bool:
    """
    Speichert ein gefundenes Projekt in Supabase – mit Mehrquellen-Logik:
      1) Exakt gleiche URL  -> nur Zeitstempel (kein Duplikat).
      2) Gleiches Bauvorhaben im selben Ort (andere URL) -> neue Quelle anhaengen
         (kein Duplikat). Kommt sie in einem spaeteren Lauf dazu -> Glocke.
      3) Sonst -> neues Projekt mit erster Quelle anlegen.
    Duplikat-/Quellen-Logik laeuft kundenweit ueber ALLE bisherigen Laeufe.
    """
    # Der Dedup-Anker (artikel["url"]) kann ein interner "search://"-Platzhalter
    # sein – der ist für die laufübergreifende Duplikaterkennung nötig, darf aber
    # NIEMALS als sichtbarer Link beim Kunden landen. Daher hier strikt trennen:
    #   url_hash      -> Dubletten-Erkennung (auch mit Platzhalter eindeutig)
    #   sichtbare_url -> nur eine echte http-URL, sonst leer (kein toter Link)
    url_hash = berechne_hash(artikel["url"])
    sichtbare_url = artikel["url"] if str(artikel["url"]).startswith("http") else ""
    jetzt    = datetime.now(timezone.utc).isoformat()

    # ── Stufe 1: exakt gleiche URL bereits vorhanden ─────────────────────────
    vorhandene = sb_get("projekte", {
        "rohdaten_hash": f"eq.{url_hash}",
        "kunden_id":     f"eq.{auftrag['kunden_id']}",
    })
    if vorhandene:
        sb_patch("projekte",
                 {"rohdaten_hash": f"eq.{url_hash}", "kunden_id": f"eq.{auftrag['kunden_id']}"},
                 {"zuletzt_gecrawlt": jetzt})
        return False

    # ── Stufe 2: gleiches Projekt im selben Ort (andere URL) -> Quelle anhaengen
    ort = (analyse.get("ort") or "").strip()
    if ort:
        kandidaten = sb_get("projekte", {
            "kunden_id": f"eq.{auftrag['kunden_id']}",
            "ort":       f"eq.{ort}",
        })
        treffer = _finde_gleiches_projekt(analyse, kandidaten)
        if treffer:
            _quelle_anhaengen(treffer, artikel, auftrag, jetzt)
            return False

    # ── Stufe 3: neues Projekt anlegen ───────────────────────────────────────
    lat, lng = geocode_ort(analyse.get("ort", ""), analyse.get("bundesland", ""))
    quellen_initial = [{"url": sichtbare_url, "quelle_name": artikel["quelle_name"], "gefunden_am": jetzt}] if sichtbare_url else []
    projekt = {
        "kunden_id":          auftrag["kunden_id"],
        "suchanfrage_id":     auftrag["id"],
        "titel":              analyse.get("titel") or artikel["titel"][:200],
        "ort":                analyse.get("ort", ""),
        "bezirk":             analyse.get("bezirk", ""),
        "bundesland":         analyse.get("bundesland", ""),
        "lat":                lat,
        "lng":                lng,
        "kategorie":          analyse.get("kategorie", "Sonstiges"),
        "volumen":            analyse.get("volumen", ""),
        "phase":              analyse.get("phase", ""),
        "quelle":             artikel["quelle_name"],
        "artikel_url":        sichtbare_url,
        "beschreibung":       analyse.get("beschreibung", ""),
        "relevanz":           analyse.get("relevanz", 5),
        "ignorieren":         False,
        "gemerkt":            False,
        "ist_oeffentlich":    False,
        "erstmals_gefunden":  jetzt,
        "zuletzt_geaendert":  jetzt,
        "zuletzt_gecrawlt":   jetzt,
        "rohdaten_hash":      url_hash,
        "cache_gueltig_bis":  (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "quellen":            quellen_initial,
        "neue_quellen_anzahl": 0,
    }

    result = sb_insert("projekte", projekt)
    return result is not None

# =============================================================================
# SCHRITT 7: E-MAIL VERSENDEN
# WICHTIG: Fixer Dashboard-Link – nur kunden_id, keine suchanfrage_id!
# =============================================================================

def _brevo_sende(empfaenger_email: str, empfaenger_name: str,
                 betreff: str, html: str, text: str) -> bool:
    """
    Zentraler E-Mail-Versand ueber die Brevo-API (identisch zur Bestaetigungs-/
    Rechnungsmail des Webhooks). Gibt True bei Erfolg zurueck. Jeder Fehler wird
    abgefangen und als False zurueckgegeben, damit der Lauf nie daran abbricht.
    """
    if not BREVO_API_KEY:
        print("  ⚠️  BREVO_API_KEY nicht gesetzt – E-Mail übersprungen")
        return False
    payload = {
        "sender":      {"name": ABSENDER_NAME, "email": ABSENDER_EMAIL},
        "to":          [{"email": empfaenger_email, "name": empfaenger_name or empfaenger_email}],
        "subject":     betreff,
        "htmlContent": html,
        "textContent": text,
    }
    try:
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key":      BREVO_API_KEY,
                "Content-Type": "application/json",
                "accept":       "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.ok:
            return True
        print(f"  ❌ Brevo-Fehler {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"  ❌ Brevo-Exception: {str(e)[:150]}")
        return False


def erstelle_email_html(kunde: dict, auftrag: dict, projekte_liste: list[dict]) -> str:
    anzahl = len(projekte_liste)

    # FIXER Link – nur kunden_id, kein suchanfrage_id
    dashboard_url = f"{DASHBOARD_BASE_URL}?kunden_id={auftrag['kunden_id']}"

    top_projekte = sorted(projekte_liste, key=lambda p: p.get("relevanz", 0), reverse=True)[:5]

    # Phasen-Farben (hell)
    def phase_style(phase: str):
        p = (phase or "").lower()
        if "ausschreibung" in p or "vergabe" in p:
            return "background:#fef3c7;color:#92400e;"   # amber
        if "bau" in p or "fertigstellung" in p:
            return "background:#dcfce7;color:#166534;"   # grün
        return "background:#dbeafe;color:#1e40af;"       # blau (Planung)

    projekt_html = ""
    for p in top_projekte:
        relevanz     = int(p.get("relevanz", 5))
        sterne       = "★" * min(relevanz // 2, 5)
        ph_style     = phase_style(p.get("phase", ""))
        artikel_link = (
            f'<a href="{p.get("artikel_url","#")}" '
            f'style="color:#2563eb;font-size:12px;font-weight:600;">→ Zum Artikel</a>'
            if p.get("artikel_url") else
            '<span style="font-size:12px;color:#94a3b8;">Kein Link verfügbar</span>'
        )
        bezirk_txt = f" · {p.get('bezirk')}" if p.get("bezirk") else ""
        volumen_txt = f" · {p.get('volumen')}" if p.get("volumen") else ""
        projekt_html += f"""
        <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:8px;padding:16px;margin-bottom:12px;border-left:4px solid #2563eb;">
          <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
            <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#64748b;">{p.get('kategorie','Sonstiges')}</span>
            <span style="font-size:10px;color:#cbd5e1;">·</span>
            <span style="{ph_style}padding:2px 8px;border-radius:20px;font-size:10px;font-weight:700;">{p.get('phase','Planung')}</span>
            <span style="font-size:10px;color:#f59e0b;margin-left:2px;">{sterne}</span>
          </div>
          <div style="font-size:15px;font-weight:700;color:#0f172a;margin-bottom:6px;line-height:1.4;">{p.get('titel','')}</div>
          <div style="font-size:12px;color:#64748b;margin-bottom:8px;">
            📍 {p.get('ort','')}{bezirk_txt} · {p.get('bundesland','')}{volumen_txt}
          </div>
          <div style="font-size:13px;color:#334155;line-height:1.6;margin-bottom:10px;">{p.get('beschreibung','')}</div>
          {artikel_link}
        </div>"""

    # Dashboard-Button (wiederverwendet an zwei Stellen)
    btn = (f'<a href="{dashboard_url}" '
           f'style="background:#2563eb;color:#ffffff;font-weight:700;font-size:14px;'
           f'padding:13px 28px;border-radius:8px;text-decoration:none;display:inline-block;">'
           f'📊 Alle {anzahl} Projekte im Dashboard ansehen</a>')

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="color-scheme" content="light">
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f1f5f9">
<tr><td align="center" style="padding:32px 16px;">
<table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;">

  <!-- HEADER -->
  <tr><td style="background:#2563eb;border-radius:12px 12px 0 0;padding:24px 32px;text-align:center;">
    <table cellpadding="0" cellspacing="0" border="0" style="margin:0 auto;">
      <tr>
        <td style="vertical-align:middle;padding-right:12px;">
          <svg width="40" height="40" viewBox="0 0 40 40" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle cx="20" cy="20" r="17" stroke="#ffffff" stroke-width="1.5" fill="none" opacity="0.35"/>
            <circle cx="20" cy="20" r="17" stroke="#bfdbfe" stroke-width="1" fill="none"/>
            <polygon points="20,5 23,20 20,17 17,20" fill="#ffffff"/>
            <polygon points="20,35 23,20 20,23 17,20" fill="#bfdbfe" opacity="0.55"/>
            <polygon points="5,20 20,17 17,20 20,23" fill="#bfdbfe" opacity="0.55"/>
            <polygon points="35,20 20,17 23,20 20,23" fill="#bfdbfe" opacity="0.55"/>
            <circle cx="20" cy="20" r="2.5" fill="#ffffff"/>
            <circle cx="20" cy="20" r="5" stroke="#ffffff" stroke-width="1" fill="none" opacity="0.3"/>
          </svg>
        </td>
        <td style="vertical-align:middle;text-align:left;">
          <div style="font-size:22px;font-weight:700;color:#ffffff;letter-spacing:-0.3px;line-height:1.1;">Project<span style="color:#bfdbfe;">Scout</span></div>
          <div style="font-size:11px;color:#bfdbfe;margin-top:3px;letter-spacing:0.2px;">Intelligentes Scouting · Österreich</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- KENNZAHLEN-BOX -->
  <tr><td style="background:#ffffff;padding:28px 32px 20px;border-bottom:1px solid #e2e8f0;">
    <table width="100%" cellpadding="0" cellspacing="0" border="0">
      <tr>
        <td style="text-align:center;background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:16px;">
          <div style="font-size:36px;font-weight:900;color:#2563eb;">{anzahl}</div>
          <div style="font-size:12px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:.5px;">Neue Projekte</div>
          <div style="font-size:12px;color:#94a3b8;margin-top:2px;">für {kunde.get('firmenname','Ihr Unternehmen')}</div>
        </td>
      </tr>
    </table>
    <p style="font-size:14px;color:#475569;margin:16px 0 0;line-height:1.6;">
      Ihr Scout-Lauf ist abgeschlossen. Hier sind die <strong>{min(anzahl,5)} relevantesten</strong> neuen Projekte – alle weiteren finden Sie im Dashboard.
    </p>
  </td></tr>

  <!-- BUTTON OBEN -->
  <tr><td style="background:#ffffff;padding:20px 32px;text-align:center;border-bottom:1px solid #e2e8f0;">
    {btn}
  </td></tr>

  <!-- PROJEKTE -->
  <tr><td style="background:#f8fafc;padding:24px 32px;">
    <div style="font-size:11px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-bottom:16px;">TOP-PROJEKTE DIESES LAUFS</div>
    {projekt_html}
  </td></tr>

  <!-- BUTTON UNTEN -->
  <tr><td style="background:#ffffff;padding:24px 32px;text-align:center;border-top:1px solid #e2e8f0;">
    {btn}
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:0 0 12px 12px;padding:20px 32px;">
    <p style="font-size:12px;color:#64748b;margin:0 0 8px;"><strong style="color:#374151;">🔖 Ihr persönlicher Dashboard-Link:</strong></p>
    <p style="margin:0 0 12px;"><a href="{dashboard_url}" style="color:#2563eb;font-size:12px;word-break:break-all;">{dashboard_url}</a></p>
    <p style="font-size:11px;color:#94a3b8;margin:0;line-height:1.6;">
      Speichern Sie diesen Link als Favorit – er bleibt bei allen Scout-Läufen gleich und wächst mit jeder Suche. ·
      Excel-Export im Dashboard verfügbar. ·
      Fragen? Einfach auf diese E-Mail antworten. ·
      <a href="https://project-scout.at/" style="color:#2563eb;">project-scout.at</a>
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""

def sende_email(kunde: dict, auftrag: dict, projekte_liste: list[dict]) -> bool:
    empfaenger = kunde.get("email")
    if not empfaenger:
        print("  ⚠️  Keine Kunden-E-Mail-Adresse vorhanden")
        return False
    anzahl  = len(projekte_liste)
    firma   = kunde.get("firmenname") or ""
    betreff = f"ProjectScout: {anzahl} neue Projekte – {firma}"

    dashboard_url = f"{DASHBOARD_BASE_URL}?kunden_id={auftrag['kunden_id']}"
    text_body = f"""ProjectScout – Ihre Ergebnisse sind da!

{anzahl} neue Projekte wurden gefunden.

Ihr persönliches Dashboard (als Favorit speichern!):
{dashboard_url}

ProjectScout – KI-gestützter Projekt-Scout für Österreich"""

    html_body = erstelle_email_html(kunde, auftrag, projekte_liste)

    ok = _brevo_sende(empfaenger, firma, betreff, html_body, text_body)
    if ok:
        print(f"  ✉️  E-Mail gesendet an {empfaenger}")
    return ok


# =============================================================================
# ADMIN-BENACHRICHTIGUNG
# =============================================================================

def sende_admin_benachrichtigung(
    kunde: dict, auftrag: dict,
    anzahl_neue: int, anzahl_gesamt: int,
    dauer_sek: float, gesamt_artikel: int
) -> bool:
    """Sendet Benachrichtigung an office@project-scout.at nach jedem Scout-Lauf."""
    if not BREVO_API_KEY:
        print("  ⚠️  Admin-Mail: BREVO_API_KEY nicht gesetzt")
        return False

    jetzt     = datetime.now().strftime("%d.%m.%Y um %H:%M:%S Uhr")
    dauer_str = f"{int(dauer_sek//60)} Min {int(dauer_sek%60)} Sek"
    vorname   = kunde.get("vorname", "–")
    nachname  = kunde.get("nachname", "–")
    firma     = kunde.get("firmenname") or "–"
    email     = kunde.get("email", "–")

    bundeslaender = auftrag.get("bundeslaender") or []
    if auftrag.get("ganz_oesterreich"):
        gebiet_str = "Ganz Österreich (alle 9 Bundesländer)"
    else:
        gebiet_str = ", ".join(bundeslaender) if isinstance(bundeslaender, list) else str(bundeslaender)

    gewerke = auftrag.get("gewerke") or []
    gewerke_str = ", ".join(gewerke) if isinstance(gewerke, list) and gewerke else "–"

    zeitraum_tage = auftrag.get("zeitraum_tage", "–")
    zeitraum_von  = auftrag.get("zeitraum_von", "–")
    zeitraum_bis  = auftrag.get("zeitraum_bis", "–")
    kosten        = auftrag.get("kosten_geschaetzt", 0)
    dashboard_url = f"{DASHBOARD_BASE_URL}?kunden_id={auftrag['kunden_id']}"

    betreff = f"🔔 ProjectScout – Scout-Lauf abgeschlossen | {vorname} {nachname}"

    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="background:#f8f7f4;font-family:Arial,sans-serif;padding:24px;max-width:600px;margin:0 auto;">
  <div style="background:white;border:1px solid #e5e4e0;border-radius:12px;padding:24px;margin-bottom:12px;">
    <div style="font-size:11px;font-weight:600;color:#2563eb;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;">🔔 Scout-Lauf abgeschlossen</div>
    <div style="display:flex;gap:10px;margin-bottom:16px;">
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:10px;flex:1;text-align:center;">
        <div style="font-size:24px;font-weight:700;color:#16a34a;">{anzahl_neue}</div>
        <div style="font-size:10px;color:#888;text-transform:uppercase;">Neue Projekte</div>
      </div>
      <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;padding:10px;flex:1;text-align:center;">
        <div style="font-size:24px;font-weight:700;color:#2563eb;">{anzahl_gesamt}</div>
        <div style="font-size:10px;color:#888;text-transform:uppercase;">Gesamt Dashboard</div>
      </div>
      <div style="background:#f8f7f4;border:1px solid #e5e4e0;border-radius:8px;padding:10px;flex:1;text-align:center;">
        <div style="font-size:16px;font-weight:700;color:#444;">{dauer_str}</div>
        <div style="font-size:10px;color:#888;text-transform:uppercase;">Laufzeit</div>
      </div>
    </div>
    <table style="width:100%;font-size:13px;border-collapse:collapse;">
      <tr style="border-bottom:1px solid #f1f0ec;"><td style="padding:7px 0;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;width:130px;">Zeitpunkt</td><td style="padding:7px 0;color:#111;">{jetzt}</td></tr>
      <tr><td style="padding:7px 0;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;">Artikel</td><td style="padding:7px 0;color:#111;">{gesamt_artikel} analysiert</td></tr>
    </table>
  </div>
  <div style="background:white;border:1px solid #e5e4e0;border-radius:12px;padding:24px;margin-bottom:12px;">
    <div style="font-size:11px;font-weight:600;color:#444;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px;">👤 Kunde</div>
    <table style="width:100%;font-size:13px;border-collapse:collapse;">
      <tr style="border-bottom:1px solid #f1f0ec;"><td style="padding:7px 0;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;width:130px;">Name</td><td style="padding:7px 0;color:#111;font-weight:500;">{vorname} {nachname}</td></tr>
      <tr style="border-bottom:1px solid #f1f0ec;"><td style="padding:7px 0;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;">Firma</td><td style="padding:7px 0;color:#111;">{firma}</td></tr>
      <tr><td style="padding:7px 0;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;">E-Mail</td><td style="padding:7px 0;"><a href="mailto:{email}" style="color:#2563eb;">{email}</a></td></tr>
    </table>
  </div>
  <div style="background:white;border:1px solid #e5e4e0;border-radius:12px;padding:24px;margin-bottom:16px;">
    <div style="font-size:11px;font-weight:600;color:#444;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px;">🔍 Suchanfrage</div>
    <table style="width:100%;font-size:13px;border-collapse:collapse;">
      <tr style="border-bottom:1px solid #f1f0ec;"><td style="padding:7px 0;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;width:130px;">Gebiet</td><td style="padding:7px 0;color:#111;">{gebiet_str}</td></tr>
      <tr style="border-bottom:1px solid #f1f0ec;"><td style="padding:7px 0;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;">Zeitraum</td><td style="padding:7px 0;color:#111;">{zeitraum_tage} Tage &nbsp;·&nbsp; {zeitraum_von} – {zeitraum_bis}</td></tr>
      <tr style="border-bottom:1px solid #f1f0ec;"><td style="padding:7px 0;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;">Preis</td><td style="padding:7px 0;color:#111;font-weight:600;">€ {kosten:.2f}</td></tr>
      <tr><td style="padding:7px 0;color:#888;font-size:11px;font-weight:600;text-transform:uppercase;vertical-align:top;">Gewerke</td><td style="padding:7px 0;color:#111;font-size:12px;">{gewerke_str}</td></tr>
    </table>
  </div>
  <div style="text-align:center;">
    <a href="{dashboard_url}" style="background:#2563eb;color:white;font-weight:600;font-size:14px;padding:12px 28px;border-radius:8px;text-decoration:none;">→ Kunden-Dashboard öffnen</a>
  </div>
</body></html>"""

    text_body = f"Scout-Lauf abgeschlossen\n{jetzt}\nKunde: {vorname} {nachname} ({email})\nFirma: {firma}\nGebiet: {gebiet_str}\nZeitraum: {zeitraum_tage} Tage\nNeue Projekte: {anzahl_neue}\nGesamt: {anzahl_gesamt}\nDauer: {dauer_str}\nDashboard: {dashboard_url}"

    ok = _brevo_sende(ADMIN_EMAIL, "ProjectScout Admin", betreff, html_body, text_body)
    if ok:
        print(f"  📧 Admin-Benachrichtigung gesendet an {ADMIN_EMAIL}")
    return ok


# =============================================================================
# CRAWLING-GLEIS: Gemeinde-Websites direkt herunterladen + mit Haiku analysieren
# =============================================================================

def _waehle_crawl_gemeinden(bundeslaender: list, max_anzahl: int) -> list:
    """
    Wählt die als Nächstes zu crawlenden Gemeinden aus den gewählten Bundesländern.
    Rotation über mehrere Läufe via optionaler Supabase-Tabelle 'gemeinde_crawl':
    Gemeinden, die noch nie / am längsten nicht gecrawlt wurden, kommen zuerst.
    Fehlt die Tabelle, wird ohne Rotation einfach der Reihe nach genommen.
    """
    alle = []
    for bl in bundeslaender:
        try:
            for g in get_gemeinden_fuer_bundeslaender([bl]):
                g2 = dict(g); g2["bundesland"] = bl
                alle.append(g2)
        except Exception:
            pass

    cache = {}
    try:
        rows = sb_get("gemeinde_crawl", {"select": "gemeinde,bundesland,letzter_crawl"})
        for r in rows or []:
            cache[(r.get("gemeinde"), r.get("bundesland"))] = r.get("letzter_crawl") or ""
    except Exception:
        cache = {}  # Tabelle existiert nicht -> ohne Rotation weiter

    # Sortierung: leerer Zeitstempel (= nie gecrawlt) zuerst, dann ältester zuerst
    alle.sort(key=lambda g: cache.get((g.get("name"), g.get("bundesland")), ""))
    return alle[:max_anzahl]


def _markiere_crawl_versuche(versuchte: list, erfolgreiche_nach_key: dict) -> None:
    """
    Aktualisiert die Cache-Tabelle 'gemeinde_crawl': ALLE versuchten Gemeinden
    bekommen den aktuellen Zeitstempel (für die Rotation), erfolgreiche zusätzlich
    die gefundene Protokoll-URL und den Inhalts-Hash. Fehlt die Tabelle, passiert
    nichts (try/except).
    """
    jetzt = datetime.now(timezone.utc).isoformat()
    for g in versuchte:
        key = (g.get("name"), g.get("bundesland"))
        datensatz = {
            "gemeinde":   g.get("name"),
            "bundesland": g.get("bundesland"),
            "letzter_crawl": jetzt,
        }
        treffer = erfolgreiche_nach_key.get(key)
        if treffer:
            datensatz["protokoll_url"] = treffer.get("quelle_url", "")
            datensatz["letzter_hash"]  = treffer.get("inhalt_hash", "")
        try:
            sb_upsert("gemeinde_crawl", datensatz, on_conflict="gemeinde,bundesland")
        except Exception:
            pass  # Tabelle fehlt o.ä. -> Rotation halt ohne Persistenz


def _analyse_pdf_aufruf(pdf_bytes: bytes, prompt: str, modell: str,
                        max_tokens: int = 3500) -> list:
    """
    Workaround für GESCANNTE Protokoll-PDFs ohne extrahierbaren Text: das PDF wird
    direkt (als Dokument) an Claude geschickt; Claude liest es per Vision. Nur für
    kleinere PDFs (<5 MB), um Kosten/Zeit zu begrenzen.
    """
    import base64
    if not pdf_bytes or len(pdf_bytes) > 5_000_000:
        return []
    try:
        b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
        response = ANTHROPIC_CLIENT.messages.create(
            model=modell,
            max_tokens=max_tokens,
            system=SUCHE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": [
                {"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        try:
            _TOKEN_STATS["input"]  += response.usage.input_tokens
            _TOKEN_STATS["output"] += response.usage.output_tokens
        except Exception:
            pass
        _TOKEN_STATS["calls"] += 1
        text = " ".join(b.text for b in response.content
                        if getattr(b, "type", None) == "text" and getattr(b, "text", "")).strip()
        return _parse_json_array(text)
    except Exception as ex:
        print(f"    ❌ PDF-Analyse-Fehler: {str(ex)[:120]}")
        return []


def analysiere_gecrawlten_inhalt(inhalt_obj: dict, gewerke_txt: str,
                                 cutoff: str, heute: str) -> list:
    """
    Schickt den gecrawlten Inhalt EINER Gemeinde an Haiku und lässt relevante
    Bau-/Infrastruktur-/Energie-/Immobilienvorhaben als JSON extrahieren.
    Ist nur ein gescanntes PDF vorhanden (kaum Text), wird das PDF direkt analysiert.
    """
    gemeinde = inhalt_obj.get("gemeinde", "")
    bl       = inhalt_obj.get("bundesland", "")
    bl_name  = BL_NAMEN.get(bl, bl)
    text     = inhalt_obj.get("inhalt", "") or ""

    # Echte Quell-URLs aus dem Crawl-Objekt: Die KI kennt nur den Text, nicht die
    # genaue Adresse. Damit die Treffer einen ECHTEN, möglichst konkreten Link
    # bekommen (statt eines toten "search://"-Platzhalters), reichen wir hier die
    # vom Crawler ermittelte URL durch:
    #   - bei Text-Analyse:  quelle_url  (die konkrete Bereichsseite, z.B. Amtstafel)
    #   - bei PDF-Analyse:   pdf_url     (das konkrete Protokoll-PDF) – am genauesten
    quelle_url_text = (inhalt_obj.get("quelle_url") or "").strip()
    quelle_url_pdf  = (inhalt_obj.get("pdf_url") or inhalt_obj.get("quelle_url") or "").strip()

    def _mit_quelle(treffer_liste, url):
        """Setzt die echte URL in jeden Treffer, der noch keine (http-)URL hat."""
        if url and url.startswith("http") and isinstance(treffer_liste, list):
            for t in treffer_liste:
                if isinstance(t, dict) and not str(t.get("artikel_url") or "").strip():
                    t["artikel_url"] = url
        return treffer_liste

    prompt = f"""Analysiere den folgenden Inhalt von der offiziellen Website der Gemeinde {gemeinde} ({bl_name}, Österreich). Es handelt sich um Gemeinderatsprotokolle, Sitzungsberichte oder Gemeinde-Nachrichten.

AUFGABE: Extrahiere ALLE konkreten Bau-, Infrastruktur-, Energie- und Immobilienvorhaben, die für folgende Gewerke des Kunden relevant sind:
{gewerke_txt}

ZEITRAUM: Nur Vorhaben/Beschlüsse vom {cutoff} bis {heute}. Ältere Beschlüsse ignorieren.

WICHTIG:
- bundesland = "{bl}"
- ort = "{gemeinde}" (oder der genaue Ortsteil, falls im Text genannt)
- quelle_name = "{gemeinde} (Gemeinde-Website)"
- Beschreibe je Treffer WAS gebaut/saniert wird, WANN und (falls genannt) das Volumen.

Findest du keine konkreten Bauvorhaben im Zeitraum: gib [] zurück.

INHALT DER GEMEINDE-WEBSITE:
{text}"""

    # Genug Text vorhanden -> normale (günstige) Textanalyse
    if len(text) >= 200:
        treffer = _analyse_aufruf(prompt, MODELL_HAIKU)
        if treffer:
            return _mit_quelle(treffer, quelle_url_text)

    # Kaum Text -> PDF vorhanden? ZUERST den Text aus dem PDF ziehen (pdfplumber):
    # zuverlaessig + guenstig + behebt den bisherigen 400er beim Roh-PDF-Versand.
    # Nur ein echtes Scan-PDF ohne Textebene geht als Notloesung per Vision.
    if inhalt_obj.get("pdf_bytes"):
        pdf_txt = _pdf_text(inhalt_obj["pdf_bytes"])
        if pdf_txt and len(pdf_txt) >= 200:
            prompt_pdf = (
                "Analysiere dieses Gemeinderatsprotokoll/Sitzungsdokument der Gemeinde "
                + gemeinde + " (" + bl_name + ", Oesterreich).\n"
                "AUFGABE: Extrahiere ALLE konkreten Bau-, Infrastruktur-, Energie- und "
                "Immobilienvorhaben fuer die Gewerke: " + gewerke_txt + "\n"
                "ZEITRAUM: nur " + cutoff + " bis " + heute + ". "
                "bundesland='" + bl + "', ort='" + gemeinde + "', "
                "quelle_name='" + gemeinde + " (Gemeinde-Website)'. Nichts gefunden: []\n\n"
                "INHALT:\n" + pdf_txt[:12000]
            )
            return _mit_quelle(_analyse_aufruf(prompt_pdf, MODELL_HAIKU), quelle_url_pdf)
        # echtes Scan-PDF ohne Textebene -> Vision-Notloesung (bestehender Weg):
        pdf_prompt = (f"Dies ist ein Gemeinderatsprotokoll der Gemeinde {gemeinde} ({bl_name}). "
                      f"Extrahiere relevante Bauvorhaben für die Gewerke: {gewerke_txt}. "
                      f"Zeitraum {cutoff} bis {heute}. bundesland=\"{bl}\", ort=\"{gemeinde}\", "
                      f"quelle_name=\"{gemeinde} (Gemeinde-Website)\". Nichts gefunden: []")
        return _mit_quelle(_analyse_pdf_aufruf(inhalt_obj["pdf_bytes"], pdf_prompt, MODELL_HAIKU), quelle_url_pdf)

    return []


# =============================================================================
# HAUPTFUNKTION
# =============================================================================

def _pdf_text(pdf_bytes: bytes) -> str:
    """Liest Text aus einem PDF (pdfplumber). Leerer String bei Scan-PDF/Fehler."""
    if not pdf_bytes:
        return ""
    try:
        import io
        import pdfplumber
        teile = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for seite in pdf.pages[:30]:
                t = seite.extract_text() or ""
                if t:
                    teile.append(t)
        return "\n".join(teile).strip()
    except Exception:
        return ""


# meinbezirk pp:state -> Bundesland-Kuerzel (deterministisch, ohne LLM)
_REGIO_STATE_BL = {
    "w": "W", "wien": "W",
    "noe": "NOE", "niederoesterreich": "NOE",
    "ooe": "OOE", "oberoesterreich": "OOE",
    "sbg": "SBG", "szg": "SBG", "salzburg": "SBG",
    "stmk": "STK", "stk": "STK", "steiermark": "STK",
    "ktn": "KTN", "kaernten": "KTN",
    "t": "TIR", "tir": "TIR", "tirol": "TIR",
    "vbg": "VBG", "vorarlberg": "VBG",
    "bgld": "BGR", "bgr": "BGR", "burgenland": "BGR",
}

# Bau-/Infrastruktur-/Energie-/Immobilien-Stichwoerter (Kleinschreibung, Teilstring).
# Zweck: KOSTENLOSER Vorfilter vor der KI. Nur Artikel mit mindestens einem Treffer
# werden per Haiku analysiert -> spart bei tausenden Regionalartikeln massiv Kosten,
# ohne Abdeckung zu verlieren (die Liste ist bewusst breit; Fehl-Treffer kosten nur
# einen guenstigen KI-Aufruf, der dann [] zurueckgibt).
_REGIO_BAU_KEYWORDS = (
    "bau", "sanier", "errich", "neubau", "umbau", "zubau", "anbau", "ausbau",
    "bebau", "projekt", "investit", "gemeinderat", "stadtrat", "beschluss",
    "beschloss", "widmung", "bebauungsplan", "spatenstich", "eroeffn",
    "er\u00f6ffn", "entsteh", "erweiter", "modernisier", "revitalisier",
    "areal", "quartier", "siedlung", "wohnanlage", "wohnbau", "grundst\u00fcck",
    "grundstueck", "immobil", "bautr\u00e4ger", "bautraeger", "planung",
    "vergabe", "ausschreibung", "abriss", "abbruch", "neugestaltung",
    "ansiedl", "nahversorg", "gewerbegebiet", "betriebsgebiet", "betriebsbau",
    "stra\u00dfe", "strasse", "kanal", "wasserleitung", "leitung", "br\u00fccke",
    "bruecke", "radweg", "gehsteig", "kreisverkehr", "tunnel", "bahnhof",
    "photovolta", "pv-anlage", "windkraft", "windpark", "w\u00e4rmepump",
    "waermepump", "nahw\u00e4rme", "nahwaerme", "fernw\u00e4rme", "fernwaerme",
    "heizwerk", "kraftwerk", "umspannwerk", "stromnetz", "trafostation",
    "hotel", "halle", "zentrum", "schule", "kindergarten", "feuerwehr",
    "bauhof", "amtsgeb\u00e4ude", "amtsgebaeude", "rathaus", "klinik",
    "pflegeheim", "supermarkt", "spar-markt", "billa", "kindergruppe",
)


def _hat_bau_keyword(a: dict) -> bool:
    blob = (str(a.get("titel", "")) + " " + str(a.get("beschreibung", "")) + " "
            + str(a.get("text", ""))).lower()
    return any(kw in blob for kw in _REGIO_BAU_KEYWORDS)


def _analyse_regionalmedien(artikel_liste: list, gewerke_txt: str,
                            cutoff: str, heute: str, modell: str,
                            zeit_ok=lambda: True, max_workers: int = 8) -> list:
    """
    Analysiert die geernteten meinbezirk-Artikel auf Bauvorhaben – EIN Artikel je
    KI-Aufruf, parallel. Zwei Schutzmechanismen:
      1) KOSTENLOSER Bau-Stichwort-Vorfilter: nur baurelevante Artikel kosten ein
         KI-Token (spart bei tausenden Regionalartikeln deutlich Kosten/Zeit).
      2) Der Agent BESITZT die Quelle: artikel_url / quelle_name / bundesland werden
         DETERMINISTISCH aus dem Artikel gesetzt – egal was die KI ausgibt. Keine
         vertippten/halluzinierten Links, keine Zuordnungsfehler, kein Schema-Konflikt
         mit dem System-Prompt.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    relevante = [a for a in artikel_liste if _hat_bau_keyword(a)]
    print("     [Saeule C] Stichwort-Vorfilter: " + str(len(relevante)) + " von "
          + str(len(artikel_liste)) + " Artikeln baurelevant -> KI-Analyse")

    def _eine(a: dict) -> list:
        if not zeit_ok():
            return []
        txt = (a.get("text") or "")[:4000]
        if len(txt) < 60:
            return []
        prompt = (
            "Analysiere DIESEN EINEN Artikel von meinbezirk.at (Oesterreich) auf "
            "konkrete Bau-, Infrastruktur-, Energie- und Immobilienvorhaben, die fuer "
            "folgende Gewerke relevant sind:\n" + gewerke_txt + "\n\n"
            "ZEITRAUM: nur Vorhaben/Berichte vom " + cutoff + " bis " + heute + ".\n"
            "Gib pro konkretem Vorhaben ein JSON-Objekt nach dem vorgegebenen Schema "
            "zurueck. Reine Veranstaltungen, Sport, Unfaelle, Personalien zaehlen "
            "NICHT. Findest du nichts: []\n\n"
            "TITEL: " + (a.get("titel") or "") + "\n"
            "ORT/REGION: " + (a.get("region") or "") + "\n"
            "DATUM: " + (a.get("datum") or "") + "\n\n"
            "ARTIKELTEXT:\n" + txt
        )
        roh = _analyse_aufruf(prompt, modell, max_tokens=2000)
        out = []
        for p in (roh or []):
            if not isinstance(p, dict):
                continue
            p["artikel_url"] = a.get("url", "")
            p["quelle_name"] = "meinbezirk.at"
            if not p.get("ort"):
                p["ort"] = a.get("region", "")
            st = (a.get("state") or "").strip().lower()
            p["bundesland"] = _REGIO_STATE_BL.get(st) or p.get("bundesland", "") or ""
            out.append(p)
        return out

    treffer = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_eine, a) for a in relevante]
        for fut in as_completed(futs):
            try:
                treffer.extend(fut.result())
            except Exception:
                pass
    return treffer


def verarbeite_auftrag(auftrag: dict) -> None:
    sid = auftrag["id"]
    print(f"\n{'='*60}")
    print(f"🚀 Starte Auftrag: {sid}")
    print(f"   Gewerke:      {auftrag.get('gewerke')}")
    print(f"   Bundesländer: {auftrag.get('bundeslaender')}")
    print(f"   Zeitraum:     {auftrag.get('zeitraum_tage', 30)} Tage")
    print(f"{'='*60}")

    sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {"status": "agent_laeuft"})
    start_zeit = time.time()
    # Startzeitpunkt des Laufs (ISO): damit speichere_projekt unterscheiden kann,
    # ob eine Zusatzquelle im selben Lauf (Erstfund) oder in einem spaeteren Lauf
    # (echte Neuigkeit -> Glocke) hinzukommt.
    auftrag["_lauf_start_iso"] = datetime.now(timezone.utc).isoformat()

    def zeit_aufgebraucht() -> bool:
        """True wenn das Zeitbudget für neue Suchen erschöpft ist."""
        return (time.time() - start_zeit) > ZEITBUDGET_SEKUNDEN

    try:
        kunde = lade_kundendaten(auftrag["kunden_id"])
        if not kunde:
            raise ValueError(f"Keine Kundendaten für ID {auftrag['kunden_id']} gefunden")
        print(f"  👤 Kunde: {kunde.get('firmenname')} ({kunde.get('email')})")

        suchbegriffe  = baue_suchbegriffe(auftrag)
        zeitraum_tage = int(auftrag.get("zeitraum_tage", 30) or 30)

        # Konkretes Cutoff-Datum statt vager "letzte X Tage"-Beschreibung.
        # Das verhindert, dass Claude alte Artikel (z.B. aus dem Vorjahr) liefert.
        heute_dt  = datetime.now(timezone.utc)
        cutoff_dt = heute_dt - timedelta(days=zeitraum_tage)
        heute_str  = heute_dt.strftime("%d.%m.%Y")
        cutoff_str = cutoff_dt.strftime("%d.%m.%Y")

        # Bundesländer bestimmen
        if auftrag.get("ganz_oesterreich"):
            bundeslaender = get_alle_bundeslaender_kuerzel()
        else:
            bundeslaender = auftrag.get("bundeslaender") or []
            if isinstance(bundeslaender, str):
                try: bundeslaender = json.loads(bundeslaender)
                except Exception: bundeslaender = [bundeslaender]
        bundeslaender = [b for b in bundeslaender if b]
        if not bundeslaender:
            bundeslaender = get_alle_bundeslaender_kuerzel()

        # Bundesland-Filter für Validierung: nur bei spezifischer Suche scharf
        # filtern. Bei "ganz Österreich" werden Treffer NICHT nach BL verworfen.
        einzel_bl_filter = (not auftrag.get("ganz_oesterreich")) and len(bundeslaender) <= 9

        # Gewerke laden + in fokussierte Suchgruppen aufteilen
        gewerke = auftrag.get("gewerke") or []
        if isinstance(gewerke, str):
            try: gewerke = json.loads(gewerke)
            except Exception: gewerke = [gewerke]
        gewerke_gruppen = _gruppiere_gewerke_fuer_suche(gewerke)

        # Adaptive Modellwahl: bei wenigen Bundesländern hochwertiges Sonnet,
        # bei vielen das schnellere Haiku (damit das Zeitbudget hält).
        modell = _modell_fuer_scope(len(bundeslaender))
        modell_kurz = "Sonnet" if modell == MODELL_SONNET else ("Haiku" if modell == MODELL_HAIKU else modell)

        # Suchgleise pro Bundesland zusammenstellen
        anzahl_gleise_pro_bl = len(gewerke_gruppen) + 3  # +Vergabe +Amtlich +Kommunal
        print(f"\n  🔧 Konfiguration:")
        print(f"     Modell:         {modell_kurz}")
        print(f"     Bundesländer:   {len(bundeslaender)} → {bundeslaender}")
        print(f"     Gewerkegruppen: {list(gewerke_gruppen.keys())}")
        print(f"     Suchgleise:     {anzahl_gleise_pro_bl} pro Bundesland "
              f"(~{anzahl_gleise_pro_bl * len(bundeslaender)} API-Calls)")
        print(f"     Zeitraum:       {cutoff_str} – {heute_str} ({zeitraum_tage} Tage)")

        neue_projekte: list = []
        gesehen: set = set()       # laufinterner Duplikat-Schutz
        gesamt_gefunden = 0
        abgebrochen     = False

        # Gewerke-Text fuer Direkt-Analysen (Crawl + Regionalmedien)
        gewerke_txt_crawl = ", ".join(gewerke) if gewerke else \
            "alle Bau-, Infrastruktur-, Energie- und Immobilienvorhaben"

        # ==================================================================
        # SAEULE C: REGIONALMEDIEN-ERNTE (meinbezirk) - DAS RUECKGRAT
        # Liest die Bezirks-/Gemeinde-Feeds DIREKT aus (nicht ueber die
        # Suchmaschine), damit jeder Bezirk gleich tief abgedeckt ist.
        # Laeuft mit eigenem Zeitbudget-Anteil ZUERST (ergiebigste Quelle).
        # ==================================================================
        def regio_zeit_ok() -> bool:
            return (time.time() - start_zeit) < (ZEITBUDGET_SEKUNDEN * REGIO_ZEITBUDGET_ANTEIL)

        if REGIO_AKTIV and regio_zeit_ok():
            print("\n" + "=" * 60)
            print("  [Saeule C] Regionalmedien (meinbezirk) direkt auslesen")
            print("=" * 60)
            bezirke_filter = auftrag.get("bezirke") or []
            gemeinden_filter = auftrag.get("gemeinden") or []
            if isinstance(bezirke_filter, str):
                try:
                    bezirke_filter = json.loads(bezirke_filter)
                except Exception:
                    bezirke_filter = [bezirke_filter]
            if isinstance(gemeinden_filter, str):
                try:
                    gemeinden_filter = json.loads(gemeinden_filter)
                except Exception:
                    gemeinden_filter = [gemeinden_filter]
            try:
                regio_artikel = regionalmedien.ernte_meinbezirk(
                    bundeslaender=bundeslaender,
                    cutoff_dt=cutoff_dt, heute_dt=heute_dt,
                    bezirke_filter=[str(b) for b in bezirke_filter] or None,
                    gemeinden_filter=[str(g) for g in gemeinden_filter] or None,
                    zeit_ok=regio_zeit_ok,
                    max_artikel=REGIO_MAX_ARTIKEL,
                    max_workers=REGIO_WORKERS,
                    log=print,
                )
                print("     -> " + str(len(regio_artikel)) + " Artikel geerntet - Analyse laeuft ...")
                roh_regio = _analyse_regionalmedien(
                    regio_artikel, gewerke_txt_crawl,
                    cutoff_str, heute_str, MODELL_HAIKU, zeit_ok=regio_zeit_ok,
                )
                gesamt_gefunden += len(roh_regio)
                neu_regio = _verarbeite_treffer(
                    roh_regio, "", 3, gesehen, neue_projekte, auftrag,
                    cutoff_dt=cutoff_dt, heute_dt=heute_dt,
                )
                print("     SAEULE C ERGEBNIS: " + str(len(roh_regio)) + " Treffer, "
                      + str(neu_regio) + " neue Projekte")
            except Exception as regio_err:
                print("     [!] Regionalmedien-Ernte Fehler: " + str(regio_err)[:160])

        # CRAWLING-GLEIS (Säule B): Gemeinde-Websites DIREKT herunterladen und mit
        # Haiku analysieren. Läuft VOR der web_search-Schleife mit eigenem
        # Zeitbudget-Anteil, damit es garantiert zum Zug kommt.
        gewerke_txt_crawl = ", ".join(gewerke) if gewerke else \
            "alle Bau-, Infrastruktur-, Energie- und Immobilienvorhaben"

        def crawl_zeit_ok() -> bool:
            return (time.time() - start_zeit) < (ZEITBUDGET_SEKUNDEN * CRAWL_ZEITBUDGET_ANTEIL)

        if CRAWLING_AKTIV and crawl_zeit_ok():
            print(f"\n{'═'*60}")
            print(f"  🕸️  CRAWLING-GLEIS: Gemeinde-Websites direkt durchsuchen")
            print(f"{'═'*60}")
            crawl_gemeinden = _waehle_crawl_gemeinden(bundeslaender, CRAWL_MAX_GEMEINDEN)
            print(f"     {len(crawl_gemeinden)} Gemeinden ausgewählt "
                  f"(max {CRAWL_MAX_GEMEINDEN}/Lauf, Rotation via Cache)")

            roh_inhalte = crawler.crawle_gemeinden_parallel(
                crawl_gemeinden, PROTOKOLL_PFADE,
                max_workers=CRAWL_WORKERS,
                zeit_ok=crawl_zeit_ok,
                fortschritt=lambda n: print(f"     … {n} Gemeinden gecrawlt"),
            )
            print(f"     → {len(roh_inhalte)} Gemeinden mit verwertbarem Inhalt")

            erfolge_key = {(o["gemeinde"], o["bundesland"]): o for o in roh_inhalte}
            _markiere_crawl_versuche(crawl_gemeinden, erfolge_key)

            # KI-Analyse der gecrawlten Inhalte PARALLEL (I/O-gebunden: jeder
            # Aufruf wartet nur auf die Haiku-Antwort). Bei 160 Gemeinden sinkt
            # die Analysezeit so von ~20-25 Min auf ~4 Min. Das SPEICHERN bleibt
            # sequenziell im Hauptthread (Mehrquellen-/Dedup-Logik braucht das).
            from concurrent.futures import ThreadPoolExecutor, as_completed
            crawl_neu = 0
            with ThreadPoolExecutor(max_workers=6) as ki_pool:
                futs = {ki_pool.submit(analysiere_gecrawlten_inhalt, obj,
                                       gewerke_txt_crawl, cutoff_str, heute_str): obj
                        for obj in roh_inhalte}
                for fut in as_completed(futs):
                    if zeit_aufgebraucht():
                        for f in futs:
                            f.cancel()  # storniert alles noch nicht Gestartete
                        print("     ⏱️  Zeitbudget erreicht – Crawling-Analyse gestoppt.")
                        break
                    obj = futs[fut]
                    try:
                        roh = fut.result()
                    except Exception:
                        roh = []
                    gesamt_gefunden += len(roh)
                    neu = _verarbeite_treffer(roh, obj["bundesland"], 3, gesehen,
                                              neue_projekte, auftrag,
                                              cutoff_dt=cutoff_dt, heute_dt=heute_dt)
                    crawl_neu += neu
                    if roh:
                        print(f"     ✅ {obj['gemeinde']}: {len(roh)} gefunden, {neu} neu")
            print(f"     CRAWLING-ERGEBNIS: {crawl_neu} neue Projekte aus Gemeinde-Websites")


        # ──────────────────────────────────────────────────────────────────
        # HAUPTSCHLEIFE: pro Bundesland alle Suchgleise abarbeiten
        # ──────────────────────────────────────────────────────────────────
        for bl in bundeslaender:
            if zeit_aufgebraucht():
                print(f"\n  ⏱️  Zeitbudget erreicht – beende Suche vor {bl}, finalisiere.")
                abgebrochen = True
                break

            bl_name = BL_NAMEN.get(bl, bl)
            bl_filter = bl if einzel_bl_filter else ""
            print(f"\n{'─'*60}")
            print(f"  📍 BUNDESLAND: {bl_name} ({bl})")
            print(f"{'─'*60}")

            # ── Gleis 1/2: Projektsuche je Gewerkegruppe ──
            for gruppe_name, gruppe_gewerke in gewerke_gruppen.items():
                if zeit_aufgebraucht():
                    abgebrochen = True; break
                print(f"\n  🔍 [{bl}] Projektsuche · {gruppe_name}")
                time.sleep(1.5)
                roh = suche_projekte(
                    bundesland=bl, kategorie_name=gruppe_name,
                    gewerke_liste=gruppe_gewerke, suchbegriffe=suchbegriffe,
                    cutoff=cutoff_str, heute=heute_str, modell=modell,
                )
                gesamt_gefunden += len(roh)
                neu = _verarbeite_treffer(roh, bl_filter, 3, gesehen, neue_projekte, auftrag, cutoff_dt=cutoff_dt, heute_dt=heute_dt)
                print(f"     → {len(roh)} gefunden, {neu} neu gespeichert")

            # ── Gleis 3: Vergabeportale ──
            if not zeit_aufgebraucht():
                print(f"\n  📋 [{bl}] Vergabe-/Ausschreibungssuche")
                time.sleep(1.5)
                roh = suche_vergaben(
                    bundesland=bl, suchbegriffe=suchbegriffe,
                    cutoff=cutoff_str, heute=heute_str, modell=modell,
                )
                gesamt_gefunden += len(roh)
                neu = _verarbeite_treffer(roh, bl_filter, 3, gesehen, neue_projekte, auftrag, cutoff_dt=cutoff_dt, heute_dt=heute_dt)
                print(f"     → {len(roh)} gefunden, {neu} neu gespeichert")

            # ── Gleis 3b: Amtliche Quellen (UVP, Amtsblätter, Auflagen) ──
            # Behörden melden Vorhaben oft Wochen vor der Presse. Wien ist
            # ausgenommen – dort deckt das Wien-Spezialgleis diese Quellen ab.
            if not zeit_aufgebraucht() and bl != "W":
                print(f"\n  📜 [{bl}] Amtliche Quellen (UVP, Amtsblätter, Auflagen)")
                time.sleep(1.5)
                roh = suche_amtlich(
                    bundesland=bl, suchbegriffe=suchbegriffe,
                    cutoff=cutoff_str, heute=heute_str, modell=modell,
                )
                gesamt_gefunden += len(roh)
                neu = _verarbeite_treffer(roh, bl_filter, 3, gesehen, neue_projekte, auftrag, cutoff_dt=cutoff_dt, heute_dt=heute_dt)
                print(f"     → {len(roh)} gefunden, {neu} neu gespeichert")

            # ── Gleis 4: Kommunal / Gemeinderats- & Stadtratsbeschlüsse ──
            if not zeit_aufgebraucht():
                if bl == "W":
                    # Wien hat eigene amtliche Quellen → Spezialgleis.
                    print(f"\n  🏛️  [W] Wien-Spezialsuche (INFODAT, Flächenwidmung, 23 Bezirke)")
                    time.sleep(1.5)
                    roh = suche_wien(
                        suchbegriffe=suchbegriffe, cutoff=cutoff_str,
                        heute=heute_str, modell=modell,
                    )
                else:
                    print(f"\n  🏘️  [{bl}] Kommunal- & Gemeinderatssuche")
                    bezirke_orte = _bezirke_mit_orten(bl)
                    if bezirke_orte:
                        print(f"     ({len(bezirke_orte)} Bezirke mit echten Ortsnamen)")
                    time.sleep(1.5)
                    # Kommunalprojekte sind oft kleiner → min_relevanz=3
                    roh = suche_kommunal(
                        bundesland=bl, bezirke_orte=bezirke_orte,
                        suchbegriffe=suchbegriffe, cutoff=cutoff_str,
                        heute=heute_str, modell=modell,
                    )
                gesamt_gefunden += len(roh)
                neu = _verarbeite_treffer(roh, bl_filter, 3, gesehen, neue_projekte, auftrag, cutoff_dt=cutoff_dt, heute_dt=heute_dt)
                print(f"     → {len(roh)} gefunden, {neu} neu gespeichert")

            if abgebrochen:
                break

        # ──────────────────────────────────────────────────────────────────
        # FINALISIERUNG (läuft IMMER, auch nach Zeitbudget-Abbruch)
        # ──────────────────────────────────────────────────────────────────
        # Kosten je nach eingesetztem Modell (Preise per 1M Tokens, Stand Mai 2026)
        # Sonnet 4.6: $3.00 input / $15.00 output
        # Haiku 4.5:  $1.00 input / $5.00 output
        if modell == MODELL_HAIKU:
            preis_input, preis_output = 1.00, 5.00
        else:  # Sonnet (oder Fix)
            preis_input, preis_output = 3.00, 15.00
        kosten_input  = (_TOKEN_STATS["input"]  / 1_000_000) * preis_input
        kosten_output = (_TOKEN_STATS["output"] / 1_000_000) * preis_output
        # Web-Suche: $10 pro 1000 tatsächlich durchgeführte Suchen.
        # Jetzt mit der ECHTEN Anzahl (server_tool_use) statt der früheren
        # Pauschale "5 Suchen je API-Call" (die ~3× zu hoch lag).
        kosten_search = (_TOKEN_STATS["searches"] / 1000) * 10.00
        kosten_gesamt = kosten_input + kosten_output + kosten_search

        print(f"\n{'='*60}")
        print(f"  📊 ZUSAMMENFASSUNG")
        print(f"{'='*60}")
        print(f"     API-Calls (web_search):  {_TOKEN_STATS['calls']}")
        print(f"     Web-Suchen (abgerechnet):{_TOKEN_STATS['searches']}")
        print(f"     Treffer roh gesamt:      {gesamt_gefunden}")
        print(f"     NEU gespeichert:         {len(neue_projekte)}")
        print(f"     Kosten (gemessen):       ${kosten_gesamt:.3f}")
        if abgebrochen:
            print(f"     ⚠️  Lauf wegen Zeitbudget vorzeitig beendet (sauber finalisiert)")

        # Alle nicht-ignorierten Projekte dieses Kunden für das Dashboard/E-Mail
        alle_projekte_kunde = sb_get("projekte", {
            "kunden_id":  f"eq.{auftrag['kunden_id']}",
            "ignorieren": "eq.false",
            "order":      "relevanz.desc.nullslast",
        })

        sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {
            "status":              "abgeschlossen",
            # Gemessene Kosten (kein Schätz-Faktor mehr nötig, da die Suchanzahl
            # jetzt exakt aus der API kommt).
            "kosten_tatsaechlich": round(kosten_gesamt, 4),
        })

        # E-Mail: bevorzugt die NEUEN Treffer dieses Laufs; sonst Top-Projekte
        email_projekte = neue_projekte if neue_projekte else alle_projekte_kunde[:10]
        sende_email(kunde, auftrag, email_projekte)

        dauer = time.time() - start_zeit
        sende_admin_benachrichtigung(
            kunde=kunde, auftrag=auftrag,
            anzahl_neue=len(neue_projekte),
            anzahl_gesamt=len(alle_projekte_kunde),
            dauer_sek=dauer, gesamt_artikel=gesamt_gefunden,
        )

        print(f"\n  ✅ Auftrag {sid} abgeschlossen")
        print(f"     Neu in diesem Lauf:  {len(neue_projekte)}")
        print(f"     Gesamt im Dashboard: {len(alle_projekte_kunde)}")
        print(f"     Dauer:               {dauer/60:.1f} Minuten")

    except Exception as e:
        print(f"\n  ❌ FEHLER bei Auftrag {sid}: {e}")
        import traceback
        traceback.print_exc()
        sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {"status": "fehler"})


# =============================================================================
# EINSTIEGSPUNKT
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🔍  ProjectScout Agent – Start")
    print(f"   Zeitpunkt: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    spezifische_id = os.environ.get("SUCHANFRAGE_ID", "").strip() or None
    auftraege = lade_offene_auftraege(spezifische_id)

    if not auftraege:
        print("✅ Keine offenen Aufträge – Agent beendet.")
        sys.exit(0)

    for auftrag in auftraege:
        verarbeite_auftrag(auftrag)

    print("\n" + "=" * 60)
    print("✅ Alle Aufträge abgearbeitet.")
    print("=" * 60)
