"""The report: one row per offer and month, the indicators, and the Excel export.

The dashboard and the Excel file are both built from rows() and summary(), so
their filters and totals cannot disagree.
"""

import calendar
import unicodedata
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .prepare_requests import MONTHS_ACC

# What the team sees for an offer in a month.
REPORTED = "Δηλώθηκε"
REVIEW = "Προς έλεγχο"
PENDING = "Εκκρεμεί απάντηση"
NOT_ASKED = "Δεν έχει σταλεί αίτημα"
UNDELIVERED = "Ανεπίδοτο email"
NO_EMAIL = "Συνεργάτης χωρίς email"
NO_MERCHANT = "Χωρίς συνεργάτη"
STATUSES = [REPORTED, REVIEW, PENDING, NOT_ASKED, UNDELIVERED, NO_EMAIL, NO_MERCHANT]

MONTHS_NOM = [
    "Ιανουάριος", "Φεβρουάριος", "Μάρτιος", "Απρίλιος", "Μάιος", "Ιούνιος",
    "Ιούλιος", "Αύγουστος", "Σεπτέμβριος", "Οκτώβριος", "Νοέμβριος", "Δεκέμβριος",
]  # fmt: skip
assert len(MONTHS_NOM) == len(MONTHS_ACC)


def month_name(period: str) -> str:
    year, month = period.split("-")
    return f"{MONTHS_NOM[int(month) - 1]} {year}"


def _row_status(r) -> str:
    if r["redemption_status"] == "needs_review" or r["request_status"] == "replied":
        return REVIEW if r["redeemed"] is None or r["redemption_status"] == "needs_review" else REPORTED
    if r["redeemed"] is not None:
        return REPORTED
    if r["merchant"] is None:
        return NO_MERCHANT
    if r["request_status"] == "undelivered":
        return UNDELIVERED
    if r["request_status"] in (None, "draft"):
        return NOT_ASKED if r["contacts"] else NO_EMAIL
    return PENDING


def rows(con, f: dict | None = None) -> list:
    """Offer rows for the filters. Keys of f (all optional): program,
    period_from, period_to, merchant, category, status, q (text in the offer)."""
    f = {k: v for k, v in (f or {}).items() if v not in (None, "")}
    where, args = ["a.assignments > 0"], []
    for key, sql in (
        ("program", "o.program = ?"),
        ("period_from", "a.period >= ?"),
        ("period_to", "a.period <= ?"),
        ("merchant", "m.name = ?"),
        ("category", "o.category = ?"),
    ):
        if key in f:
            where.append(sql)
            args.append(f[key])
    if "q" in f:
        where.append("lower(o.admin_name) LIKE ?")
        args.append(f"%{str(f['q']).lower()}%")
    found = con.execute(
        f"""SELECT a.period, o.program, m.name AS merchant, o.uuid, o.admin_name AS offer,
                   o.category, o.segment, a.assignments AS assigned, d.redeemed,
                   d.status AS redemption_status, d.source, r.id AS request_id,
                   r.status AS request_status, r.followups_sent,
                   (SELECT count(*) FROM merchant_contacts c WHERE c.merchant_id = o.merchant_id) AS contacts
              FROM assignments a
              JOIN offers o ON o.uuid = a.offer_uuid
              LEFT JOIN merchants m ON m.id = o.merchant_id
              LEFT JOIN redemptions d ON d.offer_uuid = a.offer_uuid AND d.period = a.period
              LEFT JOIN requests r ON r.merchant_id = o.merchant_id AND r.period = a.period
                                  AND r.program = o.program
                                  AND EXISTS (SELECT 1 FROM request_offers ro
                                               WHERE ro.request_id = r.id AND ro.offer_uuid = o.uuid)
             WHERE {' AND '.join(where)}
             ORDER BY a.period DESC, m.name IS NULL, lower(m.name), o.admin_name""",
        args,
    ).fetchall()
    out = []
    for r in found:
        row = dict(r)
        row["status"] = _row_status(r)
        # Not a match of the same coupons, so it is not capped at 100%.
        row["rate"] = None if r["redeemed"] is None or not r["assigned"] else r["redeemed"] / r["assigned"]
        out.append(row)
    if "status" in f:
        out = [r for r in out if r["status"] == f["status"]]
    return out


