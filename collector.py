#!/usr/bin/env python3
"""
Συλλέκτης προσφορών διακοπών για οικογένεια (2 ενήλικες + 3 παιδιά).

Πηγές:
  * Ryanair fare finder (δημόσιο API): ναύλοι μετ' επιστροφής από ATH/SKG
    προς όλους τους προορισμούς, Ελλάδα και εξωτερικό, για τις σχολικές αργίες.
  * RSS sites προσφορών (HolidayPirates, Travelzoo, Fly4free κ.ά.): πακέτα,
    ξενοδοχεία, αεροπορικά.
  * scrapers.py (Playwright): Google Flights (όλες οι αεροπορικές),
    Airbnb και Google Hotels για 2 ενήλικες + 3 παιδιά.

Κάθε εκτέλεση αποθηκεύει τις τιμές σε SQLite (ιστορικό) και γράφει το
site/data.js που διαβάζει η ιστοσελίδα. Τρέχει κάθε 3 ώρες μέσω Task Scheduler.

    python collector.py              # πλήρης συλλογή
    python collector.py --no-scrape  # μόνο Ryanair + RSS (γρήγορα)
"""
import datetime as dt
import html
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
DATA_DIR = BASE / "data"
DB_PATH = DATA_DIR / "prices.db"
OUT_PATH = BASE / "site" / "data.js"
LOG_PATH = DATA_DIR / "collector.log"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
REFRESH_HOURS = 1

FAMILY = CFG["family"]
N_PAX = FAMILY["adults"] + len(FAMILY["children_ages"])
RULES = CFG["deal_rules"]

NOW = int(time.time())
TODAY = dt.date.today()


# ----------------------------------------------------------------- utilities
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def log(msg):
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    if sys.stdout is None:  # pythonw.exe
        DATA_DIR.mkdir(exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode())
    DATA_DIR.mkdir(exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url, timeout=30, accept="application/json, text/xml, */*"):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": accept,
        "Accept-Language": "el-GR,el;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url):
    return json.loads(fetch(url).decode("utf-8"))


def iso(d):
    return d.isoformat()


