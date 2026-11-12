"""OpsDesk API (FastAPI + SQLite).

Run:  uvicorn main:app --reload      then open http://127.0.0.1:8000/docs

This file only turns HTTP into calls to rules.py, where every business rule
lives. Every endpoint except /api/users (demo login list) and /api/suggest
(keyword helper) needs a user_id and is checked against that user's role.
"""
import csv
import io
import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator, model_validator

import db
import rules

db.load_env()


# ------------------------------------------------------------------- models

def _clean_text(value, limit):
    """Trim; empty becomes None; too long is rejected."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise ValueError(f"must be at most {limit} characters")
    return text


class TicketIn(BaseModel):
    user_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    agency: Optional[str] = None
    tboh: Optional[str] = None
    priority: str = "Normal"
    region: Optional[str] = None
    intent: Optional[str] = None
    check_in: str
    lcd: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        if len(v) > 200:
            raise ValueError("must be at most 200 characters")
        return v

    @field_validator("description")
    @classmethod
    def _description(cls, v):
        if v is not None and len(v) > 5000:
            raise ValueError("must be at most 5000 characters")
        return v

    @field_validator("agency", "tboh", "region", "intent")
    @classmethod
    def _short_text(cls, v):
        return _clean_text(v, 100)

    @field_validator("intent")
    @classmethod
    def _intent(cls, v):
        return v.lower() if v else v

    @field_validator("priority")
    @classmethod
    def _priority(cls, v):
        return rules.normalize_priority(v)

    @field_validator("check_in")
    @classmethod
    def _check_in(cls, v):
        return rules.normalize_dt_input(v)

    @field_validator("lcd")
    @classmethod
    def _lcd(cls, v):
        if v is None or not v.strip():
            return None
        return rules.normalize_dt_input(v)

    @model_validator(mode="after")
    def _lcd_before_check_in(self):
        if self.lcd and rules.parse_dt(self.lcd) > rules.parse_dt(self.check_in):
            raise ValueError("lcd cannot be later than check_in")
        return self


class EscalateIn(BaseModel):
    user_id: Optional[int] = None
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        if len(v) > 1000:
            raise ValueError("must be at most 1000 characters")
        return v


class AssignIn(BaseModel):
    user_id: Optional[int] = None
    agent_id: int


class StatusIn(BaseModel):
    user_id: Optional[int] = None
    status: str

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        return rules.normalize_status(v)


class SideIn(BaseModel):
    user_id: Optional[int] = None
    kind: str
    recipients: List[str] = []
    body: str

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        v = v.strip().lower()
        if v not in rules.SIDE_KINDS:
            raise ValueError("must be one of: internal, external")
        return v

    @field_validator("body")
    @classmethod
    def _body(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        if len(v) > 5000:
            raise ValueError("must be at most 5000 characters")
        return v


# ------------------------------------------------------------- dependencies

def get_db(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def acting_user(conn, query_user_id, body_user_id=None):
    """The user making the call: ?user_id= or user_id in the JSON body."""
    user_id = query_user_id if query_user_id is not None else body_user_id
    if user_id is None:
        raise HTTPException(status_code=422, detail="user_id is required")
    return rules.get_user(conn, user_id)


# ------------------------------------------------------------------- routes

router = APIRouter(prefix="/api")


@router.get("/users")
def list_users(conn=Depends(get_db)):
    """Demo login list. No real authentication in this hackathon build."""
    rows = conn.execute(
        "SELECT id, name, role, region, agency, available FROM users ORDER BY id"
    ).fetchall()
    return [{**dict(r), "available": bool(r["available"])} for r in rows]


@router.get("/suggest")
def suggest(text: str = Query("", max_length=2000)):
    return {"intent": rules.suggest_intent(text)}


@router.get("/tickets")
def list_tickets(
    user_id: int = Query(...),
    region: Optional[str] = Query(None, max_length=100),
    intent: Optional[str] = Query(None, max_length=100),
    unassigned: bool = Query(False),
    assignee: Optional[int] = Query(None),
    conn=Depends(get_db),
):
    user = rules.get_user(conn, user_id)
    return rules.list_tickets(conn, user, region=region, intent=intent,
                              unassigned=unassigned, assignee=assignee)


@router.post("/tickets")
def create_ticket(body: TicketIn, user_id: Optional[int] = Query(None), conn=Depends(get_db)):
    actor = acting_user(conn, user_id, body.user_id)
    fields = body.model_dump(exclude={"user_id"})
    return {"id": rules.create_ticket(conn, fields, actor)}


@router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id: int, user_id: int = Query(...), conn=Depends(get_db)):
    user = rules.get_user(conn, user_id)
    ticket = rules.get_ticket(conn, ticket_id, user)
    return rules.ticket_detail(conn, ticket, user)


@router.post("/tickets/{ticket_id}/escalate")
def escalate(ticket_id: int, body: EscalateIn, user_id: Optional[int] = Query(None),
             conn=Depends(get_db)):
    actor = acting_user(conn, user_id, body.user_id)
    ticket = rules.get_ticket(conn, ticket_id, actor)
    rules.escalate_ticket(conn, ticket, body.reason, actor)
    return {"ok": True}


@router.post("/tickets/{ticket_id}/assign")
def assign(ticket_id: int, body: AssignIn, user_id: Optional[int] = Query(None),
           conn=Depends(get_db)):
    actor = acting_user(conn, user_id, body.user_id)
    ticket = rules.get_ticket(conn, ticket_id, actor)
    rules.assign_ticket(conn, ticket, body.agent_id, actor)
    return {"ok": True}


@router.post("/tickets/{ticket_id}/status")
def change_status(ticket_id: int, body: StatusIn, user_id: Optional[int] = Query(None),
                  conn=Depends(get_db)):
    actor = acting_user(conn, user_id, body.user_id)
    ticket = rules.get_ticket(conn, ticket_id, actor)
    rules.set_status(conn, ticket, body.status, actor)
    return {"ok": True}


@router.post("/tickets/{ticket_id}/side")
def add_side(ticket_id: int, body: SideIn, user_id: Optional[int] = Query(None),
             conn=Depends(get_db)):
    actor = acting_user(conn, user_id, body.user_id)
    ticket = rules.get_ticket(conn, ticket_id, actor)
    rules.add_side_conversation(conn, ticket, body.kind, body.recipients, body.body, actor)
    return {"ok": True}


@router.get("/insights")
def insights(user_id: int = Query(...), conn=Depends(get_db)):
    user = rules.get_user(conn, user_id)
    return rules.insights(conn, user)


@router.get("/export")
def export_csv(request: Request, user_id: int = Query(...), conn=Depends(get_db)):
    user = rules.get_user(conn, user_id)
    rules.require_staff(user, "export tickets")  # fail now, not mid-stream
    path = request.app.state.db_path
    now = rules.now_local()

    def stream():
        # Own connection: the request's one may be closed before streaming ends.
        own = db.connect(path)
        try:
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(rules.EXPORT_COLUMNS)
            for n, row in enumerate(rules.iter_export_rows(own, user, now), start=1):
                writer.writerow(row)
                if n % 200 == 0:
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate(0)
            yield buffer.getvalue()
        finally:
            own.close()

    return StreamingResponse(
        stream(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="opsdesk_tickets.csv"'},
    )


# ---------------------------------------------------------------------- app

def create_app(db_path: Optional[str] = None, use_project_files: bool = True) -> FastAPI:
    """Build the app. Tests pass a temp db_path and use_project_files=False."""
    path = db_path or db.default_db_path()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if use_project_files:
            db.init_db(path)
        else:
            db.init_db(path, schema_file=None, seed_file=None)
        yield

    app = FastAPI(title="OpsDesk API", version="1.0", lifespan=lifespan)
    app.state.db_path = path

    # CORS only when the React dev server runs on another port:
    #   OPSDESK_CORS_ORIGINS=http://localhost:5173
    origins = [o.strip() for o in os.getenv("OPSDESK_CORS_ORIGINS", "").split(",") if o.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.exception_handler(rules.RuleError)
    async def rule_error_handler(request: Request, exc: rules.RuleError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        parts = []
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"] if p not in ("body", "query", "path"))
            message = str(err["msg"]).removeprefix("Value error, ")
            parts.append(f"{where}: {message}" if where else message)
        return JSONResponse(status_code=422, content={"detail": "; ".join(parts)})

    app.include_router(router)
    return app


app = create_app()