def _top(items: dict, n=10) -> list:
    ranked = sorted(items.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"name": k, "redeemed": v} for k, v in ranked[:n] if v > 0]


def summary(report_rows: list, con=None, max_followups: int = 3) -> dict:
    """Indicators for exactly the given rows. Requests are counted once each,
    through the rows that belong to them."""
    reported = [r for r in report_rows if r["redeemed"] is not None]
    total = sum(r["redeemed"] for r in reported)
    assigned_reported = sum(r["assigned"] for r in reported)

    months = {}
    for r in report_rows:
        m = months.setdefault(
            r["period"],
            {"period": r["period"], "label": month_name(r["period"]), "offers": 0, "reported": 0,
             "assigned": 0, "redeemed": 0, "open": 0},
        )  # fmt: skip
        m["offers"] += 1
        m["assigned"] += r["assigned"]
        if r["redeemed"] is not None:
            m["reported"] += 1
            m["redeemed"] += r["redeemed"]
        elif r["status"] in (PENDING, REVIEW, UNDELIVERED):
            m["open"] += 1
    monthly = [months[k] for k in sorted(months)]
    for m in monthly:
        if not m["reported"]:
            m["redeemed"] = None  # nobody answered yet: unknown, not zero
    with_data = [m for m in monthly if m["reported"]]

    by_merchant, by_offer = {}, {}
    for r in reported:
        by_merchant[r["merchant"] or "(χωρίς συνεργάτη)"] = by_merchant.get(r["merchant"] or "(χωρίς συνεργάτη)", 0) + r["redeemed"]
        by_offer[r["offer"]] = by_offer.get(r["offer"], 0) + r["redeemed"]

    requests = {}
    for r in report_rows:
        if r["request_id"] and r["request_status"] != "draft":
            requests[r["request_id"]] = r
    count = lambda *st: sum(1 for r in requests.values() if r["request_status"] in st)
    # "Sent" means it reached the partner: an undelivered request is a contact
    # problem, not a communication.
    sent = len(requests) - count("undelivered")
    return {
        "offers": len(report_rows),
        "reported": len(reported),
        "coverage": len(reported) / len(report_rows) if report_rows else None,
        "total_redeemed": total,
        "total_assigned": sum(r["assigned"] for r in report_rows),
        # Redeemed / Assigned over the offers that have an answer: mixing in the
        # assignments of unanswered offers would understate the rate.
        "rate": total / assigned_reported if assigned_reported else None,
        "months_with_data": len(with_data),
        "monthly_average": total / len(with_data) if with_data else None,
        "monthly": monthly,
        "top_merchants": _top(by_merchant),
        "top_offers": _top(by_offer),
        "requests": {
            "sent": sent,
            "completed": count("completed"),
            "response_rate": count("completed") / sent if sent else None,
            "pending": count("sent"),
            "partial": count("partial"),
            "review": count("replied"),
            "undelivered": count("undelivered"),
            "exhausted": sum(
                1 for r in requests.values()
                if r["request_status"] in ("sent", "partial") and r["followups_sent"] >= max_followups
            ),
        },
    }


def options(con) -> dict:
    """Values for the filter controls."""
    col = lambda sql: [r[0] for r in con.execute(sql) if r[0]]
    periods = col("SELECT DISTINCT period FROM assignments ORDER BY period")
    return {
        "programs": col("SELECT DISTINCT program FROM offers ORDER BY program"),
        "periods": [{"value": p, "label": month_name(p)} for p in periods],
        "merchants": col("SELECT name FROM merchants ORDER BY lower(name)"),
        "categories": col("SELECT DISTINCT category FROM offers ORDER BY category"),
        "statuses": STATUSES,
    }


# ---------------------------------------------------------------------- Excel
_HEAD = ["Μήνας", "Πρόγραμμα", "Συνεργάτης", "UUID προσφοράς", "Offer", "Category",
         "Assigned", "Redeemed", "Redemption Rate", "Κατάσταση"]  # fmt: skip
_BOLD = Font(bold=True)
_FILL = PatternFill("solid", fgColor="E8EEF7")


def _header(ws, titles):
    ws.append(titles)
    for c in ws[ws.max_row]:
        c.font, c.fill = _BOLD, _FILL
        c.alignment = Alignment(vertical="center", wrap_text=True)


