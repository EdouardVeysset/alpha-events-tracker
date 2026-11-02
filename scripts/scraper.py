"""
Scraper d'événements (Luxembourg finance) -> JSONBin.

Garanties de sécurité des données :
  * Si la lecture de JSONBin échoue, on ABANDONNE (on n'écrase jamais le bin).
  * Aucun événement existant n'est supprimé ; les inscriptions (`attendees`) sont conservées.
  * Les anciens événements (IDs instables issus de hash()) sont rapprochés des nouveaux
    par (organisateur, titre normalisé) et gardent leur ancien ID.
  * Une sauvegarde locale du bin est écrite avant chaque mise à jour.
  * Vérification finale avant écriture : aucun ID perdu, aucune inscription perdue.
  * Sans JSONBIN_* ou avec --dry-run : aucune écriture.
"""
import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import date, datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BIN_ID = os.environ.get("JSONBIN_BIN_ID")
API_KEY = os.environ.get("JSONBIN_API_KEY")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TIMEOUT = 15
BACKUP_DIR = os.environ.get("BACKUP_DIR", "backups")

log = logging.getLogger("scraper")

# --------------------------------------------------------------------------- HTTP

def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "PUT"))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s

SESSION = make_session()

# --------------------------------------------------------------------------- Dates

MONTHS = {
    "january": 1, "jan": 1, "janvier": 1, "janv": 1,
    "february": 2, "feb": 2, "février": 2, "fevrier": 2, "févr": 2, "fév": 2,
    "march": 3, "mar": 3, "mars": 3,
    "april": 4, "apr": 4, "avril": 4, "avr": 4,
    "may": 5, "mai": 5,
    "june": 6, "jun": 6, "juin": 6,
    "july": 7, "jul": 7, "juillet": 7, "juil": 7,
    "august": 8, "aug": 8, "août": 8, "aout": 8,
    "september": 9, "sep": 9, "sept": 9, "septembre": 9,
    "october": 10, "oct": 10, "octobre": 10,
    "november": 11, "nov": 11, "novembre": 11,
    "december": 12, "dec": 12, "décembre": 12, "decembre": 12, "déc": 12,
}
_MONTH_RE = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))
_YEAR = r"(20\d{2})"

def _valid(y, m, d):
    try:
        datetime(int(y), int(m), int(d))
    except ValueError:
        return None
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

