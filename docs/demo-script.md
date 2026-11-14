# OpsDesk demo script (5 minutes)

A step-by-step walkthrough for judges. It follows journeys A to E from the brief, with a short privacy moment at the end.

## Before you start (2 minutes, once)

1. Stop the backend, delete `backend/opsdesk.db`, then start the backend and the frontend (see the README's Quick start). This reseeds fresh dates, so the "at risk" tickets are still at risk today.
2. Open the app in the browser and press `Ctrl+Shift+R`.
3. Log in as **Aarav Mehta** (Agent) using the account picker at the top right.

Ticket numbers below are from the built-in demo data. Start from a fresh database so they match.

## The walkthrough

| Time | Journey | What you do | What the judges see |
|---|---|---|---|
| 0:00 | **Intro** | Show the Ticket Queue. | Real tickets sorted by urgency, with SLA badges (on track, at risk, breached) and hours left. |
| 0:40 | **A. Context and linking** | Open ticket **#1** (cancel hotel booking, `TBOH-10231`). Click the **Linked tickets** tab. | Ticket **#2** (refund for the same booking) and other tickets from the same agency appear without anyone linking them by hand. |
| 1:30 | **B. Routing** | Click **New ticket**. Type a title such as "Refund not received for booking", add a booking ID, set the priority to **Urgent**, choose a region and a check-in date, then create it. | The intent is suggested from the text. The ticket opens with an assignee and a **Why this ticket?** card explaining the choice: available, on shift, same region, lowest open load. Open **Smart Routing** to show the rules and each agent's workload. |
| 2:30 | **C. Side conversation** | Open ticket **#2**. In the Conversation tab choose **External conversation**, enter a recipient such as `supplier@example.com`, write a message and send. Then use **Reassign ticket** in the right panel to move it to another agent. | The thread keeps the same recipients after the transfer. Leaving the recipients empty on an external thread shows a clear error. |
| 3:30 | **D. SLA and escalate** | Open an at-risk ticket (**#1** if it still shows an amber hours-left badge). Enter a reason and click **Escalate to manager**. Then switch to **Dev Malhotra** (Escalation Manager) and open **Escalation Queue**. | The ticket is now in his queue, status **Escalated**, and the **Activity** tab shows the event on the timeline. Ticket **#6** is already escalated and breached. |
| 4:20 | **E. Insights and data** | Switch back to Aarav. Open **Insights and Analytics**. Change the period dropdown and click **Export CSV**. | Top themes with counts, an SLA overview, and a CSV that downloads with every ticket and no row cap. |
| 4:50 | **Privacy** | Switch to **Priya Nair** (Requester, Sunrise Travels). | Only Sunrise Travels tickets, no escalate, assign or status controls, and no internal notes. The API enforces this too, so guessing another agency's ticket number returns an error. |

## Talking points

- **Why it matters:** other helpdesks treat each ticket as isolated. OpsDesk understands agency, booking ID (TBOH), check-in date and last cancellation date out of the box.
- **Competitor research:** say "in the vendor documentation we reviewed", not "they cannot". Details and caveats are in `docs/competitor-research.pdf`.
- **Roles are enforced by the backend**, not just hidden in the interface.
- **What was cut:** real login, sending emails, editing tickets and pagination. Be upfront about it; the README lists them.

## If something goes wrong

| Problem | Fix |
|---|---|
| "Failed to fetch" on screen | The backend is not running. Start it again with `uvicorn main:app --reload` in `backend/`. |
| No ticket shows as "at risk" | The demo data is old. Delete `backend/opsdesk.db` and restart the backend. |
| The period filter shows the same count for every choice | All demo tickets are recent. That is expected until older tickets are added to the seed data. |
| Page looks unstyled or broken | Hard refresh with `Ctrl+Shift+R`, then check the frontend terminal for red errors. |
