"""Arul Motors CRM API (FastAPI): customers, bikes, service bookings, call logs,
dashboard stats, staff logins, and a relay that lets logged-in staff start a
voice-agent call for a customer.

Live updates: WebSocket /api/ws?token=<staff token>. Data changes, calls in
progress and notifications are streamed to every signed-in CRM.

Run:          python api.py                      (port API_PORT, default 8001)
Create user:  python api.py create-user USERNAME [--role admin] [--name "Full Name"]
"""

import argparse
import asyncio
import contextlib
import getpass
import json
import logging
import os
import secrets
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

import auth
import crm_export
import database

# import dialer  # Plivo disabled — pending client decision on telephony

load_dotenv(".env.local")
load_dotenv()

logger = logging.getLogger("arul-motors-api")

# How often the API checks the event table for new events to push (seconds),
# and how often it looks for missed bookings / prunes old events.
EVENT_POLL_SECS = 0.75
HOUSEKEEPING_SECS = 600


class LiveHub:
    """Connected CRM browsers. Events are read from the shared database (so
    changes made by the voice agent's separate process are seen too) and
    pushed to every socket."""

    def __init__(self) -> None:
        self.sockets: set[WebSocket] = set()

    async def broadcast(self, message: dict) -> None:
        text = json.dumps(message, default=str)
        for ws in list(self.sockets):
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001 - a dead socket is removed on its own disconnect
                self.sockets.discard(ws)

    async def run(self) -> None:
        last_id = await asyncio.to_thread(database.latest_event_id)
        last_housekeeping = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                if loop.time() - last_housekeeping > HOUSEKEEPING_SECS:
                    last_housekeeping = loop.time()
                    await asyncio.to_thread(database.flag_missed_appointments)
                    await asyncio.to_thread(database.prune_events)
                if self.sockets:
                    for event in await asyncio.to_thread(database.get_events_after, last_id):
                        last_id = event["id"]
                        await self.broadcast(event)
                else:
                    # Nobody to push to: just keep up with the latest id. A new
                    # socket gets a full snapshot ("hello") anyway.
                    last_id = await asyncio.to_thread(database.latest_event_id)
            except Exception:  # noqa: BLE001 - keep streaming after a transient DB error
                logger.exception("Live event loop error")
            await asyncio.sleep(EVENT_POLL_SECS)


hub = LiveHub()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(hub.run())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="Arul Motors Yamaha (Nazareth, Thoothukudi) CRM API", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Data version: every write appends an event, so (latest event id, showroom
# date) changes exactly when any CRM data can have changed. The date is part of
# it because "today", "missed" and "due" depend on it.
# ---------------------------------------------------------------------------
def data_version() -> str:
    return f"{database.latest_event_id()}-{database.today().isoformat()}"


class VersionedCache:
    """Results reused until the data version changes (no TTL guessing)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, object]] = {}

    def get(self, key: str, compute):
        version = data_version()
        with self._lock:
            hit = self._entries.get(key)
            if hit and hit[0] == version:
                return hit[1]
        value = compute()
        with self._lock:
            self._entries[key] = (version, value)
        return value

    def json_response(self, key: str, compute) -> Response:
        """Like get(), but caches the encoded JSON bytes, so a hit skips both
        the query and FastAPI's (slow, for big lists) serialization."""
        body = self.get(f"json:{key}", lambda: json.dumps(compute(), default=str, separators=(",", ":")).encode())
        return Response(content=body, media_type="application/json")

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


cache = VersionedCache()

# GET routes whose response depends only on CRM data (not on who asks), so
# they can answer 304 Not Modified when nothing changed.
ETAG_PREFIXES = (
    "/api/stats", "/api/customers", "/api/vehicles", "/api/due-services", "/api/appointments",
    "/api/calls", "/api/service-types", "/api/notifications",
)


def _has_valid_credentials(request: Request) -> bool:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return auth.read_token(authorization.split(" ", 1)[1].strip()) is not None
    expected = os.getenv("API_KEY")
    key = request.headers.get("x-api-key")
    return bool(expected and key and secrets.compare_digest(key, expected))


@app.middleware("http")
async def etag_middleware(request: Request, call_next):
    """Conditional GETs: browsers revalidate with If-None-Match and get an
    empty 304 when the data version hasn't changed (fetch() handles it)."""
    cacheable = request.method == "GET" and request.url.path.startswith(ETAG_PREFIXES)
    if not cacheable or not _has_valid_credentials(request):
        return await call_next(request)
    etag = f'W/"{await asyncio.to_thread(data_version)}"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response = await call_next(request)
    if response.status_code == 200:
        response.headers.update(headers)
    return response


class LoginThrottle:
    """At most MAX_FAILURES failed sign-ins per client per WINDOW_SECS."""

    MAX_FAILURES = 10
    WINDOW_SECS = 15 * 60

    def __init__(self) -> None:
        self._failures: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _recent(self, client: str) -> deque:
        now = time.monotonic()
        attempts = self._failures.setdefault(client, deque())
        while attempts and now - attempts[0] > self.WINDOW_SECS:
            attempts.popleft()
        return attempts

    def blocked(self, client: str) -> bool:
        with self._lock:
            return len(self._recent(client)) >= self.MAX_FAILURES

    def failed(self, client: str) -> None:
        with self._lock:
            self._recent(client).append(time.monotonic())

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


login_throttle = LoginThrottle()


def _client_ip(request: Request) -> str:
    # Behind Caddy the browser's address is the first X-Forwarded-For entry.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")

# The CRM frontend (static site on GoDaddy) calls this API from the browser.
# CORS_ORIGINS lists the sites allowed to, comma-separated, e.g.
# "https://crm.arulmotors.com,https://arulmotors.com". Unset = any (local dev).
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Lets the CRM (another domain) read download file names.
    expose_headers=["Content-Disposition"],
)

