# =============================================================================
# BauScout – KI-Agent
# Datei: agent/agent.py
#
# Ablauf:
#   1. Bezahlte Suchanfragen aus Supabase laden
#   2. Passende Medienquellen nach Bundesland filtern
#   3. Artikel crawlen (mit Cache-Prüfung)
#   4. KI-Analyse mit Claude Haiku (Relevanz-Check)
#   5. Projekte in Supabase speichern (Duplikat-Check via Hash)
#   6. Status auf "abgeschlossen" setzen
#   7. E-Mail mit Ergebnis-Zusammenfassung versenden
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
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse, quote_plus

# Medien-Datenbank importieren (liegt im selben Ordner)
sys.path.insert(0, os.path.dirname(__file__))
from medien_datenbank import get_quellen_fuer_bundeslaender, get_alle_bundeslaender_kuerzel

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

DASHBOARD_BASE_URL = "https://romanbaumschlager-netizen.github.io/bauscout/dashboard.html"

# Crawling-Einstellungen
MAX_ARTIKEL_PRO_QUELLE = 5       # Wie viele Artikel pro Quelle analysiert werden
REQUEST_TIMEOUT        = 15      # Sekunden pro HTTP-Request
PAUSE_ZWISCHEN_QUELLEN = 1.0     # Sekunden Pause zwischen Quellen (höfliches Crawling)

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
    """Daten aus Supabase lesen."""
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    resp = requests.get(url, headers=SUPABASE_HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

def sb_patch(tabelle: str, filter_params: dict, daten: dict) -> None:
    """Datensatz in Supabase aktualisieren."""
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    resp = requests.patch(url, headers=headers, params=filter_params, json=daten, timeout=10)
    resp.raise_for_status()

def sb_insert(tabelle: str, daten: dict) -> dict | None:
    """Neuen Datensatz in Supabase einfügen. Gibt eingefügten Datensatz zurück."""
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    resp = requests.post(url, headers=SUPABASE_HEADERS, json=daten, timeout=10)
    if resp.status_code in (200, 201):
        result = resp.json()
        return result[0] if isinstance(result, list) and result else result
    return None

def sb_upsert(tabelle: str, daten: dict, on_conflict: str) -> None:
    """Datensatz einfügen oder aktualisieren (Upsert)."""
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    headers = {**SUPABASE_HEADERS, "Prefer": f"resolution=merge-duplicates,return=minimal"}
    params = {"on_conflict": on_conflict}
    resp = requests.post(url, headers=headers, params=params, json=daten, timeout=10)
    resp.raise_for_status()

# =============================================================================
# SCHRITT 1: OFFENE AUFTRÄGE LADEN
# =============================================================================

def lade_offene_auftraege(spezifische_id: str = None) -> list:
    """
    Lädt alle Suchanfragen mit Status 'bezahlt' aus Supabase.
    Optional: nur eine spezifische ID laden.
    """
    if spezifische_id:
        params = {"id": f"eq.{spezifische_id}"}
    else:
        params = {"status": "eq.bezahlt"}

    auftraege = sb_get("suchanfragen", params)
    print(f"📋 {len(auftraege)} offener Auftrag/Aufträge gefunden")
    return auftraege

def lade_kundendaten(kunden_id: str) -> dict | None:
    """Kundendaten (E-Mail, Firmenname) für einen Auftrag laden."""
    ergebnis = sb_get("kunden", {"id": f"eq.{kunden_id}"})
    return ergebnis[0] if ergebnis else None

# =============================================================================
# SCHRITT 2: SUCHBEGRIFFE AUFBAUEN
# =============================================================================

# Gewerk → relevante Suchbegriffe auf Deutsch
GEWERK_KEYWORDS = {
    # ── BAU & ROHBAU ──
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

    # ── AUSBAU & HAUSTECHNIK ──
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

    # ── ENERGIE & UMWELT ──
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

    # ── INFRASTRUKTUR & VERKEHR ──
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

    # ── IMMOBILIEN & GRUNDSTÜCKE ──
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

    # ── ÖFFENTLICHE PROJEKTE & SOZIALES ──
    "Schulen / Kindergärten":               ["Schule", "Kindergarten", "Bildungseinrichtung", "Schulbau", "Volksschule"],
    "Pflegeheime / Senioreneinrichtungen":  ["Pflegeheim", "Seniorenheim", "Altenheim", "Senioreneinrichtung"],
    "Krankenhäuser / Ärztezentren":         ["Krankenhaus", "Klinik", "Ärztehaus", "Ambulanz", "Gesundheitszentrum"],
    "Sporthallen / Freizeitanlagen":        ["Sporthalle", "Sportzentrum", "Freizeitanlage", "Turnhalle", "Hallenbad"],
    "Gemeindebauten / Rathäuser":           ["Gemeindeamt", "Rathaus", "Gemeindegebäude", "Verwaltungsgebäude"],
    "Feuerwehr / Rettung":                  ["Feuerwehr", "Feuerwehrhaus", "Feuerwehrgebäude", "Rettungsstation"],
    "Sozialwohnbau":                        ["Sozialwohnbau", "Gemeindebau", "Genossenschaftswohnbau", "Sozialbau"],
    "Kultureinrichtungen":                  ["Kulturhaus", "Theater", "Museum", "Musikschule", "Veranstaltungssaal"],
    "Friedhöfe / Kapellen":                 ["Friedhof", "Kapelle", "Aufbahrungshalle", "Friedhofsanlage"],

    # ── FAHRZEUGE & AUSRÜSTUNG ──
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

    # ── LANDSCHAFT & AUSSENANLAGEN ──
    "Landschaftsbau / Gartengestaltung":    ["Landschaftsbau", "Gartengestaltung", "Grünanlage", "Begrünung"],
    "Parkanlagen / Grünflächen":            ["Parkanlage", "Grünfläche", "Stadtgrün", "Bepflanzung"],
    "Spielplätze / Freizeitanlagen":        ["Spielplatz", "Spielgeräte", "Freizeitanlage", "Kinderspielplatz"],
    "Sportplätze / Kunstrasen":             ["Sportplatz", "Fußballplatz", "Kunstrasen", "Sportanlage"],
    "Bewässerungsanlagen":                  ["Bewässerungsanlage", "Beregnung", "Bewässerungssystem"],
    "Forstarbeiten / Holzschlägerung":      ["Forstarbeiten", "Holzschlägerung", "Waldpflege", "Forstwirtschaft"],
    "Schädlingsbekämpfung / Pflanzenpflege":["Schädlingsbekämpfung", "Pflanzenschutz", "Baumpflege", "Pflanzenpflege"],
    "Flurbereinigung":                      ["Flurbereinigung", "Grundzusammenlegung", "Agrargemeinschaft"],

    # ── GASTRONOMIE & TOURISMUS ──
    "Hotelneubauten / Erweiterungen":       ["Hotel", "Hotelbau", "Hotelerweiterung", "Beherbergung", "Resort"],
    "Gastronomiebetriebe / Konzessionen":   ["Gastronomie", "Restaurant", "Gasthaus", "Konzession", "Gastronomiebetrieb"],
    "Tourismusinfrastruktur":               ["Tourismus", "Tourismusanlage", "Tourismusentwicklung", "Freizeitinfrastruktur"],
    "Seilbahnen / Skilifte":               ["Seilbahn", "Skilift", "Gondelbahn", "Sesselbahn", "Bergbahn"],
    "Campingplätze":                        ["Campingplatz", "Camping", "Zeltplatz", "Wohnmobilstellplatz"],
    "Veranstaltungsstätten":                ["Veranstaltungsstätte", "Messehalle", "Kongresszentrum", "Eventhalle"],
    "Küchen- / Gastronomieausstattung":     ["Küchenausstattung", "Gastronomieausstattung", "Gastrogeräte"],
    "Freizeitparks / Erlebnisanlagen":      ["Freizeitpark", "Erlebnisanlage", "Attraktionen", "Freizeiteinrichtung"],

    # ── PLANUNG & BERATUNG ──
    "Architektur / Gebäudeplanung":         ["Architekt", "Architektur", "Gebäudeplanung", "Planung", "Entwurf"],
    "Statik / Tragwerksplanung":            ["Statik", "Tragwerksplanung", "Statiker", "Tragwerksplaner"],
    "Vermessung / Geodäsie":                ["Vermessung", "Geodäsie", "Vermessungsbüro", "Kataster", "Lageplan"],
    "Umweltgutachten / UVP":                ["Umweltgutachten", "UVP", "Umweltverträglichkeit", "Gutachten"],
    "Projektmanagement / Bauleitung":       ["Projektmanagement", "Bauleitung", "Projektsteuerung", "Örtliche Bauaufsicht"],
    "Energieberatung":                      ["Energieberatung", "Energieausweis", "Energieeffizienz", "Energiekonzept"],
    "Rechtsberatung / Vergaberecht":        ["Vergaberecht", "Rechtsberatung", "Ausschreibung", "Vergabeverfahren"],
    "Finanzierung / Fördermittel":          ["Förderung", "Fördermittel", "Wohnbauförderung", "Investitionsförderung"],

    # ── LANDWIRTSCHAFT & FORST ──
    "Landwirtschaftliche Bauten / Stallbau":["Stallbau", "Landwirtschaftliches Gebäude", "Halle", "Maschinenhalle"],
    "Silos / Lagerhallen":                  ["Silo", "Lagerhalle", "Getreidesilo", "Lagergebäude"],
    "Biogasanlagen":                        ["Biogasanlage", "Biogas", "Biogasanlage", "Vergärungsanlage"],
    "Bewässerung / Drainage":               ["Bewässerung", "Drainage", "Drainagesystem", "Entwässerung"],
    "Forststraßen":                         ["Forststraße", "Waldweg", "Forstweg", "Erschließung"],
    "Landmaschinen / Geräte":               ["Landmaschine", "Traktor", "Erntemaschine", "Landwirtschaftsmaschine"],
    "Weinbau / Obstbau Infrastruktur":      ["Weinbau", "Obstbau", "Weingut", "Mosterei", "Kellerei"],
    "Fischzucht / Aquakultur":              ["Fischzucht", "Aquakultur", "Fischteich", "Fischerei"],

    # ── INDUSTRIE & GEWERBE ──
    "Industriehallen / Werkshallen":        ["Industriehalle", "Werkshalle", "Produktionshalle", "Fabrik"],
    "Gewerbeparks / Betriebsanlagen":       ["Gewerbepark", "Betriebsanlage", "Betriebsgebäude", "Gewerbezone"],
    "Produktionsanlagen":                   ["Produktionsanlage", "Fertigungsanlage", "Produktionsstätte"],
    "Lagerhallen / Logistikzentren":        ["Lagerhalle", "Logistikzentrum", "Logistikhalle", "Distributionszentrum"],
    "Reinräume / Labore":                   ["Reinraum", "Labor", "Laborgebäude", "Reinraumanlage"],
    "Tankstellen / Waschanlagen":           ["Tankstelle", "Waschanlage", "Tankstellenbau", "Autopflegeanlage"],
    "Kälteanlagen / Kühlhäuser":            ["Kälteanlage", "Kühlhaus", "Tiefkühlanlage", "Kältetechnik"],
    "Fördertechnik / Förderanlagen":        ["Fördertechnik", "Förderanlage", "Förderband", "Materialfluss"],
}

# Allgemeine Keywords (immer dabei – unabhängig von Branche)
BASIS_KEYWORDS = ["Ausschreibung", "Vergabe", "Baubewilligung", "Projekt", "Vorhaben", "Planung", "Beschluss"]

def baue_suchbegriffe(auftrag: dict) -> list[str]:
    """
    Erstellt eine Liste von Suchbegriffen basierend auf den gewählten Gewerken
    und optionalen Zusatz-Keywords des Kunden.
    """
    begriffe = list(BASIS_KEYWORDS)

    gewerke = auftrag.get("gewerke") or []
    if isinstance(gewerke, str):
        try:
            gewerke = json.loads(gewerke)
        except Exception:
            gewerke = [gewerke]

    for gewerk in gewerke:
        if gewerk in GEWERK_KEYWORDS:
            begriffe.extend(GEWERK_KEYWORDS[gewerk])

    # Zusatz-Keywords des Kunden (freies Textfeld)
    zusatz = auftrag.get("zusatz_keywords") or ""
    if zusatz:
        for kw in re.split(r"[,;\n]+", zusatz):
            kw = kw.strip()
            if kw:
                begriffe.append(kw)

    # Duplikate entfernen, Reihenfolge behalten
    gesehen = set()
    ergebnis = []
    for b in begriffe:
        if b.lower() not in gesehen:
            gesehen.add(b.lower())
            ergebnis.append(b)

    print(f"  🔍 Suchbegriffe ({len(ergebnis)}): {', '.join(ergebnis[:8])}{'...' if len(ergebnis) > 8 else ''}")
    return ergebnis

# =============================================================================
# SCHRITT 3: QUELLEN FILTERN
# =============================================================================

def waehle_quellen(auftrag: dict) -> list[dict]:
    """
    Filtert Medienquellen nach den gewählten Bundesländern.
    Wenn ganz_oesterreich=True, alle Quellen.
    """
    if auftrag.get("ganz_oesterreich"):
        bundeslaender = get_alle_bundeslaender_kuerzel()
        print(f"  🗺️  Ganz Österreich – alle Quellen")
    else:
        bundeslaender = auftrag.get("bundeslaender") or []
        if isinstance(bundeslaender, str):
            try:
                bundeslaender = json.loads(bundeslaender)
            except Exception:
                bundeslaender = [bundeslaender]

    quellen = get_quellen_fuer_bundeslaender(bundeslaender)
    print(f"  📰 {len(quellen)} Medienquellen für Bundesländer: {bundeslaender}")
    return quellen

# =============================================================================
# SCHRITT 4: ARTIKEL CRAWLEN
# =============================================================================

def berechne_hash(text: str) -> str:
    """SHA256-Hash eines Textes – für Duplikat-Erkennung."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

def crawle_suchergebnis(quelle: dict, suchbegriffe: list[str]) -> list[dict]:
    """
    Öffnet die Suchseite einer Quelle und extrahiert Artikel-Links + Titel.
    Gibt Liste von {"titel": ..., "url": ..., "snippet": ...} zurück.
    """
    artikel = []
    # Ersten relevanten Suchbegriff nehmen (meistens reicht einer pro Quelle)
    # Wir suchen nach dem ersten Nicht-Basis-Keyword, sonst Basis
    suchbegriff = suchbegriffe[0] if suchbegriffe else "Bauprojekt"

    suchpfad = quelle.get("suchpfad", "")
    if not suchpfad:
        return []

    url = suchpfad + quote_plus(suchbegriff)

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; BauScout/1.0; +https://romanbaumschlager-netizen.github.io/bauscout)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "de-AT,de;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)

        if resp.status_code != 200:
            print(f"    ⚠️  {quelle['name']}: HTTP {resp.status_code}")
            return []

        html = resp.text

        # Einfache Link-Extraktion: alle <a href="...">Titel</a>
        # Sucht nach Links die nach Artikeln aussehen (enthalten Jahreszahl oder /artikel/ etc.)
        muster = re.compile(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{10,120})</a>',
            re.IGNORECASE | re.DOTALL
        )
        basis_url = f"{urlparse(resp.url).scheme}://{urlparse(resp.url).netloc}"

        gefundene_urls = set()
        for match in muster.finditer(html):
            href, title = match.group(1).strip(), match.group(2).strip()
            title = re.sub(r'\s+', ' ', title).strip()

            # Relative URLs zu absoluten machen
            if href.startswith("/"):
                href = basis_url + href
            elif not href.startswith("http"):
                continue

            # Nur Links von derselben Domain
            if urlparse(href).netloc != urlparse(basis_url).netloc:
                continue

            # Offensichtliche Nicht-Artikel herausfiltern
            skip_patterns = ["/impressum", "/kontakt", "/datenschutz", "/agb",
                             "/login", "/register", "/suche", "/search", "#",
                             "javascript:", "mailto:"]
            if any(p in href.lower() for p in skip_patterns):
                continue

            if href in gefundene_urls:
                continue
            gefundene_urls.add(href)

            artikel.append({
                "titel": title,
                "url":   href,
                "quelle_name": quelle["name"],
            })

            if len(artikel) >= MAX_ARTIKEL_PRO_QUELLE:
                break

    except requests.exceptions.Timeout:
        print(f"    ⏱️  {quelle['name']}: Timeout")
    except Exception as e:
        print(f"    ❌ {quelle['name']}: {e}")

    return artikel

def lade_artikel_text(artikel_url: str) -> str:
    """
    Lädt den Volltext eines Artikels und extrahiert den Textinhalt.
    Gibt maximal 3000 Zeichen zurück (genug für KI-Analyse).
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; BauScout/1.0)",
            "Accept": "text/html",
            "Accept-Language": "de-AT,de;q=0.9",
        }
        resp = requests.get(artikel_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return ""

        html = resp.text

        # HTML-Tags entfernen
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>',  ' ', text,  flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&[a-zA-Z]+;', ' ', text)   # HTML-Entities
        text = re.sub(r'\s+', ' ', text).strip()

        return text[:3000]
    except Exception:
        return ""