# ------------------------------------------------------------ travel windows
def orthodox_easter(year):
    """Ορθόδοξο Πάσχα (αλγόριθμος Meeus, έγκυρος 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = (d + e + 114) % 31 + 1
    return dt.date(year, month, day) + dt.timedelta(days=13)


def travel_windows():
    """Σχολικές αργίες & καλοκαίρι. Κάθε παράθυρο: εύρος αναχώρησης/επιστροφής."""
    W = []

    def add(season, key, label, out_from, out_to, in_from, in_to, dmin, dmax):
        out_from = max(out_from, TODAY + dt.timedelta(days=2))
        if out_from > out_to:
            return
        W.append(dict(season=season, key=key, label=label,
                      out_from=out_from, out_to=out_to,
                      in_from=in_from, in_to=in_to, dmin=dmin, dmax=dmax))

    # Χριστούγεννα (σχολεία κλειστά ~23/12 - 7/1)
    y = TODAY.year if TODAY < dt.date(TODAY.year, 12, 28) else TODAY.year + 1
    add(f"xmas{y}", f"xmas{y}", f"Χριστούγεννα {y}",
        dt.date(y, 12, 21), dt.date(y, 12, 31),
        dt.date(y, 12, 25), dt.date(y + 1, 1, 7), 4, 9)

    # Καθαρά Δευτέρα & Πάσχα
    ey = TODAY.year if TODAY < orthodox_easter(TODAY.year) else TODAY.year + 1
    easter = orthodox_easter(ey)
    clean_monday = easter - dt.timedelta(days=48)
    add(f"apokries{ey}", f"apokries{ey}", f"Καθαρά Δευτέρα {ey}",
        clean_monday - dt.timedelta(days=3), clean_monday - dt.timedelta(days=1),
        clean_monday, clean_monday + dt.timedelta(days=1), 2, 4)
    add(f"easter{ey}", f"easter{ey}", f"Πάσχα {ey}",
        easter - dt.timedelta(days=8), easter + dt.timedelta(days=3),
        easter, easter + dt.timedelta(days=8), 4, 9)

    # Καλοκαίρι: ~16/6 έως ~10/9, σε δεκαπενθήμερα για σύγκριση εβδομάδων
    sy = TODAY.year if TODAY < dt.date(TODAY.year, 8, 20) else TODAY.year + 1
    parts = [
        ("jun2", "β' μισό Ιουνίου", (6, 16), (6, 30)),
        ("jul1", "α' μισό Ιουλίου", (7, 1), (7, 15)),
        ("jul2", "β' μισό Ιουλίου", (7, 16), (7, 31)),
        ("aug1", "α' μισό Αυγούστου", (8, 1), (8, 15)),
        ("aug2", "β' μισό Αυγούστου", (8, 16), (8, 31)),
        ("sep1", "αρχές Σεπτεμβρίου", (9, 1), (9, 4)),
    ]
    for k, lbl, (m1, d1), (m2, d2) in parts:
        of, ot = dt.date(sy, m1, d1), dt.date(sy, m2, d2)
        add(f"summer{sy}", f"summer{sy}-{k}", f"Καλοκαίρι {sy} · {lbl}",
            of, ot, of + dt.timedelta(days=5), min(ot + dt.timedelta(days=10),
                                                   dt.date(sy, 9, 10)), 5, 10)
    return W


# ------------------------------------------------------------------ database
def db():
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots(
            key TEXT, ts INTEGER, price REAL, total REAL, meta TEXT);
        CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(key, ts);
        CREATE TABLE IF NOT EXISTS rss_seen(
            guid TEXT PRIMARY KEY, first_seen INTEGER, last_seen INTEGER);
        CREATE TABLE IF NOT EXISTS runs(
            ts INTEGER, source TEXT, ok INTEGER, n INTEGER, msg TEXT);
        CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    """)
    return con


def history(con, key, limit=240):
    rows = con.execute(
        "SELECT ts, price FROM snapshots WHERE key=? ORDER BY ts DESC LIMIT ?",
        (key, limit)).fetchall()
    return list(reversed(rows))


def record_run(con, source, ok, n, msg=""):
    con.execute("INSERT INTO runs VALUES(?,?,?,?,?)",
                (NOW, source, int(ok), n, msg[:500]))


# --------------------------------------------------------------- deal score
def assess(prev_hist, price, season_peers=None, prev_price=None,
           cheap_threshold=None):
    """Επιστρέφει (score, label, reasons). prev_hist: τιμές προηγούμενων σαρώσεων."""
    score, reasons = 0, []
    hist = [p for p in prev_hist if p]
    if len(hist) >= RULES["min_history_samples"]:
        med, lo = statistics.median(hist), min(hist)
        r = price / med if med else 1
        if price < lo - 0.5:
            score += 2
            reasons.append(f"Χαμηλότερη τιμή από όσες έχουμε δει (πριν: {lo:.0f}€)")
        if r <= RULES["great_ratio_vs_history"]:
            score += 2
            reasons.append(f"{(1 - r) * 100:.0f}% κάτω από τη μέση τιμή ιστορικού ({med:.0f}€)")
        elif r <= RULES["good_ratio_vs_history"]:
            score += 1
            reasons.append(f"{(1 - r) * 100:.0f}% κάτω από τη μέση τιμή ιστορικού")
        elif r >= 1.15:
            score -= 2
            reasons.append(f"{(r - 1) * 100:.0f}% ακριβότερη από το συνηθισμένο")
    if season_peers and len(season_peers) >= 3:
        pm = statistics.median(season_peers)
        if price <= pm * RULES["great_ratio_vs_season"]:
            score += 1
            reasons.append(f"Φθηνότερη από τις άλλες εβδομάδες της περιόδου (διάμεση {pm:.0f}€)")
        elif price >= pm * 1.3:
            score -= 1
            reasons.append("Ακριβή εβδομάδα σε σχέση με την υπόλοιπη περίοδο")
    if prev_price and price <= prev_price * RULES["price_drop_ratio"]:
        score += 1
        reasons.append(f"Πτώση τιμής από {prev_price:.0f}€")
    if cheap_threshold and price <= cheap_threshold:
        score += 1
        reasons.append(f"Πολύ χαμηλός ναύλος (≤{cheap_threshold:.0f}€/άτομο μετ' επιστροφής)")

    if score >= 3:
        label = "great"
    elif score >= 1:
        label = "good"
    elif score <= -1:
        label = "pricey"
    else:
        label = "normal"
    return score, label, reasons


