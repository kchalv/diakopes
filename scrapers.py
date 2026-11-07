"""
Scrapers με πραγματικό browser (Playwright/Chromium) για πηγές χωρίς δημόσιο API:

  * Google Flights  – όλες οι αεροπορικές, τιμή για όλη την οικογένεια
  * Airbnb          – διαμερίσματα/σπίτια για 2 ενήλικες + 3 παιδιά, συνολική τιμή
  * Google Hotels   – ξενοδοχεία (συγκεντρώνει Booking, Expedia κ.ά.) με ηλικίες παιδιών

Δεν παρακάμπτει CAPTCHA ή μηχανισμούς προστασίας. Αν ένα site μπλοκάρει, η
αναζήτηση απλώς παραλείπεται και καταγράφεται. Booking.com και Skyscanner
δεν περιλαμβάνονται γιατί μπλοκάρουν τους αυτοματοποιημένους browsers.

Κάθε εκτέλεση κάνει ένα κομμάτι των αναζητήσεων (κυκλικά), ώστε ο φόρτος να
είναι μικρός και κάθε αναζήτηση να ανανεώνεται περίπου μία φορά τη μέρα.
"""
import base64
import datetime as dt
import json
import os
import re
import statistics
import time

GF_PAX = None  # ορίζεται στο run()


# ------------------------------------------------------ Google Flights tfs
def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _fld(num, wt, payload):
    key = _varint((num << 3) | wt)
    if wt == 0:
        return key + _varint(payload)
    return key + _varint(len(payload)) + payload


def _s(num, text):
    return _fld(num, 2, text.encode())


def gf_tfs(o, d, out, inn, pax):
    """Κωδικοποίηση αναζήτησης Google Flights (protobuf) με επιβάτες.
    pax: 1=ενήλικας(12+), 2=παιδί(2-11), 3=βρέφος σε θέση, 4=βρέφος στην αγκαλιά."""
    def leg(date, a, b):
        return _s(2, date) + _fld(13, 2, _s(2, a)) + _fld(14, 2, _s(2, b))
    msg = _fld(3, 2, leg(out, o, d)) + _fld(3, 2, leg(inn, d, o))
    for p in pax:
        msg += _fld(8, 0, p)
    msg += _fld(9, 0, 1) + _fld(19, 0, 1)  # economy, round trip
    return base64.urlsafe_b64encode(msg).decode().rstrip("=")


def gf_pax(family):
    pax = [1] * family["adults"]
    for a in family["children_ages"]:
        pax.append(4 if a < 2 else 2 if a < 12 else 1)
    return pax


# ---------------------------------------------------------- representative dates
def first_sat(d):
    while d.weekday() != 5:
        d += dt.timedelta(days=1)
    return d


def rep_dates(c, w):
    """Αντιπροσωπευτικές ημερομηνίες για κάθε περίοδο (για scraping)."""
    if w["season"].startswith("apokries"):
        out, inn = w["out_from"], w["in_from"]
    elif w["season"].startswith("easter"):
        easter = c.orthodox_easter(int(w["season"][-4:]))
        out, inn = easter - dt.timedelta(days=3), easter + dt.timedelta(days=3)
    else:
        out = first_sat(w["out_from"])
        inn = out + dt.timedelta(days=7)
    if out <= c.TODAY + dt.timedelta(days=2):
        return None
    return out, inn


# ---------------------------------------------------------------- browser
class Browser:
    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.b = self._pw.chromium.launch(headless=True, channel="chromium")
        self.ctx = self.b.new_context(locale="en-GB", timezone_id="Europe/Athens",
                                      viewport={"width": 1366, "height": 900})
        self.ctx.set_default_timeout(30000)
        self.page = self.ctx.new_page()
        self.google_consent_done = False

    def goto(self, url):
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def google_consent(self):
        if self.google_consent_done:
            return
        for name in ("Accept all", "Αποδοχή όλων"):
            btn = self.page.get_by_role("button", name=name)
            if btn.count():
                btn.first.click()
                self.page.wait_for_load_state("domcontentloaded")
                break
        self.google_consent_done = True

    def why(self):
        try:
            body = self.page.inner_text("body", timeout=3000)[:160].replace("\n", " ")
        except Exception:  # noqa: BLE001
            body = ""
        return f"«{self.page.title()[:60]}» {body}"

    def close(self):
        try:
            self.b.close()
        finally:
            self._pw.stop()


