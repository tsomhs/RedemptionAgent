"""Read the team's existing Redemptions_Report.xlsx (one sheet per month)."""

import hashlib
import re
from pathlib import Path

from openpyxl import load_workbook

from .contacts import guess_merchant

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]  # fmt: skip


def _norm(name) -> str:
    return " ".join(str(name).split()).lower()


def redeemed_by_offer(path: Path, period: str) -> dict:
    """Offer name (normalised) -> redeemed, for the sheet of that period.
    Offers whose Redeemed cell is empty are left out: empty means 'no answer',
    which is not the same as 0."""
    year, month = period.split("-")
    sheet = f"{_MONTHS[int(month) - 1]} {year}"
    wb = load_workbook(path, read_only=True, data_only=True)
    if sheet not in wb.sheetnames:
        raise ValueError(f"Το report δεν έχει φύλλο «{sheet}»")
    out = {}
    for row in wb[sheet].iter_rows(min_row=3, values_only=True):
        if len(row) < 4 or not row[1] or row[3] is None or str(row[3]).strip() == "":
            continue
        out[_norm(row[1])] = int(float(row[3]))
    return out


def lookup(values: dict, admin_name: str):
    return values.get(_norm(admin_name))


# ------------------------------------------------------------ whole history
def _whole(value, what, where):
    if value is None or str(value).strip() == "":
        return None
    number = float(value)
    if number < 0 or number != int(number):
        raise ValueError(f"{where}: μη έγκυρη τιμή {what} ({value!r})")
    return int(number)


def read_history(path: Path) -> dict:
    """period -> list of rows (offer, category, assigned, redeemed) for every
    month sheet of the report. The Category cell is filled only on the first
    row of each group, so it carries down. Rows without an offer name (the
    totals line at the bottom) are skipped."""
    wb = load_workbook(path, read_only=True, data_only=True)
    out = {}
    try:
        for ws in wb.worksheets:
            m = re.fullmatch(r"(\w+) (\d{4})", ws.title.strip())
            if not m or m.group(1) not in _MONTHS:
                continue
            period = f"{m.group(2)}-{_MONTHS.index(m.group(1)) + 1:02d}"
            rows, category, started = [], None, False
            for n, row in enumerate(ws.iter_rows(values_only=True), start=1):
                row = tuple(row) + (None,) * 4
                if not started:
                    started = str(row[1] or "").strip() == "Offer"
                    continue
                if row[0] and str(row[0]).strip() and row[1]:
                    category = str(row[0]).strip()
                if not row[1] or not str(row[1]).strip():
                    continue
                where = f"Φύλλο «{ws.title}», γραμμή {n}"
                rows.append({
                    "offer": " ".join(str(row[1]).split()),
                    "category": category,
                    "assigned": _whole(row[2], "Assigned", where) or 0,
                    "redeemed": _whole(row[3], "Redeemed", where),
                })  # fmt: skip
            out[period] = rows
    finally:
        wb.close()
    return out


def history_id(offer_name: str) -> str:
    """Stand-in id for an offer that exists only in the old report, which has
    no UUID column. Recognisable as not being an Engage UUID."""
    return "report-" + hashlib.sha1(_norm(offer_name).encode("utf-8")).hexdigest()[:20]


def import_history(con, path: Path, program: str, now_iso: str, periods=None) -> dict:
    """Load the months of the hand-made report: assigned and redeemed per offer.

    Offers are matched to known ones by exact name. An offer the database does
    not know gets a stand-in id (history_id) and is tied to a partner only when
    the name inside it is exactly an existing partner. Nothing already in the
    database is overwritten, so running this again changes nothing."""
    data = read_history(Path(path))
    source = f"report:{Path(path).name}"
    known = {_norm(r["admin_name"]): r["uuid"] for r in con.execute("SELECT uuid, admin_name FROM offers")}
    merchants = {r["name"].lower(): r["id"] for r in con.execute("SELECT id, name FROM merchants")}
    stats = {}
    with con:
        for period in sorted(data):
            if periods and period not in periods:
                continue
            st = stats[period] = {"rows": 0, "new_offers": 0, "assigned": 0, "redeemed": 0, "empty": 0, "kept": 0}
            for r in data[period]:
                st["rows"] += 1
                uuid = known.get(_norm(r["offer"]))
                if uuid is None:
                    uuid = known[_norm(r["offer"])] = history_id(r["offer"])
                    segment = re.split(r"[_ ]", r["offer"], maxsplit=1)[0].lower() or "report"
                    con.execute(
                        """INSERT OR IGNORE INTO offers (uuid, program, segment, admin_name, category, merchant_id)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (uuid, program, segment, r["offer"], r["category"],
                         merchants.get(guess_merchant(r["offer"]).lower())),
                    )  # fmt: skip
                    st["new_offers"] += 1
                con.execute(
                    """INSERT OR IGNORE INTO assignments (offer_uuid, period, assignments, source_file)
                       VALUES (?, ?, ?, ?)""",
                    (uuid, period, r["assigned"], Path(path).name),
                )
                st["assigned"] += r["assigned"]
                old = con.execute(
                    "SELECT redeemed FROM redemptions WHERE offer_uuid=? AND period=?", (uuid, period)
                ).fetchone()
                if r["redeemed"] is None:
                    con.execute("INSERT OR IGNORE INTO redemptions (offer_uuid, period) VALUES (?, ?)", (uuid, period))
                    st["empty"] += 1
                elif old is not None and old["redeemed"] is not None:
                    st["kept"] += 1
                else:
                    con.execute(
                        """INSERT INTO redemptions (offer_uuid, period, redeemed, status, source, updated_at)
                           VALUES (?, ?, ?, 'reported', ?, ?)
                           ON CONFLICT(offer_uuid, period) DO UPDATE SET
                             redeemed=excluded.redeemed, status='reported',
                             source=excluded.source, updated_at=excluded.updated_at""",
                        (uuid, period, r["redeemed"], source, now_iso),
                    )
                    con.execute(
                        """INSERT INTO redemption_history (offer_uuid, period, old_value, new_value, source, changed_at)
                           VALUES (?, ?, NULL, ?, ?, ?)""",
                        (uuid, period, r["redeemed"], source, now_iso),
                    )
                    st["redeemed"] += r["redeemed"]
    return stats
