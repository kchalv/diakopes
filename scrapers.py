"""
Scrapers με πραγματικό browser (Playwright/Chromium) για πηγές χωρίς δημόσιο API:

  * Google Flights  – όλες οι αεροπορικές εκτός από τις εξαιρούμενες (config → excluded_airlines),
                      τιμή για όλη την οικογένεια, μία αναζήτηση ανά προορισμό και εβδομάδα
  * Google Hotels   – μόνο οργανωμένα ξενοδοχεία 3★+ με βαθμολογία 4+ (συγκεντρώνει Booking.com,
                      Expedia κ.ά.), η οικογένεια σε δύο δωμάτια, σύνδεσμοι κράτησης στο Booking.com

Διαμονές εξωτερικού ψάχνονται μόνο για τις εβδομάδες όπου τα αεροπορικά προς το ίδιο
μέρος έχουν έκπτωση (βλ. stay_targets), ώστε κάθε διαμονή να είναι πακέτο με πτήση.

Δεν παρακάμπτει CAPTCHA ή μηχανισμούς προστασίας. Αν ένα site μπλοκάρει, η
αναζήτηση απλώς παραλείπεται και καταγράφεται. Booking.com και Skyscanner
δεν διαβάζονται απευθείας γιατί μπλοκάρουν τους αυτοματοποιημένους browsers.
Airbnb, διαμερίσματα και hostels δεν ψάχνονται καθόλου.

Κάθε σάρωση κάνει ΟΛΕΣ τις αναζητήσεις πτήσεων (κάθε προορισμός × κάθε εβδομάδα). Στο
GitHub Actions μοιράζονται σε παράλληλα τμήματα (gf_shard), το καθένα σε δικό του server.
"""
import base64
import datetime as dt
import json
import os
import re
import statistics
import time
import unicodedata
import urllib.parse

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


def gf_tfs(o, d, out, inn, pax, nonstop=False):
    """Κωδικοποίηση αναζήτησης Google Flights (protobuf) με επιβάτες.
    pax: 1=ενήλικας(12+), 2=παιδί(2-11), 3=βρέφος σε θέση, 4=βρέφος στην αγκαλιά.
    nonstop: φίλτρο «χωρίς στάση» (μέγιστες στάσεις = 0) σε κάθε σκέλος."""
    def leg(date, a, b):
        return (_s(2, date) + (_fld(5, 0, 0) if nonstop else b"")
                + _fld(13, 2, _s(2, a)) + _fld(14, 2, _s(2, b)))
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
    if w["season"].startswith(("apokries", "summer")):
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


def gf_groups(pax):
    """Το Google Flights συχνά δεν τιμολογεί 5+ θέσεις μαζί (λείπουν εταιρείες όπως Aegean,
    ITA, TAP, ενώ για έως 4 επιβάτες εμφανίζονται). Γι' αυτό η οικογένεια ψάχνεται σε δύο
    κρατήσεις στην ίδια πτήση: 1 ενήλικας + τα παιδιά, και οι υπόλοιποι ενήλικες."""
    if len(pax) <= 4:
        return [pax]
    adults = [p for p in pax if p == 1]
    kids = [p for p in pax if p != 1]
    return [adults[:1] + kids, adults[1:]] if len(adults) > 1 else [pax]


def scrape_gflights(br, o, d, out, inn, pax, nonstop=False):
    """Τιμή για όλη την οικογένεια: άθροισμα των κρατήσεων του gf_groups για την ίδια πτήση
    (ίδια εταιρεία, ώρα αναχώρησης και στάσεις)."""
    groups = gf_groups(pax)
    total, url = None, None
    for g in groups:
        res, url = _gf_search(br, o, d, out, inn, g, nonstop)
        part = {}
        for x in res:
            part.setdefault((x["airline"], x["dep"], x["stops"]), x["total"])
        total = part if total is None else {k: v + part[k] for k, v in total.items() if k in part}
        if not total:
            break
        if len(groups) > 1:
            time.sleep(1.5)
    res = [dict(total=v, stops=k[2], airline=k[0], dep=k[1]) for k, v in (total or {}).items()]
    res.sort(key=lambda r: (r["total"], r["stops"]))
    return res, url