def _why(br):
    """Σύντομη περιγραφή της σελίδας όταν δεν βρέθηκαν αποτελέσματα (για το log)."""
    return br.why()


# ---------------------------------------------------------- Google Flights
GF_RE = re.compile(
    r"From ([\d,]+) euros round trip total\..*?(Nonstop|\d+ stops?) flights? with ([^.]+)\."
    r".*?Leaves .*? at (\d{1,2}:\d{2}\s?[AP]M) on \w+, (\w+ \d{1,2})", re.S)


def _to24(t):
    return dt.datetime.strptime(t.replace("\u202f", " ").strip(), "%I:%M %p").strftime("%H:%M")


def scrape_gflights(br, o, d, out, inn, pax):
    url = (f"https://www.google.com/travel/flights/search?tfs={gf_tfs(o, d, out.isoformat(), inn.isoformat(), pax)}"
           f"&hl=en&gl=GR&curr=EUR")
    br.goto(url)
    br.google_consent()
    try:
        br.page.wait_for_selector('[aria-label^="From "]', timeout=25000)
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"καμία πτήση · {_why(br)}") from None
    time.sleep(1.5)
    labels = br.page.eval_on_selector_all(
        "[aria-label]", 'els => els.map(e => e.getAttribute("aria-label")).filter(t => /^From \\d/.test(t))')
    res, seen = [], set()
    for lab in labels:
        m = GF_RE.search(lab.replace("\u202f", " "))
        if not m:
            continue
        price = int(m.group(1).replace(",", ""))
        stops = 0 if m.group(2) == "Nonstop" else int(m.group(2).split()[0])
        airline = m.group(3).replace(" and ", ", ").strip()
        k = (price, airline, m.group(4))
        if k in seen:
            continue
        seen.add(k)
        res.append(dict(total=price, stops=stops, airline=airline, dep=_to24(m.group(4))))
    if labels and not res:
        raise RuntimeError(f"άγνωστη μορφή τιμής: {labels[0][:160]}")
    res.sort(key=lambda r: (r["total"], r["stops"]))
    return res, url


# ------------------------------------------------------------------ Airbnb
AB_PRICE_RE = re.compile(r"€\s?([\d,]+)\s*(?:total|for \d+ nights?)")
AB_RATING_RE = re.compile(r"([\d.]+) out of 5 average rating, ([\d,]+) review")


def scrape_airbnb(br, q, ci, co, family):
    url = (f"https://www.airbnb.com/s/{q.replace(', ', '--').replace(' ', '-')}/homes"
           f"?checkin={ci.isoformat()}&checkout={co.isoformat()}&adults={family['adults']}"
           f"&children={len(family['children_ages'])}&currency=EUR&locale=en")
    br.goto(url)
    try:
        br.page.wait_for_selector('[itemprop="itemListElement"]', timeout=30000)
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"καμία κάρτα · {_why(br)}") from None
    time.sleep(2)
    cards = br.page.locator('[itemprop="itemListElement"]')
    res = []
    for i in range(min(cards.count(), 30)):
        c = cards.nth(i)
        try:
            txt = c.inner_text(timeout=5000)
        except Exception:  # noqa: BLE001
            continue
        prices = AB_PRICE_RE.findall(txt)
        if not prices:
            continue
        total = float(prices[-1].replace(",", ""))
        rm = AB_RATING_RE.search(txt)
        name_el = c.locator('meta[itemprop="name"]')
        url_el = c.locator('meta[itemprop="url"]')
        name = name_el.first.get_attribute("content") if name_el.count() else txt.split("\n")[0]
        link = url_el.first.get_attribute("content") if url_el.count() else None
        if link and not link.startswith("http"):
            link = "https://www.airbnb.com" + (link if link.startswith("/") else "/" + link.split("/", 1)[-1])
        lines = [x.strip() for x in txt.split("\n") if x.strip()]
        ptype = next((x for x in lines if " in " in x and len(x) < 60), None)
        bedrooms = next((x for x in lines if re.match(r"^\d+ bedrooms?$", x)), None)
        res.append(dict(name=name, link=link, total=total,
                        rating=float(rm.group(1)) if rm else None,
                        reviews=int(rm.group(2).replace(",", "")) if rm else None,
                        type=ptype, bedrooms=bedrooms))
    if not res and cards.count():
        lines = [x for x in cards.first.inner_text().split("\n")
                 if re.search(r"[€$£]|total|night|EUR|USD", x, re.I)]
        raise RuntimeError("άγνωστη μορφή τιμής: " + " | ".join(lines)[:200])
    res.sort(key=lambda r: r["total"])
    return res, url


