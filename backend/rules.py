"""OpsDesk business rules.

Everything the brief calls a "rule" lives here: who may see what, SLA state,
routing, escalation, linking, side conversations and insights.  main.py only
turns HTTP into calls to this module, so every rule can be tested without
starting a server.

All SQL uses ? placeholders.  The only strings joined into SQL are fixed
fragments written in this file (never user input).
"""
import json
import os
import re
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Iterator, Optional

# ---------------------------------------------------------------- constants

PRIORITIES = ["Urgent", "High", "Normal", "Low"]
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
STATUSES = ["New", "Open", "Pending", "Escalated", "Resolved", "Closed"]
TERMINAL_STATUSES = ("Resolved", "Closed")
SIDE_KINDS = ("internal", "external")

# A ticket is "at risk" when it is inside this many hours of its deadline.
SLA_RISK_HOURS = {"Urgent": 96, "High": 72, "Normal": 48, "Low": 24}
SLA_RANK = {"breached": 0, "at_risk": 1, "ok": 2}

INTENT_KEYWORDS = {
    "cancellation": ["cancel", "lcd", "last cancellation"],
    "refund": ["refund", "money back", "chargeback", "reimburs"],
    "amendment": ["amend", "modif", "date change", "name change", "rename",
                  "extend", "extension", "shorten", "upgrade", "add guest", "change"],
    "confirmation": ["confirm", "voucher", "hcn", "reconfirm"],
    "payment": ["payment", "invoice", "charged", "double charge", "billing",
                "credit note", "paid"],
    "complaint": ["complain", "overbook", "no room", "dirty", "refused",
                  "denied", "bad service", "rude", "walked"],
}

# What a requester may see on the timeline: nothing about internal handling.
REQUESTER_EVENT_KINDS = ("created", "status_changed", "side_external")

EXPORT_COLUMNS = [
    "id", "title", "description", "agency", "tboh", "priority", "region",
    "intent", "status", "assignee_id", "assignee_name", "sla", "hours_left",
    "check_in", "lcd", "route_reason", "created_at",
]

_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RuleError(Exception):
    """A rule was broken. main.py turns this into an HTTP error response."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# --------------------------------------------------------------------- time

def _local_tz() -> timezone:
    try:
        minutes = int(os.getenv("OPSDESK_UTC_OFFSET_MIN", "330"))  # IST by default
    except ValueError:
        minutes = 330
    return timezone(timedelta(minutes=minutes))


def now_local() -> datetime:
    return datetime.now(_local_tz())


def parse_dt(value) -> datetime:
    """Parse an ISO date or datetime into an aware local datetime.

    A plain date means the start of that day.  A datetime without a zone is
    taken to be local time.  Raises ValueError with a message fit for users.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("must not be empty")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                "must be an ISO date (2026-12-01) or datetime (2026-12-01T14:30)"
            ) from None
    if not 2000 <= dt.year <= 2100:
        raise ValueError("year must be between 2000 and 2100")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz())
    try:
        return dt.astimezone(_local_tz())
    except OverflowError:
        raise ValueError("date is out of range") from None


def normalize_dt_input(value) -> str:
    """Validate a date/datetime and return the text to store."""
    text = str(value).strip()
    dt = parse_dt(text)
    return text if _DATE_ONLY.match(text) else dt.isoformat(timespec="seconds")


def _canonical(value, options, what="value") -> str:
    text = str(value).strip().lower()
    for option in options:
        if option.lower() == text:
            return option
    raise ValueError(f"must be one of: {', '.join(options)}")


def normalize_priority(value) -> str:
    return _canonical(value, PRIORITIES)


def normalize_status(value) -> str:
    return _canonical(value, STATUSES)


# ------------------------------------------------------------ roles & access

def role_kind(role) -> str:
    """Map whatever role text the database uses to requester / agent / manager.

    Anything unrecognised is treated as a requester (least privilege).
    """
    text = (role or "").strip().lower()
    if "manager" in text:
        return "manager"
    if "agent" in text:
        return "agent"
    return "requester"


def get_user(conn, user_id) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise RuleError(404, f"User {user_id} not found")
    return row


