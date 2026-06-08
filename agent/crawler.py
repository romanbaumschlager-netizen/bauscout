# =============================================================================
# ProjectScout – Crawler-Modul (agent/crawler.py)
#
# Pro Gemeinde werden MEHRERE relevante Bereiche gesucht und ausgelesen:
#   - Protokolle   (Gemeinderats-/Sitzungsprotokolle)
#   - Amtstafel    (Kundmachungen, Bauverhandlungen, Edikte)
#   - Bau          (Bauamt/Bauabteilung, Flaechenwidmung, Bebauungsplan)
#   - Aktuelles    (Neuigkeiten, Verordnungen, Mitteilungen)
# Von jedem Bereich werden Inhaltstext UND verlinkte Dokumente (PDFs) geladen.
# Die KI-Analyse (Haiku) passiert danach im agent.py – dieses Modul BESCHAFFT nur.
#
# Robust gegen das haeufigste AT-Gemeinde-CMS (RiS-Kommunal/gem2go, ASP.NET):
#   * Dokumente laufen ueber Handler wie GetDocument.ashx?fileId=... (ohne ".pdf")
#     -> Erkennung per URL-Muster + Content-Type/%PDF-Magic-Bytes.
#   * Protokolle liegen oft eine Ebene tiefer auf Jahres-Seiten
#     (sitzungsprotokoll.aspx?typid=2025) -> diese werden gezielt verfolgt.
#   * Dokument-/Jahres-Links werden aus der GANZEN Seite gesucht (das Menue
#     erzeugt keine GetDocument-/Jahres-Treffer), der Inhaltstext dagegen ohne
#     das Mega-Navigationsmenue extrahiert.
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
MAX_TEXT_PRO_BEREICH       = 6_000       # Seitentext je Bereich begrenzen
MAX_DOK_PRO_BEREICH        = 3           # je Bereich max. 3 Dokumente laden
MAX_DOK_GESAMT             = 6           # je Gemeinde max. 6 Dokumente gesamt
MAX_KANDIDATEN_PRO_SEITE   = 6           # so viele Dok-Links je Seite auf PDF pruefen
MAX_JAHRESSEITEN           = 2           # so viele Jahres-/Archivseiten je Bereich folgen

# Themen-Bereiche, Schluesselwoerter STARK -> SCHWACH geordnet (fuer gewichtete Zuordnung).
BEREICH_KEYWORDS = {
    "protokoll": ("protokoll", "sitzungsprotokoll", "niederschrift", "verhandlungsschrift",
                  "sitzungsbericht", "gemeinderatssitzung", "ratsprotokoll", "tagesordnung",
                  "sitzung", "gemeinderat", "stadtrat"),
    "amtstafel": ("amtstafel", "kundmachung", "verlautbarung", "ediktal", "edikt",
                  "anschlagtafel"),
    "bau":       ("bauen & wohnen", "bauen und wohnen", "bauabteilung", "bauamt",
                  "bauvorhaben", "bauverhandlung", "flächenwidmung", "flachenwidmung",
                  "bebauungsplan", "raumordnung", "raumplanung", "ortsbildplanung"),
    "aktuelles": ("neuigkeit", "aktuelles", "verordnung", "mitteilung", "gemeindenews",
                  "news"),
}

# Hinweis-Woerter, die ein Dokument als relevant kennzeichnen (Text/URL).
DOK_HINWEISE = ("protokoll", "sitzung", "niederschrift", "verhandlungsschrift", "gemeinderat",
                "gr-", "gr_", "kundmachung", "bescheid", "verordnung", "bauverhandlung",
                "edikt", "tagesordnung", "dokument", "pdf", "datei", "download", "anhang",
                "beilage", "widmung", "bebauungsplan")

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
RE_NUR_JAHR   = re.compile(r"^20\d{2}$")
RE_JAHR_PARAM = re.compile(r"(typid|jahr|year)=20\d{2}")


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


# ── HTML-Parsing ─────────────────────────────────────────────────────────────
def _soup(html: str):
    if not html or BeautifulSoup is None:
        return None
    try:
        return BeautifulSoup(html, "html.parser")
    except Exception:
        return None


