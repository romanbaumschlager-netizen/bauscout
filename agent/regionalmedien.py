#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ProjectScout – Regionalmedien-Ernte (Saeule C)
==============================================
Liest die RegionalMedien-Austria-Plattform **meinbezirk.at** DIREKT aus –
Bezirk fuer Bezirk, Gemeinde fuer Gemeinde, ueber alle Rubriken – statt zu
hoffen, dass eine Suchmaschine die Artikel hochspuelt. Damit wird jeder Bezirk
gleich tief abgedeckt (Kirchdorf wie Dornbirn).

Das Modul ist **selbst-konfigurierend**: Bezirks- und Gemeinde-Adressen werden
zur Laufzeit von der Live-Seite entdeckt (keine geratene Adressliste, die
veraltet). Alles wird protokolliert, damit der erste echte Lauf zugleich der
Live-Test ist.

Ablauf:
  Bundesland-Seite  -> Bezirks-Adressen entdecken (+ Validierung)
  Bezirks-Seite     -> Gemeinde-Adressen entdecken + Rubrik-Erstabruf
  Gemeinde-Seiten   -> feingranularer Erstabruf (erreicht auch aeltere Beitraege,
                       weil pro Gemeinde wenig Volumen anfaellt)
  Artikel laden     -> Datum / Region / Rubrik aus Metadaten + Volltext
  Filter            -> nur Zeitfenster + passendes Bundesland
  ->  Liste von {url, titel, datum, datum_dt, ort, region, kategorie, text}

Die KI-Analyse (Bau-Relevanz) macht der Agent. Dieses Modul liefert nur die
bereits gefilterten Artikel-Texte. So bleibt es einzeln testbar.

Standalone-Selbsttest:
    python agent/regionalmedien.py
  -> erntet beispielhaft den Bezirk Kirchdorf und prueft, ob die beiden
     bekannten Bauprojekte (Billa-Areal, Micheldorf-Hotel) gefunden werden.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

# curl_cffi (Chrome-TLS-Impersonation) als Fallback: manche Portale - z.B. die
# meinbezirk-Artikeldetailseiten - blocken serverseitige Standard-Clients von
# Rechenzentrums-IPs (TLS-Fingerprint/Bot-Erkennung). curl_cffi ahmt einen echten
# Chrome-TLS-Fingerprint nach. Optional: ist das Paket nicht installiert, faellt
# alles sauber auf requests zurueck (keine erzwungene Abhaengigkeit).
try:
    from curl_cffi import requests as cffi_requests
    _HAS_CFFI = True
except Exception:
    cffi_requests = None
    _HAS_CFFI = False

# Sobald Impersonation EINMAL dort erfolgreich war, wo requests scheiterte, wird
# sie fuer die folgenden Abrufe bevorzugt (spart die requests-Fehlversuche).
_prefer_impersonation = False

# -----------------------------------------------------------------------------
# Konfiguration
# -----------------------------------------------------------------------------
BASIS = "https://www.meinbezirk.at"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.6",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
TIMEOUT = 20

# Bundesland-Kuerzel -> meinbezirk-Slug der Bundesland-Startseite
BL_SLUG = {
    "W":   "wien",
    "NOE": "niederoesterreich",
    "OOE": "oberoesterreich",
    "SBG": "salzburg",
    "STK": "steiermark",
    "KTN": "kaernten",
    "TIR": "tirol",
    "VBG": "vorarlberg",
    "BGR": "burgenland",
}

# Rubriken, die fuer Bauprojekte relevant sind. "" = die "Alle"-Bezirksseite.
# Bauprojekte verteilen sich bei meinbezirk ueber mehrere Rubriken (Lokales,
# Wirtschaft, Politik, Bauen) – deshalb werden ALLE gelesen, nicht nur "Bauen".
RUBRIKEN = ["", "c-lokales", "c-wirtschaft", "c-politik", "c-bauen", "c-motor"]

# Einzelne Segmente, die KEINE Bezirke sind (zum Aussortieren bei der Entdeckung)
STOP_SEGMENTE = {
    "login", "search", "newsletter", "epaper", "event", "tag", "s", "cad",
    "build", "list", "profile", "c-freizeit", "c-lokales", "c-politik",
    "c-sport", "c-wirtschaft", "c-leute", "c-bauen", "c-motor", "c-reisen",
    "c-gesundheit", "c-regionauten-community", "jobs", "impressum", "agb",
    "datenschutz", "kontakt", "ueber-uns", "oesterreich",
}