def visibility_clause(user, alias: str = "t"):
    """SQL fragment + params limiting tickets to what this user may see."""
    kind = role_kind(user["role"])
    if kind == "requester":
        agency = (user["agency"] or "").strip()
        if not agency:
            return "0 = 1", []
        return f"{alias}.agency = ? COLLATE NOCASE", [agency]
    if kind == "manager":
        return f"({alias}.status = 'Escalated' OR {alias}.assignee_id = ?)", [user["id"]]
    return "1 = 1", []


def can_see(user, ticket) -> bool:
    """Same rule as visibility_clause, for a single ticket row."""
    kind = role_kind(user["role"])
    if kind == "agent":
        return True
    if kind == "manager":
        return ticket["status"] == "Escalated" or ticket["assignee_id"] == user["id"]
    agency = (user["agency"] or "").strip().lower()
    return bool(agency) and (ticket["agency"] or "").strip().lower() == agency


def require_staff(user, action: str) -> None:
    if role_kind(user["role"]) == "requester":
        raise RuleError(403, f"Requesters are not allowed to {action}")


def _authorize(user, ticket, action: str) -> None:
    require_staff(user, action)
    if not can_see(user, ticket):
        raise RuleError(403, "You do not have access to this ticket")


def _col(row, name):
    return row[name] if name in row.keys() else None


# ---------------------------------------------------------------------- SLA

def sla(check_in, lcd, priority, status, now=None):
    """Return (state, hours_left).

    hours_left counts to the earlier of check-in and LCD.  Past that moment the
    ticket is "breached"; inside the priority's window it is "at_risk".
    Resolved and closed tickets are never flagged.
    """
    now = now or now_local()
    deadlines = []
    for value in (check_in, lcd):
        if value:
            try:
                deadlines.append(parse_dt(value))
            except ValueError:
                pass
    if not deadlines:
        return "ok", None
    raw = (min(deadlines) - now).total_seconds() / 3600
    hours_left = round(raw, 1)
    if status in TERMINAL_STATUSES:
        return "ok", hours_left
    if raw < 0:
        return "breached", hours_left
    limit = SLA_RISK_HOURS.get(priority, SLA_RISK_HOURS["Normal"])
    return ("at_risk" if raw <= limit else "ok"), hours_left


# ----------------------------------------------------------- intent suggest

def suggest_intent(text) -> str:
    """Pick the intent whose keywords match the text best ("other" if none)."""
    lowered = (text or "").lower()
    best, best_score = "other", 0
    for intent, stems in INTENT_KEYWORDS.items():
        score = sum(1 for s in stems if re.search(r"\b" + re.escape(s), lowered))
        if score > best_score:
            best, best_score = intent, score
    return best


# ------------------------------------------------------------------ routing

def _parse_hhmm(text) -> Optional[dtime]:
    try:
        hours, minutes = str(text).split(":")[:2]
        return dtime(int(hours), int(minutes))
    except (ValueError, TypeError):
        return None


def in_shift(start, end, now) -> bool:
    """Is `now` inside the working window? No window set means always yes."""
    s, e = _parse_hhmm(start) if start else None, _parse_hhmm(end) if end else None
    if s is None or e is None or s == e:
        return True
    t = now.time().replace(second=0, microsecond=0)
    if s < e:
        return s <= t < e
    return t >= s or t < e  # overnight window, e.g. 22:00-06:00


def open_loads(conn) -> dict:
    marks = ", ".join("?" for _ in TERMINAL_STATUSES)
    rows = conn.execute(
        "SELECT assignee_id, COUNT(*) AS n FROM tickets "
        f"WHERE assignee_id IS NOT NULL AND status NOT IN ({marks}) "
        "GROUP BY assignee_id",
        TERMINAL_STATUSES,
    ).fetchall()
    return {r["assignee_id"]: r["n"] for r in rows}