# ------------------------------------------------------------------ Ryanair
def flight_extras():
    e = CFG["flight_extras_estimate"]
    return 2 * (e["checked_bags"] * e["bag_price_per_leg_eur"]
                + e["paid_seats"] * e["seat_price_per_leg_eur"])


def collect_ryanair(con):
    items, n_calls, errors = [], 0, []
    extras = flight_extras()
    for origin in CFG["origins"]:
        for w in travel_windows():
            q = urllib.parse.urlencode(dict(
                departureAirportIataCode=origin,
                outboundDepartureDateFrom=iso(w["out_from"]),
                outboundDepartureDateTo=iso(w["out_to"]),
                inboundDepartureDateFrom=iso(w["in_from"]),
                inboundDepartureDateTo=iso(w["in_to"]),
                durationFrom=w["dmin"], durationTo=w["dmax"],
                currency="EUR", market="el-gr"))
            url = "https://www.ryanair.com/api/farfnd/v4/roundTripFares?" + q
            try:
                data = fetch_json(url)
                n_calls += 1
            except Exception as ex:  # noqa: BLE001
                errors.append(f"{origin} {w['key']}: {ex}")
                continue
            finally:
                time.sleep(1.2)
            for f in data.get("fares", []):
                ob, ib, sm = f["outbound"], f["inbound"], f["summary"]
                arr = ob["arrivalAirport"]
                pp = float(sm["price"]["value"])
                prev = (sm.get("previousPrice") or {}).get("value")
                dest = arr["iataCode"]
                items.append(dict(
                    kind="flight", source="Ryanair",
                    key=f"FR|{origin}|{dest}|{w['key']}",
                    season=w["season"], window=w["key"], window_label=w["label"],
                    origin=origin, origin_name=ob["departureAirport"]["name"],
                    dest=dest, dest_name=arr["name"],
                    city=arr.get("city", {}).get("name") or arr["name"],
                    country=arr["countryName"],
                    cc=arr.get("city", {}).get("countryCode"),
                    region="gr" if arr.get("city", {}).get("countryCode") == "gr" else "abroad",
                    out_dt=ob["departureDate"], out_arr=ob["arrivalDate"],
                    in_dt=ib["departureDate"], in_arr=ib["arrivalDate"],
                    out_fn=ob.get("flightNumber"), in_fn=ib.get("flightNumber"),
                    nights=sm.get("tripDurationDays"),
                    pp=round(pp, 2), prev_pp=float(prev) if prev else None,
                    family_fare=round(pp * N_PAX, 2),
                    family_total=round(pp * N_PAX + extras, 2),
                ))
    # αποθήκευση & αξιολόγηση
    peers = {}
    for it in items:
        peers.setdefault((it["origin"], it["dest"], it["season"]), []).append(
            (it["window"], it["pp"]))
    for it in items:
        h = history(con, it["key"])
        prev_hist = [p for ts, p in h if ts < NOW]
        same_season = peers[(it["origin"], it["dest"], it["season"])]
        others = [p for w, p in same_season if w != it["window"]]
        score, label, reasons = assess(prev_hist, it["pp"], others, it["prev_pp"],
                                       RULES["cheap_pp_roundtrip_eur"])
        con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)",
                    (it["key"], NOW, it["pp"], it["family_total"],
                     json.dumps({"o": it["out_dt"], "i": it["in_dt"]})))
        it.update(score=score, deal=label, reasons=reasons,
                  hist=[[ts, p] for ts, p in h if ts < NOW][-60:] + [[NOW, it["pp"]]],
                  hist_n=len(prev_hist),
                  hist_min=min(prev_hist) if prev_hist else None,
                  hist_median=statistics.median(prev_hist) if prev_hist else None)
    record_run(con, "Ryanair", not errors or bool(items), len(items),
               "; ".join(errors[:5]))
    log(f"Ryanair: {n_calls} κλήσεις, {len(items)} ναύλοι, {len(errors)} σφάλματα")
    return items, errors