# =============================================================================
# SCHRITT 5: KI-ANALYSE MIT CLAUDE HAIKU
# =============================================================================

ANALYSE_SYSTEM_PROMPT = """Du bist ein Assistent für ein österreichisches Bauunternehmen.
Deine Aufgabe: Analysiere Nachrichtenartikel und entscheide ob sie ein relevantes Bauprojekt beschreiben.

Antworte NUR mit einem JSON-Objekt. Kein Text davor oder danach.

Format:
{
  "ist_bauprojekt": true/false,
  "relevanz": 0-10,
  "titel": "Kurzer prägnanter Projekttitel",
  "ort": "Gemeinde oder Stadt",
  "bezirk": "Bezirksname oder leer",
  "bundesland": "Bundesland-Kürzel (W/NOE/OOE/SBG/STK/KTN/TIR/VBG/BGR)",
  "kategorie": "Hochbau/Tiefbau/Straßenbau/Kanal/Elektro/Dach/Fassade/Innenausbau/Sonstiges",
  "volumen": "Geschätztes Bauvolumen in Euro oder leer",
  "phase": "Planung/Ausschreibung/Vergabe/Bau/Fertigstellung",
  "beschreibung": "2-3 Sätze Zusammenfassung des Projekts"
}

Ein Artikel ist relevant (ist_bauprojekt=true) wenn er über:
- Neubauprojekte, Sanierungen, Infrastrukturprojekte berichtet
- Ausschreibungen oder Vergaben für Bauleistungen enthält
- Baubewilligungen oder Gemeinderatsbeschlüsse für Bauvorhaben beschreibt

Nicht relevant: Unfälle, Personalberichte, politische Meinungen, reine Immobilienpreisartikel"""