def route(conn, region, now=None):
    """Choose who gets a new ticket. Returns (agent_id or None, why_text).

    Rule order:
      1. only agents marked available
      2. prefer agents whose working hours cover the current time
      3. prefer the ticket's region
      4. lowest open load (ties go to the lowest id)
    """
    now = now or now_local()
    agents = [u for u in conn.execute("SELECT * FROM users WHERE available = 1").fetchall()
              if role_kind(u["role"]) == "agent"]
    if not agents:
        return None, "No agent is available right now, so the ticket stays in the triage queue (unassigned)."

    on_shift = [a for a in agents
                if in_shift(_col(a, "shift_start"), _col(a, "shift_end"), now)]
    pool = on_shift or agents
    region_key = (region or "").strip().lower()
    same_region = [a for a in pool
                   if region_key and (a["region"] or "").strip().lower() == region_key]
    candidates = same_region or pool

    loads = open_loads(conn)
    best = min(candidates, key=lambda a: (loads.get(a["id"], 0), a["id"]))
    load = loads.get(best["id"], 0)

    parts = ["available"]
    s, e = _col(best, "shift_start"), _col(best, "shift_end")
    if on_shift:
        parts.append(f"on shift ({s}-{e})" if s and e else "no fixed shift")
    else:
        parts.append("nobody is on shift right now, so all available agents were considered")
    if same_region:
        parts.append(f"same region ({best['region']})")
    elif region_key:
        parts.append(f"no available agent in {region}, so other regions were considered")
    else:
        parts.append("the ticket has no region")
    parts.append(f"lowest open load ({load} open ticket{'s' if load != 1 else ''})")
    return best["id"], f"Routed to {best['name']}: " + ", ".join(parts) + "."


def pick_escalation_manager(conn, region):
    managers = [u for u in conn.execute("SELECT * FROM users").fetchall()
                if role_kind(u["role"]) == "manager"]
    if not managers:
        return None
    pool = [m for m in managers if m["available"]] or managers
    region_key = (region or "").strip().lower()
    same = [m for m in pool if region_key and (m["region"] or "").strip().lower() == region_key]
    loads = open_loads(conn)
    return min(same or pool, key=lambda m: (loads.get(m["id"], 0), m["id"]))


# ------------------------------------------------------------- reading data

def fetch_ticket(conn, ticket_id):
    return conn.execute(
        "SELECT t.*, u.name AS assignee_name FROM tickets t "
        "LEFT JOIN users u ON u.id = t.assignee_id WHERE t.id = ?",
        (ticket_id,),
    ).fetchone()


def get_ticket(conn, ticket_id, user):
    """The ticket row, or 404 (unknown id) / 403 (not yours to read)."""
    ticket = fetch_ticket(conn, ticket_id)
    if ticket is None:
        raise RuleError(404, f"Ticket {ticket_id} not found")
    if not can_see(user, ticket):
        raise RuleError(403, "You do not have access to this ticket")
    return ticket


def ticket_view(row, user, now=None) -> dict:
    now = now or now_local()
    data = dict(row)
    data["sla"], data["hours_left"] = sla(
        row["check_in"], row["lcd"], row["priority"], row["status"], now)
    data.setdefault("assignee_name", None)
    if role_kind(user["role"]) == "requester":
        data["route_reason"] = None  # internal handling detail
    return data


def _urgency(t):
    return (
        1 if t["status"] in TERMINAL_STATUSES else 0,
        SLA_RANK[t["sla"]],
        PRIORITY_RANK.get(t["priority"], 99),
        t["hours_left"] if t["hours_left"] is not None else 1e9,
        t["id"],
    )


def list_tickets(conn, user, region=None, intent=None, unassigned=False,
                 assignee=None, now=None):
    """Tickets this user may see, most urgent first, with optional filters."""
    if unassigned and assignee is not None:
        raise RuleError(422, "unassigned and assignee cannot be used together")
    now = now or now_local()
    vis, args = visibility_clause(user, "t")
    where, params = [vis], list(args)
    if region and region.strip():
        where.append("t.region = ? COLLATE NOCASE")
        params.append(region.strip())
    if intent and intent.strip():
        where.append("t.intent = ? COLLATE NOCASE")
        params.append(intent.strip())
    if unassigned:
        where.append("t.assignee_id IS NULL")
    if assignee is not None:
        where.append("t.assignee_id = ?")
        params.append(assignee)
    sql = ("SELECT t.*, u.name AS assignee_name FROM tickets t "
           "LEFT JOIN users u ON u.id = t.assignee_id WHERE " + " AND ".join(where))
    views = [ticket_view(r, user, now) for r in conn.execute(sql, params).fetchall()]
    views.sort(key=_urgency)
    return views


