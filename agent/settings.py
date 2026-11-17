"""Settings the admin changes from the dashboard. They live in the database
and take precedence over the env file, which only supplies the first values.
"""

import json
import re
from datetime import date

from .config import Config

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    pass


def defaults(cfg: Config) -> dict:
    return {
        # Master switch. Off: nothing runs by itself; the commands still work.
        "automation_enabled": False,
        # --- monthly cycle
        "cycle_day_rule": "first_working_day",   # first_working_day | day_of_month
        "cycle_day": 1,                          # with day_of_month; moved to the next working day
        "cycle_time": "09:00",                   # Europe/Athens
        "require_approval": True,                # drafts wait for "send" in the dashboard
        # --- reminders
        "followup_days": cfg.followup_days,
        "max_followups": cfg.max_followups,
        "reminder_time": "10:00",
        # --- replies
        "poll_minutes": 30,
        # --- working days
        "working_days_only": True,               # no emails on weekends and holidays
        "greek_holidays": True,
        "extra_holidays": [],                    # ISO dates
    }


def _int(value, lo, hi, label):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise SettingsError(f"{label}: ακέραιος από {lo} έως {hi}")
    return value


def _bool(value, label):
    if not isinstance(value, bool):
        raise SettingsError(f"{label}: ναι ή όχι")
    return value


def _time(value, label):
    if not isinstance(value, str) or not _TIME.match(value):
        raise SettingsError(f"{label}: ώρα στη μορφή ΩΩ:ΛΛ")
    return value


def _dates(value, label):
    if not isinstance(value, list):
        raise SettingsError(f"{label}: λίστα ημερομηνιών")
    out = set()
    for item in value:
        try:
            out.add(date.fromisoformat(str(item).strip()).isoformat())
        except ValueError:
            raise SettingsError(f"{label}: «{item}» δεν είναι ημερομηνία της μορφής ΕΕΕΕ-ΜΜ-ΗΗ") from None
    return sorted(out)


def _rule(value, label):
    if value not in ("first_working_day", "day_of_month"):
        raise SettingsError(f"{label}: μη έγκυρη επιλογή")
    return value


_CHECKS = {
    "automation_enabled": lambda v: _bool(v, "Αυτοματισμός"),
    "cycle_day_rule": lambda v: _rule(v, "Έναρξη κύκλου"),
    "cycle_day": lambda v: _int(v, 1, 28, "Ημέρα του μήνα"),
    "cycle_time": lambda v: _time(v, "Ώρα έναρξης κύκλου"),
    "require_approval": lambda v: _bool(v, "Έγκριση πριν την αποστολή"),
    "followup_days": lambda v: _int(v, 1, 60, "Ημέρες μεταξύ υπενθυμίσεων"),
    "max_followups": lambda v: _int(v, 0, 10, "Πλήθος υπενθυμίσεων"),
    "reminder_time": lambda v: _time(v, "Ώρα υπενθυμίσεων"),
    "poll_minutes": lambda v: _int(v, 5, 1440, "Έλεγχος εισερχομένων"),
    "working_days_only": lambda v: _bool(v, "Μόνο εργάσιμες"),
    "greek_holidays": lambda v: _bool(v, "Ελληνικές αργίες"),
    "extra_holidays": lambda v: _dates(v, "Επιπλέον αργίες"),
}


def load(con, cfg: Config) -> dict:
    """Effective settings: the defaults, overridden by what was saved."""
    out = defaults(cfg)
    for row in con.execute("SELECT key, value FROM settings"):
        if row["key"] in out:
            out[row["key"]] = json.loads(row["value"])
    return out


def save(con, cfg: Config, changes: dict) -> dict:
    """Validate and store. Nothing is written unless every value is valid."""
    unknown = [k for k in changes if k not in _CHECKS]
    if unknown:
        raise SettingsError(f"Άγνωστη ρύθμιση: {', '.join(unknown)}")
    clean = {k: _CHECKS[k](v) for k, v in changes.items()}
    with con:
        for k, v in clean.items():
            con.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)),
            )
    return load(con, cfg)


def apply(con, cfg: Config) -> dict:
    """Load the settings and copy the ones the rest of the code reads from
    cfg (reminder interval and count). Called at the start of every command,
    web request and scheduler pass."""
    s = load(con, cfg)
    cfg.followup_days, cfg.max_followups = s["followup_days"], s["max_followups"]
    return s


def extra_dates(s: dict) -> list:
    return [date.fromisoformat(d) for d in s["extra_holidays"]]