def analysiere_artikel_mit_ki(artikel: dict, suchbegriffe: list[str]) -> dict | None:
    """
    Sendet Artikel-Text an Claude Haiku zur Analyse.
    Gibt strukturiertes Ergebnis zurück oder None wenn nicht relevant.
    """
    volltext = lade_artikel_text(artikel["url"])
    if not volltext:
        volltext = artikel.get("titel", "")

    user_prompt = f"""Analysiere diesen Artikel auf Bauprojekt-Relevanz.

Gesuchte Gewerke/Themen: {', '.join(suchbegriffe[:5])}

Quelle: {artikel['quelle_name']}
URL: {artikel['url']}
Titel: {artikel['titel']}

Artikeltext (Auszug):
{volltext}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5",
                "max_tokens": 500,
                "system":     ANALYSE_SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": user_prompt}
                ],
            },
            timeout=30,
        )

        if resp.status_code != 200:
            print(f"    ⚠️  Anthropic API Fehler: {resp.status_code}")
            return None

        antwort_text = resp.json()["content"][0]["text"].strip()

        # JSON parsen (manchmal kommt es in ```json ... ``` verpackt)
        antwort_text = re.sub(r'^```json\s*', '', antwort_text)
        antwort_text = re.sub(r'\s*```$', '', antwort_text)

        ergebnis = json.loads(antwort_text)
        return ergebnis

    except json.JSONDecodeError as e:
        print(f"    ⚠️  JSON-Parse-Fehler: {e}")
        return None
    except Exception as e:
        print(f"    ❌ KI-Analyse Fehler: {e}")
        return None

# =============================================================================
# SCHRITT 6: PROJEKTE IN SUPABASE SPEICHERN
# =============================================================================

def speichere_projekt(analyse: dict, artikel: dict, auftrag: dict) -> bool:
    """
    Speichert ein gefundenes Projekt in der Supabase-Tabelle 'projekte'.
    Prüft vorher ob das Projekt schon bekannt ist (via URL-Hash).
    Gibt True zurück wenn neu gespeichert, False wenn bereits vorhanden.
    """
    # Duplikat-Check via URL-Hash
    url_hash = berechne_hash(artikel["url"])

    vorhandene = sb_get("projekte", {
        "rohdaten_hash": f"eq.{url_hash}",
        "kunden_id":     f"eq.{auftrag['kunden_id']}",
    })

    if vorhandene:
        # Bereits bekannt – nur "zuletzt_gecrawlt" aktualisieren
        sb_patch("projekte",
                 {"rohdaten_hash": f"eq.{url_hash}", "kunden_id": f"eq.{auftrag['kunden_id']}"},
                 {"zuletzt_gecrawlt": datetime.now(timezone.utc).isoformat()})
        return False

    # Neu → speichern
    jetzt = datetime.now(timezone.utc).isoformat()
    projekt = {
        "kunden_id":       auftrag["kunden_id"],
        "suchanfrage_id":  auftrag["id"],
        "titel":           analyse.get("titel") or artikel["titel"][:200],
        "ort":             analyse.get("ort", ""),
        "bezirk":          analyse.get("bezirk", ""),
        "bundesland":      analyse.get("bundesland", ""),
        "kategorie":       analyse.get("kategorie", "Sonstiges"),
        "volumen":         analyse.get("volumen", ""),
        "phase":           analyse.get("phase", ""),
        "quelle":          artikel["quelle_name"],
        "artikel_url":     artikel["url"],
        "beschreibung":    analyse.get("beschreibung", ""),
        "relevanz":        analyse.get("relevanz", 5),
        "ignorieren":      False,
        "ist_oeffentlich": False,
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
# =============================================================================

def erstelle_email_html(kunde: dict, auftrag: dict, projekte_liste: list[dict]) -> str:
    """Erstellt HTML-E-Mail mit Projektzusammenfassung."""
    anzahl = len(projekte_liste)
    dashboard_url = f"{DASHBOARD_BASE_URL}?kunden_id={auftrag['kunden_id']}&suchanfrage_id={auftrag['id']}"

    # Top-Projekte für E-Mail (max. 5, sortiert nach Relevanz)
    top_projekte = sorted(projekte_liste, key=lambda p: p.get("relevanz", 0), reverse=True)[:5]

    projekt_html = ""
    for p in top_projekte:
        relevanz_sterne = "★" * min(int(int(p.get("relevanz", 5)) / 2), 5)
        projekt_html += f"""
        <div style="background:#1a1a2e;border:1px solid #d4a017;border-radius:6px;padding:16px;margin-bottom:12px;">
          <div style="font-size:11px;color:#d4a017;font-family:monospace;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
            {p.get('kategorie','Sonstiges')} · {p.get('phase','unbekannt')} · {relevanz_sterne}
          </div>
          <div style="font-size:16px;font-weight:600;color:#f0f0f0;margin-bottom:6px;">{p.get('titel','')}</div>
          <div style="font-size:13px;color:#8b949e;margin-bottom:8px;">
            📍 {p.get('ort','')} {('· ' + p.get('bezirk','')) if p.get('bezirk') else ''} · {p.get('bundesland','')}
            {(' · 💶 ' + p.get('volumen','')) if p.get('volumen') else ''}
          </div>
          <div style="font-size:13px;color:#c9d1d9;margin-bottom:10px;">{p.get('beschreibung','')}</div>
          <a href="{p.get('artikel_url','#')}" style="color:#d4a017;font-size:12px;">→ Zum Artikel</a>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="background:#0a0a0a;color:#e6edf3;font-family:'DM Sans',Arial,sans-serif;max-width:600px;margin:0 auto;padding:32px 16px;">

  <div style="text-align:center;margin-bottom:32px;">
    <div style="font-size:32px;font-weight:900;letter-spacing:4px;color:#d4a017;">BAUSCOUT</div>
    <div style="font-size:13px;color:#8b949e;margin-top:4px;">KI-Bauprojekt-Scout für Österreich</div>
  </div>

  <div style="background:#161b22;border:1px solid #238636;border-radius:8px;padding:24px;margin-bottom:24px;text-align:center;">
    <div style="font-size:40px;font-weight:900;color:#d4a017;">{anzahl}</div>
    <div style="font-size:16px;color:#e6edf3;margin-top:4px;">Relevante Bauprojekte gefunden</div>
    <div style="font-size:13px;color:#8b949e;margin-top:8px;">für {kunde.get('firmenname','Ihr Unternehmen')}</div>
  </div>

  <p style="color:#8b949e;font-size:14px;margin-bottom:20px;">
    Ihr BauScout-Lauf ist abgeschlossen. Hier sind die {min(anzahl, 5)} relevantesten Projekte:
  </p>

  {projekt_html}

  <div style="text-align:center;margin:32px 0;">
    <a href="{dashboard_url}"
       style="background:#d4a017;color:#0a0a0a;font-weight:700;font-size:15px;padding:14px 32px;border-radius:4px;text-decoration:none;display:inline-block;">
      → Alle {anzahl} Projekte im Dashboard ansehen
    </a>
  </div>

  <div style="background:#161b22;border-radius:6px;padding:16px;font-size:12px;color:#8b949e;margin-top:24px;">
    <strong style="color:#e6edf3;">Excel-Export</strong> steht im Dashboard zum Download bereit.<br><br>
    Sie haben Fragen? Antworten Sie auf diese E-Mail.<br>
    <a href="https://romanbaumschlager-netizen.github.io/bauscout/" style="color:#d4a017;">bauscout.at</a>
  </div>