def find_linked(conn, ticket, user, limit: int = 50):
    """Other tickets with the same TBOH, or the same agency and intent.

    Only tickets this user may see are returned, so a requester never learns
    about another agency's ticket through a shared TBOH.
    """
    tboh = (ticket["tboh"] or "").strip()
    agency = (ticket["agency"] or "").strip()
    intent = (ticket["intent"] or "").strip()
    clauses, args = [], []
    if tboh:
        clauses.append("t.tboh = ? COLLATE NOCASE")
        args.append(tboh)
    if agency and intent:
        clauses.append("(t.agency = ? COLLATE NOCASE AND t.intent = ? COLLATE NOCASE)")
        args += [agency, intent]
    if not clauses:
        return []
    vis, vis_args = visibility_clause(user, "t")
    sql = ("SELECT t.id, t.title, t.status, t.priority, t.agency, t.tboh, t.intent "
           "FROM tickets t WHERE t.id != ? AND (" + " OR ".join(clauses) + ") AND "
           + vis + " ORDER BY t.id DESC LIMIT ?")
    out = []
    for r in conn.execute(sql, [ticket["id"], *args, *vis_args, limit]).fetchall():
        item = dict(r)
        same_tboh = bool(tboh) and (r["tboh"] or "").strip().lower() == tboh.lower()
        item["link_reason"] = "same TBOH" if same_tboh else "same agency and intent"
        out.append(item)
    return out


def get_events(conn, ticket_id, user):
    sql = ("SELECT e.id, e.kind, e.detail, e.actor_id, u.name AS actor_name, e.created_at "
           "FROM events e LEFT JOIN users u ON u.id = e.actor_id WHERE e.ticket_id = ?")
    args = [ticket_id]
    if role_kind(user["role"]) == "requester":
        marks = ", ".join("?" for _ in REQUESTER_EVENT_KINDS)
        sql += f" AND e.kind IN ({marks})"
        args += REQUESTER_EVENT_KINDS
    sql += " ORDER BY e.id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_side(conn, ticket_id, user):
    sql = ("SELECT s.id, s.kind, s.recipients, s.body, s.author_id, "
           "u.name AS author_name, s.created_at "
           "FROM side_conversations s LEFT JOIN users u ON u.id = s.author_id "
           "WHERE s.ticket_id = ?")
    args = [ticket_id]
    if role_kind(user["role"]) == "requester":
        sql += " AND s.kind = 'external'"
    sql += " ORDER BY s.id"
    out = []
    for r in conn.execute(sql, args).fetchall():
        item = dict(r)
        try:
            item["recipients"] = json.loads(r["recipients"] or "[]")
        except ValueError:
            item["recipients"] = []
        out.append(item)
    return out


def ticket_detail(conn, ticket, user, now=None) -> dict:
    data = ticket_view(ticket, user, now)
    data["linked"] = find_linked(conn, ticket, user)
    data["events"] = get_events(conn, ticket["id"], user)
    data["side"] = get_side(conn, ticket["id"], user)
    return data


# ---------------------------------------------------------- changing tickets

def add_event(conn, ticket_id, kind, detail, actor_id, now) -> None:
    conn.execute(
        "INSERT INTO events (ticket_id, kind, detail, actor_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ticket_id, kind, detail, actor_id, now.isoformat(timespec="seconds")),
    )


def _name(conn, user_id) -> str:
    if user_id is None:
        return "nobody"
    row = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["name"] if row else f"user {user_id}"