# ----------------------------------------------------------- Google Hotels
GH_LABEL_RE = re.compile(r"^(.*?),\s*(?:€\s?([\d,. \xa0]+)|([\d,. \xa0]+)\s?€|EUR\s?([\d,.]+))\s*$")


def scrape_ghotels(br, q, ci, co, family):
    text = f"hotels in {q} {ci.day} {ci:%B %Y} to {co.day} {co:%B %Y}"
    url = "https://www.google.com/travel/search?hl=en&gl=GR&curr=EUR&q=" + text.replace(" ", "%20")
    br.goto(url)
    br.google_consent()
    pg = br.page
    pg.wait_for_selector('[aria-label^="Number of travelers"]', timeout=25000)
    # επισκέπτες: ενήλικες + παιδιά με ηλικίες
    pg.locator('[aria-label^="Number of travelers"]').first.click()
    time.sleep(0.8)
    cur_adults = 2
    for _ in range(family["adults"] - cur_adults):
        pg.get_by_role("button", name="Add adult").first.click()
    for _ in range(cur_adults - family["adults"]):
        pg.get_by_role("button", name="Remove adult").first.click()
    for i, age in enumerate(family["children_ages"], 1):
        pg.get_by_role("button", name="Add child").first.click()
        time.sleep(0.4)
        pg.locator(f'[aria-label="Age of child {i}"]').first.click()
        time.sleep(0.4)
        opt = pg.locator('[role="option"]:visible').filter(has_text=re.compile(rf"^\s*{age}\s*$"))
        opt.first.click()
        time.sleep(0.3)
    pg.get_by_role("button", name="Done").first.click()
    time.sleep(3)
    n = len(family["children_ages"]) + family["adults"]
    lab = pg.locator('[aria-label^="Number of travelers"]').first.get_attribute("aria-label") or ""
    if f"is {n}" not in lab:
        raise RuntimeError(f"Δεν ορίστηκαν σωστά οι επισκέπτες ({lab})")
    # φίλτρο βαθμολογίας 4+
    rb = pg.locator('button[aria-label^="4+ rating"]')
    if rb.count():
        rb.first.click()
        time.sleep(3)
    pg.wait_for_load_state("domcontentloaded")
    labels = pg.eval_on_selector_all(
        "a[aria-label]", 'els => els.map(e => [e.getAttribute("aria-label"), e.href])')
    nights = (co - ci).days
    res, seen = [], set()
    for lab, href in labels:
        m = GH_LABEL_RE.match(lab or "")
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        num = re.sub(r"[^\d]", "", next(g for g in m.groups()[1:] if g))
        if not num:
            continue
        night = float(num)
        res.append(dict(name=m.group(1), link=href, per_night=night, total=night * nights,
                        rating=None, type="hotel"))
    if not res:
        sample = [l for l, _ in labels if l and re.search(r"\d", l)][:5]
        raise RuntimeError(f"καμία τιμή ({len(labels)} labels, με αριθμούς: {sample}) · {_why(br)}")
    res.sort(key=lambda r: r["total"])
    return res, pg.url


# ------------------------------------------------------------- job planning
def rotate(con, key, jobs, n):
    if not jobs:
        return []
    row = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    ptr = int(row[0]) if row else 0
    ptr %= len(jobs)
    take = [jobs[(ptr + i) % len(jobs)] for i in range(min(n, len(jobs)))]
    con.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", (key, str((ptr + n) % len(jobs))))
    return take


def load_cache(con, key):
    row = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row else {}


