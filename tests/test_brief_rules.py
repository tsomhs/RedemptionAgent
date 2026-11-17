"""Κανόνες του brief: ανεπίδοτα, επαναποστολή, αμφίσημες διορθώσεις, λάθος αρχείο μήνα."""

import unittest
from datetime import date
from email import message_from_string, policy
from types import SimpleNamespace

from openpyxl import Workbook

from base import OFFER_A, OFFER_B, OFFER_C, PERIOD, PROGRAM, AgentTestCase, day
from agent import assignments, mail, ai_reader, report, review

HEADER = ["UUID", "Admin Name", "Category", "Assignments", "Unique Users", "Engage URL"]


def bounce(original_id, failed, action="failed", mid="<dsn-1@mail.example>"):
    """A delivery status notification shaped like the ones Gmail sends."""
    return message_from_string(
        f"""From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: agent@warply.test
Subject: Delivery Status Notification (Failure)
Message-ID: {mid}
X-Failed-Recipients: {failed}
References: {original_id}
In-Reply-To: {original_id}
MIME-Version: 1.0
Content-Type: multipart/report; boundary="b1"; report-type=delivery-status

--b1
Content-Type: text/plain; charset="UTF-8"

Address not found. Your message wasn't delivered to {failed}.
--b1
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com

Final-Recipient: rfc822; {failed}
Action: {action}
Status: 5.1.1
--b1--
""",
        policy=policy.default,
    )


class BounceTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.rid = self.sent_request("Kotsovolos", "kotsovolos@partners.test")
        self.out = {"new": [], "auto": [], "unmatched": [], "bounced": [], "ignored": 0}

    def incoming(self, msg, at=None):
        mail.store_incoming(self.con, self.cfg, msg, at or day(1), self.out)

    def test_bounce_is_a_contact_problem(self):
        """Ανεπίδοτο email: το αίτημα δεν θεωρείται σταλμένο, δεν παίρνει υπενθυμίσεις και μπαίνει στην ουρά"""
        self.incoming(bounce(f"<initial-{self.rid}@warply.test>", "kotsovolos@partners.test"))
        self.assertEqual(self.status(self.rid), "undelivered")
        self.assertEqual(self.out["bounced"], [("Kotsovolos", ["kotsovolos@partners.test"])])
        self.assertEqual(self.followups(day(30))["sent"], [])
        (item,) = review.queue(self.con)["contact_problems"]
        self.assertEqual((item["merchant"], item["failed"]), ("Kotsovolos", ["kotsovolos@partners.test"]))
        self.assertIn("Address not found", item["bounce_text"])
        rows = report.rows(self.con)
        self.assertEqual({r["status"] for r in rows}, {report.UNDELIVERED})
        s = report.summary(rows)["requests"]
        self.assertEqual((s["sent"], s["undelivered"]), (0, 1))

    def test_same_bounce_twice(self):
        """Το ίδιο ανεπίδοτο δεν αποθηκεύεται δύο φορές"""
        for _ in range(2):
            self.incoming(bounce(f"<initial-{self.rid}@warply.test>", "kotsovolos@partners.test"))
        self.assertEqual(self.con.execute("SELECT count(*) FROM messages WHERE kind='bounce'").fetchone()[0], 1)

    def test_delay_notice_is_not_a_failure(self):
        """Ειδοποίηση καθυστέρησης δεν είναι αποτυχία: το αίτημα μένει σταλμένο"""
        self.incoming(bounce(f"<initial-{self.rid}@warply.test>", "kotsovolos@partners.test", action="delayed"))
        self.assertEqual(self.status(self.rid), "sent")
        self.assertEqual(review.queue(self.con)["contact_problems"], [])

    def test_one_of_two_addresses_fails(self):
        """Αν αποτύχει μία από δύο διευθύνσεις, το αίτημα ισχύει και η κακή διεύθυνση φαίνεται ως πρόβλημα"""
        with self.con:
            self.con.execute(
                "UPDATE messages SET to_addrs='kotsovolos@partners.test, old@partners.test' WHERE request_id=?",
                (self.rid,),
            )
        self.incoming(bounce(f"<initial-{self.rid}@warply.test>", "old@partners.test"))
        self.assertEqual(self.status(self.rid), "sent")
        self.assertEqual(len(review.queue(self.con)["contact_problems"]), 1)
        review.dismiss_bounce(self.con, self.rid, mail._iso(day(2)))
        self.assertEqual(review.queue(self.con)["contact_problems"], [])

    def test_unrelated_bounce_ignored(self):
        """Ανεπίδοτο που δεν αφορά δικό μας email αγνοείται"""
        self.incoming(bounce("<something-else@other.example>", "nobody@other.example"))
        self.assertEqual(self.out["ignored"], 1)
        self.assertEqual(self.status(self.rid), "sent")

    def test_fix_address_and_send_again(self):
        """Διόρθωση διεύθυνσης και επαναποστολή: το ιστορικό μένει και οι υπενθυμίσεις ξεκινούν από την αρχή"""
        self.incoming(bounce(f"<initial-{self.rid}@warply.test>", "kotsovolos@partners.test"))
        with self.assertRaises(review.ReviewError):
            review.resend(self.con, self.cfg, self.rid, ["not-an-email"], day(2))
        with self.assertRaises(review.ReviewError):  # test mode: outside the allowlist
            review.resend(self.con, self.cfg, self.rid, ["someone@kotsovolos.gr"], day(2))
        with self.assertRaises(review.ReviewError):  # nobody got it, so it cannot just be dismissed
            review.dismiss_bounce(self.con, self.rid, mail._iso(day(2)))
        self.assertEqual(self.smtp.sent, [])

        review.resend(self.con, self.cfg, self.rid, ["Sales@partners.test"], day(2))
        self.assertEqual(self.smtp.sent[0]["To"], "sales@partners.test")
        self.assertEqual(self.status(self.rid), "sent")
        self.assertEqual(review.queue(self.con)["contact_problems"], [])
        kinds = [m["kind"] for m in review.thread(self.con, self.rid)["messages"]]
        self.assertEqual(kinds, ["initial", "bounce", "resend"])
        # Reminders count from the new email and are threaded under it.
        self.assertEqual(self.followups(day(8))["sent"], [])
        self.assertEqual(self.followups(day(9))["sent"], [("Kotsovolos", 1)])
        self.assertEqual(self.smtp.sent[1]["In-Reply-To"], self.smtp.sent[0]["Message-ID"])

    def test_server_refuses_recipient(self):
        """Αν ο mail server αρνηθεί τον παραλήπτη, το αίτημα σημειώνεται ανεπίδοτο και τα υπόλοιπα στέλνονται"""
        from agent import prepare_requests

        self.add_merchant("Avis", "avis@partners.test", [OFFER_C])
        prepare_requests.plan(self.con, PERIOD, PROGRAM)
        real_send = self.smtp.send_message

        def send(msg):
            if "avis" in msg["To"]:
                raise mail.smtplib.SMTPRecipientsRefused({"avis@partners.test": (550, b"No such user")})
            real_send(msg)

        self.smtp.send_message = send
        r = mail.send_requests(self.con, self.cfg, PERIOD, PROGRAM, day(0))
        self.assertEqual(r["refused"], {"Avis": ["avis@partners.test"]})
        status = self.con.execute(
            "SELECT r.status FROM requests r JOIN merchants m ON m.id=r.merchant_id WHERE m.name='Avis'"
        ).fetchone()[0]
        self.assertEqual(status, "undelivered")
        self.assertEqual([t["merchant"] for t in review.queue(self.con)["contact_problems"]], ["Avis"])

    def test_partner_without_email_is_listed(self):
        """Συνεργάτης χωρίς email φαίνεται ως εκκρεμότητα στο dashboard και στο report"""
        self.add_merchant("Avis", None, [OFFER_C])
        self.assertEqual(review.queue(self.con)["no_email"], [{"merchant": "Avis", "period": PERIOD, "offers": 1}])
        row = next(r for r in report.rows(self.con) if r["uuid"] == OFFER_C)
        self.assertEqual(row["status"], report.NO_EMAIL)