def _gf_search(br, o, d, out, inn, pax, nonstop=False):
    url = (f"https://www.google.com/travel/flights/search?tfs={gf_tfs(o, d, out.isoformat(), inn.isoformat(), pax, nonstop)}"
           f"&hl=en&gl=GR&curr=EUR")
    br.goto(url)
    br.google_consent()
    # Το Google φορτώνει τις τιμές σταδιακά (για 5 επιβάτες εταιρείες όπως η Aegean έρχονται
    # 9–12 δευτερόλεπτα μετά τις πρώτες), άρα περιμένουμε να σταθεροποιηθεί η λίστα.
    # Τρεις εκβάσεις: λίστα πτήσεων, «δεν υπάρχουν πτήσεις» (π.χ. καμία απευθείας: κενή λίστα,
    # όχι σφάλμα), ή προσωρινό σφάλμα («Oops, something went wrong»: νέα προσπάθεια).
    # Το «δεν υπάρχουν πτήσεις» βγαίνει μερικές φορές και κατά λάθος, γι' αυτό γίνεται δεκτό
    # μόνο αν το δείξει δύο φορές.
    probe = """() => { const t = document.body ? document.body.innerText : '';
        return {n: document.querySelectorAll('[aria-label^="From "]').length,
                done: /Prices are currently|price insights/i.test(t),
                oops: /something went wrong/i.test(t),
                none: /No (nonstop )?flights found|No options matching/i.test(t)}; }"""
    state, nones = "timeout", 0
    for attempt in range(4):
        state, last, stable, t0 = "timeout", -1, 0, time.time()
        while time.time() - t0 < 32:
            time.sleep(2)
            try:
                i = br.page.evaluate(probe)
            except Exception:  # noqa: BLE001
                continue
            el = time.time() - t0
            if i["n"]:
                stable = stable + 1 if i["n"] == last else 0
                last = i["n"]
                if (i["done"] and stable >= 1) or stable >= 4:
                    state = "ok"
                    break
            elif i["oops"] and el >= 8:
                state = "oops"
                break
            elif i["none"] and el >= 10:
                state = "none"
                break
        if state == "timeout" and last > 0:
            state = "ok"
        nones += state == "none"
        if state == "ok" or nones >= 2:
            break
        time.sleep(4 + 3 * attempt)
        br.page.reload(wait_until="domcontentloaded", timeout=60000)
    if state == "none":
        return [], url
    if state != "ok":
        raise RuntimeError(f"καμία απάντηση ({state}) · {_why(br)}") from None
    try:
        more = br.page.get_by_role("button", name=re.compile(r"more flights", re.I))
        if more.count():
            more.first.click(timeout=5000)
            time.sleep(2.5)
    except Exception:  # noqa: BLE001
        pass
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
    if not res:
        raise RuntimeError(f"άγνωστη μορφή τιμής: {(labels or ['(καμία ετικέτα)'])[0][:160]}")
    res.sort(key=lambda r: (r["total"], r["stops"]))
    return res, url


# ----------------------------------------------------------- Google Hotels
# Μόνο οργανωμένα ξενοδοχεία (όχι Airbnb, διαμερίσματα ή hostels). Οι τιμές διαβάζονται από το
# Google Hotels, που συγκεντρώνει Booking.com, Expedia κ.ά. Το ίδιο το Booking.com δεν διαβάζεται
# αυτόματα (δίνει κενή σελίδα στους αυτοματοποιημένους browsers), γι' αυτό κάθε ξενοδοχείο έχει
# έτοιμο σύνδεσμο προς το Booking.com για τις ίδιες ημερομηνίες και όλη την οικογένεια.
# Στη Γαλλία γράφει «4-star tourist hotel»· τα «tourist residence» (διαμερίσματα) μένουν εκτός.
GH_STAR_RE = re.compile(r"Amenities for (.+?), an? (\d)-star (?:tourist )?hotel")
GH_PRICE_RE = re.compile(r"^Prices starting from €\s?([\d,.\s\xa0]+),\s*(.+)$")
GH_RATING_RE = re.compile(r"^([\d.]+) out of 5 stars from ([\d,.]+) reviews?,\s*(.+)$")
# Λέξεις στο όνομα που δείχνουν ότι δεν είναι οργανωμένο ξενοδοχείο (το Google δίνει «αστέρια» και
# σε hostels, ξενώνες και ενοικιαζόμενα δωμάτια). Αλλάζουν από το config → scrape.hotel_exclude_words.
GH_EXCLUDE_WORDS = ["hostel", "hostal", "hostelle", "ostello", "albergue", "auberge", "pension",
                    "guest house", "guesthouse", "bed & breakfast", "bed and breakfast", "b & b",
                    "apartment", "apartamento", "appartement", "appartament", "rooms", "capsule", "pod ",
                    "camping", "agriturismo", " - b&b", "remote check-in", "self check-in", "meininger", "generator", "onefam",
                    "a&o ", "safestay", "wombat", "st christopher", "yellowsquare", "siesta & go", "combo,"]


