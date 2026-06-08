# =============================================================================
# ProjectScout – Crawler-Modul (agent/crawler.py)
#
# Aufgabe: Echtes Crawling der Gemeinde-Websites (zweite Saeule neben web_search).
# Pro Gemeinde werden MEHRERE relevante Bereiche gesucht und ausgelesen:
#   - Protokolle   (Gemeinderats-/Sitzungsprotokolle)
#   - Amtstafel    (Kundmachungen, Bauverhandlungen, Edikte)
#   - Bau          (Bauamt/Bauabteilung, Flaechenwidmung, Bebauungsplan)
#   - Aktuelles    (Neuigkeiten, Verordnungen, Mitteilungen)
# Von jedem Bereich werden Inhaltstext UND verlinkte Dokumente (PDFs) geladen.
# Die KI-Analyse (Haiku) passiert danach im agent.py – dieses Modul BESCHAFFT nur.
#
# Robustheit gegenueber dem haeufigsten AT-Gemeinde-CMS (RiS-Kommunal/gem2go):
#   * gem2go rendert das KOMPLETTE Menue in jeder Seite -> Text/Links werden nur
#     aus dem Inhalts-Container (#content/main) gelesen, nicht aus der Navigation.
#   * Dokumente werden ueber Handler wie GetDocument.ashx?fileid=... ausgeliefert,
#     also OHNE ".pdf" in der URL -> Erkennung per Content-Type / %PDF-Magic-Bytes.
#
# Defensiv: jeder Netzwerk-/Parsing-Fehler ueberspringt nur die betroffene
# Gemeinde/Seite, niemals Abbruch des Gesamtlaufs.
# =============================================================================

import re
import io
import hashlib
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

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
HTTP_TIMEOUT               = 12          # Sekunden pro Einzelabruf
MAX_PDF_BYTES              = 12_000_000  # PDFs groesser als ~12 MB ueberspringen
MAX_PDF_SEITEN             = 25          # je PDF hoechstens so viele Seiten lesen
MAX_TEXT_ZEICHEN           = 22_000      # so viel Text je Gemeinde max. an die KI
MAX_BEREICHE               = 4           # je Gemeinde max. 4 Bereichsseiten (1 je Typ)
MAX_DOK_PRO_BEREICH        = 4           # je Bereich max. 4 Dokumente laden
MAX_DOK_GESAMT             = 8           # je Gemeinde max. 8 Dokumente gesamt
MAX_KANDIDATEN_PRO_BEREICH = 7           # so viele Doc-Links je Bereich auf PDF pruefen

# Themen-Bereiche und ihre Erkennungs-Schluesselwoerter (Link-Text/URL, lowercase).
BEREICH_KEYWORDS = {
    "protokoll": ("protokoll", "sitzung", "gemeinderat", "ratsprotokoll", "niederschrift",
                  "gemeinderatssitzung", "stadtrat", "sitzungsbericht", "tagesordnung",
                  "verhandlungsschrift"),
    "amtstafel": ("amtstafel", "kundmachung", "verlautbarung", "ediktal", "edikt",
                  "anschlagtafel", "schwarzes brett"),
    "bau":       ("bauamt", "bauabteilung", "bauen & wohnen", "bauen und wohnen",
                  "flächenwidmung", "flachenwidmung", "bebauungsplan", "raumordnung",
                  "raumplanung", "bauvorhaben", "bauverhandlung", "ortsbildplanung"),
    "aktuelles": ("neuigkeit", "aktuelles", "verordnung", "mitteilung", "gemeindenews",
                  "news"),
}

# Hinweis-Woerter, die ein Dokument als relevant kennzeichnen (Text/URL).
DOK_HINWEISE = ("protokoll", "sitzung", "niederschrift", "gemeinderat", "gr-", "gr_",
                "sitzungsprotokoll", "kundmachung", "bescheid", "verordnung",
                "bauverhandlung", "edikt", "tagesordnung", "dokument", "pdf", "datei",
                "download", "anhang", "beilage", "widmung", "bebauungsplan")

# Handler-/Download-Muster: PDFs werden im CMS oft ueber solche URLs ausgeliefert.
HANDLER_HINWEISE = ("getdocument", "getfile", "downloadfile", "showfile", "showdocument",
                    "fileid=", "docid=", "?file=", "file=", "/dokument", "/dl/", "/download")