def save_cache(c, con, key, cache, date_idx):
    today = c.TODAY.isoformat()
    cache = {k: v for k, v in cache.items() if k.split("|")[date_idx] >= today}
    con.execute("INSERT OR REPLACE INTO kv VALUES(?, ?)", (key, json.dumps(cache, ensure_ascii=False)))


# --------------------------------------------------------------------- main
def run(c, con, ryanair_flights):
    """c: το module collector (για config, ιστορικό, αξιολόγηση)."""
    S = c.CFG.get("scrape", {})
    if not S.get("enabled"):
        return [], [], []
    try:
        import playwright  # noqa: F401
    except ImportError:
        return [], [], ["Scraping: λείπει το Playwright (pip install playwright && python -m playwright install chromium)"]

    fam = c.FAMILY
    pax = gf_pax(fam)
    windows = c.travel_windows()
    errors = []
    t0 = time.time()

    # ---- Google Flights jobs
    gf_jobs = []
    for o in S.get("gflights_origins", ["ATH"]):
        for r in S.get("gflights_routes", []):
            if r["iata"] == o or not c.dest_allowed(r["cc"], r["iata"]):
                continue
            for w in windows:
                rd = rep_dates(c, w)
                if rd:
                    gf_jobs.append((o, r, w, rd))
    gf_take = rotate(con, "gf_ptr", gf_jobs, S.get("gflights_per_run", 24))

    # ---- stays jobs
    wanted = S.get("stays_windows", ["summer"])
    st_windows = [w for w in windows if any(w["season"].startswith(x) for x in wanted)]
    st_jobs = []
    for p in S.get("stays_places", []):
        for w in st_windows:
            rd = rep_dates(c, w)
            if rd:
                st_jobs.append((p, w, rd))
    per = S.get("stays_per_run", {"airbnb": 10, "google_hotels": 6})
    ab_take = rotate(con, "ab_ptr", st_jobs, per.get("airbnb", 10))
    gh_take = rotate(con, "gh_ptr", st_jobs, per.get("google_hotels", 6))
    # Από servers εκτός Ευρώπης (GitHub Actions) το Google Hotels αγνοεί προορισμό και
    # νόμισμα (γενικά αποτελέσματα «Ελλάδα» σε $), οπότε εκεί δεν το χρησιμοποιούμε.
    if os.environ.get("GITHUB_ACTIONS") and not S.get("google_hotels_online", False):
        gh_take = []

    # combo: διαμονή στις ημερομηνίες των καλύτερων πτήσεων Ryanair
    best = sorted([f for f in ryanair_flights if f["deal"] in ("great", "good")],
                  key=lambda f: (-f["score"], f["family_total"]))
    place_by_ap = {a: p for p in S.get("stays_places", []) for a in p.get("airports", [])}
    combo, seen_dest = [], set()
    for f in best:
        if f["dest"] in seen_dest:
            continue
        seen_dest.add(f["dest"])
        ci = dt.date.fromisoformat(f["out_arr"][:10])
        co = dt.date.fromisoformat(f["in_dt"][:10])
        place = place_by_ap.get(f["dest"]) or {"name": f["city"], "q": f"{f['city']}, {f['country']}"}
        combo.append(({**place, "airports": [f["dest"]] + list(place.get("airports", []))},
                      {"key": f["window"], "label": f["window_label"]}, (ci, co), f))
        if len(combo) >= S.get("combo_from_best_flights", 3):
            break

    gf_cache = load_cache(con, "gf_cache")
    ab_cache = load_cache(con, "ab_cache")
    gh_cache = load_cache(con, "gh_cache")

    br = None
    try:
        br = Browser()
        # Google Flights
        ok = 0
        for o, r, w, (out, inn) in gf_take:
            key = f"GF|{o}|{r['iata']}|{w['key']}|{out.isoformat()}|{inn.isoformat()}"
            try:
                res, url = scrape_gflights(br, o, r["iata"], out, inn, pax)
                if res:
                    gf_cache[key] = {"ts": c.NOW, "res": res[:5], "o": o, "r": r, "w": w["key"],
                                     "wl": w["label"], "season": w["season"],
                                     "out": out.isoformat(), "in": inn.isoformat()}
                    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)",
                                (key, c.NOW, res[0]["total"] / c.N_PAX, res[0]["total"], "{}"))
                    ok += 1
            except Exception as ex:  # noqa: BLE001
                errors.append(f"Google Flights {o}-{r['iata']} {out}: {str(ex)[:120]}")
            time.sleep(1.5)
        c.record_run(con, "Google Flights", ok > 0 or not gf_take, ok, "; ".join(errors[-3:]))
        c.log(f"Google Flights: {ok}/{len(gf_take)} αναζητήσεις OK")
        con.commit()

        # Airbnb (κυκλικά + combo)
        ok = skipped = 0
        for p, w, (ci, co), *fl in ab_take + combo:
            key = f"AB|{p['q']}|{ci.isoformat()}|{co.isoformat()}"
            cached = ab_cache.get(key)
            if cached and c.NOW - cached["ts"] < 12 * 3600:
                skipped += 1
                continue
            try:
                res, url = scrape_airbnb(br, p["q"], ci, co, fam)
                if res:
                    good = [x for x in res if (x["rating"] or 0) >= S.get("min_airbnb_rating", 4.5)] or res
                    ab_cache[key] = {"ts": c.NOW, "res": good[:6], "name": p["name"], "q": p["q"],
                                     "ap": p.get("airports", []),
                                     "w": w["key"], "wl": w["label"], "ci": ci.isoformat(),
                                     "co": co.isoformat(), "url": url,
                                     "median": statistics.median([x["total"] for x in res])}
                    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)",
                                (key, c.NOW, good[0]["total"], good[0]["total"], "{}"))
                    ok += 1
            except Exception as ex:  # noqa: BLE001
                errors.append(f"Airbnb {p['name']} {ci}: {str(ex)[:120]}")
            time.sleep(2)
        c.record_run(con, "Airbnb", ok > 0, ok, "")
        c.log(f"Airbnb: {ok} αναζητήσεις OK, {skipped} πρόσφατες από cache")
        con.commit()

        # Google Hotels
        ok = 0
        for p, w, (ci, co) in gh_take:
            key = f"GH|{p['q']}|{ci.isoformat()}|{co.isoformat()}"
            try:
                res, url = scrape_ghotels(br, p["q"], ci, co, fam)
                if res:
                    gh_cache[key] = {"ts": c.NOW, "res": res[:6], "name": p["name"], "q": p["q"],
                                     "ap": p.get("airports", []),
                                     "w": w["key"], "wl": w["label"], "ci": ci.isoformat(),
                                     "co": co.isoformat(), "url": url,
                                     "median": statistics.median([x["total"] for x in res])}
                    con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)",
                                (key, c.NOW, res[0]["total"], res[0]["total"], "{}"))
                    ok += 1
            except Exception as ex:  # noqa: BLE001
                errors.append(f"Google Hotels {p['name']} {ci}: {str(ex)[:120]}")
            time.sleep(2)
        c.record_run(con, "Google Hotels", ok > 0 or not gh_take, ok, "")
        c.log(f"Google Hotels: {ok}/{len(gh_take)} αναζητήσεις OK")
    except Exception as ex:  # noqa: BLE001
        errors.append(f"Scraping browser: {str(ex)[:200]}")
        c.log(errors[-1])
    finally:
        if br:
            try:
                br.close()
            except Exception:  # noqa: BLE001
                pass

    save_cache(c, con, "gf_cache", gf_cache, 4)
    save_cache(c, con, "ab_cache", ab_cache, 2)
    save_cache(c, con, "gh_cache", gh_cache, 2)
    con.commit()
    c.log(f"Scraping ολοκληρώθηκε σε {time.time() - t0:.0f}s, {len(errors)} σφάλματα")

    gf_flights = build_flights(c, con, gf_cache)
    return gf_flights, build_stays(c, con, ab_cache, gh_cache, ryanair_flights + gf_flights), errors


