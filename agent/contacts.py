"""Offer -> merchant -> email mapping, maintained by the team in an Excel file."""

import re
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

HEADERS = ["UUID", "Segment", "Admin Name", "Category", "Merchant", "Emails"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_PREFIXES = ["brunching_out", "generic", "green", "business", "family", "student", "horeca"]
_STOP_WORDS = {"έκπτωση", "δωρεάν", "μόνο", "μενού", "new", "online", "από", "έως", "επιπλέον", "προσφορά"}


def guess_merchant(admin_name: str) -> str:
    """Best-effort merchant name out of the offer's admin name, e.g.
    'family_kotsovolos_10% έκπτωση_31-12-2026' -> 'kotsovolos'. Only a
    suggestion for the person filling in the contacts file."""
    text = admin_name.strip()
    low = text.lower()
    for p in _PREFIXES:
        if low.startswith(p) and len(text) > len(p) and text[len(p)] in "_ -":
            text = text[len(p) + 1 :]
            break
    else:
        return ""  # unknown naming pattern: leave it for a human
    kept = []
    for tok in re.split(r"[_\s]+", text):
        if not tok:
            continue
        # A price, percentage, date or bare number ends the name; digits inside
        # a name (24home.gr, 3GUYS) do not.
        is_value = re.search(r"[%€]", tok) or re.fullmatch(r"[\d\-/.,+]+", tok)
        if is_value or tok.lower() in _STOP_WORDS:
            break
        kept.append(tok)
    return " ".join(kept)


def _slug(name: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or fallback


def export_contacts(con, out_path: Path, fake_emails: str | None = None) -> int:
    """fake_emails is a pattern such as '{slug}@partners.test': it fills the
    Emails column with made-up addresses, for testing without real contacts."""
    rows = con.execute(
        """SELECT o.uuid, o.segment, o.admin_name, o.category, m.name AS merchant,
                  (SELECT group_concat(email, '; ') FROM merchant_contacts c
                    WHERE c.merchant_id = o.merchant_id) AS emails
             FROM offers o LEFT JOIN merchants m ON m.id = o.merchant_id
            ORDER BY o.segment, o.admin_name"""
    ).fetchall()
    wb = Workbook()
    ws = wb.active
    ws.title = "contacts"
    ws.append(HEADERS)
    for c in ws[1]:
        c.font = Font(bold=True)
    for n, r in enumerate(rows, start=1):
        merchant = r["merchant"] or guess_merchant(r["admin_name"])
        emails = r["emails"] or ""
        if fake_emails and merchant:
            emails = fake_emails.format(slug=_slug(merchant, f"partner{n}"))
        ws.append([r["uuid"], r["segment"], r["admin_name"], r["category"], merchant, emails])
    for col, width in zip("ABCDEF", (34, 14, 60, 20, 28, 45)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    wb.save(out_path)
    return len(rows)


def import_contacts(con, path: Path) -> dict:
    """Apply the filled-in contacts file. Rows without merchant are left unmapped.
    A merchant's email list is the union of the emails on all of its rows."""
    ws = load_workbook(path, read_only=True, data_only=True).active
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h else "" for h in next(it)]
    missing = [h for h in ("UUID", "Merchant", "Emails") if h not in header]
    if missing:
        raise ValueError(f"Λείπουν οι στήλες {', '.join(missing)}")
    iu, im, ie = header.index("UUID"), header.index("Merchant"), header.index("Emails")

    stats = {"mapped": 0, "unmapped": 0, "unknown_uuid": [], "bad_emails": []}
    emails_by_merchant = {}
    with con:
        for n, row in enumerate(it, start=2):
            uuid = str(row[iu] or "").strip()
            if not uuid:
                continue
            if not con.execute("SELECT 1 FROM offers WHERE uuid = ?", (uuid,)).fetchone():
                stats["unknown_uuid"].append(uuid)
                continue
            merchant = " ".join(str(row[im] or "").split())
            if not merchant:
                con.execute("UPDATE offers SET merchant_id = NULL WHERE uuid = ?", (uuid,))
                stats["unmapped"] += 1
                continue
            found = con.execute(
                "SELECT id, name FROM merchants WHERE lower(name) = lower(?)", (merchant,)
            ).fetchone()
            if found:
                mid = found["id"]
            else:
                mid = con.execute("INSERT INTO merchants (name) VALUES (?)", (merchant,)).lastrowid
            con.execute("UPDATE offers SET merchant_id = ? WHERE uuid = ?", (mid, uuid))
            stats["mapped"] += 1
            bucket = emails_by_merchant.setdefault(mid, set())
            for e in re.split(r"[;,\s]+", str(row[ie] or "")):
                e = e.strip().lower()
                if not e:
                    continue
                if EMAIL_RE.match(e):
                    bucket.add(e)
                else:
                    stats["bad_emails"].append(f"γραμμή {n}: {e}")
        # The file is the source of truth for the merchants it mentions.
        for mid, emails in emails_by_merchant.items():
            con.execute("DELETE FROM merchant_contacts WHERE merchant_id = ?", (mid,))
            con.executemany(
                "INSERT INTO merchant_contacts (merchant_id, email) VALUES (?, ?)",
                [(mid, e) for e in sorted(emails)],
            )
    return stats
