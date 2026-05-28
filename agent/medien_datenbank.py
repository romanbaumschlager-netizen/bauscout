# =============================================================================
# ProjectScout – Österreichische Medien-Datenbank
# Alle Quellen geordnet nach Bundesland + überregionale Quellen
# Jeder Eintrag: { "name": ..., "url": ..., "bundeslaender": [...], "typ": ... }
#
# Typen:
#   "bundesweit"   – erscheint österreichweit
#   "regional"     – erscheint in 1–3 Bundesländern
#   "lokal"        – Bezirks- oder Gemeindezeitung
#   "online"       – rein digitales Medium
#   "oeffentlich"  – ORF und öffentl. Medien
#
# Bundesland-Kürzel:
#   W  = Wien
#   NOE = Niederösterreich
#   OOE = Oberösterreich
#   SBG = Salzburg
#   STK = Steiermark
#   KTN = Kärnten
#   TIR = Tirol
#   VBG = Vorarlberg
#   BGR = Burgenland
# =============================================================================

MEDIEN = [

    # =========================================================================
    # ÜBERREGIONAL / BUNDESWEIT
    # =========================================================================
    {
        "name": "Kronen Zeitung",
        "url": "https://www.krone.at",
        "suchpfad": "https://www.krone.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "bundesweit"
    },
    {
        "name": "Kurier",
        "url": "https://kurier.at",
        "suchpfad": "",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "bundesweit"
    },
    {
        "name": "Der Standard",
        "url": "https://www.derstandard.at",
        "suchpfad": "https://www.derstandard.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "bundesweit"
    },
    {
        "name": "Die Presse",
        "url": "https://www.diepresse.com",
        "suchpfad": "https://www.diepresse.com/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "bundesweit"
    },
    {
        "name": "Österreich / oe24",
        "url": "https://www.oe24.at",
        "suchpfad": "",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "bundesweit"
    },
    {
        "name": "Heute",
        "url": "https://www.heute.at",
        "suchpfad": "",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "bundesweit"
    },

    # =========================================================================
    # ORF – Öffentlich-rechtlich (bundesweit + 9 Landesstudios)
    # =========================================================================
    {
        "name": "ORF.at",
        "url": "https://orf.at",
        "suchpfad": "",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "oeffentlich"
    },
    {
        "name": "ORF Wien",
        "url": "https://wien.orf.at",
        "suchpfad": "",
        "bundeslaender": ["W"],
        "typ": "oeffentlich"
    },
    {
        "name": "ORF Niederösterreich",
        "url": "https://noe.orf.at",
        "suchpfad": "",
        "bundeslaender": ["NOE"],
        "typ": "oeffentlich"
    },
    {
        "name": "ORF Oberösterreich",
        "url": "https://ooe.orf.at",
        "suchpfad": "",
        "bundeslaender": ["OOE"],
        "typ": "oeffentlich"
    },
    {
        "name": "ORF Salzburg",
        "url": "https://salzburg.orf.at",
        "suchpfad": "",
        "bundeslaender": ["SBG"],
        "typ": "oeffentlich"
    },
    {
        "name": "ORF Steiermark",
        "url": "https://steiermark.orf.at",
        "suchpfad": "",
        "bundeslaender": ["STK"],
        "typ": "oeffentlich"
    },
    {
        "name": "ORF Kärnten",
        "url": "https://kaernten.orf.at",
        "suchpfad": "",
        "bundeslaender": ["KTN"],
        "typ": "oeffentlich"
    },
    {
        "name": "ORF Tirol",
        "url": "https://tirol.orf.at",
        "suchpfad": "",
        "bundeslaender": ["TIR"],
        "typ": "oeffentlich"
    },
    {
        "name": "ORF Vorarlberg",
        "url": "https://vorarlberg.orf.at",
        "suchpfad": "",
        "bundeslaender": ["VBG"],
        "typ": "oeffentlich"
    },
    {
        "name": "ORF Burgenland",
        "url": "https://burgenland.orf.at",
        "suchpfad": "",
        "bundeslaender": ["BGR"],
        "typ": "oeffentlich"
    },

    # =========================================================================
    # MEINBEZIRK.AT – RMA Plattform (Bundeslandfilter möglich!)
    # =========================================================================
    {
        "name": "meinBezirk.at (gesamt)",
        "url": "https://www.meinbezirk.at",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "regional"
    },
    {
        "name": "meinBezirk Wien",
        "url": "https://www.meinbezirk.at/wien",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["W"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Niederösterreich",
        "url": "https://www.meinbezirk.at/niederoesterreich",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Oberösterreich",
        "url": "https://www.meinbezirk.at/oberoesterreich",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Salzburg",
        "url": "https://www.meinbezirk.at/salzburg",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["SBG"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Steiermark",
        "url": "https://www.meinbezirk.at/steiermark",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Kärnten",
        "url": "https://www.meinbezirk.at/kaernten",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Tirol",
        "url": "https://www.meinbezirk.at/tirol",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Vorarlberg",
        "url": "https://www.meinbezirk.at/vorarlberg",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["VBG"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Burgenland",
        "url": "https://www.meinbezirk.at/burgenland",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["BGR"],
        "typ": "lokal"
    },

    # =========================================================================
    # WIEN
    # =========================================================================
    {
        "name": "Wiener Zeitung",
        "url": "https://www.wienerzeitung.at",
        "suchpfad": "https://www.wienerzeitung.at/suche/?q=",
        "bundeslaender": ["W"],
        "typ": "regional"
    },
    {
        "name": "bz-Wiener Bezirkszeitung",
        "url": "https://www.meinbezirk.at/wien",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["W"],
        "typ": "lokal"
    },
    {
        "name": "Falter",
        "url": "https://www.falter.at",
        "suchpfad": "https://www.falter.at/suche/?q=",
        "bundeslaender": ["W"],
        "typ": "regional"
    },
    {
        "name": "Vienna Online",
        "url": "https://www.vienna.at",
        "suchpfad": "https://www.vienna.at/suche?q=",
        "bundeslaender": ["W"],
        "typ": "online"
    },

    # =========================================================================
    # NIEDERÖSTERREICH
    # =========================================================================
    {
        "name": "NÖN – Niederösterreichische Nachrichten",
        "url": "https://www.noen.at",
        "suchpfad": "https://www.noen.at/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "regional"
    },
    {
        "name": "NÖN Amstetten",
        "url": "https://www.noen.at/amstetten",
        "suchpfad": "https://www.noen.at/amstetten/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Baden",
        "url": "https://www.noen.at/region/baden",
        "suchpfad": "https://www.noen.at/region/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Bruck/Leitha",
        "url": "https://www.noen.at/bruck-an-der-leitha",
        "suchpfad": "https://www.noen.at/bruck-an-der-leitha/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Gänserndorf",
        "url": "https://www.noen.at/gaenserndorf",
        "suchpfad": "https://www.noen.at/gaenserndorf/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Gmünd",
        "url": "https://www.noen.at/gmünd",
        "suchpfad": "https://www.noen.at/gmuend/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Hollabrunn",
        "url": "https://www.noen.at/hollabrunn",
        "suchpfad": "https://www.noen.at/hollabrunn/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Horn",
        "url": "https://www.noen.at/horn",
        "suchpfad": "https://www.noen.at/horn/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Klosterneuburg",
        "url": "https://www.noen.at/klosterneuburg",
        "suchpfad": "https://www.noen.at/klosterneuburg/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Korneuburg",
        "url": "https://www.noen.at/korneuburg",
        "suchpfad": "https://www.noen.at/korneuburg/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Krems",
        "url": "https://www.noen.at/krems",
        "suchpfad": "https://www.noen.at/krems/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Lilienfeld",
        "url": "https://www.noen.at/lilienfeld",
        "suchpfad": "https://www.noen.at/lilienfeld/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Melk",
        "url": "https://www.noen.at/melk",
        "suchpfad": "https://www.noen.at/melk/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Mistelbach",
        "url": "https://www.noen.at/mistelbach",
        "suchpfad": "https://www.noen.at/mistelbach/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Mödling",
        "url": "https://www.noen.at/moedling",
        "suchpfad": "https://www.noen.at/moedling/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Neunkirchen",
        "url": "https://www.noen.at/neunkirchen",
        "suchpfad": "https://www.noen.at/neunkirchen/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN St. Pölten",
        "url": "https://www.noen.at/st-poelten",
        "suchpfad": "https://www.noen.at/st-poelten/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Tulln",
        "url": "https://www.noen.at/tulln",
        "suchpfad": "https://www.noen.at/tulln/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Waidhofen/Thaya",
        "url": "https://www.noen.at/waidhofen-thaya",
        "suchpfad": "https://www.noen.at/waidhofen-thaya/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Wiener Neustadt",
        "url": "https://www.noen.at/wr-neustadt",
        "suchpfad": "https://www.noen.at/wr-neustadt/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },
    {
        "name": "NÖN Zwettl",
        "url": "https://www.noen.at/zwettl",
        "suchpfad": "https://www.noen.at/zwettl/suche?q=",
        "bundeslaender": ["NOE"],
        "typ": "lokal"
    },

    # =========================================================================
    # OBERÖSTERREICH
    # =========================================================================
    {
        "name": "Oberösterreichische Nachrichten (OÖN)",
        "url": "https://www.nachrichten.at",
        "suchpfad": "https://www.nachrichten.at/suche/?q=",
        "bundeslaender": ["OOE"],
        "typ": "regional"
    },
    {
        "name": "BezirksRundSchau OÖ (gesamt)",
        "url": "https://www.meinbezirk.at/oberoesterreich",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "regional"
    },
    {
        "name": "BezirksRundSchau Braunau",
        "url": "https://www.meinbezirk.at/braunau-am-inn",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Eferding",
        "url": "https://www.meinbezirk.at/grieskirchen-eferding",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Freistadt",
        "url": "https://www.meinbezirk.at/freistadt",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Gmunden",
        "url": "https://www.meinbezirk.at/gmunden",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Grieskirchen",
        "url": "https://www.meinbezirk.at/grieskirchen-eferding",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Kirchdorf",
        "url": "https://www.meinbezirk.at/kirchdorf-an-der-krems",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Linz-Land",
        "url": "https://www.meinbezirk.at/linz-land",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Linz Stadt",
        "url": "https://www.meinbezirk.at/linz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Perg",
        "url": "https://www.meinbezirk.at/perg",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Ried im Innkreis",
        "url": "https://www.meinbezirk.at/ried-im-innkreis",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Rohrbach",
        "url": "https://www.meinbezirk.at/rohrbach",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Schärding",
        "url": "https://www.meinbezirk.at/schaerding",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Steyr",
        "url": "https://www.meinbezirk.at/steyr",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Urfahr-Umgebung",
        "url": "https://www.meinbezirk.at/urfahr-umgebung",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Vöcklabruck",
        "url": "https://www.meinbezirk.at/voecklabruck",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "BezirksRundSchau Wels",
        "url": "https://www.meinbezirk.at/wels",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "Tips OÖ (18 Regionalausgaben)",
        "url": "https://www.tips.at",
        "suchpfad": "https://www.tips.at/suche?q=",
        "bundeslaender": ["OOE"],
        "typ": "regional"
    },
    {
        "name": "Linz Aktuell (Stadt Linz)",
        "url": "https://www.linz.at",
        "suchpfad": "https://www.linz.at/suche.asp?q=",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Wels",
        "url": "https://www.meinbezirk.at/wels",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["OOE"],
        "typ": "lokal"
    },

    # =========================================================================
    # SALZBURG
    # =========================================================================
    {
        "name": "Salzburger Nachrichten",
        "url": "https://www.sn.at",
        "suchpfad": "https://www.sn.at/suche?q=",
        "bundeslaender": ["SBG"],
        "typ": "regional"
    },
    {
        "name": "BezirksBlätter Salzburg (gesamt)",
        "url": "https://www.meinbezirk.at/salzburg",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["SBG"],
        "typ": "regional"
    },
    {
        "name": "BezirksBlätter Hallein",
        "url": "https://www.meinbezirk.at/tennengau",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["SBG"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Salzburg Stadt",
        "url": "https://www.meinbezirk.at/salzburg-stadt",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["SBG"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Salzburg Umgebung",
        "url": "https://www.meinbezirk.at/flachgau",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["SBG"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter St. Johann/Pongau",
        "url": "https://www.meinbezirk.at/pongau",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["SBG"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Tamsweg/Lungau",
        "url": "https://www.meinbezirk.at/lungau",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["SBG"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Zell am See/Pinzgau",
        "url": "https://www.meinbezirk.at/pinzgau",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["SBG"],
        "typ": "lokal"
    },

    # =========================================================================
    # STEIERMARK
    # =========================================================================
    {
        "name": "Kleine Zeitung Steiermark",
        "url": "https://www.kleinezeitung.at/steiermark",
        "suchpfad": "https://www.kleinezeitung.at/suche?q=",
        "bundeslaender": ["STK"],
        "typ": "regional"
    },
    {
        "name": "Woche Steiermark (gesamt)",
        "url": "https://www.meinbezirk.at/steiermark",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "regional"
    },
    {
        "name": "Woche Graz",
        "url": "https://www.meinbezirk.at/graz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Bruck-Mürzzuschlag",
        "url": "https://www.meinbezirk.at/bruck-muerzzuschlag",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Deutschlandsberg",
        "url": "https://www.meinbezirk.at/deutschlandsberg",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Feldbach/Südoststeiermark",
        "url": "https://www.meinbezirk.at/suedoststeiermark",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Fürstenfeld",
        "url": "https://www.meinbezirk.at/hartberg-fuerstenfeld",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Judenburg/Murtal",
        "url": "https://www.meinbezirk.at/murtal",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Leibnitz",
        "url": "https://www.meinbezirk.at/leibnitz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Leoben",
        "url": "https://www.meinbezirk.at/leoben",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Liezen",
        "url": "https://www.meinbezirk.at/liezen",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Murau",
        "url": "https://www.meinbezirk.at/murau",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Voitsberg",
        "url": "https://www.meinbezirk.at/voitsberg",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Weiz",
        "url": "https://www.meinbezirk.at/weiz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "Woche Zeltweg",
        "url": "https://www.meinbezirk.at/murtal",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },
    {
        "name": "steirerkrone.at",
        "url": "https://www.krone.at/steiermark",
        "suchpfad": "https://www.krone.at/suche?q=steiermark+",
        "bundeslaender": ["STK"],
        "typ": "regional"
    },
    {
        "name": "Grazer Woche",
        "url": "https://www.meinbezirk.at/graz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["STK"],
        "typ": "lokal"
    },

    # =========================================================================
    # KÄRNTEN
    # =========================================================================
    {
        "name": "Kleine Zeitung Kärnten",
        "url": "https://www.kleinezeitung.at/kaernten",
        "suchpfad": "https://www.kleinezeitung.at/suche?q=",
        "bundeslaender": ["KTN"],
        "typ": "regional"
    },
    {
        "name": "Woche Kärnten (gesamt)",
        "url": "https://www.meinbezirk.at/kaernten",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "regional"
    },
    {
        "name": "Woche Klagenfurt",
        "url": "https://www.meinbezirk.at/klagenfurt",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },
    {
        "name": "Woche Villach",
        "url": "https://www.meinbezirk.at/villach",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },
    {
        "name": "Woche Hermagor/Gailtal",
        "url": "https://www.meinbezirk.at/hermagor",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },
    {
        "name": "Woche Spittal/Drau",
        "url": "https://www.meinbezirk.at/spittal-an-der-drau",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },
    {
        "name": "Woche St. Veit/Glan",
        "url": "https://www.meinbezirk.at/st-veit-an-der-glan",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },
    {
        "name": "Woche Völkermarkt",
        "url": "https://www.meinbezirk.at/voelkermarkt",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },
    {
        "name": "Woche Wolfsberg/Lavanttal",
        "url": "https://www.meinbezirk.at/wolfsberg",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },
    {
        "name": "Woche Feldkirchen",
        "url": "https://www.meinbezirk.at/feldkirchen",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Völkermarkt",
        "url": "https://www.meinbezirk.at/voelkermarkt",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["KTN"],
        "typ": "lokal"
    },

    # =========================================================================
    # TIROL
    # =========================================================================
    {
        "name": "Tiroler Tageszeitung (TT)",
        "url": "https://www.tt.com",
        "suchpfad": "https://www.tt.com/suche?q=",
        "bundeslaender": ["TIR"],
        "typ": "regional"
    },
    {
        "name": "Tiroler Krone",
        "url": "https://www.krone.at/tirol",
        "suchpfad": "https://www.krone.at/suche?q=tirol+",
        "bundeslaender": ["TIR"],
        "typ": "regional"
    },
    {
        "name": "BezirksBlätter Tirol (gesamt)",
        "url": "https://www.meinbezirk.at/tirol",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "regional"
    },
    {
        "name": "BezirksBlätter Innsbruck",
        "url": "https://www.meinbezirk.at/innsbruck",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Innsbruck-Land",
        "url": "https://www.meinbezirk.at/innsbruck-land",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Imst",
        "url": "https://www.meinbezirk.at/imst",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Kitzbühel",
        "url": "https://www.meinbezirk.at/kitzbuehel",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Kufstein",
        "url": "https://www.meinbezirk.at/kufstein",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Landeck",
        "url": "https://www.meinbezirk.at/landeck",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Lienz/Osttirol",
        "url": "https://www.meinbezirk.at/lienz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Reutte",
        "url": "https://www.meinbezirk.at/reutte",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Schwaz",
        "url": "https://www.meinbezirk.at/schwaz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },
    {
        "name": "meinBezirk Schwaz/Zillertal",
        "url": "https://www.meinbezirk.at/schwaz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["TIR"],
        "typ": "lokal"
    },

    # =========================================================================
    # VORARLBERG
    # =========================================================================
    {
        "name": "Vorarlberger Nachrichten (VN)",
        "url": "https://www.vn.at",
        "suchpfad": "https://www.vn.at/suche?q=",
        "bundeslaender": ["VBG"],
        "typ": "regional"
    },
    {
        "name": "Neue Vorarlberger Tageszeitung",
        "url": "https://www.neue.at",
        "suchpfad": "https://www.neue.at/suche?q=",
        "bundeslaender": ["VBG"],
        "typ": "regional"
    },
    {
        "name": "Regionalzeitung Vorarlberg (gesamt)",
        "url": "https://www.meinbezirk.at/vorarlberg",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["VBG"],
        "typ": "regional"
    },
    {
        "name": "Regionalzeitung Bludenz",
        "url": "https://www.meinbezirk.at/bludenz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["VBG"],
        "typ": "lokal"
    },
    {
        "name": "Regionalzeitung Bregenz",
        "url": "https://www.meinbezirk.at/bregenz",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["VBG"],
        "typ": "lokal"
    },
    {
        "name": "Regionalzeitung Dornbirn",
        "url": "https://www.meinbezirk.at/dornbirn",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["VBG"],
        "typ": "lokal"
    },
    {
        "name": "Regionalzeitung Feldkirch",
        "url": "https://www.meinbezirk.at/feldkirch",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["VBG"],
        "typ": "lokal"
    },

    # =========================================================================
    # BURGENLAND
    # =========================================================================
    {
        "name": "Burgenländische Volkszeitung (BVZ)",
        "url": "https://www.bvz.at",
        "suchpfad": "https://www.bvz.at/suche?q=",
        "bundeslaender": ["BGR"],
        "typ": "regional"
    },
    {
        "name": "BezirksBlätter Burgenland (gesamt)",
        "url": "https://www.meinbezirk.at/burgenland",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["BGR"],
        "typ": "regional"
    },
    {
        "name": "BezirksBlätter Eisenstadt",
        "url": "https://www.meinbezirk.at/eisenstadt",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["BGR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Güssing",
        "url": "https://www.meinbezirk.at/guessing",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["BGR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Jennersdorf",
        "url": "https://www.meinbezirk.at/jennersdorf",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["BGR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Mattersburg",
        "url": "https://www.meinbezirk.at/mattersburg",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["BGR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Neusiedl am See",
        "url": "https://www.meinbezirk.at/neusiedl-am-see",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["BGR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Oberpullendorf",
        "url": "https://www.meinbezirk.at/oberpullendorf",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["BGR"],
        "typ": "lokal"
    },
    {
        "name": "BezirksBlätter Oberwart",
        "url": "https://www.meinbezirk.at/oberwart",
        "suchpfad": "https://www.meinbezirk.at/tag/",
        "bundeslaender": ["BGR"],
        "typ": "lokal"
    },

    # =========================================================================
    # ONLINE-MEDIEN / BRANCHENPORTALE (bundesweit, für Bauprojekte relevant)
    # =========================================================================
    {
        "name": "bauzeitung.at",
        "url": "https://www.bauzeitung.at",
        "suchpfad": "https://www.bauzeitung.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "online"
    },
    {
        "name": "Immobilien Magazin",
        "url": "https://www.immobilien-magazin.at",
        "suchpfad": "https://www.immobilien-magazin.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "online"
    },
    {
        "name": "Kommunal.at",
        "url": "https://www.kommunal.at",
        "suchpfad": "https://www.kommunal.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "online"
    },
    {
        "name": "Österreichische Gemeindezeitung",
        "url": "https://www.gemeindezeitung.at",
        "suchpfad": "https://www.gemeindezeitung.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "online"
    },
    # === VERGABE & AUSSCHREIBUNGEN (öffentlich, kein Login) ===
    {
        "name": "USP Ausschreibungssuche (Österreich)",
        "url": "https://ausschreibungen.usp.gv.at",
        "suchpfad": "https://ausschreibungen.usp.gv.at/at.gv.bmdw.eproc-p/public/tenderlist?searchText=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "vergabe"
    },
    {
        "name": "OffeneVergaben.at (Open Data)",
        "url": "https://www.offenevergaben.at",
        "suchpfad": "https://www.offenevergaben.at/?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "vergabe"
    },
    {
        "name": "Wirtschaftskammer Ausschreibungsmarkt",
        "url": "https://ausschreibungsmarkt.wko.at",
        "suchpfad": "https://ausschreibungsmarkt.wko.at/Suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "vergabe"
    },
    {
        "name": "Gemeindebund Österreich",
        "url": "https://www.gemeindebund.at",
        "suchpfad": "https://www.gemeindebund.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "kommunal"
    },
    {
        "name": "Land Tirol – Aktuelle Meldungen",
        "url": "https://www.tirol.gv.at",
        "suchpfad": "https://www.tirol.gv.at/suche/?q=",
        "bundeslaender": ["TIR"],
        "typ": "land"
    },
    {
        "name": "Land Steiermark – Meldungen",
        "url": "https://www.steiermark.at",
        "suchpfad": "https://www.steiermark.at/suche?q=",
        "bundeslaender": ["STK"],
        "typ": "land"
    },
    {
        "name": "Land Oberösterreich – Meldungen",
        "url": "https://www.ooe.gv.at",
        "suchpfad": "https://www.ooe.gv.at/suche?q=",
        "bundeslaender": ["OOE"],
        "typ": "land"
    },
    {
        "name": "Land Niederösterreich – Meldungen",
        "url": "https://www.noe.gv.at",
        "suchpfad": "https://www.noe.gv.at/noe/suche.html?q=",
        "bundeslaender": ["NOE"],
        "typ": "land"
    },
    {
        "name": "Land Salzburg – Meldungen",
        "url": "https://www.salzburg.gv.at",
        "suchpfad": "https://www.salzburg.gv.at/suche?q=",
        "bundeslaender": ["SBG"],
        "typ": "land"
    },
    {
        "name": "Land Kärnten – Meldungen",
        "url": "https://www.ktn.gv.at",
        "suchpfad": "https://www.ktn.gv.at/suche?q=",
        "bundeslaender": ["KTN"],
        "typ": "land"
    },
    {
        "name": "Land Vorarlberg – Meldungen",
        "url": "https://www.vorarlberg.at",
        "suchpfad": "https://www.vorarlberg.at/suche?q=",
        "bundeslaender": ["VBG"],
        "typ": "land"
    },
    {
        "name": "Land Burgenland – Meldungen",
        "url": "https://www.burgenland.at",
        "suchpfad": "https://www.burgenland.at/suche?q=",
        "bundeslaender": ["BGR"],
        "typ": "land"
    },
    {
        "name": "Stadt Wien – Ausschreibungen",
        "url": "https://www.wien.gv.at",
        "suchpfad": "https://www.wien.gv.at/suche/?q=",
        "bundeslaender": ["W"],
        "typ": "land"
    },
    {
        "name": "Energieautarkie.at (Energie & PV Projekte)",
        "url": "https://www.energieautarkie.at",
        "suchpfad": "https://www.energieautarkie.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "energie"
    },
    {
        "name": "PV Austria (Photovoltaik Österreich)",
        "url": "https://www.pvaustria.at",
        "suchpfad": "https://www.pvaustria.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "energie"
    },
    # === BAU & ARCHITEKTUR FACHMEDIEN ===
    {
        "name": "a3BAU – Baumagazin Österreich",
        "url": "https://a3bau.at",
        "suchpfad": "https://a3bau.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    {
        "name": "energie-bau.at (Energie & Architektur)",
        "url": "https://www.energie-bau.at",
        "suchpfad": "https://www.energie-bau.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    {
        "name": "Holzmagazin (Holzbau Österreich/DACH)",
        "url": "https://holzmagazin.com",
        "suchpfad": "https://holzmagazin.com/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    {
        "name": "handwerkundbau.at (Haustechnik & Bau)",
        "url": "https://www.handwerkundbau.at",
        "suchpfad": "https://www.handwerkundbau.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    # === GASTRONOMIE & TOURISMUS ===
    {
        "name": "GAST.at – Gastronomie & Hotellerie",
        "url": "https://www.gast.at",
        "suchpfad": "https://www.gast.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    {
        "name": "gastro.at – Österreichisches Gastronomieportal",
        "url": "https://gastroportal.at",
        "suchpfad": "https://gastroportal.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    {
        "name": "ÖHV – Österreichische Hoteliervereinigung",
        "url": "https://www.oehv.at",
        "suchpfad": "https://www.oehv.at/news/?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    # === LANDWIRTSCHAFT & FORST ===
    {
        "name": "LK Österreich – Landwirtschaftskammer",
        "url": "https://www.lko.at",
        "suchpfad": "https://www.lko.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    {
        "name": "Bauernbund Österreich",
        "url": "https://bauernbund.at",
        "suchpfad": "https://bauernbund.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    {
        "name": "bauernnetzwerk.at – Agrar-News",
        "url": "https://bauernnetzwerk.at",
        "suchpfad": "https://bauernnetzwerk.at/news/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    # === IMMOBILIEN ===
    {
        "name": "immodirekt.at – Landwirtschaftliche Immobilien",
        "url": "https://www.immodirekt.at",
        "suchpfad": "https://www.immodirekt.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "immobilien"
    },
    # === PLANUNG & ARCHITEKTUR ===
    {
        "name": "Architekturzentrum Wien",
        "url": "https://www.azw.at",
        "suchpfad": "https://www.azw.at/suche/?q=",
        "bundeslaender": ["W"],
        "typ": "fachmedium"
    },
    {
        "name": "architektur.aktuell (Architektur Österreich)",
        "url": "https://www.architektur-aktuell.at",
        "suchpfad": "https://www.architektur-aktuell.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    # === INDUSTRIE & GEWERBE ===
    {
        "name": "WKO Nachrichten – Wirtschaftskammer",
        "url": "https://www.wko.at",
        "suchpfad": "https://www.wko.at/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    {
        "name": "industriemagazin.at – Industrie & Technik",
        "url": "https://industriemagazin.at",
        "suchpfad": "https://industriemagazin.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    # === ENERGIE ===
    {
        "name": "AEE – Austrian Energy Agency",
        "url": "https://www.energyagency.at",
        "suchpfad": "https://www.energyagency.at/?s=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    {
        "name": "klimaaktiv.at (Energie & Klimaschutz)",
        "url": "https://www.klimaaktiv.at",
        "suchpfad": "https://www.klimaaktiv.at/service/suche.html?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "fachmedium"
    },
    # === INFRASTRUKTUR ===
    {
        "name": "ASFINAG Pressemeldungen",
        "url": "https://www.asfinag.at",
        "suchpfad": "https://www.asfinag.at/presse/presseaussendungen/?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "infrastruktur"
    },
    {
        "name": "ÖBB Presse (Bahnbauprojekte)",
        "url": "https://konzern.oebb.at",
        "suchpfad": "https://konzern.oebb.at/de/presse-medien/suche?q=",
        "bundeslaender": ["W","NOE","OOE","SBG","STK","KTN","TIR","VBG","BGR"],
        "typ": "infrastruktur"
    },

]

def get_quellen_fuer_bundeslaender(bundeslaender_liste: list[str]) -> list[dict]:
    """
    Gibt alle Medienquellen zurück, die mindestens eines der
    angegebenen Bundesländer abdecken.
    
    Parameter:
        bundeslaender_liste: Liste von Bundesland-Kürzeln, z.B. ["OOE", "SBG"]
                             Für ganz Österreich: alle 9 Kürzel übergeben
    Rückgabe:
        Gefilterte Liste von Medien-Dicts
    """
    if not bundeslaender_liste:
        return []
    
    ziel = set(bundeslaender_liste)
    return [
        m for m in MEDIEN
        if ziel.intersection(set(m["bundeslaender"]))
    ]


def get_alle_bundeslaender_kuerzel() -> list[str]:
    return ["W", "NOE", "OOE", "SBG", "STK", "KTN", "TIR", "VBG", "BGR"]


def get_quellen_ganz_oesterreich() -> list[dict]:
    return get_quellen_fuer_bundeslaender(get_alle_bundeslaender_kuerzel())


# =============================================================================
# TEST – Ausgabe beim direkten Ausführen
# =============================================================================
if __name__ == "__main__":
    print(f"Gesamt Quellen: {len(MEDIEN)}")
    
    print("\n--- Nur OÖ ---")
    ooe = get_quellen_fuer_bundeslaender(["OOE"])
    for m in ooe:
        print(f"  [{m['typ']:10}] {m['name']}")
    print(f"  → {len(ooe)} Quellen")

    print("\n--- Nur Vorarlberg ---")
    vbg = get_quellen_fuer_bundeslaender(["VBG"])
    for m in vbg:
        print(f"  [{m['typ']:10}] {m['name']}")
    print(f"  → {len(vbg)} Quellen")

    print("\n--- Ganz Österreich ---")
    print(f"  → {len(get_quellen_ganz_oesterreich())} Quellen")
