"""Αυτοματισμοί: εργάσιμες ημέρες, ρυθμίσεις από το dashboard, χρονοπρογραμματιστής."""

import unittest
from datetime import date, datetime, timezone

from openpyxl import Workbook

from base import OFFER_A, OFFER_B, AgentTestCase, day
from agent import automation, mail, settings, workdays

HEADER = ["UUID", "Admin Name", "Category", "Assignments", "Unique Users", "Engage URL"]


def athens(y, mo, d, h=12, mi=0):
    """A moment given in Greek time, as the UTC value the code works with."""
    return datetime(y, mo, d, h, mi, tzinfo=automation.TZ).astimezone(timezone.utc)


class WorkdayTests(unittest.TestCase):
    def test_orthodox_easter(self):
        """Το Πάσχα υπολογίζεται σωστά (2026: 12 Απριλίου, 2027: 2 Μαΐου)"""
        self.assertEqual(workdays.orthodox_easter(2026), date(2026, 4, 12))
        self.assertEqual(workdays.orthodox_easter(2027), date(2027, 5, 2))
        names = workdays.greek_holidays(2026)
        self.assertEqual(names[date(2026, 2, 23)], "Καθαρά Δευτέρα")
        self.assertEqual(names[date(2026, 6, 1)], "Αγίου Πνεύματος")

    def test_first_working_day(self):
        """Η πρώτη εργάσιμη προσπερνά Σαββατοκύριακα και αργίες"""
        self.assertEqual(workdays.first_working_day(2026, 12), date(2026, 12, 1))
        self.assertEqual(workdays.first_working_day(2027, 1), date(2027, 1, 4))    # Πρωτοχρονιά Παρασκευή
        self.assertEqual(workdays.first_working_day(2027, 5), date(2027, 5, 4))    # Πρωτομαγιά, Πάσχα, Δευτέρα του Πάσχα
        self.assertEqual(workdays.first_working_day(2027, 1, use_greek=False), date(2027, 1, 1))
        self.assertEqual(workdays.first_working_day(2026, 12, extra=[date(2026, 12, 1)]), date(2026, 12, 2))


class SettingsTests(AgentTestCase):
    def test_defaults_and_save(self):
        """Οι ρυθμίσεις ξεκινούν από το αρχείο ρυθμίσεων και ό,τι αποθηκευτεί υπερισχύει"""
        s = settings.load(self.con, self.cfg)
        self.assertEqual((s["automation_enabled"], s["followup_days"], s["max_followups"]), (False, 7, 3))
        settings.save(self.con, self.cfg, {"followup_days": 5, "max_followups": 2, "extra_holidays": ["2026-12-24", " 2026-12-24 "]})
        settings.apply(self.con, self.cfg)
        self.assertEqual((self.cfg.followup_days, self.cfg.max_followups), (5, 2))
        self.assertEqual(settings.load(self.con, self.cfg)["extra_holidays"], ["2026-12-24"])

    def test_invalid_values_rejected(self):
        """Μη έγκυρη ρύθμιση απορρίπτεται και δεν αποθηκεύεται τίποτα από την ίδια αλλαγή"""
        for bad in ({"followup_days": 0}, {"max_followups": 11}, {"cycle_time": "25:00"}, {"cycle_day": 31},
                    {"automation_enabled": "yes"}, {"extra_holidays": ["24/12/2026"]}, {"unknown": 1},
                    {"poll_minutes": 1}, {"followup_days": 5, "reminder_time": "x"}):  # fmt: skip
            with self.assertRaises(settings.SettingsError):
                settings.save(self.con, self.cfg, bad)
        self.assertEqual(settings.load(self.con, self.cfg)["followup_days"], 7)

    def test_reminder_interval_is_used(self):
        """Αλλαγή του διαστήματος υπενθυμίσεων ισχύει στην επόμενη αποστολή"""
        self.sent_request("Kotsovolos", "kotsovolos@partners.test")
        settings.save(self.con, self.cfg, {"followup_days": 3, "max_followups": 1})
        settings.apply(self.con, self.cfg)
        self.assertEqual(self.followups(day(2))["sent"], [])
        self.assertEqual(self.followups(day(3))["sent"], [("Kotsovolos", 1)])
        self.assertEqual(self.followups(day(30))["exhausted"], ["Kotsovolos"])