# Artikel-URL: ... /c-<rubrik>/<slug>_a<Ziffern>
RE_ARTIKEL = re.compile(r'(?:' + re.escape(BASIS) + r')?(/[^"\'\s]+?/c-[^"\'/]+/[^"\'/]+_a\d+)')
# Gemeinde-Adresse: /<Name>-<XX>  (XX = 2 Grossbuchstaben = Kfz-Bezirkskennzeichen)
RE_GEMEINDE = re.compile(r'href="(/[A-Za-zÄÖÜäöüß][^"/]*-[A-Z]{2})"')
# Bezirks-Kandidat: einzelnes Kleinbuchstaben-Segment
RE_BEZIRK_KAND = re.compile(r'href="(/[a-z][a-z0-9-]{2,40})"')


# -----------------------------------------------------------------------------
# Low-Level
# -----------------------------------------------------------------------------
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _fetch_requests(url: str, s: requests.Session, versuche: int = 2) -> str | None:
    for i in range(versuche):
        try:
            r = s.get(url, timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                return r.text
            if r.status_code in (429, 503):
                time.sleep(2 + i * 2)
        except Exception:
            time.sleep(1 + i)
    return None


def _fetch_cffi(url: str, versuche: int = 2) -> str | None:
    """Abruf mit Chrome-TLS-Impersonation (umgeht TLS-Fingerprint-/Bot-Sperren)."""
    if not _HAS_CFFI:
        return None
    for i in range(versuche):
        try:
            r = cffi_requests.get(
                url, impersonate="chrome",
                headers={"Accept-Language": "de-AT,de;q=0.9,en;q=0.6"},
                timeout=TIMEOUT, allow_redirects=True,
            )
            if r.status_code == 200 and r.text:
                return r.text
            if r.status_code in (429, 503):
                time.sleep(2 + i * 2)
        except Exception:
            time.sleep(1 + i)
    return None


def _get(url: str, s: requests.Session, versuche: int = 2) -> str | None:
    """Holt eine Seite robust: erst Standard-Client, bei Sperre Chrome-Impersonation.

    Listen-/Bezirksseiten laufen weiter ueber requests (keine Regression). Greift
    dort eine Sperre (typisch bei Artikeldetailseiten von Rechenzentrums-IPs),
    wird automatisch curl_cffi nachgezogen - und danach bevorzugt genutzt.
    """
    global _prefer_impersonation
    if _prefer_impersonation and _HAS_CFFI:
        html = _fetch_cffi(url, versuche)
        if html:
            return html
        return _fetch_requests(url, s, versuche)

    html = _fetch_requests(url, s, versuche)
    if html:
        return html
    html = _fetch_cffi(url, versuche)
    if html:
        _prefer_impersonation = True
    return html


def _meta(soup: BeautifulSoup, key: str) -> str:
    m = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
    c = m.get("content") if m else None
    return c.strip() if c else ""


def _voll_url(pfad: str) -> str:
    return pfad if pfad.startswith("http") else BASIS + pfad


def _docid(pfad: str) -> str:
    m = re.search(r"_a(\d+)", pfad)
    return m.group(1) if m else pfad


# -----------------------------------------------------------------------------
# Entdeckung der Struktur (selbst-konfigurierend)
# -----------------------------------------------------------------------------
def _ist_bezirksseite(html: str, slug: str) -> bool:
    """Heuristik: ist die abgerufene Seite wirklich eine Bezirks-Startseite?"""
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    titel = _meta(soup, "og:title")
    typ = _meta(soup, "og:type")
    url = _meta(soup, "og:url")
    return (typ == "website"
            and titel.startswith("Aktuelle Nachrichten aus")
            and url.rstrip("/").endswith("/" + slug))


def entdecke_bezirke(bl_kuerzel: str, s: requests.Session, log=print) -> list[str]:
    """Liest die Bundesland-Seite und entdeckt + validiert die Bezirks-Slugs."""
    bl_slug = BL_SLUG.get(bl_kuerzel)
    if not bl_slug:
        log(f"     [meinbezirk] Unbekanntes Bundesland-Kuerzel: {bl_kuerzel}")
        return []
    html = _get(f"{BASIS}/{bl_slug}", s)
    if not html:
        log(f"     [meinbezirk] Bundesland-Seite /{bl_slug} nicht erreichbar")
        return []

    kandidaten = []
    gesehen = set()
    for m in RE_BEZIRK_KAND.finditer(html):
        seg = m.group(1).lstrip("/")
        if seg in STOP_SEGMENTE or seg in gesehen or "-ki" in seg.lower():
            continue
        if seg.startswith("c-") or "/" in seg:
            continue
        gesehen.add(seg)
        kandidaten.append(seg)

    # Kandidaten parallel validieren (echte Bezirksseite?)
    bezirke = []
    def pruefe(seg):
        h = _get(f"{BASIS}/{seg}", s)
        return seg if _ist_bezirksseite(h, seg) else None

    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed([ex.submit(pruefe, k) for k in kandidaten]):
            r = fut.result()
            if r:
                bezirke.append(r)

    bezirke = sorted(set(bezirke))
    log(f"     [meinbezirk] {bl_kuerzel}: {len(bezirke)} Bezirke entdeckt")
    return bezirke


def _slug_kandidaten(bezirk_name: str) -> list[str]:
    """Leitet aus einem Bezirksnamen moegliche meinbezirk-Slugs ab.
    'Kirchdorf an der Krems' -> {'kirchdorf','kirchdorf-an-der-krems','kirchdorf-krems'};
    'Linz-Land' -> {'linz','linz-land'}. Welcher real existiert, entscheidet
    die Live-Validierung."""
    toks = _norm_txt(bezirk_name).split()
    if not toks:
        return []
    kand = {toks[0], "-".join(toks)}
    kerne = [t for t in toks if len(t) >= 4 and t != "sankt"]
    if kerne:
        kand.add(kerne[0])
        kand.add("-".join(kerne))
    return [k for k in kand if k]


def _seed_bezirk_slugs(namen: list[str], s: requests.Session, log=print) -> list[str]:
    """Steuert die gewaehlten Bezirke DIREKT als Slug an (statt sich auf die
    Verlinkung der BL-Startseite zu verlassen) und behaelt nur die, die als
    echte Bezirksseite validieren. Behebt fehlende Bezirke (z.B. Kirchdorf)."""
    kandidaten = set()
    for name in namen:
        kandidaten.update(_slug_kandidaten(name))
    if not kandidaten:
        return []

    def pruefe(slug):
        return slug if _ist_bezirksseite(_get(f"{BASIS}/{slug}", s), slug) else None

    gueltig = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        for fut in as_completed([ex.submit(pruefe, k) for k in kandidaten]):
            r = fut.result()
            if r:
                gueltig.append(r)
    if gueltig:
        log(f"     [meinbezirk] {len(set(gueltig))} Bezirke per Direkt-Slug validiert: {sorted(set(gueltig))}")
    return gueltig


def entdecke_gemeinden(bezirk_slug: str, html: str | None = None,
                       s: requests.Session | None = None) -> list[str]:
    """Entdeckt die Gemeinde-Adressen (/<Name>-XX) aus der Bezirks-Seite.
    Robust: per BeautifulSoup, akzeptiert relative UND absolute Links."""
    if html is None and s is not None:
        html = _get(f"{BASIS}/{bezirk_slug}", s)
    if not html:
        return []
    gem, gesehen = [], set()
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(BASIS):
            href = href[len(BASIS):]
        m = re.match(r'^(/[A-Za-zÄÖÜäöüß][^/?#"]*-[A-Z]{2})(?:[/?#].*)?$', href)
        if not m:
            continue
        p = m.group(1)
        if p in gesehen or p.lstrip("/").lower() in STOP_SEGMENTE:
            continue
        gesehen.add(p)
        gem.append(p)
    return gem


# -----------------------------------------------------------------------------
# Artikel sammeln + laden
# -----------------------------------------------------------------------------
# Artikel-Pfad (ohne Query/Fragment) fuer den Abgleich aus <a href="...">
RE_ART_PFAD = re.compile(r'^(/[^?#"\s]+/c-[^/?#"]+/[^/?#"]+_a\d+)')

# Bau-/Infrastruktur-/Energie-/Immobilien-Stichwoerter (Kleinschreibung, Teilstring).
# Vorfilter schon beim Einsammeln: Beitraege mit eindeutig bau-fremdem Titel
# (Sport, Todesanzeigen, Veranstaltungen) werden gar nicht erst geladen -> bei
# vielen Bezirken massiv weniger HTTP-Last/Kosten. Bewusst breit; Artikel OHNE
# erkennbaren Titel werden sicherheitshalber NICHT verworfen.
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
    "pflegeheim", "supermarkt", "billa", "expandier", "standort",
)


