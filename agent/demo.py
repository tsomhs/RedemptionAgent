"""A demo database with invented data, for a first look at the dashboard.

Real figures never go into git, so a fresh copy of the project has nothing to
show. When the settings point to data/demo.db and that database is empty, it
is filled with made-up partners, six months of numbers and one month in
progress with a request in every state. Every address ends in example.test,
which cannot receive mail, and nothing here is sent anywhere: the emails of
the demo month are written straight into the database.
"""

import hashlib
import uuid
from datetime import timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage

from . import automation, mail, prepare_requests, replies

NAME = "demo.db"
PROGRAM = automation.DEFAULT_PROGRAM
AGENT = "agent@example.test"

# partner, email (None: the team has not filled it in), offers as (segment, text, category)
PARTNERS = [
    ("Demo Coffee", "orders@demo-coffee.example.test", [
        ("generic", "2€ έκπτωση", "Φαγητό & Ρόφημα"), ("family", "1+1 ρόφημα", "Φαγητό & Ρόφημα")]),
    ("Alpha Travel", "sales@alpha-travel.example.test", [
        ("generic", "10% έκπτωση", "Απόδραση"), ("student", "15% έκπτωση", "Απόδραση")]),
    ("Beta Sport", "info@beta-sport.example.test", [
        ("generic", "20% έκπτωση", "Μόδα"), ("generic", "15€ έκπτωση", "Μόδα"), ("student", "25% έκπτωση", "Μόδα")]),
    ("Gamma Home", "hello@gamma-home.example.test", [
        ("generic", "5€ έκπτωση", "Σπίτι"), ("family", "10% έκπτωση", "Σπίτι")]),
    ("Delta Tech", "b2b@delta-tech.example.test", [
        ("generic", "50€ έκπτωση", "Τεχνολογία"), ("business", "30€ έκπτωση", "Τεχνολογία")]),
    ("Epsilon Books", "old-address@epsilon-books.example.test", [
        ("generic", "3€ έκπτωση", "Ψυχαγωγία")]),
    ("Zeta Fitness", "team@zeta-fitness.example.test", [
        ("generic", "1 μήνας δώρο", "Υγεία - Ευεξία"), ("student", "20% έκπτωση", "Υγεία - Ευεξία")]),
    ("Eta Garden", None, [
        ("generic", "10% έκπτωση", "Σπίτι")]),
]  # fmt: skip


def _n(seed: str, low: int, high: int) -> int:
    """A number that looks random but is the same on every run."""
    return low + int(hashlib.sha1(seed.encode()).hexdigest(), 16) % (high - low + 1)


def _months_before(period: str, count: int) -> list:
    year, month = (int(x) for x in period.split("-"))
    out = []
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        out.append(f"{year}-{month:02d}")
    return out[::-1]


def is_empty(con) -> bool:
    return con.execute("SELECT 1 FROM offers LIMIT 1").fetchone() is None