def create_ticket(conn, fields: dict, actor, now=None) -> int:
    """Create, route and log a ticket. `fields` must already be validated."""
    now = now or now_local()
    fields = dict(fields)
    kind = role_kind(actor["role"])
    if kind == "requester":
        own = (actor["agency"] or "").strip()
        if not own:
            raise RuleError(403, "Your account is not linked to an agency")
        given = (fields.get("agency") or "").strip()
        if given and given.lower() != own.lower():
            raise RuleError(403, "Requesters can only create tickets for their own agency")
        fields["agency"] = own
        if not fields.get("region"):
            fields["region"] = actor["region"]
    if not (fields.get("agency") or "").strip():
        raise RuleError(422, "agency is required")

    title = fields["title"].strip()
    description = fields.get("description") or ""
    intent = (fields.get("intent") or "").strip().lower() or suggest_intent(f"{title} {description}")
    agent_id, why = route(conn, fields.get("region"), now)
    stamp = now.isoformat(timespec="seconds")

    with conn:
        cur = conn.execute(
            "INSERT INTO tickets (title, description, agency, tboh, priority, region, "
            "intent, check_in, lcd, status, assignee_id, route_reason, created_by, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'New', ?, ?, ?, ?, ?)",
            (title, description, fields["agency"].strip(), fields.get("tboh"),
             fields.get("priority") or "Normal", fields.get("region"), intent,
             fields["check_in"], fields.get("lcd"), agent_id, why, actor["id"],
             stamp, stamp),
        )
        ticket_id = cur.lastrowid
        add_event(conn, ticket_id, "created",
                  f"Ticket created by {actor['name']} for {fields['agency'].strip()}",
                  actor["id"], now)
        add_event(conn, ticket_id, "routed", why, None, now)
    return ticket_id


def assign_ticket(conn, ticket, target_id, actor, now=None) -> bool:
    """Transfer a ticket. Side-conversation recipients are stored on each
    thread, so they are untouched. Returns False if nothing changed."""
    now = now or now_local()
    _authorize(actor, ticket, "transfer tickets")
    target = conn.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone()
    if target is None:
        raise RuleError(404, f"User {target_id} not found")
    if role_kind(target["role"]) == "requester":
        raise RuleError(422, "agent_id must belong to an agent or manager")
    if ticket["assignee_id"] == target["id"]:
        return False
    before = _name(conn, ticket["assignee_id"])
    with conn:
        conn.execute("UPDATE tickets SET assignee_id = ?, updated_at = ? WHERE id = ?",
                     (target["id"], now.isoformat(timespec="seconds"), ticket["id"]))
        add_event(conn, ticket["id"], "assigned",
                  f"Transferred from {before} to {target['name']} by {actor['name']}",
                  actor["id"], now)
    return True


def escalate_ticket(conn, ticket, reason, actor, now=None) -> None:
    now = now or now_local()
    _authorize(actor, ticket, "escalate tickets")
    reason = (reason or "").strip()
    if not reason:
        raise RuleError(422, "reason must not be empty")
    if len(reason) > 1000:
        raise RuleError(422, "reason must be at most 1000 characters")
    if ticket["status"] in TERMINAL_STATUSES:
        raise RuleError(409, f"A {ticket['status']} ticket cannot be escalated")
    if ticket["status"] == "Escalated":
        raise RuleError(409, "Ticket is already escalated")
    manager = pick_escalation_manager(conn, ticket["region"])
    if manager is None:
        raise RuleError(409, "There is no Escalation Manager to receive this ticket")
    before = _name(conn, ticket["assignee_id"])
    with conn:
        conn.execute(
            "UPDATE tickets SET status = 'Escalated', assignee_id = ?, updated_at = ? WHERE id = ?",
            (manager["id"], now.isoformat(timespec="seconds"), ticket["id"]))
        add_event(conn, ticket["id"], "escalated",
                  f"Escalated by {actor['name']} to {manager['name']} "
                  f"(was with {before}). Reason: {reason}",
                  actor["id"], now)


def set_status(conn, ticket, new_status, actor, now=None) -> bool:
    now = now or now_local()
    _authorize(actor, ticket, "change ticket status")
    try:
        new_status = normalize_status(new_status)
    except ValueError as exc:
        raise RuleError(422, f"status {exc}") from None
    if ticket["status"] == new_status:
        return False
    with conn:
        conn.execute("UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
                     (new_status, now.isoformat(timespec="seconds"), ticket["id"]))
        add_event(conn, ticket["id"], "status_changed",
                  f"Status changed from {ticket['status']} to {new_status} by {actor['name']}",
                  actor["id"], now)
    return True