def _titel_baurelevant(titel: str) -> bool:
    t = (titel or "").lower()
    return any(kw in t for kw in _REGIO_BAU_KEYWORDS)


def sammle_artikel_links(seiten_url: str, s: requests.Session,
                         max_seiten: int = 10) -> list:
    """Sammelt (Pfad, Titel) aus einer Listen-/Bezirks-/Gemeinde-Seite ueber
    mehrere Seiten. meinbezirk blaettert Listen ueber /2, /3, ... -> wir folgen
    den Folgeseiten, bis eine Seite keine NEUEN Artikel mehr liefert (Listenende)
    oder max_seiten erreicht ist. So werden auch aeltere Beitraege erfasst, die
    auf Seite 1 schon weggescrollt sind."""
    out: dict = {}
    basis = seiten_url.rstrip("/")
    for seite in range(1, max_seiten + 1):
        url = basis if seite == 1 else f"{basis}/{seite}"
        html = _get(url, s)
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        neu = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith(BASIS):
                href = href[len(BASIS):]
            m = RE_ART_PFAD.match(href)
            if not m:
                continue
            pfad = m.group(1)
            titel = a.get_text(" ", strip=True)
            if pfad not in out:
                out[pfad] = titel
                neu += 1
            elif titel and len(titel) > len(out[pfad]):
                out[pfad] = titel
        if neu == 0:
            break
    return list(out.items())


