"""Ανάγνωση απαντήσεων στη συμφωνημένη μορφή και καταχώριση εξαργυρώσεων."""

import unittest

from base import OFFER_A, OFFER_B, AgentTestCase, day
from agent import replies


class ParseTests(unittest.TestCase):
    def test_formats(self):
        """Διαβάζει «[κωδικός] όνομα: 12» και «[κωδικός] = 0», αγνοεί το υπόλοιπο κείμενο"""
        values, conflicts = replies.parse(
            "Καλησπέρα,\n\n[AAAAAAAA] Student_24home.gr_10€_31-10-2026: 12\n[bbbbbbbb] = 0\n\nΜε εκτίμηση"
        )
        self.assertEqual(values, {"aaaaaaaa": 12, "bbbbbbbb": 0})
        self.assertEqual(conflicts, set())

    def test_no_number_no_value(self):
        """Γραμμή χωρίς αριθμό στο τέλος δεν δίνει τιμή"""
        values, _ = replies.parse("[aaaaaaaa] Student_24home.gr_10€_31-10-2026\n[bbbbbbbb] offer: περίπου 5")
        self.assertEqual(values, {})

    def test_typed_wins_over_quoted(self):
        """Η διόρθωση που γράφει ο συνεργάτης υπερισχύει της παλιάς τιμής στο παρατιθέμενο κείμενο"""
        values, conflicts = replies.parse("[aaaaaaaa] offer: 15\n\n> [aaaaaaaa] offer: 12\n> [bbbbbbbb] offer: 3")
        self.assertEqual(values, {"aaaaaaaa": 15, "bbbbbbbb": 3})
        self.assertEqual(conflicts, set())

    def test_two_different_numbers(self):
        """Δύο διαφορετικοί αριθμοί για την ίδια προσφορά είναι αντίφαση, όχι τιμή"""
        values, conflicts = replies.parse("[aaaaaaaa] offer: 12\n[aaaaaaaa] offer: 14")
        self.assertEqual(values, {})
        self.assertEqual(conflicts, {"aaaaaaaa"})


class ProcessTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.rid = self.sent_request("Kotsovolos", "kotsovolos@partners.test")

    def history(self):
        return [tuple(r) for r in self.con.execute("SELECT old_value, new_value FROM redemption_history ORDER BY id")]

    def test_complete(self):
        """Πλήρης απάντηση: καταχωρούνται οι αριθμοί και το αίτημα ολοκληρώνεται"""
        self.reply(self.rid, "[aaaaaaaa] offer: 4\n[bbbbbbbb] offer: 7")
        self.assertEqual(self.redeemed(OFFER_A), (4, "reported"))
        self.assertEqual(self.redeemed(OFFER_B), (7, "reported"))
        self.assertEqual(self.status(self.rid), "completed")

    def test_zero_is_not_missing(self):
        """Το 0 που δηλώνει ο συνεργάτης καταχωρείται ως 0· η σιωπή μένει κενή"""
        self.reply(self.rid, "[aaaaaaaa] offer: 0")
        self.assertEqual(self.redeemed(OFFER_A), (0, "reported"))
        self.assertEqual(self.redeemed(OFFER_B), (None, "pending"))
        self.assertEqual(self.status(self.rid), "partial")

    def test_correction_replaces(self):
        """Δεύτερη απάντηση διορθώνει την τιμή (δεν προστίθεται) και κρατιέται ιστορικό"""
        self.reply(self.rid, "[aaaaaaaa] offer: 4\n[bbbbbbbb] offer: 7")
        self.reply(self.rid, "Διόρθωση:\n[aaaaaaaa] offer: 6", at=day(4))
        self.assertEqual(self.redeemed(OFFER_A), (6, "reported"))
        self.assertEqual(self.redeemed(OFFER_B), (7, "reported"))
        history = self.history()
        self.assertEqual(sorted(history[:2], key=lambda h: h[1]), [(None, 4), (None, 7)])
        self.assertEqual(history[2:], [(4, 6)])

    def test_same_email_once(self):
        """Το ίδιο email δεν μετριέται δύο φορές, όσες φορές κι αν τρέξει η επεξεργασία"""
        self.reply(self.rid, "[aaaaaaaa] offer: 4\n[bbbbbbbb] offer: 7")
        again = replies.process(self.con, "2026-11-10 09:00:00", self.cfg)
        self.assertEqual(again, [])
        self.assertEqual(len(self.history()), 2)

    def test_total_is_never_split(self):
        """Συγκεντρωτικό σύνολο χωρίς ανάλυση δεν μοιράζεται στις προσφορές"""
        res = self.reply(self.rid, "Συνολικά είχαμε 40 εξαργυρώσεις τον Οκτώβριο.")
        self.assertEqual(self.redeemed(OFFER_A), (None, "pending"))
        self.assertEqual(self.redeemed(OFFER_B), (None, "pending"))
        self.assertEqual(self.status(self.rid), "replied")
        self.assertIn("θέλει έλεγχο", res[0]["note"])

    def test_conflict_needs_review(self):
        """Αντιφατικοί αριθμοί: δεν καταχωρείται τίποτα και η προσφορά σημειώνεται για έλεγχο"""
        res = self.reply(self.rid, "[aaaaaaaa] offer: 4\n[aaaaaaaa] offer: 9")
        self.assertEqual(self.redeemed(OFFER_A), (None, "needs_review"))
        self.assertEqual(res[0]["conflicts"], ["offer aaaaaaaa"])

    def test_unknown_code_ignored(self):
        """Κωδικός που δεν ανήκει στο αίτημα αγνοείται και αναφέρεται"""
        res = self.reply(self.rid, "[aaaaaaaa] offer: 4\n[12345678] κάτι άλλο: 99")
        self.assertEqual(res[0]["unknown"], ["12345678"])
        self.assertEqual(self.redeemed(OFFER_A), (4, "reported"))
        total = self.con.execute("SELECT sum(redeemed) FROM redemptions").fetchone()[0]
        self.assertEqual(total, 4)


if __name__ == "__main__":
    unittest.main()
