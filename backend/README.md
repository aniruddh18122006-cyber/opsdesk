# OpsDesk

Support-ticket tool for travel agencies (TBO Hackathon 2026).

| Part | Owner | Folder |
|---|---|---|
| API, business rules, security, tests | Aniruddh | `backend/` |
| Screens | Nandini | `frontend/` |
| Schema and seed data | Nandini | `database/` |
| Notes, demo script | Both | `docs/` |

## Run the backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows   (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn main:app --reload
```

Open http://127.0.0.1:8000/docs to try every endpoint. The first start creates
`backend/opsdesk.db` and fills it with demo data. To start fresh, stop the server
and delete that file.

Run the tests: `cd backend && pytest -q`

Optional settings go in `backend/.env` (copy `.env.example`). The file is never committed.

## Demo accounts

Pass the `id` as `user_id`. There is no password in this hackathon build.

| id | Name | Role | Sees |
|---|---|---|---|
| 1 | Priya Nair | Requester (Sunrise Travels) | Own agency only |
| 2 | Rahul Verma | Requester (Globe Trotters) | Own agency only |
| 3 | Aarav Mehta | Agent (North) | All tickets |
| 4 | Sneha Iyer | Agent (South) | All tickets |
| 5 | Kabir Singh | Agent (West) | All tickets |
| 6 | Meera Joshi | Agent (West, unavailable) | All tickets |
| 7 | Dev Malhotra | Escalation Manager | Escalated or own tickets |

## How the six problems are solved

1. **Linking.** The ticket detail returns `linked[]`: tickets with the same TBOH, or the same agency and intent.
2. **Routing.** New tickets go to an agent who is (1) available, (2) on shift now, (3) in the ticket's region, then (4) has the lowest open load. The reason is saved in `route_reason`.
3. **Side conversations.** Each thread stores its own recipients, so transferring a ticket does not change them. External threads need valid email recipients.
4. **SLA and escalation.** `sla` is `breached` once the earlier of check-in and LCD has passed, and `at_risk` inside the priority limit (Urgent 96h, High 72h, Normal 48h, Low 24h). Escalation moves the ticket to the Escalation Manager with status `Escalated` and writes a timeline event.
5. **Insights.** `/api/insights` returns theme counts with sample ticket ids. `/api/export` streams a CSV with no row cap.
6. **Data access.** Every endpoint except `/api/users` and `/api/suggest` needs `user_id` and is checked against the role. The API returns 403 when you are not allowed and 404 for an unknown ticket.

## API

| Method and path | Send | Get back |
|---|---|---|
| GET `/api/users` | | users |
| GET `/api/tickets?user_id=` | optional `region`, `intent`, `unassigned`, `assignee` | tickets, most urgent first |
| POST `/api/tickets` | title, description, agency, tboh, priority, region, intent, check_in, lcd, user_id | `{id}` |
| GET `/api/suggest?text=` | text | `{intent}` |
| GET `/api/tickets/{id}?user_id=` | | ticket + `linked[]` + `events[]` + `side[]` |
| POST `/api/tickets/{id}/escalate` | user_id, reason | `{ok}` |
| POST `/api/tickets/{id}/assign` | user_id, agent_id | `{ok}` |
| POST `/api/tickets/{id}/status` | user_id, status | `{ok}` |
| POST `/api/tickets/{id}/side` | user_id, kind, recipients[], body | `{ok}` |
| GET `/api/insights?user_id=` | | `[{theme, n, sample_ids}]` |
| GET `/api/export?user_id=` | | CSV |

For POST calls, `user_id` can go in the JSON body or in the URL (`?user_id=`).

## Assumptions

- Login is a demo user picker, not real authentication.
- Dates without a time mean the start of that day. Times without a zone are India time (UTC+05:30).
- Agents see every ticket. Requesters see their agency. Managers see escalated tickets and their own.
- Requesters can create tickets for their own agency and add external side conversations, but cannot change status, assign or escalate.
- A requester's timeline hides internal events and the routing reason.
- If `database/schema.sql` or `database/seed.py` exists, the backend uses it instead of the built-in ones.

## What was cut

- Real login and passwords.
- Email sending. External side conversations are stored, not sent.
- Editing or deleting tickets.
- Pagination on the ticket list.
