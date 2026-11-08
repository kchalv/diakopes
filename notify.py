"""
Email για ΕΞΑΙΡΕΤΙΚΕΣ προσφορές (σήμα «🔥 Πραγματική προσφορά»).

Ρυθμίσεις στο config.json → "email". Ο κωδικός SMTP διαβάζεται από το τοπικό
αρχείο secrets.json ({"smtp_password": "..."}) ή από τη μεταβλητή περιβάλλοντος
DIAKOPES_SMTP_PASSWORD. Το secrets.json δεν ανεβαίνει ποτέ στο GitHub.

Κάθε προσφορά στέλνεται μία φορά. Ξαναστέλνεται μόνο αν η τιμή πέσει κι άλλο
(τουλάχιστον 5%). Όλες οι νέες προσφορές μιας σάρωσης πάνε σε ένα email.

Δοκιμή:  python notify.py --test-email
"""
import html
import json
import os
import re
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

BASE = Path(__file__).resolve().parent


def settings():
    """config.json → email, συμπληρωμένο με τα ιδιωτικά στοιχεία από
    secrets.json (τοπικά) ή από τη μεταβλητή DIAKOPES_EMAIL (GitHub Secret, JSON)."""
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8")).get("email", {})
    sp = BASE / "secrets.json"
    if sp.exists():
        s = json.loads(sp.read_text(encoding="utf-8"))
        cfg.update(s.get("email", {}))
        if s.get("smtp_password"):
            cfg["smtp_password"] = s["smtp_password"]
    if os.environ.get("DIAKOPES_EMAIL"):
        cfg.update(json.loads(os.environ["DIAKOPES_EMAIL"].lstrip("﻿").strip()))
    cfg.setdefault("smtp_password", "")
    return cfg


def recipients(cfg):
    """Το "to" μπορεί να είναι λίστα ή διευθύνσεις χωρισμένες με κόμμα/ερωτηματικό."""
    to = cfg.get("to") or ""
    if isinstance(to, str):
        to = re.split(r"[,;\s]+", to)
    return [t.strip() for t in to if t and t.strip()]


