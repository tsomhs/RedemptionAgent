"""Υπενθυμίσεις: πότε φεύγουν, πόσες, και πότε σταματούν."""

import unittest

from base import PERIOD, AgentTestCase, day
from agent import prepare_requests


class FollowupTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.rid = self.sent_request("Kotsovolos", "kotsovolos@partners.test")

    def test_schedule(self):
        """Υπενθυμίσεις στις 7, 14 και 21 ημέρες, και μετά καμία άλλη"""
        self.assertEqual(self.followups(day(6))["sent"], [])
        self.assertEqual(self.followups(day(7))["sent"], [("Kotsovolos", 1)])
        self.assertEqual(self.followups(day(7, hour=15))["sent"], [], "δεύτερη εκτέλεση την ίδια μέρα")
        self.assertEqual(self.followups(day(13))["sent"], [])
        self.assertEqual(self.followups(day(14))["sent"], [("Kotsovolos", 2)])
        self.assertEqual(self.followups(day(21))["sent"], [("Kotsovolos", 3)])
        late = self.followups(day(60))
        self.assertEqual(late["sent"], [])
        self.assertEqual(late["exhausted"], ["Kotsovolos"])
        self.assertEqual(len(self.smtp.sent), 3)

    def test_missed_runs(self):
        """Αν ο agent έμεινε κλειστός έναν μήνα, δεν στέλνει τις χαμένες υπενθυμίσεις μαζεμένες"""
        self.assertEqual(self.followups(day(30))["sent"], [("Kotsovolos", 1)])
        self.assertEqual(self.followups(day(30, hour=10))["sent"], [])
        self.assertEqual(self.followups(day(31))["sent"], [])
        self.assertEqual(self.followups(day(37))["sent"], [("Kotsovolos", 2)])

    def test_same_thread(self):
        """Η υπενθύμιση μπαίνει στο ίδιο νήμα με το αρχικό email και αναφέρει τον μήνα"""
        self.followups(day(7))
        msg = self.smtp.sent[0]
        self.assertEqual(msg["In-Reply-To"], f"<initial-{self.rid}@warply.test>")
        self.assertEqual(msg["Subject"], "Re: Εξαργυρώσεις")
        self.assertEqual(msg["To"], "kotsovolos@partners.test")
        self.assertIn(prepare_requests.period_label(PERIOD), msg.get_content())
        kinds = [r[0] for r in self.con.execute("SELECT kind FROM messages ORDER BY id")]
        self.assertEqual(kinds, ["initial", "followup"])

    def test_complete_reply_stops(self):
        """Πλήρης απάντηση σταματά τις υπενθυμίσεις"""
        self.reply(self.rid, "[aaaaaaaa] offer: 4\n[bbbbbbbb] offer: 0")
        self.assertEqual(self.status(self.rid), "completed")
        self.assertEqual(self.followups(day(30))["sent"], [])

    def test_partial_reply_reminds_for_the_rest(self):
        """Μερική απάντηση: η υπενθύμιση ζητά μόνο τις προσφορές που λείπουν, μια εβδομάδα μετά την απάντηση"""
        self.reply(self.rid, "[aaaaaaaa] offer: 4")          # arrives on day 3
        self.assertEqual(self.status(self.rid), "partial")
        self.assertEqual(self.followups(day(7))["sent"], [], "όχι την επομένη της απάντησης")
        self.assertEqual(self.followups(day(10))["sent"], [("Kotsovolos", 1)])
        text = self.smtp.sent[0].get_content()
        self.assertIn("[bbbbbbbb]", text)
        self.assertNotIn("[aaaaaaaa]", text)
        self.assertIn("μας λείπουν ακόμη", text)
        # Once the rest arrives the request is complete and nothing more is sent.
        self.reply(self.rid, "[bbbbbbbb] offer: 2", at=day(11))
        self.assertEqual(self.status(self.rid), "completed")
        self.assertEqual(self.followups(day(40))["sent"], [])

    def test_unreadable_reply_waits(self):
        """Απάντηση που δεν διαβάζεται περιμένει άνθρωπο, δεν ξαναενοχλεί τον συνεργάτη"""
        self.reply(self.rid, "Καλησπέρα, είχαμε περίπου 40 εξαργυρώσεις συνολικά.")
        self.assertEqual(self.status(self.rid), "replied")
        self.assertEqual(self.followups(day(30))["sent"], [])

    def test_allowlist(self):
        """Σε δοκιμαστική λειτουργία δεν φεύγει υπενθύμιση σε πραγματική διεύθυνση"""
        self.sent_request("Avis", "someone@avis.gr", uuids=("cccccccc-0000-0000-0000-000000000003",))
        r = self.followups(day(7))
        self.assertEqual(r["sent"], [("Kotsovolos", 1)])
        self.assertEqual(r["blocked"], {"Avis": ["someone@avis.gr"]})
        self.assertEqual([m["To"] for m in self.smtp.sent], ["kotsovolos@partners.test"])


if __name__ == "__main__":
    unittest.main()