def _content_node(suppe):
    """Best-effort Inhalts-Container (ohne leere Sprungmarken). Sonst body."""
    if suppe is None:
        return None
    kand = []
    for attrs in ({"id": "content"}, {"id": "inhalt"}, {"id": "maincontent"},
                  {"id": "main"}, {"role": "main"}):
        kand += suppe.find_all(attrs=attrs)
    try:
        kand += suppe.find_all(id=re.compile(r"(content|inhalt|maincontent)$", re.I))
        kand += suppe.find_all(class_=re.compile(r"(content|inhalt|maincontent|main-content)", re.I))
    except Exception:
        pass
    m = suppe.find("main")
    if m is not None:
        kand.append(m)
    for node in kand:
        try:
            if len(node.get_text(" ", strip=True)) > 120 or len(node.find_all("a", href=True)) >= 2:
                return node
        except Exception:
            continue
    return suppe.body or suppe


def _text_aus_html(html: str) -> str:
    """Sichtbarer Text – Skripte, Navigation und das Mega-Menue werden entfernt.
    WICHTIG: <form> wird NICHT entfernt (ASP.NET/gem2go verpackt alles in ein form)."""
    suppe = _soup(html)
    if suppe is not None:
        node = _content_node(suppe)
        try:
            for tag in node.find_all(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            for liste in node.find_all(["ul", "ol"]):
                if len(liste.find_all("a")) > 25:   # Mega-Navigationsmenue
                    liste.decompose()
            for el in node.find_all(attrs={"role": "navigation"}):
                el.decompose()
        except Exception:
            pass
        txt = node.get_text(" ", strip=True)
        return re.sub(r"\s+", " ", txt).strip()
    txt = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", txt).strip()


def _alle_links(basis_url: str, html: str):
    """Alle Links der GANZEN Seite (Text, absolute_url)."""
    suppe = _soup(html)
    out = []
    if suppe is not None:
        try:
            for a in suppe.find_all("a", href=True):
                text = (a.get_text(" ") or "").strip()
                out.append((text, urljoin(basis_url, a["href"])))
            return out
        except Exception:
            pass
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html or "", re.I):
        out.append(("", urljoin(basis_url, m.group(1))))
    return out


# ── Datum / Klassifizierung / Dokument-Erkennung ─────────────────────────────
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


def _klassifiziere_link(text: str, url: str):
    """Ordnet einen Link einem Bereich zu und gibt (typ, staerke) zurueck oder None.
    staerke ist hoeher fuer spezifischere Schluesselwoerter (Protokolle > Gemeinderat)."""
    s = (text + " " + url).lower()
    bester = None
    for typ, kws in BEREICH_KEYWORDS.items():
        for i, kw in enumerate(kws):
            if kw in s:
                staerke = len(kws) - i
                if bester is None or staerke > bester[1]:
                    bester = (typ, staerke)
                break
    return bester


def _ist_dokument_link(text: str, url: str) -> bool:
    """Vorfilter: koennte dieser Link ein Dokument (PDF) sein? (Bilder/Menue raus.)
    Endgueltige Pruefung macht _lade_pdf ueber den Content-Type."""
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


def _ist_jahr_oder_archiv(text: str, url: str) -> bool:
    """Erkennt Jahres-/Archiv-Unterseiten (z. B. sitzungsprotokoll.aspx?typid=2025)."""
    t = (text or "").strip().lower()
    u = (url or "").lower()
    if RE_NUR_JAHR.match(t):
        return True
    if "archiv" in t or "archiv" in u:
        return True
    if RE_JAHR_PARAM.search(u):
        return True
    if "sitzungsprotokoll" in u:
        return True
    return False


