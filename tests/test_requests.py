"""Εισαγωγή του μηνιαίου αρχείου, προετοιμασία και αποστολή αιτημάτων."""

import unittest

from openpyxl import Workbook

from base import OFFER_A, OFFER_B, OFFER_C, PERIOD, PROGRAM, SENT, AgentTestCase
from agent import assignments, mail, prepare_requests
from agent.config import Config

HEADER = ["UUID", "Admin Name", "Category", "Assignments", "Unique Users", "Engage URL"]


class IngestTests(AgentTestCase):
    def make_file(self, sheets, header=HEADER):
        wb = Workbook()
        wb.remove(wb.active)
        for title, rows in sheets.items():
            ws = wb.create_sheet(title)
            ws.append(header)
            for r in rows:
                ws.append(r)
        path = self.dir / "month.xlsx"
        wb.save(path)
        return path

    def test_only_offers_with_assignments(self):
        """Εισάγονται μόνο προσφορές με assignments > 0, από όλα τα φύλλα"""
        path = self.make_file({
            "generic": [[OFFER_A, "generic_kotsovolos_10%", "Tech", 25, 20, "http://x"],
                        [OFFER_B, "generic_avis_5€", "Travel", 0, 0, "http://y"]],
            "family": [[OFFER_C, "family_efood_3€", "Food", 7, 7, "http://z"]],
        })  # fmt: skip
        r = assignments.ingest(self.con, path, PERIOD, PROGRAM)
        self.assertEqual((r.imported, r.skipped_zero, r.total_assignments), (2, 1, 32))
        self.assertEqual(r.per_segment, {"generic": 1, "family": 1})
        # No answer yet is an empty value, never a zero.
        values = [r[0] for r in self.con.execute("SELECT redeemed FROM redemptions")]
        self.assertEqual(values, [None, None])

    def test_same_file_twice(self):
        """Η εισαγωγή του ίδιου αρχείου δεύτερη φορά δεν διπλασιάζει τίποτα"""
        path = self.make_file({"generic": [[OFFER_A, "generic_kotsovolos_10%", "Tech", 25, 20, "http://x"]]})
        assignments.ingest(self.con, path, PERIOD, PROGRAM)
        assignments.ingest(self.con, path, PERIOD, PROGRAM)
        self.assertEqual(self.con.execute("SELECT count(*), sum(assignments) FROM assignments").fetchone()[:], (1, 25))

    def test_duplicate_uuid_rejected(self):
        """Αρχείο με διπλό UUID απορρίπτεται ολόκληρο, δεν εισάγεται μισό"""
        path = self.make_file({
            "generic": [[OFFER_A, "generic_kotsovolos_10%", "Tech", 25, 20, "http://x"]],
            "family": [[OFFER_A, "family_kotsovolos_10%", "Tech", 5, 5, "http://x"]],
        })  # fmt: skip
        with self.assertRaises(assignments.IngestError):
            assignments.ingest(self.con, path, PERIOD, PROGRAM)
        self.assertEqual(self.con.execute("SELECT count(*) FROM offers").fetchone()[0], 0)

    def test_missing_column_rejected(self):
        """Αρχείο χωρίς τη στήλη Assignments απορρίπτεται με σαφές μήνυμα"""
        path = self.make_file({"generic": [[OFFER_A, "x", "Tech", "http://x"]]},
                              header=["UUID", "Admin Name", "Category", "Engage URL"])  # fmt: skip
        with self.assertRaisesRegex(assignments.IngestError, "Assignments"):
            assignments.ingest(self.con, path, PERIOD, PROGRAM)


class PlanAndSendTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.add_merchant("Kotsovolos", "kotsovolos@partners.test", [OFFER_A, OFFER_B])
        self.add_merchant("efood", "efood@partners.test", [OFFER_C])

    def plan(self):
        return prepare_requests.plan(self.con, PERIOD, PROGRAM)

    def send(self, cfg=None):
        return mail.send_requests(self.con, cfg or self.cfg, PERIOD, PROGRAM, SENT)

    def test_one_request_per_partner(self):
        """Ένα email ανά συνεργάτη, με όλες τις προσφορές του και τον κωδικό καθεμιάς"""
        drafts = {d["merchant"]: d for d in self.plan()["drafts"]}
        self.assertEqual(sorted(drafts), ["Kotsovolos", "efood"])
        self.assertEqual(drafts["Kotsovolos"]["offers"], 2)
        self.assertIn("[aaaaaaaa]", drafts["Kotsovolos"]["body"])
        self.assertIn("[bbbbbbbb]", drafts["Kotsovolos"]["body"])
        self.assertNotIn("[cccccccc]", drafts["Kotsovolos"]["body"])
        self.assertIn("Οκτώβριο 2026", drafts["Kotsovolos"]["subject"])

    def test_plan_twice(self):
        """Η προετοιμασία δεύτερη φορά δεν δημιουργεί δεύτερο αίτημα"""
        self.plan()
        self.plan()
        self.assertEqual(self.con.execute("SELECT count(*) FROM requests").fetchone()[0], 2)

    def test_no_contact_no_email(self):
        """Συνεργάτης χωρίς email δεν παίρνει αίτημα· εμφανίζεται ως εκκρεμότητα"""
        self.add_merchant("Avis", None, ["dddddddd-0000-0000-0000-000000000004"])
        r = self.plan()
        self.assertEqual(r["no_contact"], {"Avis": 1})
        self.assertNotIn("Avis", [d["merchant"] for d in r["drafts"]])

    def test_send_once(self):
        """Τα αιτήματα στέλνονται μία φορά· η επανάληψη δεν ξαναστέλνει"""
        self.plan()
        self.assertEqual(len(self.send()["sent"]), 2)
        self.assertEqual(self.send()["sent"], [])
        self.assertEqual(len(self.smtp.sent), 2)
        again = self.plan()
        self.assertEqual(again["drafts"], [])
        self.assertEqual(sorted(again["already_sent"]), ["Kotsovolos", "efood"])

    def test_test_mode_blocks_real_addresses(self):
        """Σε δοκιμαστική λειτουργία δεν φεύγει τίποτα σε διεύθυνση εκτός της επιτρεπόμενης λίστας"""
        self.add_merchant("Avis", "someone@avis.gr", ["dddddddd-0000-0000-0000-000000000004"])
        self.plan()
        r = self.send()
        self.assertEqual(r["blocked"], {"Avis": ["someone@avis.gr"]})
        self.assertNotIn("someone@avis.gr", [m["To"] for m in self.smtp.sent])
        status = self.con.execute(
            "SELECT r.status FROM requests r JOIN merchants m ON m.id=r.merchant_id WHERE m.name='Avis'"
        ).fetchone()[0]
        self.assertEqual(status, "draft")

    def test_empty_allowlist_blocks_everything(self):
        """Με κενή λίστα επιτρεπόμενων παραληπτών δεν στέλνεται κανένα email"""
        self.plan()
        r = self.send(Config(mode="test", mail_from="agent@warply.test", allowed=[]))
        self.assertEqual(r["sent"], [])
        self.assertEqual(self.smtp.sent, [])


if __name__ == "__main__":
    unittest.main()
