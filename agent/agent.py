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
#   SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS
# =============================================================================

import os
import sys
import json
import time
import hashlib
import smtplib
import requests
import re
import anthropic
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(__file__))
from medien_datenbank import get_alle_bundeslaender_kuerzel
from gemeinden_datenbank import get_gemeinden_fuer_bundeslaender

# =============================================================================
# KONFIGURATION
# =============================================================================

SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SECRET_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SMTP_HOST         = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER         = os.environ.get("SMTP_USER", "")
SMTP_PASS         = os.environ.get("SMTP_PASS", "")

DASHBOARD_BASE_URL = "https://project-scout.at/dashboard.html"
ADMIN_EMAIL        = "office@project-scout.at"

# Anthropic Client für web_search-gestützte Suche
ANTHROPIC_CLIENT = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Modellwahl ADAPTIV nach Umfang:
#   - Wenige Bundesländer (≤3): Sonnet (höchste Qualität, Zeit reicht locker)
#   - Viele Bundesländer (≥4):  Haiku  (schnell genug für das Zeitbudget)
# Beide Strings sind erprobt. Zum Erzwingen einfach MODELL_FIX setzen.
MODELL_SONNET = "claude-sonnet-4-5"
MODELL_HAIKU  = "claude-haiku-4-5-20251001"
MODELL_FIX    = None   # z.B. MODELL_SONNET um immer Sonnet zu verwenden

def _modell_fuer_scope(anzahl_bundeslaender: int) -> str:
    if MODELL_FIX:
        return MODELL_FIX
    return MODELL_SONNET if anzahl_bundeslaender <= 3 else MODELL_HAIKU

# web_search-Tool: wie viele Einzelsuchen Claude pro API-Call durchführen darf.
# Höher = gründlicher = mehr Treffer (aber etwas langsamer/teurer).
WEB_SEARCH_MAX_USES = 9
MAX_TOKENS_SUCHE    = 4500

# Zeitbudget: nach dieser Zeit werden KEINE neuen Suchen mehr gestartet, damit
# der Agent IMMER sauber finalisiert (E-Mail + Status) bevor GitHub bei 55 Min
# hart abbricht. Verhindert "agent_laeuft"-Hänger wie in früheren Läufen.
ZEITBUDGET_SEKUNDEN = 45 * 60

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

# Token-Zähler für Kosten-Logging
_TOKEN_STATS = {"input": 0, "output": 0, "calls": 0}

SUPABASE_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

# =============================================================================
# SUPABASE HILFSFUNKTIONEN
# =============================================================================