</body>
</html>"""

def sende_email(kunde: dict, auftrag: dict, projekte_liste: list[dict]) -> bool:
    """Versendet die Ergebnis-E-Mail an den Kunden."""
    if not SMTP_USER or not SMTP_PASS:
        print("  ⚠️  SMTP nicht konfiguriert – E-Mail übersprungen")
        return False

    empfaenger = kunde.get("email")
    if not empfaenger:
        print("  ⚠️  Keine Kunden-E-Mail-Adresse vorhanden")
        return False

    anzahl = len(projekte_liste)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"BauScout: {anzahl} Bauprojekte gefunden – {kunde.get('firmenname','')}"
    msg["From"]    = f"BauScout <{SMTP_USER}>"
    msg["To"]      = empfaenger

    # Text-Fallback
    text_body = f"""BauScout – Ihre Ergebnisse sind da!

{anzahl} relevante Bauprojekte wurden gefunden.

Dashboard: {DASHBOARD_BASE_URL}?kunden_id={auftrag['kunden_id']}&suchanfrage_id={auftrag['id']}

BauScout – KI-Bauprojekt-Scout für Österreich"""

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
# HAUPTFUNKTION: EINEN AUFTRAG ABARBEITEN
# =============================================================================

def verarbeite_auftrag(auftrag: dict) -> None:
    """Vollständige Verarbeitung eines bezahlten Auftrags."""
    sid = auftrag["id"]
    print(f"\n{'='*60}")
    print(f"🚀 Starte Auftrag: {sid}")
    print(f"   Gewerke:      {auftrag.get('gewerke')}")
    print(f"   Bundesländer: {auftrag.get('bundeslaender')}")
    print(f"   Zeitraum:     {auftrag.get('zeitraum_tage', 30)} Tage")
    print(f"{'='*60}")

    # Status → agent_laeuft
    sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {"status": "agent_laeuft"})

    try:
        # Kundendaten laden
        kunde = lade_kundendaten(auftrag["kunden_id"])
        if not kunde:
            raise ValueError(f"Keine Kundendaten für ID {auftrag['kunden_id']} gefunden")
        print(f"  👤 Kunde: {kunde.get('firmenname')} ({kunde.get('email')})")

        # Suchbegriffe + Quellen bestimmen
        suchbegriffe = baue_suchbegriffe(auftrag)
        quellen      = waehle_quellen(auftrag)

        # Crawling + KI-Analyse
        neue_projekte   = []
        gesamt_artikel  = 0
        gesamt_relevant = 0

        for i, quelle in enumerate(quellen, 1):
            print(f"\n  [{i:3}/{len(quellen)}] {quelle['name']}")
            time.sleep(PAUSE_ZWISCHEN_QUELLEN)

            artikel_liste = crawle_suchergebnis(quelle, suchbegriffe)
            if not artikel_liste:
                print(f"         → Keine Artikel gefunden")
                continue

            print(f"         → {len(artikel_liste)} Artikel gefunden")
            gesamt_artikel += len(artikel_liste)

            for artikel in artikel_liste:
                analyse = analysiere_artikel_mit_ki(artikel, suchbegriffe)
                if not analyse:
                    continue

                if not analyse.get("ist_bauprojekt"):
                    continue

                relevanz = analyse.get("relevanz", 0)
                if relevanz < 4:
                    continue

                print(f"         ✅ RELEVANT (Relevanz {relevanz}/10): {analyse.get('titel','')[:60]}")
                gesamt_relevant += 1

                ist_neu = speichere_projekt(analyse, artikel, auftrag)
                if ist_neu:
                    neue_projekte.append(analyse)

        print(f"\n  📊 Zusammenfassung:")
        print(f"     Artikel analysiert:  {gesamt_artikel}")
        print(f"     Relevante gefunden:  {gesamt_relevant}")
        print(f"     Neu gespeichert:     {len(neue_projekte)}")

        # Alle Projekte dieses Auftrags für E-Mail laden
        alle_projekte = sb_get("projekte", {
            "suchanfrage_id": f"eq.{sid}",
            "ignorieren":     "eq.false",
            "order":          "relevanz.desc.nullslast",
        })

        # Status → abgeschlossen
        sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {
            "status":               "abgeschlossen",
            "kosten_tatsaechlich":  berechne_tatsaechliche_kosten(gesamt_artikel),
        })

        # E-Mail versenden
        if alle_projekte:
            sende_email(kunde, auftrag, alle_projekte)
        else:
            print("  ℹ️  Keine relevanten Projekte – E-Mail mit 0-Ergebnis-Hinweis")
            sende_email(kunde, auftrag, [])

        print(f"\n  ✅ Auftrag {sid} abgeschlossen – {len(alle_projekte)} Projekte geliefert")

    except Exception as e:
        print(f"\n  ❌ FEHLER bei Auftrag {sid}: {e}")
        import traceback
        traceback.print_exc()
        # Status → fehler
        sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {
            "status": "fehler",
        })

def berechne_tatsaechliche_kosten(anzahl_artikel: int) -> float:
    """
    Grobe Kostenschätzung basierend auf API-Nutzung.
    Haiku: $1/$5 per MTok Input/Output
    ~500 Token Input + 200 Token Output pro Artikel
    """
    input_token  = anzahl_artikel * 500
    output_token = anzahl_artikel * 200
    kosten_usd   = (input_token / 1_000_000 * 1.0) + (output_token / 1_000_000 * 5.0)
    kosten_eur   = round(kosten_usd * 0.92, 4)
    return kosten_eur

# =============================================================================
# EINSTIEGSPUNKT
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🏗️  BauScout Agent – Start")
    print(f"   Zeitpunkt: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Optionale spezifische Auftrag-ID via Umgebungsvariable
    spezifische_id = os.environ.get("SUCHANFRAGE_ID", "").strip() or None

    auftraege = lade_offene_auftraege(spezifische_id)

    if not auftraege:
        print("✅ Keine offenen Aufträge – Agent beendet.")
        sys.exit(0)

    for auftrag in auftraege:
        verarbeite_auftrag(auftrag)

    print("\n" + "=" * 60)
    print("✅ Alle Aufträge abgearbeitet.")
    print("=" * 60)    resp.raise_for_status()

def sb_insert(tabelle: str, daten: dict) -> dict | None:
    """Neuen Datensatz in Supabase einfügen. Gibt eingefügten Datensatz zurück."""
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    resp = requests.post(url, headers=SUPABASE_HEADERS, json=daten, timeout=10)
    if resp.status_code in (200, 201):
        result = resp.json()
        return result[0] if isinstance(result, list) and result else result
    return None

def sb_upsert(tabelle: str, daten: dict, on_conflict: str) -> None:
    """Datensatz einfügen oder aktualisieren (Upsert)."""
    url = f"{SUPABASE_URL}/rest/v1/{tabelle}"
    headers = {**SUPABASE_HEADERS, "Prefer": f"resolution=merge-duplicates,return=minimal"}
    params = {"on_conflict": on_conflict}
    resp = requests.post(url, headers=headers, params=params, json=daten, timeout=10)
    resp.raise_for_status()

# =============================================================================
# SCHRITT 1: OFFENE AUFTRÄGE LADEN
# =============================================================================

def lade_offene_auftraege(spezifische_id: str = None) -> list:
    """
    Lädt alle Suchanfragen mit Status 'bezahlt' aus Supabase.
    Optional: nur eine spezifische ID laden.
    """
    if spezifische_id:
        params = {"id": f"eq.{spezifische_id}"}
    else:
        params = {"status": "eq.bezahlt"}

    auftraege = sb_get("suchanfragen", params)
    print(f"📋 {len(auftraege)} offener Auftrag/Aufträge gefunden")
    return auftraege

def lade_kundendaten(kunden_id: str) -> dict | None:
    """Kundendaten (E-Mail, Firmenname) für einen Auftrag laden."""
    ergebnis = sb_get("kunden", {"id": f"eq.{kunden_id}"})
    return ergebnis[0] if ergebnis else None

# =============================================================================
# SCHRITT 2: SUCHBEGRIFFE AUFBAUEN
# =============================================================================

# Gewerk → relevante Suchbegriffe auf Deutsch
GEWERK_KEYWORDS = {
    # ── BAU & ROHBAU ──
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

    # ── AUSBAU & HAUSTECHNIK ──
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

    # ── ENERGIE & UMWELT ──
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

    # ── INFRASTRUKTUR & VERKEHR ──
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

    # ── IMMOBILIEN & GRUNDSTÜCKE ──
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

    # ── ÖFFENTLICHE PROJEKTE & SOZIALES ──
    "Schulen / Kindergärten":               ["Schule", "Kindergarten", "Bildungseinrichtung", "Schulbau", "Volksschule"],
    "Pflegeheime / Senioreneinrichtungen":  ["Pflegeheim", "Seniorenheim", "Altenheim", "Senioreneinrichtung"],
    "Krankenhäuser / Ärztezentren":         ["Krankenhaus", "Klinik", "Ärztehaus", "Ambulanz", "Gesundheitszentrum"],
    "Sporthallen / Freizeitanlagen":        ["Sporthalle", "Sportzentrum", "Freizeitanlage", "Turnhalle", "Hallenbad"],
    "Gemeindebauten / Rathäuser":           ["Gemeindeamt", "Rathaus", "Gemeindegebäude", "Verwaltungsgebäude"],
    "Feuerwehr / Rettung":                  ["Feuerwehr", "Feuerwehrhaus", "Feuerwehrgebäude", "Rettungsstation"],
    "Sozialwohnbau":                        ["Sozialwohnbau", "Gemeindebau", "Genossenschaftswohnbau", "Sozialbau"],
    "Kultureinrichtungen":                  ["Kulturhaus", "Theater", "Museum", "Musikschule", "Veranstaltungssaal"],
    "Friedhöfe / Kapellen":                 ["Friedhof", "Kapelle", "Aufbahrungshalle", "Friedhofsanlage"],

    # ── FAHRZEUGE & AUSRÜSTUNG ──
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

    # ── LANDSCHAFT & AUSSENANLAGEN ──
    "Landschaftsbau / Gartengestaltung":    ["Landschaftsbau", "Gartengestaltung", "Grünanlage", "Begrünung"],
    "Parkanlagen / Grünflächen":            ["Parkanlage", "Grünfläche", "Stadtgrün", "Bepflanzung"],
    "Spielplätze / Freizeitanlagen":        ["Spielplatz", "Spielgeräte", "Freizeitanlage", "Kinderspielplatz"],
    "Sportplätze / Kunstrasen":             ["Sportplatz", "Fußballplatz", "Kunstrasen", "Sportanlage"],
    "Bewässerungsanlagen":                  ["Bewässerungsanlage", "Beregnung", "Bewässerungssystem"],
    "Forstarbeiten / Holzschlägerung":      ["Forstarbeiten", "Holzschlägerung", "Waldpflege", "Forstwirtschaft"],
    "Schädlingsbekämpfung / Pflanzenpflege":["Schädlingsbekämpfung", "Pflanzenschutz", "Baumpflege", "Pflanzenpflege"],
    "Flurbereinigung":                      ["Flurbereinigung", "Grundzusammenlegung", "Agrargemeinschaft"],

    # ── GASTRONOMIE & TOURISMUS ──
    "Hotelneubauten / Erweiterungen":       ["Hotel", "Hotelbau", "Hotelerweiterung", "Beherbergung", "Resort"],
    "Gastronomiebetriebe / Konzessionen":   ["Gastronomie", "Restaurant", "Gasthaus", "Konzession", "Gastronomiebetrieb"],
    "Tourismusinfrastruktur":               ["Tourismus", "Tourismusanlage", "Tourismusentwicklung", "Freizeitinfrastruktur"],
    "Seilbahnen / Skilifte":               ["Seilbahn", "Skilift", "Gondelbahn", "Sesselbahn", "Bergbahn"],
    "Campingplätze":                        ["Campingplatz", "Camping", "Zeltplatz", "Wohnmobilstellplatz"],
    "Veranstaltungsstätten":                ["Veranstaltungsstätte", "Messehalle", "Kongresszentrum", "Eventhalle"],
    "Küchen- / Gastronomieausstattung":     ["Küchenausstattung", "Gastronomieausstattung", "Gastrogeräte"],
    "Freizeitparks / Erlebnisanlagen":      ["Freizeitpark", "Erlebnisanlage", "Attraktionen", "Freizeiteinrichtung"],

    # ── PLANUNG & BERATUNG ──
    "Architektur / Gebäudeplanung":         ["Architekt", "Architektur", "Gebäudeplanung", "Planung", "Entwurf"],
    "Statik / Tragwerksplanung":            ["Statik", "Tragwerksplanung", "Statiker", "Tragwerksplaner"],
    "Vermessung / Geodäsie":                ["Vermessung", "Geodäsie", "Vermessungsbüro", "Kataster", "Lageplan"],
    "Umweltgutachten / UVP":                ["Umweltgutachten", "UVP", "Umweltverträglichkeit", "Gutachten"],
    "Projektmanagement / Bauleitung":       ["Projektmanagement", "Bauleitung", "Projektsteuerung", "Örtliche Bauaufsicht"],
    "Energieberatung":                      ["Energieberatung", "Energieausweis", "Energieeffizienz", "Energiekonzept"],
    "Rechtsberatung / Vergaberecht":        ["Vergaberecht", "Rechtsberatung", "Ausschreibung", "Vergabeverfahren"],
    "Finanzierung / Fördermittel":          ["Förderung", "Fördermittel", "Wohnbauförderung", "Investitionsförderung"],

    # ── LANDWIRTSCHAFT & FORST ──
    "Landwirtschaftliche Bauten / Stallbau":["Stallbau", "Landwirtschaftliches Gebäude", "Halle", "Maschinenhalle"],
    "Silos / Lagerhallen":                  ["Silo", "Lagerhalle", "Getreidesilo", "Lagergebäude"],
    "Biogasanlagen":                        ["Biogasanlage", "Biogas", "Biogasanlage", "Vergärungsanlage"],
    "Bewässerung / Drainage":               ["Bewässerung", "Drainage", "Drainagesystem", "Entwässerung"],
    "Forststraßen":                         ["Forststraße", "Waldweg", "Forstweg", "Erschließung"],
    "Landmaschinen / Geräte":               ["Landmaschine", "Traktor", "Erntemaschine", "Landwirtschaftsmaschine"],
    "Weinbau / Obstbau Infrastruktur":      ["Weinbau", "Obstbau", "Weingut", "Mosterei", "Kellerei"],
    "Fischzucht / Aquakultur":              ["Fischzucht", "Aquakultur", "Fischteich", "Fischerei"],

    # ── INDUSTRIE & GEWERBE ──
    "Industriehallen / Werkshallen":        ["Industriehalle", "Werkshalle", "Produktionshalle", "Fabrik"],
    "Gewerbeparks / Betriebsanlagen":       ["Gewerbepark", "Betriebsanlage", "Betriebsgebäude", "Gewerbezone"],
    "Produktionsanlagen":                   ["Produktionsanlage", "Fertigungsanlage", "Produktionsstätte"],
    "Lagerhallen / Logistikzentren":        ["Lagerhalle", "Logistikzentrum", "Logistikhalle", "Distributionszentrum"],
    "Reinräume / Labore":                   ["Reinraum", "Labor", "Laborgebäude", "Reinraumanlage"],
    "Tankstellen / Waschanlagen":           ["Tankstelle", "Waschanlage", "Tankstellenbau", "Autopflegeanlage"],
    "Kälteanlagen / Kühlhäuser":            ["Kälteanlage", "Kühlhaus", "Tiefkühlanlage", "Kältetechnik"],
    "Fördertechnik / Förderanlagen":        ["Fördertechnik", "Förderanlage", "Förderband", "Materialfluss"],
}

# Allgemeine Keywords (immer dabei – unabhängig von Branche)
BASIS_KEYWORDS = ["Ausschreibung", "Vergabe", "Baubewilligung", "Projekt", "Vorhaben", "Planung", "Beschluss"]

def baue_suchbegriffe(auftrag: dict) -> list[str]:
    """
    Erstellt eine Liste von Suchbegriffen basierend auf den gewählten Gewerken
    und optionalen Zusatz-Keywords des Kunden.
    """
    begriffe = list(BASIS_KEYWORDS)

    gewerke = auftrag.get("gewerke") or []
    if isinstance(gewerke, str):
        try:
            gewerke = json.loads(gewerke)
        except Exception:
            gewerke = [gewerke]

    for gewerk in gewerke:
        if gewerk in GEWERK_KEYWORDS:
            begriffe.extend(GEWERK_KEYWORDS[gewerk])

    # Zusatz-Keywords des Kunden (freies Textfeld)
    zusatz = auftrag.get("zusatz_keywords") or ""
    if zusatz:
        for kw in re.split(r"[,;\n]+", zusatz):
            kw = kw.strip()
            if kw:
                begriffe.append(kw)

    # Duplikate entfernen, Reihenfolge behalten
    gesehen = set()
    ergebnis = []
    for b in begriffe:
        if b.lower() not in gesehen:
            gesehen.add(b.lower())
            ergebnis.append(b)

    print(f"  🔍 Suchbegriffe ({len(ergebnis)}): {', '.join(ergebnis[:8])}{'...' if len(ergebnis) > 8 else ''}")
    return ergebnis

# =============================================================================
# SCHRITT 3: QUELLEN FILTERN
# =============================================================================

def waehle_quellen(auftrag: dict) -> list[dict]:
    """
    Filtert Medienquellen nach den gewählten Bundesländern.
    Wenn ganz_oesterreich=True, alle Quellen.
    """
    if auftrag.get("ganz_oesterreich"):
        bundeslaender = get_alle_bundeslaender_kuerzel()
        print(f"  🗺️  Ganz Österreich – alle Quellen")
    else:
        bundeslaender = auftrag.get("bundeslaender") or []
        if isinstance(bundeslaender, str):
            try:
                bundeslaender = json.loads(bundeslaender)
            except Exception:
                bundeslaender = [bundeslaender]

    quellen = get_quellen_fuer_bundeslaender(bundeslaender)
    print(f"  📰 {len(quellen)} Medienquellen für Bundesländer: {bundeslaender}")
    return quellen

# =============================================================================
# SCHRITT 4: ARTIKEL CRAWLEN
# =============================================================================

def berechne_hash(text: str) -> str:
    """SHA256-Hash eines Textes – für Duplikat-Erkennung."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