# Eindeutige Bild-/Nicht-Dokument-Muster -> nie als Dokument behandeln.
BILD_HINWEISE = ("getimage", "/image", "/bild", ".jpg", ".jpeg", ".png", ".gif", ".svg",
                 ".webp", ".ico", "logo", "banner", "thumb", "favicon")

# "Eltern"-Menuepunkte, denen man 1 Ebene folgt, falls Bereiche nicht direkt verlinkt sind.
ELTERN_KEYWORDS = ("politik", "bürgerservice", "buergerservice", "mitgestalten",
                   "verwaltung", "gemeindeamt", "rathaus", "e-government", "egovernment",
                   "bürgerinfo", "buergerinfo")

# Datums-Erkennung (fuer "neueste zuerst").
MONATE = {"jänner": 1, "jaenner": 1, "januar": 1, "februar": 2, "feber": 2, "märz": 3,
          "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7, "august": 8,
          "september": 9, "oktober": 10, "november": 11, "dezember": 12}
RE_DMY        = re.compile(r"(\d{1,2})[.\-_/](\d{1,2})[.\-_/](20\d{2})")
RE_YMD        = re.compile(r"(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})")
RE_MONATSNAME = re.compile(
    r"(\d{1,2})?\.?\s*(j[äae]nner|januar|februar|feber|m[äae]rz|april|mai|juni|juli|"
    r"august|september|oktober|november|dezember)\s*(20\d{2})", re.I)
RE_JAHR       = re.compile(r"(20\d{2})")


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _hole(url: str, erlaube_pdf: bool = False):
    """Robuster GET. Gibt das requests.Response-Objekt zurueck oder None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT,
                            allow_redirects=True, stream=erlaube_pdf)
        if resp.status_code == 200:
            return resp
    except Exception:
        pass
    return None


# ── HTML-Parsing (nur Inhaltsbereich, ohne Navigation) ───────────────────────
def _soup(html: str):
    if not html or BeautifulSoup is None:
        return None
    try:
        return BeautifulSoup(html, "html.parser")
    except Exception:
        return None


def _content_node(suppe):
    """Liefert den Inhalts-Container (ohne Navigation/Menue), sonst body/Gesamt.
    gem2go & viele CMS legen den Hauptinhalt in #content / <main> / [role=main]."""
    if suppe is None:
        return None
    for attrs in ({"id": "content"}, {"id": "inhalt"}, {"id": "main"}, {"id": "maincontent"},
                  {"role": "main"}, {"class": "content"}, {"class": "main-content"}):
        node = suppe.find(attrs=attrs)
        if node is not None:
            return node
    main = suppe.find("main")
    if main is not None:
        return main
    return suppe.body or suppe


def _text_aus_html(html: str) -> str:
    """Sichtbarer Text aus dem INHALTSBEREICH (Navigation/Skripte entfernt)."""
    suppe = _soup(html)
    if suppe is not None:
        node = _content_node(suppe)
        try:
            for tag in node.find_all(["script", "style", "nav", "header", "footer", "form"]):
                tag.decompose()
        except Exception:
            pass
        txt = node.get_text(" ", strip=True)
        return re.sub(r"\s+", " ", txt).strip()
    txt = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", txt).strip()


def _links_aus_node(basis_url: str, node):
    out = []
    if node is None:
        return out
    try:
        for a in node.find_all("a", href=True):
            text = (a.get_text(" ") or "").strip()
            out.append((text, urljoin(basis_url, a["href"])))
    except Exception:
        pass
    return out


def _alle_links(basis_url: str, html: str):
    """Alle Links der GANZEN Seite (fuer Bereichs-Discovery; gem2go-Vollmenue)."""
    suppe = _soup(html)
    if suppe is not None:
        return _links_aus_node(basis_url, suppe)
    out = []
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html or "", re.I):
        out.append(("", urljoin(basis_url, m.group(1))))
    return out


# ── Datum / Dokument-Erkennung ───────────────────────────────────────────────
def _datum_score(text: str) -> int:
    """Sortierbare Datumszahl (jjjjmmtt) aus Text/Dateiname; 0 wenn kein Datum."""
    t = (text or "").lower()
    m = RE_MONATSNAME.search(t)
    if m:
        tag = int(m.group(1)) if m.group(1) else 0
        return int(m.group(3)) * 10000 + MONATE.get(m.group(2).lower(), 0) * 100 + tag
    m = RE_DMY.search(t)
    if m:
        return int(m.group(3)) * 10000 + int(m.group(2)) * 100 + int(m.group(1))
    m = RE_YMD.search(t)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    m = RE_JAHR.search(t)
    if m:
        return int(m.group(1)) * 10000
    return 0


