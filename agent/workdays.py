"""Working days in Greece: Monday to Friday, minus public holidays.

The public holidays are computed (the movable ones follow Orthodox Easter).
The team can switch them off or add its own dates in the settings.
"""

from datetime import date, timedelta


def orthodox_easter(year: int) -> date:
    """Orthodox Easter Sunday in the Gregorian calendar (valid 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return date(year, month, day + 1) + timedelta(days=13)


def greek_holidays(year: int) -> dict:
    """date -> name of the public holidays of that year."""
    easter = orthodox_easter(year)
    return {
        date(year, 1, 1): "Πρωτοχρονιά",
        date(year, 1, 6): "Θεοφάνεια",
        easter - timedelta(days=48): "Καθαρά Δευτέρα",
        date(year, 3, 25): "25η Μαρτίου",
        easter - timedelta(days=2): "Μεγάλη Παρασκευή",
        easter + timedelta(days=1): "Δευτέρα του Πάσχα",
        date(year, 5, 1): "Πρωτομαγιά",
        easter + timedelta(days=50): "Αγίου Πνεύματος",
        date(year, 8, 15): "Δεκαπενταύγουστος",
        date(year, 10, 28): "28η Οκτωβρίου",
        date(year, 12, 25): "Χριστούγεννα",
        date(year, 12, 26): "Δεύτερη μέρα Χριστουγέννων",
    }


def holidays(year: int, use_greek: bool, extra) -> dict:
    out = dict(greek_holidays(year)) if use_greek else {}
    for d in extra or []:
        if d.year == year:
            out.setdefault(d, "Αργία (ρυθμίσεις)")
    return out


def is_working_day(d: date, use_greek: bool = True, extra=()) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year, use_greek, extra)


def on_or_after(d: date, use_greek: bool = True, extra=()) -> date:
    """d itself if it is a working day, otherwise the next one."""
    while not is_working_day(d, use_greek, extra):
        d += timedelta(days=1)
    return d


def first_working_day(year: int, month: int, use_greek: bool = True, extra=()) -> date:
    return on_or_after(date(year, month, 1), use_greek, extra)