# ---------------------------------------------------------------------- RSS
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"

GREECE_WORDS = [
    "greece", "greek", "griechenland", "griechisch", "grecja", "ελλάδα",
    "crete", "kreta", "κρήτη", "heraklion", "chania", "rethymno", "rhodes",
    "rhodos", "ρόδο", "corfu", "korfu", "κέρκυρα", "kos", "zakynthos", "zante",
    "kefalonia", "cephalonia", "halkidiki", "chalkidiki", "chalkidike",
    "χαλκιδική", "santorini", "mykonos", "naxos", "paros", "lefkada", "lefkas",
    "athens", "athen", "αθήνα", "thessaloniki", "peloponnese",
    "peloponnes", "kalamata", "preveza", "parga", "skiathos", "thassos",
    "samos", "lesbos", "karpathos", "milos", "aegean", "ionian",
]
FROM_GREECE = ["from athens", "from thessaloniki", "ab athen", "ab thessaloniki",
               "z aten", "από αθήνα", "από θεσσαλονίκη"]
FAMILY_WORDS = [
    "family", "families", "kids", "kid", "children", "child", "water park",
    "waterpark", "aquapark", "aqua park", "slides", "kids club", "kids go free",
    "famili*", "kinder*", "wasserpark", "rutschen", "family room",
    "apartment", "villa", "all-inclusive", "all inclusive", "all-inc",
    "all-inkl", "alles inklusive", "οικογέν*", "παιδ*",
]
PACKAGE_WORDS = [
    "inc flights", "incl. flights", "with flights", "w/flights", "flights +",
    "+ flights", "flight +", "& flights", "inkl. flug", "mit flug", "flug +",
    "pauschal", "holiday", "package", "all-inclusive", "all inclusive",
    "hotel +", "+ hotel", "night", "nights", "nächte",
]
FLIGHT_WORDS = ["flight", "flights", "flüge", "flug", "fares", "return",
                "round-trip", "airlines", "airways"]
CURRENCY_RE = re.compile(
    r"(?P<cur>[£€$])\s?(?P<num>\d[\d.,]*)|(?P<num2>\d[\d.,]*)\s?(?P<cur2>€|eur|euro|zł|pln)",
    re.I)


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_price(text):
    m = CURRENCY_RE.search(text or "")
    if not m:
        return None, None
    num = m.group("num") or m.group("num2")
    cur = (m.group("cur") or m.group("cur2") or "").lower()
    cur = {"£": "GBP", "€": "EUR", "eur": "EUR", "euro": "EUR", "$": "USD",
           "zł": "PLN", "pln": "PLN"}.get(cur, cur.upper())
    num = num.rstrip(".,")
    if re.search(r"[.,]\d{2}$", num):
        num = num[:-3]
    try:
        return int(re.sub(r"[.,]", "", num)), cur
    except ValueError:
        return None, None


_WORD_RE_CACHE = {}


def has_any(text, words):
    """Ταίριασμα ολόκληρων λέξεων (λέξεις που λήγουν σε '*' = πρόθεμα)."""
    key = id(words)
    if key not in _WORD_RE_CACHE:
        parts = []
        for w in words:
            w = w.strip()
            if w.endswith("*"):
                parts.append(r"(?<!\w)" + re.escape(w[:-1]))
            else:
                parts.append(r"(?<!\w)" + re.escape(w) + r"(?!\w)")
        _WORD_RE_CACHE[key] = re.compile("|".join(parts), re.I)
    return _WORD_RE_CACHE[key].findall(text)


