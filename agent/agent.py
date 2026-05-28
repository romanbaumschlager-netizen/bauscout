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
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse, quote_plus

sys.path.insert(0, os.path.dirname(__file__))
from medien_datenbank import get_quellen_fuer_bundeslaender, get_alle_bundeslaender_kuerzel
from gemeinden_datenbank import get_gemeinden_fuer_bundeslaender, PROTOKOLL_PFADE

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

MAX_ARTIKEL_PRO_QUELLE = 15
REQUEST_TIMEOUT        = 15
PAUSE_ZWISCHEN_QUELLEN = 1.0

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
# SCHRITT 3: QUELLEN FILTERN
# =============================================================================

def waehle_quellen(auftrag: dict) -> list[dict]:
    if auftrag.get("ganz_oesterreich"):
        bundeslaender = get_alle_bundeslaender_kuerzel()
        print(f"  🗺️  Ganz Österreich – alle Quellen")
    else:
        bundeslaender = auftrag.get("bundeslaender") or []
        if isinstance(bundeslaender, str):
            try: bundeslaender = json.loads(bundeslaender)
            except Exception: bundeslaender = [bundeslaender]
    quellen = get_quellen_fuer_bundeslaender(bundeslaender)
    print(f"  📰 {len(quellen)} Medienquellen für Bundesländer: {bundeslaender}")
    return quellen

# =============================================================================
# SCHRITT 4: ARTIKEL CRAWLEN
# =============================================================================

def berechne_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

def _extrahiere_links(html: str, basis_url: str, gefundene_urls: set, quelle_name: str, limit: int) -> list:
    muster = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]{12,150})</a>', re.IGNORECASE | re.DOTALL)
    skip_patterns = ["/impressum", "/kontakt", "/datenschutz", "/agb", "/login", "/register",
                     "/suche", "/search", "#", "javascript:", "mailto:", "/kategorie", "/tag/",
                     "/autor/", "/author/", "/feed", ".xml", ".rss"]
    neu = []
    for match in muster.finditer(html):
        href, title = match.group(1).strip(), match.group(2).strip()
        title = re.sub(r'\s+', ' ', title).strip()
        if len(title) < 12: continue
        if href.startswith("/"): href = basis_url + href
        elif not href.startswith("http"): continue
        if urlparse(href).netloc != urlparse(basis_url).netloc: continue
        if any(p in href.lower() for p in skip_patterns): continue
        if href in gefundene_urls: continue
        gefundene_urls.add(href)
        neu.append({"titel": title, "url": href, "quelle_name": quelle_name})
        if len(neu) >= limit: break
    return neu

