# OpsDesk frontend

React + Vite interface for the FastAPI backend in `../backend`. The UI uses the existing `/api` routes and demo user IDs; the backend remains the source of truth for permissions, routing, SLA state, and ticket visibility.

## Run locally

In one terminal, start the backend as described in the repository README. In another:

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL printed in the terminal (normally `http://localhost:5173`). Vite forwards `/api` requests to `http://127.0.0.1:8000`, so the backend does not need a separate CORS setting for local development.

## Screens

- Ticket queue with search and views for personal, unassigned, at-risk, and breached tickets
- Ticket details with conversation, linked tickets, activity, and booking context
- Ticket creation with intent suggestion from `/api/suggest`
- Side conversation inbox, escalation queue, and insights with CSV export

The user control in the header cycles through the backend's demo accounts. The selected user ID is kept in local browser storage. This is a demo identity picker, not authentication.
