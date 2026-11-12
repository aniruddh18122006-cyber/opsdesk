"""Database setup for OpsDesk: connection, schema loading, seeding.

Schema:  database/schema.sql is used when it exists, otherwise FALLBACK_SCHEMA
         below (so the API runs before Nandini shares hers).
Seed:    database/seed.py is used when it exists and defines seed(conn),
         otherwise a small built-in demo data set is loaded.
Both only run on an empty database; delete opsdesk.db to start over.
"""
import importlib.util
import logging
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import rules

log = logging.getLogger("opsdesk")

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
SCHEMA_FILE = ROOT_DIR / "database" / "schema.sql"
SEED_FILE = ROOT_DIR / "database" / "seed.py"

FALLBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    region      TEXT,
    agency      TEXT,
    available   INTEGER NOT NULL DEFAULT 1,
    shift_start TEXT,            -- optional working hours, "HH:MM" local time
    shift_end   TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description  TEXT NOT NULL DEFAULT '',
    agency       TEXT NOT NULL,
    tboh         TEXT,
    priority     TEXT NOT NULL CHECK (priority IN ('Urgent','High','Normal','Low')),
    region       TEXT,
    intent       TEXT,
    check_in     TEXT NOT NULL,
    lcd          TEXT,
    status       TEXT NOT NULL DEFAULT 'New'
                 CHECK (status IN ('New','Open','Pending','Escalated','Resolved','Closed')),
    assignee_id  INTEGER REFERENCES users(id),
    route_reason TEXT,
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_tboh     ON tickets(tboh);
CREATE INDEX IF NOT EXISTS idx_tickets_agency   ON tickets(agency, intent);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee ON tickets(assignee_id, status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id),
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    actor_id   INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id);

CREATE TABLE IF NOT EXISTS side_conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id),
    kind       TEXT NOT NULL CHECK (kind IN ('internal','external')),
    recipients TEXT NOT NULL DEFAULT '[]',   -- JSON list, owned by this thread
    body       TEXT NOT NULL,
    author_id  INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_side_ticket ON side_conversations(ticket_id);
"""


def load_env(path=None) -> None:
    """Read KEY=VALUE lines from backend/.env into the environment (no package needed)."""
    path = Path(path) if path else BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def default_db_path() -> str:
    raw = os.getenv("OPSDESK_DB", "opsdesk.db")
    path = Path(raw)
    return str(path if path.is_absolute() else BASE_DIR / path)


def connect(path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI may run a request's steps on different
    # threads, but each connection is only ever used by one request at a time.
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _table_exists(conn, name) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def init_db(path, schema_file=SCHEMA_FILE, seed_file=SEED_FILE) -> None:
    """Create tables (if missing) and seed an empty database."""
    conn = connect(path)
    try:
        if not _table_exists(conn, "tickets"):
            if schema_file and Path(schema_file).exists():
                log.info("Loading schema from %s", schema_file)
                conn.executescript(Path(schema_file).read_text(encoding="utf-8"))
            else:
                conn.executescript(FALLBACK_SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            if not _run_external_seed(conn, seed_file):
                seed_demo(conn)
    finally:
        conn.close()


def _run_external_seed(conn, seed_file) -> bool:
    if not seed_file or not Path(seed_file).exists():
        return False
    spec = importlib.util.spec_from_file_location("opsdesk_seed", seed_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, "seed", None)
    if not callable(func):
        log.warning("%s has no seed(conn) function; using the built-in demo data", seed_file)
        return False
    func(conn)
    conn.commit()
    log.info("Seeded from %s", seed_file)
    return True


def seed_demo(conn, now=None) -> None:
    """Small demo data set. Dates are relative to now so SLA states vary."""
    now = now or rules.now_local()
    has_shift = "shift_start" in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}

    users = [  # name, role, region, agency, available, shift
        ("Priya Nair", "Requester", "West", "Sunrise Travels", 1, None),
        ("Rahul Verma", "Requester", "North", "Globe Trotters", 1, None),
        ("Aarav Mehta", "Agent", "North", None, 1, ("09:00", "18:00")),
        ("Sneha Iyer", "Agent", "South", None, 1, ("06:00", "15:00")),
        ("Kabir Singh", "Agent", "West", None, 1, ("14:00", "02:00")),
        ("Meera Joshi", "Agent", "West", None, 0, ("09:00", "18:00")),  # on leave
        ("Dev Malhotra", "Escalation Manager", "North", None, 1, None),
    ]
    with conn:
        for name, role, region, agency, available, shift in users:
            if has_shift:
                conn.execute(
                    "INSERT INTO users (name, role, region, agency, available, shift_start, shift_end) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, role, region, agency, available, *(shift or (None, None))))
            else:
                conn.execute(
                    "INSERT INTO users (name, role, region, agency, available) VALUES (?, ?, ?, ?, ?)",
                    (name, role, region, agency, available))

    def when(**delta):
        return (now + timedelta(**delta)).isoformat(timespec="seconds")

    priya, rahul, aarav = (rules.get_user(conn, i) for i in (1, 2, 3))
    tickets = [  # (requester, fields)
        (priya, dict(title="Cancel hotel booking before LCD tomorrow",
                     description="Guest cancelled the trip. Please process free cancellation.",
                     tboh="TBOH-10231", priority="Urgent", region="West",
                     check_in=when(days=5), lcd=when(hours=30))),
        (priya, dict(title="Refund pending for cancelled Goa booking",
                     description="Refund not received after cancellation.",
                     tboh="TBOH-10231", priority="High", region="West",
                     check_in=when(days=5))),
        (priya, dict(title="Amend guest name on confirmed booking",
                     description="Wrong spelling of the lead guest name.",
                     tboh="TBOH-10544", priority="Normal", region="West",
                     check_in=when(days=9))),
        (priya, dict(title="Request to modify check-out date",
                     description="Guest wants to extend the stay by one night.",
                     tboh="TBOH-10987", priority="Low", region="West",
                     check_in=when(days=40))),
        (rahul, dict(title="Hotel voucher not received",
                     description="Voucher for Dubai stay has not arrived.",
                     tboh="TBOH-20877", priority="Normal", region="North",
                     check_in=when(hours=60))),
        (rahul, dict(title="Guest refused entry, property says overbooked",
                     description="Guest is at the front desk right now.",
                     tboh="TBOH-20990", priority="Urgent", region="North",
                     check_in=when(hours=3))),
        (rahul, dict(title="Payment charged twice for Jaipur stay",
                     description="Card was charged two times for the same booking.",
                     tboh="TBOH-20312", priority="High", region="North",
                     check_in=when(hours=-5))),
    ]
    ids = [rules.create_ticket(conn, fields, who, now) for who, fields in tickets]
    rules.escalate_ticket(conn, rules.fetch_ticket(conn, ids[5]),
                          "Guest is stranded at the property", aarav, now)
    rules.set_status(conn, rules.fetch_ticket(conn, ids[6]), "Open", aarav, now)
    log.info("Seeded built-in demo data (%d users, %d tickets)", len(users), len(ids))