def sb_get(tabelle: str, params: dict = None) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    resp = requests.get(url, headers=SUPABASE_HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

def sb_patch(tabelle: str, filter_params: dict, daten: dict) -> None:
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    resp = requests.patch(url, headers=headers, params=filter_params, json=daten, timeout=10)
    resp.raise_for_status()

def sb_insert(tabelle: str, daten: dict) -> dict | None:
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    resp = requests.post(url, headers=SUPABASE_HEADERS, json=daten, timeout=10)
    if resp.status_code in (200, 201):
        result = resp.json()
        return result[0] if isinstance(result, list) and result else result
    return None

def sb_upsert(tabelle: str, daten: dict, on_conflict: str) -> None:
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    headers = {**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    params = {"on_conflict": on_conflict}
    resp = requests.post(url, headers=headers, params=params, json=daten, timeout=10)
    resp.raise_for_status()

# =============================================================================
# SCHRITT 1: OFFENE AUFTRÄGE LADEN
# =============================================================================

def lade_offene_auftraege(spezifische_id: str = None) -> list:
    if spezifische_id:
        params = {"id": f"eq.{spezifische_id}"}
    else:
        params = {"status": "eq.bezahlt"}
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


def _bezirke_mit_orten(bundesland: str, max_bezirke: int = 14,
                       orte_pro_bezirk: int = 5) -> dict:
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
- Sammle ALLE konkreten Projekte, auch kleinere oder regionale. Lieber 12 Projekte als 3.

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
    "datum": "YYYY-MM-DD des Berichts/Beschlusses wenn bekannt, sonst leer",
    "artikel_url": "vollständige https-URL der Quelle",
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
        print(f"    ❌ web_search Fehler: {str(ex)[:140]}")
        return []


# -----------------------------------------------------------------------------
# Die vier Suchgleise
# -----------------------------------------------------------------------------

def suche_projekte(bundesland: str, kategorie_name: str, gewerke_liste: list,
                   suchbegriffe: list, cutoff: str, heute: str, modell: str) -> list:
    """Suchgleis 1/2: gewerk-fokussierte Projekt-/Bausuche in Medien."""
    bl_name     = BL_NAMEN.get(bundesland, bundesland)
    gewerke_txt = ", ".join(gewerke_liste[:14]) if gewerke_liste else kategorie_name
    signale     = ", ".join(suchbegriffe[:10])

    prompt = f"""Suche aktuelle Projekte im Bereich "{kategorie_name}" in {bl_name} (Österreich).

ZEITRAUM: Nur Projekte/Meldungen vom {cutoff} bis {heute}. Ältere Berichte ignorieren.

GESUCHTE GEWERKE DES KUNDEN: {gewerke_txt}
NÜTZLICHE SIGNALWÖRTER: {signale}

FÜHRE MEHRERE VERSCHIEDENE WEB-SUCHEN DURCH, z.B.:
- "Spatenstich {bl_name} 2026"
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

DURCHSUCHE GEZIELT DIESE VERGABEPORTALE (mehrere Suchen!):
- ankoe.at und vergabe.gv.at (offizielles österreichisches Vergabeportal)
- auftrag.at
- lieferanzeiger.at
- offenevergaben.at
- ted.europa.eu (EU-Ausschreibungen, Land = Österreich)
- bbg.gv.at (Bundesbeschaffung)
- Vergabe-/Beschaffungsplattformen der Länder und größeren Städte

SUCHE u.a. NACH: "Bauausschreibung {bl_name}", "Vergabe Bauleistung {bl_name}",
"Generalunternehmer Ausschreibung {bl_name}", "Hochbau/Tiefbau Ausschreibung {bl_name}",
sowie {signale} jeweils + "{bl_name}".

Für jeden Treffer: Auftraggeber, Gewerk, geschätzte Auftragssumme, Frist.
Diese Treffer sind besonders wertvoll → phase="Ausschreibung" oder "Vergabe",
relevanz typischerweise 8-10. Antworte als JSON-Array."""
    return _websearch_aufruf(prompt, modell, max_searches=WEB_SEARCH_MAX_USES + 1)


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
- "[Ort] Spatenstich Gemeinde 2026"
- "[Bezirk] Gemeinde Bauprojekt 2026"
- "Stadtrat [Stadt] Neubau Beschluss"
- "[Ort] Kindergarten Neubau" / "[Ort] Schule Erweiterung" / "[Ort] Feuerwehrhaus"
- "[Ort] Bauhof Neubau" / "[Ort] Amtsgebäude" / "[Ort] Gemeindezentrum"
- "[Ort] Kanal Wasserleitung Sanierung" / "[Ort] Ortsstraße Sanierung"

QUELLEN: meinbezirk.at (nach Bezirk gegliedert!), lokale Bezirksblätter,
Gemeinde-Websites (.gv.at), tips.at, Landespresse {bl_name}.

Kommunale Projekte sind oft kleiner – nimm sie TROTZDEM auf (relevanz 4-7).
Ziel: möglichst viele konkrete kommunale Vorhaben. Antworte als JSON-Array."""
    return _websearch_aufruf(prompt, modell, max_searches=WEB_SEARCH_MAX_USES + 1)


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

    # ── BUNDESLAND-FILTER ──────────────────────────────────────────────────
    bl = _normalisiere_bundesland(str(projekt.get("bundesland") or ""))
    if bundesland_erwartet:
        if bl and bl != bundesland_erwartet:
            # Claude hat explizit ein anderes BL angegeben → verwerfen
            return None
        if not bl:
            # Claude hat kein BL angegeben → erwartetes BL einsetzen
            bl = bundesland_erwartet

    url = str(projekt.get("artikel_url") or "").strip()
    if url and not url.startswith("http"):
        url = ""

    return {
        "titel":       titel[:200],
        "beschreibung": beschreibung,
        "ort":         str(projekt.get("ort") or "").strip(),
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
    Speichert ein gefundenes Projekt in Supabase.
    Duplikat-Check via URL-Hash über ALLE bisherigen Läufe des Kunden.
    So wächst das persönliche Dashboard des Kunden – ohne Duplikate.
    """
    url_hash = berechne_hash(artikel["url"])

    # Prüfen ob dieses Projekt für diesen Kunden BEREITS EXISTIERT (egal aus welchem Lauf)
    vorhandene = sb_get("projekte", {
        "rohdaten_hash": f"eq.{url_hash}",
        "kunden_id":     f"eq.{auftrag['kunden_id']}",
        # KEIN suchanfrage_id Filter → laufübergreifende Duplikaterkennung
    })

    if vorhandene:
        # Bereits bekannt – nur Timestamp aktualisieren
        sb_patch("projekte",
                 {"rohdaten_hash": f"eq.{url_hash}", "kunden_id": f"eq.{auftrag['kunden_id']}"},
                 {"zuletzt_gecrawlt": datetime.now(timezone.utc).isoformat()})
        return False

    # Neu → speichern
    jetzt = datetime.now(timezone.utc).isoformat()
    projekt = {
        "kunden_id":         auftrag["kunden_id"],
        "suchanfrage_id":    auftrag["id"],
        "titel":             analyse.get("titel") or artikel["titel"][:200],
        "ort":               analyse.get("ort", ""),
        "bezirk":            analyse.get("bezirk", ""),
        "bundesland":        analyse.get("bundesland", ""),
        "kategorie":         analyse.get("kategorie", "Sonstiges"),
        "volumen":           analyse.get("volumen", ""),
        "phase":             analyse.get("phase", ""),
        "quelle":            artikel["quelle_name"],
        "artikel_url":       artikel["url"],
        "beschreibung":      analyse.get("beschreibung", ""),
        "relevanz":          analyse.get("relevanz", 5),
        "ignorieren":        False,
        "gemerkt":           False,
        "ist_oeffentlich":   False,
        "erstmals_gefunden": jetzt,
        "zuletzt_geaendert": jetzt,
        "zuletzt_gecrawlt":  jetzt,
        "rohdaten_hash":     url_hash,
        "cache_gueltig_bis": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }

    result = sb_insert("projekte", projekt)
    return result is not None

# =============================================================================
# SCHRITT 7: E-MAIL VERSENDEN
# WICHTIG: Fixer Dashboard-Link – nur kunden_id, keine suchanfrage_id!
# =============================================================================

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
    if not SMTP_USER or not SMTP_PASS:
        print("  ⚠️  SMTP nicht konfiguriert – E-Mail übersprungen")
        return False
    empfaenger = kunde.get("email")
    if not empfaenger:
        print("  ⚠️  Keine Kunden-E-Mail-Adresse vorhanden")
        return False
    anzahl = len(projekte_liste)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"ProjectScout: {anzahl} neue Projekte – {kunde.get('firmenname','')}"
    msg["From"]    = f"ProjectScout <{SMTP_USER}>"
    msg["To"]      = empfaenger

    dashboard_url = f"{DASHBOARD_BASE_URL}?kunden_id={auftrag['kunden_id']}"
    text_body = f"""ProjectScout – Ihre Ergebnisse sind da!

{anzahl} neue Projekte wurden gefunden.

Ihr persönliches Dashboard (als Favorit speichern!):
{dashboard_url}

ProjectScout – KI-gestützter Projekt-Scout für Österreich"""

    html_body = erstelle_email_html(kunde, auftrag, projekte_liste)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, empfaenger, msg.as_string())
        print(f"  ✉️  E-Mail gesendet an {empfaenger}")
        return True
    except Exception as e:
        print(f"  ❌ E-Mail-Fehler: {e}")
        return False


# =============================================================================
# ADMIN-BENACHRICHTIGUNG
# =============================================================================

def sende_admin_benachrichtigung(
    kunde: dict, auftrag: dict,
    anzahl_neue: int, anzahl_gesamt: int,
    dauer_sek: float, gesamt_artikel: int
) -> bool:
    """Sendet Benachrichtigung an office@project-scout.at nach jedem Scout-Lauf."""
    if not SMTP_USER or not SMTP_PASS:
        print("  ⚠️  Admin-Mail: SMTP nicht konfiguriert")
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

    msg = MIMEMultipart("alternative")
    msg["Subject"] = betreff
    msg["From"]    = f"ProjectScout <{SMTP_USER}>"
    msg["To"]      = ADMIN_EMAIL
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo(); server.starttls(); server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ADMIN_EMAIL, msg.as_string())
        print(f"  📧 Admin-Benachrichtigung gesendet an {ADMIN_EMAIL}")
        return True
    except Exception as e:
        print(f"  ⚠️  Admin-Mail Fehler: {e}")
        return False