def send(cfg, subject, html_body, text_body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(recipients(cfg))
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["from"].split("@")[-1])
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    port = int(cfg.get("smtp_port", 587))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(cfg["smtp_host"], port, context=ctx, timeout=30) as s:
            s.login(cfg["smtp_user"], cfg["smtp_password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["smtp_host"], port, timeout=30) as s:
            s.ehlo()
            if cfg.get("starttls", True):
                s.starttls(context=ctx)
                s.ehlo()
            s.login(cfg["smtp_user"], cfg["smtp_password"])
            s.send_message(msg)


def eur(n):
    return f"{round(n):,}".replace(",", ".") + " €"


def _deal_rows(payload, budget):
    rows = []
    for f in payload["flights"]:
        if f["deal"] == "great" and f["family_total"] <= budget:
            rows.append(dict(key=f["key"], price=f["family_total"], kind="✈️ Πτήση",
                             title=f"{f['dest_name']} ({f['country']}) από {f['origin']}",
                             when=f"{f['out_dt'][:10]} → {f['in_dt'][:10]} · {f['window_label']}",
                             who=f.get("airline") or "Ryanair", reasons=f["reasons"],
                             url=f.get("book_url") or
                             f"https://www.ryanair.com/gr/el/trip/flights/select?adults=2&teens=1&children=2&infants=0"
                             f"&dateOut={f['out_dt'][:10]}&dateIn={f['in_dt'][:10]}&isReturn=true"
                             f"&originIata={f['origin']}&destinationIata={f['dest']}"))
    for h in payload["hotels"]:
        if h["deal"] == "great":
            price = h.get("combo_total") or h["total"]
            cf = h.get("combo_flight") or {}
            rows.append(dict(key=h["key"], price=price,
                             kind="🧩 Πτήση + διαμονή" if h.get("combo_total") else "🏠 Διαμονή",
                             title=f"{h['place']} · {h['results'][0]['name']}",
                             when=f"{h['check_in']} → {h['check_out']} · {h['nights']} νύχτες",
                             who=(f"{cf.get('airline')} από Αθήνα {eur(cf.get('total', 0))} + {h['source']}"
                                  if cf else h["source"]),
                             reasons=h["reasons"],
                             url=h.get("search_url") or h["results"][0].get("link") or ""))
    rows.sort(key=lambda r: r["price"])
    return rows


def notify_deals(con, payload, log=print):
    cfg = settings()
    if not cfg.get("enabled"):
        return "Email ανενεργό"
    if not all(cfg.get(k) for k in ("smtp_password", "from", "smtp_host", "smtp_user")) or not recipients(cfg):
        return "Email: λείπουν στοιχεία SMTP (secrets.json ή DIAKOPES_EMAIL)"
    row = con.execute("SELECT v FROM kv WHERE k='emailed'").fetchone()
    sent = json.loads(row[0]) if row else {}
    budget = cfg.get("max_family_budget_eur", payload.get("budget_default", 4000))
    new = [r for r in _deal_rows(payload, budget)
           if r["key"] not in sent or r["price"] <= sent[r["key"]] * 0.95]
    if not new:
        return "Email: καμία νέα εξαιρετική προσφορά"
    new = new[: cfg.get("max_items_per_email", 15)]
    page = cfg.get("page_url", "")
    fam = payload["family"]
    items_html = "".join(
        f"""<tr><td style="padding:12px 0;border-top:1px solid #e5e1d8">
        <div style="font-size:12px;color:#5d6675">{html.escape(r['kind'])} · {html.escape(r['who'])}</div>
        <div style="font-size:16px;font-weight:700;color:#1d2430">{html.escape(r['title'])}</div>
        <div style="font-size:13px;color:#5d6675">{html.escape(r['when'])}</div>
        <div style="font-size:13px;color:#0d7a43">{html.escape(' · '.join(r['reasons']))}</div>
        </td><td style="padding:12px 0 12px 12px;border-top:1px solid #e5e1d8;text-align:right;vertical-align:top;white-space:nowrap">
        <div style="font-size:20px;font-weight:700;color:#1d2430">{eur(r['price'])}</div>
        {f'<a href="{html.escape(r["url"])}" style="font-size:13px;color:#0b6e99">Δείτε →</a>' if r['url'] else ''}
        </td></tr>""" for r in new)
    body = f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:640px;margin:auto;color:#1d2430">
      <h2 style="margin:0 0 4px">🔥 {len(new)} εξαιρετικές προσφορές διακοπών</h2>
      <p style="margin:0 0 12px;color:#5d6675">Για {fam['adults']} ενήλικες + {len(fam['children_ages'])} παιδιά
      ({', '.join(map(str, fam['children_ages']))} ετών). Τιμές για όλη την οικογένεια.</p>
      <table style="width:100%;border-collapse:collapse">{items_html}</table>
      {f'<p style="margin-top:16px"><a href="{html.escape(page)}" style="color:#0b6e99;font-weight:600">Όλες οι προσφορές στη σελίδα →</a></p>' if page else ''}
      <p style="font-size:12px;color:#8a929e">Οι τιμές αλλάζουν συνεχώς και επιβεβαιώνονται μόνο στην κράτηση.</p></div>"""
    text = "\n\n".join(f"{r['kind']} | {r['title']}\n{r['when']}\n{eur(r['price'])} | {r['url']}" for r in new)
    if page:
        text += f"\n\nΌλες οι προσφορές: {page}"
    send(cfg, f"🔥 {len(new)} εξαιρετικές προσφορές διακοπών (από {eur(new[0]['price'])})", body, text)
    for r in new:
        sent[r["key"]] = r["price"]
    con.execute("INSERT OR REPLACE INTO kv VALUES('emailed', ?)", (json.dumps(sent),))
    con.commit()
    return f"Email στάλθηκε με {len(new)} προσφορές"


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if "--test-email" in sys.argv:
        cfg = settings()
        if not cfg.get("smtp_password"):
            print("Λείπει ο κωδικός SMTP (secrets.json ή DIAKOPES_SMTP_PASSWORD)")
            sys.exit(1)
        send(cfg, "Δοκιμή: προσφορές διακοπών",
             "<p>Το email ειδοποιήσεων για προσφορές διακοπών λειτουργεί.</p>",
             "Το email ειδοποιήσεων για προσφορές διακοπών λειτουργεί.")
        print(f"Στάλθηκε δοκιμαστικό email σε: {', '.join(recipients(cfg))}")