def _plain(text):
    """Πεζά χωρίς τόνους/διαλυτικά, για συγκρίσεις ονομάτων (Zürich = Zurich)."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch)).lower()


GH_KEY = "GH2"  # κλειδί cache/ιστορικού· το παλιό "GH" ήταν τιμή ενός δωματίου, μη συγκρίσιμη


def gh_rooms(family):
    """Δωμάτια ξενοδοχείου για την οικογένεια, ως [(ενήλικες, [ηλικίες παιδιών])]. Τα ξενοδοχεία
    σχεδόν ποτέ δεν δίνουν τιμή για 5 άτομα σε ένα δωμάτιο, άρα (όπως και στις πτήσεις) η
    οικογένεια ψάχνεται σε δύο δωμάτια του ίδιου ξενοδοχείου: 2 ενήλικες, και οι υπόλοιποι
    ενήλικες με τα παιδιά."""
    adults, kids = family["adults"], list(family["children_ages"])
    if adults + len(kids) <= 3 or adults < 2:
        return [(adults, kids)]
    if adults == 2:
        h = (len(kids) + 1) // 2
        return [(1, kids[:h]), (1, kids[h:])]
    return [(2, []), (adults - 2, kids)]


def gh_ts(ci, co, adults, ages, stars=(3, 4, 5), rating4=True, by_price=False, cur="EUR"):
    """Κωδικοποίηση αναζήτησης Google Hotels (protobuf): επισκέπτες με ηλικίες παιδιών, ημερομηνίες,
    νόμισμα, κατηγορίες αστέρων, βαθμολογία 4+ και (προαιρετικά) ταξινόμηση κατά τιμή."""
    g = b"".join(_fld(1, 2, _fld(1, 0, 3)) for _ in range(adults))
    g += b"".join(_fld(1, 2, _fld(1, 0, 2) + _fld(2, 0, a)) for a in ages) + _fld(2, 0, 1)

    def d(x):
        return _fld(1, 0, x.year) + _fld(2, 0, x.month) + _fld(3, 0, x.day)
    dates = _fld(2, 2, _fld(1, 2, d(ci)) + _fld(2, 2, d(co)) + _fld(3, 0, (co - ci).days))
    f1 = ((_fld(2, 2, bytes(stars)) if stars else b"") + (_fld(5, 0, 3) if by_price else b"")
          + _s(7, cur))
    f5 = _fld(1, 2, f1) + (_fld(4, 2, _fld(3, 0, 1)) + _fld(5, 0, 8) if rating4 else b"")
    msg = _fld(1, 0, 1) + _fld(2, 2, g) + _fld(3, 2, _fld(2, 2, dates)) + _fld(5, 2, f5)
    return base64.urlsafe_b64encode(msg).decode().rstrip("=")


def booking_url(ss, ci, co, family, hotels_only=True):
    """Έτοιμη αναζήτηση στο Booking.com για όλη την οικογένεια (σε τόσα δωμάτια όσα το gh_rooms)."""
    q = [("ss", ss), ("checkin", ci.isoformat()), ("checkout", co.isoformat()),
         ("group_adults", family["adults"]), ("group_children", len(family["children_ages"]))]
    q += [("age", a) for a in family["children_ages"]]
    q += [("no_rooms", len(gh_rooms(family))), ("selected_currency", "EUR")]
    if hotels_only:
        q.append(("nflt", "ht_id=204"))  # τύπος καταλύματος: ξενοδοχεία
    return "https://www.booking.com/searchresults.el.html?" + urllib.parse.urlencode(q)


def _gh_read(pg):
    """Διαβάζει τα ξενοδοχεία που δείχνει τώρα η σελίδα (περιμένει να σταθεροποιηθούν οι τιμές, που
    έρχονται σταδιακά). Επιστρέφει ({όνομα: (τιμή/νύχτα σε €, αστέρια, βαθμολογία, κριτικές)},
    πλήθος ξενοδοχείων, δείγμα ετικετών τιμής)."""
    labels, last, stable, t0 = [], -1, 0, time.time()
    while time.time() - t0 < 12:
        time.sleep(1.5)
        labels = pg.eval_on_selector_all(
            "[aria-label]", 'els => els.map(e => e.getAttribute("aria-label") || "")'
                            '.filter(t => /^Prices starting|out of 5 stars/.test(t))')
        n = sum(1 for x in labels if x.startswith("Prices"))
        stable = stable + 1 if n == last else 0
        last = n
        if (n and stable >= 2) or (not n and time.time() - t0 > 7):
            break
    body = pg.inner_text("body")
    cls = {n.strip(): int(st) for n, st in GH_STAR_RE.findall(body)}
    ratings, out = {}, {}
    for lab in labels:
        m = GH_RATING_RE.match(lab)
        if m:
            ratings[m.group(3).strip()] = (float(m.group(1)), int(re.sub(r"\D", "", m.group(2)) or 0))
    for lab in labels:
        m = GH_PRICE_RE.match(lab)
        if not m or m.group(2).strip() not in cls:
            continue
        num = re.sub(r"\D", "", re.sub(r"[.,]\d{2}$", "", m.group(1).strip()))
        if num:
            name = m.group(2).strip()
            if name not in ratings:
                # Σε μια παραλλαγή της σελίδας η ετικέτα βαθμολογίας δεν έχει το όνομα: τότε η
                # βαθμολογία διαβάζεται από το κείμενο της κάρτας («Όνομα / €62 / 4.0 / (4.5K)»· από τους
                # servers του GitHub γράφει «4.4/5»).
                rm = re.search(re.escape(name) + r"\n(?:[^\n]*\n){0,3}?(\d(?:\.\d)?)(?:/5)?\n\(([\d.,]+)(K?)\)", body)
                if rm:
                    cnt = float(rm.group(2).replace(",", "")) * (1000 if rm.group(3) else 1)
                    ratings[name] = (float(rm.group(1)), int(cnt))
            out[name] = (float(num), cls[name]) + ratings.get(name, (None, None))
    return out, len(cls), [x for x in labels if x.startswith("Prices")][:2]


_GH_DIAG = False


def _gh_page(br, q, ci, co, adults, ages, stars, by_price, pages=2):
    """Αποτελέσματα μιας αναζήτησης (οι πρώτες `pages` σελίδες):
    {όνομα: (τιμή/νύχτα σε €, αστέρια, βαθμολογία, κριτικές)}.
    Το Google δίνει πού και πού μια κενή παραλλαγή της σελίδας (χωρίς αποτελέσματα): έως 3 προσπάθειες."""
    url = ("https://www.google.com/travel/search?hl=en&gl=GR&curr=EUR&q="
           + urllib.parse.quote("hotels in " + q) + "&ts=" + gh_ts(ci, co, adults, ages, stars, True, by_price))
    pg = br.page
    err = "καμία απάντηση"
    for attempt in range(3):
        if attempt:
            time.sleep(4 * attempt)
        br.goto(url)
        br.google_consent()
        try:
            pg.wait_for_selector('[aria-label^="Number of travelers"]', timeout=25000)
        except Exception:  # noqa: BLE001
            err = f"δεν φόρτωσε · {_why(br)}"
            continue
        out, n_cls, sample = _gh_read(pg)
        # Ο τίτλος λέει για ποιο μέρος είναι τα αποτελέσματα («Search for accommodations in Rome»):
        # αν το Google αγνόησε τον προορισμό, η σελίδα δεν χρησιμοποιείται.
        m = re.match(r"Search for accommodations in (.+?) - Google", pg.title())
        where = _plain(m.group(1) if m else "")
        if not where or not (where in _plain(q) or _plain(q.split(",")[0]) in where):
            err = f"λάθος ή κενή σελίδα · {_why(br)}"
            continue
        if not out:
            err = f"καμία τιμή σε € ({n_cls} ξενοδοχεία, δείγμα: {sample})"
            continue
        # Πού και πού η σελίδα έρχεται χωρίς καμία βαθμολογία/κριτικές: νέα προσπάθεια (στην
        # δεύτερη γίνεται δεκτή όπως είναι, και οι κριτικές παίρνονται από το άλλο δωμάτιο· αν δεν
        # υπάρχουν πουθενά — όπως από τους servers του GitHub — το ξενοδοχείο δεν κόβεται γι' αυτό:
        # μένουν το φίλτρο βαθμολογίας 4+ του ίδιου του Google, οι λέξεις στο όνομα και η τιμή).
        if all(v[2] is None for v in out.values()):
            global _GH_DIAG
            if not _GH_DIAG:  # μία φορά ανά σάρωση: πώς μοιάζει η κάρτα, για διάγνωση από το log
                _GH_DIAG = True
                body, name = pg.inner_text("body"), next(iter(out))
                i = body.find(name)
                print(f"  Google Hotels χωρίς βαθμολογίες ({q}): {body[max(i, 0):max(i, 0) + 300]!r}")
            if attempt < 1:
                err = "σελίδα χωρίς βαθμολογίες"
                continue
        for _ in range(pages - 1):  # επόμενες σελίδες αποτελεσμάτων
            try:
                nxt = pg.locator('button:has-text("Next")').locator("visible=true")
                if not nxt.count():
                    break
                nxt.last.scroll_into_view_if_needed(timeout=5000)
                nxt.last.click(timeout=5000)
                time.sleep(2.5)
                more = _gh_read(pg)[0]
            except Exception:  # noqa: BLE001
                break
            for k, v in more.items():
                out.setdefault(k, v)
        return out, url
    raise RuntimeError(err)


def scrape_ghotels(br, q, ci, co, family, S=None):
    """Ξενοδοχεία για όλη την οικογένεια: άθροισμα των δωματίων του gh_rooms στο ίδιο ξενοδοχείο.
    Κάθε δωμάτιο ψάχνεται δύο φορές (προτεινόμενα και φθηνότερα πρώτα), ώστε να βρεθούν περισσότερα
    ξενοδοχεία με τιμή και για τα δύο δωμάτια. Κόβονται όσα δεν είναι οργανωμένα ξενοδοχεία:
    λέξεις όπως hostel στο όνομα, λίγες κριτικές, και τιμές πολύ κάτω από τα υπόλοιπα (κρεβάτι σε
    κοιτώνα αντί για δωμάτιο)."""
    S = S or {}
    stars = tuple(range(int(S.get("min_hotel_stars", 3)), 6))
    words = [_plain(w) for w in S.get("hotel_exclude_words", GH_EXCLUDE_WORDS)]
    min_reviews = int(S.get("min_hotel_reviews", 300))
    rooms = gh_rooms(family)
    nights = (co - ci).days
    per_room, url = [], None
    for adults, ages in rooms:
        found = {}
        for by_price in (False, True):
            try:
                part, u = _gh_page(br, q, ci, co, adults, ages, stars, by_price)
            except Exception:  # noqa: BLE001
                if by_price and found:
                    continue  # αρκεί η πρώτη σελίδα
                raise
            url = url or u
            for k, v in part.items():
                found.setdefault(k, v)
            time.sleep(1.5)
        per_room.append(found)
    place = q.split(",")[0]
    res, cut = [], {"δωμάτια": 0, "όνομα": 0, "κριτικές": 0}
    for name, first in per_room[0].items():
        if first[2] is None:  # βαθμολογία από όποιο δωμάτιο την έχει
            first = first[:2] + next((r[name][2:] for r in per_room if name in r and r[name][2] is not None),
                                     (None, None))
        why = ("δωμάτια" if any(name not in r for r in per_room)
               else "όνομα" if any(w in _plain(name) + " " for w in words)
               else "κριτικές" if first[3] is not None and first[3] < min_reviews else None)
        if why:
            cut[why] += 1
            continue
        split = [r[name][0] for r in per_room]
        night = sum(split)
        res.append(dict(name=name, link=booking_url(f"{name}, {place}", ci, co, family, False),
                        per_night=night, total=night * nights, split=split, stars=first[1],
                        rating=first[2], reviews=first[3], type="hotel"))
    if not res:
        raise RuntimeError(f"κανένα κατάλληλο ξενοδοχείο (ανά δωμάτιο {[len(r) for r in per_room]}, κόπηκαν: {cut})")
    if len(res) >= 4:
        floor = S.get("hotel_min_price_ratio", 0.6) * statistics.median(r["total"] for r in res)
        res = [r for r in res if r["total"] >= floor]
    res.sort(key=lambda r: r["total"])
    return res, url


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


def pax_sig(family):
    """Σύνθεση οικογένειας· οι αποθηκευμένες διαμονές άλλης σύνθεσης αγνοούνται."""
    return f"{family['adults']}+{','.join(map(str, family['children_ages']))}"


def stay_targets(c, S, flights):
    """Πού και πότε ψάχνουμε διαμονή: μόνο στις εβδομάδες όπου τα αεροπορικά προς το ίδιο
    μέρος έχουν έκπτωση (έως stays_per_place ανά μέρος, πρώτα οι μεγαλύτερες εκπτώσεις).
    Όσο ένα μέρος δεν έχει ακόμη έκπτωση, μπαίνει η φθηνότερη εβδομάδα πτήσεών του.
    Επιστρέφει [(μέρος, περίοδος, (check-in, check-out), πτήση, αιτιολογία)]."""
    place_by_ap = {}
    for p in S.get("stays_places", []):
        if not p.get("rooms_only"):
            for a in p.get("airports", []):
                place_by_ap.setdefault(a, p)
    by_place = {}
    for f in flights:
        p = place_by_ap.get(f["dest"])
        if p:
            by_place.setdefault(p["q"], (p, []))[1].append(f)
    n = S.get("stays_per_place", 3)
    out, seen = [], set()
    for p, fs in by_place.values():
        fs = sorted(fs, key=lambda f: (-f["score"], f["family_total"]))
        picks = [(f, "Έκπτωση στα αεροπορικά αυτής της εβδομάδας")
                 for f in fs if f["deal"] in ("great", "good")][:n]
        if not picks:
            picks = [(min(fs, key=lambda f: f["family_total"]),
                      "Η φθηνότερη εβδομάδα πτήσεων για τον προορισμό")]
        for f, why in picks:
            ci = dt.date.fromisoformat(f["out_arr"][:10])
            co = dt.date.fromisoformat(f["in_dt"][:10])
            if (p["q"], ci, co) in seen:
                continue
            seen.add((p["q"], ci, co))
            out.append((p, {"key": f["window"], "label": f["window_label"]}, (ci, co), f, why))
    out.sort(key=lambda t: (-t[3]["score"], t[3]["family_total"]))
    return out


# ------------------------------------------------- Google Flights: πλήρης σάρωση
def gf_all_jobs(c):
    """ΟΛΕΣ οι αναζητήσεις πτήσεων μιας σάρωσης: κάθε προορισμός × κάθε εβδομάδα."""
    S = c.CFG.get("scrape", {})
    pax = gf_pax(c.FAMILY)
    # "|ns": χωριστό ιστορικό για απευθείας· "|2": τιμή σε δύο κρατήσεις (gf_groups)
    suffix = ("|ns" if c.CFG.get("nonstop_only") else "") + ("|2" if len(gf_groups(pax)) > 1 else "")
    jobs = []
    for o in S.get("gflights_origins", ["ATH"]):
        for r in S.get("gflights_routes", []):
            if r["iata"] == o or not c.dest_allowed(r["cc"], r["iata"]):
                continue
            for w in c.flight_windows():
                rd = rep_dates(c, w)
                if rd:
                    out, inn = rd[0].isoformat(), rd[1].isoformat()
                    jobs.append({"key": f"GF|{o}|{r['iata']}|{w['key']}|{out}|{inn}{suffix}",
                                 "o": o, "r": r, "w": w["key"], "wl": w["label"],
                                 "season": w["season"], "out": out, "in": inn})
    return jobs


def gf_scrape_jobs(c, br, jobs):
    """Εκτελεί τις αναζητήσεις. Όσες αποτύχουν (προσωρινά σφάλματα του Google) ξαναγίνονται
    σε δεύτερο και τρίτο γύρο. Επιστρέφει μία εγγραφή ανά αναζήτηση με status ok / none / error."""
    pax = gf_pax(c.FAMILY)
    nonstop = bool(c.CFG.get("nonstop_only"))
    recs = {}
    for rnd in range(3):
        todo = jobs if rnd == 0 else [j for j in jobs if recs[j["key"]]["status"] == "error"]
        if rnd and todo:
            c.log(f"Google Flights: προσπάθεια {rnd + 1} για {len(todo)} αναζητήσεις")
            time.sleep(30 * rnd)
        for j in todo:
            try:
                res, _url = scrape_gflights(br, j["o"], j["r"]["iata"], dt.date.fromisoformat(j["out"]),
                                            dt.date.fromisoformat(j["in"]), pax, nonstop)
                res = [x for x in res if c.airline_allowed(x["airline"])
                       and not (nonstop and x["stops"])]
                recs[j["key"]] = {**j, "status": "ok" if res else "none", "res": res[:8]}
            except Exception as ex:  # noqa: BLE001
                recs[j["key"]] = {**j, "status": "error", "err": str(ex)[:120]}
            time.sleep(1.5)
    return list(recs.values())


def gf_shard(c, i, n, out_path):
    """Ένα από τα n παράλληλα τμήματα της σάρωσης πτήσεων (GitHub Actions, χωριστός server
    το καθένα ώστε να μη μας κόβει το Google). Γράφει τα αποτελέσματα σε αρχείο JSON."""
    jobs = gf_all_jobs(c)[i::n]
    # Κάθε τμήμα ξεκινά από άλλο σημείο της λίστας, ώστε τα τμήματα να μη ρωτούν το Google
    # για τον ίδιο προορισμό την ίδια στιγμή (τότε γυρίζει σφάλμα σε όλα μαζί).
    k = len(jobs) * i // max(n, 1)
    jobs = jobs[k:] + jobs[:k]
    t0 = time.time()
    br = Browser()
    try:
        recs = gf_scrape_jobs(c, br, jobs)
    finally:
        br.close()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False)
    n_by = {k: sum(1 for r in recs if r["status"] == k) for k in ("ok", "none", "error")}
    print(f"Τμήμα {i + 1}/{n}: {len(jobs)} αναζητήσεις σε {time.time() - t0:.0f}s · με τιμή {n_by['ok']}, "
          f"χωρίς κατάλληλη πτήση {n_by['none']}, σφάλματα {n_by['error']}")
    for r in recs:
        if r["status"] == "error":
            print(f"  σφάλμα {r['o']}-{r['r']['iata']} {r['out']}: {r['err']}")


def gf_apply(c, con, cache, jobs, recs, errors):
    """Περνά τα αποτελέσματα της σάρωσης πτήσεων στην cache και στο ιστορικό τιμών."""
    by_key = {r["key"]: r for r in recs}
    ok = none = err = 0
    for j in jobs:
        r = by_key.get(j["key"])
        if r is None or r["status"] == "error":
            err += 1  # κρατάμε ό,τι ξέραμε· θα ξαναδοκιμαστεί στην επόμενη σάρωση
            if r is not None and len(errors) < 12:
                errors.append(f"Google Flights {j['o']}-{j['r']['iata']} {j['out']}: {r['err']}")
        elif r["status"] == "ok":
            cache[j["key"]] = {"ts": c.NOW, "res": r["res"], "o": j["o"], "r": j["r"], "w": j["w"],
                               "wl": j["wl"], "season": j["season"], "out": j["out"], "in": j["in"]}
            con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)",
                        (j["key"], c.NOW, r["res"][0]["total"] / c.N_PAX, r["res"][0]["total"], "{}"))
            ok += 1
        else:
            # Καμία κατάλληλη πτήση. Το Google το λέει μερικές φορές και κατά λάθος, άρα μια τιμή
            # της προηγούμενης σάρωσης μένει μία φορά ακόμη και σβήνει αν επαναληφθεί.
            if c.NOW - cache.get(j["key"], {}).get("ts", 0) > 6 * 3600:
                cache.pop(j["key"], None)
            none += 1
    missing = sum(1 for j in jobs if j["key"] not in by_key)
    if missing:
        errors.append(f"Google Flights: δεν έτρεξαν {missing} από τις {len(jobs)} αναζητήσεις (απέτυχε τμήμα της σάρωσης)")
    c.record_run(con, "Google Flights", ok > 0 or not jobs, ok, "; ".join(errors[-3:]))
    c.log(f"Google Flights: {len(jobs)} αναζητήσεις · με τιμή {ok}, χωρίς κατάλληλη πτήση {none}, σφάλματα {err}")
    con.commit()


# --------------------------------------------------------------------- main
def run(c, con, ryanair_flights, gf_recs=None):
    """c: το module collector (για config, ιστορικό, αξιολόγηση).
    gf_recs: αποτελέσματα πτήσεων από τα παράλληλα τμήματα (gf_shard)· αν λείπουν, όλες οι
    αναζητήσεις πτήσεων γίνονται εδώ, η μία μετά την άλλη."""
    S = c.CFG.get("scrape", {})
    if not S.get("enabled"):
        return [], [], []
    try:
        import playwright  # noqa: F401
    except ImportError:
        return [], [], ["Scraping: λείπει το Playwright (pip install playwright && python -m playwright install chromium)"]

    fam = c.FAMILY
    sig = pax_sig(fam)
    errors = []
    t0 = time.time()

    # ---- Google Flights: σε κάθε σάρωση ΟΛΟΙ οι προορισμοί × ΟΛΕΣ οι εβδομάδες
    gf_jobs = gf_all_jobs(c)

    # ---- τυχόν μέρη "priority" στο config (μόνο δωμάτια): δικές τους περίοδοι, cache 12 ωρών
    wanted = S.get("stays_windows", ["summer"])
    prio_jobs = []
    for p in S.get("stays_places", []):
        if not p.get("priority"):
            continue
        want = p.get("windows", wanted)
        for w in c.travel_windows():
            if any(w["season"].startswith(x) for x in want):
                rd = rep_dates(c, w)
                if rd:
                    prio_jobs.append((p, w, rd, 12))
    per = S.get("stays_per_run", {"google_hotels": 30})

    gf_cache = load_cache(con, "gf_cache")
    gh_cache = load_cache(con, "gh_cache")
    con.execute("DELETE FROM kv WHERE k='ab_cache'")  # παλιά αποτελέσματα Airbnb: δεν χρησιμοποιούνται πια

    def stay_jobs(cache, prefix, n):
        """Ξενοδοχεία για ΟΛΕΣ τις εβδομάδες με έκπτωση πτήσεων σε κάθε σάρωση (το όριο n και
        η cache 3 ωρών είναι μόνο δικλείδες ασφαλείας) και μετά τα μέρη "priority"."""
        def stale(job):
            p, w, (ci, co), hours = job
            v = cache.get(f"{prefix}|{p['q']}|{ci.isoformat()}|{co.isoformat()}")
            # αποτελέσματα χωρίς κριτικές (δεν πέρασαν το φίλτρο κριτικών) ξαναψάχνονται αμέσως
            return not (v and v.get("pax") == sig and c.NOW - v["ts"] < hours * 3600
                        and any(x.get("reviews") is not None for x in v.get("res") or []))
        targets = [(p, w, rd, 3) for p, w, rd, _f, _why in stay_targets(c, S, gf_flights)]
        return [j for j in targets if stale(j)][:n] + [j for j in prio_jobs if stale(j)]

    def stay_entry(p, w, ci, co, res, shown, url):
        return {"ts": c.NOW, "res": shown, "name": p["name"], "q": p["q"], "pax": sig,
                "ap": p.get("airports", []), "ro": bool(p.get("rooms_only")),
                "region": p.get("region", "abroad"),
                "w": w["key"], "wl": w["label"], "ci": ci.isoformat(),
                "co": co.isoformat(), "url": url,
                "median": statistics.median([x["total"] for x in res])}

    gf_flights = []
    br = None
    try:
        br = Browser()
        if gf_recs is None:
            gf_recs = gf_scrape_jobs(c, br, gf_jobs)
        gf_apply(c, con, gf_cache, gf_jobs, gf_recs, errors)

        # Οι πτήσεις όπως είναι τώρα· από αυτές βγαίνουν οι εβδομάδες με έκπτωση για διαμονή
        gf_flights = build_flights(c, con, gf_cache)

        # Ξενοδοχεία (Google Hotels): η οικογένεια σε δύο δωμάτια του ίδιου ξενοδοχείου
        ok = 0
        gh_take = stay_jobs(gh_cache, GH_KEY, per.get("google_hotels", 30))
        for p, w, (ci, co), _h in gh_take:
            key = f"{GH_KEY}|{p['q']}|{ci.isoformat()}|{co.isoformat()}"
            try:
                res, url = scrape_ghotels(br, p["q"], ci, co, fam, S)
                e = stay_entry(p, w, ci, co, res, res[:6], booking_url(p["q"], ci, co, fam))
                e["gh_url"] = url
                gh_cache[key] = e
                con.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)",
                            (key, c.NOW, res[0]["total"], res[0]["total"], "{}"))
                ok += 1
            except Exception as ex:  # noqa: BLE001
                errors.append(f"Ξενοδοχεία {p['name']} {ci}: {str(ex)[:120]}")
            time.sleep(2)
        c.record_run(con, "Google Hotels", ok > 0 or not gh_take, ok, "")
        c.log(f"Ξενοδοχεία (Google Hotels): {ok}/{len(gh_take)} αναζητήσεις OK")
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
    save_cache(c, con, "gh_cache", gh_cache, 2)
    con.commit()
    c.log(f"Scraping ολοκληρώθηκε σε {time.time() - t0:.0f}s, {len(errors)} σφάλματα")

    gf_flights = build_flights(c, con, gf_cache)
    return gf_flights, build_stays(c, con, gh_cache, ryanair_flights + gf_flights), errors


# ------------------------------------------------------------ build output
def _hist(c, con, key, upto):
    h = c.history(con, key)
    prev = [p for ts, p in h if ts < upto]
    return h, prev


def build_flights(c, con, cache):
    items = []
    pax = gf_pax(c.FAMILY)
    extras = c.flight_extras()
    live = {w["key"] for w in c.flight_windows()}
    nonstop = bool(c.CFG.get("nonstop_only"))
    # μόνο τρέχουσες περίοδοι/προορισμοί, επιτρεπόμενες αεροπορικές και (αν ζητείται) απευθείας
    rows = {}
    for key, v in cache.items():
        res = [x for x in v["res"] if c.airline_allowed(x["airline"]) and not (nonstop and x["stops"])]
        if (res and v["w"] in live and ("|ns" in key) == nonstop
                and key.endswith("|2") == (len(gf_groups(pax)) > 1) and v["o"] in c.CFG["origins"]
                and c.dest_allowed(v["r"]["cc"], v["r"]["iata"])):
            rows[key] = {**v, "res": res}
    peers = {}
    for v in rows.values():
        peers.setdefault((v["o"], v["r"]["iata"], v["season"]), []).append((v["w"], v["res"][0]["total"]))
    for key, v in rows.items():
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
            options=v["res"][:6], fetched=v["ts"],
            book_url=(f"https://www.google.com/travel/flights/search?tfs="
                      f"{gf_tfs(v['o'], r['iata'], v['out'], v['in'], pax, nonstop)}&hl=el&curr=EUR"),
            score=score, deal=label, reasons=reasons,
            hist=[[ts, p] for ts, p in h][-60:], hist_n=len(prev),
            hist_min=min(prev) if prev else None,
            hist_median=statistics.median(prev) if prev else None,
            split_urls=[(f"https://www.google.com/travel/flights/search?tfs="
                         f"{gf_tfs(v['o'], r['iata'], v['out'], v['in'], g, nonstop)}&hl=el&curr=EUR")
                        for g in gf_groups(pax)] if len(gf_groups(pax)) > 1 else None))
    return items


def _flight_option(f):
    """Μια πτήση (Ryanair ή Google Flights) ως επιλογή μέσα σε πακέτο."""
    return dict(source=f["source"], airline=f.get("airline") or "Ryanair",
                total=f["family_total"], stops=f.get("stops", 0),
                dep=f["out_dt"][11:16] or None, url=f.get("book_url"), dest=f["dest"])


def build_stays(c, con, gh_cache, all_flights):
    """Ξενοδοχεία· όπου υπάρχουν πτήσεις ίδιων ημερομηνιών προς το ίδιο μέρος, φτιάχνει και
    «πακέτο» πτήση + διαμονή, με όλες τις (επιτρεπόμενες) αεροπορικές των ημερομηνιών αυτών.
    Διαμονές εξωτερικού εμφανίζονται μόνο για τις εβδομάδες του stay_targets."""
    fl_by_dest = {}
    for f in all_flights:
        fl_by_dest.setdefault((f["dest"], f["out_arr"][:10], f["in_dt"][:10]), []).append(f)
    sig = pax_sig(c.FAMILY)
    places = {p["q"] for p in c.CFG.get("scrape", {}).get("stays_places", [])}
    targets = {(p["q"], ci.isoformat(), co.isoformat()): (f, why)
               for p, _w, (ci, co), f, why in stay_targets(c, c.CFG.get("scrape", {}), all_flights)}
    out = []
    for src, cache in (("Google Hotels", gh_cache),):
        for key, v in cache.items():
            # μόνο τρέχοντες προορισμοί και μόνο αποτελέσματα της τωρινής μεθόδου (δύο δωμάτια, 3★+)
            if (not key.startswith(GH_KEY + "|") or not v["res"] or "ap" not in v
                    or v.get("pax") != sig or v["q"] not in places):
                continue
            # εξωτερικό: μόνο οι εβδομάδες όπου τα αεροπορικά έχουν έκπτωση
            target = targets.get((v["q"], v["ci"], v["co"]))
            if not v.get("ro") and not target:
                continue
            top = v["res"][0]
            h, prev = _hist(c, con, key, v["ts"])
            score, label, reasons = c.assess(prev, top["total"])
            ci, co = dt.date.fromisoformat(v["ci"]), dt.date.fromisoformat(v["co"])
            item = dict(kind="room", source=src, key=key, place=v["name"], q=v["q"],
                        window_label=v["wl"], check_in=v["ci"], check_out=v["co"],
                        nights=(co - ci).days, region=v.get("region", "abroad"),
                        rooms_only=bool(v.get("ro")),
                        results=v["res"][:5], total=top["total"], median=v.get("median"),
                        search_url=v.get("url"), gh_url=v.get("gh_url"), fetched=v["ts"],
                        rooms=len(gh_rooms(c.FAMILY)),
                        score=score, deal=label, reasons=reasons,
                        hist=[[ts, p] for ts, p in h][-60:], hist_n=len(prev))
            cands = [] if v.get("ro") else [f for a in v["ap"]
                                              for f in fl_by_dest.get((a, v["ci"], v["co"]), [])]
            if cands:
                f = min(cands, key=lambda x: x["family_total"])
                # όλες οι αεροπορικές για τις ίδιες ημερομηνίες (Ryanair + επιλογές Google Flights)
                extras = c.flight_extras()
                opts, seen = [], set()
                for x in sorted(cands, key=lambda x: x["family_total"]):
                    more = [dict(source=x["source"], airline=g["airline"], total=g["total"] + extras,
                                 stops=g.get("stops", 0), dep=g.get("dep"), url=x.get("book_url"),
                                 dest=x["dest"]) for g in (x.get("options") or [])[1:]]
                    for o in [_flight_option(x)] + more:
                        k = (o["airline"], o["dep"], round(o["total"]))
                        if k not in seen:
                            seen.add(k)
                            opts.append(o)
                opts.sort(key=lambda o: o["total"])
                item.update(origin=f["origin"], combo_total=round(top["total"] + f["family_total"]),
                            combo_flight=_flight_option(f), combo_flights=opts[:8],
                            flight_deal=target[0]["deal"], flight_why=target[1],
                            flight_reasons=target[0]["reasons"])
            out.append(item)
    return out
