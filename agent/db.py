"""SQLite storage for the redemptions agent."""

import sqlite3
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS merchant_contacts (
    merchant_id INTEGER NOT NULL REFERENCES merchants(id),
    email       TEXT NOT NULL,
    PRIMARY KEY (merchant_id, email)
);

-- One row per offer of the Engage backend. merchant_id stays NULL until the
-- team maps the offer to a merchant through the contacts file.
CREATE TABLE IF NOT EXISTS offers (
    uuid        TEXT PRIMARY KEY,
    program     TEXT NOT NULL,
    segment     TEXT NOT NULL,          -- sheet of origin (generic, family, ...)
    admin_name  TEXT NOT NULL,
    category    TEXT,
    engage_url  TEXT,
    merchant_id INTEGER REFERENCES merchants(id)
);

-- Coupons handed out per offer per period (period = 'YYYY-MM').
CREATE TABLE IF NOT EXISTS assignments (
    offer_uuid   TEXT NOT NULL REFERENCES offers(uuid),
    period       TEXT NOT NULL,
    assignments  INTEGER NOT NULL,
    unique_users INTEGER,
    source_file  TEXT,
    imported_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (offer_uuid, period)
);

-- One request per merchant, program and period. The UNIQUE constraint is what
-- makes preparing the requests twice safe: it can never create a second one.
CREATE TABLE IF NOT EXISTS requests (
    id             INTEGER PRIMARY KEY,
    program        TEXT NOT NULL,
    merchant_id    INTEGER NOT NULL REFERENCES merchants(id),
    period         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft',   -- draft | sent | undelivered | replied | partial | completed
    subject        TEXT NOT NULL,
    body           TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at        TEXT,
    followups_sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE (program, merchant_id, period)
);

CREATE TABLE IF NOT EXISTS request_offers (
    request_id INTEGER NOT NULL REFERENCES requests(id),
    offer_uuid TEXT NOT NULL REFERENCES offers(uuid),
    PRIMARY KEY (request_id, offer_uuid)
);

-- Every email of a request, both directions. message_id is UNIQUE so the same
-- email can never be stored (and later counted) twice. request_id is NULL for
-- incoming mail that could not be tied to a request.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    request_id  INTEGER REFERENCES requests(id),
    direction   TEXT NOT NULL,          -- out | in
    kind        TEXT NOT NULL,          -- out: initial | resend | followup | clarification
                                        -- in:  reply | auto | bounce | ignored
    message_id  TEXT NOT NULL UNIQUE,
    in_reply_to TEXT,
    from_addr   TEXT,
    to_addrs    TEXT,
    subject     TEXT,
    body        TEXT,
    attachments TEXT,
    at          TEXT NOT NULL,
    processed_at TEXT,                  -- when a reply was read into redemptions
    parse_note   TEXT,
    -- 1 while a person still has to look at this reply (nothing usable in it,
    -- or it contradicts what is recorded), whatever the state of its request.
    needs_review INTEGER NOT NULL DEFAULT 0
);

-- redeemed stays NULL until the merchant reports a number. NULL (no answer)
-- and 0 (merchant said zero) are different things and must stay different.
CREATE TABLE IF NOT EXISTS redemptions (
    offer_uuid TEXT NOT NULL REFERENCES offers(uuid),
    period     TEXT NOT NULL,
    redeemed   INTEGER,
    status     TEXT NOT NULL DEFAULT 'pending',     -- pending | reported | needs_review
    source     TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (offer_uuid, period)
);

CREATE TABLE IF NOT EXISTS redemption_history (
    id         INTEGER PRIMARY KEY,
    offer_uuid TEXT NOT NULL,
    period     TEXT NOT NULL,
    old_value  INTEGER,
    new_value  INTEGER,
    source     TEXT,
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Settings changed from the dashboard (value is JSON). They override the env file.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- What the scheduler has already done (e.g. 'cycle.2026-10.done'), so that a
-- restart or a repeated pass does not do it again.
CREATE TABLE IF NOT EXISTS automation_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- What the scheduler did and when; shown on the settings page.
CREATE TABLE IF NOT EXISTS runs (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    job     TEXT NOT NULL,              -- cycle | fetch | followups | scheduler
    level   TEXT NOT NULL,              -- info | error
    message TEXT NOT NULL
);
"""


def connect(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    # Databases made before a column existed get it here.
    if "needs_review" not in {r[1] for r in con.execute("PRAGMA table_info(messages)")}:
        con.execute("ALTER TABLE messages ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0")
    return con