def crawle_suchergebnis(quelle: dict, suchbegriffe: list[str]) -> list[dict]:
    """
    Öffnet die Suchseite einer Quelle und extrahiert Artikel-Links + Titel.
    Gibt Liste von {"titel": ..., "url": ..., "snippet": ...} zurück.
    """
    artikel = []
    # Ersten relevanten Suchbegriff nehmen (meistens reicht einer pro Quelle)
    # Wir suchen nach dem ersten Nicht-Basis-Keyword, sonst Basis
    suchbegriff = suchbegriffe[0] if suchbegriffe else "Bauprojekt"

    suchpfad = quelle.get("suchpfad", "")
    if not suchpfad:
        return []

    url = suchpfad + quote_plus(suchbegriff)

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; BauScout/1.0; +https://romanbaumschlager-netizen.github.io/bauscout)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "de-AT,de;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)

        if resp.status_code != 200:
            print(f"    ⚠️  {quelle['name']}: HTTP {resp.status_code}")
            return []

        html = resp.text

        # Einfache Link-Extraktion: alle <a href="...">Titel</a>
        # Sucht nach Links die nach Artikeln aussehen (enthalten Jahreszahl oder /artikel/ etc.)
        muster = re.compile(
            r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{10,120})</a>',
            re.IGNORECASE | re.DOTALL
        )
        basis_url = f"{urlparse(resp.url).scheme}://{urlparse(resp.url).netloc}"

        gefundene_urls = set()
        for match in muster.finditer(html):
            href, title = match.group(1).strip(), match.group(2).strip()
            title = re.sub(r'\s+', ' ', title).strip()

            # Relative URLs zu absoluten machen
            if href.startswith("/"):
                href = basis_url + href
            elif not href.startswith("http"):
                continue

            # Nur Links von derselben Domain
            if urlparse(href).netloc != urlparse(basis_url).netloc:
                continue

            # Offensichtliche Nicht-Artikel herausfiltern
            skip_patterns = ["/impressum", "/kontakt", "/datenschutz", "/agb",
                             "/login", "/register", "/suche", "/search", "#",
                             "javascript:", "mailto:"]
            if any(p in href.lower() for p in skip_patterns):
                continue

            if href in gefundene_urls:
                continue
            gefundene_urls.add(href)

            artikel.append({
                "titel": title,
                "url":   href,
                "quelle_name": quelle["name"],
            })

            if len(artikel) >= MAX_ARTIKEL_PRO_QUELLE:
                break

    except requests.exceptions.Timeout:
        print(f"    ⏱️  {quelle['name']}: Timeout")
    except Exception as e:
        print(f"    ❌ {quelle['name']}: {e}")

    return artikel

