"""Η δοκιμαστική βάση που βλέπει όποιος κατεβάζει το project."""

import unittest

from base import AgentTestCase
from agent import demo, report, review


class DemoTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.period = demo.build(self.con, self.cfg)

    def test_every_state_is_there(self):
        """Η δοκιμαστική βάση έχει αίτημα σε κάθε κατάσταση και γεμάτη ουρά ελέγχου"""
        states = dict(self.con.execute("SELECT status, count(*) FROM requests GROUP BY status").fetchall())
        self.assertEqual(states, {"completed": 1, "partial": 2, "replied": 1, "sent": 1, "undelivered": 1, "draft": 1})
        q = review.queue(self.con)
        self.assertEqual(sorted(t["merchant"] for t in q["requests"]), ["Beta Sport", "Gamma Home"])
        self.assertEqual([t["merchant"] for t in q["contact_problems"]], ["Epsilon Books"])
        self.assertEqual(len(q["unmatched"]), 1)
        self.assertEqual([x["merchant"] for x in q["no_email"]], ["Eta Garden"])

    def test_numbers_look_like_a_real_year(self):
        """Έχει επτά μήνες: έξι με στοιχεία και έναν σε εξέλιξη, χωρίς ψεύτικα μηδενικά"""
        s = report.summary(report.rows(self.con))
        self.assertEqual(len(s["monthly"]), 7)
        self.assertEqual(s["monthly"][-1]["period"], self.period)
        self.assertGreater(s["monthly"][-1]["open"], 0)
        self.assertTrue(all(m["redeemed"] > 0 for m in s["monthly"][:-1]))
        self.assertLess(s["coverage"], 1)

    def test_nothing_can_be_sent(self):
        """Όλες οι διευθύνσεις είναι example.test και τίποτα δεν στάλθηκε πραγματικά"""
        emails = [r[0] for r in self.con.execute("SELECT email FROM merchant_contacts")]
        self.assertTrue(emails and all(e.endswith(".example.test") for e in emails))
        self.assertEqual(self.smtp.sent, [])

    def test_built_once(self):
        """Η δοκιμαστική βάση φτιάχνεται μόνο σε άδεια βάση"""
        self.assertFalse(demo.is_empty(self.con))


if __name__ == "__main__":
    unittest.main()
