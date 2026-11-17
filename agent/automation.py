"""The scheduler: what the agent does by itself, and when.

tick() is one pass. It looks at the clock (Europe/Athens), the settings and
what has already been done, and runs whatever is due:

  * the monthly cycle: on its start day, import the previous month's file,
    prepare the requests and (with or without approval) send them
  * replies: read the mailbox every few minutes
  * reminders: once a day, at the set time

Every step records what it did in the 'runs' table, which the dashboard shows.
Each pass is safe to repeat: progress is kept in 'automation_state', and the
steps themselves never send or count anything twice.
"""

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import assignments, mail, prepare_requests, replies, settings, workdays
from .config import ROOT, Config

TZ = ZoneInfo("Europe/Athens")
DEFAULT_PROGRAM = "ΔΕΗ myRewards Coupons"
INPUT_DIR = ROOT / "input"
PREVIEW_DIR = ROOT / "data" / "outbox_preview"
HEARTBEAT_SECONDS = 60


# ------------------------------------------------------------------- state
def get_state(con, key):
    row = con.execute("SELECT value FROM automation_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(con, key, value):
    with con:
        con.execute(
            "INSERT INTO automation_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def log(con, now, job, message, level="info", once=False):
    """once: skip when this job already said the same thing today, so a
    condition that lasts for hours is reported one time."""
    at = mail._iso(now)
    if once and con.execute(
        "SELECT 1 FROM runs WHERE job=? AND message=? AND substr(at, 1, 10)=? LIMIT 1", (job, message, at[:10])
    ).fetchone():
        return
    with con:
        con.execute("INSERT INTO runs (at, job, level, message) VALUES (?, ?, ?, ?)", (at, job, level, message))


# ---------------------------------------------------------------- calendar
def _hm(text):
    h, m = text.split(":")
    return int(h), int(m)


def previous_month(d) -> str:
    year, month = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return f"{year}-{month:02d}"


def cycle_start(year: int, month: int, s: dict) -> datetime:
    """When the cycle of that calendar month begins (it asks about the month before)."""
    extra = settings.extra_dates(s)
    if s["cycle_day_rule"] == "day_of_month":
        day = workdays.on_or_after(datetime(year, month, s["cycle_day"]).date(), s["greek_holidays"], extra)
    else:
        day = workdays.first_working_day(year, month, s["greek_holidays"], extra)
    h, m = _hm(s["cycle_time"])
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def _may_send_today(local, s) -> bool:
    return not s["working_days_only"] or workdays.is_working_day(
        local.date(), s["greek_holidays"], settings.extra_dates(s)
    )


def find_file(period: str):
    """The assignments export of the month: the one .xlsx in input/ with the
    month (YYYY-MM) in its name. Returns (path, problem)."""
    found = sorted(p for p in INPUT_DIR.glob("*.xlsx") if period in p.name and not p.name.startswith("~$"))
    if not found:
        return None, f"Δεν βρέθηκε αρχείο assignments για τον {period}: χρειάζεται ένα .xlsx με «{period}» στο όνομα μέσα στον φάκελο input"
    if len(found) > 1:
        return None, f"Βρέθηκαν {len(found)} αρχεία για τον {period} στον φάκελο input ({', '.join(p.name for p in found)})· χρειάζεται ακριβώς ένα"
    return found[0], None


# -------------------------------------------------------------------- jobs
def _drafts(con, period) -> int:
    return con.execute("SELECT count(*) FROM requests WHERE period=? AND status='draft'", (period,)).fetchone()[0]


def _send(con, cfg, period, now) -> str:
    r = mail.send_requests(con, cfg, period, DEFAULT_PROGRAM, now)
    parts = [f"Στάλθηκαν {len(r['sent'])} αιτήματα για τον {period}"]
    if r["blocked"]:
        parts.append(f"{len(r['blocked'])} μπλοκαρίστηκαν (δοκιμαστική λειτουργία)")
    if r["refused"]:
        parts.append(f"{len(r['refused'])} ανεπίδοτα")
    if r["no_contact"]:
        parts.append(f"{len(r['no_contact'])} χωρίς email")
    if r["failed"]:
        parts.append(f"{len(r['failed'])} απορρίφθηκαν από τον mail server ({'; '.join(f'{k}: {v}' for k, v in r['failed'].items())})")
    return ", ".join(parts)


def _cycle(con, cfg, s, now, local):
    period = previous_month(local.date())
    left = con.execute(
        "SELECT period, count(*) AS n FROM requests WHERE status='draft' AND period < ? GROUP BY period", (period,)
    ).fetchall()
    for row in left:
        log(con, now, "cycle", f"{row['n']} αιτήματα του {row['period']} δεν στάλθηκαν ποτέ και ο μήνας έχει κλείσει", "error", once=True)
    if local < cycle_start(local.year, local.month, s):
        return
    if not con.execute("SELECT 1 FROM assignments WHERE period=? LIMIT 1", (period,)).fetchone():
        path, problem = find_file(period)
        if problem:
            # No silent fallback to an older file: the cycle waits and says why.
            log(con, now, "cycle", problem, "error", once=True)
            return
        r = assignments.ingest(con, path, period, DEFAULT_PROGRAM, today=local.date())
        log(con, now, "cycle", f"Εισαγωγή {path.name}: {r.imported} προσφορές με assignments, {r.total_assignments} κουπόνια")
    # Prepared again at every mailbox interval for the rest of the month, so a
    # partner who gets an email address later still receives the request.
    planned = get_state(con, f"cycle.{period}.planned")
    if not planned or now - mail._parse(planned) >= timedelta(minutes=s["poll_minutes"]):
        r = prepare_requests.plan(con, period, DEFAULT_PROGRAM, PREVIEW_DIR)
        set_state(con, f"cycle.{period}.planned", mail._iso(now))
        if r["drafts"] or not planned:
            log(con, now, "cycle",
                f"Ετοιμάστηκαν {len(r['drafts'])} αιτήματα για τον {period}. "
                f"Χωρίς email: {len(r['no_contact'])} συνεργάτες. Χωρίς συνεργάτη: {len(r['no_merchant'])} προσφορές.",
                once=True)  # fmt: skip
        if r["not_covered"]:
            log(con, now, "cycle",
                f"{len(r['not_covered'])} προσφορές του {period} δεν είναι σε κανένα αίτημα, γιατί ο συνεργάτης τους "
                f"ορίστηκε αφού είχε φύγει το email του: {', '.join(o for _, o in r['not_covered'][:5])}",
                "error", once=True)  # fmt: skip
    if not _drafts(con, period):
        return
    if s["require_approval"]:
        log(con, now, "cycle", f"{_drafts(con, period)} αιτήματα για τον {period} περιμένουν έγκριση αποστολής", once=True)
        return
    if not _may_send_today(local, s):
        return
    log(con, now, "cycle", _send(con, cfg, period, now), once=True)


def approve_send(con, cfg: Config, period: str, now) -> str:
    """The admin pressed "send" for the prepared requests of a month."""
    settings.apply(con, cfg)
    if not _drafts(con, period):
        raise ValueError(f"Δεν υπάρχουν αιτήματα προς αποστολή για τον {period}")
    summary = _send(con, cfg, period, now)
    log(con, now, "cycle", summary + " (έγκριση από το dashboard)")
    return summary


def fetch_now(con, cfg: Config, now) -> str:
    """Read the mailbox and process what arrived. Also the "check now" button."""
    r = mail.fetch_replies(con, cfg, now)
    done = replies.process(con, mail._iso(now), cfg)
    set_state(con, "fetch.last", mail._iso(now))
    set_state(con, "fetch.ok", mail._iso(now))  # the mailbox really was read
    recorded = sum(len(x["written"]) for x in done)
    waiting = sum(1 for x in done if x["status"] == "replied")
    summary = (
        f"Νέες απαντήσεις: {len(r['new'])}, ανεπίδοτα: {len(r['bounced'])}, χωρίς αντιστοίχιση: {len(r['unmatched'])}. "
        f"Καταχωρήθηκαν {recorded} τιμές, {waiting} απαντήσεις πήγαν για έλεγχο."
    )
    if r["new"] or r["bounced"] or r["unmatched"] or done:
        log(con, now, "fetch", summary)
    return summary


def _fetch(con, cfg, s, now):
    last = get_state(con, "fetch.last")
    if last and now - mail._parse(last) < timedelta(minutes=s["poll_minutes"]):
        return
    fetch_now(con, cfg, now)


def _followups(con, cfg, s, now, local):
    today = local.date().isoformat()
    if get_state(con, "followups.date") == today or not _may_send_today(local, s):
        return
    if (local.hour, local.minute) < _hm(s["reminder_time"]):
        return
    # Read the mailbox first. A partner who answered an hour ago must not get
    # a reminder; if the mailbox cannot be read, no reminder goes out at all.
    read = get_state(con, "fetch.ok")
    if not read or now - mail._parse(read) > timedelta(minutes=2):
        fetch_now(con, cfg, now)
    r = mail.send_followups(con, cfg, now, prepare_requests.period_label)
    set_state(con, "followups.date", today)
    if r["sent"] or r["blocked"]:
        text = f"Στάλθηκαν {len(r['sent'])} υπενθυμίσεις"
        if r["sent"]:
            text += ": " + ", ".join(f"{name} ({n}η)" for name, n in r["sent"])
        if r["blocked"]:
            text += f". {len(r['blocked'])} δεν στάλθηκαν (χωρίς επιτρεπτή διεύθυνση)"
        log(con, now, "followups", text)
    if r["failed"]:
        log(con, now, "followups", "Ο mail server απέρριψε υπενθυμίσεις: " + "; ".join(f"{k}: {v}" for k, v in r["failed"].items()), "error", once=True)


def tick(con, cfg: Config, now=None) -> dict:
    """One pass of the scheduler. Returns the settings it ran with."""
    now = now or mail.utcnow()
    s = settings.apply(con, cfg)
    set_state(con, "scheduler.heartbeat", mail._iso(now))
    if not s["automation_enabled"]:
        return s
    local = now.astimezone(TZ)
    for job, fn in (
        ("cycle", lambda: _cycle(con, cfg, s, now, local)),
        ("fetch", lambda: _fetch(con, cfg, s, now)),
        ("followups", lambda: _followups(con, cfg, s, now, local)),
    ):
        # A job that just failed waits one mailbox interval before its next try,
        # instead of hitting a mail server that is down once a minute.
        failed = get_state(con, f"{job}.retry")
        if failed and timedelta(0) <= now - mail._parse(failed) < timedelta(minutes=s["poll_minutes"]):
            continue
        try:
            fn()
        except Exception as e:
            # One failing job (mail server down, bad file, anything unexpected)
            # must not stop the others.
            log(con, now, job, f"Αποτυχία: {e}", "error", once=True)
            set_state(con, f"{job}.retry", mail._iso(now))
    return s


def run_forever(connect, cfg: Config, stop=None, interval: int = HEARTBEAT_SECONDS):
    """connect: function returning a new database connection. Loops until stop
    (a threading.Event) is set."""
    while not (stop and stop.is_set()):
        con = connect()
        try:
            tick(con, cfg)
        except Exception as e:  # keep the loop alive whatever happens
            try:
                log(con, mail.utcnow(), "scheduler", f"Απρόσμενο σφάλμα: {e}", "error", once=True)
            except Exception:
                pass
        finally:
            con.close()
        if stop:
            stop.wait(interval)
        else:
            time.sleep(interval)


# ------------------------------------------------------------------ status
def status(con, cfg: Config, now=None) -> dict:
    """What the settings page shows: where things stand and what comes next."""
    now = now or mail.utcnow()
    s = settings.load(con, cfg)
    local = now.astimezone(TZ)
    period = previous_month(local.date())
    start = cycle_start(local.year, local.month, s)
    sent_any = con.execute("SELECT 1 FROM requests WHERE period=? AND status!='draft' LIMIT 1", (period,)).fetchone()
    if local < start:
        stage = "not_due"
    elif sent_any and not _drafts(con, period):
        stage = "done"
    elif not con.execute("SELECT 1 FROM assignments WHERE period=? LIMIT 1", (period,)).fetchone():
        stage = "waiting_file"
    elif _drafts(con, period) and s["require_approval"]:
        stage = "awaiting_approval"
    else:
        stage = "in_progress"
    nxt = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    beat = get_state(con, "scheduler.heartbeat")
    alive = bool(beat) and now - mail._parse(beat) < timedelta(seconds=3 * HEARTBEAT_SECONDS)
    last_fetch = get_state(con, "fetch.last")
    year_days = {}
    for y in (local.year, local.year + 1):
        year_days.update(workdays.holidays(y, s["greek_holidays"], settings.extra_dates(s)))
    return {
        "now": mail._iso(now),
        "scheduler_alive": alive,
        "mode": cfg.mode,
        "period": period,
        "cycle_start": mail._iso(start.astimezone(timezone.utc)),
        "next_cycle_start": mail._iso(cycle_start(nxt[0], nxt[1], s).astimezone(timezone.utc)),
        "stage": stage,
        "drafts": _drafts(con, period),
        "expected_file": f"input/…{period}….xlsx",
        "last_fetch": last_fetch,
        "reminders_today": get_state(con, "followups.date") == local.date().isoformat(),
        "sending_today": _may_send_today(local, s),
        "holidays": [
            {"date": d.isoformat(), "name": name}
            for d, name in sorted(year_days.items())
            if d >= local.date()
        ][:14],
        "runs": [
            dict(r) for r in con.execute("SELECT at, job, level, message FROM runs ORDER BY id DESC LIMIT 30")
        ],
    }