# The team's own report (Redemptions_Report.xlsx) has one sheet per month, the
# offers grouped under a label and sorted by Assigned, and a subtotal line per
# group. The month sheets below follow it. The label is the offer's category
# for the general programme and the programme segment for the rest.
_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]  # fmt: skip
_SEGMENT_LABEL = {"business": "Business", "sustainability": "Green", "green": "Green",
                  "student": "Student", "family": "Family", "horeca": "HORECA"}  # fmt: skip
_GROUP_ORDER = ["Μόδα", "Απόδραση", "Εκπαίδευση", "Παιδί", "Σπίτι", "Τεχνολογία", "Υγεία - Ευεξία",
                "Υπηρεσίες", "Φαγητό & Ρόφημα", "Ψυχαγωγία", "Brunching out", "Αυτοκίνητο", "Business",
                "Green", "Student", "Family", "Προσωπική φροντίδα", "Εστιατόρια", "HORECA"]  # fmt: skip
_ORDER = {g.lower(): i for i, g in enumerate(_GROUP_ORDER)}
_TITLE_FILL = PatternFill("solid", fgColor="002060")
_HEAD_FILL = PatternFill("solid", fgColor="D9E2F3")
_GROUP_FILL = PatternFill("solid", fgColor="EFEFEF")
_SUB_FILL = PatternFill("solid", fgColor="FFF2CC")


def group_label(row) -> str:
    """Which block of the month sheet an offer goes under."""
    # NFC: the same Greek word can arrive with its accents stored in two ways.
    category = unicodedata.normalize("NFC", " ".join(str(row["category"] or "").split()))
    # Offers that came from the old report carry its block label as category.
    if not str(row["uuid"]).startswith("report-"):
        label = _SEGMENT_LABEL.get(str(row["segment"] or "").lower())
        if label:
            return label
    known = _ORDER.get(category.lower())
    return _GROUP_ORDER[known] if known is not None else (category or "Χωρίς κατηγορία")


def _month_sheet(wb, period, month_rows):
    year, month = (int(x) for x in period.split("-"))
    ws = wb.create_sheet(f"{_MONTHS_EN[month - 1]} {year}")
    ws.append([f"01/{month:02d} - {calendar.monthrange(year, month)[1]}/{month:02d}"])
    ws.merge_cells("A1:E1")
    ws["A1"].font, ws["A1"].fill = Font(bold=True, color="FFFFFF"), _TITLE_FILL
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.append(["Category", "Offer", "Assigned", "Redeemed", "Redemption Rate", "Συνεργάτης", "Κατάσταση"])
    for c in ws[2]:
        c.font, c.fill = _BOLD, _HEAD_FILL
    groups = {}
    for r in month_rows:
        groups.setdefault(group_label(r), []).append(r)
    for label in sorted(groups, key=lambda g: (_ORDER.get(g.lower(), len(_ORDER)), g.lower())):
        items = sorted(groups[label], key=lambda r: (-r["assigned"], r["offer"].lower()))
        first = ws.max_row + 1
        for r in items:
            n = ws.max_row + 1
            # An offer with no answer keeps Redeemed and its rate empty: empty
            # is "we do not know", which is not 0%.
            rate = f"=D{n}/C{n}" if r["redeemed"] is not None and r["assigned"] else None
            ws.append([label if n == first else None, r["offer"], r["assigned"], r["redeemed"], rate, r["merchant"], r["status"]])
            ws.cell(n, 3).number_format = "#,##0"
            ws.cell(n, 5).number_format = "0.00%"
        last = ws.max_row
        ws.cell(first, 1).fill = _GROUP_FILL
        ws.cell(first, 1).alignment = Alignment(vertical="top")
        if last > first:
            ws.merge_cells(start_row=first, start_column=1, end_row=last, end_column=1)
        n = last + 1
        # Like the offer rows: with no answer in the block the subtotal stays
        # empty instead of showing 0, and the rate counts only the coupons of
        # the offers that did answer, as the dashboard does.
        d, c = f"D{first}:D{last}", f"C{first}:C{last}"
        ws.append([
            None, None, f"=SUM({c})", f'=IF(COUNT({d})=0,"",SUM({d}))',
            f'=IF(COUNT({d})=0,"",SUM({d})/SUMIF({d},"<>",{c}))',
        ])  # fmt: skip
        for c in ws[n][:4]:
            c.font, c.fill = _BOLD, _SUB_FILL
        ws.cell(n, 3).number_format = "#,##0"
        ws.cell(n, 5).number_format = "0.00%"
    for col, width in zip("ABCDEFG", (22, 70, 12, 12, 17, 26, 24)):
        ws.column_dimensions[col].width = width
    return ws


