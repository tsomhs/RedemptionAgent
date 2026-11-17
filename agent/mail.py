"""Sending requests and follow-ups over SMTP, collecting replies over IMAP."""

import email
import hashlib
import html
import imaplib
import json
import re
import smtplib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parseaddr
from pathlib import Path

from .config import ROOT, Config
from .replies import offer_code

FOLLOWUP_TEXT = (
    "Καλημέρα σας,\n\n"
    "Επανερχόμαστε στο παρακάτω αίτημά μας, καθώς δεν έχουμε λάβει ακόμη τα στοιχεία "
    "εξαργυρώσεων για τον μήνα {label}. Θα μας βοηθούσε να τα έχουμε το συντομότερο δυνατό.\n\n"
    "Ευχαριστούμε εκ των προτέρων."
)
PARTIAL_FOLLOWUP_TEXT = (
    "Καλημέρα σας,\n\n"
    "ευχαριστούμε για τα στοιχεία που μας στείλατε. Για τον μήνα {label} μας λείπουν ακόμη "
    "οι εξαργυρώσεις για τις παρακάτω προσφορές:\n\n{lines}\n\n"
    "Ευχαριστούμε εκ των προτέρων."
)
# The email a reminder or clarification is threaded under: the latest request
# email that actually went out (a request can be sent again after a bounce).
THREAD_ID_SQL = (
    "(SELECT message_id FROM messages x WHERE x.request_id=r.id "
    "AND x.kind IN ('initial', 'resend') ORDER BY x.id DESC LIMIT 1)"
)
_IMAP_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


class MailError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


# --------------------------------------------------------------- safety guard
def blocked_recipients(cfg: Config, addresses) -> list:
    """In test mode nothing may be sent outside ALLOWED_RECIPIENTS. An empty
    allowlist therefore blocks everything, which is the safe default."""
    if cfg.mode == "live":
        return []
    bad = []
    for a in addresses:
        a = a.lower()
        domain = "@" + a.rsplit("@", 1)[-1]
        if a not in cfg.allowed and _base(a) not in cfg.allowed and domain not in cfg.allowed:
            bad.append(a)
    return bad


def _base(address: str) -> str:
    """name+tag@host -> name@host. Plus-aliases deliver to the same mailbox, so
    one test inbox can stand in for many partners."""
    local, _, host = address.lower().partition("@")
    return f"{local.split('+', 1)[0]}@{host}"


def _require_password(cfg: Config, host: str, password=None):
    if host not in ("localhost", "127.0.0.1") and not (cfg.password if password is None else password):
        raise MailError("Λείπει το MAIL_PASSWORD στο αρχείο ρυθμίσεων (app password του λογαριασμού)")


# ----------------------------------------------------------------------- SMTP
def _smtp(cfg: Config, user=None, password=None):
    """user/password default to the agent's mailbox; the test partner mailbox
    passes its own."""
    if not cfg.smtp_host:
        raise MailError("Δεν έχει οριστεί SMTP_HOST στο αρχείο ρυθμίσεων")
    _require_password(cfg, cfg.smtp_host, password)
    if user is None:
        user, password = cfg.user, cfg.password
    if cfg.smtp_security == "ssl":
        s = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30)
    else:
        s = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
        if cfg.smtp_security == "starttls":
            s.starttls()
    if user and cfg.smtp_security != "none":
        s.login(user, password)
    return s


