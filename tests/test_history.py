"""Φόρτωση των παλιών μηνών από το χειροκίνητο report."""

import unittest

from openpyxl import Workbook

from base import OFFER_A, PROGRAM, AgentTestCase
from agent import old_report, report

NOW = "2026-11-16 10:00:00"


class HistoryTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        # One offer the database already knows (with its real UUID) and its partner.
        self.add_merchant("Kotsovolos", "kotsovolos@partners.test", [])
        mid = self.con.execute("SELECT id FROM merchants").fetchone()[0]
        with self.con:
            self.con.execute(
                "INSERT INTO offers (uuid, program, segment, admin_name, merchant_id) VALUES (?, ?, 'generic', ?, ?)",
                (OFFER_A, PROGRAM, "generic_Kotsovolos_10%_έκπτωση_31-12-2026", mid),
            )
        wb = Workbook()
        wb.remove(wb.active)
        for title, rows in {
            "August 2026": [("Τεχνολογία", "generic_Kotsovolos_10%_έκπτωση_31-12-2026", 50, 20),
                            (None, "family_kotsovolos_5€_31-08-2026", 10, 0),
                            ("Φαγητό", "generic_efood_3€_31-08-2026", 30, None),
                            ("!", None, 90, 20)],
            "September 2026": [("Τεχνολογία", "generic_Kotsovolos_10%_έκπτωση_31-12-2026", 40, 12)],
            "Σημειώσεις": [("x", "y", 1, 1)],
        }.items():  # fmt: skip
            ws = wb.create_sheet(title)
            ws.append([None, None, "01/08 - 31/08"])
            ws.append(["Category ", "Offer", "Assigned", "Redeemed", "Redemption Rate"])
            for r in rows:
                ws.append(list(r))
        self.path = self.dir / "report.xlsx"
        wb.save(self.path)

    def test_import(self):
        """Φορτώνονται όλοι οι μήνες· η γραμμή συνόλου και τα άσχετα φύλλα αγνοούνται"""
        stats = old_report.import_history(self.con, self.path, PROGRAM, NOW)
        self.assertEqual(sorted(stats), ["2026-08", "2026-09"])
        self.assertEqual((stats["2026-08"]["rows"], stats["2026-08"]["redeemed"]), (3, 20))
        monthly = {m["period"]: (m["offers"], m["reported"], m["assigned"], m["redeemed"])
                   for m in report.summary(report.rows(self.con))["monthly"]}  # fmt: skip
        self.assertEqual(monthly, {"2026-08": (3, 2, 90, 20), "2026-09": (1, 1, 40, 12)})

    def test_known_offer_keeps_its_uuid(self):
        """Προσφορά που υπάρχει ήδη κρατά το UUID της· οι άγνωστες παίρνουν προσωρινό αναγνωριστικό"""
        old_report.import_history(self.con, self.path, PROGRAM, NOW)
        ids = {r["admin_name"]: r["uuid"] for r in self.con.execute("SELECT admin_name, uuid FROM offers")}
        self.assertEqual(ids["generic_Kotsovolos_10%_έκπτωση_31-12-2026"], OFFER_A)
        self.assertTrue(ids["family_kotsovolos_5€_31-08-2026"].startswith("report-"))
        rows = {r["offer"]: r for r in report.rows(self.con, {"period_from": "2026-08", "period_to": "2026-08"})}
        # Tied to a partner only when the name in the offer is exactly a known partner.
        self.assertEqual(rows["family_kotsovolos_5€_31-08-2026"]["merchant"], "Kotsovolos")
        self.assertIsNone(rows["generic_efood_3€_31-08-2026"]["merchant"])
        self.assertEqual(rows["family_kotsovolos_5€_31-08-2026"]["category"], "Τεχνολογία")

    def test_empty_is_not_zero(self):
        """Κενό Redeemed στο report μένει κενό· το 0 μένει 0"""
        old_report.import_history(self.con, self.path, PROGRAM, NOW)
        got = {r["offer"]: r["redeemed"] for r in report.rows(self.con, {"period_from": "2026-08", "period_to": "2026-08"})}
        self.assertEqual(got["family_kotsovolos_5€_31-08-2026"], 0)
        self.assertIsNone(got["generic_efood_3€_31-08-2026"])

    def test_twice_changes_nothing(self):
        """Δεύτερη φόρτωση δεν διπλασιάζει ούτε αλλάζει τίποτα, ούτε πειράζει χειροκίνητη διόρθωση"""
        old_report.import_history(self.con, self.path, PROGRAM, NOW)
        with self.con:
            self.con.execute("UPDATE redemptions SET redeemed=99 WHERE offer_uuid=? AND period='2026-09'", (OFFER_A,))
        before = self.con.execute("SELECT count(*) FROM redemption_history").fetchone()[0]
        again = old_report.import_history(self.con, self.path, PROGRAM, NOW)
        self.assertEqual(again["2026-08"]["redeemed"] + again["2026-09"]["redeemed"], 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM redemption_history").fetchone()[0], before)
        self.assertEqual(self.con.execute("SELECT count(*) FROM offers").fetchone()[0], 3)
        self.assertEqual(self.con.execute("SELECT redeemed FROM redemptions WHERE offer_uuid=? AND period='2026-09'", (OFFER_A,)).fetchone()[0], 99)


if __name__ == "__main__":
    unittest.main()