# =============================================================================
# HAUPTFUNKTION
# =============================================================================

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
        anzahl_gleise_pro_bl = len(gewerke_gruppen) + 2  # +Vergabe +Kommunal
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
                neu = _verarbeite_treffer(roh, bl_filter, 4, gesehen, neue_projekte, auftrag, cutoff_dt=cutoff_dt, heute_dt=heute_dt)
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
                neu = _verarbeite_treffer(roh, bl_filter, 4, gesehen, neue_projekte, auftrag, cutoff_dt=cutoff_dt, heute_dt=heute_dt)
                print(f"     → {len(roh)} gefunden, {neu} neu gespeichert")

            # ── Gleis 4: Kommunal / Gemeinderats- & Stadtratsbeschlüsse ──
            if not zeit_aufgebraucht():
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
        kosten_input  = (_TOKEN_STATS["input"]  / 1_000_000) * 0.80
        kosten_output = (_TOKEN_STATS["output"] / 1_000_000) * 4.00
        kosten_search = (_TOKEN_STATS["calls"] * 5 / 1000) * 10.00
        kosten_gesamt = kosten_input + kosten_output + kosten_search

        print(f"\n{'='*60}")
        print(f"  📊 ZUSAMMENFASSUNG")
        print(f"{'='*60}")
        print(f"     API-Calls (web_search):  {_TOKEN_STATS['calls']}")
        print(f"     Treffer roh gesamt:      {gesamt_gefunden}")
        print(f"     NEU gespeichert:         {len(neue_projekte)}")
        print(f"     Kosten ca.:              ${kosten_gesamt:.3f}")
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
            "kosten_tatsaechlich": round(kosten_gesamt * 0.92, 4),
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
    print("=" * 60)BL_NAMEN = {
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

# Token-Zähler für Kosten-Logging
_TOKEN_STATS = {"input": 0, "output": 0, "calls": 0}

SUPABASE_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

# =============================================================================
# SUPABASE HILFSFUNKTIONEN
# =============================================================================

def sb_get(tabelle: str, params: dict = None) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    resp = requests.get(url, headers=SUPABASE_HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

def sb_patch(tabelle: str, filter_params: dict, daten: dict) -> None:
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    resp = requests.patch(url, headers=headers, params=filter_params, json=daten, timeout=10)
    resp.raise_for_status()

def sb_insert(tabelle: str, daten: dict) -> dict | None:
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    resp = requests.post(url, headers=SUPABASE_HEADERS, json=daten, timeout=10)
    if resp.status_code in (200, 201):
        result = resp.json()
        return result[0] if isinstance(result, list) and result else result
    return None

def sb_upsert(tabelle: str, daten: dict, on_conflict: str) -> None:
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    headers = {**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"}
    params = {"on_conflict": on_conflict}
    resp = requests.post(url, headers=headers, params=params, json=daten, timeout=10)
    resp.raise_for_status()

# =============================================================================
# SCHRITT 1: OFFENE AUFTRÄGE LADEN
# =============================================================================

def lade_offene_auftraege(spezifische_id: str = None) -> list:
    if spezifische_id:
        params = {"id": f"eq.{spezifische_id}"}
    else:
        params = {"status": "eq.bezahlt"}
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


def _bezirke_mit_orten(bundesland: str, max_bezirke: int = 14,
                       orte_pro_bezirk: int = 5) -> dict:
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
- Sammle ALLE konkreten Projekte, auch kleinere oder regionale. Lieber 12 Projekte als 3.

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
    "datum": "YYYY-MM-DD des Berichts/Beschlusses wenn bekannt, sonst leer",
    "artikel_url": "vollständige https-URL der Quelle",
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
        print(f"    ❌ web_search Fehler: {str(ex)[:140]}")
        return []


# -----------------------------------------------------------------------------
# Die vier Suchgleise
# -----------------------------------------------------------------------------

def suche_projekte(bundesland: str, kategorie_name: str, gewerke_liste: list,
                   suchbegriffe: list, cutoff: str, heute: str, modell: str) -> list:
    """Suchgleis 1/2: gewerk-fokussierte Projekt-/Bausuche in Medien."""
    bl_name     = BL_NAMEN.get(bundesland, bundesland)
    gewerke_txt = ", ".join(gewerke_liste[:14]) if gewerke_liste else kategorie_name
    signale     = ", ".join(suchbegriffe[:10])

    prompt = f"""Suche aktuelle Projekte im Bereich "{kategorie_name}" in {bl_name} (Österreich).

ZEITRAUM: Nur Projekte/Meldungen vom {cutoff} bis {heute}. Ältere Berichte ignorieren.

GESUCHTE GEWERKE DES KUNDEN: {gewerke_txt}
NÜTZLICHE SIGNALWÖRTER: {signale}

FÜHRE MEHRERE VERSCHIEDENE WEB-SUCHEN DURCH, z.B.:
- "Spatenstich {bl_name} 2026"
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

DURCHSUCHE GEZIELT DIESE VERGABEPORTALE (mehrere Suchen!):
- ankoe.at und vergabe.gv.at (offizielles österreichisches Vergabeportal)
- auftrag.at
- lieferanzeiger.at
- offenevergaben.at
- ted.europa.eu (EU-Ausschreibungen, Land = Österreich)
- bbg.gv.at (Bundesbeschaffung)
- Vergabe-/Beschaffungsplattformen der Länder und größeren Städte

SUCHE u.a. NACH: "Bauausschreibung {bl_name}", "Vergabe Bauleistung {bl_name}",
"Generalunternehmer Ausschreibung {bl_name}", "Hochbau/Tiefbau Ausschreibung {bl_name}",
sowie {signale} jeweils + "{bl_name}".

Für jeden Treffer: Auftraggeber, Gewerk, geschätzte Auftragssumme, Frist.
Diese Treffer sind besonders wertvoll → phase="Ausschreibung" oder "Vergabe",
relevanz typischerweise 8-10. Antworte als JSON-Array."""
    return _websearch_aufruf(prompt, modell, max_searches=WEB_SEARCH_MAX_USES + 1)


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
- "[Ort] Spatenstich Gemeinde 2026"
- "[Bezirk] Gemeinde Bauprojekt 2026"
- "Stadtrat [Stadt] Neubau Beschluss"
- "[Ort] Kindergarten Neubau" / "[Ort] Schule Erweiterung" / "[Ort] Feuerwehrhaus"
- "[Ort] Bauhof Neubau" / "[Ort] Amtsgebäude" / "[Ort] Gemeindezentrum"
- "[Ort] Kanal Wasserleitung Sanierung" / "[Ort] Ortsstraße Sanierung"

QUELLEN: meinbezirk.at (nach Bezirk gegliedert!), lokale Bezirksblätter,
Gemeinde-Websites (.gv.at), tips.at, Landespresse {bl_name}.

Kommunale Projekte sind oft kleiner – nimm sie TROTZDEM auf (relevanz 4-7).
Ziel: möglichst viele konkrete kommunale Vorhaben. Antworte als JSON-Array."""
    return _websearch_aufruf(prompt, modell, max_searches=WEB_SEARCH_MAX_USES + 1)


# -----------------------------------------------------------------------------
# Validierung & Normalisierung eines einzelnen Treffers
# -----------------------------------------------------------------------------

def validiere_und_normalisiere_projekt(projekt: dict, bundesland_erwartet: str,
                                        min_relevanz: int = 4) -> dict | None:
    """
    Prüft Plausibilität und normalisiert ein gefundenes Projekt.
    - Titel + Beschreibung vorhanden und lang genug?
    - Relevanz >= min_relevanz?
    - Bundesland plausibel (robuste Normalisierung; nur bei klarer Abweichung verwerfen)?
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

    bl = _normalisiere_bundesland(str(projekt.get("bundesland") or ""))
    # Bundesland-Filter nur bei spezifischer (Einzel-)Suche und klarer Abweichung
    if bundesland_erwartet and bl and bl != bundesland_erwartet:
        return None

    url = str(projekt.get("artikel_url") or "").strip()
    if url and not url.startswith("http"):
        url = ""

    return {
        "titel":       titel[:200],
        "beschreibung": beschreibung,
        "ort":         str(projekt.get("ort") or "").strip(),
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
                        gesehen: set, neue_projekte: list, auftrag: dict) -> int:
    """
    Validiert, dedupliziert (innerhalb des Laufs) und speichert eine Liste roher
    Treffer in Supabase. Gibt die Anzahl NEU gespeicherter Projekte zurück.
    """
    gespeichert = 0
    for raw in raw_liste:
        projekt = validiere_und_normalisiere_projekt(raw, bundesland, min_relevanz)
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
    Speichert ein gefundenes Projekt in Supabase.
    Duplikat-Check via URL-Hash über ALLE bisherigen Läufe des Kunden.
    So wächst das persönliche Dashboard des Kunden – ohne Duplikate.
    """
    url_hash = berechne_hash(artikel["url"])

    # Prüfen ob dieses Projekt für diesen Kunden BEREITS EXISTIERT (egal aus welchem Lauf)
    vorhandene = sb_get("projekte", {
        "rohdaten_hash": f"eq.{url_hash}",
        "kunden_id":     f"eq.{auftrag['kunden_id']}",
        # KEIN suchanfrage_id Filter → laufübergreifende Duplikaterkennung
    })

    if vorhandene:
        # Bereits bekannt – nur Timestamp aktualisieren
        sb_patch("projekte",
                 {"rohdaten_hash": f"eq.{url_hash}", "kunden_id": f"eq.{auftrag['kunden_id']}"},
                 {"zuletzt_gecrawlt": datetime.now(timezone.utc).isoformat()})
        return False

    # Neu → speichern
    jetzt = datetime.now(timezone.utc).isoformat()
    projekt = {
        "kunden_id":         auftrag["kunden_id"],
        "suchanfrage_id":    auftrag["id"],
        "titel":             analyse.get("titel") or artikel["titel"][:200],
        "ort":               analyse.get("ort", ""),
        "bezirk":            analyse.get("bezirk", ""),
        "bundesland":        analyse.get("bundesland", ""),
        "kategorie":         analyse.get("kategorie", "Sonstiges"),
        "volumen":           analyse.get("volumen", ""),
        "phase":             analyse.get("phase", ""),
        "quelle":            artikel["quelle_name"],
        "artikel_url":       artikel["url"],
        "beschreibung":      analyse.get("beschreibung", ""),
        "relevanz":          analyse.get("relevanz", 5),
        "ignorieren":        False,
        "gemerkt":           False,
        "ist_oeffentlich":   False,
        "erstmals_gefunden": jetzt,
        "zuletzt_geaendert": jetzt,
        "zuletzt_gecrawlt":  jetzt,
        "rohdaten_hash":     url_hash,
        "cache_gueltig_bis": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    }

    result = sb_insert("projekte", projekt)
    return result is not None

# =============================================================================
# SCHRITT 7: E-MAIL VERSENDEN
# WICHTIG: Fixer Dashboard-Link – nur kunden_id, keine suchanfrage_id!
# =============================================================================

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
    if not SMTP_USER or not SMTP_PASS:
        print("  ⚠️  SMTP nicht konfiguriert – E-Mail übersprungen")
        return False
    empfaenger = kunde.get("email")
    if not empfaenger:
        print("  ⚠️  Keine Kunden-E-Mail-Adresse vorhanden")
        return False
    anzahl = len(projekte_liste)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"ProjectScout: {anzahl} neue Projekte – {kunde.get('firmenname','')}"
    msg["From"]    = f"ProjectScout <{SMTP_USER}>"
    msg["To"]      = empfaenger

    dashboard_url = f"{DASHBOARD_BASE_URL}?kunden_id={auftrag['kunden_id']}"
    text_body = f"""ProjectScout – Ihre Ergebnisse sind da!

{anzahl} neue Projekte wurden gefunden.

Ihr persönliches Dashboard (als Favorit speichern!):
{dashboard_url}

ProjectScout – KI-gestützter Projekt-Scout für Österreich"""

    html_body = erstelle_email_html(kunde, auftrag, projekte_liste)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, empfaenger, msg.as_string())
        print(f"  ✉️  E-Mail gesendet an {empfaenger}")
        return True
    except Exception as e:
        print(f"  ❌ E-Mail-Fehler: {e}")
        return False


# =============================================================================
# ADMIN-BENACHRICHTIGUNG
# =============================================================================

def sende_admin_benachrichtigung(
    kunde: dict, auftrag: dict,
    anzahl_neue: int, anzahl_gesamt: int,
    dauer_sek: float, gesamt_artikel: int
) -> bool:
    """Sendet Benachrichtigung an office@project-scout.at nach jedem Scout-Lauf."""
    if not SMTP_USER or not SMTP_PASS:
        print("  ⚠️  Admin-Mail: SMTP nicht konfiguriert")
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

    msg = MIMEMultipart("alternative")
    msg["Subject"] = betreff
    msg["From"]    = f"ProjectScout <{SMTP_USER}>"
    msg["To"]      = ADMIN_EMAIL
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html",  "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo(); server.starttls(); server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ADMIN_EMAIL, msg.as_string())
        print(f"  📧 Admin-Benachrichtigung gesendet an {ADMIN_EMAIL}")
        return True
    except Exception as e:
        print(f"  ⚠️  Admin-Mail Fehler: {e}")
        return False


# =============================================================================
# HAUPTFUNKTION
# =============================================================================

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
        anzahl_gleise_pro_bl = len(gewerke_gruppen) + 2  # +Vergabe +Kommunal
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
                neu = _verarbeite_treffer(roh, bl_filter, 4, gesehen, neue_projekte, auftrag)
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
                neu = _verarbeite_treffer(roh, bl_filter, 4, gesehen, neue_projekte, auftrag)
                print(f"     → {len(roh)} gefunden, {neu} neu gespeichert")

            # ── Gleis 4: Kommunal / Gemeinderats- & Stadtratsbeschlüsse ──
            if not zeit_aufgebraucht():
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
                neu = _verarbeite_treffer(roh, bl_filter, 3, gesehen, neue_projekte, auftrag)
                print(f"     → {len(roh)} gefunden, {neu} neu gespeichert")

            if abgebrochen:
                break

        # ──────────────────────────────────────────────────────────────────
        # FINALISIERUNG (läuft IMMER, auch nach Zeitbudget-Abbruch)
        # ──────────────────────────────────────────────────────────────────
        kosten_input  = (_TOKEN_STATS["input"]  / 1_000_000) * 0.80
        kosten_output = (_TOKEN_STATS["output"] / 1_000_000) * 4.00
        kosten_search = (_TOKEN_STATS["calls"] * 5 / 1000) * 10.00
        kosten_gesamt = kosten_input + kosten_output + kosten_search

        print(f"\n{'='*60}")
        print(f"  📊 ZUSAMMENFASSUNG")
        print(f"{'='*60}")
        print(f"     API-Calls (web_search):  {_TOKEN_STATS['calls']}")
        print(f"     Treffer roh gesamt:      {gesamt_gefunden}")
        print(f"     NEU gespeichert:         {len(neue_projekte)}")
        print(f"     Kosten ca.:              ${kosten_gesamt:.3f}")
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
            "kosten_tatsaechlich": round(kosten_gesamt * 0.92, 4),
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