def _lade_pdf(url: str):
    """Laedt eine URL und gibt die Bytes zurueck, WENN es ein echtes PDF ist
    (Content-Type application/pdf ODER %PDF-Magic-Bytes). Sonst None."""
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
    """Findet pro Themen-Typ die beste (spezifischste) Bereichsseite.
    gem2go rendert das ganze Menue in jeder Seite -> meist reicht die Startseite,
    sonst 1 Ebene tiefer ueber 'Eltern'-Menuepunkte."""
    gefunden = {}   # typ -> (url, staerke)

    def scanne(html, quelle_url):
        for text, url in _alle_links(quelle_url, html):
            kl = _klassifiziere_link(text, url)
            if kl is None:
                continue
            typ, staerke = kl
            u = url.split("#")[0]
            if typ not in gefunden or staerke > gefunden[typ][1]:
                gefunden[typ] = (u, staerke)

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
                if len(gefunden) >= 4 or len(besucht) >= 6:
                    break
    return {typ: u for typ, (u, _st) in gefunden.items()}


def _direkte_dok_urls(seiten_url: str, html: str) -> list:
    """Dokument-Links der Seite (neueste zuerst), als URL-Liste."""
    links = _alle_links(seiten_url, html)
    kand = []
    gesehen = set()
    for idx, (text, url) in enumerate(links):
        u = url.split("#")[0]
        if u in gesehen:
            continue
        if _ist_dokument_link(text, u):
            gesehen.add(u)
            kand.append((-_datum_score(text + " " + u), idx, u))
    kand.sort()
    return [u for _, _, u in kand]


def _jahr_urls(seiten_url: str, html: str) -> list:
    """Jahres-/Archiv-Unterseiten der Seite (neuestes Jahr zuerst), als URL-Liste."""
    links = _alle_links(seiten_url, html)
    kand = []
    gesehen = set()
    selbst = seiten_url.split("#")[0]
    for idx, (text, url) in enumerate(links):
        u = url.split("#")[0]
        if u in gesehen or u == selbst:
            continue
        if _ist_jahr_oder_archiv(text, url):
            gesehen.add(u)
            kand.append((-_datum_score(text + " " + url), idx, u))
    kand.sort()
    return [u for _, _, u in kand]