def _text_aus_artikel(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
        tag.decompose()
    # Bevorzugt <article>, sonst Hauptinhalt
    haupt = soup.find("article") or soup.find("main") or soup.body or soup
    txt = haupt.get_text(" ", strip=True)
    txt = re.sub(r"\s+", " ", txt)
    return txt[:6000]


def lade_artikel(pfad: str, s: requests.Session) -> dict | None:
    """Laedt einen Artikel und liest Metadaten + Volltext aus."""
    html = _get(_voll_url(pfad), s)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    datum_roh = _meta(soup, "article:published_time")
    datum_dt = None
    if datum_roh:
        try:
            datum_dt = datetime.fromisoformat(datum_roh.replace("Z", "+00:00"))
            if datum_dt.tzinfo is None:
                datum_dt = datum_dt.replace(tzinfo=timezone.utc)
        except Exception:
            datum_dt = None
    return {
        "url":       _voll_url(pfad),
        "docid":     _docid(pfad),
        "titel":     _meta(soup, "og:title"),
        "datum":     datum_roh,
        "datum_dt":  datum_dt,
        "state":     _meta(soup, "pp:state"),       # z.B. "ooe"
        "region":    _meta(soup, "pp:region"),      # z.B. "kirchdorf_krems"
        "kategorie": _meta(soup, "pp:category"),    # z.B. "lokales"
        "beschreibung": _meta(soup, "og:description"),
        "text":      _text_aus_artikel(soup),
    }


# Bundesland-Kuerzel -> meinbezirk pp:state-Wert
_STATE_ZU_BL = {
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


def _bl_aus_state(state: str) -> str:
    return _STATE_ZU_BL.get((state or "").strip().lower(), "")


def _im_fenster(datum_dt, cutoff_dt, heute_dt) -> bool:
    if datum_dt is None:
        return True   # ohne Datum lieber behalten (Agent entscheidet)
    return cutoff_dt <= datum_dt <= (heute_dt + timedelta(days=2))


# -----------------------------------------------------------------------------
# Robuster Abgleich Kunden-Bezirksname  <->  meinbezirk-Slug
# umlaut- und suffix-fest: "Kirchdorf an der Krems" passt auf Slug "kirchdorf",
# "Schärding" auf "schaerding", "Eferding" auf "grieskirchen-eferding".
# -----------------------------------------------------------------------------
def _norm_txt(s: str) -> str:
    s = (s or "").lower()
    s = (s.replace("ä", "ae").replace("ö", "oe")
           .replace("ü", "ue").replace("ß", "ss"))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


_BEZIRK_FUELL = re.compile(r"\b(stadt|land|am|an|im|in|bei|ob|der|die|das|den|umgebung)\b")


def _bezirk_kerne(name: str) -> set:
    """Kernwoerter eines Bezirksnamens (Fuellwoerter + kurze Tokens raus)."""
    n = _BEZIRK_FUELL.sub(" ", _norm_txt(name))
    kerne = {t for t in n.split() if len(t) >= 4}
    return kerne or {n.strip()}


def _bezirk_passt(slug: str, filter_namen: list) -> bool:
    slug_tokens = set(_norm_txt(slug.replace("-", " ")).split())
    if not slug_tokens:
        return False
    for fn in filter_namen:
        for kern in _bezirk_kerne(fn):
            for st in slug_tokens:
                if kern == st or kern in st or st in kern:
                    return True
    return False


def _kompakt(s: str) -> str:
    """Kompaktform fuer Gemeinde-Abgleich: klein, ohne Umlaute/Sonderzeichen,
    'Sankt'->'st', alle Trenner entfernt -> 'stwolfgangimsalzkammergut'."""
    s = re.sub(r"\bsankt\b", "st", _norm_txt(s))
    return re.sub(r"\s+", "", s)


def _gemeinde_passt(slug_path: str, filter_kompakt: list) -> bool:
    """Robuster Abgleich Kunden-Gemeindename <-> meinbezirk-Gemeinde-Slug.
    Vergleicht Kompaktformen (Gleichheit oder Praefix ab 5 Zeichen) – das
    vermeidet Fehltreffer kurzer Namen wie 'Au' oder 'Ach'."""
    core = re.sub(r"-[A-Za-z]{2}$", "", slug_path.lstrip("/"))
    sk = _kompakt(core)
    if not sk:
        return False
    for fk in filter_kompakt:
        if not fk:
            continue
        if sk == fk:
            return True
        if len(fk) >= 5 and sk.startswith(fk):
            return True
        if len(sk) >= 5 and fk.startswith(sk):
            return True
    return False


# -----------------------------------------------------------------------------
# Oeffentliche Hauptfunktion
# -----------------------------------------------------------------------------
def ernte_meinbezirk(bundeslaender: list[str],
                     cutoff_dt: datetime,
                     heute_dt: datetime,
                     bezirke_filter: list[str] | None = None,
                     gemeinden_filter: list[str] | None = None,
                     zeit_ok=lambda: True,
                     max_artikel: int = 4000,
                     max_pro_tag_bezirk: int = 3,
                     max_workers: int = 16,
                     log=print) -> list[dict]:
    """
    Erntet meinbezirk fuer die gewaehlten Bundeslaender.

    bezirke_filter / gemeinden_filter: optionale Namensfilter (Teilstring,
    klein geschrieben). Sind sie gesetzt, werden nur passende Bezirke/Gemeinden
    geerntet (zielgenau, wenn der Kunde eingrenzt). Sonst das ganze Bundesland.
    """
    s = _session()
    bezirke_filter = [b.lower() for b in (bezirke_filter or [])]
    gemeinden_kompakt = [k for k in (_kompakt(g) for g in (gemeinden_filter or [])) if k]

    # 1) Bezirke je Bundesland entdecken + Listen-/Gemeinde-Seiten zusammenstellen
    listen_urls: list[str] = []
    for bl in bundeslaender:
        if not zeit_ok():
            break
        bezirke = entdecke_bezirke(bl, s, log)
        if bezirke_filter:
            # (1) auf der BL-Startseite gefundene Bezirke, die zum Kundenfilter passen
            bezirke = [b for b in bezirke if _bezirk_passt(b, bezirke_filter)]
            # (2) zusaetzlich JEDEN gewaehlten Bezirk direkt ansteuern + validieren –
            #     unabhaengig davon, ob die Startseite ihn gerade verlinkt.
            bezirke = sorted(set(bezirke) | set(_seed_bezirk_slugs(bezirke_filter, s, log)))
        for bez in bezirke:
            if not zeit_ok():
                break
            bez_html = _get(f"{BASIS}/{bez}", s)
            # Rubrik-Erstabrufe des Bezirks
            for ru in RUBRIKEN:
                listen_urls.append(f"{BASIS}/{bez}" + (f"/{ru}" if ru else ""))
            # Gemeinde-Seiten des Bezirks (feingranular -> erreicht auch Aelteres)
            gemeinden = entdecke_gemeinden(bez, html=bez_html)
            if gemeinden_kompakt:
                gemeinden = [g for g in gemeinden if _gemeinde_passt(g, gemeinden_kompakt)]
            listen_urls.extend(_voll_url(g) for g in gemeinden)
            log(f"     [meinbezirk] {bez}: {len(gemeinden)} Gemeinde-Seiten + {len(RUBRIKEN)} Rubriken")

    # 2) Artikel-Links (+ Titel) aus allen Listen-/Gemeinde-Seiten parallel sammeln.
    #    sammle_artikel_links folgt den Folgeseiten /2, /3, ... -> auch aeltere
    #    Beitraege werden erfasst (nicht nur die erste Seite).
    artikel_titel: dict = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(sammle_artikel_links, u, s): u for u in listen_urls}
        for fut in as_completed(futs):
            if not zeit_ok():
                break
            try:
                for pfad, titel in fut.result():
                    alt = artikel_titel.get(pfad)
                    if alt is None or (titel and len(titel) > len(alt)):
                        artikel_titel[pfad] = titel
            except Exception:
                pass

    # Titel-Vorfilter: eindeutig bau-fremde Ueberschriften erst gar nicht laden.
    # Artikel ohne erkennbaren Titel werden behalten (kein falsches Negativ).
    kandidaten = [p for p, t in artikel_titel.items()
                  if (not t) or _titel_baurelevant(t)]
    log(f"     [meinbezirk] {len(artikel_titel)} Artikel-Links gesammelt -> "
        f"{len(kandidaten)} nach Titel-Vorfilter "
        f"(aus {len(listen_urls)} Listen-/Gemeinde-Seiten)")

    # 3) Artikel laden + filtern (Zeitfenster + Bundesland), NEUESTE ZUERST.
    # Die Artikel-ID _a<n> steigt global monoton mit der Zeit. Wir laden in
    # Bloecken von den neuesten abwaerts und HOEREN AUF, sobald ein ganzer Block
    # vollstaendig vor dem cutoff liegt -> alle Artikel im Zeitfenster werden
    # erfasst (unabhaengig vom Gesamtvolumen), ohne dass ein starres Limit die
    # aelteren, aber noch gueltigen Beitraege abschneidet. max_artikel bleibt
    # nur als harte Sicherheits-Obergrenze.
    erlaubte_bl = set(bundeslaender)
    treffer: list[dict] = []
    geprueft = 0
    # Diagnose-Zaehler: zeigen im Log GENAU, wo Artikel ausscheiden.
    n_http_fail = 0     # Seite nicht ladbar (None) -> z.B. Sperre/Timeout
    n_wrong_bl = 0      # eindeutig anderes Bundesland
    n_out_window = 0    # Datum ausserhalb des Zeitfensters
    n_no_date = 0       # ohne Datum -> behalten
    n_in_window = 0     # Datum im Fenster -> behalten

    def _aid(p):
        m = re.search(r"_a(\d+)", p)
        return int(m.group(1)) if m else 0

    # Bezirks-Budget (skaliert mit den Tagen des Kunden): pro Bezirk hoechstens
    # (Tage x max_pro_tag_bezirk) Artikel zur Analyse, NEUESTE zuerst. Ruhige
    # Bezirke bleiben voll abgedeckt; nur sehr aktive werden gedeckelt -> die
    # Kosten skalieren fair mit dem, was der Kunde gewaehlt und bezahlt hat.
    _tage = max(1, (heute_dt - cutoff_dt).days)
    _budget = _tage * max(1, max_pro_tag_bezirk)

    def _bezirk(pf: str) -> str:
        teile = pf.split("/")
        return teile[1].lower() if len(teile) > 1 else ""

    _pro_bezirk: dict = {}
    for pf in sorted(kandidaten, key=_aid, reverse=True):   # neueste zuerst
        lst = _pro_bezirk.setdefault(_bezirk(pf), [])
        if len(lst) < _budget:
            lst.append(pf)
    kandidaten_capped = [pf for lst in _pro_bezirk.values() for pf in lst]
    if len(kandidaten_capped) < len(kandidaten):
        log(f"     [meinbezirk] Bezirks-Budget {_budget}/Bezirk "
            f"({_tage} Tage x {max_pro_tag_bezirk}) -> "
            f"{len(kandidaten_capped)} von {len(kandidaten)} Artikeln zur Analyse")

    pfade = sorted(kandidaten_capped, key=_aid, reverse=True)
    obergrenze = min(len(pfade), max_artikel)
    BLOCK = 200
    i = 0
    stop = False
    while i < obergrenze and not stop:
        if not zeit_ok():
            log("     [meinbezirk] Zeitbudget erreicht \u2013 Artikel-Laden gestoppt.")
            break
        block = pfade[i:i + BLOCK]
        i += BLOCK
        aelteste = None
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(lade_artikel, p, s): p for p in block}
            for fut in as_completed(futs):
                if not zeit_ok():
                    stop = True
                    break
                geprueft += 1
                try:
                    art = fut.result()
                except Exception:
                    art = None
                if not art:
                    n_http_fail += 1
                    continue
                bl_art = _bl_aus_state(art["state"])
                if bl_art and erlaubte_bl and bl_art not in erlaubte_bl:
                    n_wrong_bl += 1
                    continue
                d = art["datum_dt"]
                if d is not None and (aelteste is None or d < aelteste):
                    aelteste = d
                if _im_fenster(art["datum_dt"], cutoff_dt, heute_dt):
                    treffer.append(art)
                    if d is None:
                        n_no_date += 1
                    else:
                        n_in_window += 1
                else:
                    n_out_window += 1
        # Ganzer Block aelter als cutoff -> alle weiteren (kleinere ID) sind aelter
        if aelteste is not None and aelteste < cutoff_dt:
            stop = True

    # Diagnose-Aufschluesselung: macht eine '0 Treffer'-Situation sofort erklaerbar.
    log(f"     [meinbezirk] {geprueft} versucht -> {len(treffer)} uebernommen "
        f"({n_no_date} ohne Datum + {n_in_window} im Fenster) | "
        f"verworfen: {n_http_fail} HTTP-Fehler, {n_wrong_bl} anderes BL, "
        f"{n_out_window} ausserhalb Zeitfenster")
    if n_http_fail and not treffer:
        _mode = "aktiv" if _prefer_impersonation else ("verfuegbar" if _HAS_CFFI else "NICHT installiert")
        log(f"     [meinbezirk] HINWEIS: alle Artikel HTTP-Fehler -> Portal blockt den "
            f"Abruf. Chrome-Impersonation: {_mode}. Bleibt es bei 0, ist die IP "
            f"gesperrt (dann Proxy noetig).")
    return treffer


