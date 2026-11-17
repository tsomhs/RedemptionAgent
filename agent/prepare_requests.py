"""Decide who gets a request for a period and prepare the emails (no sending)."""

import re
from pathlib import Path


MONTHS_ACC = [
    "Ιανουάριο", "Φεβρουάριο", "Μάρτιο", "Απρίλιο", "Μάιο", "Ιούνιο",
    "Ιούλιο", "Αύγουστο", "Σεπτέμβριο", "Οκτώβριο", "Νοέμβριο", "Δεκέμβριο",
]  # fmt: skip


def period_label(period: str) -> str:
    year, month = period.split("-")
    return f"{MONTHS_ACC[int(month) - 1]} {year}"


def offer_lines(offers) -> str:
    """The offers of an email, one per line, by the name the partner knows."""
    return "\n".join(f"- {o['admin_name']}" for o in offers)


def compose(program: str, period: str, offers) -> tuple[str, str]:
    """The team's text, followed by the offers the request is about."""
    label = period_label(period)
    subject = f"{program} – Εξαργυρώσεις κουπονιών, {label}"
    body = (
        "Καλημέρα σας,\n"
        f"Στα πλαίσια της συνεργασίας που διατηρούμε στο {program}, θα θέλαμε να μας "
        "στείλετε τη λίστα με τα κουπόνια που έχουν εξαργυρωθεί στην βάση σας τον μήνα "
        f"{label} για την καταγραφή τους και στη δική μας βάση.\n\n"
        f"Το αίτημα αφορά τις παρακάτω προσφορές:\n{offer_lines(offers)}\n\n"
        "Ευχαριστούμε εκ των προτέρων."
    )
    return subject, body


def plan(con, period: str, program: str, preview_dir: Path | None = None) -> dict:
    """Create or refresh draft requests for the period: one per merchant, only
    for offers with assignments whose redemptions are not known yet (a month
    already filled in from the old report needs no request). Requests that already left 'draft' are never
    touched, so running this twice cannot produce a duplicate request."""
    offers = con.execute(
        """SELECT o.uuid, o.admin_name, o.merchant_id, m.name AS merchant
             FROM assignments a
             JOIN offers o ON o.uuid = a.offer_uuid
             LEFT JOIN merchants m ON m.id = o.merchant_id
             LEFT JOIN redemptions d ON d.offer_uuid = a.offer_uuid AND d.period = a.period
            WHERE a.period = ? AND a.assignments > 0 AND o.program = ?
              AND (d.redeemed IS NULL OR EXISTS (
                     SELECT 1 FROM request_offers ro JOIN requests r ON r.id = ro.request_id
                      WHERE ro.offer_uuid = a.offer_uuid AND r.period = a.period AND r.status != 'draft'))
            ORDER BY o.admin_name""",
        (period, program),
    ).fetchall()

    out = {"drafts": [], "already_sent": [], "no_merchant": [], "no_contact": {}, "not_covered": []}
    by_merchant = {}
    for o in offers:
        if o["merchant_id"] is None:
            out["no_merchant"].append(o["admin_name"])
        else:
            by_merchant.setdefault(o["merchant_id"], []).append(o)

    with con:
        for mid, items in by_merchant.items():
            name = items[0]["merchant"]
            emails = [
                r["email"]
                for r in con.execute(
                    "SELECT email FROM merchant_contacts WHERE merchant_id = ? ORDER BY email",
                    (mid,),
                )
            ]
            if not emails:
                # Never invent a recipient: surface it as something to fix.
                out["no_contact"][name] = len(items)
                continue
            existing = con.execute(
                "SELECT id, status FROM requests WHERE program=? AND merchant_id=? AND period=?",
                (program, mid, period),
            ).fetchone()
            if existing and existing["status"] != "draft":
                out["already_sent"].append(name)
                # Offers tied to this partner after the request went out are in
                # no email. Nothing is sent for them automatically; they are
                # listed so that someone asks.
                asked = {
                    r["offer_uuid"]
                    for r in con.execute("SELECT offer_uuid FROM request_offers WHERE request_id=?", (existing["id"],))
                }
                out["not_covered"] += [(name, o["admin_name"]) for o in items if o["uuid"] not in asked]
                continue
            subject, body = compose(program, period, items)
            if existing:
                rid = existing["id"]
                con.execute(
                    "UPDATE requests SET subject=?, body=? WHERE id=?", (subject, body, rid)
                )
                con.execute("DELETE FROM request_offers WHERE request_id=?", (rid,))
            else:
                rid = con.execute(
                    """INSERT INTO requests (program, merchant_id, period, subject, body)
                       VALUES (?, ?, ?, ?, ?)""",
                    (program, mid, period, subject, body),
                ).lastrowid
            con.executemany(
                "INSERT INTO request_offers (request_id, offer_uuid) VALUES (?, ?)",
                [(rid, o["uuid"]) for o in items],
            )
            out["drafts"].append(
                {"merchant": name, "to": emails, "offers": len(items), "subject": subject, "body": body}
            )

    if preview_dir is not None and out["drafts"]:
        target = Path(preview_dir) / period
        target.mkdir(parents=True, exist_ok=True)
        for d in out["drafts"]:
            safe = re.sub(r"[^\w\-]+", "_", d["merchant"]).strip("_") or "merchant"
            (target / f"{safe}.txt").write_text(
                f"To: {', '.join(d['to'])}\nSubject: {d['subject']}\n\n{d['body']}\n",
                encoding="utf-8",
            )
    return out
