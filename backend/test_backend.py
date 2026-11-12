"""Tests for OpsDesk. Run from the backend folder:  pytest -q

Rule tests need only the standard library. API tests need fastapi + httpx and
are skipped automatically if those are not installed.
"""
from datetime import datetime, timedelta, timezone

import pytest

import db
import rules

IST = timezone(timedelta(minutes=330))
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=IST)  # a Friday, 10:00 local


def hrs(n):
    return (NOW + timedelta(hours=n)).isoformat(timespec="seconds")


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path, schema_file=None, seed_file=None)  # built-in schema + demo data
    c = db.connect(path)
    yield c
    c.close()


def user(conn, name):
    return conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()


def make(conn, actor, **kw):
    fields = dict(title="Test ticket", description="", tboh=None, priority="Normal",
                  region="West", check_in=hrs(200))
    fields.update(kw)
    return rules.create_ticket(conn, fields, actor, NOW)


# ---------------------------------------------------------------------- SLA

@pytest.mark.parametrize("priority,hours,expected", [
    ("Urgent", 95, "at_risk"), ("Urgent", 97, "ok"),
    ("High", 71, "at_risk"), ("High", 73, "ok"),
    ("Normal", 47, "at_risk"), ("Normal", 49, "ok"),
    ("Low", 23, "at_risk"), ("Low", 25, "ok"),
    ("Normal", -1, "breached"),
])
def test_sla_limits_by_priority(priority, hours, expected):
    state, _ = rules.sla(hrs(hours), None, priority, "Open", NOW)
    assert state == expected


def test_sla_uses_earlier_of_check_in_and_lcd():
    state, left = rules.sla(hrs(300), hrs(10), "Normal", "Open", NOW)
    assert state == "at_risk" and left == 10.0


def test_sla_resolved_is_never_flagged():
    assert rules.sla(hrs(-5), None, "Urgent", "Resolved", NOW)[0] == "ok"


# ------------------------------------------------------------------ routing

def test_routing_skips_off_shift_agent(conn):
    # At 10:00 Kabir (West, 14:00-02:00) is off shift and Meera is unavailable,
    # so even a West ticket goes to an on-shift agent (Aarav or Sneha).
    agent_id, why = rules.route(conn, "West", NOW)
    assert agent_id in (user(conn, "Aarav Mehta")["id"], user(conn, "Sneha Iyer")["id"])
    assert "on shift" in why and "lowest open load" in why


def test_routing_same_region_wins_when_on_shift(conn):
    late = NOW.replace(hour=16)  # Kabir on shift (West)
    agent_id, why = rules.route(conn, "West", late)
    assert agent_id == user(conn, "Kabir Singh")["id"]
    assert "same region" in why


def test_routing_never_picks_unavailable_agent(conn):
    for hour in range(24):
        agent_id, _ = rules.route(conn, "West", NOW.replace(hour=hour))
        assert agent_id != user(conn, "Meera Joshi")["id"]


def test_routing_lowest_load_breaks_ties(conn):
    conn.execute("UPDATE users SET shift_start=NULL, shift_end=NULL")
    conn.execute("UPDATE users SET region='West' WHERE role='Agent'")
    conn.commit()
    agents = [u["id"] for u in conn.execute(
        "SELECT id FROM users WHERE role='Agent' AND available=1").fetchall()]
    loads = rules.open_loads(conn)
    expected = min(agents, key=lambda a: (loads.get(a, 0), a))
    assert rules.route(conn, "West", NOW)[0] == expected


def test_routing_no_agent_available(conn):
    conn.execute("UPDATE users SET available = 0")
    conn.commit()
    agent_id, why = rules.route(conn, "West", NOW)
    assert agent_id is None and "No agent" in why


# ------------------------------------------------------------------ linking

def test_linking_same_tboh_and_same_agency_intent(conn):
    priya, rahul = user(conn, "Priya Nair"), user(conn, "Rahul Verma")
    a = make(conn, priya, tboh="LINK-1", intent="refund")
    b = make(conn, priya, tboh="LINK-1", intent="amendment")
    c = make(conn, priya, tboh="OTHER", intent="refund")
    d = make(conn, rahul, tboh="LINK-1", intent="refund")  # other agency
    ids = {t["id"]: t["link_reason"]
           for t in rules.find_linked(conn, rules.fetch_ticket(conn, a), priya)}
    assert b in ids and ids[b] == "same TBOH"
    assert c in ids and ids[c] == "same agency and intent"
    assert d not in ids  # requester must not learn about another agency


# ------------------------------------------------------------------- roles

