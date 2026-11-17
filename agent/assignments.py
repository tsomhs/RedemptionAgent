"""Import the monthly assignments export (one sheet per segment)."""

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from openpyxl import load_workbook

REQUIRED = ["UUID", "Admin Name", "Category", "Assignments", "Unique Users", "Engage URL"]
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class IngestError(Exception):
    pass


@dataclass
class IngestResult:
    period: str
    offers_in_file: int = 0
    imported: int = 0
    skipped_zero: int = 0
    total_assignments: int = 0
    per_segment: dict = field(default_factory=dict)


def _to_int(value, what, where):
    if value is None or str(value).strip() == "":
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise IngestError(f"{where}: το πεδίο {what} δεν είναι αριθμός ({value!r})") from None
    if number < 0 or number != int(number):
        raise IngestError(f"{where}: μη έγκυρη τιμή {what} ({value!r})")
    return int(number)


def read_file(path: Path):
    """Return the offer rows of every sheet. Raises IngestError on bad data,
    so a broken export is never half-imported."""
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # not an Excel file, or one still being copied
        raise IngestError(f"Το αρχείο {Path(path).name} δεν διαβάζεται ως Excel ({type(e).__name__})") from None
    try:
        rows = _read_sheets(wb)
    finally:
        wb.close()  # read-only mode keeps the file open until told otherwise
    if not rows:
        raise IngestError("Το αρχείο δεν περιέχει καμία προσφορά")
    return rows


def _read_sheets(wb):
    rows, seen = [], {}
    for ws in wb.worksheets:
        it = ws.iter_rows(values_only=True)
        header = next(it, None)
        if header is None:
            continue
        cols = {str(h).strip(): i for i, h in enumerate(header) if h is not None}
        missing = [c for c in REQUIRED if c not in cols]
        if missing:
            raise IngestError(f"Φύλλο «{ws.title}»: λείπουν οι στήλες {', '.join(missing)}")
        for n, row in enumerate(it, start=2):
            if row is None or all(v is None or str(v).strip() == "" for v in row):
                continue
            where = f"Φύλλο «{ws.title}», γραμμή {n}"
            get = lambda c, row=row, cols=cols: row[cols[c]] if cols[c] < len(row) else None
            uuid = str(get("UUID") or "").strip()
            name = str(get("Admin Name") or "").strip()
            if not uuid or not name:
                raise IngestError(f"{where}: λείπει UUID ή Admin Name")
            if uuid in seen:
                raise IngestError(f"{where}: το UUID {uuid} υπάρχει ήδη ({seen[uuid]})")
            seen[uuid] = where
            rows.append(
                {
                    "uuid": uuid,
                    "segment": ws.title.strip(),
                    "admin_name": name,
                    "category": (str(get("Category")).strip() if get("Category") else None),
                    "assignments": _to_int(get("Assignments"), "Assignments", where),
                    "unique_users": _to_int(get("Unique Users"), "Unique Users", where),
                    "engage_url": (str(get("Engage URL")).strip() if get("Engage URL") else None),
                }
            )
    return rows


def check_period(con, rows, period: str, today: date):
    """Guard against the wrong file. The export carries no date, so two
    things are checked: the month must be over (a request covers a whole
    calendar month), and the file must not be a copy of another month's."""
    if period >= f"{today.year}-{today.month:02d}":
        raise IngestError(
            f"Ο μήνας {period} δεν έχει ολοκληρωθεί ακόμη· ο κύκλος αφορά ολόκληρο ημερολογιακό μήνα."
        )
    new = {r["uuid"]: r["assignments"] for r in rows if r["assignments"] > 0}
    other = {}
    for r in con.execute("SELECT period, offer_uuid, assignments FROM assignments WHERE period != ?", (period,)):
        other.setdefault(r["period"], {})[r["offer_uuid"]] = r["assignments"]
    same = sorted(p for p, values in other.items() if values == new)
    if same:
        raise IngestError(
            f"Το αρχείο έχει ακριβώς τα ίδια assignments με τον μήνα {same[-1]}· "
            f"μάλλον είναι το αρχείο εκείνου του μήνα και όχι του {period}."
        )


def ingest(con, path: Path, period: str, program: str, today: date | None = None, force: bool = False) -> IngestResult:
    """today: the date in Europe/Athens, for the wrong-file checks; force
    skips them (test data, or a deliberate reload)."""
    if not PERIOD_RE.match(period):
        raise IngestError(f"Ο μήνας πρέπει να έχει μορφή YYYY-MM (δόθηκε {period!r})")
    rows = read_file(Path(path))
    if today is not None and not force:
        check_period(con, rows, period, today)
    result = IngestResult(period=period, offers_in_file=len(rows))
    with con:
        for r in rows:
            if r["assignments"] <= 0:
                result.skipped_zero += 1
                continue
            # Never touch merchant_id here: the mapping belongs to the contacts file.
            con.execute(
                """INSERT INTO offers (uuid, program, segment, admin_name, category, engage_url)
                   VALUES (:uuid, :program, :segment, :admin_name, :category, :engage_url)
                   ON CONFLICT(uuid) DO UPDATE SET
                     program=excluded.program, segment=excluded.segment,
                     admin_name=excluded.admin_name, category=excluded.category,
                     engage_url=excluded.engage_url""",
                {**r, "program": program},
            )
            con.execute(
                """INSERT INTO assignments (offer_uuid, period, assignments, unique_users, source_file)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(offer_uuid, period) DO UPDATE SET
                     assignments=excluded.assignments, unique_users=excluded.unique_users,
                     source_file=excluded.source_file, imported_at=datetime('now')""",
                (r["uuid"], period, r["assignments"], r["unique_users"], Path(path).name),
            )
            con.execute(
                "INSERT OR IGNORE INTO redemptions (offer_uuid, period) VALUES (?, ?)",
                (r["uuid"], period),
            )
            result.imported += 1
            result.total_assignments += r["assignments"]
            result.per_segment[r["segment"]] = result.per_segment.get(r["segment"], 0) + 1
    return result
