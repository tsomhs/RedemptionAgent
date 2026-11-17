"""The review queue: replies the agent would not record on its own, and what a
person can do about them (enter the values, ask the partner, re-attach a stray
email). Every value entered here goes through the same history as automatic ones.
"""

import json

from . import mail, replies
from .contacts import EMAIL_RE

CLARIFY_TEXT = (
    "Καλημέρα σας,\n\n"
    "ευχαριστούμε για την απάντησή σας. Για να καταχωρήσουμε τα στοιχεία του μήνα {label} "
    "χρειαζόμαστε τον ακριβή αριθμό εξαργυρώσεων ξεχωριστά για κάθε προσφορά:\n\n{lines}\n\n"
    "Ευχαριστούμε εκ των προτέρων."
)


class ReviewError(Exception):
    pass


def _offers(con, request_id, period):
    return [
        dict(r)
        for r in con.execute(
            """SELECT o.uuid, o.admin_name, s.assignments AS assigned, d.redeemed, d.status
                 FROM request_offers ro
                 JOIN offers o ON o.uuid = ro.offer_uuid
                 LEFT JOIN assignments s ON s.offer_uuid = o.uuid AND s.period = ?
                 LEFT JOIN redemptions d ON d.offer_uuid = o.uuid AND d.period = ?
                WHERE ro.request_id = ? ORDER BY o.admin_name""",
            (period, period, request_id),
        )
    ]


def _messages(con, request_id):
    out = []
    for m in con.execute("SELECT * FROM messages WHERE request_id=? ORDER BY id", (request_id,)):
        out.append({
            "id": m["id"], "direction": m["direction"], "kind": m["kind"], "at": m["at"],
            "from": m["from_addr"], "to": m["to_addrs"], "subject": m["subject"], "body": m["body"] or "",
            "attachments": [a.replace("\\", "/").rsplit("/", 1)[-1] for a in json.loads(m["attachments"] or "[]")],
            "note": m["parse_note"], "needs_review": bool(m["needs_review"]),
        })  # fmt: skip
    return out


def _request(con, request_id):
    r = con.execute(
        """SELECT r.*, m.name AS merchant FROM requests r
             JOIN merchants m ON m.id = r.merchant_id WHERE r.id = ?""",
        (request_id,),
    ).fetchone()
    if r is None:
        raise ReviewError("Το αίτημα δεν βρέθηκε")
    return r


def thread(con, request_id: int) -> dict:
    """Everything about one request: its offers and the whole correspondence."""
    r = _request(con, request_id)
    return {
        "id": r["id"], "merchant": r["merchant"], "program": r["program"], "period": r["period"],
        "status": r["status"], "sent_at": r["sent_at"], "followups_sent": r["followups_sent"],
        "offers": _offers(con, r["id"], r["period"]),
        "messages": _messages(con, r["id"]),
    }  # fmt: skip


def queue(con) -> dict:
    """Open items. A request is here when its reply could not be read into
    numbers ('replied') or one of its offers is marked needs_review."""
    ids = [
        r[0]
        for r in con.execute(
            """SELECT r.id FROM requests r JOIN merchants m ON m.id = r.merchant_id
                WHERE r.status = 'replied'
                   OR EXISTS (SELECT 1 FROM messages x WHERE x.request_id = r.id AND x.needs_review = 1)
                   OR EXISTS (SELECT 1 FROM request_offers ro
                                JOIN redemptions d ON d.offer_uuid = ro.offer_uuid AND d.period = r.period
                               WHERE ro.request_id = r.id AND d.status = 'needs_review')
                ORDER BY r.period DESC, lower(m.name)"""
        )
    ]
    items = []
    for rid in ids:
        t = thread(con, rid)
        replies_in = [m for m in t["messages"] if m["direction"] == "in" and m["kind"] == "reply"]
        last = next((m for m in reversed(replies_in) if m["needs_review"]), replies_in[-1] if replies_in else None)
        t["reason"] = (last or {}).get("note") or "Αντιφατικά στοιχεία στην απάντηση"
        items.append(t)
    unmatched = [
        {"id": m["id"], "at": m["at"], "from": m["from_addr"], "subject": m["subject"], "body": m["body"] or ""}
        for m in con.execute(
            "SELECT * FROM messages WHERE request_id IS NULL AND direction='in' AND kind != 'ignored' ORDER BY id"
        )
    ]
    open_requests = [
        {"id": r["id"], "label": f"{r['name']} – {r['period']}"}
        for r in con.execute(
            """SELECT r.id, r.period, m.name FROM requests r JOIN merchants m ON m.id = r.merchant_id
                WHERE r.status NOT IN ('draft', 'completed') ORDER BY lower(m.name), r.period"""
        )
    ]
    return {
        "requests": items,
        "unmatched": unmatched,
        "open_requests": open_requests,
        "contact_problems": contact_problems(con),
        "no_email": partners_without_email(con),
    }