def test_requester_cannot_read_other_agency_ticket(conn):
    rahul, priya = user(conn, "Rahul Verma"), user(conn, "Priya Nair")
    tid = make(conn, priya)
    with pytest.raises(rules.RuleError) as e:
        rules.get_ticket(conn, tid, rahul)
    assert e.value.status_code == 403


def test_requester_list_only_own_agency(conn):
    priya = user(conn, "Priya Nair")
    rows = rules.list_tickets(conn, priya, now=NOW)
    assert rows and all(r["agency"] == "Sunrise Travels" for r in rows)


def test_requester_cannot_act_or_use_internal(conn):
    priya = user(conn, "Priya Nair")
    t = rules.fetch_ticket(conn, make(conn, priya))
    for call in (
        lambda: rules.escalate_ticket(conn, t, "why", priya, NOW),
        lambda: rules.set_status(conn, t, "Open", priya, NOW),
        lambda: rules.assign_ticket(conn, t, 3, priya, NOW),
        lambda: rules.add_side_conversation(conn, t, "internal", [], "x", priya, NOW),
        lambda: rules.insights(conn, priya),
    ):
        with pytest.raises(rules.RuleError) as e:
            call()
        assert e.value.status_code == 403


def test_requester_hidden_internal_data(conn):
    priya, aarav = user(conn, "Priya Nair"), user(conn, "Aarav Mehta")
    t = rules.fetch_ticket(conn, make(conn, priya))
    rules.add_side_conversation(conn, t, "internal", [], "secret note", aarav, NOW)
    detail = rules.ticket_detail(conn, t, priya, NOW)
    assert detail["route_reason"] is None
    assert all(s["kind"] == "external" for s in detail["side"])
    assert all(e["kind"] != "routed" for e in detail["events"])


def test_requester_ticket_created_for_own_agency_only(conn):
    priya = user(conn, "Priya Nair")
    with pytest.raises(rules.RuleError) as e:
        make(conn, priya, agency="Globe Trotters")
    assert e.value.status_code == 403


# ------------------------------------------------ actions and their events

def test_escalate_moves_to_manager_and_logs_event(conn):
    aarav = user(conn, "Aarav Mehta")
    t = rules.fetch_ticket(conn, make(conn, user(conn, "Priya Nair")))
    rules.escalate_ticket(conn, t, "Guest stranded", aarav, NOW)
    after = rules.fetch_ticket(conn, t["id"])
    assert after["status"] == "Escalated"
    assert after["assignee_id"] == user(conn, "Dev Malhotra")["id"]
    kinds = [e["kind"] for e in rules.get_events(conn, t["id"], aarav)]
    assert "escalated" in kinds
    with pytest.raises(rules.RuleError) as e:  # cannot escalate twice
        rules.escalate_ticket(conn, after, "again", aarav, NOW)
    assert e.value.status_code == 409


def test_escalate_needs_reason(conn):
    aarav = user(conn, "Aarav Mehta")
    t = rules.fetch_ticket(conn, make(conn, user(conn, "Priya Nair")))
    with pytest.raises(rules.RuleError) as e:
        rules.escalate_ticket(conn, t, "   ", aarav, NOW)
    assert e.value.status_code == 422


def test_transfer_keeps_side_conversation_recipients(conn):
    aarav, sneha = user(conn, "Aarav Mehta"), user(conn, "Sneha Iyer")
    t = rules.fetch_ticket(conn, make(conn, user(conn, "Priya Nair")))
    rules.add_side_conversation(conn, t, "external", ["hotel@example.com"], "Please confirm", aarav, NOW)
    rules.assign_ticket(conn, t, sneha["id"], aarav, NOW)
    side = rules.get_side(conn, t["id"], aarav)
    assert side[0]["recipients"] == ["hotel@example.com"]
    assert rules.fetch_ticket(conn, t["id"])["assignee_id"] == sneha["id"]


def test_external_side_conversation_validation(conn):
    aarav = user(conn, "Aarav Mehta")
    t = rules.fetch_ticket(conn, make(conn, user(conn, "Priya Nair")))
    for recipients in ([], ["not-an-email"]):
        with pytest.raises(rules.RuleError) as e:
            rules.add_side_conversation(conn, t, "external", recipients, "hi", aarav, NOW)
        assert e.value.status_code == 422


def test_status_change_validates_and_logs(conn):
    aarav = user(conn, "Aarav Mehta")
    t = rules.fetch_ticket(conn, make(conn, user(conn, "Priya Nair")))
    with pytest.raises(rules.RuleError) as e:
        rules.set_status(conn, t, "Banana", aarav, NOW)
    assert e.value.status_code == 422
    rules.set_status(conn, t, "pending", aarav, NOW)
    assert rules.fetch_ticket(conn, t["id"])["status"] == "Pending"
    assert any(ev["kind"] == "status_changed" for ev in rules.get_events(conn, t["id"], aarav))