def lade_artikel_text(artikel_url: str) -> str:
    """
    Lädt den Volltext eines Artikels und extrahiert den Textinhalt.
    Gibt maximal 3000 Zeichen zurück (genug für KI-Analyse).
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; BauScout/1.0)",
            "Accept": "text/html",
            "Accept-Language": "de-AT,de;q=0.9",
        }
        resp = requests.get(artikel_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return ""

        html = resp.text

        # HTML-Tags entfernen
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>',  ' ', text,  flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&[a-zA-Z]+;', ' ', text)   # HTML-Entities
        text = re.sub(r'\s+', ' ', text).strip()

        return text[:3000]
    except Exception:
        return ""

# =============================================================================
# SCHRITT 5: KI-ANALYSE MIT CLAUDE HAIKU
# =============================================================================

ANALYSE_SYSTEM_PROMPT = """Du bist ein Assistent für ein österreichisches Bauunternehmen.
Deine Aufgabe: Analysiere Nachrichtenartikel und entscheide ob sie ein relevantes Bauprojekt beschreiben.

Antworte NUR mit einem JSON-Objekt. Kein Text davor oder danach.

Format:
{
  "ist_bauprojekt": true/false,
  "relevanz": 0-10,
  "titel": "Kurzer prägnanter Projekttitel",
  "ort": "Gemeinde oder Stadt",
  "bezirk": "Bezirksname oder leer",
  "bundesland": "Bundesland-Kürzel (W/NOE/OOE/SBG/STK/KTN/TIR/VBG/BGR)",
  "kategorie": "Hochbau/Tiefbau/Straßenbau/Kanal/Elektro/Dach/Fassade/Innenausbau/Sonstiges",
  "volumen": "Geschätztes Bauvolumen in Euro oder leer",
  "phase": "Planung/Ausschreibung/Vergabe/Bau/Fertigstellung",
  "beschreibung": "2-3 Sätze Zusammenfassung des Projekts"
}