def contact_problems(con) -> list:
    """Requests with a delivery failure nobody has dealt with yet."""
    out = []
    for r in con.execute(
        """SELECT DISTINCT r.id FROM requests r
             JOIN messages x ON x.request_id = r.id AND x.kind = 'bounce' AND x.processed_at IS NULL
             JOIN merchants m ON m.id = r.merchant_id
            ORDER BY r.period DESC, lower(m.name)"""
    ).fetchall():
        t = thread(con, r["id"])
        bounces = [m for m in t["messages"] if m["kind"] == "bounce"]
        t["failed"] = sorted({a for m in bounces for a in (m["to"] or "").split(", ") if a})
        t["bounce_text"] = bounces[-1]["body"]
        t["contacts"] = mail._contacts(con, _request(con, r["id"])["merchant_id"])
        out.append(t)
    return out


def partners_without_email(con) -> list:
    """Partners that have offers in a month whose requests are being prepared
    or sent, but no email address: nothing can go out to them."""
    return [
        dict(r)
        for r in con.execute(
            """SELECT m.name AS merchant, a.period, count(*) AS offers
                 FROM assignments a
                 JOIN offers o ON o.uuid = a.offer_uuid
                 JOIN merchants m ON m.id = o.merchant_id
                WHERE a.assignments > 0
                  AND a.period IN (SELECT DISTINCT period FROM requests)
                  AND NOT EXISTS (SELECT 1 FROM merchant_contacts c WHERE c.merchant_id = m.id)
                GROUP BY m.id, a.period ORDER BY a.period DESC, lower(m.name)"""
        )
    ]


def resend(con, cfg, request_id: int, emails, now) -> dict:
    """Fix the partner's addresses and send the request again. The failed
    attempt stays in the thread; reminders start over from this email."""
    r = _request(con, request_id)
    if r["status"] in ("completed", "replied", "partial"):
        raise ReviewError("Ο συνεργάτης έχει ήδη απαντήσει σε αυτό το αίτημα")
    clean = sorted({str(e).strip().lower() for e in (emails or []) if str(e).strip()})
    if not clean:
        raise ReviewError("Δώσε τουλάχιστον μία διεύθυνση email")
    wrong = [e for e in clean if not EMAIL_RE.match(e)]
    if wrong:
        raise ReviewError(f"Μη έγκυρη διεύθυνση: {', '.join(wrong)}")
    bad = mail.blocked_recipients(cfg, clean)
    if bad:
        raise ReviewError(f"Δοκιμαστική λειτουργία: δεν επιτρέπεται αποστολή σε {', '.join(bad)}")
    msg = mail._build(cfg.mail_from, clean, r["subject"], r["body"])
    smtp = mail._smtp(cfg)
    try:
        smtp.send_message(msg)
    finally:
        smtp.quit()
    with con:
        con.execute("DELETE FROM merchant_contacts WHERE merchant_id=?", (r["merchant_id"],))
        con.executemany(
            "INSERT INTO merchant_contacts (merchant_id, email) VALUES (?, ?)",
            [(r["merchant_id"], e) for e in clean],
        )
        mail._record(con, request_id, "out", "resend", msg, r["body"], now)
        con.execute(
            "UPDATE requests SET status='sent', sent_at=?, followups_sent=0 WHERE id=?",
            (mail._iso(now), request_id),
        )
        con.execute(
            "UPDATE messages SET processed_at=? WHERE request_id=? AND kind='bounce' AND processed_at IS NULL",
            (mail._iso(now), request_id),
        )
    return {"to": clean, "status": "sent"}


