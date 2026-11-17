"""Περιπτώσεις που βρήκε ο έλεγχος του κώδικα: τίποτα δεν χάνεται και τίποτα δεν κολλάει."""

import unittest
from datetime import timedelta
from email import message_from_bytes, message_from_string, policy

from openpyxl import Workbook

from base import OFFER_A, OFFER_B, OFFER_C, PERIOD, AgentTestCase, day
from agent import automation, mail, prepare_requests, replies, report, review, settings
from test_automation import athens

OUT = lambda: {"new": [], "auto": [], "unmatched": [], "bounced": [], "ignored": 0}


class UnclearReplyTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.rid = self.sent_request("Kotsovolos", "kotsovolos@partners.test")

    def queue(self):
        return [t["id"] for t in review.queue(self.con)["requests"]]

    def test_unclear_reply_after_a_partial_one_is_reviewed(self):
        """Ασαφής απάντηση σε αίτημα που έχει ήδη μερικά στοιχεία πάει κι αυτή για έλεγχο"""
        self.reply(self.rid, "[aaaaaaaa] offer: 5")
        self.assertEqual(self.queue(), [])
        self.reply(self.rid, "Το πρώτο ήταν λάθος, συνολικά και για τα δύο είναι 40.", at=day(4))
        self.assertEqual(self.status(self.rid), "partial")
        self.assertEqual(self.queue(), [self.rid])
        self.assertIn("συνολικά", review.queue(self.con)["requests"][0]["messages"][-1]["body"])
        self.assertEqual(self.redeemed(OFFER_A), (5, "reported"))

    def test_unclear_reply_after_completion_is_reviewed(self):
        """Ασαφές μήνυμα μετά την ολοκλήρωση δεν χάνεται: εμφανίζεται για έλεγχο"""
        self.reply(self.rid, "[aaaaaaaa] offer: 5\n[bbbbbbbb] offer: 7")
        self.reply(self.rid, "Διόρθωση: είναι 9 και όχι 7.", at=day(4))
        self.assertEqual(self.status(self.rid), "completed")
        self.assertEqual(self.queue(), [self.rid])
        review.set_values(self.con, self.rid, {OFFER_B: 9}, mail._iso(day(5)))
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.redeemed(OFFER_B), (9, "reported"))

    def test_not_an_answer_keeps_waiting(self):
        """«Θα σας στείλουμε»: ο χρήστης το σημειώνει ως μη απάντηση και οι υπενθυμίσεις συνεχίζουν"""
        self.reply(self.rid, "Καλησπέρα, θα σας τα στείλουμε την άλλη εβδομάδα.")
        self.assertEqual(self.status(self.rid), "replied")
        self.assertEqual(self.followups(day(30))["sent"], [])
        self.assertEqual(review.keep_waiting(self.con, self.rid, mail._iso(day(4)))["status"], "sent")
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.followups(day(30))["sent"], [("Kotsovolos", 1)])

    def test_keep_waiting_on_partial(self):
        """«Δεν χρειάζεται ενέργεια» σε μερικό αίτημα το αφήνει μερικό, με τις τιμές του"""
        self.reply(self.rid, "[aaaaaaaa] offer: 5")
        self.reply(self.rid, "Ευχαριστούμε!", at=day(4))
        self.assertEqual(review.keep_waiting(self.con, self.rid, mail._iso(day(5)))["status"], "partial")
        self.assertEqual(self.redeemed(OFFER_A), (5, "reported"))

    def test_clarification_on_partial_stays_partial(self):
        """Διευκρίνιση σε μερικό αίτημα: η επόμενη υπενθύμιση ζητά μόνο ό,τι λείπει"""
        self.reply(self.rid, "[aaaaaaaa] offer: 5")
        self.reply(self.rid, "Το άλλο είναι περίπου 10.", at=day(4))
        review.send_clarification(self.con, self.cfg, self.rid, "Ποιος είναι ο ακριβής αριθμός;", day(5))
        self.assertEqual(self.status(self.rid), "partial")
        self.followups(day(13))
        text = self.smtp.sent[-1].get_content()
        self.assertIn("[bbbbbbbb]", text)
        self.assertNotIn("[aaaaaaaa]", text)

    def test_full_stop_after_the_number(self):
        """Τελεία μετά τον αριθμό δεν χαλάει τη συμφωνημένη μορφή· το 1.200 δεν διαβάζεται ως 200"""
        self.assertEqual(replies.parse("[aaaaaaaa] offer: 12.")[0], {"aaaaaaaa": 12})
        self.assertEqual(replies.parse("[aaaaaaaa] offer: 1.200")[0], {})
        self.assertEqual(replies.parse("[aaaaaaaa] offer: 12,5")[0], {})


class IncomingMailTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.rid = self.sent_request("Kotsovolos", "kotsovolos@partners.test")
        self._root = mail.ROOT
        mail.ROOT = self.dir

    def tearDown(self):
        mail.ROOT = self._root
        super().tearDown()

    def raw(self, extra_headers="", body="[aaaaaaaa] offer: 3"):
        return (f"From: kotsovolos@partners.test\nTo: agent@warply.test\nSubject: Re: x\n"
                f"In-Reply-To: <initial-{self.rid}@warply.test>\n{extra_headers}\n{body}\n")  # fmt: skip

    def test_no_message_id_is_stored_once(self):
        """Email χωρίς Message-ID αποθηκεύεται μία φορά, όσες φορές κι αν διαβαστεί το mailbox"""
        for _ in range(3):
            mail.store_incoming(self.con, self.cfg, message_from_string(self.raw(), policy=policy.default), day(1), OUT())
        self.assertEqual(self.con.execute("SELECT count(*) FROM messages WHERE direction='in'").fetchone()[0], 1)

    def test_unknown_charset_does_not_crash(self):
        """Email με άγνωστη κωδικοποίηση χαρακτήρων διαβάζεται όσο γίνεται, χωρίς σφάλμα"""
        msg = message_from_bytes(
            self.raw("Message-ID: <c@x>\nContent-Type: text/plain; charset=x-unknown-9\n").encode(), policy=policy.default
        )
        mail.store_incoming(self.con, self.cfg, msg, day(1), OUT())
        replies.process(self.con, mail._iso(day(1)), self.cfg)
        self.assertEqual(self.redeemed(OFFER_A), (3, "reported"))

    def attachment_email(self, names):
        from email.message import EmailMessage

        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = "kotsovolos@partners.test", "agent@warply.test", "Re: x"
        m["Message-ID"], m["In-Reply-To"] = "<att@x>", f"<initial-{self.rid}@warply.test>"
        m.set_content("συνημμένα")
        for i, name in enumerate(names):
            m.add_attachment(f"file {i}".encode(), maintype="application", subtype="octet-stream", filename=name)
        return message_from_bytes(m.as_bytes(), policy=policy.default)

    def test_same_named_attachments_both_survive(self):
        """Δύο συνημμένα με το ίδιο όνομα αποθηκεύονται και τα δύο"""
        mail.store_incoming(self.con, self.cfg, self.attachment_email(["report.xlsx", "report.xlsx"]), day(1), OUT())
        import json

        paths = json.loads(self.con.execute("SELECT attachments FROM messages WHERE message_id='<att@x>'").fetchone()[0])
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual(sorted((self.dir / p).read_bytes() for p in paths), [b"file 0", b"file 1"])

    def test_very_long_attachment_name(self):
        """Συνημμένο με πολύ μακρύ όνομα αποθηκεύεται με συντομευμένο όνομα και την κατάληξή του"""
        mail.store_incoming(self.con, self.cfg, self.attachment_email(["Εξαργυρώσεις " * 30 + ".xlsx"]), day(1), OUT())
        import json

        (path,) = json.loads(self.con.execute("SELECT attachments FROM messages WHERE message_id='<att@x>'").fetchone()[0])
        self.assertTrue(path.endswith(".xlsx"))
        self.assertTrue((self.dir / path).is_file())

    def test_unreadable_email_is_kept_for_a_person(self):
        """Email που δεν μπορεί να διαβαστεί κρατιέται για έλεγχο και δεν ξαναδοκιμάζεται"""
        msg = message_from_string(self.raw("Message-ID: <bad@x>\n"), policy=policy.default)
        for _ in range(2):
            mail._store_unreadable(self.con, msg, "<bad@x>", ValueError("χαλασμένο"), day(1))
        (item,) = review.queue(self.con)["unmatched"]
        self.assertIn("δεν μπόρεσε να διαβαστεί", item["body"])
        review.ignore_message(self.con, item["id"])
        self.assertEqual(review.queue(self.con)["unmatched"], [])


class SendingTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.sent_request("Avis", "avis@partners.test", (OFFER_A,))
        self.k = self.sent_request("Kotsovolos", "kotsovolos@partners.test", (OFFER_B,))

    def test_refused_reminder_does_not_stop_the_others(self):
        """Αν ο server αρνηθεί έναν παραλήπτη υπενθύμισης, γίνεται πρόβλημα επαφής και οι άλλοι την παίρνουν"""
        real = self.smtp.send_message

        def send(msg):
            if "avis" in msg["To"]:
                raise mail.smtplib.SMTPRecipientsRefused({"avis@partners.test": (550, b"No such user")})
            real(msg)

        self.smtp.send_message = send
        r = self.followups(day(7))
        self.assertEqual(r["sent"], [("Kotsovolos", 1)])
        self.assertEqual(self.status(self.a), "undelivered")
        self.assertEqual([t["merchant"] for t in review.queue(self.con)["contact_problems"]], ["Avis"])

    def test_partly_refused_recipients_are_noted(self):
        """Αν ο server δεχτεί μόνο κάποιους παραλήπτες, οι υπόλοιποι σημειώνονται ως πρόβλημα επαφής"""
        self.smtp.send_message = lambda msg: {"old@partners.test": (550, b"gone")}
        self.followups(day(7))
        self.assertEqual(self.status(self.k), "sent")
        failed = {a for t in review.queue(self.con)["contact_problems"] for a in t["failed"]}
        self.assertEqual(failed, {"old@partners.test"})

    def test_only_one_sender_at_a_time(self):
        """Δύο αποστολές ταυτόχρονα δεν γίνονται: η δεύτερη αρνείται, και το κλείδωμα λήγει αν μείνει πίσω"""
        with mail.sending(self.con, day(7)):
            with self.assertRaises(mail.MailError):
                self.followups(day(7))
            self.assertEqual(self.smtp.sent, [])
        self.assertEqual(len(self.followups(day(7))["sent"]), 2)          # released afterwards
        self.con.execute("INSERT INTO automation_state VALUES ('lock.send', ?)", (mail._iso(day(14) - timedelta(hours=1)),))
        self.con.commit()
        self.assertEqual(len(self.followups(day(14))["sent"]), 2)         # a stale lock does not block for ever


class SchedulerRobustnessTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self._saved = (automation.INPUT_DIR, automation.PREVIEW_DIR, mail.fetch_replies)
        automation.INPUT_DIR, automation.PREVIEW_DIR = self.dir / "input", self.dir / "preview"
        automation.INPUT_DIR.mkdir()
        self.fetches = 0

        def fetch(con, cfg, now):
            self.fetches += 1
            return OUT()

        mail.fetch_replies = fetch
        settings.save(self.con, self.cfg, {"automation_enabled": True, "require_approval": False})

    def tearDown(self):
        automation.INPUT_DIR, automation.PREVIEW_DIR, mail.fetch_replies = self._saved
        super().tearDown()

    def log(self, job):
        return [r[0] for r in self.con.execute("SELECT message FROM runs WHERE job=? ORDER BY id", (job,))]

    def due_reminder(self):
        rid = self.sent_request("efood", "efood@partners.test", (OFFER_C,))
        with self.con:
            self.con.execute("UPDATE requests SET sent_at='2026-11-20 08:00:00' WHERE id=?", (rid,))
        return rid

    def test_no_reminders_when_the_mailbox_cannot_be_read(self):
        """Αν δεν διαβάζεται το mailbox, δεν φεύγει υπενθύμιση: μπορεί ο συνεργάτης να έχει ήδη απαντήσει"""
        self.due_reminder()

        def broken(con, cfg, now):
            raise OSError("δεν υπάρχει σύνδεση")

        mail.fetch_replies = broken
        automation.tick(self.con, self.cfg, athens(2026, 11, 30, 10, 0))
        self.assertEqual(self.smtp.sent, [])
        mail.fetch_replies = lambda con, cfg, now: OUT()
        automation.tick(self.con, self.cfg, athens(2026, 11, 30, 10, 1))    # still inside the retry interval
        self.assertEqual(self.smtp.sent, [])
        automation.tick(self.con, self.cfg, athens(2026, 11, 30, 10, 45))
        self.assertEqual(len(self.smtp.sent), 1)

    def test_mailbox_is_read_before_reminders(self):
        """Πριν από τις υπενθυμίσεις διαβάζεται πάντα το mailbox, ακόμη κι αν ο τακτικός έλεγχος είναι αραιός"""
        self.due_reminder()
        settings.save(self.con, self.cfg, {"poll_minutes": 1440})
        automation.tick(self.con, self.cfg, athens(2026, 11, 30, 8, 0))     # the day's regular read
        self.assertEqual((self.fetches, self.smtp.sent), (1, []))
        automation.tick(self.con, self.cfg, athens(2026, 11, 30, 10, 0))
        self.assertEqual((self.fetches, len(self.smtp.sent)), (2, 1))

    def test_broken_file_does_not_stop_everything(self):
        """Χαλασμένο αρχείο στον φάκελο input καταγράφεται και οι υπόλοιπες εργασίες συνεχίζουν"""
        self.due_reminder()
        (automation.INPUT_DIR / "assignments_2026-11.xlsx").write_bytes(b"not an excel file")
        automation.tick(self.con, self.cfg, athens(2026, 12, 1, 10, 30))
        self.assertIn("δεν διαβάζεται ως Excel", self.log("cycle")[0])
        self.assertEqual(self.fetches, 1)
        self.assertEqual(len(self.smtp.sent), 1)

    def test_partner_given_an_email_later_still_gets_the_request(self):
        """Συνεργάτης που αποκτά email αργότερα μέσα στον μήνα παίρνει το αίτημά του"""
        wb = Workbook()
        ws = wb.active
        ws.append(["UUID", "Admin Name", "Category", "Assignments", "Unique Users", "Engage URL"])
        ws.append([OFFER_A, "offer a", "Tech", 25, 20, "http://x"])
        wb.save(automation.INPUT_DIR / "assignments_2026-11.xlsx")
        mid = self.add_merchant("Avis", None, [OFFER_A])
        automation.tick(self.con, self.cfg, athens(2026, 12, 1, 9, 0))
        self.assertEqual(self.smtp.sent, [])
        with self.con:
            self.con.execute("INSERT INTO merchant_contacts VALUES (?, 'avis@partners.test')", (mid,))
        automation.tick(self.con, self.cfg, athens(2026, 12, 3, 9, 0))
        self.assertEqual([m["To"] for m in self.smtp.sent], ["avis@partners.test"])
        automation.tick(self.con, self.cfg, athens(2026, 12, 4, 9, 0))
        self.assertEqual(len(self.smtp.sent), 1)

    def test_unsent_requests_of_a_closed_month_are_reported(self):
        """Αιτήματα που έμειναν πρόχειρα και ο μήνας τους έκλεισε αναφέρονται, δεν χάνονται σιωπηλά"""
        self.add_merchant("Avis", "avis@partners.test", [OFFER_A])
        prepare_requests.plan(self.con, PERIOD, "ΔΕΗ myRewards Coupons")       # October drafts, never sent
        automation.tick(self.con, self.cfg, athens(2026, 12, 1, 8, 0))         # now the cycle is about November
        self.assertIn("δεν στάλθηκαν ποτέ", self.log("cycle")[0])

    def test_offer_added_after_the_request_is_listed(self):
        """Προσφορά που δόθηκε σε συνεργάτη αφού έφυγε το email του αναφέρεται ως εκτός αιτήματος"""
        self.sent_request("Kotsovolos", "kotsovolos@partners.test", (OFFER_A,))
        mid = self.con.execute("SELECT id FROM merchants WHERE name='Kotsovolos'").fetchone()[0]
        with self.con:
            self.con.execute(
                "INSERT INTO offers (uuid, program, segment, admin_name, merchant_id) VALUES (?, ?, 'generic', 'late offer', ?)",
                (OFFER_B, "ΔΕΗ myRewards Coupons", mid),
            )
            self.con.execute("INSERT INTO assignments (offer_uuid, period, assignments) VALUES (?, ?, 4)", (OFFER_B, PERIOD))
        r = prepare_requests.plan(self.con, PERIOD, "ΔΕΗ myRewards Coupons")
        self.assertEqual(r["not_covered"], [("Kotsovolos", "late offer")])


class ReportTests(AgentTestCase):
    def test_month_without_answers_is_empty_not_zero(self):
        """Μήνας χωρίς καμία απάντηση δείχνει κενό στις εξαργυρώσεις, όχι 0"""
        self.sent_request("Kotsovolos", "kotsovolos@partners.test")
        (month,) = report.summary(report.rows(self.con))["monthly"]
        self.assertIsNone(month["redeemed"])
        self.assertEqual(month["open"], 2)


if __name__ == "__main__":
    unittest.main()