# -----------------------------------------------------------------------------
# Standalone-Selbsttest (read-only)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    print("ProjectScout – Regionalmedien-Ernte: SELBSTTEST (read-only)")
    print("Zeitpunkt:", datetime.now().isoformat(), "\n")

    heute = datetime.now(timezone.utc)
    cutoff = heute - timedelta(days=120)

    artikel = ernte_meinbezirk(
        bundeslaender=["OOE"],
        cutoff_dt=cutoff,
        heute_dt=heute,
        bezirke_filter=["Kirchdorf an der Krems"],  # echter Bezirksname -> testet auch Slug-Ableitung
        max_artikel=1500,
        max_workers=12,
        log=print,
    )

    print(f"\nGEERNTET: {len(artikel)} Artikel im Bezirk Kirchdorf (letzte 120 Tage)\n")
    # Stichprobe
    for a in sorted(artikel, key=lambda x: x.get("datum") or "", reverse=True)[:12]:
        print(f"  {a['datum'] or '?':25s} [{a['kategorie']:10s}] {a['titel'][:60]}")

    # Werden die zwei bekannten Bauprojekte gefunden?
    ziele = {"8597473": "Billa-Areal", "8526871": "Micheldorf-Hotel"}
    gefunden = {d: any(a["docid"] == d for a in artikel) for d in ziele}
    print("\nZielartikel-Check:")
    for d, label in ziele.items():
        print(f"  {label:18s} (docid {d}): {'GEFUNDEN ✓' if gefunden[d] else 'NICHT gefunden ✗'}")

    print("\n=== ENDE Selbsttest ===")
    import sys
    sys.exit(0 if all(gefunden.values()) else 1)