def dismiss_bounce(con, request_id: int, now_iso: str):
    """The delivery failure needs no action (for example one of several
    addresses failed and the others are enough)."""
    r = _request(con, request_id)
    if r["status"] == "undelivered":
        raise ReviewError("Το αίτημα δεν παραδόθηκε σε κανέναν· διόρθωσε τη διεύθυνση και ξαναστείλε το")
    with con:
        con.execute(
            "UPDATE messages SET processed_at=? WHERE request_id=? AND kind='bounce' AND processed_at IS NULL",
            (now_iso, request_id),
        )


def _seen(con, request_id):
    """A person has dealt with the replies of this request."""
    con.execute("UPDATE messages SET needs_review=0 WHERE request_id=? AND needs_review=1", (request_id,))


def keep_waiting(con, request_id: int, now_iso: str) -> dict:
    """The reply needs nothing from us: it was not an answer ("we will send
    it", an out-of-office), or the numbers on record are right as they are.
    Reminders resume for whatever is still missing."""
    r = _request(con, request_id)
    if r["status"] in ("draft", "undelivered"):
        raise ReviewError("Το αίτημα δεν έχει απάντηση προς έλεγχο")
    with con:
        _seen(con, request_id)
        # Offers marked as contradictory go back to what they were.
        con.execute(
            """UPDATE redemptions SET status = CASE WHEN redeemed IS NULL THEN 'pending' ELSE 'reported' END,
                                      updated_at = ?
                WHERE status = 'needs_review' AND period = ?
                  AND offer_uuid IN (SELECT offer_uuid FROM request_offers WHERE request_id = ?)""",
            (now_iso, r["period"], request_id),
        )
        status = _refresh_status(con, request_id, r["period"], "sent")
    return {"status": status}


def _refresh_status(con, request_id, period, current):
    total, filled = con.execute(
        """SELECT count(*), count(d.redeemed) FROM request_offers ro
             LEFT JOIN redemptions d ON d.offer_uuid = ro.offer_uuid AND d.period = ?
            WHERE ro.request_id = ?""",
        (period, request_id),
    ).fetchone()
    status = "completed" if filled == total else "partial" if filled else current
    con.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))
    return status