EXCLUDE_WORDS = ["adults-only", "adults only", "adult-only", "nur für erwachsene",
                 "erwachsenenhotel", "honeymoon", "romantic", "couples",
                 "für paare", "spa break", "wellness"]
LUXURY_WORDS = ["cruise", "cruises", "kreuzfahrt", "aida", "lux", "luxury",
                "ultra-lux", "luxus*", "5-star", "5⭐", "5-sterne", "suite", "private jacuzzi"]


def collect_rss(con):
    out, errors = [], []
    for feed in CFG["rss_feeds"]:
        try:
            raw = fetch(feed["url"], accept="application/rss+xml, application/atom+xml, text/xml")
            root = ET.fromstring(raw)
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{feed['name']}: {ex}")
            record_run(con, feed["name"], False, 0, str(ex))
            continue
        entries = root.findall(".//item") or root.findall(f".//{ATOM}entry")
        n = 0
        for e in entries:
            title = (e.findtext("title") or e.findtext(f"{ATOM}title") or "").strip()
            link = e.findtext("link") or ""
            if not link:
                le = e.find(f"{ATOM}link")
                link = le.get("href") if le is not None else ""
            desc = strip_html(e.findtext("description") or e.findtext(f"{ATOM}summary") or "")
            date = (e.findtext("pubDate") or e.findtext(f"{ATOM}published")
                    or e.findtext(f"{ATOM}updated") or "")
            img = None
            enc = e.find("enclosure")
            if enc is not None and (enc.get("type") or "").startswith("image"):
                img = enc.get("url")
            if not img:
                mc = e.find(f"{MEDIA}content")
                if mc is not None:
                    img = mc.get("url")
            text = f" {title} {desc[:600]} ".lower()
            gr = has_any(title.lower(), GREECE_WORDS)          # Ελλάδα στον τίτλο
            gr_desc = not gr and has_any(text, GREECE_WORDS)   # μόνο στην περιγραφή
            from_gr = has_any(text, FROM_GREECE)
            fam = has_any(text, FAMILY_WORDS)
            pkg = has_any(text, PACKAGE_WORDS)
            fl = has_any(text, FLIGHT_WORDS)
            lux = has_any(text, LUXURY_WORDS)
            relevance = (3 * bool(gr) + 1 * bool(gr_desc) + 3 * bool(from_gr)
                         + 2 * bool(fam) + 1 * bool(pkg) - 2 * bool(lux))
            if has_any(text, EXCLUDE_WORDS):
                continue
            # Μόνο ό,τι κλείνεται από Ελλάδα: αναχώρηση από Ελλάδα ή σκέτη διαμονή στην Ελλάδα
            if not (from_gr or (gr and not fl)):
                continue
            if relevance < CFG.get("rss_min_relevance", 2):
                continue
            price, cur = parse_price(title)
            if price is None:
                price, cur = parse_price(desc)
            guid = e.findtext("guid") or e.findtext(f"{ATOM}id") or link
            row = con.execute("SELECT first_seen FROM rss_seen WHERE guid=?", (guid,)).fetchone()
            first_seen = row[0] if row else NOW
            con.execute("INSERT OR REPLACE INTO rss_seen VALUES(?,?,?)", (guid, first_seen, NOW))
            if pkg and not (fl and not any(w in text for w in ("hotel", "night", "nächte", "all-in"))):
                kind = "package"
            elif fl:
                kind = "flight_deal"
            else:
                kind = "package"
            pp_note = bool(re.search(r"\bpp\b|p\.p\.|per person|pro person|p\.p|/os", text))
            out.append(dict(
                kind=kind, source=feed["name"], title=title, link=link,
                desc=desc[:280], date=date, img=img, price=price, currency=cur,
                per_person=pp_note,
                region="gr" if (gr or from_gr) else "abroad",
                tags=dict(greece=bool(gr), from_greece=bool(from_gr),
                          family=bool(fam), package=bool(pkg), luxury=bool(lux)),
                relevance=relevance, first_seen=first_seen,
                is_new=(NOW - first_seen) < REFRESH_HOURS * 3600 + 600,
            ))
            n += 1
        record_run(con, feed["name"], True, n)
        log(f"RSS {feed['name']}: {len(entries)} εγγραφές, {n} σχετικές")
    out.sort(key=lambda x: (-x["relevance"], -x["first_seen"]))
    return out, errors