Ein Artikel ist relevant (ist_bauprojekt=true) wenn er über:
- Neubauprojekte, Sanierungen, Infrastrukturprojekte berichtet
- Ausschreibungen oder Vergaben für Bauleistungen enthält
- Baubewilligungen oder Gemeinderatsbeschlüsse für Bauvorhaben beschreibt

Nicht relevant: Unfälle, Personalberichte, politische Meinungen, reine Immobilienpreisartikel"""

def analysiere_artikel_mit_ki(artikel: dict, suchbegriffe: list[str]) -> dict | None:
    """
    Sendet Artikel-Text an Claude Haiku zur Analyse.
    Gibt strukturiertes Ergebnis zurück oder None wenn nicht relevant.
    """
    volltext = lade_artikel_text(artikel["url"])
    if not volltext:
        volltext = artikel.get("titel", "")

    user_prompt = f"""Analysiere diesen Artikel auf Bauprojekt-Relevanz.

Gesuchte Gewerke/Themen: {', '.join(suchbegriffe[:5])}

Quelle: {artikel['quelle_name']}
URL: {artikel['url']}
Titel: {artikel['titel']}

Artikeltext (Auszug):
{volltext}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5",
                "max_tokens": 500,
                "system":     ANALYSE_SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": user_prompt}
                ],
            },
            timeout=30,
        )

        if resp.status_code != 200:
            print(f"    ⚠️  Anthropic API Fehler: {resp.status_code}")
            return None

        antwort_text = resp.json()["content"][0]["text"].strip()

        # JSON parsen (manchmal kommt es in ```json ... ``` verpackt)
        antwort_text = re.sub(r'^```json\s*', '', antwort_text)
        antwort_text = re.sub(r'\s*```$', '', antwort_text)

        ergebnis = json.loads(antwort_text)
        return ergebnis

    except json.JSONDecodeError as e:
        print(f"    ⚠️  JSON-Parse-Fehler: {e}")
        return None
    except Exception as e:
        print(f"    ❌ KI-Analyse Fehler: {e}")
        return None

# =============================================================================
# SCHRITT 6: PROJEKTE IN SUPABASE SPEICHERN
# =============================================================================