# ------------------------------------------------------------ build output
def _hist(c, con, key, upto):
    h = c.history(con, key)
    prev = [p for ts, p in h if ts < upto]
    return h, prev


def build_flights(c, con, cache):
    items = []
    pax = gf_pax(c.FAMILY)
    extras = c.flight_extras()
    peers = {}
    for key, v in cache.items():
        peers.setdefault((v["o"], v["r"]["iata"], v["season"]), []).append((v["w"], v["res"][0]["total"]))
    cache = {k: v for k, v in cache.items() if c.dest_allowed(v["r"]["cc"], v["r"]["iata"])
             and v["o"] in c.CFG["origins"]}
    for key, v in cache.items():
        best = v["res"][0]
        total = best["total"]
        pp = total / c.N_PAX
        h, prev = _hist(c, con, key, v["ts"])
        prev_pp = [p for p in prev]
        others = [t / c.N_PAX for w, t in peers[(v["o"], v["r"]["iata"], v["season"])] if w != v["w"]]
        score, label, reasons = c.assess(prev_pp, pp, others, None, None)
        r = v["r"]
        items.append(dict(
            kind="flight", source="Google Flights", key=key, season=v["season"],
            window=v["w"], window_label=v["wl"], origin=v["o"], origin_name=v["o"],
            dest=r["iata"], dest_name=r["name"], city=r["name"].split(" (")[0],
            country=r["country"], cc=r["cc"], region="gr" if r["cc"] == "gr" else "abroad",
            out_dt=f"{v['out']}T{best['dep']}:00", out_arr=f"{v['out']}T00:00:00",
            in_dt=f"{v['in']}T00:00:00", in_time_unknown=True,
            airline=best["airline"], stops=best["stops"],
            nights=(dt.date.fromisoformat(v["in"]) - dt.date.fromisoformat(v["out"])).days,
            pp=round(pp, 2), prev_pp=None, family_fare=total, family_total=round(total + extras),
            options=v["res"][:4], fetched=v["ts"],
            book_url=(f"https://www.google.com/travel/flights/search?tfs="
                      f"{gf_tfs(v['o'], r['iata'], v['out'], v['in'], pax)}&hl=el&curr=EUR"),
            score=score, deal=label, reasons=reasons,
            hist=[[ts, p] for ts, p in h][-60:], hist_n=len(prev),
            hist_min=min(prev) if prev else None,
            hist_median=statistics.median(prev) if prev else None))
    return items


