"""Local dashboard: a small web server for this computer only. It has no login.

It reads the same database as the command line. The only things it can change
are the ones the review queue offers: entering redemption numbers, sending a
clarification email (under the same test-mode recipient guard as every other
send) and filing a stray incoming email.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import automation, db, mail, prepare_requests, report, review, settings

PAGE = Path(__file__).with_name("dashboard.html")
FILTERS = ("program", "period_from", "period_to", "merchant", "category", "status", "q")


def _requests(con, period):
    return [
        dict(r)
        for r in con.execute(
            """SELECT r.id, m.name AS merchant, r.period, r.status, r.sent_at, r.followups_sent,
                      (SELECT count(*) FROM request_offers ro WHERE ro.request_id = r.id) AS offers,
                      (SELECT count(d.redeemed) FROM request_offers ro
                         JOIN redemptions d ON d.offer_uuid = ro.offer_uuid AND d.period = r.period
                        WHERE ro.request_id = r.id) AS filled
                 FROM requests r JOIN merchants m ON m.id = r.merchant_id
                WHERE r.status != 'draft' AND (? = '' OR r.period = ?)
                ORDER BY r.period DESC, lower(m.name)""",
            (period, period),
        )
    ]


def make_handler(cfg):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RedemptionsAgent"

        def log_message(self, *a):  # keep the terminal quiet
            pass

        # ------------------------------------------------------------ helpers
        def _send(self, code, body: bytes, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code=200):
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            """Only requests addressed to this computer by name. A page that
            points its own domain at 127.0.0.1 is turned away."""
            return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

        def _local_only(self) -> bool:
            """Refuse writes that a page on another site could trigger in the
            user's browser: they cannot set this header or use this origin."""
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            return (
                self.headers.get("X-Agent") == "1"
                and self.headers.get("Content-Type", "").startswith("application/json")
                and host.split(":")[0] in ("127.0.0.1", "localhost")
                and (origin is None or origin == f"http://{host}")
            )

        # ---------------------------------------------------------------- GET
        def do_GET(self):
            if not self._host_ok():
                return self._json({"error": "Μη επιτρεπτό αίτημα"}, 403)
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            con = db.connect(cfg.db)
            try:
                settings.apply(con, cfg)
                f = {k: q.get(k, "") for k in FILTERS}
                if url.path == "/api/settings":
                    return self._json({"values": settings.load(con, cfg), "status": automation.status(con, cfg)})
                if url.path == "/api/options":
                    data = report.options(con)
                    data["mode"], data["db"], data["max_followups"] = cfg.mode, cfg.db.name, cfg.max_followups
                    return self._json(data)
                if url.path == "/api/report":
                    rows = report.rows(con, f)
                    return self._json({"rows": rows, "summary": report.summary(rows, con, cfg.max_followups)})
                if url.path == "/api/export.xlsx":
                    rows = report.rows(con, f)
                    data = report.export_xlsx(rows, report.summary(rows, con, cfg.max_followups), f)
                    return self._send(
                        200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        {"Content-Disposition": 'attachment; filename="redemptions_report.xlsx"'},
                    )  # fmt: skip
                if url.path == "/api/queue":
                    return self._json(review.queue(con))
                if url.path == "/api/requests":
                    return self._json(_requests(con, q.get("period", "")))
                if url.path == "/api/thread":
                    return self._json(review.thread(con, int(q.get("id", "0"))))
                if url.path == "/api/clarification":
                    return self._json({"text": review.clarification_text(con, int(q.get("id", "0")), prepare_requests.period_label)})
                return self._json({"error": "Δεν βρέθηκε"}, 404)
            except (review.ReviewError, ValueError) as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:  # never drop the connection without an answer
                return self._json({"error": f"Εσωτερικό σφάλμα ({type(e).__name__})"}, 500)
            finally:
                con.close()

        # --------------------------------------------------------------- POST
        def do_POST(self):
            if not self._local_only():
                return self._json({"error": "Μη επιτρεπτό αίτημα"}, 403)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            except ValueError:
                return self._json({"error": "Μη έγκυρα δεδομένα"}, 400)
            if not isinstance(body, dict):
                return self._json({"error": "Μη έγκυρα δεδομένα"}, 400)
            now = mail.utcnow()
            con = db.connect(cfg.db)
            try:
                settings.apply(con, cfg)
                path = urlparse(self.path).path
                if path == "/api/settings":
                    values = settings.save(con, cfg, body.get("values") or {})
                    return self._json({"values": values, "status": automation.status(con, cfg)})
                if path == "/api/automation/fetch":
                    return self._json({"summary": automation.fetch_now(con, cfg, now)})
                if path == "/api/automation/approve":
                    return self._json({"summary": automation.approve_send(con, cfg, str(body["period"]), now)})
                if path == "/api/values":
                    return self._json(review.set_values(con, int(body["request_id"]), body.get("values") or {}, mail._iso(now)))
                if path == "/api/clarify":
                    return self._json(review.send_clarification(con, cfg, int(body["request_id"]), body.get("text", ""), now))
                if path == "/api/assign":
                    results = review.assign_message(con, int(body["message_id"]), int(body["request_id"]), mail._iso(now), cfg)
                    return self._json({"processed": [{"merchant": r["merchant"], "status": r["status"], "note": r["note"]} for r in results]})
                if path == "/api/keep-waiting":
                    return self._json(review.keep_waiting(con, int(body["request_id"]), mail._iso(now)))
                if path == "/api/resend":
                    return self._json(review.resend(con, cfg, int(body["request_id"]), body.get("emails") or [], now))
                if path == "/api/dismiss-bounce":
                    review.dismiss_bounce(con, int(body["request_id"]), mail._iso(now))
                    return self._json({"ok": True})
                if path == "/api/ignore":
                    review.ignore_message(con, int(body["message_id"]))
                    return self._json({"ok": True})
                return self._json({"error": "Δεν βρέθηκε"}, 404)
            except (review.ReviewError, mail.MailError, KeyError, TypeError, ValueError) as e:  # incl. SettingsError
                return self._json({"error": str(e) or "Μη έγκυρα δεδομένα"}, 400)
            except (OSError, mail.smtplib.SMTPException, mail.imaplib.IMAP4.error) as e:
                return self._json({"error": f"Σφάλμα σύνδεσης με τον mail server: {e}"}, 502)
            except Exception as e:  # never drop the connection without an answer
                return self._json({"error": f"Εσωτερικό σφάλμα ({type(e).__name__})"}, 500)
            finally:
                con.close()

    return Handler


def serve(cfg, port: int = 8765):
    """This computer only (127.0.0.1): the dashboard has no login, so it must
    not be reachable from other machines."""
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg))