def _bereich_typ(text: str, url: str):
    """Welcher Themen-Bereich passt zu einem Link? (oder None)"""
    s = (text + " " + url).lower()
    for typ, kws in BEREICH_KEYWORDS.items():
        if any(kw in s for kw in kws):
            return typ
    return None


def _ist_dokument_link(text: str, url: str) -> bool:
    """Vorfilter: koennte dieser Link ein Dokument (PDF) sein? (Bilder/Menue raus.)
    Die endgueltige Pruefung macht _lade_pdf ueber den Content-Type."""
    u = (url or "").lower()
    if u.startswith(("mailto:", "tel:", "javascript:")) or u.strip() in ("", "#"):
        return False
    if any(b in u for b in BILD_HINWEISE):
        return False
    if ".pdf" in u:
        return True
    s = (text + " " + u).lower()
    hat_handler = any(h in u for h in HANDLER_HINWEISE)
    hat_hinweis = any(h in s for h in DOK_HINWEISE) or _datum_score(s) > 0
    if hat_handler and hat_hinweis:
        return True
    if ".ashx" in u and hat_hinweis:
        return True
    return False


def _lade_pdf(url: str):
    """Laedt eine URL und gibt die Bytes zurueck, WENN es ein echtes PDF ist
    (Content-Type application/pdf ODER %PDF-Magic-Bytes). Sonst None.
    So werden auch Handler-URLs (GetDocument.ashx) ohne '.pdf' korrekt erkannt."""
    resp = _hole(url, erlaube_pdf=False)
    if resp is None:
        return None
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit() and int(cl) > MAX_PDF_BYTES:
        return None
    try:
        daten = resp.content
    except Exception:
        return None
    if not daten or len(daten) > MAX_PDF_BYTES:
        return None
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ctype or daten[:4] == b"%PDF":
        return daten
    return None


def _pdf_text(pdf_bytes: bytes) -> str:
    """Text aus PDF-Bytes (pdfplumber). Leer bei gescannten PDFs (-> Vision im agent)."""
    if pdfplumber is None or not pdf_bytes:
        return ""
    try:
        teile = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for seite in pdf.pages[:MAX_PDF_SEITEN]:
                t = seite.extract_text() or ""
                if t:
                    teile.append(t)
        return re.sub(r"\s+", " ", " ".join(teile)).strip()
    except Exception:
        return ""


# ── Bereichs-Discovery + Dokument-Sammlung ───────────────────────────────────
def _finde_bereichsseiten(basis: str, start_html: str) -> dict:
    """Findet pro Themen-Typ die beste Bereichsseite. gem2go rendert das ganze
    Menue in jeder Seite -> meist reicht die Startseite. Sonst 1 Ebene tiefer
    ueber 'Eltern'-Menuepunkte (Politik/Buergerservice/...)."""
    gefunden = {}

    def scanne(html, quelle_url):
        for text, url in _alle_links(quelle_url, html):
            typ = _bereich_typ(text, url)
            if typ and typ not in gefunden:
                gefunden[typ] = url.split("#")[0]

    scanne(start_html, basis)
    if len(gefunden) < 2:
        besucht = set()
        for text, url in _alle_links(basis, start_html):
            if any(kw in (text + " " + url).lower() for kw in ELTERN_KEYWORDS):
                u = url.split("#")[0]
                if u in besucht:
                    continue
                besucht.add(u)
                resp = _hole(u)
                if resp is not None:
                    scanne(resp.text, u)
                if len(gefunden) >= MAX_BEREICHE or len(besucht) >= 6:
                    break
    return gefunden