def crawle_suchergebnis(quelle: dict, suchbegriffe: list) -> list:
    suchpfad = quelle.get("suchpfad", "")
    if not suchpfad: return []
    basis = {"ausschreibung", "vergabe", "baubewilligung", "projekt", "vorhaben", "planung", "beschluss"}
    spezifisch = [b for b in suchbegriffe if b.lower() not in basis]
    top_keywords = spezifisch[:3] if spezifisch else suchbegriffe[:3]
    if not top_keywords: top_keywords = ["Bauprojekt"]
    headers_req = {
        "User-Agent": "Mozilla/5.0 (compatible; ProjectScout/1.0; +https://project-scout.at)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "de-AT,de;q=0.9",
    }
    alle_artikel = []
    gefundene_urls: set = set()
    limit_pro_kw = max(5, MAX_ARTIKEL_PRO_QUELLE // len(top_keywords))
    for keyword in top_keywords:
        if len(alle_artikel) >= MAX_ARTIKEL_PRO_QUELLE: break
        url = suchpfad + quote_plus(keyword)
        try:
            resp = requests.get(url, headers=headers_req, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if resp.status_code != 200:
                print(f"    ⚠️  {quelle['name']} [{keyword}]: HTTP {resp.status_code}")
                continue
            basis_url = f"{urlparse(resp.url).scheme}://{urlparse(resp.url).netloc}"
            neu = _extrahiere_links(resp.text, basis_url, gefundene_urls, quelle["name"], limit_pro_kw)
            alle_artikel.extend(neu)
            if len(top_keywords) > 1: time.sleep(0.4)
        except requests.exceptions.Timeout:
            print(f"    ⏱️  {quelle['name']} [{keyword}]: Timeout")
        except Exception as e:
            print(f"    ❌ {quelle['name']} [{keyword}]: {e}")
    return alle_artikel[:MAX_ARTIKEL_PRO_QUELLE]

def crawle_gemeinde_protokolle(gemeinde: dict, suchbegriffe: list) -> list:
    base = gemeinde["url"].rstrip("/")
    name = gemeinde["name"]
    headers_req = {
        "User-Agent": "Mozilla/5.0 (compatible; ProjectScout/1.0; +https://project-scout.at)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "de-AT,de;q=0.9",
    }
    for pfad in PROTOKOLL_PFADE:
        url = base + pfad
        try:
            resp = requests.get(url, headers=headers_req, timeout=6, allow_redirects=True)
            if resp.status_code != 200: continue
            text_lower = resp.text.lower()
            if not any(w in text_lower for w in ["protokoll", "sitzung", "beschluss", "tagesordnung", "niederschrift"]):
                continue
            basis_url = f"{urlparse(resp.url).scheme}://{urlparse(resp.url).netloc}"
            artikel = []
            gefundene_urls: set = set()
            pdf_muster = re.compile(r'href=["\']([^"\']*\.pdf)["\']\s', re.IGNORECASE)
            for match in pdf_muster.finditer(resp.text):
                href = match.group(1)
                if href.startswith("/"): href = basis_url + href
                elif not href.startswith("http"): continue
                if href in gefundene_urls: continue
                gefundene_urls.add(href)
                artikel.append({"titel": f"Gemeinderatsprotokoll {name}", "url": href, "quelle_name": f"Gemeinde {name}"})
                if len(artikel) >= 5: break
            if not artikel:
                artikel = _extrahiere_links(resp.text, basis_url, gefundene_urls, f"Gemeinde {name}", 5)
            if artikel:
                print(f"    {name}: {len(artikel)} Protokoll(e)")
            return artikel
        except Exception:
            continue
    return []

def crawle_alle_gemeinden(bundeslaender: list, suchbegriffe: list) -> list:
    gemeinden = get_gemeinden_fuer_bundeslaender(bundeslaender)
    print(f"  [{len(gemeinden)} Gemeinden werden auf Protokolle durchsucht...]")
    alle_artikel = []
    for i, gemeinde in enumerate(gemeinden):
        artikel = crawle_gemeinde_protokolle(gemeinde, suchbegriffe)
        alle_artikel.extend(artikel)
        if i > 0 and i % 10 == 0: time.sleep(1)
    print(f"  [{len(gemeinden)} Gemeinden durchsucht | {len(alle_artikel)} Protokoll-Links]")
    return alle_artikel

def lade_artikel_text(artikel_url: str) -> str:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; ProjectScout/1.0; +https://project-scout.at)",
            "Accept": "text/html",
            "Accept-Language": "de-AT,de;q=0.9",
        }
        resp = requests.get(artikel_url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200: return ""
        html = resp.text
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>',  ' ', text,  flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&[a-zA-Z]+;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:4500]
    except Exception:
        return ""

# =============================================================================
# SCHRITT 5: KI-ANALYSE MIT CLAUDE HAIKU
# =============================================================================

ANALYSE_SYSTEM_PROMPT = """Du bist ein Analyse-Assistent für ProjectScout, eine österreichische Plattform die Projekte, Ausschreibungen, Grundstücke und Vorhaben für Unternehmen aus allen Branchen findet.

Deine Aufgabe: Analysiere Nachrichtenartikel und entscheide ob sie ein relevantes Projekt, Vorhaben oder eine Ausschreibung beschreiben.

Antworte NUR mit einem JSON-Objekt. Absolut kein Text davor oder danach. Kein Markdown.

Format:
{
  "ist_bauprojekt": true/false,
  "relevanz": 0-10,
  "titel": "Kurzer prägnanter Projekttitel",
  "ort": "Gemeinde oder Stadt",
  "bezirk": "Bezirksname oder leer",
  "bundesland": "Bundesland-Kürzel (W/NOE/OOE/SBG/STK/KTN/TIR/VBG/BGR)",
  "kategorie": "Hochbau/Tiefbau/Energie/Infrastruktur/Immobilien/Öffentlich/Landschaft/Gastronomie/Planung/Landwirtschaft/Industrie/Fahrzeuge/Sonstiges",
  "volumen": "Geschätztes Projektvolumen in Euro oder leer",
  "phase": "Planung/Ausschreibung/Vergabe/Bau/Fertigstellung/Betrieb",
  "beschreibung": "2-3 präzise Sätze: Was wird gebaut/gemacht, wo, wann, wer ist Auftraggeber?"
}

Relevanz-Skala:
- 9-10: Konkrete Ausschreibung oder Vergabe mit Auftragssumme, direkt umsetzbar
- 7-8:  Beschlossenes Projekt mit konkreten Details (Ort, Zeitplan, Volumen)
- 5-6:  Geplantes Vorhaben mit ersten konkreten Angaben
- 3-4:  Allgemeiner Hinweis auf mögliches Projekt, wenig Details
- 1-2:  Nur vage Erwähnung, kein konkretes Projekt erkennbar
- 0:    Kein Projekt (Unfall, Meinung, reine Statistik, Personalthema)

ist_bauprojekt=true wenn der Artikel über EINES dieser Themen berichtet:
- Bau-, Sanierungs-, Infrastruktur- oder Energieprojekte
- Ausschreibungen oder Vergaben
- Baubewilligungen, Gemeinderatsbeschlüsse, Widmungen
- Grundstücksverkäufe oder -entwicklungen mit Bauabsicht
- Förderprojekte mit konkretem Investitionsvorhaben
- Neue Betriebe, Betriebserweiterungen
- Öffentliche Projekte: Schulen, Kindergärten, Straßen, Kanal, Wasser
- Energieprojekte: PV, Windkraft, Wärmepumpen, Nahwärme
- Tourismus/Gastronomie: Hotelneubauten, Umbau, Konzessionen

ist_bauprojekt=false: Unfallberichte, politische Meinungsartikel ohne Projekt, reine Preisstatistiken, Personalberichte, Veranstaltungen.

Berücksichtige die gesuchten Gewerke bei der Relevanz-Bewertung. Nicht gesuchte Gewerke → Relevanz max. 3."""

def analysiere_artikel_mit_ki(artikel: dict, suchbegriffe: list[str]) -> dict | None:
    volltext = lade_artikel_text(artikel["url"])
    if not volltext: volltext = artikel.get("titel", "")
    basis = {"ausschreibung", "vergabe", "baubewilligung", "projekt", "vorhaben", "planung", "beschluss"}
    top_begriffe = [b for b in suchbegriffe if b.lower() not in basis][:10] or suchbegriffe[:5]
    user_prompt = f"""Analysiere diesen Artikel. Bewerte die Relevanz für die gesuchten Themen.

Gesuchte Gewerke/Themen des Kunden: {', '.join(top_begriffe)}

Quelle: {artikel['quelle_name']}
URL: {artikel['url']}
Titel: {artikel['titel']}

Artikeltext:
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
                "messages":   [{"role": "user", "content": user_prompt}],
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"    ⚠️  Anthropic API Fehler: {resp.status_code}")
            return None
        antwort_text = resp.json()["content"][0]["text"].strip()
        antwort_text = re.sub(r'^```json\s*', '', antwort_text)
        antwort_text = re.sub(r'\s*```$', '', antwort_text)
        return json.loads(antwort_text)
    except json.JSONDecodeError as e:
        print(f"    ⚠️  JSON-Parse-Fehler: {e}")
        return None
    except Exception as e:
        print(f"    ❌ KI-Analyse Fehler: {e}")
        return None

# =============================================================================
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
    <div style="font-size:32px;font-weight:900;letter-spacing:4px;color:#d4a017;">ProjectScout</div>
    <div style="font-size:13px;color:#8b949e;margin-top:4px;">KI-Projekt-Scout für Österreich</div>
  </div>

  <div style="background:#161b22;border:1px solid #238636;border-radius:8px;padding:24px;margin-bottom:24px;text-align:center;">
    <div style="font-size:40px;font-weight:900;color:#d4a017;">{anzahl}</div>
    <div style="font-size:16px;color:#e6edf3;margin-top:4px;">Neue Projekte gefunden</div>
    <div style="font-size:13px;color:#8b949e;margin-top:8px;">für {kunde.get('firmenname','Ihr Unternehmen')}</div>
  </div>

  <p style="color:#8b949e;font-size:14px;margin-bottom:20px;">
    Ihr Scout-Lauf ist abgeschlossen. Hier sind die {min(anzahl,5)} relevantesten neuen Projekte:
  </p>

  {projekt_html}

  <div style="text-align:center;margin:32px 0;">
    <a href="{dashboard_url}"
       style="background:#d4a017;color:#0a0a0a;font-weight:700;font-size:15px;padding:14px 32px;border-radius:4px;text-decoration:none;display:inline-block;">
      → Alle {anzahl} Projekte im Dashboard ansehen
    </a>
  </div>

  <div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:16px;font-size:12px;color:#8b949e;margin-top:24px;">
    <strong style="color:#e6edf3;">🔖 Ihr persönlicher Dashboard-Link:</strong><br>
    <a href="{dashboard_url}" style="color:#d4a017;word-break:break-all;">{dashboard_url}</a><br><br>
    Speichern Sie diesen Link als Favorit – er bleibt bei allen zukünftigen Scout-Läufen gleich.<br><br>
    Excel-Export steht im Dashboard zum Download bereit.<br>
    Fragen? Antworten Sie auf diese E-Mail.<br>
    <a href="https://project-scout.at/" style="color:#d4a017;">project-scout.at</a>
  </div>

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

    try:
        kunde = lade_kundendaten(auftrag["kunden_id"])
        if not kunde:
            raise ValueError(f"Keine Kundendaten für ID {auftrag['kunden_id']} gefunden")
        print(f"  👤 Kunde: {kunde.get('firmenname')} ({kunde.get('email')})")

        suchbegriffe = baue_suchbegriffe(auftrag)
        quellen      = waehle_quellen(auftrag)

        # Bundesländer für Gemeinde-Crawling bestimmen
        if auftrag.get("ganz_oesterreich"):
            bundeslaender = get_alle_bundeslaender_kuerzel()
        else:
            bundeslaender = auftrag.get("bundeslaender") or []
            if isinstance(bundeslaender, str):
                try: bundeslaender = json.loads(bundeslaender)
                except Exception: bundeslaender = [bundeslaender]

        neue_projekte   = []
        gesamt_artikel  = 0
        gesamt_relevant = 0

        # ── MEDIENQUELLEN CRAWLEN ──
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
                if not analyse or not analyse.get("ist_bauprojekt"): continue
                relevanz = analyse.get("relevanz", 0)
                if relevanz < 4: continue
                print(f"         ✅ RELEVANT ({relevanz}/10): {analyse.get('titel','')[:60]}")
                gesamt_relevant += 1
                ist_neu = speichere_projekt(analyse, artikel, auftrag)
                if ist_neu: neue_projekte.append(analyse)

        # ── GEMEINDERATSPROTOKOLLE CRAWLEN ──
        print(f"\n{'='*60}")
        print(f"  🏘️  GEMEINDERATSPROTOKOLLE")
        print(f"{'='*60}")
        gemeinde_artikel = crawle_alle_gemeinden(bundeslaender, suchbegriffe)
        gesamt_artikel += len(gemeinde_artikel)
        for artikel in gemeinde_artikel:
            analyse = analysiere_artikel_mit_ki(artikel, suchbegriffe)
            if not analyse or not analyse.get("ist_bauprojekt"): continue
            relevanz = analyse.get("relevanz", 0)
            if relevanz < 3: continue
            print(f"         ✅ PROTOKOLL ({relevanz}/10): {analyse.get('titel','')[:60]}")
            gesamt_relevant += 1
            ist_neu = speichere_projekt(analyse, artikel, auftrag)
            if ist_neu: neue_projekte.append(analyse)

        print(f"\n  📊 Zusammenfassung:")
        print(f"     Artikel analysiert:  {gesamt_artikel}")
        print(f"     Relevante gefunden:  {gesamt_relevant}")
        print(f"     Neu gespeichert:     {len(neue_projekte)}")

        # Alle Projekte dieses Kunden für E-Mail laden (nicht nur dieser Lauf)
        alle_projekte_kunde = sb_get("projekte", {
            "kunden_id":  f"eq.{auftrag['kunden_id']}",
            "ignorieren": "eq.false",
            "order":      "relevanz.desc.nullslast",
        })

        sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {
            "status":              "abgeschlossen",
            "kosten_tatsaechlich": berechne_tatsaechliche_kosten(gesamt_artikel),
        })

        # E-Mail mit den NEUEN Projekten dieses Laufs (nicht alle)
        email_projekte = neue_projekte if neue_projekte else alle_projekte_kunde[:10]
        sende_email(kunde, auftrag, email_projekte)

        # Admin-Benachrichtigung
        dauer = time.time() - start_zeit
        sende_admin_benachrichtigung(
            kunde=kunde,
            auftrag=auftrag,
            anzahl_neue=len(neue_projekte),
            anzahl_gesamt=len(alle_projekte_kunde),
            dauer_sek=dauer,
            gesamt_artikel=gesamt_artikel,
        )

        print(f"\n  ✅ Auftrag {sid} abgeschlossen")
        print(f"     Neu in diesem Lauf: {len(neue_projekte)}")
        print(f"     Gesamt im Dashboard: {len(alle_projekte_kunde)}")

    except Exception as e:
        print(f"\n  ❌ FEHLER bei Auftrag {sid}: {e}")
        import traceback
        traceback.print_exc()
        sb_patch("suchanfragen", {"id": f"eq.{sid}"}, {"status": "fehler"})

def berechne_tatsaechliche_kosten(anzahl_artikel: int) -> float:
    input_token  = anzahl_artikel * 500
    output_token = anzahl_artikel * 200
    kosten_usd   = (input_token / 1_000_000 * 1.0) + (output_token / 1_000_000 * 5.0)
    return round(kosten_usd * 0.92, 4)

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