def clean_recipients(kind, recipients) -> list:
    cleaned, seen = [], set()
    for item in recipients or []:
        text = str(item).strip()
        if not text or text.lower() in seen:
            continue
        if len(text) > 200:
            raise RuleError(422, "each recipient must be at most 200 characters")
        seen.add(text.lower())
        cleaned.append(text)
    if len(cleaned) > 20:
        raise RuleError(422, "a side conversation can have at most 20 recipients")
    if kind == "external":
        if not cleaned:
            raise RuleError(422, "external side conversations need at least one recipient")
        bad = [r for r in cleaned if not _EMAIL.match(r)]
        if bad:
            raise RuleError(422, f"external recipients must be email addresses: {', '.join(bad)}")
    return cleaned


def add_side_conversation(conn, ticket, kind, recipients, body, actor, now=None) -> int:
    now = now or now_local()
    kind = str(kind).strip().lower()
    if kind not in SIDE_KINDS:
        raise RuleError(422, "kind must be one of: internal, external")
    if role_kind(actor["role"]) == "requester":
        if kind == "internal":
            raise RuleError(403, "Requesters are not allowed to use internal side conversations")
        if not can_see(actor, ticket):
            raise RuleError(403, "You do not have access to this ticket")
    else:
        _authorize(actor, ticket, "start side conversations")
    body = (body or "").strip()
    if not body:
        raise RuleError(422, "body must not be empty")
    if len(body) > 5000:
        raise RuleError(422, "body must be at most 5000 characters")
    people = clean_recipients(kind, recipients)
    with conn:
        cur = conn.execute(
            "INSERT INTO side_conversations (ticket_id, kind, recipients, body, author_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ticket["id"], kind, json.dumps(people), body, actor["id"],
             now.isoformat(timespec="seconds")))
        note = f"{kind.capitalize()} side conversation started by {actor['name']}"
        if people:
            note += f" with {len(people)} recipient{'s' if len(people) != 1 else ''}"
        add_event(conn, ticket["id"], f"side_{kind}", note, actor["id"], now)
    return cur.lastrowid


# ---------------------------------------------------------- insights/export

def insights(conn, user, limit: int = 10):
    """Top themes (by intent) among the tickets this user may see."""
    require_staff(user, "view insights")
    vis, args = visibility_clause(user, "t")
    rows = conn.execute(
        "SELECT t.id, t.intent FROM tickets t WHERE " + vis + " ORDER BY t.id DESC", args
    ).fetchall()
    themes = {}
    for r in rows:
        theme = (r["intent"] or "").strip().lower() or "other"
        entry = themes.setdefault(theme, {"theme": theme, "n": 0, "sample_ids": []})
        entry["n"] += 1
        if len(entry["sample_ids"]) < 5:
            entry["sample_ids"].append(r["id"])  # newest first
    ranked = sorted(themes.values(), key=lambda e: (-e["n"], e["theme"]))
    return ranked[:limit]


def csv_safe(value):
    """Stop spreadsheet apps treating text as a formula (CSV injection)."""
    if isinstance(value, str) and value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def iter_export_rows(conn, user, now=None, batch: int = 500) -> Iterator[list]:
    """Every ticket the user may see, one list per row, in batches (no cap)."""
    require_staff(user, "export tickets")
    now = now or now_local()
    vis, args = visibility_clause(user, "t")
    cur = conn.execute(
        "SELECT t.*, u.name AS assignee_name FROM tickets t "
        "LEFT JOIN users u ON u.id = t.assignee_id WHERE " + vis + " ORDER BY t.id", args)
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            break
        for r in rows:
            values = dict(r)
            values["sla"], values["hours_left"] = sla(
                r["check_in"], r["lcd"], r["priority"], r["status"], now)
            yield [csv_safe(values.get(col)) for col in EXPORT_COLUMNS]