def _dokumente_einer_seite(seiten_url: str, html: str) -> list:
    """Findet Dokument-Links im INHALTSBEREICH, sortiert neueste zuerst, prueft sie
    per Content-Type auf PDF und gibt [(url, pdf_bytes), ...] zurueck.
    Folgt zusaetzlich EINER Archiv-/Jahres-Unterseite, falls direkt wenig da ist."""
    suppe = _soup(html)
    node = _content_node(suppe) if suppe is not None else None
    links = _links_aus_node(seiten_url, node) if node is not None else _alle_links(seiten_url, html)

    kandidaten = []
    gesehen = set()
    for idx, (text, url) in enumerate(links):
        u = url.split("#")[0]
        if u in gesehen:
            continue
        if _ist_dokument_link(text, u):
            gesehen.add(u)
            kandidaten.append((-_datum_score(text + " " + u), idx, u))
    kandidaten.sort()

    dokumente = []
    for _, _, u in kandidaten[:MAX_KANDIDATEN_PRO_BEREICH]:
        pdf = _lade_pdf(u)
        if pdf:
            dokumente.append((u, pdf))
        if len(dokumente) >= MAX_DOK_PRO_BEREICH:
            break

    # Falls kaum Dokumente direkt: EINER Archiv-/Jahres-Unterseite folgen.
    if len(dokumente) < 2 and node is not None:
        for text, url in links:
            t = (text or "").strip().lower()
            u = url.split("#")[0]
            if (re.fullmatch(r"20\d{2}", t) or "archiv" in t) and u != seiten_url.split("#")[0]:
                resp = _hole(u)
                if resp is not None:
                    sub = _content_node(_soup(resp.text))
                    for stext, surl in _links_aus_node(u, sub):
                        su = surl.split("#")[0]
                        if _ist_dokument_link(stext, su):
                            pdf = _lade_pdf(su)
                            if pdf:
                                dokumente.append((su, pdf))
                            if len(dokumente) >= MAX_DOK_PRO_BEREICH:
                                break
                break  # nur EINE Archiv-Seite, um die Last zu begrenzen
    return dokumente[:MAX_DOK_PRO_BEREICH]


# ── Eine Gemeinde komplett crawlen ───────────────────────────────────────────
def crawle_gemeinde(gemeinde: dict, protokoll_pfade: list):
    """Crawlt EINE Gemeinde (Protokolle + Amtstafel + Bau + Aktuelles) und gibt ein
    dict zurueck (Keys: gemeinde, bezirk, bundesland, quelle_url, inhalt, pdf_bytes,
    pdf_url, inhalt_hash) oder None, wenn nichts Brauchbares abrufbar war."""
    name = (gemeinde.get("name") or "").strip()
    url  = (gemeinde.get("url") or "").strip()
    if not url:
        return None
    p = urlparse(url if "://" in url else "http://" + url)
    basis = f"{p.scheme or 'http'}://{p.netloc or p.path}"

    resp = _hole(basis)
    start_html = resp.text if resp is not None else ""

    bereiche = _finde_bereichsseiten(basis, start_html) if start_html else {}

    # Direkter Protokoll-Pfad als schneller Zusatz-Hinweis (manche CMS).
    if "protokoll" not in bereiche:
        for pfad in (protokoll_pfade or [])[:10]:
            ziel = urljoin(basis, pfad)
            r = _hole(ziel)
            if r is not None and len(r.text) > 400:
                bereiche["protokoll"] = ziel
                break

    def _leer_ergebnis(quelle, text):
        if len(text) < 80:
            return None
        return {
            "gemeinde": name,
            "bezirk": (gemeinde.get("bezirk") or "").strip(),
            "bundesland": (gemeinde.get("bundesland") or "").strip(),
            "quelle_url": quelle,
            "inhalt": text,
            "pdf_bytes": None,
            "pdf_url": "",
            "inhalt_hash": hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16],
        }

    if not bereiche:
        return _leer_ergebnis(basis, _text_aus_html(start_html)[:MAX_TEXT_ZEICHEN]) if start_html else None

    text_teile = []
    dok_gesamt = 0
    erstes_pdf_bytes = None
    erstes_pdf_url = ""
    quelle_primary = ""

    for typ in ("protokoll", "amtstafel", "bau", "aktuelles"):
        if typ not in bereiche:
            continue
        seiten_url = bereiche[typ]
        r = _hole(seiten_url)
        if r is None:
            continue
        seiten_html = r.text
        if not quelle_primary:
            quelle_primary = seiten_url
        seiten_text = _text_aus_html(seiten_html)
        if seiten_text:
            text_teile.append(f"[{typ.upper()}] {seiten_text}")
        if dok_gesamt < MAX_DOK_GESAMT:
            for durl, pdf in _dokumente_einer_seite(seiten_url, seiten_html):
                if dok_gesamt >= MAX_DOK_GESAMT:
                    break
                ptext = _pdf_text(pdf)
                if ptext:
                    text_teile.append(ptext)
                if erstes_pdf_bytes is None:
                    erstes_pdf_bytes = pdf
                    erstes_pdf_url = durl
                dok_gesamt += 1

    inhalt = re.sub(r"\s+", " ", " ".join(text_teile)).strip()[:MAX_TEXT_ZEICHEN]
    if len(inhalt) < 80 and erstes_pdf_bytes is None:
        return None

    return {
        "gemeinde": name,
        "bezirk": (gemeinde.get("bezirk") or "").strip(),
        "bundesland": (gemeinde.get("bundesland") or "").strip(),
        "quelle_url": quelle_primary or basis,
        "inhalt": inhalt,
        "pdf_bytes": erstes_pdf_bytes,
        "pdf_url": erstes_pdf_url,
        "inhalt_hash": hashlib.sha256(inhalt.encode("utf-8", "ignore")).hexdigest()[:16],
    }