def reply_email(message_id, in_reply_to):
    """A partner's email with a number in the agreed format."""
    return message_from_string(
        f"""From: kotsovolos@partners.test
To: agent@warply.test
Subject: Re: Εξαργυρώσεις
Message-ID: {message_id}
In-Reply-To: {in_reply_to}

[aaaaaaaa] offer: 23
""",
        policy=policy.default,
    )


class MatchingTests(AgentTestCase):
    def test_reply_without_thread_is_not_guessed(self):
        """Email γνωστού συνεργάτη χωρίς σύνδεση με αίτημα πάει για έλεγχο, δεν αποδίδεται στο μόνο ανοιχτό αίτημα"""
        rid = self.sent_request("Kotsovolos", "kotsovolos@partners.test")
        out = {"new": [], "auto": [], "unmatched": [], "bounced": [], "ignored": 0}
        stray = reply_email("<stray@partners.test>", "<unknown@warply.test>")
        mail.store_incoming(self.con, self.cfg, stray, day(1), out)
        self.assertEqual(len(out["unmatched"]), 1)
        self.assertEqual(self.status(rid), "sent")
        self.assertEqual(self.redeemed(OFFER_A), (None, "pending"))
        self.assertEqual(len(review.queue(self.con)["unmatched"]), 1)

    def test_reply_in_thread_is_matched(self):
        """Απάντηση στο νήμα του αιτήματος αντιστοιχίζεται κανονικά"""
        rid = self.sent_request("Kotsovolos", "kotsovolos@partners.test")
        out = {"new": [], "auto": [], "unmatched": [], "bounced": [], "ignored": 0}
        reply = reply_email("<ok@partners.test>", f"<initial-{rid}@warply.test>")
        mail.store_incoming(self.con, self.cfg, reply, day(1), out)
        self.assertEqual(len(out["new"]), 1)
        self.assertEqual(self.status(rid), "replied")

    def test_no_request_for_offers_already_filled(self):
        """Προσφορές που έχουν ήδη τιμή (π.χ. από το παλιό report) δεν μπαίνουν σε νέο αίτημα"""
        from agent import prepare_requests

        self.add_merchant("Kotsovolos", "kotsovolos@partners.test", [OFFER_A, OFFER_B])
        self.add_merchant("Avis", "avis@partners.test", [OFFER_C])
        with self.con:
            self.con.execute("UPDATE redemptions SET redeemed=5, status='reported' WHERE offer_uuid IN (?, ?)", (OFFER_A, OFFER_C))
        drafts = {d["merchant"]: d for d in prepare_requests.plan(self.con, PERIOD, PROGRAM)["drafts"]}
        self.assertEqual(list(drafts), ["Kotsovolos"])
        self.assertEqual(drafts["Kotsovolos"]["offers"], 1)
        self.assertIn("offer bbbbbbbb", drafts["Kotsovolos"]["body"])
        self.assertNotIn("offer aaaaaaaa", drafts["Kotsovolos"]["body"])