def build_stays(c, con, ab_cache, gh_cache, all_flights):
    """Διαμονές· όπου υπάρχει πτήση ίδιων ημερομηνιών προς το ίδιο μέρος,
    φτιάχνει και «πακέτο» πτήση + διαμονή."""
    fl_by_dest = {}
    for f in all_flights:
        k = (f["dest"], f["out_arr"][:10], f["in_dt"][:10])
        if k not in fl_by_dest or f["family_total"] < fl_by_dest[k]["family_total"]:
            fl_by_dest[k] = f
    out = []
    for src, cache in (("Airbnb", ab_cache), ("Google Hotels", gh_cache)):
        for key, v in cache.items():
            # μόνο τρέχοντες προορισμοί (παλιές ελληνικές αναζητήσεις δεν έχουν "ap")
            if not v["res"] or not v.get("ap"):
                continue
            top = v["res"][0]
            h, prev = _hist(c, con, key, v["ts"])
            score, label, reasons = c.assess(prev, top["total"])
            ci, co = dt.date.fromisoformat(v["ci"]), dt.date.fromisoformat(v["co"])
            item = dict(kind="room", source=src, key=key, place=v["name"], q=v["q"],
                        window_label=v["wl"], check_in=v["ci"], check_out=v["co"],
                        nights=(co - ci).days, region="abroad",
                        results=v["res"][:5], total=top["total"], median=v.get("median"),
                        search_url=v.get("url"), fetched=v["ts"],
                        score=score, deal=label, reasons=reasons,
                        hist=[[ts, p] for ts, p in h][-60:], hist_n=len(prev))
            cands = [fl_by_dest[(a, v["ci"], v["co"])] for a in v["ap"] if (a, v["ci"], v["co"]) in fl_by_dest]
            f = min(cands, key=lambda x: x["family_total"]) if cands else None
            if f:
                item.update(origin=f["origin"], combo_total=round(top["total"] + f["family_total"]),
                            combo_flight=dict(source=f["source"], airline=f.get("airline") or "Ryanair",
                                              total=f["family_total"],
                                              url=f.get("book_url")))
            out.append(item)
    return out
