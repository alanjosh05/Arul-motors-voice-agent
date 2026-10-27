"""Tests for the Arul Motors (Yamaha) service follow-up tools and database layer.

The database tests are deterministic (no network) and run against a temporary
SQLite file. Dates are computed relative to today so they never go stale.

They also run against MySQL when TEST_MYSQL_HOST is set (plus TEST_MYSQL_PORT,
TEST_MYSQL_USER, TEST_MYSQL_PASSWORD, TEST_MYSQL_DATABASE). Every table in that
database is dropped before each test, so never point it at real data.
"""

import os
from datetime import date, timedelta

import pytest

MYSQL_TABLES = ["call_logs", "service_appointments", "vehicles", "customers"]
BACKENDS = ["sqlite"] + (["mysql"] if os.getenv("TEST_MYSQL_HOST") else [])


def days_ahead(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


@pytest.fixture(autouse=True)
def no_mysql_by_default(monkeypatch):
    """A MYSQL_HOST in the developer's environment must not leak into tests."""
    for name in ("MYSQL_HOST", "MYSQL_SSL", "MYSQL_SSL_CA", "SEED_DEMO"):
        monkeypatch.delenv(name, raising=False)
    # The API's result cache is keyed by data version (latest event id + date);
    # each test has a fresh database whose ids restart, so start it empty.
    import sys

    if "api" in sys.modules:
        sys.modules["api"].cache.clear()


@pytest.fixture(params=BACKENDS)
def db(request, tmp_path, monkeypatch):
    """Provide a fresh, isolated database module for each test."""
    import database

    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
    # Tests run at any hour: the CRM is open all day unless a test says otherwise.
    monkeypatch.setattr(database, "SHIFT_START", "00:00")
    monkeypatch.setattr(database, "SHIFT_END", "24:00")
    # Tests book "n days ahead", which may be a Sunday: open every day unless a
    # test sets closed days itself.
    monkeypatch.setattr(database, "CLOSED_WEEKDAYS", set())
    if request.param == "mysql":
        for name in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE"):
            monkeypatch.setenv(f"MYSQL_{name}", os.environ.get(f"TEST_MYSQL_{name}", ""))
        monkeypatch.setenv("SEED_DEMO", "1")
        conn = database._connect()
        cursor = conn.cursor()
        for table in MYSQL_TABLES:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
        conn.close()
    database.init_db()
    return database


def test_seed_and_due_list(db):
    customers = db.get_all_customers()
    assert len(customers) == 4
    assert all(c["model"].startswith("Yamaha") for c in customers)
    assert all(c["purchase_date"] for c in customers)
    # Every seeded bike is due within a week and has no service date yet.
    assert len(db.get_due_services(within_days=7)) == 4
    assert len(db.get_due_services(within_days=2)) == 1  # only Anitha's Fascino


def test_normalize_date(db):
    assert db.normalize_date("tomorrow") == days_ahead(1)
    assert db.normalize_date("25/12/2026") == "2026-12-25"
    assert db.normalize_date("someday") is None


def test_create_appointment_date_only(db):
    ok, msg, appointment_id = db.create_appointment(1, days_ahead(2))
    assert ok, msg
    assert appointment_id == "AM1004"  # AM1001-AM1003 are the seeded past services
    # The message carries the weekday so the agent can say "Tuesday" etc.
    assert date.fromisoformat(days_ahead(2)).strftime("%A") in msg
    assert all(row["customer_id"] != 1 for row in db.get_due_services(7))

    # The appointment is stored in the DB (service_appointments table).
    stored = [a for a in db.get_all_appointments() if a["status"] == "CONFIRMED"]
    assert len(stored) == 1
    assert stored[0]["appointment_date"] == days_ahead(2)
    assert stored[0]["customer_name"] == "Karthik Raja"
    assert stored[0]["service_type"] == "First free service"
    assert stored[0]["status"] == "CONFIRMED"

    # A second appointment for the same bike is refused (update it instead).
    ok, msg, _ = db.create_appointment(1, days_ahead(3))
    assert ok is False and "update that appointment" in msg.lower()


def test_create_with_custom_service_and_notes(db):
    ok, msg, _ = db.create_appointment(
        2, days_ahead(1), service_type="General check-up", notes="Brake noise"
    )
    assert ok, msg
    appt = db.get_appointments(2)[0]
    assert appt["service_type"] == "General check-up"
    assert appt["notes"] == "Brake noise"


def test_no_slot_limit(db):
    # No time slots or capacity: every customer can pick the same day.
    for customer_id in range(1, 5):
        ok, msg, _ = db.create_appointment(customer_id, days_ahead(1))
        assert ok, msg


def test_rejects_bad_dates(db, monkeypatch):
    ok, msg, _ = db.create_appointment(1, days_ahead(-1))
    assert ok is False and "past" in msg.lower()

    ok, msg, _ = db.create_appointment(1, days_ahead(db.MAX_DAYS_AHEAD + 1))
    assert ok is False

    ok, msg, _ = db.create_appointment(1, "someday")
    assert ok is False and "understand" in msg.lower()

    # Closed days are configurable (open every day by default).
    closed = date.fromisoformat(days_ahead(1)).weekday()
    monkeypatch.setattr(db, "CLOSED_WEEKDAYS", {closed})
    ok, msg, _ = db.create_appointment(1, days_ahead(1))
    assert ok is False and "closed" in msg.lower()


def test_update_and_cancel(db):
    _, _, appointment_id = db.create_appointment(2, days_ahead(1))

    ok, msg = db.update_appointment(2, new_date=days_ahead(4))
    assert ok, msg
    assert db.get_appointments(2)[0]["appointment_date"] == days_ahead(4)

    ok, msg = db.update_appointment(2, appointment_id, notes="Check chain")
    assert ok, msg
    assert db.get_appointments(2)[0]["notes"] == "Check chain"

    assert db.update_appointment(2)[0] is False  # nothing to change
    assert db.update_appointment(2, new_date=days_ahead(-1))[0] is False

    # Another customer can't touch this appointment.
    assert db.cancel_appointment(3, appointment_id)[0] is False
    assert db.update_appointment(3, appointment_id, notes="x")[0] is False

    ok, msg = db.cancel_appointment(2, appointment_id.lower())
    assert ok, msg
    assert db.get_appointments(2) == []
    # Cancelling deletes the booking and its notifications: only the real
    # (completed) service history is left.
    history = db.get_appointments(2, include_cancelled=True)
    assert {h["status"] for h in history} == {"COMPLETED"}
    assert not db.event_ref_exists(f"booking:{appointment_id}")
    assert db.search_appointments(appointment_id=appointment_id) == []


def test_restart_never_invents_service_history(db):
    """Editing a bike's last service date, then restarting, must not add a
    'completed service' the customer never had (only demo seeding does)."""
    bike = db.get_customer(1)["vehicles"][0]  # Karthik: never serviced
    before = len(db.get_appointments(1, include_cancelled=True))
    assert db.update_vehicle(bike["id"], last_service_date=days_ahead(-20))[0]
    db.init_db()  # what a service restart does
    assert len(db.get_appointments(1, include_cancelled=True)) == before


def test_clock_uses_showroom_timezone(db):
    assert db.now().tzinfo is not None
    assert db.now().utcoffset().total_seconds() == 5.5 * 3600  # IST
    assert db.today() == db.now().date()


def test_migrates_old_service_visits_table(tmp_path, monkeypatch):
    import sqlite3

    import database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE service_visits (id TEXT PRIMARY KEY, vehicle_id INTEGER, "
        "customer_id INTEGER, visit_date TEXT, service_type TEXT, notes TEXT, "
        "status TEXT DEFAULT 'CONFIRMED', created_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO service_visits VALUES ('AM1001', 4, 4, '2026-10-25', 'Paid service', NULL, 'CONFIRMED', NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_FILE", str(path))
    database.init_db()
    rows = database.get_appointments(4)
    assert rows[0]["id"] == "AM1001" and rows[0]["appointment_date"] == "2026-10-25"


def test_seeded_service_history(db):
    completed = db.search_appointments(state="completed")
    # Every seeded bike that was serviced before has one previous service.
    assert {r["customer_name"] for r in completed} == {"Priya Lakshmi", "Suresh Kumar", "Anitha Devi"}
    priya = db.search_appointments(customer_name="Priya", state="completed")[0]
    assert priya["service_type"] == "First free service"  # she is due for the second
    # Re-running init_db doesn't duplicate history.
    db.init_db()
    assert len(db.search_appointments(state="completed")) == 3


def test_search_by_each_field(db):
    db.create_appointment(1, days_ahead(2), notes="Chain noise")

    def ids(**filters):
        return {r["customer_name"] for r in db.search_appointments(**filters)}

    assert ids(customer_name="karthik") == {"Karthik Raja"}
    assert ids(customer_name="Karthick") == {"Karthik Raja"}  # spoken spelling
    assert ids(mobile_number="3210") == {"Karthik Raja"}
    assert ids(registration_number="tn09ab1234") == {"Karthik Raja"}
    assert ids(registration_number="TN 22 EF 9012") == {"Suresh Kumar"}
    assert ids(registration_number="9012") == {"Suresh Kumar"}
    assert ids(model="fzs") == {"Karthik Raja"}
    assert ids(model="R15") == {"Suresh Kumar"}
    assert ids(service_type="first free") == {"Karthik Raja", "Priya Lakshmi"}
    assert ids(state="upcoming") == {"Karthik Raja"}
    assert ids(customer_name="Nobody") == set()

    # Filters combine with AND; date range is inclusive.
    assert ids(model="Ray", state="completed") == {"Priya Lakshmi"}
    assert ids(model="Ray", state="upcoming") == set()
    assert ids(date_from=days_ahead(1), date_to=days_ahead(3)) == {"Karthik Raja"}

    # Newest first.
    rows = db.search_appointments()
    assert [r["appointment_date"] for r in rows] == sorted(
        (r["appointment_date"] for r in rows), reverse=True
    )


def test_complete_appointment(db):
    _, _, appointment_id = db.create_appointment(1, days_ahead(1))
    ok, msg = db.complete_appointment(
        appointment_id, next_service_date=days_ahead(25), next_service_type="Second free service"
    )
    assert ok, msg
    row = db.search_appointments(appointment_id=appointment_id)[0]
    assert row["state"] == "completed"
    bike = db.get_customer(1)["vehicles"][0]
    assert bike["last_service_date"] == days_ahead(1)
    assert bike["next_service_type"] == "Second free service"
    assert db.complete_appointment(appointment_id)[0] is False  # already done


def test_call_log(db):
    ok, _ = db.log_call(1, "callback_requested", vehicle_id=1, callback_date="tomorrow")
    assert ok
    log = db.get_call_logs()[0]
    assert log["outcome"] == "callback_requested"
    assert log["callback_date"] == days_ahead(1)

    assert db.log_call(1, "bike_sold")[0] is True
    assert db.log_call(1, "maybe")[0] is False


def test_add_bike_sale(db):
    ok, msg = db.add_customer_vehicle(
        "Ravi", "9000000001", "tn 01 zz 0001", "Yamaha MT-15", days_ahead(20),
        "First free service", purchase_date="today",
    )
    assert ok, msg
    dup_ok, _ = db.add_customer_vehicle(
        "Ravi", "9000000001", "TN 01 ZZ 0001", "Yamaha MT-15", days_ahead(20),
        "First free service",
    )
    assert dup_ok is False


# [Plivo disabled — pending client decision on telephony]
# def test_dialer_plivo_numbers_and_xml():
#     import dialer
#
#     assert dialer.to_plivo_number("9876543210") == "919876543210"
#     assert dialer.to_plivo_number("+91 98765 43210") == "919876543210"
#     assert dialer.build_answer_url("abc.ngrok.app", 3) == (
#         "https://abc.ngrok.app/plivo/answer?customer_id=3"
#     )
#     xml = dialer.build_stream_xml("abc.ngrok.app")
#     assert 'bidirectional="true"' in xml and ">wss://abc.ngrok.app/ws</Stream>" in xml
#
#
# def test_plivo_answer_webhook(db):
#     import bot
#     from fastapi.testclient import TestClient
#
#     client = TestClient(bot.app)
#     response = client.post(
#         "/plivo/answer?customer_id=3",
#         content="CallUUID=abc-123&From=919000000000&To=919840012345",
#         headers={"content-type": "application/x-www-form-urlencoded"},
#     )
#     assert response.status_code == 200
#     assert "<Stream" in response.text and "/ws</Stream>" in response.text
#     assert db.get_customer_id_for_call("abc-123") == 3
#
#     # GET with CallUUID in the query string (what dialer.py asks Plivo to use).
#     response = client.get("/plivo/answer?customer_id=2&CallUUID=def-456")
#     assert response.status_code == 200
#     assert db.get_customer_id_for_call("def-456") == 2


def test_bot_module_imports(db, monkeypatch, tmp_path):
    """The bot should import, expose its tools, and build a per-customer prompt."""
    monkeypatch.setenv("DB_FILE", str(tmp_path / "bot.db"))
    import bot

    names = {t.name for t in bot.TOOLS.standard_tools}
    assert names == {
        "get_current_datetime",
        "search_service_bookings",
        "get_service_types",
        "create_service_appointment",
        "get_service_appointments",
        "update_service_appointment",
        "cancel_service_appointment",
        "log_call_outcome",
        "end_call",
    }
    # Every advertised tool has a handler on ServiceCall.
    call = bot.ServiceCall(db.get_customer(1))
    assert all(callable(getattr(call, name)) for name in names)

    prompt = bot.build_system_instruction(db.get_customer(1))
    assert "Karthik Raja" in prompt and "TN 09 AB 1234" in prompt
    assert "Yamaha" in prompt and "Nazareth" in prompt and "spoken tamil" in prompt.lower()
    assert "Thoothukudi" in prompt
    # Today's dates are in the prompt (a mid-call lookup made the agent repeat itself).
    assert f"({db.today().isoformat()})" in prompt and "TODAY" in prompt
    assert "Nazareth" in bot.GREETING_KICKOFF
    assert "Meera" not in prompt and "Meera" not in bot.GREETING_KICKOFF


def test_llm_uses_vertex_with_adc(db, monkeypatch):
    """The agent runs on Gemini Live via Vertex AI with ADC, never an AI Studio key."""
    import bot
    from google.oauth2.credentials import Credentials
    from pipecat.services.google.gemini_live.vertex.llm import GeminiLiveVertexLLMService

    seen = {}

    def fake_credentials(credentials, credentials_path):
        seen["args"] = (credentials, credentials_path)
        return Credentials(token="test-token")

    monkeypatch.setattr(
        GeminiLiveVertexLLMService, "_get_credentials", staticmethod(fake_credentials)
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "should-not-be-used")

    llm = bot._build_llm(db.get_customer(1))
    assert isinstance(llm, GeminiLiveVertexLLMService)
    # No key or key file passed: Pipecat falls back to Application Default Credentials.
    assert seen["args"] == (None, None)
    assert llm._project_id == bot.GOOGLE_CLOUD_PROJECT == "project-2c317546-b2c6-4a7a-8d4"
    assert llm._location == bot.GOOGLE_CLOUD_LOCATION == "us-central1"
    assert llm._settings.model == bot.GEMINI_MODEL == "gemini-3.8-live"
    # Tamil speech (not Pipecat's en-US default) and no tight reply cap.
    assert llm._settings.language == bot.GEMINI_LANGUAGE == "ta-IN"
    assert llm._settings.max_tokens >= 2048
    # Vertex rejects a custom thinking level for this project.
    assert not llm._settings.thinking


def test_resolve_customer(db):
    import bot
    from pipecat.runner.types import RunnerArguments

    # [Plivo disabled — pending client decision on telephony]
    # # Phone call: the Plivo call UUID was mapped to a customer by /plivo/answer.
    # db.register_phone_call("plivo-call-1", 3)
    # phone = RunnerArguments(call_data=CallData(call_id="plivo-call-1"))
    # assert bot.resolve_customer(phone)["id"] == 3

    # Browser test call: customer_id comes from requestData.
    browser = RunnerArguments(body={"customer_id": 2})
    assert bot.resolve_customer(browser)["id"] == 2

    # No customer given: the most overdue one (Anitha, due in 2 days).
    assert bot.resolve_customer(RunnerArguments())["name"] == "Anitha Devi"


async def test_voice_tools_crud_and_datetime(db):
    """Drive the agent's tool handlers the way Gemini calls them."""
    from types import SimpleNamespace

    import bot

    call = bot.ServiceCall(db.get_customer(1))
    results = []

    async def result_callback(message, **_):
        results.append(message)

    async def invoke(name, **arguments):
        await getattr(call, name)(
            SimpleNamespace(arguments=arguments, result_callback=result_callback)
        )
        return results[-1]

    now = await invoke("get_current_datetime")
    assert db.today().isoformat() in now and "IST" in now
    assert days_ahead(7) in now  # coming days listed for "next Saturday" etc.

    assert "created" in await invoke("create_service_appointment", date=days_ahead(3))
    assert days_ahead(3) in await invoke("get_service_appointments")
    assert "updated" in await invoke("update_service_appointment", new_date=days_ahead(5))
    assert days_ahead(5) in await invoke("get_service_appointments")
    assert "cancelled" in await invoke("cancel_service_appointment")
    assert "no upcoming" in (await invoke("get_service_appointments")).lower()

    # The same outcome logged twice in one call is stored once.
    await invoke("log_call_outcome", outcome="date_confirmed")
    await invoke("log_call_outcome", outcome="date_confirmed")
    assert len(db.get_call_logs()) == 1


async def test_voice_search_privacy(db):
    """The search tool shows the caller's bookings freely, but another
    customer's only with their exact registration or mobile number."""
    from types import SimpleNamespace

    import bot

    call = bot.ServiceCall(db.get_customer(2))  # Priya is on the call
    results = []

    async def result_callback(message, **_):
        results.append(message)

    async def search(**arguments):
        await call.search_service_bookings(
            SimpleNamespace(arguments=arguments, result_callback=result_callback)
        )
        return results[-1]

    # No details: her own history.
    own = await search()
    assert "Ray ZR 125" in own and "completed" in own and "this customer" in own

    # Her own bike by model or partial registration (one free-text query).
    assert "First free service" in await search(query="ray")
    assert "Found 1" in await search(query="5678")
    assert "completed" in await search(query="ray", state="completed")

    # Another customer by name only: hidden.
    hidden = await search(query="Suresh")
    assert "hidden" in hidden and "R15" not in hidden
    # ...but shown with the full registration number.
    shown = await search(query="TN22EF9012")
    assert "R15 V4" in shown and "customer Suresh Kumar" in shown
    # ...or the full mobile number, and the number itself is never echoed.
    by_mobile = await search(query="9840012345")
    assert "R15 V4" in by_mobile and "9840012345" not in by_mobile

    assert "No matching" in await search(query="Nobody")


def _api_client(monkeypatch, *, admin=True):
    """TestClient for api.py, signed in as a freshly created staff user."""
    from fastapi.testclient import TestClient

    import api
    import auth
    import database

    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    role = "admin" if admin else "staff"
    database.create_staff_user(role + "1", auth.hash_password("password123"), "Test " + role, role)
    client = TestClient(api.app)
    token = client.post("/api/auth/login", json={"username": role + "1", "password": "password123"}).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def test_api_requires_login_api_key_and_cors(db, monkeypatch):
    """Every API route needs a staff login (or X-API-Key for scripts); CORS
    allows only the CRM frontend's origin."""
    import importlib

    from fastapi.testclient import TestClient

    import api

    client = TestClient(api.app)
    assert client.get("/api/health").status_code == 200  # open
    assert client.get("/api/customers").status_code == 401

    monkeypatch.setenv("API_KEY", "s3cret")
    assert client.get("/api/customers", headers={"X-API-Key": "wrong"}).status_code == 401
    ok = client.get("/api/customers", headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200 and len(ok.json()) == 4

    monkeypatch.setenv("CORS_ORIGINS", "https://crm.arulmotors.example")
    client = TestClient(importlib.reload(api).app)
    preflight = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}
    allowed = client.options("/api/customers", headers={"Origin": "https://crm.arulmotors.example", **preflight})
    assert allowed.headers.get("access-control-allow-origin") == "https://crm.arulmotors.example"
    blocked = client.options("/api/customers", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in blocked.headers


def test_staff_login_tokens_and_roles(db, monkeypatch):
    import auth

    hashed = auth.hash_password("password123")
    assert auth.verify_password("password123", hashed) and not auth.verify_password("nope", hashed)
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    token = auth.create_token({"id": 1, "username": "a", "role": "admin"})
    assert auth.read_token(token)["sub"] == 1
    assert auth.read_token(token[:-2] + "xx") is None  # tampered
    assert auth.read_token(auth.create_token({"id": 1, "username": "a", "role": "admin"}, ttl_secs=-1)) is None

    admin = _api_client(monkeypatch)
    assert admin.post("/api/auth/login", json={"username": "admin1", "password": "wrong"}).status_code == 401
    assert admin.get("/api/auth/me").json()["role"] == "admin"
    # Admins manage staff; staff can't.
    created = admin.post("/api/staff", json={"username": "ravi", "password": "password123", "full_name": "Ravi"})
    assert created.status_code == 201
    assert admin.post("/api/staff", json={"username": "x", "password": "short"}).status_code == 400
    staff = _api_client(monkeypatch, admin=False)
    assert staff.get("/api/staff").status_code == 403
    assert staff.post("/api/auth/password", json={"current_password": "password123", "new_password": "newpassword9"}).status_code == 200
    assert staff.post("/api/auth/login", json={"username": "staff1", "password": "newpassword9"}).status_code == 200


def test_crm_customers_vehicles_bookings_and_stats(db, monkeypatch):
    client = _api_client(monkeypatch)

    # Customers: list (with bikes), search, create, update, detail, delete.
    customers = client.get("/api/customers").json()
    assert len(customers) == 4 and all(c["vehicles"] for c in customers)
    assert [c["name"] for c in client.get("/api/customers", params={"q": "fascino"}).json()] == ["Anitha Devi"]
    new = client.post("/api/customers", json={"name": "Murugan", "mobile_number": "98765 11111"})
    assert new.status_code == 201
    cid = new.json()["id"]
    assert client.post("/api/customers", json={"name": "Dup", "mobile_number": "9876511111"}).status_code == 400
    assert client.patch(f"/api/customers/{cid}", json={"name": "Murugan S"}).status_code == 200

    # Bikes: add to the new customer, edit, list.
    sale = {
        "name": "Murugan S", "mobile_number": "9876511111", "registration_number": "tn 69 zz 0001",
        "model": "Yamaha MT-15", "next_service_date": days_ahead(20), "next_service_type": "First free service",
    }
    assert client.post("/api/vehicles", json=sale).status_code == 201
    detail = client.get(f"/api/customers/{cid}").json()
    vid = detail["vehicles"][0]["id"]
    assert detail["vehicles"][0]["registration_number"] == "TN 69 ZZ 0001"
    assert client.patch(f"/api/vehicles/{vid}", json={"odometer_km": 750}).status_code == 200
    assert any(v["odometer_km"] == 750 for v in client.get("/api/vehicles").json())

    # Bookings: create, reschedule, cancel; complete another.
    booked = client.post("/api/appointments", json={"customer_id": cid, "appointment_date": days_ahead(3)})
    assert booked.status_code == 201
    aid = booked.json()["id"]
    assert client.patch(f"/api/appointments/{aid}", json={"appointment_date": days_ahead(4)}).status_code == 200
    assert client.get(f"/api/appointments/{aid}").json()["appointment_date"] == days_ahead(4)
    assert client.post(f"/api/appointments/{aid}/cancel").status_code == 200
    assert client.get(f"/api/appointments/{aid}").status_code == 404  # deleted, no trace
    assert client.post("/api/appointments", json={"customer_id": cid, "appointment_date": "2000-01-01"}).status_code == 400
    assert client.get("/api/appointments", params={"state": "cancelled"}).json() == []

    # Staff-logged call + dashboard stats.
    assert client.post("/api/calls/log", json={"customer_id": cid, "outcome": "date_confirmed"}).status_code == 201
    assert client.post("/api/calls/log", json={"customer_id": cid, "outcome": "bogus"}).status_code == 400
    stats = client.get("/api/stats").json()
    assert stats["totals"]["customers"] == 5 and stats["totals"]["vehicles"] == 5
    assert stats["totals"]["calls_this_week"] == 1 and stats["totals"]["confirmation_rate"] == 100
    assert len(stats["appointments_next_14_days"]) == 14 and len(stats["calls_last_14_days"]) == 14
    assert client.get(f"/api/customers/{cid}").json()["calls"][0]["outcome"] == "date_confirmed"

    # Deleting removes the customer and everything recorded for them.
    assert client.delete(f"/api/customers/{cid}").status_code == 200
    assert client.get(f"/api/customers/{cid}").status_code == 404
    assert len(client.get("/api/customers").json()) == 4


def test_voice_relay_checks_customer_and_forwards(db, monkeypatch):
    import api

    client = _api_client(monkeypatch, admin=False)
    sent = []

    async def fake_relay(method, payload):
        sent.append((method, payload))
        return {"sdp": "answer", "type": "answer", "pc_id": "pc1"}

    monkeypatch.setattr(api, "_relay_to_bot", fake_relay)
    offer = {"sdp": "offer", "type": "offer", "requestData": {"customer_id": 2}}
    assert client.post("/api/voice/offer", json=offer).json()["pc_id"] == "pc1"
    assert sent[0] == (
        "POST",
        {"sdp": "offer", "type": "offer", "request_data": {"customer_id": 2, "started_by": "Test staff"}},
    )
    assert client.post("/api/voice/offer", json={"sdp": "o", "type": "offer"}).status_code == 400
    assert client.post("/api/voice/offer", json={**offer, "requestData": {"customer_id": 999}}).status_code == 404
    assert client.patch("/api/voice/offer", json={"pc_id": "pc1", "candidates": []}).status_code == 200
    assert client.get("/api/voice/config").json()["ice_servers"][0]["urls"].startswith("stun:")
    # Not signed in: no calls.
    from fastapi.testclient import TestClient

    assert TestClient(api.app).post("/api/voice/offer", json=offer).status_code == 401


def test_sqlite_shared_by_two_processes(db):
    """Voice agent and API share the SQLite file: WAL mode + a lock timeout."""
    conn = db._connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()


def test_live_events_notifications_and_voice_calls(db):
    """Writes feed the CRM's live stream; only the agent's actions notify staff."""
    start = db.latest_event_id()
    ok, _, aid = db.create_appointment(1, days_ahead(3))  # by the agent (default)
    assert ok
    events = db.get_events_after(start)
    assert {"data.changed", "notification"} <= {e["type"] for e in events}
    changed = next(e for e in events if e["type"] == "data.changed")["data"]
    assert "appointments" in changed["entities"] and changed["customer_id"] == 1
    booked = next(e for e in events if e["type"] == "notification")["data"]
    assert booked["kind"] == "booking_created" and "Karthik Raja" in booked["title"]

    # Staff edits refresh the CRM but don't notify.
    mark = db.latest_event_id()
    db.update_appointment(1, aid, new_date=days_ahead(4), actor="staff:ravi")
    assert [e["type"] for e in db.get_events_after(mark)] == ["data.changed"]

    # Agent call outcomes notify (except date_confirmed, covered by the booking).
    mark = db.latest_event_id()
    db.log_call(2, "callback_requested", callback_date=days_ahead(2))
    db.log_call(2, "date_confirmed")
    titles = [e["data"]["title"] for e in db.get_events_after(mark) if e["type"] == "notification"]
    assert titles == ["Call back requested · Priya Lakshmi"]

    # Missed bookings are flagged once.
    conn = db._connect()
    conn.execute("UPDATE service_appointments SET appointment_date = ? WHERE id = ?", (days_ahead(-1), aid))
    conn.commit()
    conn.close()
    assert db.flag_missed_appointments() == 1
    assert db.flag_missed_appointments() == 0
    assert db.recent_notifications()[0]["data"]["kind"] == "booking_missed"

    # Voice calls in progress.
    call_id = db.start_voice_call(1, "Karthik Raja", "Ravi")
    assert [c["id"] for c in db.active_voice_calls()] == [call_id]
    db.end_voice_call(call_id, "date_confirmed")
    assert db.active_voice_calls() == []
    ended = [e for e in db.get_events_after(mark) if e["type"] == "call.ended"][-1]["data"]
    assert ended["outcome"] == "date_confirmed" and ended["started_by"] == "Ravi"


def test_live_websocket_requires_login_and_streams_events(db, monkeypatch):
    import pytest as _pytest
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    import api
    import auth
    import database

    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setattr(api, "EVENT_POLL_SECS", 0.05)
    database.create_staff_user("ravi", auth.hash_password("password123"), "Ravi", "staff")
    user = database.get_staff_user(username="ravi")
    token = auth.create_token(user)

    with TestClient(api.app) as client:  # runs the live event loop
        with _pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/ws?token=bad") as ws:
                ws.receive_text()
        with client.websocket_connect(f"/api/ws?token={token}") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["data"]["user"]["username"] == "ravi"
            assert "active_calls" in hello["data"] and "notifications" in hello["data"]
            ws.send_text("ping")
            assert ws.receive_json()["type"] == "pong"
            database.start_voice_call(3, "Suresh Kumar", "Ravi")
            message = ws.receive_json()
            while message["type"] != "call.started":
                message = ws.receive_json()
            assert message["data"]["customer_name"] == "Suresh Kumar"
        assert client.get(
            "/api/voice/calls/active", headers={"Authorization": f"Bearer {token}"}
        ).json()[0]["customer_name"] == "Suresh Kumar"
        assert client.get("/api/notifications", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_service_types_crud_rename_and_delete_rules(db, monkeypatch):
    names = [t["name"] for t in db.list_service_types()]
    assert names[:4] == ["First free service", "Second free service", "Third free service", "Paid service"]
    assert "General check-up" in names
    first = db.list_service_types()[0]
    assert first["is_free"] is True and first["interval_days"] == 30 and first["bikes_due"] == 1  # Karthik's FZ-S

    client = _api_client(monkeypatch)
    created = client.post("/api/service-types", json={"name": "Engine oil change", "interval_days": 90, "price": 650})
    assert created.status_code == 201
    tid = created.json()["id"]
    assert client.post("/api/service-types", json={"name": "engine OIL change"}).status_code == 400  # duplicate
    assert client.post("/api/service-types", json={"name": "Bad", "interval_days": 0}).status_code == 400

    # Reorder, deactivate, clear the price.
    before = [t["name"] for t in client.get("/api/service-types").json()]
    above = before[before.index("Engine oil change") - 1]
    assert client.post(f"/api/service-types/{tid}/move", params={"direction": "up"}).status_code == 200
    order = [t["name"] for t in client.get("/api/service-types").json()]
    assert order.index("Engine oil change") == order.index(above) - 1
    assert client.patch(f"/api/service-types/{tid}", json={"active": False, "price": None}).status_code == 200
    assert "Engine oil change" not in [t["name"] for t in client.get("/api/service-types", params={"active_only": True}).json()]

    # Renaming a used type renames it on bikes and bookings too.
    paid = next(t for t in db.list_service_types() if t["name"] == "Paid service")
    assert client.patch(f"/api/service-types/{paid['id']}", json={"name": "Periodic paid service"}).status_code == 200
    assert db.get_customer(4)["vehicles"][0]["next_service_type"] == "Periodic paid service"

    # Used types can't be deleted; unused ones can.
    assert client.delete(f"/api/service-types/{paid['id']}").status_code == 400
    assert client.delete(f"/api/service-types/{tid}").status_code == 200


def test_api_caching_etag_gzip_throttle_and_limits(db, monkeypatch):
    import api

    client = _api_client(monkeypatch)

    # Conditional GET: same data version -> 304, after a write -> fresh 200.
    first = client.get("/api/customers")
    etag = first.headers["etag"]
    assert first.status_code == 200 and first.headers["cache-control"] == "private, no-cache"
    assert client.get("/api/customers", headers={"If-None-Match": etag}).status_code == 304
    client.post("/api/customers", json={"name": "Vel Murugan", "mobile_number": "9000000001"})
    fresh = client.get("/api/customers", headers={"If-None-Match": etag})
    assert fresh.status_code == 200 and len(fresh.json()) == 5  # versioned cache refreshed too
    # No 304 without valid credentials (the route still answers 401).
    from fastapi.testclient import TestClient
    assert TestClient(api.app).get("/api/customers", headers={"If-None-Match": fresh.headers["etag"]}).status_code == 401

    # Large JSON is gzipped.
    assert client.get("/api/stats", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") == "gzip"

    # Call log is capped (newest first) with a separate total.
    for _ in range(3):
        client.post("/api/calls/log", json={"customer_id": 1, "outcome": "no_answer"})
    assert len(client.get("/api/calls", params={"limit": 2}).json()) == 2
    assert client.get("/api/calls/count").json()["total"] >= 3

    # Too many failed sign-ins from one client -> 429, even with the right password next.
    monkeypatch.setattr(api.LoginThrottle, "MAX_FAILURES", 3)
    api.login_throttle._failures.clear()
    anon = TestClient(api.app)
    for _ in range(3):
        assert anon.post("/api/auth/login", json={"username": "admin1", "password": "wrong"}).status_code == 401
    assert anon.post("/api/auth/login", json={"username": "admin1", "password": "password123"}).status_code == 429
    api.login_throttle._failures.clear()


def test_database_indexes_exist(db):
    conn = db._connect()
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
    conn.close()
    assert {name for name, _, _ in db.INDEXES} <= names


def test_prompt_service_info_guardrails_and_visit_date(db):
    """The prompt names the due service with its CRM details (never a price)
    and carries the strict-scope rules; a confirmed booking's date reaches the
    call log and the CRM's end-of-call event."""
    import asyncio
    from types import SimpleNamespace

    import bot

    paid = next(t for t in db.list_service_types() if t["name"] == "Paid service")
    db.update_service_type(paid["id"], description="Oil change, chain lube, brake check", price=1499)
    prompt = bot.build_system_instruction(db.get_customer(4))  # Anitha: Paid service due
    assert "Paid service: paid; Oil change, chain lube, brake check; next one due 120 days" in prompt
    assert "1499" not in prompt and "1,499" not in prompt  # prices are never given to the agent
    assert "STRICT SCOPE" in prompt and "Never reveal" in prompt and "end_call" in prompt
    assert "-" not in prompt.split("CUSTOMER:")[1].split("SERVICE INFO")[0].split("due ")[1][:10]  # spoken dates

    call = bot.ServiceCall(db.get_customer(1))
    results = []

    async def invoke(name, **arguments):
        await getattr(call, name)(
            SimpleNamespace(arguments=arguments, result_callback=lambda m, **_: _store(m))
        )
        return results[-1]

    async def _store(message):
        results.append(message)

    async def run():
        types = await invoke("get_service_types")
        assert "First free service: free" in types and "₹" not in types and "1499" not in types
        booked = await invoke("create_service_appointment", date=days_ahead(3))
        assert "say this date" in booked
        assert call.booked_date == days_ahead(3) and call.booked_service == "First free service"
        logged = await invoke("log_call_outcome", outcome="date_confirmed")
        assert "goodbye" in logged and "hangs up automatically" in logged
        assert call.ending  # CallGuard hangs up after the goodbye

    asyncio.run(run())
    note = db.get_call_logs(1)[0]["notes"]
    assert note.startswith("Coming on ") and "First free service" in note

    call_id = db.start_voice_call(1, "Karthik Raja", "Ravi")
    mark = db.latest_event_id()
    db.end_voice_call(call_id, "date_confirmed", service_date=days_ahead(3), service_type="First free service")
    ended = next(e for e in db.get_events_after(mark) if e["type"] == "call.ended")["data"]
    assert ended["service_date"] == days_ahead(3) and ended["service_type"] == "First free service"


async def test_call_guard_hangs_up(db, monkeypatch):
    """CallGuard ends calls in code: after the goodbye, after a quiet wrap-up,
    on silence and at the time limit."""
    import asyncio

    import bot
    from pipecat.frames.frames import BotStoppedSpeakingFrame

    def make(**state):
        call = bot.ServiceCall(db.get_customer(1))
        for k, v in state.items():
            setattr(call, k, v)
        guard = bot.CallGuard(call)
        hung = []

        async def fake_hang_up(reason):
            guard._ended = True
            guard.reason = reason
            hung.append(reason)

        monkeypatch.setattr(guard, "_hang_up", fake_hang_up)
        return guard, hung

    monkeypatch.setattr(bot, "WRAP_UP_IDLE_SECS", 0.5)
    guard, hung = make(outcome_logged=True)
    guard._last_activity -= 1
    await asyncio.wait_for(guard._watchdog(), 5)
    assert hung == ["wrap-up finished"]

    monkeypatch.setattr(bot, "SILENCE_HANGUP_SECS", 0.5)
    guard, hung = make()
    guard._last_activity -= 1
    await asyncio.wait_for(guard._watchdog(), 5)
    assert hung == ["silence"]

    monkeypatch.setattr(bot, "MAX_CALL_SECS", 0.5)
    guard, hung = make()
    guard._started -= 1
    await asyncio.wait_for(guard._watchdog(), 5)
    assert hung == ["time limit"]

    # end_call while the goodbye is playing: waits until it has really finished
    # (a short pause between phrases doesn't count).
    monkeypatch.setattr(bot, "GOODBYE_SETTLE_SECS", 0.3)
    guard, hung = make(ending=True)
    guard._bot_speaking = True
    monkeypatch.setattr(guard, "push_frame", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(bot.FrameProcessor, "process_frame", lambda self, frame, direction: asyncio.sleep(0))
    monkeypatch.setattr(guard, "create_task", lambda coro, *a, **k: asyncio.ensure_future(coro))
    guard._watch = object()  # don't start the watchdog from process_frame
    from pipecat.frames.frames import BotStartedSpeakingFrame
    await guard.process_frame(BotStoppedSpeakingFrame(), bot.FrameDirection.DOWNSTREAM)
    await guard.process_frame(BotStartedSpeakingFrame(), bot.FrameDirection.DOWNSTREAM)  # still talking
    await asyncio.sleep(0.5)
    assert hung == []
    await guard.process_frame(BotStoppedSpeakingFrame(), bot.FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.5)
    assert hung == ["goodbye finished"]


def test_alternate_mobile_store_swap_search_and_migrate(db, monkeypatch, tmp_path):
    client = _api_client(monkeypatch)
    karthik = db.get_customer_by_mobile("9876543210")

    # Set, validate, search by it, swap it in as the called number, remove it.
    url = f"/api/customers/{karthik['id']}"
    assert client.patch(url, json={"alternate_mobile": "98400 77777"}).status_code == 200
    assert db.get_customer(karthik["id"])["alternate_mobile"] == "9840077777"
    assert client.patch(url, json={"alternate_mobile": "9876543210"}).status_code == 400  # same as main
    assert client.patch(url, json={"alternate_mobile": "123"}).status_code == 400
    assert client.patch(url, json={"mobile_number": "9840077777"}).status_code == 400  # swap instead
    assert [c["name"] for c in client.get("/api/customers", params={"q": "77777"}).json()] == ["Karthik Raja"]
    swap = {"mobile_number": "9840077777", "alternate_mobile": "9876543210"}
    assert client.patch(url, json=swap).status_code == 200
    swapped = db.get_customer(karthik["id"])
    assert (swapped["mobile_number"], swapped["alternate_mobile"]) == ("9840077777", "9876543210")
    assert client.patch(url, json={"alternate_mobile": ""}).status_code == 200
    assert db.get_customer(karthik["id"])["alternate_mobile"] is None
    created = client.post("/api/customers", json={"name": "Two Phones", "mobile_number": "9000000001",
                                                  "alternate_mobile": "9000000002"})
    assert created.status_code == 201
    assert db.get_customer(created.json()["id"])["alternate_mobile"] == "9000000002"

    # Bookings search (and so the agent's lookup) matches the alternate too.
    db.update_customer(karthik["id"], alternate_mobile="9840055555")
    assert db.create_appointment(karthik["id"], days_ahead(2))[0]
    found = db.search_appointments(mobile_number="9840055555")
    assert found and {r["customer_id"] for r in found} == {karthik["id"]}

    # A database from before the column existed gets it on start.
    import sqlite3

    if db.using_mysql():
        return
    old = tmp_path / "old-customers.db"
    conn = sqlite3.connect(old)
    conn.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
                 "mobile_number TEXT NOT NULL UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO customers (name, mobile_number) VALUES ('Old', '9111111111')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "DB_FILE", str(old))
    db.init_db()
    assert db.get_customer_by_mobile("9111111111")["alternate_mobile"] is None


def test_admin_can_switch_the_voice_agent_off(db, monkeypatch):
    import auth

    client = _api_client(monkeypatch)  # admin
    assert client.get("/api/settings").json() == {"voice_agent_enabled": True}

    # Staff can read the setting but not change it.
    db.create_staff_user("desk", auth.hash_password("password123"), "Desk", "staff")
    from fastapi.testclient import TestClient

    import api

    staff = TestClient(api.app)
    token = staff.post("/api/auth/login", json={"username": "desk", "password": "password123"}).json()["token"]
    staff.headers["Authorization"] = f"Bearer {token}"
    assert staff.get("/api/settings").json()["voice_agent_enabled"] is True
    assert staff.patch("/api/settings", json={"voice_agent_enabled": False}).status_code == 403

    # Admin turns it off: no calls start, status says so, everyone is told.
    before = db.latest_event_id()
    assert client.patch("/api/settings", json={"voice_agent_enabled": False}).json() == {"voice_agent_enabled": False}
    assert db.voice_agent_enabled() is False
    offer = {"sdp": "x", "type": "offer", "requestData": {"customer_id": 1}}
    refused = client.post("/api/voice/offer", json=offer)
    assert refused.status_code == 403 and "turned off" in refused.json()["detail"]
    assert client.get("/api/voice/status").json() == {"online": False, "enabled": False}
    types = [e["type"] for e in db.get_events_after(before)]
    assert "data.changed" in types and "notification" in types

    # Unchanged value again (MySQL reports 0 rows) and back on.
    assert client.patch("/api/settings", json={"voice_agent_enabled": False}).status_code == 200
    assert client.patch("/api/settings", json={"voice_agent_enabled": True}).json() == {"voice_agent_enabled": True}
    assert db.voice_agent_enabled() is True


def test_admin_excel_export_and_database_backup(db, monkeypatch, tmp_path):
    import io
    import sqlite3

    from openpyxl import load_workbook

    client = _api_client(monkeypatch)  # admin
    db.update_customer(db.get_customer_by_mobile("9876543210")["id"], alternate_mobile="9840077777")
    db.log_call(db.get_customer_by_mobile("9876543210")["id"], "callback_requested", callback_date=days_ahead(3))

    # Excel: four sheets, a row per bike, mobiles as text, real dates.
    r = client.get("/api/admin/export.xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["Customers & Bikes", "Service Records", "Bookings", "Calls", "Service Types"]
    bikes = list(wb["Customers & Bikes"].iter_rows(values_only=True))
    assert bikes[0][:3] == ("Customer", "Mobile", "Alternate mobile") and len(bikes) == 5
    karthik = next(row for row in bikes if row[0] == "Karthik Raja")
    assert karthik[1] == "9876543210" and karthik[2] == "9840077777"
    assert karthik[5] == "Arul Motors"  # bought from
    assert hasattr(karthik[9], "year")  # next service due is a date cell
    assert karthik[13] == "Call back"
    assert len(list(wb["Calls"].iter_rows())) == 2
    assert len(list(wb["Service Types"].iter_rows())) == 1 + len(db.list_service_types())

    # Database copy: a valid SQLite file with the same data.
    r = client.get("/api/admin/backup.db")
    if db.using_mysql():
        assert r.status_code == 400
    else:
        assert r.status_code == 200 and r.content[:16] == b"SQLite format 3\x00"
        copy = tmp_path / "copy.db"
        copy.write_bytes(r.content)
        conn = sqlite3.connect(copy)
        assert conn.execute("SELECT count(*) FROM customers").fetchone()[0] == 4
        conn.close()

    # Staff can't download either.
    staff = _api_client(monkeypatch, admin=False)
    assert staff.get("/api/admin/export.xlsx").status_code == 403
    assert staff.get("/api/admin/backup.db").status_code == 403


def test_cors_exposes_download_file_names(db, monkeypatch):
    client = _api_client(monkeypatch)
    r = client.get("/api/admin/export.xlsx", headers={"Origin": "http://localhost:3000"})
    assert "content-disposition" in r.headers.get("access-control-expose-headers", "").lower()


def test_interruptions_and_no_repeats(db):
    """The customer can interrupt (Gemini's default barge-in, no override), but a
    short "ம்ம்" isn't long enough to cut the agent off; and the agent has today's
    dates up front instead of looking them up mid-reply."""
    from google.genai.types import StartSensitivity

    import bot

    llm = bot._build_llm(db.get_customer(1))
    assert type(llm) is bot.GeminiLiveVertexLLMService
    vad = llm._settings.vad
    assert vad.prefix_padding_ms == 500 and vad.start_sensitivity == StartSensitivity.START_SENSITIVITY_LOW

    prompt = bot.build_system_instruction(db.get_customer(1))
    assert "polite" in prompt and "COMPLETE sentences" in prompt
    assert "Interrupted: stop" in prompt and "don't" in prompt and "start over" in prompt
    assert "Dates come from TODAY" in prompt and "get_current_datetime once" not in prompt
    tomorrow = (db.today() + timedelta(days=1)).isoformat()
    assert f"({tomorrow})" in prompt
    assert "அச்சச்சோ" not in prompt and "Super" not in prompt


def test_cancel_removes_the_call_that_booked_it(db):
    """After cancelling, the customer mustn't still show 'Date confirmed' from
    the agent's call that made the booking; other calls stay."""
    ok, _, aid = db.create_appointment(1, days_ahead(2))
    assert ok
    db.log_call(1, "no_answer")  # an unrelated earlier call
    db.log_call(1, "date_confirmed", notes="Coming soon", appointment_id=aid)
    assert db.event_ref_exists(f"booking:{aid}")  # the "Service booked" notification
    assert [c["outcome"] for c in db.get_call_logs(1)] == ["date_confirmed", "no_answer"]

    assert db.cancel_appointment(1, aid, actor="staff:admin")[0]
    assert [c["outcome"] for c in db.get_call_logs(1)] == ["no_answer"]
    assert not db.event_ref_exists(f"booking:{aid}")
    assert next(c for c in db.list_customers() if c["id"] == 1)["last_call"]["outcome"] == "no_answer"


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def test_close_service_with_job_card_moves_bike_on_and_keeps_history(db, monkeypatch):
    """Book -> close with the job card (fields + scanned PDF) -> bike moves to
    its next service; the record and PDF show in the bike's history. Uses the
    fields of Arul Motors' Yamaha job card."""
    import json

    client = _api_client(monkeypatch, admin=False)  # any staff can close services
    karthik = db.get_customer_by_mobile("9876543210")
    bike = karthik["vehicles"][0]
    aid = client.post("/api/appointments", json={"customer_id": karthik["id"], "appointment_date": days_ahead(1)}).json()["id"]
    today = db.today().isoformat()
    parts = [
        {"part_no": "90793-AD411", "name": "Engine oil", "qty": 1, "amount": 737},
        {"part_no": "388-E3440-01", "name": "Oil cleaner", "qty": 1, "amount": 265},
        {"part_no": "90793AD8", "name": "Chain clean", "qty": 1, "amount": 194},
        {"name": "Brake shoe", "qty": 2, "amount": 300, "warranty": True},  # not charged
    ]
    form = {
        "service_date": today, "received_date": today, "service_type": "First free service",
        "job_type": "Free service", "job_card_no": "6862", "odometer_km": "3158",
        "frame_number": "ME1RG3719S0016932", "engine_number": "G3E8E0123456",
        "customer_requests": "Oil change, general checkup, water wash, brake and air check",
        "jobs_done": json.dumps(["Engine oil change", "Water wash", "General check-up", "Chain lube"]),
        "parts": json.dumps(parts),
        "labour": json.dumps([{"description": "General service labour", "amount": 0}]),
        "delivery_checks": json.dumps(["Customer demanded jobs done", "Final inspection done"]),
        "feedback": json.dumps({"rating": "Very good", "delivery_on_time": True, "washing_quality": True}),
        "technician": "Bay 2 / Ravi", "mechanic": "Kumar", "supervisor": "Selvam",
        "remarks": "Rear tyre wear noted", "next_service_date": days_ahead(90), "next_service_type": "Second free service",
    }
    pdf = {"report": ("Job card 6862.pdf", PDF, "application/pdf")}
    url = f"/api/appointments/{aid}/close"

    # Validation: PDF required and must be a PDF; dates; job type; table rows.
    assert client.post(url, data=form).status_code == 422  # no file at all
    assert client.post(url, data=form, files={"report": ("x.pdf", b"not a pdf", "application/pdf")}).status_code == 400
    big = b"%PDF" + b"0" * db.MAX_PDF_BYTES
    assert client.post(url, data=form, files={"report": ("big.pdf", big, "application/pdf")}).status_code == 400
    assert client.post(url, data={**form, "service_date": days_ahead(2)}, files=pdf).status_code == 400
    assert client.post(url, data={**form, "received_date": days_ahead(1)}, files=pdf).status_code == 400
    assert client.post(url, data={**form, "next_service_date": today}, files=pdf).status_code == 400
    assert client.post(url, data={**form, "job_type": "Car wash"}, files=pdf).status_code == 400
    assert client.post(url, data={**form, "parts": json.dumps([{"name": "", "qty": 1}])}, files=pdf).status_code == 400
    assert client.post(url, data={**form, "parts": "not json"}, files=pdf).status_code == 400
    assert client.post(url, data={**form, "feedback": json.dumps({"rating": "Wow"})}, files=pdf).status_code == 400
    nothing = {**form, "jobs_done": "[]", "parts": "[]", "labour": "[]", "work_done": " "}
    assert client.post(url, data=nothing, files=pdf).status_code == 400
    assert client.post("/api/appointments/AM9999/close", data=form, files=pdf).status_code == 404
    assert db.get_appointment(aid)["status"] == "CONFIRMED"  # nothing half-saved

    r = client.post(url, data=form, files=pdf)
    assert r.status_code == 201, r.text
    record_id = r.json()["id"]

    # Booking closed, bike moved on (and got its frame/engine numbers).
    assert db.get_appointment(aid)["status"] == "COMPLETED"
    v = next(x for x in db.get_customer(karthik["id"])["vehicles"] if x["id"] == bike["id"])
    assert (v["last_service_date"], v["odometer_km"]) == (today, 3158)
    assert (v["next_service_date"], v["next_service_type"]) == (days_ahead(90), "Second free service")
    assert (v["frame_number"], v["engine_number"]) == ("ME1RG3719S0016932", "G3E8E0123456")
    assert client.post(url, data=form, files=pdf).status_code == 404  # can't close twice

    # History: the job card's fields, totals computed by the server.
    history = client.get(f"/api/vehicles/{bike['id']}/service-history").json()
    rec = history[0]
    assert rec["kind"] == "record" and rec["job_card_no"] == "6862" and rec["job_type"] == "Free service"
    assert rec["parts_total"] == 1196 and rec["labour_total"] == 0 and rec["amount"] == 1196  # warranty part free
    assert rec["jobs_done"] == ["Engine oil change", "Water wash", "General check-up", "Chain lube"]
    assert rec["parts"][3]["warranty"] is True and rec["parts"][0]["part_no"] == "90793-AD411"
    assert rec["feedback"] == {"rating": "Very good", "delivery_on_time": True, "washing_quality": True}
    assert rec["delivery_checks"] == ["Customer demanded jobs done", "Final inspection done"]
    assert (rec["technician"], rec["mechanic"], rec["supervisor"]) == ("Bay 2 / Ravi", "Kumar", "Selvam")
    assert rec["document_name"] == "Job card 6862.pdf" and rec["document_size"] == len(PDF)
    assert rec["created_by"] == "Test staff"
    report = client.get(f"/api/service-records/{record_id}/report")
    assert report.status_code == 200 and report.content == PDF
    assert report.headers["content-type"] == "application/pdf" and "inline" in report.headers["content-disposition"]
    assert client.get("/api/service-records/9999/report").status_code == 404
    assert "Engine oil (90793-AD411) ×1 ₹737" in db.parts_summary(rec["parts"])

    # Older services without a record (e.g. imported) are listed too.
    priya = db.get_customer_by_mobile("9876501234")
    older = client.get(f"/api/vehicles/{priya['vehicles'][0]['id']}/service-history").json()
    assert older and older[0]["kind"] == "booking"

    # The Excel export carries the job card fields.
    from openpyxl import load_workbook
    import io

    admin = _api_client(monkeypatch)
    sheet = load_workbook(io.BytesIO(admin.get("/api/admin/export.xlsx").content))["Service Records"]
    row = dict(zip([c.value for c in sheet[1]], [c.value for c in sheet[2]]))
    assert row["Job card"] == "6862" and row["Total ₹"] == 1196 and row["Feedback"] == "Very good"

    # Frame/engine numbers can also be edited on the bike.
    assert admin.patch(f"/api/vehicles/{bike['id']}", json={"frame_number": "ME1NEWFRAME0001"}).status_code == 200
    assert db.get_customer(karthik["id"])["vehicles"][0]["frame_number"] == "ME1NEWFRAME0001"

    # Deleting the bike removes its records and PDFs.
    assert db.delete_vehicle(bike["id"])[0]
    assert db.get_service_document(record_id) is None and db.list_service_records() == []


def test_job_card_extract_fills_the_form_without_saving(db, monkeypatch):
    """Upload a job card -> Gemini's JSON comes back as form fields; bad dates
    are dropped; failures are a clear 502; nothing is written."""
    import json
    from types import SimpleNamespace

    import job_card_extract

    reply = {
        "job_card_no": "6862", "job_type": "Free service", "odometer_km": 3158,
        "received_date": "2026-10-23", "delivered_date": "2026-13-45",  # unreadable -> dropped
        "frame_number": "ME1RG3719S0016932", "customer_requests": "Oil change, water wash",
        "jobs_done": ["Engine oil change", "Water wash"],
        "parts": [{"part_no": "90793-AD411", "name": "Engine oil", "qty": 1, "amount": 737, "warranty": False}],
        "labour": [], "delivery_checks": [], "feedback": {"rating": "Good"}, "total_amount": 1196,
    }
    calls = []

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text=json.dumps(reply), usage_metadata=None)

    monkeypatch.setattr(job_card_extract, "_get_client", lambda: SimpleNamespace(models=FakeModels()))
    client = _api_client(monkeypatch, admin=False)
    r = client.post("/api/job-card/extract", files={"report": ("card.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["job_card_no"] == "6862" and card["odometer_km"] == 3158 and card["job_type"] == "Free service"
    assert card["received_date"] == "2026-10-23" and card["delivered_date"] is None
    assert card["parts"][0]["part_no"] == "90793-AD411" and card["jobs_done"] == ["Engine oil change", "Water wash"]
    assert card["feedback"]["rating"] == "Good" and card["total_amount"] == 1196
    assert calls[0]["model"] == job_card_extract.EXTRACT_MODEL
    assert db.list_service_records() == []  # nothing saved

    assert client.post("/api/job-card/extract", files={"report": ("x.pdf", b"nope", "application/pdf")}).status_code == 400

    class Broken:
        def generate_content(self, **kwargs):
            raise TimeoutError("vertex down")

    monkeypatch.setattr(job_card_extract, "_get_client", lambda: SimpleNamespace(models=Broken()))
    r = client.post("/api/job-card/extract", files={"report": ("card.pdf", PDF, "application/pdf")})
    assert r.status_code == 502 and "by hand" in r.json()["detail"]


def test_service_type_overdue_days_decide_when_a_bike_is_overdue(db, monkeypatch):
    """Each service type has its own overdue days: a bike due 10 days ago is
    still 'due' for a 30-day type, but overdue for a 5-day (or 0-day) type."""
    client = _api_client(monkeypatch)
    r = client.post("/api/service-types", json={"name": "Grace 30", "interval_days": 60, "overdue_days": 30})
    assert r.status_code == 201
    grace30 = r.json()["id"]
    assert client.post("/api/service-types", json={"name": "Grace 5", "interval_days": 60, "overdue_days": 5}).status_code == 201
    assert client.post("/api/service-types", json={"name": "Bad", "overdue_days": -1}).status_code == 400
    assert next(t for t in client.get("/api/service-types").json() if t["id"] == grace30)["overdue_days"] == 30

    karthik, priya = db.get_customer(1)["vehicles"][0], db.get_customer(2)["vehicles"][0]
    db.update_vehicle(karthik["id"], next_service_date=days_ahead(-10), next_service_type="Grace 30")
    db.update_vehicle(priya["id"], next_service_date=days_ahead(-10), next_service_type="Grace 5")
    due = {d["vehicle_id"]: d for d in client.get("/api/due-services", params={"days": 7}).json()}
    assert due[karthik["id"]]["is_overdue"] is False and due[karthik["id"]]["overdue_from"] == days_ahead(20)
    assert due[priya["id"]]["is_overdue"] is True and due[priya["id"]]["overdue_from"] == days_ahead(-5)
    overdue_before = client.get("/api/stats").json()["totals"]["overdue"]

    # Changing the type's overdue days changes the answer; empty means 0.
    assert client.patch(f"/api/service-types/{grace30}", json={"overdue_days": None}).status_code == 200
    due = {d["vehicle_id"]: d for d in client.get("/api/due-services", params={"days": 7}).json()}
    assert due[karthik["id"]]["is_overdue"] is True and due[karthik["id"]]["overdue_days"] == 0
    assert client.get("/api/stats").json()["totals"]["overdue"] == overdue_before + 1


def test_dates_shown_to_people_are_dd_mm_yyyy(db):
    """Stored dates stay YYYY-MM-DD (they sort and compare), but every date
    people read is DD-MM-YYYY, and DD-MM-YYYY is accepted back."""
    import bot

    assert db._human_day("2026-10-23") == "Fri, 23-10-2026"
    assert db._spoken("2026-10-21") == "21-10-2026 (Wednesday)"
    assert db.normalize_date("26-10-2026") == "2026-10-26"
    assert bot._say_date("2026-10-23") == "Fri 23 Oct"  # spoken by the agent, not shown
    ok, message, aid = db.create_appointment(1, days_ahead(2))
    assert ok and db.get_appointment(aid)["appointment_date"] == days_ahead(2)  # stored ISO
    shown = date.fromisoformat(days_ahead(2)).strftime("%d-%m-%Y")
    assert shown in message


def test_out_of_shift_hours_only_admins(db, monkeypatch):
    """07:00-21:00: everyone. Outside: staff are refused (requests, sign-in,
    live feed); admins and the API key still work. /api/health says which."""
    from datetime import datetime

    from fastapi.testclient import TestClient

    import api
    import auth

    tz = db.SHOWROOM_TZ
    # Sign in before shift hours are switched on (so this passes at any time of day).
    admin = _api_client(monkeypatch)
    staff = _api_client(monkeypatch, admin=False)
    monkeypatch.setattr(db, "SHIFT_START", "07:00")
    monkeypatch.setattr(db, "SHIFT_END", "21:00")
    assert db.shift_info(datetime(2026, 10, 25, 6, 59, tzinfo=tz))["open"] is False
    assert db.shift_info(datetime(2026, 10, 25, 7, 0, tzinfo=tz)) == {
        "open": True, "start": "07:00", "end": "21:00", "changes_at": "2026-10-25T21:00+05:30"}
    assert db.shift_info(datetime(2026, 10, 25, 21, 0, tzinfo=tz))["changes_at"] == "2026-10-26T07:00+05:30"

    monkeypatch.setattr(db, "shift_info", lambda at=None: {"open": False, "start": "07:00", "end": "21:00", "changes_at": "x"})
    r = staff.get("/api/customers")
    assert r.status_code == 403 and r.json()["detail"].startswith("Out of shift hours")
    login = TestClient(api.app).post("/api/auth/login", json={"username": "staff1", "password": "password123"})
    assert login.status_code == 403
    wrong = TestClient(api.app).post("/api/auth/login", json={"username": "staff1", "password": "nope-wrong"})
    assert wrong.status_code == 401  # wrong password still just "wrong password"
    assert admin.get("/api/customers").status_code == 200
    assert TestClient(api.app).post("/api/auth/login", json={"username": "admin1", "password": "password123"}).status_code == 200
    assert TestClient(api.app).get("/api/health").json()["shift"]["open"] is False

    import pytest
    from starlette.websockets import WebSocketDisconnect

    token = auth.create_token(db.get_staff_user(username="staff1"))
    with pytest.raises(WebSocketDisconnect) as closed:
        with TestClient(api.app).websocket_connect(f"/api/ws?token={token}") as ws:
            ws.receive_text()
    assert closed.value.code == 4403


def test_admin_deletes_calls_one_or_all(db, monkeypatch):
    """Call log: admins delete one entry, all of one outcome, or everything;
    staff can't; the customer's last call follows."""
    admin = _api_client(monkeypatch)
    staff = _api_client(monkeypatch, admin=False)
    for outcome in ("no_answer", "no_answer", "callback_requested", "date_confirmed"):
        db.log_call(1, outcome)
    calls = admin.get("/api/calls").json()
    assert len(calls) == 4

    assert staff.delete(f"/api/calls/{calls[0]['id']}").status_code == 403
    assert staff.delete("/api/calls").status_code == 403
    assert admin.delete(f"/api/calls/{calls[0]['id']}").status_code == 200  # newest: date_confirmed
    assert admin.delete(f"/api/calls/{calls[0]['id']}").status_code == 404
    assert next(c for c in db.list_customers() if c["id"] == 1)["last_call"]["outcome"] == "callback_requested"

    assert admin.delete("/api/calls", params={"outcome": "bogus"}).status_code == 400
    r = admin.delete("/api/calls", params={"outcome": "no_answer"})
    assert r.json()["deleted"] == 2 and [c["outcome"] for c in admin.get("/api/calls").json()] == ["callback_requested"]
    assert admin.delete("/api/calls").json()["deleted"] == 1
    assert admin.get("/api/calls/count").json()["total"] == 0


def test_edit_a_closed_service(db, monkeypatch):
    """Edit a service record: fields change, totals are recomputed, a new PDF
    replaces the job card, and the bike follows only its latest record."""
    import json

    client = _api_client(monkeypatch, admin=False)
    bike = db.get_customer(1)["vehicles"][0]

    def close(day_offset: int, km: str) -> int:
        aid = client.post("/api/appointments", json={"customer_id": 1, "appointment_date": days_ahead(1)}).json()["id"]
        form = {"service_date": days_ahead(day_offset), "odometer_km": km, "jobs_done": json.dumps(["Water wash"]),
                "next_service_date": days_ahead(90), "next_service_type": "Second free service"}
        r = client.post(f"/api/appointments/{aid}/close", data=form, files={"report": ("a.pdf", PDF, "application/pdf")})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    older = close(-10, "1000")
    latest = close(0, "2000")

    edit = {
        "service_date": days_ahead(0), "odometer_km": "2100", "job_card_no": "7001",
        "parts": json.dumps([{"name": "Engine oil", "qty": 2, "amount": 400}]),
        "next_service_date": days_ahead(100), "next_service_type": "Third free service",
    }
    new_pdf = b"%PDF-1.4\n% replaced\n%%EOF\n"
    r = client.patch(f"/api/service-records/{latest}", data=edit, files={"report": ("new.pdf", new_pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    rec = client.get(f"/api/vehicles/{bike['id']}/service-history").json()[0]
    assert (rec["id"], rec["job_card_no"], rec["odometer_km"], rec["amount"]) == (latest, "7001", 2100, 800)
    assert rec["updated_by"] == "Test staff" and rec["document_name"] == "new.pdf"
    assert client.get(f"/api/service-records/{latest}/report").content == new_pdf
    v = db.get_customer(1)["vehicles"][0]
    assert (v["odometer_km"], v["next_service_date"], v["next_service_type"]) == (2100, days_ahead(100), "Third free service")

    # Editing an older record doesn't move the bike; no PDF keeps the old one.
    assert client.patch(f"/api/service-records/{older}", data={**edit, "service_date": days_ahead(-10), "odometer_km": "999"}).status_code == 200
    assert db.get_customer(1)["vehicles"][0]["odometer_km"] == 2100
    assert client.get(f"/api/service-records/{older}/report").content == PDF

    # Same rules as closing.
    assert client.patch(f"/api/service-records/{latest}", data={**edit, "service_date": days_ahead(3)}).status_code == 400
    assert client.patch(f"/api/service-records/{latest}", data=edit, files={"report": ("x.pdf", b"nope", "application/pdf")}).status_code == 400
    assert client.patch("/api/service-records/9999", data=edit).status_code == 404


def test_delete_a_closed_service_reopens_its_booking_and_restores_the_bike(db, monkeypatch):
    """Admins delete a service: the record and PDF go, the booking is upcoming
    again, and the bike is back to how it was before closing."""
    import json

    staff = _api_client(monkeypatch, admin=False)
    before = db.get_customer(1)["vehicles"][0]
    aid = staff.post("/api/appointments", json={"customer_id": 1, "appointment_date": days_ahead(1)}).json()["id"]
    form = {"service_date": days_ahead(0), "odometer_km": "5000", "jobs_done": json.dumps(["Water wash"]),
            "next_service_date": days_ahead(90), "next_service_type": "Second free service"}
    rid = staff.post(f"/api/appointments/{aid}/close", data=form, files={"report": ("a.pdf", PDF, "application/pdf")}).json()["id"]
    assert "bike_before" not in staff.get(f"/api/vehicles/{before['id']}/service-history").json()[0]

    assert staff.delete(f"/api/service-records/{rid}").status_code == 403  # admins only
    admin = _api_client(monkeypatch, admin=True)
    r = admin.delete(f"/api/service-records/{rid}")
    assert r.status_code == 200, r.text
    assert admin.get(f"/api/service-records/{rid}/report").status_code == 404
    assert all(i.get("id") != rid for i in admin.get(f"/api/vehicles/{before['id']}/service-history").json())
    assert [a["id"] for a in db.get_appointments(1)] == [aid]  # upcoming again, can be closed again
    after = db.get_customer(1)["vehicles"][0]
    for column in db.BIKE_STATE:
        assert after[column] == before[column], column
    assert admin.delete(f"/api/service-records/{rid}").status_code == 404


def test_dashboard_lists_upcoming_bookings_and_recent_services(db, monkeypatch):
    import json

    client = _api_client(monkeypatch, admin=False)
    today_id = client.post("/api/appointments", json={"customer_id": 1, "appointment_date": days_ahead(0)}).json()["id"]
    later = client.post("/api/appointments", json={"customer_id": 2, "appointment_date": days_ahead(3)}).json()["id"]
    form = {"service_date": days_ahead(0), "jobs_done": json.dumps(["Water wash"]), "job_card_no": "9001"}
    rid = client.post(f"/api/appointments/{today_id}/close", data=form,
                      files={"report": ("a.pdf", PDF, "application/pdf")}).json()["id"]
    stats = client.get("/api/stats").json()
    assert later in [a["id"] for a in stats["upcoming_list"]]
    assert all(a["appointment_date"] > days_ahead(0) for a in stats["upcoming_list"])
    top = stats["recent_services"][0]
    assert (top["kind"], top["id"], top["job_card_no"], top["customer_id"]) == ("record", rid, "9001", 1)
    dates = [r["service_date"] for r in stats["recent_services"]]
    assert dates == sorted(dates, reverse=True) and len(dates) <= 8


def test_bike_bought_elsewhere_walks_in_for_its_third_free_service(db, monkeypatch):
    """Most bikes serviced here were bought from another dealer: added without
    a purchase date, then serviced as a walk-in (no booking) for whichever
    service it came for. The bike moves on from that service; deleting it
    puts the bike back."""
    import json

    import bot

    staff = _api_client(monkeypatch, admin=False)
    r = staff.post("/api/vehicles", json={
        "name": "Walk-in Murugan", "mobile_number": "9000011111", "registration_number": "tn 72 x 4321",
        "model": "Yamaha FZ-S", "next_service_date": days_ahead(0), "next_service_type": "Third free service",
        "bought_elsewhere": True, "dealer_name": "  Other Yamaha, Tirunelveli ",
    })
    assert r.status_code == 201, r.text
    bike = next(v for v in staff.get("/api/vehicles").json() if v["registration_number"] == "TN 72 X 4321")
    assert (bike["bought_elsewhere"], bike["dealer_name"], bike["purchase_date"]) == (1, "Other Yamaha, Tirunelveli", None)
    assert "bought at another dealer" in bot._bought(bike)
    assert bot._bought({"bought_elsewhere": 0, "purchase_date": None}) == "bought here"

    form = {"service_date": days_ahead(0), "service_type": "Third free service", "job_type": "Free service",
            "odometer_km": "9100", "jobs_done": json.dumps(["Engine oil change"]), "job_card_no": "7001",
            "next_service_date": days_ahead(120), "next_service_type": "Paid service"}
    pdf = {"report": ("7001.pdf", PDF, "application/pdf")}
    url = f"/api/vehicles/{bike['id']}/services"
    assert staff.post(url, data={**form, "service_type": ""}, files=pdf).status_code == 400  # which service?
    assert staff.post("/api/vehicles/99999/services", data=form, files=pdf).status_code == 404
    done_before = staff.get("/api/stats").json()["totals"]["completed_this_month"]
    r = staff.post(url, data=form, files=pdf)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]

    after = next(v for v in staff.get("/api/vehicles").json() if v["id"] == bike["id"])
    assert (after["last_service_date"], after["odometer_km"]) == (days_ahead(0), 9100)
    assert (after["next_service_type"], after["next_service_date"]) == ("Paid service", days_ahead(120))
    history = staff.get(f"/api/vehicles/{bike['id']}/service-history").json()
    assert (history[0]["id"], history[0]["service_type"], history[0]["appointment_id"]) == (rid, "Third free service", None)
    assert staff.get("/api/stats").json()["totals"]["completed_this_month"] == done_before + 1

    # With a booking open, that booking is closed instead (no double record).
    karthik = db.get_customer_by_mobile("9876543210")
    staff.post("/api/appointments", json={"customer_id": karthik["id"], "appointment_date": days_ahead(1)})
    r = staff.post(f"/api/vehicles/{karthik['vehicles'][0]['id']}/services", data=form, files=pdf)
    assert r.status_code == 400 and "Close that booking" in r.json()["detail"]

    # Deleting the walk-in service puts the bike back (nothing to reopen).
    admin = _api_client(monkeypatch, admin=True)
    assert admin.delete(f"/api/service-records/{rid}").status_code == 200
    back = next(v for v in admin.get("/api/vehicles").json() if v["id"] == bike["id"])
    assert (back["next_service_type"], back["next_service_date"], back["last_service_date"]) == (
        "Third free service", days_ahead(0), None)

    # Editing: bought here after all, with a purchase date; then "not known" again.
    assert admin.patch(f"/api/vehicles/{bike['id']}", json={"bought_elsewhere": False, "purchase_date": "2025-01-10"}).status_code == 200
    edited = next(v for v in admin.get("/api/vehicles").json() if v["id"] == bike["id"])
    assert (edited["bought_elsewhere"], edited["dealer_name"], edited["purchase_date"]) == (0, None, "2025-01-10")
    assert admin.patch(f"/api/vehicles/{bike['id']}", json={"bought_elsewhere": True, "purchase_date": ""}).status_code == 200
    edited = next(v for v in admin.get("/api/vehicles").json() if v["id"] == bike["id"])
    assert (edited["bought_elsewhere"], edited["purchase_date"]) == (1, None)


def test_service_without_oil_change_waits_for_the_oil_change_to_set_the_next_date(db, monkeypatch):
    """Case 1: serviced, but the engine oil wasn't changed, so the next date
    can't be fixed. The bike keeps its next service type, drops out of the
    call list, and the visit where the oil is changed sets the date."""
    import json

    import bot

    staff = _api_client(monkeypatch, admin=False)
    karthik = db.get_customer_by_mobile("9876543210")
    bike_id = karthik["vehicles"][0]["id"]
    aid = staff.post("/api/appointments", json={"customer_id": karthik["id"], "appointment_date": days_ahead(1)}).json()["id"]
    pdf = {"report": ("a.pdf", PDF, "application/pdf")}
    first = {"service_date": days_ahead(-7), "service_type": "First free service", "odometer_km": "1000",
             "jobs_done": json.dumps(["Water wash", "General check-up"]),
             "schedule": "pending", "next_service_type": "Second free service",
             "next_service_date": days_ahead(80)}  # ignored: no date until the oil change
    assert staff.post(f"/api/appointments/{aid}/close", data={**first, "next_service_type": ""}, files=pdf).status_code == 400
    assert staff.post(f"/api/appointments/{aid}/close", data={**first, "schedule": "maybe"}, files=pdf).status_code == 400
    assert staff.post(f"/api/appointments/{aid}/close", data=first, files=pdf).status_code == 201

    bike = db.get_customer(karthik["id"])["vehicles"][0]
    assert (bike["next_due_pending"], bike["next_service_type"], bike["last_service_date"]) == (1, "Second free service", days_ahead(-7))
    # Not in the call list, however far ahead it looks (its date is only an estimate).
    assert bike_id not in [d["vehicle_id"] for d in staff.get("/api/due-services?days=400").json()]
    assert "after its next oil change" in bot.build_system_instruction(db.get_customer(karthik["id"]))

    # A week later: oil changed (walk-in), so now the date is fixed.
    oil = {"service_date": days_ahead(0), "service_type": "General check-up", "odometer_km": "1150",
           "jobs_done": json.dumps(["Engine oil change"]), "schedule": "set",
           "next_service_type": "Second free service", "next_service_date": days_ahead(90)}
    assert staff.post(f"/api/vehicles/{bike_id}/services", data=oil, files=pdf).status_code == 201
    bike = db.get_customer(karthik["id"])["vehicles"][0]
    assert (bike["next_due_pending"], bike["next_service_type"], bike["next_service_date"]) == (0, "Second free service", days_ahead(90))
    assert "after its next oil change" not in bot.build_system_instruction(db.get_customer(karthik["id"]))


def test_accident_repair_after_a_service_leaves_the_schedule_alone(db, monkeypatch):
    """Case 2: serviced, then a few days later in for an accident repair. The
    repair is recorded (odometer too) but the service schedule doesn't move;
    editing the earlier service still updates the bike."""
    import json

    staff = _api_client(monkeypatch, admin=False)
    karthik = db.get_customer_by_mobile("9876543210")
    bike_id = karthik["vehicles"][0]["id"]
    aid = staff.post("/api/appointments", json={"customer_id": karthik["id"], "appointment_date": days_ahead(1)}).json()["id"]
    pdf = {"report": ("a.pdf", PDF, "application/pdf")}
    service = {"service_date": days_ahead(-4), "service_type": "Second free service", "odometer_km": "4000",
               "jobs_done": json.dumps(["Engine oil change"]),
               "next_service_type": "Third free service", "next_service_date": days_ahead(86)}
    sid = staff.post(f"/api/appointments/{aid}/close", data=service, files=pdf).json()["id"]
    repair = {"service_date": days_ahead(0), "service_type": "Accident repair", "job_type": "Accidental",
              "odometer_km": "4120", "work_done": "Front fork and headlamp replaced", "schedule": "keep",
              "next_service_type": "Paid service", "next_service_date": days_ahead(10)}  # ignored for a repair
    rid = staff.post(f"/api/vehicles/{bike_id}/services", data=repair, files=pdf).json()["id"]
    bike = db.get_customer(karthik["id"])["vehicles"][0]
    assert (bike["last_service_date"], bike["next_service_type"], bike["next_service_date"], bike["odometer_km"]) == (
        days_ahead(-4), "Third free service", days_ahead(86), 4120)
    history = staff.get(f"/api/vehicles/{bike_id}/service-history").json()
    assert [(h["id"], h["schedule"]) for h in history[:2]] == [(rid, "keep"), (sid, "set")]

    # The earlier service is still the one that decides the schedule.
    staff.patch(f"/api/service-records/{sid}", data={**service, "next_service_date": days_ahead(100)}, files=None)
    bike = db.get_customer(karthik["id"])["vehicles"][0]
    assert (bike["next_service_date"], bike["odometer_km"]) == (days_ahead(100), 4120)  # odometer: the repair's

    admin = _api_client(monkeypatch, admin=True)
    assert admin.delete(f"/api/service-records/{rid}").status_code == 200
    bike = db.get_customer(karthik["id"])["vehicles"][0]
    assert (bike["next_service_date"], bike["odometer_km"]) == (days_ahead(100), 4000)



def test_accident_repair_type_is_recorded_without_any_follow_up(db, monkeypatch):
    """Accident repair is a service type of its own (seeded once, editable):
    a visit under it is recorded with its job card but never changes the
    bike's schedule, so it never leads to a follow-up; it can't be a bike's
    next service either."""
    import json

    staff = _api_client(monkeypatch, admin=False)
    types = {t["name"]: t for t in staff.get("/api/service-types").json()}
    assert types["Accident repair"]["is_repair"] is True and types["Accident repair"]["interval_days"] is None
    assert types["Paid service"]["is_repair"] is False
    karthik = db.get_customer_by_mobile("9876543210")
    bike = karthik["vehicles"][0]
    pdf = {"report": ("a.pdf", PDF, "application/pdf")}
    visit = {"service_date": days_ahead(0), "service_type": "Accident repair", "job_type": "Accidental",
             "odometer_km": "6100", "work_done": "Fairing and indicator replaced",
             "schedule": "set", "next_service_type": "Paid service", "next_service_date": days_ahead(5)}
    r = staff.post(f"/api/vehicles/{bike['id']}/services", data=visit, files=pdf)  # "set" is overridden
    assert r.status_code == 201, r.text
    after = db.get_customer(karthik["id"])["vehicles"][0]
    assert (after["next_service_type"], after["next_service_date"], after["last_service_date"], after["odometer_km"]) == (
        bike["next_service_type"], bike["next_service_date"], bike["last_service_date"], 6100)
    assert staff.get(f"/api/vehicles/{bike['id']}/service-history").json()[0]["schedule"] == "keep"
    # Booked as an accident repair and closed: same, no follow-up.
    aid = staff.post("/api/appointments", json={"customer_id": karthik["id"], "appointment_date": days_ahead(1),
                                                "service_type": "Accident repair"}).json()["id"]
    assert staff.post(f"/api/appointments/{aid}/close", data=visit, files=pdf).status_code == 201
    assert db.get_customer(karthik["id"])["vehicles"][0]["next_service_date"] == bike["next_service_date"]
    # A repair can't be the next service.
    oil = {**visit, "service_type": "Paid service", "job_type": "Paid service", "next_service_type": "Accident repair"}
    r = staff.post(f"/api/vehicles/{bike['id']}/services", data=oil, files=pdf)
    assert r.status_code == 400 and "repair" in r.json()["detail"]


def test_our_staff_admins_manage_sales_spares_and_mechanics(db, monkeypatch):
    """Our staff: the showroom's people in three categories. Admins add, edit
    and remove them; every signed-in user can list them (the job card's
    mechanic dropdown)."""
    admin = _api_client(monkeypatch, admin=True)
    staff = _api_client(monkeypatch, admin=False)
    r = admin.post("/api/showroom-staff", json={"name": "  Kumar  ", "category": "mechanic", "mobile_number": "+91 98765 11111"})
    assert r.status_code == 201, r.text
    kumar = r.json()["id"]
    assert admin.post("/api/showroom-staff", json={"name": "Ravi", "category": "Sales"}).status_code == 201
    assert admin.post("/api/showroom-staff", json={"name": "Selvi", "category": "Spares"}).status_code == 201
    assert admin.post("/api/showroom-staff", json={"name": "kumar", "category": "Mechanic"}).status_code == 400  # duplicate
    assert admin.post("/api/showroom-staff", json={"name": "Kumar", "category": "Sales"}).status_code == 201  # other category: fine
    assert admin.post("/api/showroom-staff", json={"name": "X", "category": "Driver"}).status_code == 400
    assert admin.post("/api/showroom-staff", json={"name": "X", "category": "Sales", "mobile_number": "123"}).status_code == 400
    assert staff.post("/api/showroom-staff", json={"name": "Y", "category": "Sales"}).status_code == 403  # admins only

    people = staff.get("/api/showroom-staff").json()
    assert [(p["category"], p["name"]) for p in people] == [
        ("Sales", "Kumar"), ("Sales", "Ravi"), ("Spares", "Selvi"), ("Mechanic", "Kumar")]
    mech = next(p for p in people if p["id"] == kumar)
    assert (mech["mobile_number"], mech["active"]) == ("9876511111", True)

    assert staff.patch(f"/api/showroom-staff/{kumar}", json={"active": False}).status_code == 403
    assert admin.patch(f"/api/showroom-staff/{kumar}", json={"active": False, "mobile_number": ""}).status_code == 200
    mech = next(p for p in staff.get("/api/showroom-staff").json() if p["id"] == kumar)
    assert (mech["active"], mech["mobile_number"]) == (False, None)
    assert admin.delete(f"/api/showroom-staff/{kumar}").status_code == 200
    assert admin.delete(f"/api/showroom-staff/{kumar}").status_code == 404


def test_admins_manage_our_staff_categories(db, monkeypatch):
    """Sales, Spares and Mechanic to start; admins add, rename (staff move
    with it) and delete empty categories; one category's people can be the
    job card's mechanics."""
    admin = _api_client(monkeypatch, admin=True)
    staff = _api_client(monkeypatch, admin=False)
    cats = staff.get("/api/staff-categories").json()
    assert [(c["name"], c["is_mechanic"]) for c in cats] == [("Sales", False), ("Spares", False), ("Mechanic", True)]

    assert admin.post("/api/showroom-staff", json={"name": "Anand", "category": "Driver"}).status_code == 400  # no such category
    assert staff.post("/api/staff-categories", json={"name": "Driver"}).status_code == 403  # admins only
    r = admin.post("/api/staff-categories", json={"name": "  Driver "})
    assert r.status_code == 201, r.text
    driver = r.json()["id"]
    assert admin.post("/api/staff-categories", json={"name": "driver"}).status_code == 400  # duplicate
    assert admin.post("/api/showroom-staff", json={"name": "Anand", "category": "driver"}).status_code == 201

    # Rename: the people move with it.
    assert admin.patch(f"/api/staff-categories/{driver}", json={"name": "Delivery driver"}).status_code == 200
    anand = next(p for p in staff.get("/api/showroom-staff").json() if p["name"] == "Anand")
    assert anand["category"] == "Delivery driver"
    assert next(c for c in staff.get("/api/staff-categories").json() if c["id"] == driver)["staff_count"] == 1

    # Not deleted while it has people.
    r = admin.delete(f"/api/staff-categories/{driver}")
    assert r.status_code == 400 and "still has 1" in r.json()["detail"]
    admin.delete(f"/api/showroom-staff/{anand['id']}")
    assert admin.delete(f"/api/staff-categories/{driver}").status_code == 200
    assert admin.delete(f"/api/staff-categories/{driver}").status_code == 404

    # Which category is the job card's mechanics can change.
    mech = next(c for c in cats if c["name"] == "Mechanic")
    assert admin.patch(f"/api/staff-categories/{mech['id']}", json={"name": "Technician", "is_mechanic": True}).status_code == 200
    assert next(c for c in staff.get("/api/staff-categories").json() if c["id"] == mech["id"])["name"] == "Technician"


def test_bike_models_catalog(db, monkeypatch):
    """Bikes page: the model list. Filled from the bikes on record; add,
    rename (bikes follow) and delete unused models; per-model counts; a bike
    with a model not on the list (agent, import) adds it."""
    client = _api_client(monkeypatch, admin=False)
    models = {m["name"]: m for m in client.get("/api/bike-models").json()}
    assert "Yamaha FZ-S Fi" in models and models["Yamaha FZ-S Fi"]["bikes"] == 1 and models["Yamaha FZ-S Fi"]["customers"] == 1

    r = client.post("/api/bike-models", json={"name": "  Yamaha  MT-15 V2 "})
    assert r.status_code == 201, r.text
    mt = r.json()["id"]
    assert client.post("/api/bike-models", json={"name": "yamaha mt-15 v2"}).status_code == 400  # duplicate
    assert client.post("/api/bike-models", json={"name": " "}).status_code == 400

    # A bike in any case/spacing uses the catalog name; a new model is added.
    karthik = db.get_customer_by_mobile("9876543210")
    assert client.post("/api/vehicles", json={"name": karthik["name"], "mobile_number": karthik["mobile_number"],
        "registration_number": "TN 69 Z 1", "model": "yamaha mt-15   v2", "next_service_date": days_ahead(30),
        "next_service_type": "First free service"}).status_code == 201
    assert client.post("/api/vehicles", json={"name": "New Person", "mobile_number": "9000022222",
        "registration_number": "TN 69 Z 2", "model": "Yamaha Aerox 155", "next_service_date": days_ahead(30),
        "next_service_type": "First free service"}).status_code == 201
    models = {m["name"]: m for m in client.get("/api/bike-models").json()}
    assert models["Yamaha MT-15 V2"]["bikes"] == 1 and "Yamaha Aerox 155" in models
    bike = next(v for v in client.get("/api/vehicles").json() if v["registration_number"] == "TN 69 Z 1")
    assert bike["model"] == "Yamaha MT-15 V2"

    # Rename: the bikes follow. Delete: only when no bike uses it.
    assert client.patch(f"/api/bike-models/{mt}", json={"name": "Yamaha MT-15 Version 2"}).status_code == 200
    bike = next(v for v in client.get("/api/vehicles").json() if v["registration_number"] == "TN 69 Z 1")
    assert bike["model"] == "Yamaha MT-15 Version 2"
    r = client.delete(f"/api/bike-models/{mt}")
    assert r.status_code == 400 and "Change their model first" in r.json()["detail"]
    client.delete(f"/api/vehicles/{bike['id']}")
    assert client.delete(f"/api/bike-models/{mt}").status_code == 200
    assert client.delete(f"/api/bike-models/{mt}").status_code == 404
