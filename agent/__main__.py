"""Command line: python -m agent <command>"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import assignments, automation, config, contacts, db, demo, mail, old_report, prepare_requests, replies, report, settings, web

DEFAULT_PROGRAM = "ΔΕΗ myRewards Coupons"
ROOT = config.ROOT


def cmd_ingest(con, cfg, a):
    r = assignments.ingest(con, a.file, a.month, a.program, today=_athens_today(a.now), force=a.force)
    print(f"Μήνας {r.period}: {r.imported} προσφορές με assignments > 0 "
          f"(σύνολο {r.total_assignments} κουπόνια), {r.skipped_zero} αγνοήθηκαν με 0.")
    for seg, n in r.per_segment.items():
        print(f"  {seg}: {n}")


def _athens_today(now):
    """The calendar date in Greece: the monthly cycle follows Europe/Athens."""
    from zoneinfo import ZoneInfo

    try:
        return now.astimezone(ZoneInfo("Europe/Athens")).date()
    except Exception:  # no time zone database on this machine
        return now.date()


def cmd_contacts_export(con, cfg, a):
    n = contacts.export_contacts(con, a.out, a.fake_emails)
    print(f"Γράφτηκαν {n} προσφορές στο {a.out}")
    if a.fake_emails:
        print("Τα emails είναι ψεύτικα, μόνο για δοκιμές.")
    else:
        print("Η στήλη Merchant είναι πρόταση· διορθώστε τη και συμπληρώστε τα Emails.")


def cmd_contacts_import(con, cfg, a):
    s = contacts.import_contacts(con, a.file)
    print(f"Αντιστοιχίστηκαν {s['mapped']} προσφορές, {s['unmapped']} έμειναν χωρίς έμπορο.")
    if s["unknown_uuid"]:
        print(f"Άγνωστα UUID (αγνοήθηκαν): {len(s['unknown_uuid'])}")
    for e in s["bad_emails"]:
        print(f"Μη έγκυρο email, {e}")


def cmd_plan(con, cfg, a):
    r = prepare_requests.plan(con, a.month, a.program, ROOT / "data" / "outbox_preview")
    print(f"Μήνας {a.month}: {len(r['drafts'])} emails έτοιμα προς αποστολή (δεν στάλθηκε τίποτα).")
    for d in r["drafts"][:15]:
        print(f"  {d['merchant']}: {d['offers']} προσφορές → {', '.join(d['to'])}")
    if len(r["drafts"]) > 15:
        print(f"  … και άλλα {len(r['drafts']) - 15}")
    if r["already_sent"]:
        print(f"Έχουν ήδη σταλεί: {len(r['already_sent'])}")
    if r["no_contact"]:
        print(f"Έμποροι χωρίς email: {len(r['no_contact'])}")
    if r["no_merchant"]:
        print(f"Προσφορές χωρίς έμπορο: {len(r['no_merchant'])}")
    for name, offer in r["not_covered"]:
        print(f"  Εκτός αιτήματος (προστέθηκε μετά την αποστολή): {name} – {offer}")
    if r["drafts"]:
        print(f"Προεπισκόπηση: {ROOT / 'data' / 'outbox_preview' / a.month}")


def cmd_send(con, cfg, a):
    r = mail.send_requests(con, cfg, a.month, a.program, a.now, a.limit, a.only)
    print(f"Στάλθηκαν {len(r['sent'])} αιτήματα (λειτουργία: {cfg.mode}, μέσω {cfg.smtp_host}).")
    for name, to in r["sent"]:
        print(f"  {name} → {', '.join(to)}")
    for name, bad in r["blocked"].items():
        print(f"  ΜΠΛΟΚΑΡΙΣΤΗΚΕ {name}: {', '.join(bad)} εκτός ALLOWED_RECIPIENTS")
    for name in r["no_contact"]:
        print(f"  Χωρίς email: {name}")
    for name, to in r["refused"].items():
        print(f"  ΑΝΕΠΙΔΟΤΟ {name}: ο mail server αρνήθηκε {', '.join(to)}")


def cmd_followups(con, cfg, a):
    r = mail.send_followups(con, cfg, a.now, prepare_requests.period_label)
    print(f"Στάλθηκαν {len(r['sent'])} υπενθυμίσεις.")
    for name, n in r["sent"]:
        print(f"  {name}: υπενθύμιση {n}/{cfg.max_followups}")
    if r["exhausted"]:
        print(f"Χωρίς απάντηση μετά από {cfg.max_followups} υπενθυμίσεις: {', '.join(r['exhausted'])}")
    for name, bad in r["blocked"].items():
        print(f"  ΜΠΛΟΚΑΡΙΣΤΗΚΕ {name}: {', '.join(bad) or 'χωρίς email'}")


def cmd_fetch(con, cfg, a):
    r = mail.fetch_replies(con, cfg, a.now)
    print(f"Νέες απαντήσεις: {len(r['new'])}, αυτόματες: {len(r['auto'])}, ανεπίδοτα: {len(r['bounced'])}, "
          f"χωρίς αντιστοίχιση: {len(r['unmatched'])}, άσχετα (αγνοήθηκαν): {r['ignored']}")
    for name, failed in r["bounced"]:
        print(f"  [ΑΝΕΠΙΔΟΤΟ] {name}: {', '.join(failed) or 'άγνωστη διεύθυνση'}")
    for label, key in (("απάντηση", "new"), ("αυτόματη", "auto"), ("ΑΓΝΩΣΤΟ", "unmatched")):
        for sender, subject in r[key]:
            print(f"  [{label}] {sender}: {subject}")
    cmd_process(con, cfg, a)


def cmd_process(con, cfg, a):
    results = replies.process(con, a.now.strftime("%Y-%m-%d %H:%M:%S"), cfg)
    if not results:
        return
    print(f"Επεξεργασία {len(results)} απαντήσεων:")
    for r in results:
        print(f"  {r['merchant']} → {r['status']} ({r['filled']}/{r['total']} προσφορές): {r['note']}")
        for name, old, new in r["written"]:
            change = f"{new}" if old is None else f"{old} → {new} (διόρθωση)"
            print(f"      {name}: {change}")
        for code in r["unknown"]:
            print(f"      άγνωστος κωδικός [{code}]")
        for name in r["conflicts"]:
            print(f"      ΑΝΤΙΦΑΣΗ, θέλει έλεγχο: {name}")


def cmd_redemptions(con, cfg, a):
    rows = con.execute(
        """SELECT m.name, r.status AS request_status, o.admin_name, s.assignments,
                  d.redeemed, d.status
             FROM requests r
             JOIN merchants m ON m.id = r.merchant_id
             JOIN request_offers ro ON ro.request_id = r.id
             JOIN offers o ON o.uuid = ro.offer_uuid
             JOIN assignments s ON s.offer_uuid = o.uuid AND s.period = r.period
             LEFT JOIN redemptions d ON d.offer_uuid = o.uuid AND d.period = r.period
            WHERE r.period = ? AND r.status != 'draft'
            ORDER BY m.name, o.admin_name""",
        (a.month,),
    ).fetchall()
    print(f"{'Συνεργάτης':<22}{'Αίτημα':<11}{'Assigned':>9}{'Redeemed':>9}  Προσφορά")
    last = None
    for r in rows:
        name = r["name"] if r["name"] != last else ""
        req = r["request_status"] if r["name"] != last else ""
        last = r["name"]
        red = "—" if r["redeemed"] is None else str(r["redeemed"])
        flag = "  (θέλει έλεγχο)" if r["status"] == "needs_review" else ""
        print(f"{name[:21]:<22}{req:<11}{r['assignments']:>9}{red:>9}  {r['admin_name']}{flag}")
    filled = sum(1 for r in rows if r["redeemed"] is not None)
    print(f"\nΣυμπληρωμένες: {filled} από {len(rows)} προσφορές σε σταλμένα αιτήματα.")


def _report_reply_text(con, a):
    """Reply in the agreed format, with the numbers the team recorded in the
    existing report. Offers the report has no number for are left out."""
    values = old_report.redeemed_by_offer(a.from_report, a.month)
    offers = con.execute(
        """SELECT o.uuid, o.admin_name FROM requests r
             JOIN merchants m ON m.id = r.merchant_id
             JOIN request_offers ro ON ro.request_id = r.id
             JOIN offers o ON o.uuid = ro.offer_uuid
            WHERE r.period = ? AND lower(m.name) LIKE ? ORDER BY o.admin_name""",
        (a.month, f"%{a.merchant.lower()}%"),
    ).fetchall()
    lines = [
        f"[{replies.offer_code(o['uuid'])}] {o['admin_name']}: {old_report.lookup(values, o['admin_name'])}"
        for o in offers
        if old_report.lookup(values, o["admin_name"]) is not None
    ]
    return "Καλησπέρα σας,\n\nτα στοιχεία του μήνα:\n\n" + "\n".join(lines) + "\n\nΜε εκτίμηση"


def cmd_import_history(con, cfg, a):
    stats = old_report.import_history(con, a.file, a.program, a.now.strftime("%Y-%m-%d %H:%M:%S"))
    for period, s in stats.items():
        print(f"{period}: {s['rows']} προσφορές ({s['new_offers']} νέες χωρίς UUID), "
              f"νέες εξαργυρώσεις {s['redeemed']}, {s['empty']} χωρίς τιμή, {s['kept']} υπήρχαν ήδη")


def cmd_simulate_reply(con, cfg, a):
    if bool(a.text) == bool(a.from_report):
        raise ValueError("Δώσε ένα από τα --text ή --from-report")
    if a.from_report:
        a.text = _report_reply_text(con, a)
    name, sender, delivered = mail.simulate_reply(con, cfg, a.merchant, a.month, a.text, a.attach, a.auto)
    if delivered:
        where = "στα Εισερχόμενα" if delivered == "inbox" else "στα SPAM"
        print(f"Το αίτημα είχε παραδοθεί {where} του {sender}.")
    print(f"Στάλθηκε δοκιμαστική απάντηση ως {name} ({sender}). Τρέξε «fetch» για να παραληφθεί.")


def cmd_threads(con, cfg, a):
    rows = con.execute(
        """SELECT r.id, m.name, r.status, r.sent_at, r.followups_sent,
                  (SELECT count(*) FROM request_offers ro WHERE ro.request_id=r.id) AS offers
             FROM requests r JOIN merchants m ON m.id=r.merchant_id
            WHERE r.period=? ORDER BY m.name""",
        (a.month,),
    ).fetchall()
    if a.merchant:
        rows = [r for r in rows if a.merchant.lower() in r["name"].lower()]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"Μήνας {a.month}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    detailed = bool(a.merchant)
    for r in rows:
        if not detailed and r["status"] == "draft":
            continue
        print(f"\n{r['name']} [{r['status']}] {r['offers']} προσφορές, "
              f"στάλθηκε {r['sent_at'] or '-'}, υπενθυμίσεις {r['followups_sent']}/{cfg.max_followups}")
        for m in con.execute("SELECT * FROM messages WHERE request_id=? ORDER BY id", (r["id"],)):
            arrow = "→" if m["direction"] == "out" else "←"
            files = json.loads(m["attachments"] or "[]")
            print(f"   {arrow} {m['at']} {m['kind']:<8} {m['from_addr']}"
                  + (f"  [{len(files)} συνημμένα]" if files else ""))
            if detailed or m["direction"] == "in":
                for line in (m["body"] or "").splitlines()[: (40 if detailed else 6)]:
                    print(f"        {line}")
    orphans = con.execute("SELECT * FROM messages WHERE request_id IS NULL ORDER BY id").fetchall()
    if orphans and not a.merchant:
        print("\nΕισερχόμενα χωρίς αντιστοίχιση (θέλουν έλεγχο):")
        for m in orphans:
            print(f"   ← {m['at']} {m['from_addr']}: {m['subject']}")


def cmd_status(con, cfg, a):
    q = lambda sql, *p: con.execute(sql, p).fetchone()[0]
    offers = q("SELECT count(*) FROM assignments WHERE period=?", a.month)
    print(f"Μήνας {a.month}  (βάση: {cfg.db.name}, λειτουργία: {cfg.mode})")
    print(f"  Προσφορές με assignments: {offers}")
    print(f"  Κουπόνια που δόθηκαν:     {q('SELECT coalesce(sum(assignments),0) FROM assignments WHERE period=?', a.month)}")
    print(f"  Με αντιστοιχισμένο έμπορο: {q('SELECT count(*) FROM assignments a JOIN offers o ON o.uuid=a.offer_uuid WHERE a.period=? AND o.merchant_id IS NOT NULL', a.month)}")
    for st in ("draft", "sent", "undelivered", "replied", "partial", "completed"):
        print(f"  Αιτήματα {st + ':':<17} {q('SELECT count(*) FROM requests WHERE period=? AND status=?', a.month, st)}")
    print(f"  Εξαργυρώσεις δηλωμένες:    {q('SELECT count(*) FROM redemptions WHERE period=? AND redeemed IS NOT NULL', a.month)} από {offers}")


def cmd_dashboard(con, cfg, a):
    server = web.serve(cfg, a.port, a.host)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Dashboard: {url}  (βάση: {cfg.db.name}, λειτουργία: {cfg.mode})")
    print("Τρέχει μόνο σε αυτόν τον υπολογιστή. Ctrl+C για τερματισμό.")
    if not a.no_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def cmd_tick(con, cfg, a):
    s = automation.tick(con, cfg, a.now)
    if not s["automation_enabled"]:
        print("Ο αυτοματισμός είναι απενεργοποιημένος (Ρυθμίσεις στο dashboard). Δεν έγινε τίποτα.")
    for r in reversed(con.execute("SELECT at, job, level, message FROM runs ORDER BY id DESC LIMIT 5").fetchall()):
        print(f"  {r['at']} [{r['job']}] {r['message']}")


def cmd_run(con, cfg, a):
    """Scheduler and dashboard in one process."""
    import threading

    stop = threading.Event()
    worker = threading.Thread(
        target=automation.run_forever, args=(lambda: db.connect(cfg.db), cfg, stop), daemon=True
    )
    worker.start()
    server = web.serve(cfg, a.port, a.host)
    print(f"Χρονοπρογραμματιστής και dashboard: http://127.0.0.1:{a.port}/  (βάση: {cfg.db.name}, λειτουργία: {cfg.mode})")
    print("Οι αυτοματισμοί ρυθμίζονται στην καρτέλα «Ρυθμίσεις». Ctrl+C για τερματισμό.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


def cmd_export(con, cfg, a):
    f = {"period_from": a.month_from, "period_to": a.month_to, "merchant": a.merchant}
    rows = report.rows(con, f)
    a.out.write_bytes(report.export_xlsx(rows, report.summary(rows, con, cfg.max_followups), f))
    print(f"Γράφτηκαν {len(rows)} προσφορές στο {a.out}")


def _parse_now(text):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("μορφή: YYYY-MM-DD ή 'YYYY-MM-DD HH:MM'")


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="python main.py")
    p.add_argument("--env", type=Path, help="αρχείο ρυθμίσεων (προεπιλογή: .env)")
    p.add_argument("--db", type=Path, help="βάση δεδομένων (προεπιλογή: από τις ρυθμίσεις)")
    p.add_argument("--now", type=_parse_now, help="προσομοίωση ημερομηνίας, για δοκιμές")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, fn, help_):
        s = sub.add_parser(name, help=help_)
        s.set_defaults(fn=fn)
        return s

    s = add("ingest", cmd_ingest, "εισαγωγή του μηνιαίου αρχείου assignments")
    s.add_argument("file", type=Path)
    s.add_argument("--month", required=True, help="μήνας αναφοράς, YYYY-MM")
    s.add_argument("--program", default=DEFAULT_PROGRAM)
    s.add_argument("--force", action="store_true",
                   help="χωρίς τους ελέγχους λάθος αρχείου (δοκιμαστικά δεδομένα ή σκόπιμη επαναφόρτωση)")

    s = add("contacts-export", cmd_contacts_export, "αρχείο contacts προς συμπλήρωση")
    s.add_argument("out", type=Path)
    s.add_argument("--fake-emails", metavar="PATTERN",
                   help="ψεύτικα emails για δοκιμές, π.χ. {slug}@partners.test")

    s = add("contacts-import", cmd_contacts_import, "φόρτωση του συμπληρωμένου αρχείου contacts")
    s.add_argument("file", type=Path)

    s = add("plan", cmd_plan, "προετοιμασία των emails του μήνα (χωρίς αποστολή)")
    s.add_argument("--month", required=True)
    s.add_argument("--program", default=DEFAULT_PROGRAM)

    s = add("send", cmd_send, "αποστολή των έτοιμων αιτημάτων")
    s.add_argument("--month", required=True)
    s.add_argument("--program", default=DEFAULT_PROGRAM)
    s.add_argument("--limit", type=int, help="το πολύ τόσα emails")
    s.add_argument("--only", help="μόνο έμποροι που περιέχουν αυτό το κείμενο")

    add("followups", cmd_followups, "αποστολή υπενθυμίσεων που έχουν λήξει")
    add("fetch", cmd_fetch, "παραλαβή νέων απαντήσεων από το mailbox")

    s = add("simulate-reply", cmd_simulate_reply, "δοκιμαστική απάντηση εμπόρου (μόνο τοπικά)")
    s.add_argument("--merchant", required=True)
    s.add_argument("--month", required=True)
    s.add_argument("--text")
    s.add_argument("--from-report", type=Path, metavar="XLSX",
                   help="απάντηση στη συμφωνημένη μορφή με τα νούμερα του υπάρχοντος report")
    s.add_argument("--attach", type=Path)
    s.add_argument("--auto", action="store_true", help="σήμανση ως αυτόματη απάντηση")

    s = add("import-history", cmd_import_history, "φόρτωση όλων των μηνών του παλιού report")
    s.add_argument("file", type=Path)
    s.add_argument("--program", default=DEFAULT_PROGRAM)

    add("process", cmd_process, "ανάγνωση των απαντήσεων και ενημέρωση των εξαργυρώσεων")

    s = add("redemptions", cmd_redemptions, "εξαργυρώσεις ανά προσφορά για τα σταλμένα αιτήματα")
    s.add_argument("--month", required=True)

    s = add("threads", cmd_threads, "αιτήματα και αλληλογραφία του μήνα")
    s.add_argument("--month", required=True)
    s.add_argument("--merchant", help="πλήρες νήμα για έναν έμπορο")

    s = add("status", cmd_status, "σύνοψη μήνα")
    s.add_argument("--month", required=True)

    s = add("dashboard", cmd_dashboard, "άνοιγμα του dashboard στον browser")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1",
                   help="διεύθυνση ακρόασης· μην την αλλάζετε, το dashboard δεν έχει login")
    s.add_argument("--no-browser", action="store_true", help="χωρίς αυτόματο άνοιγμα browser")

    s = add("run", cmd_run, "χρονοπρογραμματιστής και dashboard μαζί (η κανονική λειτουργία)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1",
                   help="διεύθυνση ακρόασης· μην την αλλάζετε, το dashboard δεν έχει login")

    add("tick", cmd_tick, "ένα πέρασμα του χρονοπρογραμματιστή (π.χ. από cron)")

    s = add("export", cmd_export, "εξαγωγή του report σε Excel")
    s.add_argument("out", type=Path)
    s.add_argument("--month-from", help="YYYY-MM")
    s.add_argument("--month-to", help="YYYY-MM")
    s.add_argument("--merchant")

    if argv is None:
        argv = sys.argv[1:]
    a = p.parse_args(argv or ["run"])  # no command: start the agent
    try:
        cfg = config.load(a.env)
    except ValueError as e:
        print(f"Σφάλμα ρυθμίσεων: {e}", file=sys.stderr)
        return 1
    if a.db:
        cfg.db = a.db
    a.now = a.now or mail.utcnow()
    con = db.connect(cfg.db)
    try:
        if cfg.db.name == demo.NAME and demo.is_empty(con):
            demo.build(con, cfg)
            print("Δημιουργήθηκε δοκιμαστική βάση με επινοημένα δεδομένα (data/demo.db).")
        settings.apply(con, cfg)  # what the admin saved in the dashboard wins over the env file
        a.fn(con, cfg, a)
    except (assignments.IngestError, mail.MailError, ValueError, FileNotFoundError) as e:
        print(f"Σφάλμα: {e}", file=sys.stderr)
        return 1
    except (OSError, mail.smtplib.SMTPException, mail.imaplib.IMAP4.error) as e:
        print(f"Σφάλμα σύνδεσης με τον mail server: {e}", file=sys.stderr)
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