class SchedulerTests(AgentTestCase):
    def setUp(self):
        super().setUp()
        self._saved = (automation.INPUT_DIR, automation.PREVIEW_DIR, mail.fetch_replies)
        automation.INPUT_DIR = self.dir / "input"
        automation.PREVIEW_DIR = self.dir / "preview"
        automation.INPUT_DIR.mkdir()
        self.fetches = []
        mail.fetch_replies = lambda con, cfg, now: self.fetches.append(now) or {
            "new": [], "auto": [], "unmatched": [], "bounced": [], "ignored": 0}  # fmt: skip
        # Two partners with known offers; the November file below brings their assignments.
        self.add_merchant("Kotsovolos", "kotsovolos@partners.test", [OFFER_A])
        self.add_merchant("Avis", "avis@partners.test", [OFFER_B])
        settings.save(self.con, self.cfg, {"automation_enabled": True})

    def tearDown(self):
        automation.INPUT_DIR, automation.PREVIEW_DIR, mail.fetch_replies = self._saved
        super().tearDown()

    def november_file(self, name="assignments_2026-11.xlsx"):
        wb = Workbook()
        ws = wb.active
        ws.title = "generic"
        ws.append(HEADER)
        ws.append([OFFER_A, f"offer {OFFER_A[:8]}", "Tech", 25, 20, "http://x"])
        ws.append([OFFER_B, f"offer {OFFER_B[:8]}", "Travel", 7, 7, "http://y"])
        wb.save(automation.INPUT_DIR / name)

    def log(self, job=None):
        rows = self.con.execute("SELECT job, level, message FROM runs ORDER BY id").fetchall()
        return [r["message"] for r in rows if job in (None, r["job"])]

    def statuses(self, period="2026-11"):
        return sorted(r[0] for r in self.con.execute("SELECT status FROM requests WHERE period=?", (period,)))

    def test_disabled_does_nothing(self):
        """Με κλειστό τον αυτοματισμό δεν γίνεται τίποτα, ακόμη κι αν όλα είναι έτοιμα"""
        settings.save(self.con, self.cfg, {"automation_enabled": False})
        self.november_file()
        automation.tick(self.con, self.cfg, athens(2026, 12, 1))
        self.assertEqual((self.log(), self.statuses(), self.fetches), ([], [], []))

    def test_cycle_waits_for_its_day_and_time(self):
        """Ο κύκλος ξεκινά την πρώτη εργάσιμη στην ώρα του, όχι νωρίτερα"""
        self.november_file()
        automation.tick(self.con, self.cfg, athens(2026, 12, 1, 8, 59))
        self.assertEqual(self.statuses(), [])
        automation.tick(self.con, self.cfg, athens(2026, 12, 1, 9, 0))
        self.assertEqual(self.statuses(), ["draft", "draft"])

    def test_missing_file_is_an_error_not_an_old_file(self):
        """Χωρίς αρχείο του μήνα ο κύκλος περιμένει και το λέει μία φορά· δεν παίρνει παλιό αρχείο"""
        self.november_file("assignments_2026-10.xlsx")
        for hour in (9, 10, 11):
            automation.tick(self.con, self.cfg, athens(2026, 12, 1, hour))
        errors = self.log("cycle")
        self.assertEqual(len(errors), 1)
        self.assertIn("2026-11", errors[0])
        self.assertEqual(self.statuses(), [])
        self.assertEqual(automation.status(self.con, self.cfg, athens(2026, 12, 1, 12))["stage"], "waiting_file")

    def test_approval_then_send(self):
        """Με έγκριση: τα αιτήματα ετοιμάζονται αυτόματα και φεύγουν μόνο όταν το πει ο admin"""
        self.november_file()
        now = athens(2026, 12, 1, 9, 5)
        automation.tick(self.con, self.cfg, now)
        automation.tick(self.con, self.cfg, athens(2026, 12, 1, 9, 6))
        self.assertEqual(self.statuses(), ["draft", "draft"])
        self.assertEqual(self.smtp.sent, [])
        self.assertEqual(automation.status(self.con, self.cfg, now)["stage"], "awaiting_approval")
        summary = automation.approve_send(self.con, self.cfg, "2026-11", now)
        self.assertIn("Στάλθηκαν 2 αιτήματα", summary)
        self.assertEqual(self.statuses(), ["sent", "sent"])
        automation.tick(self.con, self.cfg, athens(2026, 12, 2, 9, 0))        # next day: nothing more to send
        self.assertEqual(len(self.smtp.sent), 2)
        self.assertEqual(automation.status(self.con, self.cfg, now)["stage"], "done")

    def test_send_without_approval(self):
        """Χωρίς έγκριση τα αιτήματα φεύγουν αυτόματα, μία φορά"""
        settings.save(self.con, self.cfg, {"require_approval": False})
        self.november_file()
        for minute in (0, 1, 2):
            automation.tick(self.con, self.cfg, athens(2026, 12, 1, 9, minute))
        self.assertEqual(len(self.smtp.sent), 2)
        self.assertEqual(self.statuses(), ["sent", "sent"])

    def test_fixed_day_moves_to_working_day(self):
        """Συγκεκριμένη ημέρα του μήνα που πέφτει Σάββατο πηγαίνει στη Δευτέρα"""
        settings.save(self.con, self.cfg, {"cycle_day_rule": "day_of_month", "cycle_day": 5, "require_approval": False})
        self.november_file()
        automation.tick(self.con, self.cfg, athens(2026, 12, 5, 12))           # Saturday
        self.assertEqual(self.statuses(), [])
        automation.tick(self.con, self.cfg, athens(2026, 12, 7, 9, 0))         # Monday
        self.assertEqual(self.statuses(), ["sent", "sent"])

    def test_mailbox_is_read_at_the_set_interval(self):
        """Τα εισερχόμενα διαβάζονται κάθε τόσα λεπτά όσα λέει η ρύθμιση"""
        settings.save(self.con, self.cfg, {"poll_minutes": 20})
        for minute in (0, 5, 19, 20, 39, 41):
            automation.tick(self.con, self.cfg, athens(2026, 12, 1, 12, minute))
        self.assertEqual([t.astimezone(automation.TZ).minute for t in self.fetches], [0, 20, 41])

    def test_reminders_once_a_day_on_working_days(self):
        """Οι υπενθυμίσεις φεύγουν μία φορά την ημέρα μετά την ώρα τους, όχι σε μη εργάσιμη"""
        rid = self.sent_request("efood", "efood@partners.test", ("cccccccc-0000-0000-0000-000000000003",))
        with self.con:  # the request went out on Fri 20 Nov 2026
            self.con.execute("UPDATE requests SET sent_at='2026-11-20 08:00:00' WHERE id=?", (rid,))
        settings.save(self.con, self.cfg, {"reminder_time": "10:00"})
        automation.tick(self.con, self.cfg, athens(2026, 11, 28, 11))          # Saturday, already due
        self.assertEqual(self.smtp.sent, [])
        automation.tick(self.con, self.cfg, athens(2026, 11, 30, 9, 30))       # Monday, before the hour
        self.assertEqual(self.smtp.sent, [])
        automation.tick(self.con, self.cfg, athens(2026, 11, 30, 10, 0))
        automation.tick(self.con, self.cfg, athens(2026, 11, 30, 15, 0))
        self.assertEqual(len(self.smtp.sent), 1)
        self.assertIn("efood (1η)", self.log("followups")[0])

    def test_one_failing_job_does_not_stop_the_others(self):
        """Αν αποτύχει η σύνδεση με το mailbox, καταγράφεται και οι υπόλοιπες εργασίες συνεχίζουν"""
        def broken(con, cfg, now):
            raise OSError("δεν υπάρχει σύνδεση")

        mail.fetch_replies = broken
        self.november_file()
        automation.tick(self.con, self.cfg, athens(2026, 12, 1, 9, 30))
        self.assertEqual(self.statuses(), ["draft", "draft"])
        self.assertIn("δεν υπάρχει σύνδεση", self.log("fetch")[0])


if __name__ == "__main__":
    unittest.main()