def set_values(con, request_id: int, values: dict, now_iso: str, who: str = "χειροκίνητα") -> dict:
    """Record numbers a person entered. values: offer uuid -> non-negative int.
    Offers left out are not touched, so a missing answer never becomes a zero."""
    r = _request(con, request_id)
    allowed = {o["uuid"]: o for o in _offers(con, request_id, r["period"])}
    clean = {}
    for uuid, value in values.items():
        if uuid not in allowed:
            raise ReviewError("Η προσφορά δεν ανήκει σε αυτό το αίτημα")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReviewError("Οι εξαργυρώσεις πρέπει να είναι ακέραιος αριθμός, 0 ή μεγαλύτερος")
        clean[uuid] = value
    if not clean:
        raise ReviewError("Δεν δόθηκε καμία τιμή")
    source = f"manual: {who}"
    written = []
    with con:
        for uuid, value in clean.items():
            old = allowed[uuid]["redeemed"]
            if old == value and allowed[uuid]["status"] == "reported":
                continue
            con.execute(
                """INSERT INTO redemptions (offer_uuid, period, redeemed, status, source, updated_at)
                   VALUES (?, ?, ?, 'reported', ?, ?)
                   ON CONFLICT(offer_uuid, period) DO UPDATE SET
                     redeemed=excluded.redeemed, status='reported',
                     source=excluded.source, updated_at=excluded.updated_at""",
                (uuid, r["period"], value, source, now_iso),
            )
            con.execute(
                """INSERT INTO redemption_history (offer_uuid, period, old_value, new_value, source, changed_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (uuid, r["period"], old, value, source, now_iso),
            )
            written.append(allowed[uuid]["admin_name"])
        _seen(con, request_id)
        status = _refresh_status(con, request_id, r["period"], r["status"])
    return {"written": written, "status": status}


def clarification_text(con, request_id: int, label_for) -> str:
    r = _request(con, request_id)
    missing = [o for o in _offers(con, request_id, r["period"]) if o["redeemed"] is None or o["status"] == "needs_review"]
    lines = "\n".join(f"[{replies.offer_code(o['uuid'])}] {o['admin_name']}" for o in missing)
    return CLARIFY_TEXT.format(label=label_for(r["period"]), lines=lines)


def send_clarification(con, cfg, request_id: int, text: str, now) -> dict:
    """Ask the partner again, in the same thread. The request goes back to
    'sent': it is waiting for an answer, and reminders resume a full interval
    after this email."""
    r = _request(con, request_id)
    if r["status"] in ("draft", "completed", "undelivered"):
        raise ReviewError("Το αίτημα δεν περιμένει διευκρίνιση")
    if not (text or "").strip():
        raise ReviewError("Το κείμενο είναι κενό")
    to = mail._contacts(con, r["merchant_id"])
    if not to:
        raise ReviewError("Ο συνεργάτης δεν έχει email")
    bad = mail.blocked_recipients(cfg, to)
    if bad:
        raise ReviewError(f"Δοκιμαστική λειτουργία: δεν επιτρέπεται αποστολή σε {', '.join(bad)}")
    first = con.execute(
        "SELECT message_id FROM messages WHERE request_id=? AND kind IN ('initial', 'resend') "
        "ORDER BY id DESC LIMIT 1",
        (request_id,),
    ).fetchone()
    msg = mail._build(cfg.mail_from, to, "Re: " + r["subject"], text.strip(), first["message_id"] if first else None)
    smtp = mail._smtp(cfg)
    try:
        smtp.send_message(msg)
    finally:
        smtp.quit()
    with con:
        mail._record(con, request_id, "out", "clarification", msg, text.strip(), now)
        _seen(con, request_id)
        # Waiting again: for everything ('sent') or for what is still missing ('partial').
        status = _refresh_status(con, request_id, r["period"], "sent")
    return {"to": to, "status": status}


def assign_message(con, message_id: int, request_id: int, now_iso: str, cfg=None) -> list:
    """Tie a stray incoming email to a request and read it like any other reply."""
    _request(con, request_id)
    with con:
        n = con.execute(
            "UPDATE messages SET request_id=?, kind='reply', processed_at=NULL "
            "WHERE id=? AND request_id IS NULL AND direction='in'",
            (request_id, message_id),
        ).rowcount
        if not n:
            raise ReviewError("Το μήνυμα δεν βρέθηκε ή έχει ήδη αντιστοιχιστεί")
        con.execute("UPDATE requests SET status='replied' WHERE id=? AND status='sent'", (request_id,))
    return replies.process(con, now_iso, cfg)


def ignore_message(con, message_id: int):
    """Not about any request. Kept in the database (so the next fetch does not
    bring it back) but out of the queue."""
    with con:
        n = con.execute(
            "UPDATE messages SET kind='ignored' WHERE id=? AND request_id IS NULL AND direction='in'",
            (message_id,),
        ).rowcount
    if not n:
        raise ReviewError("Το μήνυμα δεν βρέθηκε ή έχει ήδη αντιστοιχιστεί")