def export_xlsx(report_rows: list, s: dict, filters: dict | None = None) -> bytes:
    """One sheet per month laid out like the team's report, then 'Σύνοψη' with
    the indicators and 'Αναλυτικά' with the same offer rows as one flat,
    filterable list (offer rows only, so summing a column never double counts)."""
    wb = Workbook()
    wb.remove(wb.active)
    by_month = {}
    for r in report_rows:
        by_month.setdefault(r["period"], []).append(r)
    for period in sorted(by_month):
        _month_sheet(wb, period, by_month[period])

    sm = wb.create_sheet("Σύνοψη")
    _header(sm, ["Δείκτης", "Τιμή"])
    pct = lambda v: None if v is None else v
    lines = [
        ("Προσφορές στην επιλογή", s["offers"], None),
        ("Προσφορές με δηλωμένες εξαργυρώσεις", s["reported"], None),
        ("Κάλυψη απαντήσεων", pct(s["coverage"]), "0.0%"),
        ("Σύνολο εξαργυρώσεων", s["total_redeemed"], None),
        ("Σύνολο Assigned", s["total_assigned"], None),
        ("Redemption Rate (στις προσφορές με απάντηση)", pct(s["rate"]), "0.0%"),
        ("Μήνες με δεδομένα", s["months_with_data"], None),
        ("Μέσο μηνιαίο πλήθος εξαργυρώσεων", s["monthly_average"], "0.0"),
        ("Αιτήματα που στάλθηκαν", s["requests"]["sent"], None),
        ("Αιτήματα ολοκληρωμένα", s["requests"]["completed"], None),
        ("Ανταπόκριση", pct(s["requests"]["response_rate"]), "0.0%"),
        ("Αιτήματα σε αναμονή απάντησης", s["requests"]["pending"], None),
        ("Αιτήματα με μερική απάντηση", s["requests"]["partial"], None),
        ("Αιτήματα προς έλεγχο", s["requests"]["review"], None),
        ("Αιτήματα ανεπίδοτα", s["requests"]["undelivered"], None),
        ("Αιτήματα με εξαντλημένες υπενθυμίσεις", s["requests"]["exhausted"], None),
    ]
    for label, value, fmt in lines:
        sm.append([label, value])
        if fmt:
            sm.cell(sm.max_row, 2).number_format = fmt
    sm.append([])
    _header(sm, ["Μήνας", "Προσφορές", "Με απάντηση", "Assigned", "Redeemed", "Ανοικτές εκκρεμότητες"])
    for m in s["monthly"]:
        sm.append([m["label"], m["offers"], m["reported"], m["assigned"], m["redeemed"], m["open"]])
    active = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
    if active:
        sm.append([])
        _header(sm, ["Φίλτρο", "Τιμή"])
        names = {"program": "Πρόγραμμα", "period_from": "Από μήνα", "period_to": "Έως μήνα",
                 "merchant": "Συνεργάτης", "category": "Category", "status": "Κατάσταση", "q": "Κείμενο προσφοράς"}  # fmt: skip
        for k, v in active.items():
            sm.append([names.get(k, k), v])
    for col, width in zip("ABCDEF", (46, 16, 14, 12, 12, 24)):
        sm.column_dimensions[col].width = width

    ws = wb.create_sheet("Αναλυτικά")
    _header(ws, _HEAD)
    for r in report_rows:
        ws.append([
            month_name(r["period"]), r["program"], r["merchant"], r["uuid"], r["offer"], r["category"],
            r["assigned"], r["redeemed"], r["rate"], r["status"],
        ])  # fmt: skip
        ws.cell(ws.max_row, 9).number_format = "0.0%"
    for i, width in enumerate((16, 26, 26, 38, 52, 20, 11, 11, 12, 24), start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    if report_rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(_HEAD))}{ws.max_row}"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
