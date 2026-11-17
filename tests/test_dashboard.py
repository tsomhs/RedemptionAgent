"""Report, δείκτες, εξαγωγή Excel και ουρά ελέγχου."""

import unittest
from io import BytesIO

from openpyxl import load_workbook

from base import OFFER_A, OFFER_B, OFFER_C, AgentTestCase, day
from agent import mail, prepare_requests, report, review

NOW = "2026-11-10 09:00:00"


class ReportTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.kot = self.sent_request("Kotsovolos", "kotsovolos@partners.test", (OFFER_A, OFFER_B))
        self.efood = self.sent_request("efood", "efood@partners.test", (OFFER_C,))
        self.reply(self.kot, "[aaaaaaaa] offer: 4\n[bbbbbbbb] offer: 0")

    def test_missing_answer_is_not_zero(self):
        """Στο report η προσφορά χωρίς απάντηση μένει κενή και φαίνεται ως εκκρεμότητα, όχι ως 0"""
        rows = {r["uuid"]: r for r in report.rows(self.con)}
        self.assertEqual((rows[OFFER_B]["redeemed"], rows[OFFER_B]["status"]), (0, report.REPORTED))
        self.assertEqual((rows[OFFER_C]["redeemed"], rows[OFFER_C]["status"]), (None, report.PENDING))
        self.assertIsNone(rows[OFFER_C]["rate"])
        self.assertEqual(rows[OFFER_A]["rate"], 0.4)

    def test_summary(self):
        """Οι δείκτες: σύνολο, κάλυψη, rate μόνο στις απαντημένες, ανταπόκριση αιτημάτων"""
        s = report.summary(report.rows(self.con))
        self.assertEqual((s["offers"], s["reported"], s["total_redeemed"]), (3, 2, 4))
        self.assertAlmostEqual(s["coverage"], 2 / 3)
        self.assertEqual(s["rate"], 4 / 20)
        self.assertEqual((s["months_with_data"], s["monthly_average"]), (1, 4))
        self.assertEqual(s["monthly"][0]["open"], 1)
        self.assertEqual(s["top_merchants"], [{"name": "Kotsovolos", "redeemed": 4}])
        r = s["requests"]
        self.assertEqual((r["sent"], r["completed"], r["pending"], r["response_rate"]), (2, 1, 1, 0.5))

    def test_filters(self):
        """Τα φίλτρα συνεργάτη και κατάστασης περιορίζουν γραμμές και σύνολα μαζί"""
        rows = report.rows(self.con, {"merchant": "efood"})
        self.assertEqual([r["uuid"] for r in rows], [OFFER_C])
        self.assertEqual(report.summary(rows)["total_redeemed"], 0)
        pending = report.rows(self.con, {"status": report.PENDING})
        self.assertEqual([r["merchant"] for r in pending], ["efood"])
        self.assertEqual(report.rows(self.con, {"period_from": "2027-01"}), [])

    def test_excel_matches_dashboard(self):
        """Το Excel έχει τις ίδιες γραμμές και τα ίδια σύνολα με το dashboard"""
        f = {"merchant": "Kotsovolos"}
        rows = report.rows(self.con, f)
        s = report.summary(rows)
        wb = load_workbook(BytesIO(report.export_xlsx(rows, s, f)))
        data = list(wb["Αναλυτικά"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(data), len(rows))
        self.assertEqual(sum(r[7] for r in data if r[7] is not None), s["total_redeemed"])
        self.assertEqual(sum(r[6] for r in data), s["total_assigned"])
        summary = {r[0]: r[1] for r in wb["Σύνοψη"].iter_rows(values_only=True) if r[0]}
        self.assertEqual(summary["Σύνολο εξαργυρώσεων"], s["total_redeemed"])
        self.assertEqual(summary["Συνεργάτης"], "Kotsovolos")
        # Offer rows only: no totals row to be summed twice.
        self.assertNotIn("Σύνολο", [str(r[0]) for r in data])


    def test_excel_month_sheet_like_the_teams(self):
        """Το Excel έχει φύλλο ανά μήνα όπως της ομάδας: ομάδες, ταξινόμηση κατά Assigned, υποσύνολα"""
        with self.con:
            self.con.execute("UPDATE offers SET category='Μόδα' WHERE uuid IN (?, ?)", (OFFER_A, OFFER_B))
            self.con.execute("UPDATE offers SET category='Σπίτι', segment='Student' WHERE uuid=?", (OFFER_C,))
            self.con.execute("UPDATE assignments SET assignments=25 WHERE offer_uuid=?", (OFFER_B,))
        rows = report.rows(self.con)
        wb = load_workbook(BytesIO(report.export_xlsx(rows, report.summary(rows))))
        self.assertEqual(wb.sheetnames, ["October 2026", "Σύνοψη", "Αναλυτικά"])
        ws = wb["October 2026"]
        got = [[c.value for c in row[:5]] for row in ws.iter_rows(min_row=1, max_row=7)]
        self.assertEqual(got, [
            ["01/10 - 31/10", None, None, None, None],
            ["Category", "Offer", "Assigned", "Redeemed", "Redemption Rate"],
            ["Μόδα", "offer bbbbbbbb", 25, 0, "=D3/C3"],           # larger Assigned first
            [None, "offer aaaaaaaa", 10, 4, "=D4/C4"],
            [None, None, "=SUM(C3:C4)", '=IF(COUNT(D3:D4)=0,"",SUM(D3:D4))', '=IF(COUNT(D3:D4)=0,"",SUM(D3:D4)/SUMIF(D3:D4,"<>",C3:C4))'],
            ["Student", "offer cccccccc", 10, None, None],         # segment label; no answer stays empty
            [None, None, "=SUM(C6:C6)", '=IF(COUNT(D6:D6)=0,"",SUM(D6:D6))', '=IF(COUNT(D6:D6)=0,"",SUM(D6:D6)/SUMIF(D6:D6,"<>",C6:C6))'],
        ])  # fmt: skip
        self.assertEqual(ws["G6"].value, report.PENDING)


class ReviewTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.rid = self.sent_request("affidea", "affidea@partners.test", (OFFER_A, OFFER_B))
        self.reply(self.rid, "Συνολικά είχαμε 48 εξαργυρώσεις, χωρίς ανάλυση.")

    def queue_ids(self):
        return [t["id"] for t in review.queue(self.con)["requests"]]

    def test_unreadable_reply_is_queued(self):
        """Απάντηση που δεν καταχωρήθηκε εμφανίζεται στην ουρά με τον λόγο και το κείμενό της"""
        (item,) = review.queue(self.con)["requests"]
        self.assertEqual(item["merchant"], "affidea")
        self.assertIn("θέλει έλεγχο", item["reason"])
        self.assertIn("48", item["messages"][-1]["body"])

    def test_manual_values(self):
        """Τιμές που βάζει ο χρήστης καταχωρούνται με ιστορικό και κλείνουν το αίτημα"""
        r = review.set_values(self.con, self.rid, {OFFER_A: 30}, NOW)
        self.assertEqual(r["status"], "partial")
        self.assertEqual(self.redeemed(OFFER_B), (None, "pending"))
        r = review.set_values(self.con, self.rid, {OFFER_B: 18}, NOW)
        self.assertEqual(r["status"], "completed")
        self.assertEqual(self.queue_ids(), [])
        hist = [tuple(h) for h in self.con.execute("SELECT old_value, new_value, source FROM redemption_history ORDER BY id")]
        self.assertEqual(hist, [(None, 30, "manual: χειροκίνητα"), (None, 18, "manual: χειροκίνητα")])

    def test_bad_values_rejected(self):
        """Αρνητικός αριθμός, κείμενο ή προσφορά άλλου αιτήματος απορρίπτονται χωρίς να γραφτεί τίποτα"""
        for bad in ({OFFER_A: -1}, {OFFER_A: "12"}, {OFFER_A: 1.5}, {OFFER_C: 3}, {}):
            with self.assertRaises(review.ReviewError):
                review.set_values(self.con, self.rid, bad, NOW)
        self.assertEqual(self.redeemed(OFFER_A), (None, "pending"))

    def test_clarification(self):
        """Η διευκρίνιση φεύγει στο ίδιο νήμα, ζητά τις προσφορές που λείπουν και το αίτημα ξαναπεριμένει"""
        text = review.clarification_text(self.con, self.rid, prepare_requests.period_label)
        self.assertIn("[aaaaaaaa]", text)
        self.assertIn("[bbbbbbbb]", text)
        review.send_clarification(self.con, self.cfg, self.rid, text, day(4))
        msg = self.smtp.sent[0]
        self.assertEqual(msg["In-Reply-To"], f"<initial-{self.rid}@warply.test>")
        self.assertEqual(self.status(self.rid), "sent")
        self.assertEqual(self.queue_ids(), [])
        # The generic reminder waits a full week after the clarification.
        self.assertEqual(self.followups(day(8))["sent"], [])
        self.assertEqual(self.followups(day(11))["sent"], [("affidea", 1)])

    def test_clarification_respects_allowlist(self):
        """Σε δοκιμαστική λειτουργία η διευκρίνιση δεν φεύγει σε πραγματική διεύθυνση"""
        rid = self.sent_request("Avis", "someone@avis.gr", (OFFER_C,))
        self.reply(rid, "Δεν είμαι σίγουρος.")
        with self.assertRaises(review.ReviewError):
            review.send_clarification(self.con, self.cfg, rid, "κείμενο", day(4))
        self.assertEqual(self.smtp.sent, [])

    def test_unmatched_message(self):
        """Email χωρίς αντιστοίχιση: αντιστοιχίζεται σε αίτημα και διαβάζεται, ή παραμερίζεται"""
        with self.con:
            for n in (1, 2):
                self.con.execute(
                    """INSERT INTO messages (direction, kind, message_id, from_addr, subject, body, attachments, at)
                       VALUES ('in', 'reply', ?, 'affidea@partners.test', 'στοιχεία', ?, '[]', ?)""",
                    (f"<stray-{n}@x>", "[aaaaaaaa] offer: 30\n[bbbbbbbb] offer: 18", mail._iso(day(5))),
                )
        a, b = [m["id"] for m in review.queue(self.con)["unmatched"]]
        review.assign_message(self.con, a, self.rid, NOW, self.cfg)
        self.assertEqual((self.redeemed(OFFER_A)[0], self.redeemed(OFFER_B)[0]), (30, 18))
        self.assertEqual(self.status(self.rid), "completed")
        review.ignore_message(self.con, b)
        self.assertEqual(review.queue(self.con)["unmatched"], [])
        self.assertEqual(self.con.execute("SELECT count(*) FROM messages WHERE message_id='<stray-2@x>'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
