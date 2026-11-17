"""Read structured replies and write the redemptions. No AI: plain matching.

Every offer in a request carries a code (the first 8 characters of its UUID),
shown as '[445eeacb] offer name'. A reply reports a number by repeating the
code on a line that ends with ': <number>' or '= <number>':

    [445eeacb] Student_24home.gr_10€_31-10-2026: 12
    [9a72b5db] = 0

Anything that does not follow the format is left for a human; the program never
guesses a number and never splits a total across offers.
"""

import json
import re

CODE_LEN = 8
_LINE = re.compile(r"\[([0-9a-fA-F]{8})\][^\n]*?[:=]\s*(\d+)\s*\.?\s*$")


def offer_code(uuid: str) -> str:
    return uuid[:CODE_LEN].lower()


def parse(body: str):
    """Return (values, conflicts): code -> number, and the codes that were given
    two different numbers. Lines typed by the sender win over quoted ones
    ('> ...'), so a correction on top of an older quoted reply is read right,
    while numbers filled in directly inside the quoted request still count."""
    typed, quoted = {}, {}
    for raw in (body or "").splitlines():
        line = raw.strip()
        bucket = typed
        if line.startswith(">"):
            bucket, line = quoted, line.lstrip("> ").strip()
        m = _LINE.search(line)
        if m:
            bucket.setdefault(m.group(1).lower(), set()).add(int(m.group(2)))
    values, conflicts = {}, set()
    for code in set(typed) | set(quoted):
        found = typed.get(code) or quoted[code]
        if len(found) == 1:
            values[code] = next(iter(found))
        else:
            conflicts.add(code)
    return values, conflicts


def _read_with_ai(cfg, msg, offers, recorded):
    """Free-text reply -> (values, ai_note, reply_type, is_correction).
    recorded: code -> number already in the database. Raises ai_reader.ReaderError."""
    from . import prepare_requests, ai_reader  # imported here so the AI library is optional

    reading = ai_reader.read_reply(
        cfg,
        program=msg["program"],
        period_label=prepare_requests.period_label(msg["period"]),
        merchant=msg["name"],
        offers=[(code, items[0]["admin_name"]) for code, items in offers.items()],
        body=msg["body"] or "",
        attachments=json.loads(msg["attachments"] or "[]"),
        recorded=recorded,
    )
    values = {}
    if not reading.needs_review and reading.reply_type in ("complete", "partial"):
        values = {
            v.code.strip("[] ").lower(): v.redeemed for v in reading.values if v.redeemed >= 0
        }
    note = f"AI ({reading.reply_type}): {reading.reason}"
    if reading.aggregate_total is not None:
        note += f" Συγκεντρωτικό σύνολο χωρίς ανάλυση: {reading.aggregate_total}."
    return values, note, reading.reply_type, reading.is_correction