def crawle_gemeinden_parallel(gemeinden: list, protokoll_pfade: list,
                              max_workers: int = 12,
                              zeit_ok=None, fortschritt=None) -> list:
    """Crawlt mehrere Gemeinden parallel (I/O-gebunden -> Threads).
    Signatur/Rueckgabe unveraendert gegenueber der Vorversion (agent.py kompatibel)."""
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


# ── Kostenloser Selbsttest (read-only, ohne KI/Supabase/Stripe) ──────────────
# Aufruf:  python agent/crawler.py
if __name__ == "__main__":
    import sys
    from datetime import datetime

    TEST_PFADE = [
        "/Gemeinde/Politik/Gemeinderatsprotokolle", "/gemeinderatsprotokolle",
        "/ratsprotokolle", "/sitzungsprotokolle", "/politik/gemeinderatsprotokolle",
        "/amtstafel/gemeinderatsprotokolle", "/gemeinderat/protokolle",
    ]
    TEST_GEMEINDEN = [
        {"name": "Micheldorf in Oberösterreich", "bezirk": "Kirchdorf",
         "bundesland": "OOE", "url": "https://www.micheldorf.at"},
        {"name": "Kirchdorf an der Krems", "bezirk": "Kirchdorf",
         "bundesland": "OOE", "url": "https://www.kirchdorf.gv.at"},
    ]

    print("ProjectScout – Crawler-SELBSTTEST (read-only)")
    print("Zeitpunkt:", datetime.now().isoformat(), "\n")

    bestanden = True
    pdfs_total = 0
    for g in TEST_GEMEINDEN:
        print(f"=== {g['name']}  ({g['url']}) ===")
        r = _hole(g["url"].rstrip("/"))
        html = r.text if r is not None else ""
        bereiche = _finde_bereichsseiten(g["url"].rstrip("/"), html) if html else {}
        print("   Startseite erreichbar:", bool(html), "| Bereiche:",
              ", ".join(bereiche.keys()) or "KEINE")

        res = crawle_gemeinde(g, TEST_PFADE)
        if not res:
            print("   ERGEBNIS: nichts Brauchbares gefunden  ✗\n")
            bestanden = False
            continue
        txt = res["inhalt"]
        hat_pdf = res["pdf_bytes"] is not None
        if hat_pdf:
            pdfs_total += 1
        bau_woerter = sum(w in txt.lower() for w in
                          ("bau", "sanier", "projekt", "neubau", "beschluss", "gemeinderat",
                           "widmung", "kundmachung", "vergabe", "spatenstich"))
        print(f"   Quelle: {res['quelle_url']}")
        print(f"   Inhalt: {len(txt)} Zeichen | PDF geladen: {hat_pdf} ({res['pdf_url'][:70]})")
        print(f"   Bau-/Verwaltungs-Begriffe im Text: {bau_woerter}")
        print(f"   Auszug: …{txt[200:360]}…")
        ok = bool(bereiche) and len(txt) > 300
        print("   ERGEBNIS:", "OK ✓" if ok else "schwach ✗", "\n")
        bestanden = bestanden and ok

    if pdfs_total < 1:
        print("   [!] Kein einziges PDF über den Handler geladen – Dokument-Erkennung prüfen.")
        bestanden = False

    print("=== ENDE Crawler-Selbsttest:", "BESTANDEN ✓" if bestanden else "PROBLEM ✗", "===")
    sys.exit(0 if bestanden else 1)