class CorrectionTests(AgentTestCase):
    """Free-text replies go through the AI reader; here a stand-in returns
    what the model would."""

    def setUp(self):
        super().setUp()
        self.rid = self.sent_request("Kotsovolos", "kotsovolos@partners.test")
        self.cfg.gemini_key = "test"
        self.seen = []
        self._real = ai_reader.read_reply

        def fake(cfg, **kw):
            self.seen.append(kw)
            return self.reading

        ai_reader.read_reply = fake
        self.reply(self.rid, "[aaaaaaaa] offer: 12\n[bbbbbbbb] offer: 3")  # agreed format, no AI

    def tearDown(self):
        ai_reader.read_reply = self._real
        super().tearDown()

    def answer(self, value, is_correction):
        self.reading = SimpleNamespace(
            reply_type="partial", needs_review=False, aggregate_total=None, is_correction=is_correction,
            reason="δοκιμή", values=[SimpleNamespace(code="aaaaaaaa", redeemed=value, evidence="")],
        )  # fmt: skip
        return self.reply(self.rid, "ελεύθερο κείμενο", at=day(5))[0]

    def test_unclear_change_goes_to_review(self):
        """Νέος αριθμός χωρίς να λέει ότι είναι διόρθωση: η παλιά τιμή μένει και πάει για έλεγχο"""
        res = self.answer(5, is_correction=False)
        self.assertEqual(self.redeemed(OFFER_A), (12, "needs_review"))
        self.assertIn("καταχωρημένο 12, νέο 5", res["note"])
        self.assertEqual([t["id"] for t in review.queue(self.con)["requests"]], [self.rid])
        self.assertEqual(self.seen[0]["recorded"], {"aaaaaaaa": 12, "bbbbbbbb": 3})

    def test_explicit_correction_replaces(self):
        """Ρητή διόρθωση αντικαθιστά την τιμή και κρατά ιστορικό"""
        self.answer(15, is_correction=True)
        self.assertEqual(self.redeemed(OFFER_A), (15, "reported"))
        hist = [tuple(h) for h in self.con.execute(
            "SELECT old_value, new_value FROM redemption_history WHERE offer_uuid=? ORDER BY id", (OFFER_A,))]
        self.assertEqual(hist, [(None, 12), (12, 15)])

    def test_same_number_again_is_fine(self):
        """Ο ίδιος αριθμός ξανά δεν αλλάζει τίποτα και δεν θέλει έλεγχο"""
        self.answer(12, is_correction=False)
        self.assertEqual(self.redeemed(OFFER_A), (12, "reported"))
        self.assertEqual(review.queue(self.con)["requests"], [])


class WrongFileTests(AgentTestCase):
    def make_file(self, name, assignments):
        wb = Workbook()
        ws = wb.active
        ws.title = "generic"
        ws.append(HEADER)
        ws.append([OFFER_A, "generic_kotsovolos_10%", "Tech", assignments, 5, "http://x"])
        ws.append([OFFER_B, "generic_avis_5€", "Travel", 7, 7, "http://y"])
        path = self.dir / name
        wb.save(path)
        return path

    def test_month_not_over(self):
        """Αρχείο για μήνα που δεν έχει τελειώσει απορρίπτεται"""
        path = self.make_file("nov.xlsx", 25)
        with self.assertRaisesRegex(assignments.IngestError, "δεν έχει ολοκληρωθεί"):
            assignments.ingest(self.con, path, "2026-11", PROGRAM, today=date(2026, 11, 16))
        assignments.ingest(self.con, path, "2026-11", PROGRAM, today=date(2026, 12, 1))

    def test_copy_of_another_month(self):
        """Αρχείο πανομοιότυπο με άλλου μήνα απορρίπτεται· ο ίδιος μήνας ξαναφορτώνεται κανονικά"""
        today = date(2026, 12, 1)
        assignments.ingest(self.con, self.make_file("oct.xlsx", 25), "2026-10", PROGRAM, today=today)
        with self.assertRaisesRegex(assignments.IngestError, "2026-10"):
            assignments.ingest(self.con, self.make_file("nov.xlsx", 25), "2026-11", PROGRAM, today=today)
        self.assertEqual(self.con.execute("SELECT count(*) FROM assignments WHERE period='2026-11'").fetchone()[0], 0)
        assignments.ingest(self.con, self.make_file("oct.xlsx", 25), "2026-10", PROGRAM, today=today)   # same month again
        assignments.ingest(self.con, self.make_file("nov.xlsx", 31), "2026-11", PROGRAM, today=today)   # different numbers
        assignments.ingest(self.con, self.make_file("x.xlsx", 31), "2026-09", PROGRAM, today=today, force=True)


if __name__ == "__main__":
    unittest.main()