def process(con, now_iso: str, cfg=None) -> list:
    """Apply every reply not processed yet. Each message is handled once, so
    running this again cannot count the same email twice.

    Replies in the agreed format are matched directly. Anything else goes to
    the AI reader when an API key is configured; without one it is left for a
    human, exactly as before."""
    results = []
    pending = con.execute(
        """SELECT x.id, x.request_id, x.message_id, x.body, x.from_addr, x.attachments,
                  m.name, r.period, r.program
             FROM messages x
             JOIN requests r ON r.id = x.request_id
             JOIN merchants m ON m.id = r.merchant_id
            WHERE x.direction='in' AND x.kind='reply' AND x.processed_at IS NULL
            ORDER BY x.id"""
    ).fetchall()
    for msg in pending:
        offers = {}
        for o in con.execute(
            """SELECT o.uuid, o.admin_name FROM request_offers ro
                 JOIN offers o ON o.uuid = ro.offer_uuid WHERE ro.request_id=?""",
            (msg["request_id"],),
        ):
            offers.setdefault(offer_code(o["uuid"]), []).append(o)
        values, conflicts = parse(msg["body"])
        res = {"merchant": msg["name"], "written": [], "unchanged": 0, "unknown": [], "conflicts": []}
        ai_note = reply_type = None
        from_ai, is_correction = False, True
        if not values and not conflicts and cfg is not None and (cfg.anthropic_key or cfg.gemini_key):
            recorded = {}
            for code, items in offers.items():
                row = con.execute(
                    "SELECT redeemed FROM redemptions WHERE offer_uuid=? AND period=?",
                    (items[0]["uuid"], msg["period"]),
                ).fetchone()
                if len(items) == 1 and row and row["redeemed"] is not None:
                    recorded[code] = row["redeemed"]
            try:
                values, ai_note, reply_type, is_correction = _read_with_ai(cfg, msg, offers, recorded)
                from_ai = True
            except Exception as e:  # ai_reader.ReaderError, or a missing library
                # Not marked as processed: the next run tries this reply again.
                res.update(status="replied", filled=0, total=len(offers),
                           note=f"η ανάγνωση με AI απέτυχε ({e})· θα ξαναδοκιμαστεί")
                results.append(res)
                continue
        with con:
            for code in sorted(conflicts):
                if code in offers and len(offers[code]) == 1:
                    o = offers[code][0]
                    con.execute(
                        "UPDATE redemptions SET status='needs_review', updated_at=? "
                        "WHERE offer_uuid=? AND period=?",
                        (now_iso, o["uuid"], msg["period"]),
                    )
                    res["conflicts"].append(o["admin_name"])
            for code, value in values.items():
                if code not in offers or len(offers[code]) != 1:
                    res["unknown"].append(code)
                    continue
                o = offers[code][0]
                old = con.execute(
                    "SELECT redeemed FROM redemptions WHERE offer_uuid=? AND period=?",
                    (o["uuid"], msg["period"]),
                ).fetchone()
                old = old["redeemed"] if old else None
                if old == value:
                    res["unchanged"] += 1
                    continue
                if old is not None and from_ai and not is_correction:
                    # A free-text reply gives a different number for an offer
                    # that already has one, without saying it corrects it. It
                    # could be a correction or an addition ("another 5"), so
                    # the recorded value stays and a person decides.
                    con.execute(
                        "UPDATE redemptions SET status='needs_review', updated_at=? "
                        "WHERE offer_uuid=? AND period=?",
                        (now_iso, o["uuid"], msg["period"]),
                    )
                    res["conflicts"].append(o["admin_name"])
                    res.setdefault("ambiguous", []).append((o["admin_name"], old, value))
                    continue
                # A later reply replaces the value (it is a correction, not an
                # addition) and the old value is kept in the history.
                con.execute(
                    """INSERT INTO redemptions (offer_uuid, period, redeemed, status, source, updated_at)
                       VALUES (?, ?, ?, 'reported', ?, ?)
                       ON CONFLICT(offer_uuid, period) DO UPDATE SET
                         redeemed=excluded.redeemed, status='reported',
                         source=excluded.source, updated_at=excluded.updated_at""",
                    (o["uuid"], msg["period"], value, msg["message_id"], now_iso),
                )
                con.execute(
                    """INSERT INTO redemption_history
                         (offer_uuid, period, old_value, new_value, source, changed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (o["uuid"], msg["period"], old, value, msg["message_id"], now_iso),
                )
                res["written"].append((o["admin_name"], old, value))

            total, filled = con.execute(
                """SELECT count(*), count(d.redeemed) FROM request_offers ro
                     LEFT JOIN redemptions d ON d.offer_uuid = ro.offer_uuid AND d.period = ?
                    WHERE ro.request_id = ?""",
                (msg["period"], msg["request_id"]),
            ).fetchone()
            status = "completed" if filled == total else "partial" if filled else "replied"
            if reply_type == "promise" and not filled:
                # "We will send it" is not an answer: back to 'sent', so the
                # reminders keep going.
                status = "sent"
            con.execute("UPDATE requests SET status=? WHERE id=?", (status, msg["request_id"]))
            if ai_note:
                note = f"{ai_note} [{len(res['written'])} καταχωρήσεις, {len(res['unknown'])} άγνωστοι κωδικοί]"
                for name, old, new in res.get("ambiguous", []):
                    note += f" Δεν είναι σαφές αν διορθώνει ή προσθέτει: {name}, καταχωρημένο {old}, νέο {new}."
            elif not values and not conflicts:
                note = "καμία γραμμή στη συμφωνημένη μορφή· θέλει έλεγχο"
            else:
                note = (f"{len(res['written'])} καταχωρήσεις, {res['unchanged']} ίδιες, "
                        f"{len(res['unknown'])} άγνωστοι κωδικοί, {len(res['conflicts'])} αντιφάσεις")
            # A reply that gave us nothing usable, or that contradicts what is
            # recorded, must be seen by a person even when the request already
            # counts as partial or completed. A promise is the exception: it
            # only means "keep waiting".
            flag = bool(res["conflicts"]) or (
                not res["written"] and not res["unchanged"] and reply_type != "promise"
            )
            con.execute(
                "UPDATE messages SET processed_at=?, parse_note=?, needs_review=? WHERE id=?",
                (now_iso, note, int(flag), msg["id"]),
            )
        res.update(status=status, filled=filled, total=total, note=note)
        results.append(res)
    return results