# --------------------------------------------------------------------- main
def main():
    log("=== Έναρξη συλλογής ===")
    con = db()
    errors = []
    flights, hotels, rss = [], [], []
    for name, fn in (("Ryanair", lambda: collect_ryanair(con)),
                     ("RSS", lambda: collect_rss(con))):
        try:
            res, errs = fn()
            errors += errs
            if name == "Ryanair":
                flights = res
            else:
                rss = res
        except Exception:  # noqa: BLE001
            errors.append(f"{name}: {traceback.format_exc(limit=2)}")
            log(errors[-1])
        con.commit()
    try:
        if str(BASE) not in sys.path:
            sys.path.insert(0, str(BASE))
        import scrapers
        me = sys.modules[__name__]
        if "--no-scrape" in sys.argv:  # μόνο από τα αποθηκευμένα αποτελέσματα
            gf_flights = scrapers.build_flights(me, con, scrapers.load_cache(con, "gf_cache"))
            hotels = scrapers.build_stays(me, con, scrapers.load_cache(con, "ab_cache"),
                                          scrapers.load_cache(con, "gh_cache"), flights + gf_flights)
        else:
            gf_flights, hotels, errs = scrapers.run(me, con, flights)
            errors += errs
        flights = flights + gf_flights
    except Exception:  # noqa: BLE001
        errors.append(f"Scraping: {traceback.format_exc(limit=2)}")
        log(errors[-1])
    con.commit()

    windows = [dict(key=w["key"], label=w["label"], season=w["season"],
                    out_from=iso(w["out_from"]), out_to=iso(w["out_to"]))
               for w in travel_windows()]
    n_scans = con.execute("SELECT COUNT(DISTINCT ts) FROM snapshots").fetchone()[0]
    first_scan = con.execute("SELECT MIN(ts) FROM snapshots").fetchone()[0]
    payload = dict(
        generated=NOW, next_update=NOW + REFRESH_HOURS * 3600,
        family=FAMILY, n_pax=N_PAX, extras=flight_extras(),
        origins=CFG["origins"], windows=windows,
        budget_default=CFG.get("max_family_budget_eur_default", 4000),
        n_scans=n_scans, first_scan=first_scan,
        flights=flights, rss=rss, hotels=hotels,
        errors=errors[:20],
    )
    OUT_PATH.parent.mkdir(exist_ok=True)
    tmp = OUT_PATH.with_suffix(".tmp")
    tmp.write_text("window.DIAKOPES = " + json.dumps(payload, ensure_ascii=False) + ";\n",
                   encoding="utf-8")
    tmp.replace(OUT_PATH)
    try:
        import notify
        log(notify.notify_deals(con, payload))
    except Exception as ex:  # noqa: BLE001
        log(f"Email απέτυχε: {ex}")
    # καθάρισμα πολύ παλιών δεδομένων (> 400 ημέρες)
    con.execute("DELETE FROM snapshots WHERE ts < ?", (NOW - 400 * 86400,))
    con.execute("DELETE FROM runs WHERE ts < ?", (NOW - 60 * 86400,))
    con.commit()
    con.close()
    deals = sum(1 for f in flights if f["deal"] == "great")
    log(f"=== Τέλος: {len(flights)} πτήσεις ({deals} πραγματικές προσφορές), "
        f"{len(rss)} πακέτα/προσφορές, {len(hotels)} διαμονές, {len(errors)} σφάλματα ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