def _build(sender, to, subject, body, reply_to_id=None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    if reply_to_id:
        msg["In-Reply-To"] = reply_to_id
        msg["References"] = reply_to_id
    msg.set_content(body)
    return msg


def _record(con, request_id, direction, kind, msg, body, at, attachments=None):
    con.execute(
        """INSERT INTO messages (request_id, direction, kind, message_id, in_reply_to,
                                 from_addr, to_addrs, subject, body, attachments, at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            request_id, direction, kind, msg["Message-ID"], msg["In-Reply-To"],
            parseaddr(msg["From"])[1].lower(),
            ", ".join(a for _, a in getaddresses(msg.get_all("To", []))),
            msg["Subject"], body, json.dumps(attachments or [], ensure_ascii=False), _iso(at),
        ),
    )  # fmt: skip


def _contacts(con, merchant_id):
    return [
        r["email"]
        for r in con.execute(
            "SELECT email FROM merchant_contacts WHERE merchant_id=? ORDER BY email", (merchant_id,)
        )
    ]


SEND_LOCK_MINUTES = 15


@contextmanager
def sending(con, now):
    """Only one process sends at a time. Without this, the scheduler, a command
    typed in a terminal and the "send" button could each pick up the same
    request and email it twice. A lock left behind by a crash expires."""
    stale = _iso(now - timedelta(minutes=SEND_LOCK_MINUTES))
    with con:
        got = con.execute(
            """INSERT INTO automation_state (key, value) VALUES ('lock.send', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value
                WHERE automation_state.value < ?""",
            (_iso(now), stale),
        ).rowcount
    if not got:
        raise MailError("Γίνεται ήδη αποστολή από άλλη διεργασία· δοκιμάστε ξανά σε λίγο")
    try:
        yield
    finally:
        with con:
            con.execute("DELETE FROM automation_state WHERE key='lock.send'")


def _note_refused(con, request_id, refused, now):
    """Some of the addresses were turned down while the rest went through: the
    request stands, the bad addresses become a contact problem."""
    if refused:
        reason = "; ".join(f"{a}: {c} {t.decode('utf-8', 'replace')}" for a, (c, t) in refused.items())
        record_bounce(con, request_id, list(refused), f"Ο mail server αρνήθηκε μέρος των παραληπτών. {reason}", now)


def send_requests(con, cfg: Config, period, program, now, limit=None, only=None) -> dict:
    with sending(con, now):
        return _send_requests(con, cfg, period, program, now, limit, only)


def _send_requests(con, cfg: Config, period, program, now, limit=None, only=None) -> dict:
    """Send the draft requests of the period. Each request is committed right
    after its email leaves, so a crash mid-run cannot cause a second send."""
    rows = con.execute(
        """SELECT r.id, r.subject, r.body, r.merchant_id, m.name
             FROM requests r JOIN merchants m ON m.id = r.merchant_id
            WHERE r.period=? AND r.program=? AND r.status='draft' ORDER BY m.name""",
        (period, program),
    ).fetchall()
    if only:
        wanted = [w.strip().lower() for w in only.split(",") if w.strip()]
        # A term that is exactly a partner's name selects only that partner;
        # otherwise it is a substring search.
        names = {r["name"].lower() for r in rows}
        rows = [
            r for r in rows
            if any(r["name"].lower() == w if w in names else w in r["name"].lower() for w in wanted)
        ]
    if limit:
        rows = rows[:limit]
    out = {"sent": [], "blocked": {}, "no_contact": [], "refused": {}}
    if not rows:
        return out
    smtp = None
    try:
        for r in rows:
            to = _contacts(con, r["merchant_id"])
            if not to:
                out["no_contact"].append(r["name"])
                continue
            bad = blocked_recipients(cfg, to)
            if bad:
                out["blocked"][r["name"]] = bad
                continue
            smtp = smtp or _smtp(cfg)
            msg = _build(cfg.mail_from, to, r["subject"], r["body"])
            try:
                refused = smtp.send_message(msg) or {}
            except smtplib.SMTPRecipientsRefused as e:
                # The server would not take any of the addresses: nothing was
                # sent. That is a contact problem, not a delivered request.
                reason = "; ".join(f"{a}: {c} {t.decode('utf-8', 'replace')}" for a, (c, t) in e.recipients.items())
                with con:
                    con.execute("UPDATE requests SET status='undelivered' WHERE id=?", (r["id"],))
                    record_bounce(con, r["id"], to, f"Ο mail server αρνήθηκε τους παραλήπτες. {reason}", now)
                out["refused"][r["name"]] = to
                continue
            with con:
                con.execute(
                    "UPDATE requests SET status='sent', sent_at=? WHERE id=?", (_iso(now), r["id"])
                )
                _record(con, r["id"], "out", "initial", msg, r["body"], now)
                _note_refused(con, r["id"], refused, now)
            out["sent"].append((r["name"], to))
    finally:
        if smtp:
            smtp.quit()
    return out


def record_bounce(con, request_id, failed, text, at, message_id=None, subject="Αποτυχία παράδοσης"):
    """A delivery failure, stored in the thread of its request. to_addrs holds
    the addresses that failed; processed_at stays empty until someone deals
    with it in the review queue."""
    con.execute(
        """INSERT INTO messages (request_id, direction, kind, message_id, from_addr, to_addrs,
                                 subject, body, attachments, at)
           VALUES (?, 'in', 'bounce', ?, 'mailer-daemon', ?, ?, ?, '[]', ?)""",
        (request_id, message_id or make_msgid(domain="bounce.local"),
         ", ".join(sorted(failed)), subject, text, _iso(at)),
    )  # fmt: skip


def _missing_offers(con, request_id, period):
    return con.execute(
        """SELECT o.uuid, o.admin_name FROM request_offers ro
             JOIN offers o ON o.uuid = ro.offer_uuid
             LEFT JOIN redemptions d ON d.offer_uuid = o.uuid AND d.period = ?
            WHERE ro.request_id = ? AND d.redeemed IS NULL ORDER BY o.admin_name""",
        (period, request_id),
    ).fetchall()


def send_followups(con, cfg: Config, now, label_for) -> dict:
    with sending(con, now):
        return _send_followups(con, cfg, now, label_for)


def _send_followups(con, cfg: Config, now, label_for) -> dict:
    """One reminder per overdue request, up to MAX_FOLLOWUPS: FOLLOWUP_DAYS
    after the request and after each previous reminder.

    Requests still waiting ('sent') get the general reminder. Requests answered
    in part ('partial') stay open for what is missing and get a reminder that
    lists only those offers. Complete, unreadable and undelivered requests are
    not chased."""
    rows = con.execute(
        f"""SELECT r.id, r.subject, r.period, r.status, r.sent_at, r.followups_sent, r.merchant_id, m.name,
                  {THREAD_ID_SQL} AS first_id,
                  (SELECT max(x.at) FROM messages x WHERE x.request_id=r.id
                      AND (x.kind IN ('followup', 'clarification')
                           OR (x.direction='in' AND x.kind='reply'))) AS last_contact
             FROM requests r JOIN merchants m ON m.id = r.merchant_id
            WHERE r.status IN ('sent', 'partial') ORDER BY m.name"""
    ).fetchall()
    out = {"sent": [], "exhausted": [], "blocked": {}}
    smtp = None
    try:
        for r in rows:
            if r["followups_sent"] >= cfg.max_followups:
                out["exhausted"].append(r["name"])
                continue
            due = _parse(r["sent_at"]) + timedelta(days=cfg.followup_days * (r["followups_sent"] + 1))
            if r["last_contact"]:
                # Keep a full interval after the last reminder, clarification or
                # reply. Without this, missed runs would send the reminders one
                # after the other, and a partner who just wrote to us would be
                # chased the next day.
                due = max(due, _parse(r["last_contact"]) + timedelta(days=cfg.followup_days))
            if now < due:
                continue
            to = _contacts(con, r["merchant_id"])
            bad = blocked_recipients(cfg, to)
            if bad or not to:
                out["blocked"][r["name"]] = bad
                continue
            if r["status"] == "partial":
                missing = _missing_offers(con, r["id"], r["period"])
                if not missing:
                    continue
                lines = "\n".join(f"[{offer_code(o['uuid'])}] {o['admin_name']}" for o in missing)
                body = PARTIAL_FOLLOWUP_TEXT.format(label=label_for(r["period"]), lines=lines)
            else:
                body = FOLLOWUP_TEXT.format(label=label_for(r["period"]))
            smtp = smtp or _smtp(cfg)
            msg = _build(cfg.mail_from, to, "Re: " + r["subject"], body, r["first_id"])
            try:
                refused = smtp.send_message(msg) or {}
            except smtplib.SMTPRecipientsRefused as e:
                # The address stopped working since the request went out. Not
                # a reason to stop the run: this becomes a contact problem and
                # the other partners still get their reminders.
                reason = "; ".join(f"{a}: {c} {t.decode('utf-8', 'replace')}" for a, (c, t) in e.recipients.items())
                with con:
                    if r["status"] == "sent":
                        con.execute("UPDATE requests SET status='undelivered' WHERE id=?", (r["id"],))
                    record_bounce(con, r["id"], to, f"Ο mail server αρνήθηκε τους παραλήπτες της υπενθύμισης. {reason}", now)
                out["blocked"][r["name"]] = to
                continue
            with con:
                con.execute(
                    "UPDATE requests SET followups_sent = followups_sent + 1 WHERE id=?", (r["id"],)
                )
                _record(con, r["id"], "out", "followup", msg, body, now)
                _note_refused(con, r["id"], refused, now)
            out["sent"].append((r["name"], r["followups_sent"] + 1))
    finally:
        if smtp:
            smtp.quit()
    return out


# ----------------------------------------------------------------------- IMAP
def _imap(cfg: Config, user=None, password=None):
    if not cfg.imap_host:
        raise MailError("Δεν έχει οριστεί IMAP_HOST στο αρχείο ρυθμίσεων")
    _require_password(cfg, cfg.imap_host, password)
    if user is None:
        user, password = cfg.user, cfg.password
    cls = imaplib.IMAP4_SSL if cfg.imap_security == "ssl" else imaplib.IMAP4
    m = cls(cfg.imap_host, cfg.imap_port)
    m.login(user, password)
    return m


def _content(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError):  # a character set Python does not know
        return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")


def _text_of(msg) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return _content(part).strip()
    part = msg.get_body(preferencelist=("html",))
    if part is not None:
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", _content(part))
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
        return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    return ""


def _is_auto_reply(msg) -> bool:
    auto = (msg["Auto-Submitted"] or "no").lower()
    return auto != "no" or bool(msg["X-Autoreply"]) or bool(msg["X-Autorespond"])


def _save_attachments(msg, message_id) -> list:
    saved, used = [], set()
    folder = ROOT / "data" / "attachments" / re.sub(r"[^\w.\-]+", "_", message_id.strip("<>"))[:120]
    for part in msg.iter_attachments():
        name = re.sub(r"[^\w.\- ]+", "_", part.get_filename() or "attachment")
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        # Short enough for any file system, and never the same name twice:
        # two files called report.xlsx in one email must both survive.
        stem, ext = stem[:80] or "attachment", ext[:10]
        name, n = (f"{stem}.{ext}" if ext else stem), 1
        while name.lower() in used:
            n += 1
            name = f"{stem}_{n}.{ext}" if ext else f"{stem}_{n}"
        used.add(name.lower())
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(part.get_payload(decode=True) or b"")
        # Forward slashes, so a database made on Windows also reads on Linux or macOS.
        saved.append((folder / name).relative_to(ROOT).as_posix())
    return saved


def _match_request(con, msg, sender):
    refs = set(re.findall(r"<[^>]+>", f"{msg['In-Reply-To'] or ''} {msg['References'] or ''}"))
    if refs:
        row = con.execute(
            f"""SELECT request_id FROM messages WHERE direction='out'
                 AND message_id IN ({','.join('?' * len(refs))}) LIMIT 1""",
            tuple(refs),
        ).fetchone()
        if row:
            return row["request_id"]
    # No link to one of our emails. The sender alone is not enough: a partner
    # may write about something else, and an old email could be read as the
    # answer to the only request that happens to be open. It is stored without
    # a request and a person assigns it in the review queue.
    return None


def _known_sender(con, sender) -> bool:
    return any(
        _base(r["email"]) == _base(sender) for r in con.execute("SELECT email FROM merchant_contacts")
    )


def _bounce_info(msg):
    """None for ordinary mail. For a delivery status notification:
    (failed, addresses) where failed is False for 'still trying' notices."""
    sender = parseaddr(msg["From"] or "")[1].lower()
    is_dsn = (
        msg.get_content_type() == "multipart/report"
        and str(msg.get_param("report-type") or "").lower() == "delivery-status"
    )
    daemon = sender.split("@")[0] in ("mailer-daemon", "postmaster")
    if not (is_dsn or daemon or msg["X-Failed-Recipients"]):
        return None
    text = msg.as_string()
    failed = {a.strip().lower() for a in str(msg["X-Failed-Recipients"] or "").split(",") if a.strip()}
    failed |= {a.lower() for a in re.findall(r"(?im)^Final-Recipient:\s*rfc822;\s*<?([^\s>]+)", text)}
    actions = {a.lower() for a in re.findall(r"(?im)^Action:\s*([\w-]+)", text)}
    if actions and "failed" not in actions:
        return False, failed  # delayed or relayed: delivery may still happen
    return True, failed


def _bounce_request(con, msg, failed):
    """Which request a bounce is about: by the id of our email quoted in it,
    else by a failed address that belongs to exactly one open request."""
    text = msg.as_string()
    ids = set(re.findall(r"<[^<>\s]+@[^<>\s]+>", f"{msg['In-Reply-To'] or ''} {msg['References'] or ''}"))
    ids |= set(re.findall(r"(?im)^Message-ID:\s*(<[^>]+>)", text))
    if ids:
        row = con.execute(
            f"""SELECT request_id FROM messages WHERE direction='out'
                 AND message_id IN ({','.join('?' * len(ids))}) LIMIT 1""",
            tuple(ids),
        ).fetchone()
        if row:
            return row["request_id"]
    if failed:
        rows = con.execute(
            """SELECT DISTINCT r.id, c.email FROM requests r
                 JOIN merchant_contacts c ON c.merchant_id = r.merchant_id
                WHERE r.status IN ('sent', 'partial', 'replied')"""
        ).fetchall()
        found = {r["id"] for r in rows if r["email"].lower() in failed}
        if len(found) == 1:
            return found.pop()
    return None


def store_incoming(con, cfg: Config, msg, now, out: dict):
    """File one inbox message. Called by fetch_replies for every new email."""
    if not msg["Message-ID"]:
        # The same id every time this email is seen, so it is stored once.
        seed = f"{msg['From']}|{msg['Date']}|{msg['Subject']}|{_text_of(msg)}"
        msg["Message-ID"] = f"<{hashlib.sha1(seed.encode('utf-8', 'replace')).hexdigest()}@no-id.local>"
    if con.execute("SELECT 1 FROM messages WHERE message_id=?", (msg["Message-ID"],)).fetchone():
        return
    sender = parseaddr(msg["From"] or "")[1].lower()
    if sender == cfg.mail_from.lower():
        return  # our own copy (e.g. a mailbox that receives what it sends)

    bounce = _bounce_info(msg)
    if bounce is not None:
        is_failure, failed = bounce
        request_id = _bounce_request(con, msg, failed)
        if request_id is None:
            out["ignored"] += 1
            return
        if not is_failure:
            with con:
                _record(con, request_id, "in", "auto", msg, _text_of(msg)[:4000], now)
            out["auto"].append((sender, msg["Subject"] or ""))
            return
        req = con.execute(
            f"""SELECT r.status, m.name,
                       (SELECT to_addrs FROM messages x WHERE x.request_id=r.id
                         AND x.kind IN ('initial', 'resend') ORDER BY x.id DESC LIMIT 1) AS sent_to
                  FROM requests r JOIN merchants m ON m.id = r.merchant_id WHERE r.id=?""",
            (request_id,),
        ).fetchone()
        recipients = {a.strip().lower() for a in (req["sent_to"] or "").split(",") if a.strip()}
        with con:
            record_bounce(con, request_id, failed, _text_of(msg)[:4000], now, msg["Message-ID"], msg["Subject"] or "")
            # Undelivered only when nobody received it. If one of several
            # addresses failed, the request stands and the bad address is
            # shown as a contact problem.
            if req["status"] == "sent" and (not failed or recipients <= failed):
                con.execute("UPDATE requests SET status='undelivered' WHERE id=?", (request_id,))
        out["bounced"].append((req["name"], sorted(failed)))
        return

    request_id = _match_request(con, msg, sender)
    if request_id is None and not _known_sender(con, sender):
        # Unrelated mail in a shared inbox: not ours to read or keep.
        out["ignored"] += 1
        return
    kind = "auto" if _is_auto_reply(msg) else "reply"
    files = _save_attachments(msg, msg["Message-ID"])
    with con:
        _record(con, request_id, "in", kind, msg, _text_of(msg), now, files)
        if request_id and kind == "reply":
            con.execute(
                "UPDATE requests SET status='replied' WHERE id=? AND status='sent'",
                (request_id,),
            )
    entry = (sender, msg["Subject"] or "")
    (out["unmatched"] if not request_id else out["auto" if kind == "auto" else "new"]).append(entry)


def _store_unreadable(con, msg, message_id, error, now):
    try:
        subject = str(msg["Subject"] or "")
    except Exception:
        subject = ""
    with con:
        con.execute(
            """INSERT OR IGNORE INTO messages (request_id, direction, kind, message_id, from_addr, subject,
                                              body, attachments, at, needs_review)
               VALUES (NULL, 'in', 'reply', ?, ?, ?, ?, '[]', ?, 1)""",
            (message_id, parseaddr(str(msg["From"] or ""))[1].lower(), subject,
             f"Το email δεν μπόρεσε να διαβαστεί αυτόματα ({type(error).__name__}: {error}). "
             "Ανοίξτε το στο mailbox.", _iso(now)),
        )  # fmt: skip


def fetch_replies(con, cfg: Config, now) -> dict:
    """Store new inbox messages. The mailbox is opened read-only, so nothing is
    marked as read or moved. A message already stored (same Message-ID) is
    skipped, which makes repeated runs harmless."""
    first = con.execute("SELECT min(sent_at) FROM requests WHERE sent_at IS NOT NULL").fetchone()[0]
    out = {"new": [], "auto": [], "unmatched": [], "bounced": [], "ignored": 0}
    if not first:
        return out
    since = _parse(first) - timedelta(days=1)
    since_arg = f"{since.day:02d}-{_IMAP_MONTHS[since.month - 1]}-{since.year}"
    m = _imap(cfg)
    try:
        m.select("INBOX", readonly=True)
        _, data = m.uid("search", None, "SINCE", since_arg)
        for uid in data[0].split():
            _, head = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            raw_head = next((p[1] for p in head if isinstance(p, tuple)), b"")
            mid = (email.message_from_bytes(raw_head)["Message-ID"] or "").strip()
            if mid and con.execute("SELECT 1 FROM messages WHERE message_id=?", (mid,)).fetchone():
                continue
            _, full = m.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in full if isinstance(p, tuple)), None)
            if raw is None:
                continue
            msg = email.message_from_bytes(raw, policy=policy.default)
            try:
                store_incoming(con, cfg, msg, now, out)
            except Exception as e:
                # One email we cannot handle must not hide the ones after it.
                # It is kept, unread, for a person; it will not be tried again.
                _store_unreadable(con, msg, mid or f"<{hashlib.sha1(raw).hexdigest()}@no-id.local>", e, now)
                out["unmatched"].append((parseaddr(str(msg["From"] or ""))[1].lower(), "(δεν διαβάστηκε)"))
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return out


# --------------------------------------------------------------- test helpers
def _find_in_partner_mailbox(cfg: Config, message_id: str) -> str:
    """Where did our request land in the test partner mailbox?
    Returns 'inbox', 'spam' or '' (not found). Read-only."""
    m = _imap(cfg, cfg.partner_user, cfg.partner_password)
    try:
        folders = [("inbox", "INBOX")]
        _, listing = m.list()
        for line in listing or []:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            if r"\Junk" in text:
                folders.append(("spam", text.rsplit(' "/" ', 1)[-1].strip()))
        for label, folder in folders:
            if m.select(folder, readonly=True)[0] != "OK":
                continue
            _, data = m.uid("search", None, "HEADER", "Message-ID", f'"{message_id}"')
            if data and data[0].split():
                return label
        return ""
    finally:
        try:
            m.logout()
        except Exception:
            pass


def simulate_reply(con, cfg: Config, merchant, period, text, attach: Path | None = None, auto=False):
    """Play the partner and answer a request. Test mode only. With the local
    mail server the reply comes from the partner's fake address; with a real
    provider it is sent from the TEST_PARTNER_USER mailbox, after checking that
    the request actually arrived there."""
    if cfg.mode != "test":
        raise MailError("Το simulate-reply δουλεύει μόνο σε δοκιμαστική λειτουργία")
    local = cfg.smtp_host in ("localhost", "127.0.0.1")
    if not local and not (cfg.partner_user and cfg.partner_password):
        raise MailError("Χρειάζεται TEST_PARTNER_USER και TEST_PARTNER_PASSWORD στο αρχείο ρυθμίσεων")
    r = con.execute(
        f"""SELECT r.id, r.subject, r.merchant_id, m.name,
                  {THREAD_ID_SQL} AS first_id
             FROM requests r JOIN merchants m ON m.id=r.merchant_id
            WHERE r.period=? AND lower(m.name) LIKE ? AND r.status != 'draft'""",
        (period, f"%{merchant.lower()}%"),
    ).fetchall()
    if len(r) != 1:
        raise MailError(f"Βρέθηκαν {len(r)} σταλμένα αιτήματα για «{merchant}» (χρειάζεται ακριβώς 1)")
    r = r[0]
    delivered, creds = None, ()
    if local:
        sender = _contacts(con, r["merchant_id"])[0]
    else:
        if blocked_recipients(cfg, [cfg.partner_user]):
            raise MailError("Το TEST_PARTNER_USER πρέπει να είναι μέσα στα ALLOWED_RECIPIENTS")
        delivered = _find_in_partner_mailbox(cfg, r["first_id"])
        if not delivered:
            raise MailError(f"Το αίτημα προς «{r['name']}» δεν βρέθηκε στο mailbox {cfg.partner_user}")
        sender, creds = cfg.partner_user, (cfg.partner_user, cfg.partner_password)
    msg = _build(sender, [cfg.mail_from], "Re: " + r["subject"], text, r["first_id"])
    if auto:
        msg["Auto-Submitted"] = "auto-replied"
    if attach:
        data = Path(attach).read_bytes()
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=Path(attach).name)
    with _smtp(cfg, *creds) as s:
        s.send_message(msg)
    return r["name"], sender, delivered