def extract_date_from_text(text, today=None):
    """Renvoie une date ISO 'YYYY-MM-DD' ou None. N'invente JAMAIS de date.
    Formats : 2026-11-04, 04/11/2026, 4.11.2026, 4 November 2026, 4th Nov 2026,
    November 4, 2026, 4 novembre 2026. Année limitée à [année-1, année+3]."""
    if not text:
        return None
    today = today or date.today()
    lo, hi = today.year - 1, today.year + 3
    t = text.replace("\xa0", " ")

    def ok(iso):
        return iso if iso and lo <= int(iso[:4]) <= hi else None

    m = re.search(rf"\b{_YEAR}-(\d{{2}})-(\d{{2}})\b", t)
    if m and ok(_valid(m.group(1), m.group(2), m.group(3))):
        return _valid(m.group(1), m.group(2), m.group(3))

    for m in re.finditer(rf"\b(\d{{1,2}})[/.\-](\d{{1,2}})[/.\-]{_YEAR}\b", t):
        iso = ok(_valid(m.group(3), m.group(2), m.group(1)))   # jj/mm/aaaa (convention européenne)
        if iso:
            return iso

    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th|er)?\s+(?:of\s+)?({_MONTH_RE})\.?,?\s+{_YEAR}\b", t, re.I):
        iso = ok(_valid(m.group(3), MONTHS[m.group(2).lower()], m.group(1)))
        if iso:
            return iso

    for m in re.finditer(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+{_YEAR}\b", t, re.I):
        iso = ok(_valid(m.group(3), MONTHS[m.group(1).lower()], m.group(2)))
        if iso:
            return iso
    return None

# --------------------------------------------------------------------------- Identité

def normalize(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def event_key(organizer, title):
    return f"{normalize(organizer)}|{normalize(title)}"

def stable_id(organizer, title):
    """ID déterministe (contrairement à hash(), randomisé à chaque exécution Python).
    Ne dépend PAS de la date, pour que l'ID survive à un changement de date."""
    h = hashlib.sha1(event_key(organizer, title).encode()).hexdigest()
    return int(h[:12], 16) % 1_000_000_000

# --------------------------------------------------------------------------- Extraction

BLACKLIST = {
    "contact", "login", "register", "login register", "skip to content", "recherche", "rechercher",
    "search", "subscribe to our newsletter", "newsletter", "read more", "learn more", "en savoir plus",
    "see all", "view all", "events", "evenements", "agenda", "menu", "home", "accueil", "cookies",
    "privacy policy", "terms and conditions", "list your security", "market overview", "language learning",
}
CONTAINER_SELECTOR = (".event-item, .event-card, .event, .event-listing, .views-row, "
                      ".fusion-post-wrapper, .card, article")
TITLE_SELECTOR = "h1, h2, h3, h4, .event-title, a.title"

# --------------------------------------------------------------------------- Filtre de pertinence
# Thème : wealth management, private banking, asset management, asset servicing, private markets.
# Les mots-clés sont cherchés (sans accents, en minuscules, en mots entiers) dans le titre + le texte
# de la carte. Le titre pèse plus lourd. Un événement est gardé si score >= MIN_SCORE
# et qu'aucun mot "hors sujet" ne figure dans le TITRE.

MIN_SCORE = 2

STRONG_KEYWORDS = [  # 2 points dans le texte, 3 dans le titre
    "wealth management", "wealth manager", "private bank", "private banking", "family office",
    "family offices", "asset management", "asset manager", "asset managers", "investment management",
    "investment fund", "investment funds", "fund industry", "fund distribution", "fund services",
    "asset servicing", "fund administration", "transfer agent", "depositary", "custody", "custodian",
    "private markets", "private market", "private equity", "private debt", "private credit",
    "private assets", "venture capital", "infrastructure investment", "real assets", "real estate fund",
    "alternative investment", "alternatives", "aifm", "aif", "ucits", "sicav", "raif", "ucits",
    "etf", "etfs", "elte", "eltif", "wealthtech", "portfolio management", "portfolio manager",
    "limited partners", "general partners", "gp", "lp", "fund manager", "fund managers",
    "fund finance", "securities services", "management company", "manco", "mancos",
    "high net worth", "hnwi", "uhnw", "succession planning", "sfdr",
]
WEAK_KEYWORDS = [  # 1 point (titre ou texte)
    "fund", "funds", "investor", "investors", "investment", "investing", "asset", "assets",
    "wealth", "private", "portfolio", "alternative", "mifid", "dora", "aml", "kyc",
    "tokenisation", "tokenization", "digital assets", "capital markets", "pension", "institutional",
    "luxflag", "alfi", "lpea", "abbl", "lsfi",
]
EXCLUDE_TITLE = [  # dans le TITRE => rejet (sauf score très élevé)
    "language learning", "yoga", "padel", "golf", "tennis", "running", "marathon", "wine", "afterwork",
    "after work", "after-work", "christmas", "xmas", "halloween", "cooking", "cocktail", "networking drinks",
    "cybersecurity", "cyber security", "hr ", "human resources", "recruitment", "payroll", "tax return",
    "vat", "customs", "real estate market", "immobilier", "construction", "retail", "tourism", "mobility",
    "photography", "concert", "exhibition", "kids", "children", "excel", "powerpoint", "management skills",
    "leadership", "public speaking", "negotiation", "time management", "english", "french lessons",
    "webinar replay", "newsletter", "job offer",
]

EXCLUDE_TITLE += [  # communiqués / non-événements / hors thème
    "appointment", "appoints", "supports", "sponsors", "attends", "factsheet", "episode", "podcast",
    "transfer pricing", "tp documentation", "defence", "defense", "start your business", "etablir son entreprise",
    "trade mission", "trade fair", "national pavilion", "delegation", "e-invoicing", "facturation",
    "cyber", "business plan", "social media", "1031", "wirtschaftsgipfel", "sustainable innovation",
    "future finance week", "gulf transition", "energy resilience", "data centres",
]
# Bonus selon la source : ces organisateurs sont par nature dans le thème.
SOURCE_BONUS = {"ALFI": 1, "LPEA": 1, "LuxFLAG": 1}

def _compile(words):
    # le "s?" final accepte les pluriels (investment / investments)
    return [(w, re.compile(rf"(?<![a-z0-9]){re.escape(w)}s?(?![a-z0-9])")) for w in dict.fromkeys(words)]

_STRONG = _compile(STRONG_KEYWORDS)
_WEAK = _compile(WEAK_KEYWORDS)
_EXCL = _compile(EXCLUDE_TITLE)

def relevance(title, text=""):
    """Renvoie (score, mots_trouvés, motif_exclusion|None)."""
    t, x = normalize(title), normalize(text)
    score, found = 0, []
    for w, rx in _STRONG:
        if rx.search(t):
            score += 3; found.append(w)
        elif rx.search(x):
            score += 2; found.append(w)
    for w, rx in _WEAK:
        if rx.search(t) or rx.search(x):
            score += 1; found.append(w)
    excl = next((w for w, rx in _EXCL if rx.search(t)), None)
    return score, found, excl

def is_relevant(title, text="", bonus=0):
    score, _, excl = relevance(title, text)
    score += bonus
    if excl and score < MIN_SCORE + 4:     # exclusion, sauf si thème très fortement présent
        return False
    return score >= MIN_SCORE

def good_title(t):
    n = normalize(t)
    return len(t) >= 8 and len(t) <= 200 and n not in BLACKLIST and not n.startswith("skip to")

def _walk_jsonld(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and x.endswith("Event") for x in types):
            yield node
        for v in node.values():
            if isinstance(v, (list, dict)):
                yield from _walk_jsonld(v)

def extract_jsonld(soup, base_url):
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        for ev in _walk_jsonld(data):
            title = (ev.get("name") or "").strip()
            start = str(ev.get("startDate") or "")
            iso = extract_date_from_text(start[:10]) if start else None
            if not (title and iso and good_title(title)):
                continue
            tm = re.search(r"T(\d{2}:\d{2})", start)
            loc = ev.get("location")
            loc_name = loc.get("name") if isinstance(loc, dict) else (loc if isinstance(loc, str) else None)
            out.append({"title": title, "date": iso, "time": tm.group(1) if tm else None,
                        "location": loc_name, "url": urljoin(base_url, ev.get("url") or base_url),
                        "text": f"{ev.get('description') or ''} {ev.get('about') or ''}"})
    return out

def extract_html(soup, base_url):
    out = []
    for item in soup.select(CONTAINER_SELECTOR):
        text = item.get_text(" ", strip=True)
        if len(text) > 1500:          # conteneur géant = probablement toute la page
            continue
        iso = extract_date_from_text(text)
        if not iso:                    # pas de vraie date -> pas d'événement
            continue
        title_el = item.select_one(TITLE_SELECTOR) or item.select_one("a")
        if not title_el:
            continue
        title = title_el.get_text(" ", strip=True)
        if not good_title(title):
            continue
        link = title_el if title_el.name == "a" else (title_el.find("a") or item.select_one("a"))
        href = link.get("href") if link is not None and link.has_attr("href") else None
        url = urljoin(base_url, href) if href and not href.startswith(("#", "javascript:", "mailto:")) else base_url
        tm = re.search(r"\b([01]?\d|2[0-3])[:h]([0-5]\d)\b", text)
        out.append({"title": title, "date": iso,
                    "time": f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else None,
                    "location": None, "url": url, "text": text})
    return out

def scrape_source(url, organizer, default_type="conference", session=None):
    """Scrape générique : JSON-LD d'abord, HTML ensuite. Ne renvoie que des événements datés."""
    session = session or SESSION
    try:
        res = session.get(url, timeout=TIMEOUT)
        if res.status_code != 200:
            log.warning("%s: HTTP %s (%s)", organizer, res.status_code, url)
            return []
        soup = BeautifulSoup(res.text, "html.parser")
        raw = extract_jsonld(soup, res.url) or extract_html(soup, res.url)
    except Exception as e:
        log.warning("%s: erreur (%s)", organizer, e)
        return []

    events, seen, rejected = [], set(), []
    for r in raw:
        k = event_key(organizer, r["title"])
        if k in seen:
            continue
        seen.add(k)
        r["title"] = html.unescape(r["title"]).replace("​", "").strip()
        if r["date"] < date.today().isoformat():      # événement passé
            continue
        if not is_relevant(r["title"], html.unescape(r.get("text", "")), SOURCE_BONUS.get(organizer, 0)):
            rejected.append(r["title"])
            continue
        events.append({
            "id": stable_id(organizer, r["title"]),
            "title": r["title"],
            "type": default_type,
            "date": r["date"],
            "time": r["time"] or "09:00",
            "location": r["location"] or "Luxembourg",
            "organizer": organizer,
            "url": r["url"],
            "attendees": [],
        })
    log.info("%s: %d retenu(s), %d hors thème", organizer, len(events), len(rejected))
    for t in rejected:
        log.debug("  rejeté: %s", t)
    return events

# --------------------------------------------------------------------------- Sources

SOURCES = [
    ("https://www.alfi.lu/events", "ALFI", "conference"),
    ("https://www.abbl.lu/en/home/events", "ABBL", "seminar"),
    ("https://lpea.lu/events/", "LPEA", "conference"),
    ("https://www.bcc.lu/events/upcoming-events/", "BCC", "forum"),
    ("https://www.amcham.lu/events/", "AMCHAM", "forum"),
    ("https://paperjam.lu/club/agenda", "Paperjam Club", "forum"),
    ("https://www.cc.lu/agenda", "Chambre de Commerce", "seminar"),
    ("https://romlux.org/romlux-events/", "Romlux", "forum"),
    ("https://100women.org/events/", "100 Women in Finance", "conference"),
    ("https://www.futureoffinance.biz/upcoming-events", "Future of Finance", "conference"),
    ("https://www.luxembourgforfinance.com/en/events/upcoming-events/", "Luxembourg for Finance", "forum"),
    ("https://lsfi.lu/events", "LSFI", "seminar"),
    ("https://luxflag.org/events/", "LuxFLAG", "seminar"),
    ("https://www.ila.lu/event?tags=%5B4%5D", "ILA", "seminar"),
    ("https://www.hubfinance.com/events", "Hubfinance", "forum"),
    ("https://creobis.eu/en/", "Creobis", "workshop"),
    ("https://www.houseoftraining.lu/en_GB/training/planning", "House of Training", "workshop"),
    ("https://www.arendt.com/training-agenda/", "Arendt Training", "workshop"),
    ("https://www.arendt.com/news-insights/events/", "Arendt", "seminar"),
    ("https://elvingerhoss.lu/insights/events", "Elvinger Hoss", "seminar"),
    ("https://www.atoz.lu/events", "Atoz", "seminar"),
    ("https://www.ey.com/en_lu/events/location-events", "EY Luxembourg", "seminar"),
    ("https://www.pwc.lu/en/events.html", "PwC", "seminar"),
    ("https://kpmg.com/lu/en/home/events.html", "KPMG", "seminar"),
    ("https://www2.deloitte.com/lu/en/events/events.html", "Deloitte", "seminar"),
    ("https://www.grantthornton.lu/en/events/", "Grant Thornton", "seminar"),
    ("https://www.jtcgroup.com/events/", "JTC", "seminar"),
    ("https://lhoft.com/events/", "LHoFT", "workshop"),
    ("https://finologee.com/news/", "Finologee", "forum"),
    ("https://www.luxse.com/resources/events", "LuxSE", "forum"),
]

# --------------------------------------------------------------------------- Fusion

def merge_events(existing, new_ones):
    """Fusionne sans RIEN perdre.
    - Tous les événements existants sont conservés tels quels (autres champs, attendees…).
    - Un nouvel événement est rapproché d'un existant par ID, sinon par (organisateur, titre).
      L'ID existant et les `attendees` existants sont toujours conservés.
    - Pour un événement rapproché, on met à jour date/heure/url/titre uniquement si le scraping
      a trouvé une vraie valeur ; on ne remplace pas un champ existant par du vide."""
    merged = [dict(e) for e in existing]
    by_id = {e.get("id"): e for e in merged if "id" in e}
    by_key = {}
    for e in merged:
        if e.get("organizer") and e.get("title"):
            by_key.setdefault(event_key(e["organizer"], e["title"]), e)

    added = updated = 0
    for n in new_ones:
        target = by_id.get(n["id"]) or by_key.get(event_key(n["organizer"], n["title"]))
        if target is None:
            n = dict(n)
            merged.append(n)
            by_id[n["id"]] = n
            by_key[event_key(n["organizer"], n["title"])] = n
            added += 1
            continue
        changed = False
        for field in ("title", "date", "time", "url"):
            if n.get(field) and target.get(field) != n[field]:
                target[field] = n[field]
                changed = True
        target.setdefault("attendees", [])   # jamais écrasé
        updated += changed
    log.info("Fusion: %d ajouté(s), %d mis à jour, %d conservé(s) tels quels",
             added, updated, len(merged) - added - updated)
    return merged

def check_no_loss(existing, final):
    """Garde-fou : lève une erreur si un événement ou une inscription a disparu."""
    final_by_id = {e.get("id"): e for e in final}
    for e in existing:
        f = final_by_id.get(e.get("id"))
        if f is None:
            raise RuntimeError(f"Événement perdu: {e.get('id')} {e.get('title')!r}")
        old_att = e.get("attendees") or []
        new_att = f.get("attendees") or []
        if any(a not in new_att for a in old_att):
            raise RuntimeError(f"Inscriptions perdues pour {e.get('title')!r}")

# --------------------------------------------------------------------------- JSONBin

class JsonBinError(RuntimeError):
    pass

def fetch_existing_events():
    """Renvoie la liste existante. LÈVE une exception si la lecture échoue (jamais [] par défaut)."""
    url = f"https://api.jsonbin.io/v3/b/{BIN_ID}/latest"
    try:
        r = SESSION.get(url, headers={"X-Master-Key": API_KEY}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise JsonBinError(f"Lecture JSONBin impossible: {e}") from e
    if r.status_code != 200:
        raise JsonBinError(f"Lecture JSONBin: HTTP {r.status_code} {r.text[:200]}")
    record = r.json().get("record")
    if not isinstance(record, list):
        raise JsonBinError(f"Format inattendu du bin (type {type(record).__name__}); abandon.")
    return record

def backup(events):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"events_{datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    return path

def save_to_jsonbin(events):
    url = f"https://api.jsonbin.io/v3/b/{BIN_ID}"
    r = SESSION.put(url, headers={"Content-Type": "application/json", "X-Master-Key": API_KEY},
                    json=events, timeout=TIMEOUT)
    if r.status_code != 200:
        raise JsonBinError(f"Erreur sauvegarde: HTTP {r.status_code} {r.text[:200]}")
    log.info("Mise à jour réussie sur JSONBin (%d événements).", len(events))

# --------------------------------------------------------------------------- Main

def collect_new_events():
    """Scrape toutes les sources puis dédoublonne entre sources (même titre sans l'année + même date).
    L'ordre de SOURCES fait foi : la source la plus spécialisée (ex. ALFI) est gardée."""
    allev, seen = [], set()
    for url, org, typ in SOURCES:
        for e in scrape_source(url, org, typ):
            k = (re.sub(r"\b20\d{2}\b", "", normalize(e["title"])).strip(), e["date"])
            if k in seen:
                continue
            seen.add(k)
            allev.append(e)
    return allev

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ne rien écrire sur JSONBin")
    ap.add_argument("--verbose", action="store_true", help="affiche aussi les événements rejetés")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    have_creds = bool(BIN_ID and API_KEY)
    if not have_creds and not args.dry_run:
        log.error("JSONBIN_BIN_ID / JSONBIN_API_KEY manquants (utilisez --dry-run pour tester).")
        return 2

    existing = []
    if have_creds:
        try:
            existing = fetch_existing_events()
        except JsonBinError as e:
            log.error("%s -> ABANDON, rien n'est modifié.", e)
            return 1
        log.info("%d événement(s) existant(s) dans le bin.", len(existing))
    else:
        log.warning("Pas d'identifiants: dry-run sur un bin vide.")

    new_events = collect_new_events()
    if not new_events:
        log.warning("Aucun événement valide récupéré; bin inchangé.")
        return 0

    final = merge_events(existing, new_events)
    check_no_loss(existing, final)

    if args.dry_run or not have_creds:
        log.info("DRY-RUN: %d événements au total, aucune écriture.", len(final))
        return 0

    log.info("Sauvegarde locale: %s", backup(existing))
    try:
        save_to_jsonbin(final)
    except JsonBinError as e:
        log.error("%s", e)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