def speichere_projekt(analyse: dict, artikel: dict, auftrag: dict) -> bool:
    """
    Speichert ein gefundenes Projekt in der Supabase-Tabelle 'projekte'.
    Prüft vorher ob das Projekt schon bekannt ist (via URL-Hash).
    Gibt True zurück wenn neu gespeichert, False wenn bereits vorhanden.
    """
    # Duplikat-Check via URL-Hash
    url_hash = berechne_hash(artikel["url"])

    vorhandene = sb_get("projekte", {
        "rohdaten_hash": f"eq.{url_hash}",
        "kunden_id":     f"eq.{auftrag['kunden_id']}",
    })

    if vorhandene:
        # Bereits bekannt – nur "zuletzt_gecrawlt" aktualisieren
        sb_patch("projekte",
                 {"rohdaten_hash": f"eq.{url_hash}", "kunden_id": f"eq.{auftrag['kunden_id']}"},
                 {"zuletzt_gecrawlt": datetime.now(timezone.utc).isoformat()})
        return False

    # Neu → speichern
    jetzt = datetime.now(timezone.utc).isoformat()
    projekt = {
        "kunden_id":       auftrag["kunden_id"],
        "suchanfrage_id":  auftrag["id"],
        "titel":           analyse.get("titel") or artikel["titel"][:200],
        "ort":             analyse.get("ort", ""),
        "bezirk":          analyse.get("bezirk", ""),
        "bundesland":      analyse.get("bundesland", ""),
        "kategorie":       analyse.get("kategorie", "Sonstiges"),
        "volumen":         analyse.get("volumen", ""),
        "phase":           analyse.get("phase", ""),
        "quelle":          artikel["quelle_name"],
        "artikel_url":     artikel["url"],
        "beschreibung":    analyse.get("beschreibung", ""),
        "relevanz":        analyse.get("relevanz", 5),
        "ignorieren":      False,
        "ist_oeffentlich": False,
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
# =============================================================================

def erstelle_email_html(kunde: dict, auftrag: dict, projekte_liste: list[dict]) -> str:
    """Erstellt HTML-E-Mail mit Projektzusammenfassung."""
    anzahl = len(projekte_liste)
    dashboard_url = f"{DASHBOARD_BASE_URL}?kunden_id={auftrag['kunden_id']}&suchanfrage_id={auftrag['id']}"

    # Top-Projekte für E-Mail (max. 5, sortiert nach Relevanz)
    top_projekte = sorted(projekte_liste, key=lambda p: p.get("relevanz", 0), reverse=True)[:5]

    projekt_html = ""
    for p in top_projekte:
        relevanz_sterne = "★" * min(int(int(p.get("relevanz", 5)) / 2), 5)
        projekt_html += f"""
        <div style="background:#1a1a2e;border:1px solid #d4a017;border-radius:6px;padding:16px;margin-bottom:12px;">
          <div style="font-size:11px;color:#d4a017;font-family:monospace;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
            {p.get('kategorie','Sonstiges')} · {p.get('phase','unbekannt')} · {relevanz_sterne}
          </div>
          <div style="font-size:16px;font-weight:600;color:#f0f0f0;margin-bottom:6px;">{p.get('titel','')}</div>
          <div style="font-size:13px;color:#8b949e;margin-bottom:8px;">
            📍 {p.get('ort','')} {('· ' + p.get('bezirk','')) if p.get('bezirk') else ''} · {p.get('bundesland','')}
            {(' · 💶 ' + p.get('volumen','')) if p.get('volumen') else ''}
          </div>
          <div style="font-size:13px;color:#c9d1d9;margin-bottom:10px;">{p.get('beschreibung','')}</div>
          <a href="{p.get('artikel_url','#')}" style="color:#d4a017;font-size:12px;">→ Zum Artikel</a>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="background:#0a0a0a;color:#e6edf3;font-family:'DM Sans',Arial,sans-serif;max-width:600px;margin:0 auto;padding:32px 16px;">

  <div style="text-align:center;margin-bottom:32px;">
    <div style="font-size:32px;font-weight:900;letter-spacing:4px;color:#d4a017;">BAUSCOUT</div>
    <div style="font-size:13px;color:#8b949e;margin-top:4px;">KI-Bauprojekt-Scout für Österreich</div>
  </div>

  <div style="background:#161b22;border:1px solid #238636;border-radius:8px;padding:24px;margin-bottom:24px;text-align:center;">
    <div style="font-size:40px;font-weight:900;color:#d4a017;">{anzahl}</div>
    <div style="font-size:16px;color:#e6edf3;margin-top:4px;">Relevante Bauprojekte gefunden</div>
    <div style="font-size:13px;color:#8b949e;margin-top:8px;">für {kunde.get('firmenname','Ihr Unternehmen')}</div>
  </div>

  <p style="color:#8b949e;font-size:14px;margin-bottom:20px;">
    Ihr BauScout-Lauf ist abgeschlossen. Hier sind die {min(anzahl, 5)} relevantesten Projekte:
  </p>

  {projekt_html}

  <div style="text-align:center;margin:32px 0;">
    <a href="{dashboard_url}"
       style="background:#d4a017;color:#0a0a0a;font-weight:700;font-size:15px;padding:14px 32px;border-radius:4px;text-decoration:none;display:inline-block;">
      → Alle {anzahl} Projekte im Dashboard ansehen
    </a>
  </div>

  <div style="background:#161b22;border-radius:6px;padding:16px;font-size:12px;color:#8b949e;margin-top:24px;">
    <strong style="color:#e6edf3;">Excel-Export</strong> steht im Dashboard zum Download bereit.<br><br>
    Sie haben Fragen? Antworten Sie auf diese E-Mail.<br>
    <a href="https://romanbaumschlager-netizen.github.io/bauscout/" style="color:#d4a017;">bauscout.at</a>
  </div>

</body>
</html>"""

def sende_email(kunde: dict, auftrag: dict, projekte_liste: list[dict]) -> bool:
    """Versendet die Ergebnis-E-Mail an den Kunden."""
    if not SMTP_USER or not SMTP_PASS:
        print("  ⚠️  SMTP nicht konfiguriert – E-Mail übersprungen")
        return False

    empfaenger = kunde.get("email")
    if not empfaenger:
        print("  ⚠️  Keine Kunden-E-Mail-Adresse vorhanden")
        return False

    anzahl = len(projekte_liste)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"BauScout: {anzahl} Bauprojekte gefunden – {kunde.get('firmenname','')}"
    msg["From"]    = f"BauScout <{SMTP_USER}>"
    msg["To"]      = empfaenger

    # Text-Fallback
    text_body = f"""BauScout – Ihre Ergebnisse sind da!

{anzahl} relevante Bauprojekte wurden gefunden.

Dashboard: {DASHBOARD_BASE_URL}?kunden_id={auftrag['kunden_id']}&suchanfrage_id={auftrag['id']}

BauScout – KI-Bauprojekt-Scout für Österreich"""

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
# HAUPTFUNKTION: EINEN AUFTRAG ABARBEITEN
# =============================================================================

def verarbeite_auftrag(auftrag: dict) -> None:
    """Vollständige Verarbeitung eines bezahlten Auftrags."""
    sid = auftrag["id"]
    print(f"\n{'='*60}")
    print(f"🚀 Starte Auftrag: {sid}")
    print(f"   Gewerke:      {auftrag.get('gewerke')}")
    print(f"   Bundesländer: {auftrag.get('bundeslaender')}")
    print(f"   Zeitraum:     {auftrag.get('zeitraum_tage', 30)} Tage")
    print(f"{'='*60}")

    # Status → agent_laeuft
    sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {"status": "agent_laeuft"})

    try:
        # Kundendaten laden
        kunde = lade_kundendaten(auftrag["kunden_id"])
        if not kunde:
            raise ValueError(f"Keine Kundendaten für ID {auftrag['kunden_id']} gefunden")
        print(f"  👤 Kunde: {kunde.get('firmenname')} ({kunde.get('email')})")

        # Suchbegriffe + Quellen bestimmen
        suchbegriffe = baue_suchbegriffe(auftrag)
        quellen      = waehle_quellen(auftrag)

        # Crawling + KI-Analyse
        neue_projekte   = []
        gesamt_artikel  = 0
        gesamt_relevant = 0

        for i, quelle in enumerate(quellen, 1):
            print(f"\n  [{i:3}/{len(quellen)}] {quelle['name']}")
            time.sleep(PAUSE_ZWISCHEN_QUELLEN)

            artikel_liste = crawle_suchergebnis(quelle, suchbegriffe)
            if not artikel_liste:
                print(f"         → Keine Artikel gefunden")
                continue

            print(f"         → {len(artikel_liste)} Artikel gefunden")
            gesamt_artikel += len(artikel_liste)

            for artikel in artikel_liste:
                analyse = analysiere_artikel_mit_ki(artikel, suchbegriffe)
                if not analyse:
                    continue

                if not analyse.get("ist_bauprojekt"):
                    continue

                relevanz = analyse.get("relevanz", 0)
                if relevanz < 4:
                    continue

                print(f"         ✅ RELEVANT (Relevanz {relevanz}/10): {analyse.get('titel','')[:60]}")
                gesamt_relevant += 1

                ist_neu = speichere_projekt(analyse, artikel, auftrag)
                if ist_neu:
                    neue_projekte.append(analyse)

        print(f"\n  📊 Zusammenfassung:")
        print(f"     Artikel analysiert:  {gesamt_artikel}")
        print(f"     Relevante gefunden:  {gesamt_relevant}")
        print(f"     Neu gespeichert:     {len(neue_projekte)}")

        # Alle Projekte dieses Auftrags für E-Mail laden
        alle_projekte = sb_get("projekte", {
            "suchanfrage_id": f"eq.{sid}",
            "ignorieren":     "eq.false",
            "order":          "relevanz.desc.nullslast",
        })

        # Status → abgeschlossen
        sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {
            "status":               "abgeschlossen",
            "kosten_tatsaechlich":  berechne_tatsaechliche_kosten(gesamt_artikel),
        })

        # E-Mail versenden
        if alle_projekte:
            sende_email(kunde, auftrag, alle_projekte)
        else:
            print("  ℹ️  Keine relevanten Projekte – E-Mail mit 0-Ergebnis-Hinweis")
            sende_email(kunde, auftrag, [])

        print(f"\n  ✅ Auftrag {sid} abgeschlossen – {len(alle_projekte)} Projekte geliefert")

    except Exception as e:
        print(f"\n  ❌ FEHLER bei Auftrag {sid}: {e}")
        import traceback
        traceback.print_exc()
        # Status → fehler
        sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {
            "status": "fehler",
        })

def berechne_tatsaechliche_kosten(anzahl_artikel: int) -> float:
    """
    Grobe Kostenschätzung basierend auf API-Nutzung.
    Haiku: $1/$5 per MTok Input/Output
    ~500 Token Input + 200 Token Output pro Artikel
    """
    input_token  = anzahl_artikel * 500
    output_token = anzahl_artikel * 200
    kosten_usd   = (input_token / 1_000_000 * 1.0) + (output_token / 1_000_000 * 5.0)
    kosten_eur   = round(kosten_usd * 0.92, 4)
    return kosten_eur

# =============================================================================
# EINSTIEGSPUNKT
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🏗️  BauScout Agent – Start")
    print(f"   Zeitpunkt: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Optionale spezifische Auftrag-ID via Umgebungsvariable
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
