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


@pytest.fixture(params=BACKENDS)
def db(request, tmp_path, monkeypatch):
    """Provide a fresh, isolated database module for each test."""
    import database

    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
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
    # Soft delete: the row is kept as CANCELLED.
    history = db.get_appointments(2, include_cancelled=True)
    assert {h["status"] for h in history} == {"CANCELLED", "COMPLETED"}


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
    assert "Yamaha" in prompt and "Nazareth" in prompt and "TANGLISH" in prompt
    assert "Thoothukudi" in prompt
    # The date is not baked into the prompt: the agent must use the datetime tool.
    assert db.today().isoformat() not in prompt
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

    # Her own bike by model or partial registration.
    assert "First free service" in await search(model="ray")
    assert "Found 1" in await search(registration_number="5678")

    # Another customer by name only: hidden.
    hidden = await search(customer_name="Suresh")
    assert "hidden" in hidden and "R15" not in hidden
    # ...but shown with the full registration number.
    shown = await search(registration_number="TN22EF9012")
    assert "R15 V4" in shown and "customer Suresh Kumar" in shown
    # ...or the full mobile number, and the number itself is never echoed.
    by_mobile = await search(mobile_number="9840012345")
    assert "R15 V4" in by_mobile and "9840012345" not in by_mobile

    assert "No matching" in await search(customer_name="Nobody")


def test_api_key_and_cors(db, monkeypatch):
    """api.py needs X-API-Key once API_KEY is set, and allows the frontend's origin."""
    import importlib

    from fastapi.testclient import TestClient

    import api

    client = TestClient(api.app)
    assert client.get("/api/customers").status_code == 200  # no API_KEY: open (local)

    monkeypatch.setenv("API_KEY", "s3cret")
    assert client.get("/api/customers").status_code == 401
    assert client.get("/api/customers", headers={"X-API-Key": "wrong"}).status_code == 401
    ok = client.get("/api/customers", headers={"X-API-Key": "s3cret"})
    assert ok.status_code == 200 and len(ok.json()) == 4

    # CORS: only the GoDaddy frontend may call the API from a browser.
    monkeypatch.setenv("CORS_ORIGINS", "https://arulmotors.example")
    client = TestClient(importlib.reload(api).app)
    preflight = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-api-key"}
    allowed = client.options("/api/customers", headers={"Origin": "https://arulmotors.example", **preflight})
    assert allowed.headers.get("access-control-allow-origin") == "https://arulmotors.example"
    blocked = client.options("/api/customers", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in blocked.headers


def test_sqlite_shared_by_two_processes(db):
    """Voice agent and API share the SQLite file: WAL mode + a lock timeout."""
    conn = db._connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