# Public base URL of the Pipecat voice bot (browser test page).
BOT_URL = os.getenv("BOT_URL", "http://localhost:7860")
# Where this API reaches the bot to relay call setup (same machine in production).
BOT_INTERNAL_URL = os.getenv("BOT_INTERNAL_URL", "http://127.0.0.1:7860")
# STUN/TURN servers handed to the browser for the call audio.
ICE_SERVERS = os.getenv("PIPECAT_ICE_SERVERS", "stun:stun.l.google.com:19302")

# Initialize the database (shared with the agent via database.DB_FILE).
database.init_db()


def bootstrap_admin() -> None:
    """Create the first admin from CRM_ADMIN_USERNAME / CRM_ADMIN_PASSWORD when
    there are no staff users yet (e.g. a fresh server)."""
    password = os.getenv("CRM_ADMIN_PASSWORD")
    if not password or database.count_staff_users():
        return
    username = os.getenv("CRM_ADMIN_USERNAME", "admin")
    database.create_staff_user(username, auth.hash_password(password), "Administrator", "admin")


bootstrap_admin()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def current_user(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> dict:
    """Staff member from `Authorization: Bearer <token>`. Scripts may instead
    send `X-API-Key` when API_KEY is set (treated as an admin)."""
    if authorization and authorization.lower().startswith("bearer "):
        payload = auth.read_token(authorization.split(" ", 1)[1].strip())
        user = database.get_staff_user(user_id=payload["sub"]) if payload else None
        if user:
            return user
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    expected = os.getenv("API_KEY")
    if expected and x_api_key and secrets.compare_digest(x_api_key, expected):
        return {"id": 0, "username": "api-key", "full_name": "API key", "role": "admin"}
    raise HTTPException(status_code=401, detail="Sign in required.")


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admins only.")
    return user


def _check(result: tuple, not_found: bool = False):
    """Turn a database (ok, message, ...) result into a response or an HTTP error."""
    ok, message = result[0], result[1]
    if not ok:
        status = 404 if not_found or "not found" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message)
    return {"message": message, **({"id": result[2]} if len(result) > 2 else {})}


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    client = _client_ip(request)
    if login_throttle.blocked(client):
        raise HTTPException(status_code=429, detail="Too many failed sign-ins. Try again in 15 minutes.")
    user = database.get_staff_user(username=body.username, with_hash=True)
    if not user or not auth.verify_password(body.password, user["password_hash"]):
        login_throttle.failed(client)
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    login_throttle.succeeded(client)
    user.pop("password_hash")
    return {"token": auth.create_token(user), "user": user}