def _dokumente_einer_seite(seiten_url: str, html: str, limit: int = MAX_DOK_PRO_BEREICH) -> list:
    """Laedt Dokumente einer Bereichsseite. Findet direkte PDFs; falls zu wenige,
    folgt den neuesten Jahres-/Archivseiten und laedt dort. -> [(url, pdf_bytes), ...]"""
    dok = []
    geladen = set()

    def lade(urls):
        for u in urls[:MAX_KANDIDATEN_PRO_SEITE]:
            if len(dok) >= limit:
                break
            if u in geladen:
                continue
            geladen.add(u)
            pdf = _lade_pdf(u)
            if pdf:
                dok.append((u, pdf))

    lade(_direkte_dok_urls(seiten_url, html))
    if len(dok) < limit:
        for ju in _jahr_urls(seiten_url, html)[:MAX_JAHRESSEITEN]:
            r = _hole(ju)
            if r is None:
                continue
            lade(_direkte_dok_urls(ju, r.text))
            if len(dok) >= limit:
                break
    return dok[:limit]


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
    if "protokoll" not in bereiche:
        for pfad in (protokoll_pfade or [])[:10]:
            ziel = urljoin(basis, pfad)
            r = _hole(ziel)
            if r is not None and len(r.text) > 400:
                bereiche["protokoll"] = ziel
                break

    bezirk = (gemeinde.get("bezirk") or "").strip()
    bundesland = (gemeinde.get("bundesland") or "").strip()

    if not bereiche:
        if not start_html:
            return None
        text = _text_aus_html(start_html)[:MAX_TEXT_ZEICHEN]
        if len(text) < 80:
            return None
        return {"gemeinde": name, "bezirk": bezirk, "bundesland": bundesland,
                "quelle_url": basis, "inhalt": text, "pdf_bytes": None, "pdf_url": "",
                "inhalt_hash": hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]}

    text_teile = []
    pdf_text_teile = []
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
        seiten_text = _text_aus_html(seiten_html)[:MAX_TEXT_PRO_BEREICH]
        if seiten_text:
            text_teile.append(f"[{typ.upper()}] {seiten_text}")
        if dok_gesamt < MAX_DOK_GESAMT:
            rest = MAX_DOK_GESAMT - dok_gesamt
            for durl, pdf in _dokumente_einer_seite(seiten_url, seiten_html,
                                                    limit=min(MAX_DOK_PRO_BEREICH, rest)):
                ptext = _pdf_text(pdf)
                if ptext:
                    pdf_text_teile.append(ptext)
                if erstes_pdf_bytes is None:
                    erstes_pdf_bytes = pdf
                    erstes_pdf_url = durl
                dok_gesamt += 1

    # PDF-Text zuerst (wertvoller), dann Seitentext – dann hart begrenzen.
    inhalt = re.sub(r"\s+", " ", " ".join(pdf_text_teile + text_teile)).strip()[:MAX_TEXT_ZEICHEN]
    if len(inhalt) < 80 and erstes_pdf_bytes is None:
        return None

    return {
        "gemeinde": name,
        "bezirk": bezirk,
        "bundesland": bundesland,
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


# ── CMS-Erkennung (für Breitentest) ──────────────────────────────────────────
def _cms_signatur(html: str) -> str:
    """Grobe CMS-Erkennung: 'gem2go' (RiS-Kommunal) vs. 'andere'."""
    h = (html or "").lower()
    if not h:
        return "-"
    marker = ("getimage.ashx", "getdocument.ashx", "menuonr=", "riskommunal",
              "gem2go", "ris-kommunal", "/system/web/")
    return "gem2go" if any(m in h for m in marker) else "andere"


# ── Breitentest über ganz Österreich (read-only, ohne KI/Supabase/Stripe) ────
# Aufruf:  python agent/crawler.py
# Nimmt eine ueber ALLE 9 Bundeslaender gleichmaessig gestreute Stichprobe aus der
# echten Gemeinde-Datenbank, erkennt je Gemeinde das CMS, sucht die Bereiche und
# prueft, ob mindestens ein PDF ladbar ist -> zeigt, ob die generische Logik
# bundesweit/CMS-uebergreifend traegt. Stichprobengroesse je Bundesland via
# Umgebungsvariable BREITE_PRO_BL (Default 4).
if __name__ == "__main__":
    import os
    import sys
    from datetime import datetime
    from collections import defaultdict

    try:
        from gemeinden_datenbank import GEMEINDEN
    except Exception as ex:
        print("FEHLER: gemeinden_datenbank nicht importierbar:", ex)
        sys.exit(1)

    def _probe_gemeinde(gemeinde):
        url = (gemeinde.get("url") or "").strip()
        res = {"name": gemeinde.get("name", ""), "bl": gemeinde.get("bundesland", ""),
               "url": url, "erreichbar": False, "cms": "-", "bereiche": 0, "pdf": False}
        if (not url) or (not url.lower().startswith("http")) or (" " in url) or ("none" in url.lower()):
            res["cms"] = "URL kaputt"
            return res
        p = urlparse(url)
        basis = f"{p.scheme}://{p.netloc}"
        r = _hole(basis)
        if r is None:
            return res
        res["erreichbar"] = True
        html = r.text
        res["cms"] = _cms_signatur(html)
        ber = _finde_bereichsseiten(basis, html)
        res["bereiche"] = len(ber)
        versuche = 0
        for typ in ("amtstafel", "bau", "protokoll"):   # schnelle (direkte) zuerst
            if typ not in ber:
                continue
            rr = _hole(ber[typ])
            if rr is None:
                continue
            if _dokumente_einer_seite(ber[typ], rr.text, limit=1):
                res["pdf"] = True
                break
            versuche += 1
            if versuche >= 2:
                break
        return res

    K = int(os.environ.get("BREITE_PRO_BL", "4"))
    print("ProjectScout – CRAWLER-BREITENTEST ueber ganz Oesterreich (read-only)")
    print(f"Zeitpunkt: {datetime.now().isoformat()} | {K} Gemeinden je Bundesland\n")

    stichprobe = []
    for bl, liste in GEMEINDEN.items():
        if not liste:
            continue
        L = len(liste)
        idxs = sorted(set((i * L) // K for i in range(K)))
        for i in idxs:
            gm = dict(liste[i])
            gm["bundesland"] = bl
            stichprobe.append(gm)

    ergebnisse = []
    for gm in stichprobe:
        r = _probe_gemeinde(gm)
        ergebnisse.append(r)
        flag = "OK" if (r["erreichbar"] and r["bereiche"] >= 1) else "  "
        pdf = "PDF" if r["pdf"] else "   "
        print(f"  [{flag} {pdf}] {r['bl']:4s} {r['name'][:32]:32s} | "
              f"err={int(r['erreichbar'])} cms={r['cms']:10s} "
              f"bereiche={r['bereiche']} pdf={int(r['pdf'])}")

    per_bl = defaultdict(lambda: {"n": 0, "err": 0, "ber": 0, "pdf": 0})
    cms_z = defaultdict(int)
    andere = {"n": 0, "err": 0, "ber": 0, "pdf": 0}
    probleme = []
    for r in ergebnisse:
        b = per_bl[r["bl"]]
        b["n"] += 1
        b["err"] += int(r["erreichbar"])
        b["ber"] += int(r["bereiche"] >= 1)
        b["pdf"] += int(r["pdf"])
        cms_z[r["cms"]] += 1
        if r["cms"] == "andere":
            andere["n"] += 1
            andere["err"] += int(r["erreichbar"])
            andere["ber"] += int(r["bereiche"] >= 1)
            andere["pdf"] += int(r["pdf"])
        if r["erreichbar"] and r["bereiche"] == 0:
            probleme.append(r)

    print("\n=== MATRIX je Bundesland (erreichbar / mit Bereichen / mit PDF von n) ===")
    for bl in ("W", "NOE", "OOE", "SBG", "STK", "KTN", "TIR", "VBG", "BGR"):
        if bl in per_bl:
            b = per_bl[bl]
            print(f"  {bl:4s}: erreichbar {b['err']}/{b['n']} | Bereiche {b['ber']}/{b['n']} | PDF {b['pdf']}/{b['n']}")

    n = len(ergebnisse)
    err = sum(int(r["erreichbar"]) for r in ergebnisse)
    ber = sum(int(r["bereiche"] >= 1) for r in ergebnisse)
    pdf = sum(int(r["pdf"]) for r in ergebnisse)
    print(f"\n=== GESAMT ({n} Gemeinden) ===")
    print(f"  erreichbar:     {err}/{n} ({100*err//max(n,1)} %)")
    print(f"  mit Bereichen:  {ber}/{n} ({100*ber//max(n,1)} %)")
    print(f"  mit PDF:        {pdf}/{n} ({100*pdf//max(n,1)} %)")
    print("  CMS-Verteilung: " + ", ".join(f"{k}={v}" for k, v in sorted(cms_z.items(), key=lambda x: -x[1])))
    if andere["n"]:
        print(f"\n  >>> NICHT-gem2go ({andere['n']} Gemeinden) – die eigentliche Generalisierungsfrage:")
        print(f"      erreichbar {andere['err']}/{andere['n']} | Bereiche {andere['ber']}/{andere['n']} | PDF {andere['pdf']}/{andere['n']}")

    if probleme:
        print(f"\n  Erreichbar, aber KEINE Bereiche erkannt ({len(probleme)}):")
        for r in probleme[:25]:
            print(f"     - {r['bl']} {r['name'][:30]} ({r['cms']}) {r['url'][:55]}")

    erreichbare = max(err, 1)
    print("\n=== VERDIKT ===")
    if err >= 0.6 * n and (ber / erreichbare) >= 0.5:
        print(f"  Generische Logik traegt breit: {ber}/{err} erreichbarer Seiten liefern Bereiche.")
        sys.exit(0)
    else:
        print(f"  Auffaellig: nur {ber}/{err} erreichbarer Seiten mit Bereichen – bitte Stichprobe pruefen.")
        sys.exit(1)