def test_manager_sees_only_escalated_or_own(conn):
    dev = user(conn, "Dev Malhotra")
    rows = rules.list_tickets(conn, dev, now=NOW)
    assert rows and all(r["status"] == "Escalated" or r["assignee_id"] == dev["id"] for r in rows)


# ------------------------------------------------ filters, insights, export

def test_filters(conn):
    aarav = user(conn, "Aarav Mehta")
    north = rules.list_tickets(conn, aarav, region="north", now=NOW)
    assert north and all(r["region"] == "North" for r in north)
    unassigned_rows = rules.list_tickets(conn, aarav, unassigned=True, now=NOW)
    assert all(r["assignee_id"] is None for r in unassigned_rows)
    with pytest.raises(rules.RuleError):
        rules.list_tickets(conn, aarav, unassigned=True, assignee=1, now=NOW)


def test_list_is_most_urgent_first(conn):
    rows = rules.list_tickets(conn, user(conn, "Aarav Mehta"), now=NOW)
    keys = [(rules.SLA_RANK[r["sla"]], r["status"] in rules.TERMINAL_STATUSES) for r in rows]
    open_rows = [r for r in rows if r["status"] not in rules.TERMINAL_STATUSES]
    ranks = [rules.SLA_RANK[r["sla"]] for r in open_rows]
    assert ranks == sorted(ranks) and keys


def test_insights_counts(conn):
    result = rules.insights(conn, user(conn, "Aarav Mehta"))
    assert result and result[0]["n"] >= result[-1]["n"]
    assert all({"theme", "n", "sample_ids"} <= set(r) for r in result)


def test_suggest_intent():
    assert rules.suggest_intent("Please cancel my booking") == "cancellation"
    assert rules.suggest_intent("refund not received") == "refund"
    assert rules.suggest_intent("hello") == "other"


def test_csv_injection_is_neutralised():
    assert rules.csv_safe("=SUM(A1)") == "'=SUM(A1)"
    assert rules.csv_safe("normal") == "normal"


def test_export_has_every_visible_row(conn):
    aarav = user(conn, "Aarav Mehta")
    rows = list(rules.iter_export_rows(conn, aarav, NOW))
    assert len(rows) == conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]


# ---------------------------------------------------------------- HTTP layer

def _client(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import main
    app = main.create_app(str(tmp_path / "api.db"), use_project_files=False)
    return TestClient(app)


def test_api_end_to_end(tmp_path):
    with _client(tmp_path) as c:
        users = c.get("/api/users").json()
        by_name = {u["name"]: u["id"] for u in users}
        priya, rahul, aarav = by_name["Priya Nair"], by_name["Rahul Verma"], by_name["Aarav Mehta"]

        body = {"user_id": priya, "title": "Cancel booking", "check_in": "2030-01-10",
                "priority": "high", "region": "West"}
        new_id = c.post("/api/tickets", json=body).json()["id"]

        # clear errors
        assert c.post("/api/tickets", json={**body, "title": "  "}).status_code == 422
        assert c.post("/api/tickets", json={**body, "priority": "Huge"}).status_code == 422
        assert c.post("/api/tickets", json={**body, "check_in": "tomorrow"}).status_code == 422
        assert c.get("/api/tickets/99999", params={"user_id": aarav}).status_code == 404
        assert c.get("/api/tickets").status_code == 422  # user_id required

        # guessing another agency's ticket id is refused
        assert c.get(f"/api/tickets/{new_id}", params={"user_id": rahul}).status_code == 403
        # requester cannot act
        assert c.post(f"/api/tickets/{new_id}/status", json={"user_id": priya, "status": "Open"}).status_code == 403

        detail = c.get(f"/api/tickets/{new_id}", params={"user_id": priya}).json()
        assert {"linked", "events", "side", "sla", "hours_left", "assignee_name"} <= set(detail)

        assert c.post(f"/api/tickets/{new_id}/escalate",
                      json={"user_id": aarav, "reason": "urgent"}).json() == {"ok": True}
        assert c.get("/api/insights", params={"user_id": aarav}).status_code == 200
        assert c.get("/api/insights", params={"user_id": priya}).status_code == 403

        export = c.get("/api/export", params={"user_id": aarav})
        assert export.status_code == 200 and export.text.startswith("id,title")
        assert c.get("/api/export", params={"user_id": priya}).status_code == 403
        assert c.get("/api/suggest", params={"text": "refund please"}).json() == {"intent": "refund"}