@app.get("/api/health")
def health():
    return {"status": "ok", "time": database.now().isoformat(timespec="seconds")}


@app.websocket("/api/ws")
async def live_socket(websocket: WebSocket, token: str = ""):
    """Live CRM feed. Browsers can't send headers on a WebSocket, so the staff
    token comes as ?token=. Messages: {"id", "type", "data", "at"} with type
    data.changed, call.started, call.ended or notification; first a "hello"
    snapshot with calls in progress and recent notifications."""
    payload = auth.read_token(token) if token else None
    user = await asyncio.to_thread(database.get_staff_user, user_id=payload["sub"]) if payload else None
    if not user:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    hub.sockets.add(websocket)
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "data": {
                        "user": user,
                        "active_calls": await asyncio.to_thread(database.active_voice_calls),
                        "notifications": await asyncio.to_thread(database.recent_notifications),
                    },
                },
                default=str,
            )
        )
        while True:
            if await websocket.receive_text() == "ping":
                await websocket.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        hub.sockets.discard(websocket)


api = APIRouter(prefix="/api", dependencies=[Depends(current_user)])


@api.get("/auth/me")
def me(user: dict = Depends(current_user)):
    return user


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


@api.post("/auth/password")
def change_password(body: PasswordIn, user: dict = Depends(current_user)):
    stored = database.get_staff_user(user_id=user["id"], with_hash=True)
    if not stored or not auth.verify_password(body.current_password, stored["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is wrong.")
    if problem := auth.password_problem(body.new_password):
        raise HTTPException(status_code=400, detail=problem)
    database.set_staff_password(user["id"], auth.hash_password(body.new_password))
    return {"message": "Password changed."}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@api.get("/stats")
def stats():
    """Totals, charts and short lists for the CRM dashboard."""
    return cache.json_response("stats", database.get_dashboard_stats)


# ---------------------------------------------------------------------------
# Customers
# ---------------------------------------------------------------------------
class CustomerIn(BaseModel):
    name: str
    mobile_number: str
    alternate_mobile: str | None = None


class CustomerPatch(BaseModel):
    name: str | None = None
    mobile_number: str | None = None
    alternate_mobile: str | None = None  # "" removes it; swap both to change the called number


@api.get("/customers")
def get_customers(q: str | None = None):
    """Customers with their bikes, next booking and last call. `q` searches
    name, mobile, registration and model."""
    if q:
        return database.list_customers(q)
    return cache.json_response("customers", database.list_customers)


@api.post("/customers", status_code=201)
def create_customer(body: CustomerIn):
    return _check(database.create_customer(body.name, body.mobile_number, body.alternate_mobile))


@api.get("/customers/{customer_id}")
def get_customer(customer_id: int):
    customer = database.get_customer_detail(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return customer


@api.patch("/customers/{customer_id}")
def update_customer(customer_id: int, body: CustomerPatch):
    return _check(database.update_customer(customer_id, body.name, body.mobile_number, body.alternate_mobile))


@api.delete("/customers/{customer_id}")
def delete_customer(customer_id: int):
    return _check(database.delete_customer(customer_id))


# ---------------------------------------------------------------------------
# Bikes
# ---------------------------------------------------------------------------
class VehicleIn(BaseModel):
    name: str
    mobile_number: str
    registration_number: str
    model: str
    next_service_date: str
    next_service_type: str
    purchase_date: str | None = None
    odometer_km: int | None = None
    last_service_date: str | None = None
    alternate_mobile: str | None = None  # used only when this creates the customer


class VehiclePatch(BaseModel):
    registration_number: str | None = None
    model: str | None = None
    purchase_date: str | None = None
    last_service_date: str | None = None
    odometer_km: int | None = None
    next_service_date: str | None = None
    next_service_type: str | None = None
    frame_number: str | None = None
    engine_number: str | None = None


@api.get("/vehicles")
def get_vehicles():
    return database.list_vehicles()


@api.post("/vehicles", status_code=201)
def add_vehicle(vehicle: VehicleIn):
    """Add a bike sale (and the customer, if the mobile number is new)."""
    return _check(database.add_customer_vehicle(**vehicle.model_dump()))


@api.patch("/vehicles/{vehicle_id}")
def update_vehicle(vehicle_id: int, body: VehiclePatch):
    return _check(database.update_vehicle(vehicle_id, **body.model_dump()))


@api.delete("/vehicles/{vehicle_id}")
def delete_vehicle(vehicle_id: int):
    return _check(database.delete_vehicle(vehicle_id))


@api.get("/due-services")
def get_due_services(days: int = 7):
    """The follow-up call list: bikes due within `days` with no appointment yet."""
    return database.get_due_services(days)


class ServiceTypeIn(BaseModel):
    name: str
    description: str | None = None
    is_free: bool = False
    interval_days: int | None = None
    interval_km: int | None = None
    price: int | None = None
    overdue_days: int | None = None  # days after the due date before it's overdue
    active: bool = True


class ServiceTypePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    is_free: bool | None = None
    interval_days: int | None = None
    interval_km: int | None = None
    price: int | None = None
    overdue_days: int | None = None
    active: bool | None = None


@api.get("/service-types")
def get_service_types(active_only: bool = False):
    """Service types in schedule order, with usage counts."""
    return cache.json_response(f"service-types:{active_only}", lambda: database.list_service_types(active_only))


@api.post("/service-types", status_code=201)
def create_service_type(body: ServiceTypeIn):
    return _check(database.create_service_type(**body.model_dump()))


@api.patch("/service-types/{type_id}")
def update_service_type(type_id: int, body: ServiceTypePatch):
    # Only fields that were sent; an explicit null clears interval/price.
    return _check(database.update_service_type(type_id, **body.model_dump(exclude_unset=True)))


@api.post("/service-types/{type_id}/move")
def move_service_type(type_id: int, direction: str = Query(pattern="^(up|down)$")):
    return _check(database.move_service_type(type_id, direction))


@api.delete("/service-types/{type_id}")
def delete_service_type(type_id: int):
    return _check(database.delete_service_type(type_id))


# ---------------------------------------------------------------------------
# Service appointments
# ---------------------------------------------------------------------------
class AppointmentIn(BaseModel):
    customer_id: int
    appointment_date: str
    registration_number: str | None = None
    service_type: str | None = None
    notes: str | None = None


class AppointmentPatch(BaseModel):
    appointment_date: str | None = None
    notes: str | None = None


class CompleteIn(BaseModel):
    next_service_date: str | None = None
    next_service_type: str | None = None


@api.get("/appointments")
def get_appointments(
    state: str | None = Query(default=None, description="upcoming, completed, cancelled, missed"),
    customer_id: int | None = None,
):
    """Service appointments (newest date first), each with its `state`."""
    rows = database.get_all_appointments()
    for row in rows:
        row["state"] = database.appointment_state(row)
    if state:
        rows = [r for r in rows if r["state"] == state.strip().lower()]
    if customer_id:
        rows = [r for r in rows if r["customer_id"] == customer_id]
    return sorted(rows, key=lambda r: r["appointment_date"], reverse=True)


@api.get("/appointments/search")
def search_appointments(
    customer_name: str | None = None,
    mobile_number: str | None = None,
    registration_number: str | None = None,
    model: str | None = None,
    service_type: str | None = None,
    state: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    appointment_id: str | None = None,
):
    """Search previous and upcoming bookings (partial / fuzzy matching)."""
    return database.search_appointments(
        customer_name=customer_name,
        mobile_number=mobile_number,
        registration_number=registration_number,
        model=model,
        service_type=service_type,
        state=state,
        date_from=date_from,
        date_to=date_to,
        appointment_id=appointment_id,
    )


def _actor(user: dict) -> str:
    return f"staff:{user['username']}"


@api.post("/appointments", status_code=201)
def create_appointment(body: AppointmentIn, user: dict = Depends(current_user)):
    """Book a service date for a customer's bike (from the CRM)."""
    return _check(
        database.create_appointment(
            body.customer_id,
            body.appointment_date,
            registration_number=body.registration_number,
            service_type=body.service_type,
            notes=body.notes,
            actor=_actor(user),
        )
    )


def _appointment_or_404(appointment_id: str) -> dict:
    appointment = database.get_appointment(appointment_id)
    if not appointment:
        raise HTTPException(status_code=404, detail=f"No appointment {appointment_id}.")
    return appointment


@api.get("/appointments/{appointment_id}")
def get_appointment(appointment_id: str):
    return _appointment_or_404(appointment_id)


@api.patch("/appointments/{appointment_id}")
def update_appointment(appointment_id: str, body: AppointmentPatch, user: dict = Depends(current_user)):
    appointment = _appointment_or_404(appointment_id)
    return _check(
        database.update_appointment(
            appointment["customer_id"],
            appointment_id=appointment["id"],
            new_date=body.appointment_date,
            notes=body.notes,
            actor=_actor(user),
        )
    )


@api.post("/appointments/{appointment_id}/cancel")
def cancel_appointment(appointment_id: str, user: dict = Depends(current_user)):
    appointment = _appointment_or_404(appointment_id)
    return _check(database.cancel_appointment(appointment["customer_id"], appointment["id"], actor=_actor(user)))


@api.post("/appointments/{appointment_id}/complete")
def complete_appointment(appointment_id: str, body: CompleteIn | None = None):
    """Workshop: mark a bike as serviced, optionally setting its next due service."""
    body = body or CompleteIn()
    return _check(
        database.complete_appointment(appointment_id, body.next_service_date, body.next_service_type),
        not_found=True,
    )


def _json_field(raw: str | None, field: str):
    if raw in (None, ""):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {field}.") from None


@api.post("/appointments/{appointment_id}/close", status_code=201)
def close_service(
    appointment_id: str,
    service_date: str = Form(..., description="Delivered on (YYYY-MM-DD)"),
    report: UploadFile = File(..., description="Scanned job card / service report (PDF)"),
    received_date: str | None = Form(None),
    service_type: str | None = Form(None),
    job_type: str | None = Form(None),
    job_card_no: str | None = Form(None),
    odometer_km: str | None = Form(None),
    frame_number: str | None = Form(None),
    engine_number: str | None = Form(None),
    customer_requests: str | None = Form(None),
    jobs_done: str | None = Form(None, description='JSON list, e.g. ["Water wash", "Chain lube"]'),
    work_done: str | None = Form(None),
    parts: str | None = Form(None, description="JSON list of {part_no, name, qty, amount, warranty}"),
    labour: str | None = Form(None, description="JSON list of {description, amount}"),
    delivery_checks: str | None = Form(None, description="JSON list of ticked delivery checks"),
    feedback: str | None = Form(None, description="JSON {rating, delivery_on_time, washing_quality, ...}"),
    technician: str | None = Form(None),
    mechanic: str | None = Form(None),
    supervisor: str | None = Form(None),
    remarks: str | None = Form(None),
    next_service_date: str | None = Form(None),
    next_service_type: str | None = Form(None),
    user: dict = Depends(current_user),
):
    """Workshop: close a booking with its job card (multipart form): the job
    card's fields and the scanned PDF. Saves the maintenance record and moves
    the bike on to its next service. Totals are computed by the server."""
    details = {
        "service_date": service_date, "received_date": received_date, "service_type": service_type,
        "job_type": job_type, "job_card_no": job_card_no, "odometer_km": odometer_km,
        "frame_number": frame_number, "engine_number": engine_number,
        "customer_requests": customer_requests, "work_done": work_done,
        "jobs_done": _json_field(jobs_done, "jobs done"), "parts": _json_field(parts, "parts"),
        "labour": _json_field(labour, "labour"), "delivery_checks": _json_field(delivery_checks, "delivery checks"),
        "feedback": _json_field(feedback, "feedback"),
        "technician": technician, "mechanic": mechanic, "supervisor": supervisor, "remarks": remarks,
        "next_service_date": next_service_date, "next_service_type": next_service_type,
    }
    pdf = report.file.read(database.MAX_PDF_BYTES + 1)  # one byte over = too big
    ok, message, record_id = database.close_service(
        appointment_id, details, pdf, report.filename, actor=user.get("full_name") or user["username"]
    )
    if not ok:
        raise HTTPException(status_code=404 if message.startswith("No open booking") else 400, detail=message)
    return {"message": message, "id": record_id}


@api.get("/vehicles/{vehicle_id}/service-history")
def service_history(vehicle_id: int):
    """A bike's maintenance records (and older completed services), newest first."""
    return database.get_service_history(vehicle_id)


@api.get("/service-records/{record_id}/report")
def service_report(record_id: int):
    """The service report PDF of a maintenance record (opens in the browser)."""
    document = database.get_service_document(record_id)
    if not document:
        raise HTTPException(status_code=404, detail="No service report for this record.")
    return Response(
        document["data"],
        media_type=document["content_type"],
        headers={
            "Content-Disposition": f'inline; filename="{document["filename"]}"',
            "Cache-Control": "private, no-store",
        },
    )


# ---------------------------------------------------------------------------
# Call logs
# ---------------------------------------------------------------------------
class CallLogIn(BaseModel):
    customer_id: int
    outcome: str
    vehicle_id: int | None = None
    callback_date: str | None = None
    notes: str | None = None


CALL_LOG_LIMIT = 1000


@api.get("/calls/count")
def count_calls():
    """Total calls ever logged (the list itself is capped)."""
    return {"total": database.count_call_logs()}


@api.get("/calls")
def get_calls(customer_id: int | None = None, limit: int = Query(default=CALL_LOG_LIMIT, ge=1, le=5000)):
    """Call log, newest first: the latest `limit` calls (default 1000)."""
    return database.get_call_logs(customer_id, limit=limit)


@api.get("/call-outcomes")
def get_call_outcomes():
    return sorted(database.CALL_OUTCOMES)


@api.post("/calls/log", status_code=201)
def log_call(body: CallLogIn, user: dict = Depends(current_user)):
    """Record the outcome of a call made by staff (not the agent)."""
    if not database.get_customer(body.customer_id):
        raise HTTPException(status_code=404, detail="Customer not found.")
    return _check(
        database.log_call(
            body.customer_id, body.outcome, body.vehicle_id, body.callback_date, body.notes, actor=_actor(user)
        )
    )


@api.get("/notifications")
def get_notifications(limit: int = 30):
    """Recent staff notifications (newest first); the live feed pushes new ones."""
    return database.recent_notifications(min(max(limit, 1), 100))


@api.get("/voice/calls/active")
def get_active_calls():
    """Voice-agent calls in progress right now."""
    return database.active_voice_calls()


# [Plivo disabled — pending client decision on telephony]
# @api.post("/calls/{customer_id}")
# async def start_call(customer_id: int):
#     """Place an outbound reminder call to the customer via Plivo."""
#     try:
#         return await dialer.place_call(customer_id)
#     except ValueError as e:
#         raise HTTPException(status_code=404, detail=str(e)) from e
#     except RuntimeError as e:
#         raise HTTPException(status_code=502, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Voice agent: start a browser call for a customer, from the CRM
# ---------------------------------------------------------------------------
def _ice_servers() -> list[dict]:
    value = ICE_SERVERS.strip()
    if value.startswith("["):
        return json.loads(value)
    return [{"urls": url.strip()} for url in value.split(",") if url.strip()]


# ---------------------------------------------------------------------------
# Settings (read: any staff; change: admins)
# ---------------------------------------------------------------------------
class SettingsPatch(BaseModel):
    voice_agent_enabled: bool | None = None


@api.get("/settings")
def get_settings():
    return database.get_settings()


@api.patch("/settings")
def update_settings(body: SettingsPatch, user: dict = Depends(admin_user)):
    if body.voice_agent_enabled is not None:
        database.set_voice_agent_enabled(body.voice_agent_enabled, user.get("full_name") or user["username"])
    return database.get_settings()


VOICE_OFF = "The voice agent is turned off. An admin can turn it on in Settings."


# ---------------------------------------------------------------------------
# Data downloads (admins): Excel export and a copy of the database
# ---------------------------------------------------------------------------
def _download(content: bytes, media_type: str, name: str) -> Response:
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


@api.get("/admin/export.xlsx", dependencies=[Depends(admin_user)])
def export_excel():
    """The CRM as an Excel workbook (customers & bikes, bookings, calls, service types)."""
    return _download(
        crm_export.build_workbook(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        f"arul-motors-crm-{database.today().isoformat()}.xlsx",
    )


@api.get("/admin/backup.db", dependencies=[Depends(admin_user)])
def download_backup():
    """A copy of the whole database (SQLite), for safekeeping or a restore."""
    try:
        content = database.backup_bytes()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _download(content, "application/vnd.sqlite3", f"arul-motors-backup-{database.today().isoformat()}.db")


@api.get("/voice/config")
def voice_config():
    """What the CRM's call panel needs: the relay endpoint and ICE servers."""
    return {"offer_path": "/api/voice/offer", "ice_servers": _ice_servers()}


@api.get("/voice/status")
async def voice_status():
    """Whether the voice agent is switched on and reachable from the API."""
    if not await asyncio.to_thread(database.voice_agent_enabled):
        return {"online": False, "enabled": False}
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{BOT_INTERNAL_URL}/client/")
        return {"online": response.status_code < 500, "enabled": True}
    except httpx.HTTPError:
        return {"online": False, "enabled": True}


async def _relay_to_bot(method: str, payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, f"{BOT_INTERNAL_URL}/api/offer", json=payload)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail="The voice agent is not reachable.") from e
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Voice agent error ({response.status_code}).")
    return response.json()


@api.post("/voice/offer")
async def voice_offer(request: Request, user: dict = Depends(current_user)):
    """Relay the browser's WebRTC offer to the bot. The customer to call comes
    from `requestData.customer_id` and is checked here, so only signed-in
    staff can start calls, for real customers."""
    if not await asyncio.to_thread(database.voice_agent_enabled):
        raise HTTPException(status_code=403, detail=VOICE_OFF)
    payload = await request.json()
    request_data = payload.pop("requestData", None) or payload.get("request_data") or {}
    try:
        customer_id = int(request_data.get("customer_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="customer_id is required.") from None
    if not await asyncio.to_thread(database.get_customer, customer_id):
        raise HTTPException(status_code=404, detail="Customer not found.")
    payload["request_data"] = {"customer_id": customer_id, "started_by": user.get("full_name") or user["username"]}
    return await _relay_to_bot("POST", payload)


@api.patch("/voice/offer")
async def voice_ice_candidates(request: Request):
    """Relay trickled ICE candidates for an ongoing call setup."""
    return await _relay_to_bot("PATCH", await request.json())


@api.get("/connection")
def get_connection(customer_id: int | None = None):
    """Direct bot connection details (browser test page without the relay)."""
    return {
        "offerUrl": f"{BOT_URL}/api/offer",
        "requestData": {"customer_id": customer_id} if customer_id else {},
    }


# ---------------------------------------------------------------------------
# Staff users (admins only)
# ---------------------------------------------------------------------------
class StaffIn(BaseModel):
    username: str
    password: str
    full_name: str | None = None
    role: str = "staff"


@api.get("/staff", dependencies=[Depends(admin_user)])
def get_staff():
    return database.list_staff_users()


@api.post("/staff", status_code=201, dependencies=[Depends(admin_user)])
def create_staff(body: StaffIn):
    if problem := auth.password_problem(body.password):
        raise HTTPException(status_code=400, detail=problem)
    return _check(
        database.create_staff_user(body.username, auth.hash_password(body.password), body.full_name, body.role)
    )


@api.delete("/staff/{user_id}")
def delete_staff(user_id: int, user: dict = Depends(admin_user)):
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="You can't delete your own account.")
    return _check(database.delete_staff_user(user_id))


app.include_router(api)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def _create_user_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python api.py create-user", description="Create a CRM login")
    parser.add_argument("username")
    parser.add_argument("--name", default=None, help="Full name shown in the CRM")
    parser.add_argument("--role", default="staff", choices=sorted(database.STAFF_ROLES))
    args = parser.parse_args(argv)
    password = getpass.getpass("Password: ")
    if problem := auth.password_problem(password):
        print(problem)
        return 1
    if getpass.getpass("Confirm password: ") != password:
        print("Passwords don't match.")
        return 1
    ok, message, _ = database.create_staff_user(args.username, auth.hash_password(password), args.name, args.role)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "create-user":
        sys.exit(_create_user_cli(sys.argv[2:]))

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("API_PORT", "8001")))