def build(con, cfg, now=None) -> str:
    """Fill an empty database. Returns the month that is shown as in progress."""
    now = now or mail.utcnow()
    period = automation.previous_month(now.astimezone(automation.TZ).date())
    history = _months_before(period, 6)

    offers = {}  # partner -> [(uuid, admin_name)]
    with con:
        for partner, email_addr, items in PARTNERS:
            mid = con.execute("INSERT INTO merchants (name) VALUES (?)", (partner,)).lastrowid
            if email_addr:
                con.execute("INSERT INTO merchant_contacts VALUES (?, ?)", (mid, email_addr))
            for segment, text, category in items:
                name = f"{segment}_{partner}_{text}_31-12-2027"
                oid = str(uuid.uuid5(uuid.NAMESPACE_URL, "demo:" + name))
                con.execute(
                    "INSERT INTO offers (uuid, program, segment, admin_name, category, merchant_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (oid, PROGRAM, segment, name, category, mid),
                )
                offers.setdefault(partner, []).append((oid, name))
                size = _n(name, 20, 600)
                for p in history + [period]:
                    assigned = max(1, size + _n(name + p, -size // 3, size // 3))
                    con.execute(
                        "INSERT INTO assignments (offer_uuid, period, assignments, source_file) VALUES (?, ?, ?, 'demo')",
                        (oid, p, assigned),
                    )
                    con.execute("INSERT INTO redemptions (offer_uuid, period) VALUES (?, ?)", (oid, p))
                    # Past months are complete, except one partner who never answered one month.
                    if p != period and not (partner == "Gamma Home" and p == history[-2]):
                        redeemed = assigned * _n(name + "rate", 8, 55) // 100
                        con.execute(
                            "UPDATE redemptions SET redeemed=?, status='reported', source='demo' WHERE offer_uuid=? AND period=?",
                            (redeemed, oid, p),
                        )

    prepare_requests.plan(con, period, PROGRAM)
    sent_at = now - timedelta(days=9)
    out = {"new": [], "auto": [], "unmatched": [], "bounced": [], "ignored": 0}

    def request(partner):
        return con.execute(
            """SELECT r.id, r.subject, r.body, c.email FROM requests r JOIN merchants m ON m.id = r.merchant_id
                 JOIN merchant_contacts c ON c.merchant_id = m.id WHERE m.name = ? AND r.period = ?""",
            (partner, period),
        ).fetchone()

    def send(partner):
        r = request(partner)
        msg = mail._build(AGENT, [r["email"]], r["subject"], r["body"])
        with con:
            con.execute("UPDATE requests SET status='sent', sent_at=? WHERE id=?", (mail._iso(sent_at), r["id"]))
            mail._record(con, r["id"], "out", "initial", msg, r["body"], sent_at)
        return r, msg["Message-ID"]

    def reminder(r, first_id, days_ago):
        body = mail.FOLLOWUP_TEXT.format(label=prepare_requests.period_label(period))
        msg = mail._build(AGENT, [r["email"]], "Re: " + r["subject"], body, first_id)
        with con:
            con.execute("UPDATE requests SET followups_sent = followups_sent + 1 WHERE id=?", (r["id"],))
            mail._record(con, r["id"], "out", "followup", msg, body, now - timedelta(days=days_ago))

    def incoming(sender, subject, text, in_reply_to, days_ago, headers=()):
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = sender, AGENT, subject
        m["Message-ID"] = f"<demo-{hashlib.sha1((sender + text).encode()).hexdigest()[:16]}@example.test>"
        if in_reply_to:
            m["In-Reply-To"] = m["References"] = in_reply_to
        for k, v in headers:
            m[k] = v
        m.set_content(text)
        # Through the same code that files real mail, so the demo shows what the agent really does.
        mail.store_incoming(con, cfg, message_from_bytes(m.as_bytes(), policy=policy.default), now - timedelta(days=days_ago), out)

    def lines(partner, values):
        return "\n".join(
            f"[{replies.offer_code(oid)}] {name}: {v}" for (oid, name), v in zip(offers[partner], values) if v is not None
        )

    # Answered in full, in the agreed format.
    r, first = send("Demo Coffee")
    incoming(r["email"], "Re: " + r["subject"], "Καλησπέρα σας,\n\n" + lines("Demo Coffee", (148, 37)) + "\n\nΜε εκτίμηση", first, 7)

    # Answered for one offer only: the request stays open for the other.
    r, first = send("Alpha Travel")
    incoming(r["email"], "Re: " + r["subject"], "Καλημέρα, προς το παρόν έχω μόνο αυτό:\n" + lines("Alpha Travel", (21, None)), first, 6)

    # One total for three offers: the agent will not split it.
    r, first = send("Beta Sport")
    incoming(r["email"], "Re: " + r["subject"], "Καλησπέρα, συνολικά τον μήνα είχαμε 85 εξαργυρώσεις στα καταστήματά μας.", first, 5)

    # Two different numbers for the same offer.
    r, first = send("Gamma Home")
    code, name = replies.offer_code(offers["Gamma Home"][0][0]), offers["Gamma Home"][0][1]
    incoming(r["email"], "Re: " + r["subject"], f"[{code}] {name}: 12\n[{code}] {name}: 21\n" + lines("Gamma Home", (None, 9)), first, 4)

    # No answer yet: an out-of-office note, then a reminder.
    r, first = send("Delta Tech")
    incoming(r["email"], "Αυτόματη απάντηση: " + r["subject"], "Είμαι εκτός γραφείου και θα απαντήσω στην επιστροφή μου.", first, 9,
             headers=[("Auto-Submitted", "auto-replied")])  # fmt: skip
    reminder(r, first, 2)

    # The address no longer exists: a delivery failure.
    r, first = send("Epsilon Books")
    bounce = (
        f"From: Mail Delivery Subsystem <mailer-daemon@example.test>\nTo: {AGENT}\n"
        f"Subject: Delivery Status Notification (Failure)\nMessage-ID: <demo-bounce@example.test>\n"
        f"X-Failed-Recipients: {r['email']}\nIn-Reply-To: {first}\nReferences: {first}\nMIME-Version: 1.0\n"
        'Content-Type: multipart/report; boundary="b"; report-type=delivery-status\n\n'
        f'--b\nContent-Type: text/plain; charset="UTF-8"\n\nAddress not found. Your message was not delivered to {r["email"]}.\n'
        f"--b\nContent-Type: message/delivery-status\n\nFinal-Recipient: rfc822; {r['email']}\nAction: failed\nStatus: 5.1.1\n--b--\n"
    )
    mail.store_incoming(con, cfg, message_from_bytes(bounce.encode(), policy=policy.default), now - timedelta(days=9), out)

    # An email from a known partner that answers no request.
    incoming("orders@demo-coffee.example.test", "Ερώτηση για το πρόγραμμα", "Καλημέρα, πότε ξεκινά η επόμενη καμπάνια;", None, 3)

    # Zeta Fitness stays prepared but not sent; Eta Garden has no email at all.
    replies.process(con, mail._iso(now - timedelta(days=3)), None)
    automation.log(con, now, "cycle", "Δοκιμαστική βάση με επινοημένα δεδομένα. Κανένα email δεν στάλθηκε.")
    return period
