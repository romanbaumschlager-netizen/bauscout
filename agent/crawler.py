# =============================================================================
# ProjectScout – Crawler-Modul
# Datei: agent/crawler.py
#
# Aufgabe: Echtes Crawling der Gemeinde-Websites (zweite Säule neben web_search).
# Pro Gemeinde wird die Website DIREKT heruntergeladen, die Seite mit den
# Gemeinderats-/Sitzungsprotokollen gesucht, die neuesten Protokoll-PDFs geladen
# und der Textinhalt extrahiert. Die eigentliche KI-Analyse (Haiku) passiert
# danach im agent.py – dieses Modul kümmert sich nur um das BESCHAFFEN.
#
# Dieses Modul ist defensiv gebaut: Jeder einzelne Netzwerk-/Parsing-Fehler wird
# abgefangen und führt nur dazu, dass die betroffene Gemeinde übersprungen wird –
# niemals zum Abbruch des gesamten Laufs.
# =============================================================================

import re
import io
import time
import hashlib
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# Optionale Abhängigkeiten – wenn nicht installiert, degradiert der Crawler
# sauber (PDF-Text wird dann übersprungen, HTML-Parsing via Fallback).
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    import pdfplumber
except Exception:
    pdfplumber = None


# ── Konfiguration ────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ProjectScout/1.0; +https://project-scout.at)",
    "Accept-Language": "de-AT,de;q=0.9",
}
HTTP_TIMEOUT      = 12      # Sekunden pro Einzelabruf
MAX_PDF_BYTES     = 12_000_000   # PDFs größer als ~12 MB überspringen
MAX_PDF_PRO_SEITE = 2       # höchstens die 2 neuesten Protokoll-PDFs je Gemeinde
MAX_TEXT_ZEICHEN  = 18_000  # so viel Text je Gemeinde max. an die KI weiterreichen

# Schlüsselwörter, an denen Protokoll-/Sitzungs-Links erkannt werden.
PROTOKOLL_KEYWORDS = [
    "protokoll", "sitzung", "gemeinderat", "ratsprotokoll", "niederschrift",
    "verhandlungsschrift", "tagesordnung", "beschluss", "beschluesse", "beschlüsse",
    "gemeinderatssitzung", "stadtrat", "sitzungsbericht",
]

# Wörter, die in PDF-Dateinamen auf ein Protokoll hindeuten (zur Priorisierung).
PDF_PROTOKOLL_HINWEISE = PROTOKOLL_KEYWORDS + ["gr-", "gr_", "sitzungsprotokoll"]

# Datums-/Jahresmuster zum Sortieren der PDFs (neueste zuerst).
_DATUM_REGEX = re.compile(r"(20\d{2})[-_.]?(\d{2})?[-_.]?(\d{2})?")


