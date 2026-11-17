"""Shared setup for the tests: a throw-away database and a fake mail connection.

Nothing here touches the real databases in data/ or any mail server. Each test
gets its own empty database in a temporary folder, and "sending" only collects
the messages in a list.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent import db, mail, prepare_requests, replies
from agent.config import Config

PROGRAM = "ΔΕΗ myRewards Coupons"
PERIOD = "2026-10"
SENT = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
OFFER_A = "aaaaaaaa-0000-0000-0000-000000000001"
OFFER_B = "bbbbbbbb-0000-0000-0000-000000000002"
OFFER_C = "cccccccc-0000-0000-0000-000000000003"


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


def day(n, hour=9):
    """n days after the initial request went out."""
    return SENT + timedelta(days=n, hours=hour - 9)


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.con = db.connect(self.dir / "t.db")
        self.cfg = Config(mode="test", mail_from="agent@warply.test", allowed=["@partners.test"])
        self.smtp = FakeSMTP()
        self._real_smtp = mail._smtp
        mail._smtp = lambda cfg, *a, **k: self.smtp

    def tearDown(self):
        mail._smtp = self._real_smtp
        self.con.close()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- fixtures
    def add_merchant(self, name, email_addr, uuids):
        """A partner with a contact and offers that have assignments in PERIOD."""
        c = self.con
        with c:
            mid = c.execute("INSERT INTO merchants (name) VALUES (?)", (name,)).lastrowid
            if email_addr:
                c.execute("INSERT INTO merchant_contacts VALUES (?, ?)", (mid, email_addr))
            for u in uuids:
                c.execute(
                    "INSERT INTO offers (uuid, program, segment, admin_name, merchant_id) "
                    "VALUES (?, ?, 'generic', ?, ?)",
                    (u, PROGRAM, f"offer {u[:8]}", mid),
                )
                c.execute("INSERT INTO assignments (offer_uuid, period, assignments) VALUES (?, ?, 10)", (u, PERIOD))
                c.execute("INSERT INTO redemptions (offer_uuid, period) VALUES (?, ?)", (u, PERIOD))
        return mid

    def sent_request(self, name, email_addr, uuids=(OFFER_A, OFFER_B)):
        """A request that already went out at SENT, with its initial email."""
        mid = self.add_merchant(name, email_addr, uuids)
        c = self.con
        with c:
            rid = c.execute(
                """INSERT INTO requests (program, merchant_id, period, status, subject, body, sent_at)
                   VALUES (?, ?, ?, 'sent', 'Εξαργυρώσεις', 'κείμενο', ?)""",
                (PROGRAM, mid, PERIOD, mail._iso(SENT)),
            ).lastrowid
            for u in uuids:
                c.execute("INSERT INTO request_offers VALUES (?, ?)", (rid, u))
            c.execute(
                """INSERT INTO messages (request_id, direction, kind, message_id, from_addr, to_addrs, subject, body, at)
                   VALUES (?, 'out', 'initial', ?, 'agent@warply.test', ?, 'Εξαργυρώσεις', 'κείμενο', ?)""",
                (rid, f"<initial-{rid}@warply.test>", email_addr, mail._iso(SENT)),
            )
        return rid

    def reply(self, request_id, body, at=None):
        """Store an incoming reply the way 'fetch' does, then read it."""
        at = mail._iso(at or day(3))
        with self.con:
            n = self.con.execute("SELECT count(*) FROM messages").fetchone()[0]
            self.con.execute(
                """INSERT INTO messages (request_id, direction, kind, message_id, from_addr, body, attachments, at)
                   VALUES (?, 'in', 'reply', ?, 'partner@partners.test', ?, '[]', ?)""",
                (request_id, f"<reply-{n}@partners.test>", body, at),
            )
            self.con.execute("UPDATE requests SET status='replied' WHERE id=? AND status='sent'", (request_id,))
        return replies.process(self.con, at, self.cfg)

    # ----------------------------------------------------------------- lookups
    def status(self, request_id):
        return self.con.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()[0]

    def redeemed(self, uuid):
        row = self.con.execute(
            "SELECT redeemed, status FROM redemptions WHERE offer_uuid=? AND period=?", (uuid, PERIOD)
        ).fetchone()
        return row["redeemed"], row["status"]

    def followups(self, now):
        return mail.send_followups(self.con, self.cfg, now, prepare_requests.period_label)