def _hole(url: str, erlaube_pdf: bool = False):
    """Robuster GET. Gibt das requests.Response-Objekt zurück oder None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT,
                            allow_redirects=True, stream=erlaube_pdf)
        if resp.status_code == 200:
            return resp
    except Exception:
        pass
    return None


def _text_aus_html(html: str) -> str:
    """Extrahiert sichtbaren Text aus HTML (mit BeautifulSoup, sonst grob per Regex)."""
    if not html:
        return ""
    if BeautifulSoup is not None:
        try:
            suppe = BeautifulSoup(html, "html.parser")
            for tag in suppe(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return re.sub(r"\s+", " ", suppe.get_text(" ")).strip()
        except Exception:
            pass
    # Fallback ohne bs4
    ohne_tags = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", ohne_tags).strip()


def _finde_links(basis_url: str, html: str):
    """Gibt eine Liste (linktext, absolute_url) aller Links der Seite zurück."""
    links = []
    if not html:
        return links
    if BeautifulSoup is not None:
        try:
            suppe = BeautifulSoup(html, "html.parser")
            for a in suppe.find_all("a", href=True):
                text = (a.get_text(" ") or "").strip().lower()
                url  = urljoin(basis_url, a["href"])
                links.append((text, url))
            return links
        except Exception:
            pass
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        url = urljoin(basis_url, m.group(1))
        links.append(("", url))
    return links


def _datum_score(text: str) -> int:
    """Bildet aus einem Text (Dateiname/Linktext) eine sortierbare Jahres-/Datumszahl."""
    bestes = 0
    for m in _DATUM_REGEX.finditer(text):
        jahr = int(m.group(1))
        monat = int(m.group(2)) if m.group(2) else 0
        tag = int(m.group(3)) if m.group(3) else 0
        score = jahr * 10000 + monat * 100 + tag
        if score > bestes:
            bestes = score
    return bestes


def _finde_protokoll_seite(start_url: str, protokoll_pfade: list) -> tuple:
    """
    Sucht die Unterseite mit den Gemeinderatsprotokollen.
    Strategie:
      1. Direkte Pfadversuche (protokoll_pfade) an der Domain.
      2. Startseite laden und nach Links mit Protokoll-Schlüsselwörtern suchen.
    Gibt (protokoll_seiten_url, html_der_seite) zurück oder (None, None).
    """
    if not start_url:
        return None, None
    # Domain-Basis bestimmen
    p = urlparse(start_url if "://" in start_url else "http://" + start_url)
    basis = f"{p.scheme or 'http'}://{p.netloc or p.path}"

    # 1) Direkte Pfadversuche
    for pfad in protokoll_pfade or []:
        ziel = urljoin(basis, pfad)
        resp = _hole(ziel)
        if resp is not None and len(resp.text) > 400:
            return ziel, resp.text

    # 2) Startseite laden, Links scannen
    resp = _hole(basis)
    if resp is None:
        return None, None
    start_html = resp.text
    for text, url in _finde_links(basis, start_html):
        ziel_l = (text + " " + url).lower()
        if any(kw in ziel_l for kw in PROTOKOLL_KEYWORDS):
            unter = _hole(url)
            if unter is not None and len(unter.text) > 400:
                return url, unter.text
    # Nichts Spezielles gefunden -> Startseite selbst zurückgeben (besser als nichts)
    return basis, start_html


def _finde_pdf_links(seiten_url: str, html: str) -> list:
    """Findet PDF-Links auf einer Seite, neueste zuerst (nach Datum im Linktext/Dateiname)."""
    kandidaten = []
    for text, url in _finde_links(seiten_url, html):
        if ".pdf" in url.lower():
            bewertung = _datum_score(text + " " + url)
            # Protokoll-Hinweise im Namen leicht bevorzugen
            if any(h in (text + " " + url).lower() for h in PDF_PROTOKOLL_HINWEISE):
                bewertung += 5  # minimaler Bonus, ohne Datum zu überstimmen
            kandidaten.append((bewertung, url))
    # nach Bewertung absteigend, Duplikate raus
    gesehen = set()
    sortiert = []
    for _, url in sorted(kandidaten, key=lambda x: x[0], reverse=True):
        if url not in gesehen:
            gesehen.add(url)
            sortiert.append(url)
    return sortiert


def _pdf_text(pdf_bytes: bytes) -> str:
    """Extrahiert Text aus PDF-Bytes (pdfplumber). Leere Rückgabe bei gescannten PDFs."""
    if pdfplumber is None or not pdf_bytes:
        return ""
    try:
        text_teile = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for seite in pdf.pages[:25]:   # höchstens 25 Seiten je PDF
                t = seite.extract_text() or ""
                if t:
                    text_teile.append(t)
        return re.sub(r"\s+", " ", " ".join(text_teile)).strip()
    except Exception:
        return ""


def crawle_gemeinde(gemeinde: dict, protokoll_pfade: list) -> dict | None:
    """
    Crawlt EINE Gemeinde. Gibt ein dict zurück:
      {
        "gemeinde": Name, "bezirk": ..., "bundesland": ...,
        "quelle_url": gefundene Protokoll-/Quellseite,
        "inhalt": zusammengeführter Text (Seite + Protokoll-PDFs),
        "pdf_bytes": Bytes des neuesten PDFs (für optionale KI-Direktanalyse) oder None,
        "pdf_url": URL des neuesten PDFs oder "",
        "inhalt_hash": Hash des Inhalts (für Änderungserkennung im Cache),
      }
    oder None, wenn gar nichts abrufbar war.
    """
    name = (gemeinde.get("name") or "").strip()
    url  = (gemeinde.get("url") or "").strip()
    if not url:
        return None

    seite_url, seite_html = _finde_protokoll_seite(url, protokoll_pfade)
    if not seite_url:
        return None

    seiten_text = _text_aus_html(seite_html)

    # Neueste Protokoll-PDFs laden
    pdf_text_gesamt = ""
    erstes_pdf_bytes = None
    erstes_pdf_url = ""
    for pdf_url in _finde_pdf_links(seite_url, seite_html)[:MAX_PDF_PRO_SEITE]:
        resp = _hole(pdf_url, erlaube_pdf=True)
        if resp is None:
            continue
        try:
            # Größe begrenzen
            inhalt = resp.content
            if len(inhalt) > MAX_PDF_BYTES:
                continue
        except Exception:
            continue
        if erstes_pdf_bytes is None:
            erstes_pdf_bytes = inhalt
            erstes_pdf_url = pdf_url
        pdf_text_gesamt += " " + _pdf_text(inhalt)

    inhalt = (seiten_text + " " + pdf_text_gesamt).strip()[:MAX_TEXT_ZEICHEN]
    if len(inhalt) < 80 and erstes_pdf_bytes is None:
        # Praktisch nichts Brauchbares gefunden
        return None

    return {
        "gemeinde":    name,
        "bezirk":      (gemeinde.get("bezirk") or "").strip(),
        "bundesland":  (gemeinde.get("bundesland") or "").strip(),
        "quelle_url":  seite_url,
        "inhalt":      inhalt,
        "pdf_bytes":   erstes_pdf_bytes,
        "pdf_url":     erstes_pdf_url,
        "inhalt_hash": hashlib.sha256(inhalt.encode("utf-8", "ignore")).hexdigest()[:16],
    }


def crawle_gemeinden_parallel(gemeinden: list, protokoll_pfade: list,
                              max_workers: int = 12,
                              zeit_ok=None, fortschritt=None) -> list:
    """
    Crawlt mehrere Gemeinden parallel (I/O-gebunden -> Threads).
      gemeinden:       Liste von Gemeinde-Dicts (name, bezirk, url, bundesland)
      protokoll_pfade: Liste typischer Protokoll-URL-Pfade
      max_workers:     Anzahl gleichzeitiger Downloads
      zeit_ok:         optionale Funktion () -> bool; False => keine NEUEN Crawls starten
      fortschritt:     optionale Funktion (anzahl_fertig) für Logging
    Gibt eine Liste der erfolgreichen crawle_gemeinde()-Ergebnisse zurück.
    """
    ergebnisse = []
    if not gemeinden:
        return ergebnisse

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for g in gemeinden:
            if zeit_ok is not None and not zeit_ok():
                break
            futures[pool.submit(crawle_gemeinde, g, protokoll_pfade)] = g

        fertig = 0
        for fut in as_completed(futures):
            fertig += 1
            try:
                res = fut.result()
            except Exception:
                res = None
            if res:
                ergebnisse.append(res)
            if fortschritt is not None and fertig % 10 == 0:
                fortschritt(fertig)
    return ergebnisse
