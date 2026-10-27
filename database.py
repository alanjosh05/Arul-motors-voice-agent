import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

logger = logging.getLogger("arul-motors-db")

# The voice agent and the API server share one database.
# - MySQL (production, e.g. GoDaddy cPanel MySQL): set MYSQL_HOST, MYSQL_USER,
#   MYSQL_PASSWORD and MYSQL_DATABASE (optionally MYSQL_PORT, MYSQL_SSL,
#   MYSQL_SSL_CA).
# - Otherwise SQLite: a file next to this one (Arul-Motors/arul_motors.db), or
#   DB_FILE.
_HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.getenv("DB_FILE", os.path.join(_HERE, "arul_motors.db"))


def using_mysql() -> bool:
    return bool(os.getenv("MYSQL_HOST"))


# Demo customers are seeded into an empty database, but never into MySQL
# (production) unless SEED_DEMO=1.
def _seed_demo() -> bool:
    return os.getenv("SEED_DEMO", "0" if using_mysql() else "1") == "1"

# Arul Motors is a Yamaha sub-dealer showroom in Nazareth, Thoothukudi district,
# Tamil Nadu: it sells bikes and services them. The service team calls customers
# who bought a bike to find out which DATE they will bring it in for its next
# service. There are no time slots, just the date.
SHOWROOM_LOCATION = "Nazareth, Thoothukudi district, Tamil Nadu"
# All "today" / "now" logic uses the showroom's local time (IST), so dates are
# right even if the server runs in another timezone.
SHOWROOM_TZ = ZoneInfo(os.getenv("SHOWROOM_TZ", "Asia/Kolkata"))
#
# Weekdays the workshop does not take bikes (Monday=0 ... Sunday=6). Arul
# Motors' workshop is open Monday-Saturday; Sunday is the weekly holiday.
CLOSED_WEEKDAYS: set[int] = {6}
MAX_DAYS_AHEAD = 30

# Yamaha service schedule, in order (used to label a bike's previous service).
SERVICE_SEQUENCE = [
    "First free service",
    "Second free service",
    "Offer free service",
    "Paid service",
]

# Service types created in a new database (editable in the CRM). Intervals
# are starting points: when the next service of this type falls due after the
# previous one (days / km). None = on demand, not part of the schedule.
DEFAULT_SERVICE_TYPES = [
    ("First free service", "First free check-up after purchase.", 1, 30, 1000),
    ("Second free service", "Second free service.", 1, 90, 4000),
    ("Third free service", "Third free service.", 1, 180, 8000),
    ("Paid service", "Regular paid service after the free services.", 0, 120, 4000),
    ("General check-up", "Inspection for a problem the customer reports.", 0, None, None),
]
DEFAULT_REPAIR_TYPE = ("Accident repair", "Repair after an accident. Recorded with its job card; no follow-up.")

# Outcomes the agent may record for a follow-up call.
CALL_OUTCOMES = {
    "date_confirmed",
    "callback_requested",
    "not_interested",
    "already_serviced",
    "bike_sold",
    "wrong_number",
    "no_answer",
}


# Working hours (showroom time). Outside them the CRM is closed to staff;
# admins can still use it (after accepting a warning in the CRM). The VM's
# instance schedule (deploy/GCP-LESSONS.md, Lesson 10) matches these hours.
SHIFT_START = os.getenv("SHIFT_START", "07:00")
SHIFT_END = os.getenv("SHIFT_END", "21:00")


def _minutes(value: str) -> int:
    """'07:00' -> 420; '24:00' (end of the day) -> 1440."""
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def shift_info(at: datetime | None = None) -> dict:
    """Whether the CRM is within working hours now, and when that changes."""
    at = at or now()
    midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
    minute, start, end = at.hour * 60 + at.minute, _minutes(SHIFT_START), _minutes(SHIFT_END)
    is_open = start <= minute < end
    if is_open:
        changes_at = midnight + timedelta(minutes=end)  # closes
    elif minute < start:
        changes_at = midnight + timedelta(minutes=start)  # opens later today
    else:
        changes_at = midnight + timedelta(days=1, minutes=start)  # opens tomorrow
    return {
        "open": is_open,
        "start": SHIFT_START,
        "end": SHIFT_END,
        "changes_at": changes_at.isoformat(timespec="minutes"),
    }


OUT_OF_SHIFT = "Out of shift hours: the CRM is open {start} – {end}. Only admins can use it now."


def now() -> datetime:
    """Current date and time at the showroom."""
    return datetime.now(SHOWROOM_TZ)


def today() -> date:
    """Today's date at the showroom."""
    return now().date()


class _MySQLCursor:
    """Lets the SQLite-style queries below (with ? placeholders) run on MySQL."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql: str, params=()):
        return self._cursor.execute(sql.replace("?", "%s"), params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def lastrowid(self):
        return self._cursor.lastrowid


class _MySQLConnection:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self) -> _MySQLCursor:
        return _MySQLCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def _connect_mysql() -> _MySQLConnection:
    import pymysql

    tls = {}
    if os.getenv("MYSQL_SSL_CA"):
        # Encrypted and the server certificate is verified.
        tls = {"ssl_ca": os.getenv("MYSQL_SSL_CA"), "ssl_verify_cert": True}
    elif os.getenv("MYSQL_SSL", "").lower() in ("1", "true", "on", "required"):
        # Encrypted, but the certificate is not verified (no CA file available).
        tls = {"ssl_verify_cert": False}
    conn = pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
        charset="utf8mb4",  # Tamil text in names and notes
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        init_command="SET time_zone = '+00:00'",  # timestamps in UTC, as SQLite stores them
        **tls,
    )
    return _MySQLConnection(conn)


def _connect():
    # One short-lived connection per operation: shared MySQL hosting limits
    # simultaneous connections, and calls are minutes apart.
    if using_mysql():
        return _connect_mysql()
    # The voice agent and the API are separate processes sharing this file.
    # WAL (enabled once in init_db; it persists in the file) lets one read
    # while the other writes; the timeout makes a writer wait for the other's
    # write instead of failing with "database is locked". synchronous=NORMAL is
    # safe with WAL and avoids an fsync on every commit.
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA synchronous=NORMAL")
    # Reads go through the OS page cache via mmap (shared by the bot and API
    # and kept between these short-lived connections); temp tables in memory.
    conn.execute("PRAGMA mmap_size=134217728")  # 128 MB
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.row_factory = sqlite3.Row
    return conn


# Column types that differ between the two databases. MySQL can't index TEXT,
# so keys and short values are VARCHAR. Dates stay 'YYYY-MM-DD' strings in
# both, so the rest of the code compares and parses them the same way.
def _schema() -> dict:
    if using_mysql():
        return {
            "pk": "INT AUTO_INCREMENT PRIMARY KEY",
            "str": "VARCHAR(100)",
            "day": "VARCHAR(10)",
            "id": "VARCHAR(20)",
            "blob": "LONGBLOB",
            "table": "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        }
    return {
        "pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "str": "TEXT", "day": "TEXT", "id": "TEXT",
        "blob": "BLOB", "table": "",
    }


def _plain(row) -> dict:
    """A result row as a plain dict, with timestamps as 'YYYY-MM-DD HH:MM:SS'
    strings whichever database returned it."""
    return {
        key: value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value
        for key, value in dict(row).items()
    }


def _ensure_index(cursor, name: str, table: str, columns: str) -> None:
    """CREATE INDEX IF NOT EXISTS for both SQLite and MySQL."""
    if using_mysql():
        cursor.execute(
            "SELECT 1 FROM information_schema.statistics WHERE table_schema = DATABASE() "
            "AND table_name = ? AND index_name = ? LIMIT 1",
            (table, name),
        )
        if cursor.fetchone():
            return
        cursor.execute(f"CREATE INDEX {name} ON {table} ({columns})")
    else:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})")


def _ensure_column(cursor, table: str, column: str, column_type: str) -> bool:
    """Add a column to a table created by an older version (SQLite and MySQL).
    Returns True if it was added just now."""
    if using_mysql():
        cursor.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() "
            "AND table_name = ? AND column_name = ? LIMIT 1",
            (table, column),
        )
        exists = cursor.fetchone() is not None
    else:
        cursor.execute(f"PRAGMA table_info({table})")
        exists = any(row["name"] == column for row in cursor.fetchall())
    if not exists:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
    return not exists


# Indexes for the lookups the CRM, the agent and the live feed make.
INDEXES = [
    ("ix_vehicles_customer", "vehicles", "customer_id"),
    ("ix_vehicles_next_due", "vehicles", "next_service_date"),
    ("ix_appts_customer", "service_appointments", "customer_id"),
    ("ix_appts_vehicle_status", "service_appointments", "vehicle_id, status"),
    ("ix_appts_status_date", "service_appointments", "status, appointment_date"),
    ("ix_calls_customer", "call_logs", "customer_id, id"),
    ("ix_calls_created", "call_logs", "created_at"),
    ("ix_events_type", "events", "type, id"),
    ("ix_events_ref", "events", "event_ref"),
    ("ix_voice_calls_open", "voice_calls", "ended_at, started_at"),
    ("ix_service_records_vehicle", "service_records", "vehicle_id, service_date"),
    ("ix_service_records_customer", "service_records", "customer_id"),
]


def init_db():
    """Initialize the database with tables and seed demo customers."""
    conn = _connect()
    if not using_mysql():
        conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    t = _schema()

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS customers (
        id {t['pk']},
        name {t['str']} NOT NULL,
        mobile_number {t['str']} NOT NULL UNIQUE,
        alternate_mobile {t['str']},
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)
    # Second number (many customers give two); the agent calls mobile_number.
    _ensure_column(cursor, "customers", "alternate_mobile", t["str"])

    # Bikes bought from the showroom, with the next service that is due.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS vehicles (
        id {t['pk']},
        customer_id INTEGER NOT NULL,
        registration_number {t['str']} NOT NULL UNIQUE,
        model {t['str']} NOT NULL,
        purchase_date {t['day']},
        last_service_date {t['day']},
        odometer_km INTEGER,
        next_service_date {t['day']} NOT NULL,
        next_service_type {t['str']} NOT NULL,
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    ) {t['table']}
    """)

    if not using_mysql():
        # Older SQLite databases called this table service_visits; carry their rows over.
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row["name"] for row in cursor.fetchall()}
        if "service_visits" in tables and "service_appointments" not in tables:
            cursor.execute("ALTER TABLE service_visits RENAME TO service_appointments")
            cursor.execute(
                "ALTER TABLE service_appointments RENAME COLUMN visit_date TO appointment_date"
            )

    # Service appointments: the date a customer will bring their bike in. The
    # customer can create, read, update and cancel these through the agent.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS service_appointments (
        id {t['id']} PRIMARY KEY,
        vehicle_id INTEGER NOT NULL,
        customer_id INTEGER NOT NULL,
        appointment_date {t['day']} NOT NULL,
        service_type {t['str']} NOT NULL,
        notes TEXT,
        status {t['id']} DEFAULT 'CONFIRMED',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (vehicle_id) REFERENCES vehicles(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    ) {t['table']}
    """)

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS call_logs (
        id {t['pk']},
        customer_id INTEGER NOT NULL,
        vehicle_id INTEGER,
        outcome {t['str']} NOT NULL,
        callback_date {t['day']},
        notes TEXT,
        appointment_id {t['id']},
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (vehicle_id) REFERENCES vehicles(id)
    ) {t['table']}
    """)
    # The booking a "date confirmed" call made, so cancelling it removes the call too.
    _ensure_column(cursor, "call_logs", "appointment_id", t["id"])

    # CRM staff logins (dashboard users). Passwords are stored as scrypt hashes.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS staff_users (
        id {t['pk']},
        username {t['str']} NOT NULL UNIQUE,
        full_name {t['str']},
        password_hash TEXT NOT NULL,
        role {t['id']} NOT NULL DEFAULT 'staff',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)

    # The showroom's people (CRM page "Our staff"): sales, spares and mechanics.
    # Not CRM logins (those are staff_users). Mechanics fill the job card's
    # mechanic field.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS showroom_staff (
        id {t['pk']},
        name {t['str']} NOT NULL,
        category {t['id']} NOT NULL,
        mobile_number {t['str']},
        active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)

    # Bike models the showroom deals with (Bikes page): the bike form's model
    # list. Bikes store the model's name (renames cascade). Any model on a bike
    # but not on the list is added at the end of init_db.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS bike_models (
        id {t['pk']},
        name {t['str']} NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)

    # Our staff's categories, managed by admins. Staff store the category's
    # name (renames cascade). `is_mechanic`: its people are the job card's
    # mechanic list. Seeded once with Sales, Spares and Mechanic.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS staff_categories (
        id {t['pk']},
        name {t['str']} NOT NULL UNIQUE,
        is_mechanic INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)
    cursor.execute("SELECT count(*) AS n FROM staff_categories")
    if cursor.fetchone()["n"] == 0:
        for order, (name, is_mechanic) in enumerate(DEFAULT_STAFF_CATEGORIES, start=1):
            cursor.execute(
                "INSERT INTO staff_categories (name, is_mechanic, sort_order) VALUES (?, ?, ?)", (name, is_mechanic, order)
            )

    # Service types offered by the workshop, managed from the CRM. Bikes and
    # bookings store the type's name; `interval_days` / `interval_km` say when
    # the next service of this type falls due (empty = on-demand type).
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS service_types (
        id {t['pk']},
        name {t['str']} NOT NULL UNIQUE,
        description TEXT,
        is_free INTEGER NOT NULL DEFAULT 0,
        interval_days INTEGER,
        interval_km INTEGER,
        price INTEGER,
        overdue_days INTEGER,
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)
    # Days after the due date before this type counts as overdue (empty = 0).
    _ensure_column(cursor, "service_types", "overdue_days", "INTEGER")
    # Repair types (e.g. Accident repair): recorded like a service, but they
    # never change the bike's schedule, so they never lead to a follow-up.
    repairs_new = _ensure_column(cursor, "service_types", "is_repair", "INTEGER DEFAULT 0")
    cursor.execute("SELECT count(*) AS n FROM service_types")
    if cursor.fetchone()["n"] == 0:
        for order, (name, description, is_free, days, km) in enumerate(DEFAULT_SERVICE_TYPES, start=1):
            cursor.execute(
                "INSERT INTO service_types (name, description, is_free, interval_days, interval_km, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, description, is_free, days, km, order),
            )
        repairs_new = True
    if repairs_new:
        # Once (new database, or the first start with repair types): add
        # Accident repair; afterwards the showroom manages it like any type.
        name, description = DEFAULT_REPAIR_TYPE
        cursor.execute("SELECT id FROM service_types WHERE LOWER(name) = LOWER(?)", (name,))
        row = cursor.fetchone()
        if row:
            cursor.execute(
                "UPDATE service_types SET is_repair = 1, interval_days = NULL, interval_km = NULL WHERE id = ?",
                (row["id"],),
            )
        else:
            cursor.execute("SELECT max(sort_order) AS n FROM service_types")
            cursor.execute(
                "INSERT INTO service_types (name, description, is_free, is_repair, sort_order) VALUES (?, ?, 0, 1, ?)",
                (name, description, (cursor.fetchone()["n"] or 0) + 1),
            )

    # Change feed for the CRM's live updates. Both the voice agent and the API
    # append events here; the API streams new rows to browsers over WebSocket.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS events (
        id {t['pk']},
        type {t['str']} NOT NULL,
        event_ref {t['str']},
        payload TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)

    # Maintenance records: what was done at each service, written when staff
    # close a booking ("Close service"). One per booking.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS service_records (
        id {t['pk']},
        appointment_id {t['id']} UNIQUE,
        vehicle_id INTEGER NOT NULL,
        customer_id INTEGER NOT NULL,
        job_card_no {t['str']},
        job_type {t['str']},
        received_date {t['day']},
        service_date {t['day']} NOT NULL,
        service_type {t['str']} NOT NULL,
        odometer_km INTEGER,
        frame_number {t['str']},
        engine_number {t['str']},
        customer_requests TEXT,
        jobs_done TEXT,
        work_done TEXT,
        parts TEXT,
        labour TEXT,
        parts_total INTEGER,
        labour_total INTEGER,
        amount INTEGER,
        delivery_checks TEXT,
        feedback TEXT,
        technician {t['str']},
        mechanic {t['str']},
        supervisor {t['str']},
        remarks TEXT,
        next_service_date {t['day']},
        next_service_type {t['str']},
        created_by {t['str']},
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_by {t['str']},
        updated_at TIMESTAMP NULL
    ) {t['table']}
    """)
    _ensure_column(cursor, "service_records", "updated_by", t["str"])
    _ensure_column(cursor, "service_records", "updated_at", "TIMESTAMP NULL")
    # The bike as it was before this service was closed (JSON), so deleting
    # the record can put the bike back.
    _ensure_column(cursor, "service_records", "bike_before", "TEXT")
    # Bikes: frame (chassis) and engine numbers, filled from the job card.
    _ensure_column(cursor, "vehicles", "frame_number", t["str"])
    _ensure_column(cursor, "vehicles", "engine_number", t["str"])
    # Many bikes serviced here were bought from another dealer: the purchase
    # date is then often unknown, and the first visit can be any service.
    _ensure_column(cursor, "vehicles", "bought_elsewhere", "INTEGER DEFAULT 0")
    _ensure_column(cursor, "vehicles", "dealer_name", t["str"])
    # When the bike was added to the CRM (charts filter by it). Bikes added
    # before this column existed have none: they count only in "all time".
    _ensure_column(cursor, "vehicles", "created_at", "TIMESTAMP NULL")
    # A next service with no date yet (left out of due lists and calls):
    # PENDING_OIL (1) the oil wasn't changed, the oil-change visit sets it;
    # PENDING_FIRST_VISIT (2) bought elsewhere, its first visit here sets it.
    _ensure_column(cursor, "vehicles", "next_due_pending", "INTEGER DEFAULT 0")
    # How a record changed the bike's schedule: set (default) / pending (next
    # service after the oil change) / keep (a repair: schedule unchanged).
    _ensure_column(cursor, "service_records", "schedule", t["str"])

    # The service report PDF for each record, as a BLOB. A separate table so
    # lists and history never read the file; only "View PDF" does.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS service_documents (
        id {t['pk']},
        record_id INTEGER NOT NULL UNIQUE,
        filename {t['str']} NOT NULL,
        content_type {t['str']} NOT NULL,
        size_bytes INTEGER NOT NULL,
        data {t['blob']} NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)

    # Showroom-wide settings changed from the CRM (admins), e.g. whether the
    # voice agent may take calls. One row per setting; missing = default.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS app_settings (
        name {t['str']} PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)

    # Voice-agent calls: open rows (ended_at NULL) are calls in progress.
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS voice_calls (
        id {t['pk']},
        customer_id INTEGER NOT NULL,
        customer_name {t['str']},
        started_by {t['str']},
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP NULL,
        outcome {t['str']}
    ) {t['table']}
    """)

    # [Plivo disabled — pending client decision on telephony]
    # # Maps a Plivo call UUID to the customer being called. Plivo's media stream
    # # only carries the call UUID, so the bot looks the customer up here.
    # cursor.execute("""
    # CREATE TABLE IF NOT EXISTS phone_calls (
    #     call_uuid TEXT PRIMARY KEY,
    #     customer_id INTEGER NOT NULL REFERENCES customers(id),
    #     created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    # )
    # """)

    for name, table, columns in INDEXES:
        _ensure_index(cursor, name, table, columns)

    # Seed demo customers if empty. Dates are relative to today so the demo
    # always has upcoming services to call about.
    cursor.execute("SELECT count(*) AS n FROM customers")
    seeded = cursor.fetchone()["n"] == 0 and _seed_demo()
    if seeded:
        seed_day = today()
        # name, mobile, registration, model, bought (days ago), last service
        # (days ago, None = never), odometer km, due in (days), service type
        seed = [
            ("Karthik Raja", "9876543210", "TN 09 AB 1234", "Yamaha FZ-S Fi", 35, None, 950, 3, "First free service"),
            ("Priya Lakshmi", "9876501234", "TN 07 CD 5678", "Yamaha Ray ZR 125", 140, 80, 4800, 5, "Second free service"),
            ("Suresh Kumar", "9840012345", "TN 22 EF 9012", "Yamaha R15 V4", 230, 120, 9600, 7, "Third free service"),
            ("Anitha Devi", "9500098765", "TN 10 GH 3456", "Yamaha Fascino 125", 400, 110, 13200, 2, "Paid service"),
        ]
        for name, mobile, reg, model, bought_ago, serviced_ago, km, due_in, svc in seed:
            cursor.execute(
                "INSERT INTO customers (name, mobile_number) VALUES (?, ?)",
                (name, mobile),
            )
            cursor.execute(
                """
            INSERT INTO vehicles (customer_id, registration_number, model, purchase_date, last_service_date, odometer_km, next_service_date, next_service_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    cursor.lastrowid,
                    reg,
                    model,
                    (seed_day - timedelta(days=bought_ago)).isoformat(),
                    (seed_day - timedelta(days=serviced_ago)).isoformat() if serviced_ago else None,
                    km,
                    (seed_day + timedelta(days=due_in)).isoformat(),
                    svc,
                ),
            )

    # Demo data only: record each seeded bike's last service as a COMPLETED
    # appointment, so the demo has service history. Real history comes from
    # "Mark as serviced" and the client-data import, never from a restart
    # (that once turned an edited "last service date" into a service record).
    if seeded:
        cursor.execute("""
        SELECT * FROM vehicles v
        WHERE v.last_service_date IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM service_appointments a WHERE a.vehicle_id = v.id AND a.status = 'COMPLETED'
          )
        """)
        for vehicle in cursor.fetchall():
            previous = _previous_service_type(cursor, vehicle["next_service_type"])
            cursor.execute(
                """
            INSERT INTO service_appointments (id, vehicle_id, customer_id, appointment_date, service_type, notes, status)
            VALUES (?, ?, ?, ?, ?, ?, 'COMPLETED')
            """,
                (
                    _next_appointment_id(cursor),
                    vehicle["id"],
                    vehicle["customer_id"],
                    vehicle["last_service_date"],
                    previous,
                    "Recorded from the bike's last service date.",
                ),
            )

    # Cancelling deletes a booking outright; clear any left from before that.
    cursor.execute("DELETE FROM service_appointments WHERE status = 'CANCELLED'")

    # Bikes added before the two kinds of "no date yet" were told apart: one
    # waiting with no service here yet can only be waiting for its first visit.
    cursor.execute(
        "UPDATE vehicles SET next_due_pending = ? WHERE next_due_pending = ? AND COALESCE(bought_elsewhere, 0) = 1 "
        "AND NOT EXISTS (SELECT 1 FROM service_records r WHERE r.vehicle_id = vehicles.id)",
        (PENDING_FIRST_VISIT, PENDING_OIL),
    )

    # Every model on a bike is on the model list (first run: the list is built
    # from the bikes on record).
    cursor.execute("SELECT DISTINCT model FROM vehicles")
    for row in cursor.fetchall():
        if _tidy_model(row["model"]):
            _register_model(cursor, row["model"])

    conn.commit()
    conn.close()
    logger.info("Database initialized")


def _next_appointment_id(cursor) -> str:
    cursor.execute("SELECT count(*) AS n FROM service_appointments")
    return f"AM{1001 + cursor.fetchone()['n']}"


def _first_scheduled_type(cursor) -> str:
    """The first service in the workshop's schedule (a placeholder type)."""
    cursor.execute("SELECT name FROM service_types WHERE interval_days IS NOT NULL ORDER BY sort_order, id LIMIT 1")
    row = cursor.fetchone()
    return row["name"] if row else SERVICE_SEQUENCE[0]


def _previous_service_type(cursor, next_service_type: str) -> str:
    """The service before `next_service_type` in the workshop's schedule (the
    scheduled service types, in order)."""
    cursor.execute("SELECT name FROM service_types WHERE interval_days IS NOT NULL ORDER BY sort_order, id")
    names = [row["name"].lower() for row in cursor.fetchall()] or [s.lower() for s in SERVICE_SEQUENCE]
    index = names.index(next_service_type.lower()) if next_service_type.lower() in names else 0
    if index == 0:
        return "Service"
    cursor.execute("SELECT name FROM service_types WHERE LOWER(name) = ?", (names[index - 1],))
    row = cursor.fetchone()
    return row["name"] if row else "Service"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def normalize_date(value: str | None) -> str | None:
    """Turn 'today', 'tomorrow', or an ISO / DD-MM-YYYY date into YYYY-MM-DD.

    Returns None if the value can't be understood.
    """
    if not value:
        return None
    text = value.strip().lower()
    if text in ("today", "inniki", "indru"):
        return today().isoformat()
    if text in ("tomorrow", "tmrw", "naalai", "nalaiku"):
        return (today() + timedelta(days=1)).isoformat()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _check_appointment_date(value: str) -> tuple[str | None, str | None]:
    """Validate the date a customer will come in. Returns (YYYY-MM-DD, error)."""
    appointment_date = normalize_date(value)
    if not appointment_date:
        return (None, f"I couldn't understand the date '{value}'. Use YYYY-MM-DD.")
    day = date.fromisoformat(appointment_date)
    current = today()
    if day < current:
        return (None, f"{appointment_date} is in the past. Please ask for a future date.")
    if day > current + timedelta(days=MAX_DAYS_AHEAD):
        return (None, f"Please pick a date within the next {MAX_DAYS_AHEAD} days.")
    if day.weekday() in CLOSED_WEEKDAYS:
        return (None, f"The service center is closed on {day.strftime('%A')}s. Please ask for another day.")
    return (appointment_date, None)


# Dates shown to people (CRM messages, notifications, notes) are DD-MM-YYYY.
# Stored dates stay YYYY-MM-DD: they sort and compare correctly as text, and
# normalize_date accepts both, so either can be typed or sent back.
DISPLAY_DATE = "%d-%m-%Y"


def _spoken(day: str) -> str:
    """'2026-10-21' -> '21-10-2026 (Wednesday)', so staff and the agent see the day."""
    when = date.fromisoformat(day)
    return f"{when.strftime(DISPLAY_DATE)} ({when.strftime('%A')})"


# ---------------------------------------------------------------------------
# Customers & bikes
# ---------------------------------------------------------------------------
def get_customer(customer_id: int) -> dict | None:
    """Return a customer with their bikes (and any upcoming appointments)."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customers WHERE id = ?", (customer_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    customer = _plain(row)
    cursor.execute(
        "SELECT * FROM vehicles WHERE customer_id = ? ORDER BY next_service_date",
        (customer_id,),
    )
    customer["vehicles"] = [_plain(v) for v in cursor.fetchall()]
    cursor.execute(
        "SELECT * FROM service_appointments WHERE customer_id = ? AND status = 'CONFIRMED' ORDER BY appointment_date",
        (customer_id,),
    )
    customer["appointments"] = [_plain(a) for a in cursor.fetchall()]
    conn.close()
    return customer


def get_customer_by_mobile(mobile_number: str) -> dict | None:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM customers WHERE mobile_number = ?", (mobile_number,))
    row = cursor.fetchone()
    conn.close()
    return get_customer(row["id"]) if row else None


def get_all_customers() -> list:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT c.id, c.name, c.mobile_number, v.id AS vehicle_id, v.registration_number,
           v.model, v.purchase_date, v.next_service_date, v.next_service_type
    FROM customers c JOIN vehicles v ON v.customer_id = c.id
    ORDER BY v.next_service_date
    """)
    rows = [_plain(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_due_services(within_days: int = 7) -> list:
    """Bikes whose service is due within `within_days` (or overdue) and that have
    no appointment yet: the follow-up call list."""
    cutoff = (today() + timedelta(days=within_days)).isoformat()
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT c.id AS customer_id, c.name, c.mobile_number, v.id AS vehicle_id,
           v.registration_number, v.model, v.next_service_date, v.next_service_type
    FROM vehicles v JOIN customers c ON c.id = v.customer_id
    WHERE v.next_service_date <= ?
      AND COALESCE(v.next_due_pending, 0) = 0
      AND NOT EXISTS (
        SELECT 1 FROM service_appointments a WHERE a.vehicle_id = v.id AND a.status = 'CONFIRMED'
      )
    ORDER BY v.next_service_date
    """,
        (cutoff,),
    )
    rows = [_plain(r) for r in cursor.fetchall()]
    grace = _overdue_days_by_type(cursor)
    conn.close()
    for row in rows:
        row.update(_overdue_info(row["next_service_date"], grace.get(row["next_service_type"], 0)))
    return rows


def _overdue_days_by_type(cursor) -> dict[str, int]:
    """Service type name -> days after the due date before it's overdue."""
    cursor.execute("SELECT name, overdue_days FROM service_types")
    return {r["name"]: r["overdue_days"] or 0 for r in cursor.fetchall()}


def _overdue_info(next_service_date: str, overdue_days: int) -> dict:
    """When a due service becomes overdue (due date + the type's overdue days)
    and whether it is now."""
    due = date.fromisoformat(next_service_date)
    overdue_from = due + timedelta(days=overdue_days)
    return {
        "overdue_days": overdue_days,
        "overdue_from": overdue_from.isoformat(),
        "is_overdue": today() > overdue_from,
    }


def add_customer_vehicle(
    name: str,
    mobile_number: str,
    registration_number: str,
    model: str,
    next_service_date: str | None = None,
    next_service_type: str | None = None,
    purchase_date: str | None = None,
    odometer_km: int | None = None,
    last_service_date: str | None = None,
    alternate_mobile: str | None = None,
    bought_elsewhere: bool = False,
    dealer_name: str | None = None,
) -> tuple[bool, str]:
    """Add a bike (creating the customer, with `alternate_mobile`, if the
    mobile number is new): one sold here, or one `bought_elsewhere` (from
    `dealer_name`, purchase date often unknown) that comes here for service."""
    name = (name or "").strip()
    mobile_number = (mobile_number or "").strip()
    registration_number = (registration_number or "").strip().upper()
    due = normalize_date(next_service_date) if next_service_date else None
    # Bought from another dealer: its next service is usually not known until
    # it first comes in, so it can be left out. The bike then waits (no
    # follow-up call) and that visit sets the date.
    unknown_next = bought_elsewhere and not due and not next_service_type
    if next_service_date and not due:
        return (False, "Invalid next service date.")
    if not all([name, mobile_number, registration_number, model]) or not (unknown_next or (due and next_service_type)):
        return (False, "Name, mobile, registration, model, service type and a valid due date are required.")
    alternate_mobile, error = _check_alternate(alternate_mobile, mobile_number)
    if error:
        return (False, error)

    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM vehicles WHERE registration_number = ?", (registration_number,))
    if cursor.fetchone():
        conn.close()
        return (False, f"Bike {registration_number} already exists.")
    model = _register_model(cursor, model)

    cursor.execute("SELECT id FROM customers WHERE mobile_number = ?", (mobile_number,))
    row = cursor.fetchone()
    if row:
        customer_id = row["id"]
    else:
        cursor.execute(
            "INSERT INTO customers (name, mobile_number, alternate_mobile) VALUES (?, ?, ?)",
            (name, mobile_number, alternate_mobile),
        )
        customer_id = cursor.lastrowid

    cursor.execute(
        """
    INSERT INTO vehicles (customer_id, registration_number, model, purchase_date, last_service_date, odometer_km,
                          next_service_date, next_service_type, bought_elsewhere, dealer_name, next_due_pending, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """,
        (
            customer_id,
            registration_number,
            model,
            normalize_date(purchase_date),
            normalize_date(last_service_date),
            odometer_km,
            # Unknown next service: today as a placeholder (never called: pending).
            due or today().isoformat(),
            next_service_type or _first_scheduled_type(cursor),
            1 if bought_elsewhere else 0,
            (dealer_name or "").strip() or None if bought_elsewhere else None,
            PENDING_FIRST_VISIT if unknown_next else 0,
        ),
    )
    conn.commit()
    conn.close()
    _record_change(["customers", "vehicles", "due", "stats", "bike_models"], customer_id)
    return (True, f"{registration_number} added for {name}.")


def _resolve_vehicle(cursor, customer_id: int, registration_number: str | None) -> dict | None:
    """Pick the customer's bike by registration (fuzzy), or their only/most-due one."""
    cursor.execute(
        "SELECT * FROM vehicles WHERE customer_id = ? ORDER BY next_service_date",
        (customer_id,),
    )
    vehicles = [_plain(v) for v in cursor.fetchall()]
    if not vehicles:
        return None
    if registration_number:
        wanted = re.sub(r"[^A-Z0-9]", "", registration_number.upper())
        for v in vehicles:
            reg = re.sub(r"[^A-Z0-9]", "", v["registration_number"].upper())
            if reg == wanted or reg.endswith(wanted):
                return v
        return None
    return vehicles[0]


# ---------------------------------------------------------------------------
# Service appointments — CRUD (the date the customer will bring the bike in)
# ---------------------------------------------------------------------------
def create_appointment(
    customer_id: int,
    appointment_date: str,
    registration_number: str | None = None,
    service_type: str | None = None,
    notes: str | None = None,
    actor: str = "agent",
) -> tuple[bool, str, str | None]:
    """Create a service appointment for one of the customer's bikes.

    `service_type` defaults to the bike's next due service. `actor` is "agent"
    (voice agent) or "staff:<username>"; agent bookings notify the staff.
    Returns (success, message, appointment_id).
    """
    conn = _connect()
    cursor = conn.cursor()

    vehicle = _resolve_vehicle(cursor, customer_id, registration_number)
    if not vehicle:
        conn.close()
        return (False, "I couldn't find that bike for this customer.", None)

    cursor.execute(
        "SELECT id, appointment_date FROM service_appointments WHERE vehicle_id = ? AND status = 'CONFIRMED'",
        (vehicle["id"],),
    )
    existing = cursor.fetchone()
    if existing:
        conn.close()
        return (
            False,
            f"{vehicle['registration_number']} already has appointment {existing['id']} on "
            f"{_spoken(existing['appointment_date'])}. Update that appointment instead.",
            None,
        )

    day, error = _check_appointment_date(appointment_date)
    if error:
        conn.close()
        return (False, error, None)

    service = (service_type or "").strip() or vehicle["next_service_type"]
    appointment_id = _next_appointment_id(cursor)
    cursor.execute(
        """
    INSERT INTO service_appointments (id, vehicle_id, customer_id, appointment_date, service_type, notes)
    VALUES (?, ?, ?, ?, ?, ?)
    """,
        (appointment_id, vehicle["id"], customer_id, day, service, notes),
    )
    conn.commit()
    conn.close()
    _record_change(BOOKING_ENTITIES, customer_id)
    if actor == "agent":
        notify(
            "booking_created", "success",
            f"Service booked · {_customer_name(customer_id)}",
            f"{vehicle['model']} ({vehicle['registration_number']}) on {_human_day(day)} · {service}",
            customer_id, ref=f"booking:{appointment_id}",
        )
    return (
        True,
        f"Appointment {appointment_id} created: {vehicle['model']} ({vehicle['registration_number']}) "
        f"for {service} on {_spoken(day)}.",
        appointment_id,
    )


def get_appointments(customer_id: int, include_cancelled: bool = False) -> list:
    """The customer's service appointments (upcoming only, unless asked), with bike details."""
    conn = _connect()
    cursor = conn.cursor()
    status_filter = "" if include_cancelled else "AND a.status = 'CONFIRMED'"
    cursor.execute(
        f"""
    SELECT a.*, v.registration_number, v.model
    FROM service_appointments a JOIN vehicles v ON v.id = a.vehicle_id
    WHERE a.customer_id = ? {status_filter}
    ORDER BY a.appointment_date
    """,
        (customer_id,),
    )
    rows = [_plain(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def _get_own_appointment(
    cursor, customer_id: int, appointment_id: str | None
) -> tuple[dict | None, str | None]:
    """Locate one upcoming appointment of this customer (by ID, or their only one)."""
    if appointment_id:
        cursor.execute(
            "SELECT * FROM service_appointments WHERE UPPER(id) = UPPER(?) AND customer_id = ? AND status = 'CONFIRMED'",
            (appointment_id.strip(), customer_id),
        )
    else:
        cursor.execute(
            "SELECT * FROM service_appointments WHERE customer_id = ? AND status = 'CONFIRMED'",
            (customer_id,),
        )
    rows = cursor.fetchall()
    if not rows:
        return (None, "This customer has no upcoming service appointment.")
    if len(rows) > 1:
        return (None, "There is more than one appointment. Say which appointment ID to change.")
    return (_plain(rows[0]), None)


def update_appointment(
    customer_id: int,
    appointment_id: str | None = None,
    new_date: str | None = None,
    notes: str | None = None,
    actor: str = "agent",
) -> tuple[bool, str]:
    """Change the date and/or the notes of one of the customer's appointments."""
    if not new_date and notes is None:
        return (False, "There is nothing to change. Give a new date or new notes.")
    conn = _connect()
    cursor = conn.cursor()
    appointment, error = _get_own_appointment(cursor, customer_id, appointment_id)
    if error:
        conn.close()
        return (False, error)

    changes = []
    if new_date:
        day, error = _check_appointment_date(new_date)
        if error:
            conn.close()
            return (False, error)
        cursor.execute(
            "UPDATE service_appointments SET appointment_date = ? WHERE id = ?",
            (day, appointment["id"]),
        )
        changes.append(f"date moved from {_spoken(appointment['appointment_date'])} to {_spoken(day)}")
    if notes is not None:
        cursor.execute(
            "UPDATE service_appointments SET notes = ? WHERE id = ?",
            (notes.strip() or None, appointment["id"]),
        )
        changes.append("notes updated")
    conn.commit()
    conn.close()
    _record_change(BOOKING_ENTITIES, customer_id)
    if actor == "agent" and new_date:
        notify(
            "booking_rescheduled", "info",
            f"Booking moved · {_customer_name(customer_id)}",
            f"{appointment['id']}: {_human_day(appointment['appointment_date'])} → {_human_day(day)}",
            customer_id,
        )
    return (True, f"Appointment {appointment['id']} updated: {', '.join(changes)}.")


def cancel_appointment(
    customer_id: int, appointment_id: str | None = None, actor: str = "agent"
) -> tuple[bool, str]:
    """Cancel an appointment: it is deleted with every trace of it (its "booked"
    and "missed" notifications, and the agent's "date confirmed" call entry and
    notification for it), so it never shows up as booked or serviced again."""
    conn = _connect()
    cursor = conn.cursor()
    appointment, error = _get_own_appointment(cursor, customer_id, appointment_id)
    if error:
        conn.close()
        return (False, error)
    cursor.execute("DELETE FROM service_appointments WHERE id = ?", (appointment["id"],))
    cursor.execute("DELETE FROM call_logs WHERE appointment_id = ?", (appointment["id"],))
    cursor.execute(
        "DELETE FROM events WHERE event_ref IN (?, ?, ?)",
        (f"booking:{appointment['id']}", f"missed:{appointment['id']}", f"call-booking:{appointment['id']}"),
    )
    conn.commit()
    conn.close()
    _record_change([*BOOKING_ENTITIES, "calls"], customer_id)
    return (
        True,
        f"Appointment {appointment['id']} on {_spoken(appointment['appointment_date'])} has been cancelled.",
    )


def get_all_appointments() -> list:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT a.*, c.name AS customer_name, c.mobile_number, v.registration_number, v.model
    FROM service_appointments a
    JOIN customers c ON c.id = a.customer_id
    JOIN vehicles v ON v.id = a.vehicle_id
    ORDER BY a.appointment_date
    """)
    rows = [_plain(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def complete_appointment(
    appointment_id: str,
    next_service_date: str | None = None,
    next_service_type: str | None = None,
) -> tuple[bool, str]:
    """Workshop side: mark an appointment as serviced (COMPLETED).

    Updates the bike's last service date and, if given, its next due service.
    """
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM service_appointments WHERE UPPER(id) = UPPER(?) AND status = 'CONFIRMED'",
        ((appointment_id or "").strip(),),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return (False, f"No open appointment with ID {appointment_id}.")
    cursor.execute(
        "UPDATE service_appointments SET status = 'COMPLETED' WHERE id = ?", (row["id"],)
    )
    cursor.execute(
        "UPDATE vehicles SET last_service_date = ? WHERE id = ?",
        (row["appointment_date"], row["vehicle_id"]),
    )
    next_due = normalize_date(next_service_date)
    if next_due and next_service_type:
        cursor.execute(
            "UPDATE vehicles SET next_service_date = ?, next_service_type = ? WHERE id = ?",
            (next_due, next_service_type, row["vehicle_id"]),
        )
    conn.commit()
    conn.close()
    _record_change(BOOKING_ENTITIES + ["vehicles"], row["customer_id"])
    return (True, f"Appointment {row['id']} marked as completed.")


# ---------------------------------------------------------------------------
# Maintenance records ("Close service")
# ---------------------------------------------------------------------------
MAX_PDF_BYTES = 10 * 1024 * 1024  # job card / service report upload limit (10 MB)

# From the Arul Motors Yamaha job card.
JOB_TYPES = ["Free service", "Paid service", "Accidental", "Quick repair", "Warranty repair"]
# What a recorded visit does to the bike's service schedule:
# set     - fixes the next service (type and date), as usual;
# pending - the engine oil wasn't changed: the next service's type is known,
#           its date is fixed at the visit where the oil is changed;
# keep    - a repair (e.g. after an accident): the schedule doesn't change.
SCHEDULE_CHOICES = ("set", "pending", "keep")
# vehicles.next_due_pending: why a next service has no date yet (0 = it has one).
PENDING_OIL = 1  # serviced without an oil change: the oil-change visit sets it
PENDING_FIRST_VISIT = 2  # bought elsewhere, not serviced here yet: its first visit sets it
DEFAULT_INTERVAL_DAYS = 90
FEEDBACK_RATINGS = ["Excellent", "Very good", "Good", "Average", "Below expectation"]
FEEDBACK_CHECKS = ["delivery_on_time", "washing_quality", "staff_behaviour", "fairness_of_charges", "repairing_quality"]
_JSON_FIELDS = ("jobs_done", "parts", "labour", "delivery_checks", "feedback")


def _clean_text(value: str | None, limit: int = 2000) -> str | None:
    value = (value or "").strip()
    return value[:limit] or None


def _whole(value, field: str, low: int = 0, high: int = 10_000_000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a whole number.") from None
    if not low <= number <= high:
        raise ValueError(f"{field} must be between {low} and {high}.")
    return number


def _clean_labels(values, field: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 30:
        raise ValueError(f"Invalid {field}.")
    return list(dict.fromkeys(_clean_text(str(v), 120) for v in values if _clean_text(str(v), 120)))


def _clean_parts(rows) -> list[dict]:
    """Parts replaced: [{part_no, name, qty, amount, warranty}]. Warranty parts
    aren't charged to the customer."""
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > 60:
        raise ValueError("Invalid parts list.")
    clean = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError("Invalid parts list.")
        name = _clean_text(row.get("name"), 120)
        if not name:
            raise ValueError(f"Part {i}: enter the part name.")
        clean.append({
            "part_no": _clean_text(row.get("part_no"), 40),
            "name": name,
            "qty": _whole(row.get("qty", 1), f"Part {i} quantity", 1, 999),
            "amount": _whole(row.get("amount") or 0, f"Part {i} amount"),
            "warranty": bool(row.get("warranty")),
        })
    return clean


def _clean_labour(rows) -> list[dict]:
    """Labour: [{description, amount}]."""
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > 40:
        raise ValueError("Invalid labour list.")
    clean = []
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError("Invalid labour list.")
        description = _clean_text(row.get("description"), 160)
        if not description:
            raise ValueError(f"Labour {i}: enter the description.")
        clean.append({"description": description, "amount": _whole(row.get("amount") or 0, f"Labour {i} amount")})
    return clean


def _clean_feedback(value) -> dict:
    """Customer feedback: {rating, delivery_on_time, washing_quality, ...}; each
    check is true (Yes/OK) or false (No/Not OK); missing = not asked."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Invalid customer feedback.")
    clean = {}
    if value.get("rating"):
        if value["rating"] not in FEEDBACK_RATINGS:
            raise ValueError("Invalid feedback rating.")
        clean["rating"] = value["rating"]
    for key in FEEDBACK_CHECKS:
        if value.get(key) is not None:
            clean[key] = bool(value[key])
    return clean


def _record_row(row) -> dict:
    """A service_records row with its JSON fields as lists/dicts."""
    record = _plain(row)
    record.pop("bike_before", None)  # internal: for deleting the record
    for key in _JSON_FIELDS:
        raw = record.get(key)
        try:
            record[key] = json.loads(raw) if raw else ({} if key == "feedback" else [])
        except (TypeError, ValueError):
            record[key] = {} if key == "feedback" else []
    return record


def parts_summary(parts: list[dict]) -> str:
    """'Engine oil ×1 ₹737; Oil cleaner ×1 ₹265 (warranty)' for exports."""
    items = []
    for p in parts:
        number = " (" + p["part_no"] + ")" if p.get("part_no") else ""
        warranty = " (warranty)" if p.get("warranty") else ""
        items.append(f"{p['name']}{number} ×{p['qty']} ₹{p['amount']}{warranty}")
    return "; ".join(items)


def _clean_job_card(d: dict) -> dict:
    """Check and tidy the job card fields from the form (raises ValueError).
    Totals are computed here, never trusted from the form."""
    day = normalize_date(d.get("service_date"))
    if not day:
        raise ValueError("Enter the date the bike was delivered.")
    if day > today().isoformat():
        raise ValueError("The delivery date can't be in the future.")
    received = normalize_date(d.get("received_date")) if d.get("received_date") else None
    if d.get("received_date") and not received:
        raise ValueError("Invalid received date.")
    if received and received > day:
        raise ValueError("The bike can't be received after it was delivered.")
    job_type = _clean_text(d.get("job_type"), 40)
    if job_type and job_type not in JOB_TYPES:
        raise ValueError(f"Type of job must be one of: {', '.join(JOB_TYPES)}.")
    odometer = _whole(d["odometer_km"], "Odometer", 0, 1_000_000) if d.get("odometer_km") not in (None, "") else None
    jobs_done = _clean_labels(d.get("jobs_done"), "jobs done")
    parts = _clean_parts(d.get("parts"))
    labour = _clean_labour(d.get("labour"))
    work_done = _clean_text(d.get("work_done"))
    if not (jobs_done or work_done or parts or labour):
        raise ValueError("Record the work done: tick the jobs, add parts or labour, or describe it.")
    next_due = normalize_date(d.get("next_service_date")) if d.get("next_service_date") else None
    if d.get("next_service_date") and not next_due:
        raise ValueError("Invalid next service date.")
    if next_due and next_due <= day:
        raise ValueError("The next service must be after this service.")
    next_type = _clean_text(d.get("next_service_type"), 100)
    schedule = (d.get("schedule") or "set").strip().lower()
    if schedule not in SCHEDULE_CHOICES:
        raise ValueError(f"Next service must be one of: {', '.join(SCHEDULE_CHOICES)}.")
    if schedule == "pending":
        if not next_type:
            raise ValueError("Choose the next service type (its date is set at the oil change).")
        next_due = None
    elif schedule == "keep":
        next_due, next_type = None, None
    if next_due and not next_type:
        raise ValueError("Choose the next service type.")
    parts_total = sum(p["qty"] * p["amount"] for p in parts if not p["warranty"])
    labour_total = sum(item["amount"] for item in labour)
    return {
        "job_card_no": _clean_text(d.get("job_card_no"), 40),
        "job_type": job_type,
        "received_date": received,
        "service_date": day,
        "service_type": _clean_text(d.get("service_type"), 100),  # caller falls back to the booking's
        "odometer_km": odometer,
        "frame_number": _clean_text(d.get("frame_number"), 40),
        "engine_number": _clean_text(d.get("engine_number"), 40),
        "customer_requests": _clean_text(d.get("customer_requests")),
        "jobs_done": json.dumps(jobs_done),
        "work_done": work_done,
        "parts": json.dumps(parts),
        "labour": json.dumps(labour),
        "parts_total": parts_total,
        "labour_total": labour_total,
        "amount": parts_total + labour_total,
        "delivery_checks": json.dumps(_clean_labels(d.get("delivery_checks"), "delivery checks")),
        "feedback": json.dumps(_clean_feedback(d.get("feedback"))),
        "technician": _clean_text(d.get("technician"), 100),
        "mechanic": _clean_text(d.get("mechanic"), 100),
        "supervisor": _clean_text(d.get("supervisor"), 100),
        "remarks": _clean_text(d.get("remarks")),
        "next_service_date": next_due,
        "next_service_type": next_type if next_due or schedule == "pending" else None,
        "schedule": schedule,
    }


def _check_pdf(pdf: bytes | None) -> None:
    if not pdf:
        raise ValueError("Attach the job card (PDF).")
    if len(pdf) > MAX_PDF_BYTES:
        raise ValueError(f"The PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB.")
    if not pdf.startswith(b"%PDF"):
        raise ValueError("The job card must be a PDF file.")


def _pdf_filename(name: str | None) -> str:
    filename = re.sub(r"[^\w.\- ]", "_", (name or "job-card.pdf").strip())[:80] or "job-card.pdf"
    return filename if filename.lower().endswith(".pdf") else filename + ".pdf"


# What closing a service changes on the bike (and deleting the record restores).
BIKE_STATE = ("last_service_date", "next_service_date", "next_service_type", "odometer_km", "frame_number", "engine_number",
              "next_due_pending")
# The part of BIKE_STATE that any visit updates (a repair too); the rest is the schedule.
READING_STATE = ("odometer_km", "frame_number", "engine_number")


def _repair_types(cursor) -> set[str]:
    cursor.execute("SELECT name FROM service_types WHERE COALESCE(is_repair, 0) = 1")
    return {row["name"].lower() for row in cursor.fetchall()}


def _apply_repair_rule(cursor, rec: dict) -> None:
    """A visit under a repair type (e.g. Accident repair) keeps the schedule:
    no next service, so no follow-up. A repair type is never a next service."""
    repairs = _repair_types(cursor)
    if (rec["service_type"] or "").lower() in repairs:
        rec.update(schedule="keep", next_service_date=None, next_service_type=None)
    elif (rec["next_service_type"] or "").lower() in repairs:
        raise ValueError(f"{rec['next_service_type']} is a repair, so it can't be the next service.")


def _move_bike_on(cursor, vehicle_id: int, record_id: int, rec: dict) -> None:
    """Update the bike from a saved maintenance record `rec` (id `record_id`):
    - readings (odometer, frame/engine numbers) if it's the bike's latest record;
    - its schedule (last service, next service) if it's the latest record that
      counts as a service: a repair ("keep") never moves the schedule, so a
      service a few days before an accident still decides the next one.
    """
    def latest(where: str = "") -> int | None:
        cursor.execute(
            f"SELECT id FROM service_records WHERE vehicle_id = ? {where} ORDER BY service_date DESC, id DESC LIMIT 1",
            (vehicle_id,),
        )
        row = cursor.fetchone()
        return row["id"] if row else None

    if latest() == record_id:
        for column in ("odometer_km", "frame_number", "engine_number"):
            if rec[column] is not None:
                cursor.execute(f"UPDATE vehicles SET {column} = ? WHERE id = ?", (rec[column], vehicle_id))
    schedule = rec.get("schedule") or "set"
    if schedule == "keep" or latest("AND COALESCE(schedule, 'set') != 'keep'") != record_id:
        return
    cursor.execute("UPDATE vehicles SET last_service_date = ? WHERE id = ?", (rec["service_date"], vehicle_id))
    if schedule == "pending":
        # The type is known; the date comes at the oil change. Keep an estimate
        # (the type's interval from this visit), but nothing calls on it.
        cursor.execute("SELECT interval_days FROM service_types WHERE name = ?", (rec["next_service_type"],))
        row = cursor.fetchone()
        days = (row and row["interval_days"]) or DEFAULT_INTERVAL_DAYS
        estimate = (date.fromisoformat(rec["service_date"]) + timedelta(days=days)).isoformat()
        cursor.execute(
            "UPDATE vehicles SET next_service_date = ?, next_service_type = ?, next_due_pending = ? WHERE id = ?",
            (estimate, rec["next_service_type"], PENDING_OIL, vehicle_id),
        )
    elif rec["next_service_date"]:
        cursor.execute(
            "UPDATE vehicles SET next_service_date = ?, next_service_type = ?, next_due_pending = 0 WHERE id = ?",
            (rec["next_service_date"], rec["next_service_type"], vehicle_id),
        )


def close_service(appointment_id: str, details: dict, pdf: bytes | None, pdf_name: str | None,
                  actor: str | None = None) -> tuple[bool, str, int | None]:
    """Close a booking with its job card: saves a maintenance record (the job
    card's fields) and the scanned job card PDF, marks the booking COMPLETED,
    and moves the bike on (last service date, odometer, frame/engine numbers,
    next due service). All in one transaction. Returns (ok, message, record_id).

    details: service_date (delivered), received_date, service_type, job_type,
    job_card_no, odometer_km, frame_number, engine_number, customer_requests,
    jobs_done [str], work_done, parts [{part_no, name, qty, amount, warranty}],
    labour [{description, amount}], delivery_checks [str], feedback {...},
    technician, mechanic, supervisor, remarks, next_service_date, next_service_type.
    """
    try:
        rec = _clean_job_card(details)
        _check_pdf(pdf)
    except ValueError as e:
        return (False, str(e), None)

    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM service_appointments WHERE UPPER(id) = UPPER(?) AND status = 'CONFIRMED'",
        ((appointment_id or "").strip(),),
    )
    booking = cursor.fetchone()
    if not booking:
        conn.close()
        return (False, f"No open booking with ID {appointment_id}.", None)
    rec["service_type"] = rec["service_type"] or booking["service_type"]
    try:
        _apply_repair_rule(cursor, rec)
    except ValueError as e:
        conn.close()
        return (False, str(e), None)
    cursor.execute(f"SELECT {', '.join(BIKE_STATE)} FROM vehicles WHERE id = ?", (booking["vehicle_id"],))
    bike = cursor.fetchone()
    bike_before = json.dumps({column: bike[column] for column in BIKE_STATE}) if bike else None
    columns = ["appointment_id", "vehicle_id", "customer_id", *rec, "created_by", "bike_before"]
    values = [booking["id"], booking["vehicle_id"], booking["customer_id"], *rec.values(), actor, bike_before]
    try:
        cursor.execute(
            f"INSERT INTO service_records ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        record_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO service_documents (record_id, filename, content_type, size_bytes, data) VALUES (?, ?, ?, ?, ?)",
            (record_id, _pdf_filename(pdf_name), "application/pdf", len(pdf), pdf),
        )
        cursor.execute("UPDATE service_appointments SET status = 'COMPLETED' WHERE id = ?", (booking["id"],))
        _move_bike_on(cursor, booking["vehicle_id"], record_id, rec)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    _record_change(BOOKING_ENTITIES + ["vehicles", "service_records"], booking["customer_id"])
    return (True, f"Service closed for booking {booking['id']}.", record_id)


def record_walk_in_service(vehicle_id: int, details: dict, pdf: bytes | None, pdf_name: str | None,
                           actor: str | None = None) -> tuple[bool, str, int | None]:
    """Record a service done without a booking (a walk-in): the bike, often
    bought from another dealer, came in for whichever service it was due (say
    its 3rd free service), and the job card is recorded as that service. Same
    fields and rules as closing a booking. If this is now the bike's latest
    service, the bike moves on (last service, odometer, next due service).
    Returns (ok, message, record_id)."""
    try:
        rec = _clean_job_card(details)
        _check_pdf(pdf)
    except ValueError as e:
        return (False, str(e), None)
    if not rec["service_type"]:
        return (False, "Pick the service that was done (e.g. Third free service).", None)

    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, customer_id, registration_number FROM vehicles WHERE id = ?", (vehicle_id,))
    vehicle = cursor.fetchone()
    if not vehicle:
        conn.close()
        return (False, "Bike not found.", None)
    cursor.execute(
        "SELECT id FROM service_appointments WHERE vehicle_id = ? AND status = 'CONFIRMED' ORDER BY appointment_date LIMIT 1",
        (vehicle_id,),
    )
    booking = cursor.fetchone()
    if booking:
        conn.close()
        return (False, f"This bike has booking {booking['id']}. Close that booking with the job card instead.", None)
    try:
        _apply_repair_rule(cursor, rec)
    except ValueError as e:
        conn.close()
        return (False, str(e), None)
    cursor.execute(f"SELECT {', '.join(BIKE_STATE)} FROM vehicles WHERE id = ?", (vehicle_id,))
    bike = cursor.fetchone()
    bike_before = json.dumps({column: bike[column] for column in BIKE_STATE})
    columns = ["vehicle_id", "customer_id", *rec, "created_by", "bike_before"]
    values = [vehicle_id, vehicle["customer_id"], *rec.values(), actor, bike_before]
    try:
        cursor.execute(
            f"INSERT INTO service_records ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        record_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO service_documents (record_id, filename, content_type, size_bytes, data) VALUES (?, ?, ?, ?, ?)",
            (record_id, _pdf_filename(pdf_name), "application/pdf", len(pdf), pdf),
        )
        # An older service typed in afterwards doesn't move the bike back.
        _move_bike_on(cursor, vehicle_id, record_id, rec)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    _record_change(["vehicles", "service_records", "customers", "due", "stats"], vehicle["customer_id"])
    return (True, f"{rec['service_type']} recorded for {vehicle['registration_number']}.", record_id)


def update_service_record(record_id: int, details: dict, pdf: bytes | None = None, pdf_name: str | None = None,
                          actor: str | None = None) -> tuple[bool, str]:
    """Edit a maintenance record (same rules as closing). A new PDF replaces the
    job card; without one the current PDF stays. If it's the bike's latest
    record, the bike follows (last service, odometer, frame/engine, next due)."""
    try:
        rec = _clean_job_card(details)
        if pdf:
            _check_pdf(pdf)
    except ValueError as e:
        return (False, str(e))
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM service_records WHERE id = ?", (record_id,))
    current = cursor.fetchone()
    if not current:
        conn.close()
        return (False, "Service record not found.")
    rec["service_type"] = rec["service_type"] or current["service_type"]
    try:
        _apply_repair_rule(cursor, rec)
    except ValueError as e:
        conn.close()
        return (False, str(e))
    try:
        assignments = ", ".join(f"{column} = ?" for column in rec)
        cursor.execute(
            f"UPDATE service_records SET {assignments}, updated_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [*rec.values(), actor, record_id],
        )
        if pdf:
            cursor.execute(
                "UPDATE service_documents SET filename = ?, size_bytes = ?, data = ?, created_at = CURRENT_TIMESTAMP "
                "WHERE record_id = ?",
                (_pdf_filename(pdf_name), len(pdf), pdf, record_id),
            )
        # Only the bike's latest records decide its current state.
        _move_bike_on(cursor, current["vehicle_id"], record_id, rec)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    _record_change(["vehicles", "service_records", "customers", "due", "stats"], current["customer_id"])
    return (True, "Service record updated.")


def delete_service_record(record_id: int) -> tuple[bool, str]:
    """Delete a maintenance record and its job card PDF (admins). Its booking
    opens again (CONFIRMED), so the service is upcoming and can be closed
    again. If it was the bike's latest record, the bike goes back to how it
    was before the service was closed."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM service_records WHERE id = ?", (record_id,))
    current = cursor.fetchone()
    if not current:
        conn.close()
        return (False, "Service record not found.")
    vehicle_id = current["vehicle_id"]
    cursor.execute(
        "SELECT id FROM service_appointments WHERE vehicle_id = ? AND status = 'CONFIRMED' AND id != ?",
        (vehicle_id, current["appointment_id"] or ""),
    )
    other = cursor.fetchone()
    if other and current["appointment_id"]:
        conn.close()
        return (False, f"This bike already has booking {other['id']}. Cancel or close it first, then delete this service.")
    cursor.execute(
        "SELECT id FROM service_records WHERE vehicle_id = ? ORDER BY service_date DESC, id DESC LIMIT 1", (vehicle_id,)
    )
    latest = cursor.fetchone()["id"] == record_id
    # Undo only what this record decided: its readings if it's the latest
    # record, its schedule if it's the latest service (a repair has none).
    is_repair = (current["schedule"] or "set") == "keep"
    cursor.execute(
        "SELECT id FROM service_records WHERE vehicle_id = ? AND COALESCE(schedule, 'set') != 'keep' "
        "ORDER BY service_date DESC, id DESC LIMIT 1",
        (vehicle_id,),
    )
    row = cursor.fetchone()
    latest_service = not is_repair and row is not None and row["id"] == record_id
    try:
        cursor.execute("DELETE FROM service_documents WHERE record_id = ?", (record_id,))
        cursor.execute("DELETE FROM service_records WHERE id = ?", (record_id,))
        if current["appointment_id"]:
            cursor.execute(
                "UPDATE service_appointments SET status = 'CONFIRMED' WHERE id = ?", (current["appointment_id"],)
            )
        before = json.loads(current["bike_before"]) if current["bike_before"] else None
        if before and (latest or latest_service):
            columns = [c for c in BIKE_STATE if (c in READING_STATE and latest) or (c not in READING_STATE and latest_service)]
            assignments = ", ".join(f"{column} = ?" for column in columns)
            cursor.execute(
                f"UPDATE vehicles SET {assignments} WHERE id = ?",
                # Snapshots taken before the pending flag existed: not pending.
                [*(before.get(c, 0 if c == "next_due_pending" else None) for c in columns), vehicle_id],
            )
        elif not before and latest and not is_repair:
            # Closed before the bike's state was kept: the service it was
            # booked for is due again; the last service is the previous one.
            cursor.execute(
                "SELECT service_date FROM service_records WHERE vehicle_id = ? ORDER BY service_date DESC, id DESC LIMIT 1",
                (vehicle_id,),
            )
            previous = cursor.fetchone()
            cursor.execute(
                "SELECT max(appointment_date) AS day FROM service_appointments WHERE vehicle_id = ? AND status = 'COMPLETED'",
                (vehicle_id,),
            )
            days = [d for d in (previous and previous["service_date"], cursor.fetchone()["day"]) if d]
            cursor.execute("SELECT last_service_date FROM vehicles WHERE id = ?", (vehicle_id,))
            last = cursor.fetchone()["last_service_date"]
            if last == current["service_date"]:
                last = max(days) if days else None
            cursor.execute("SELECT appointment_date, service_type FROM service_appointments WHERE id = ?",
                           (current["appointment_id"] or "",))
            booking = cursor.fetchone()
            cursor.execute(
                "UPDATE vehicles SET last_service_date = ?, next_service_date = ?, next_service_type = ? WHERE id = ?",
                (last, booking["appointment_date"] if booking else current["service_date"],
                 booking["service_type"] if booking else current["service_type"], vehicle_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    _record_change(BOOKING_ENTITIES + ["vehicles", "service_records"], current["customer_id"])
    return (True, "Service deleted. Its booking is open again." if current["appointment_id"] else "Service deleted.")


def get_service_history(vehicle_id: int) -> list:
    """A bike's service history, newest first: maintenance records (job card
    fields, with the PDF's name and size) and, for older services without a
    record (e.g. the client's import), the completed booking itself (kind
    'booking')."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT r.*, d.filename AS document_name, d.size_bytes AS document_size
    FROM service_records r LEFT JOIN service_documents d ON d.record_id = r.id
    WHERE r.vehicle_id = ?
    """,
        (vehicle_id,),
    )
    records = [{"kind": "record", **_record_row(r)} for r in cursor.fetchall()]
    cursor.execute(
        """
    SELECT a.id AS appointment_id, a.appointment_date AS service_date, a.service_type, a.notes AS remarks
    FROM service_appointments a
    WHERE a.vehicle_id = ? AND a.status = 'COMPLETED'
      AND NOT EXISTS (SELECT 1 FROM service_records r WHERE r.appointment_id = a.id)
    """,
        (vehicle_id,),
    )
    older = [{"kind": "booking", **_plain(r)} for r in cursor.fetchall()]
    conn.close()
    return sorted(records + older, key=lambda r: (r["service_date"], r.get("id") or 0), reverse=True)


def list_service_records() -> list:
    """Every maintenance record with its customer and bike, newest first (no PDFs)."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT r.*, c.name AS customer_name, c.mobile_number, v.model, v.registration_number,
           d.filename AS document_name
    FROM service_records r
    JOIN customers c ON c.id = r.customer_id
    JOIN vehicles v ON v.id = r.vehicle_id
    LEFT JOIN service_documents d ON d.record_id = r.id
    ORDER BY r.service_date DESC, r.id DESC
    """
    )
    rows = [_record_row(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_service_document(record_id: int) -> dict | None:
    """The service report PDF of a record: {filename, content_type, data}."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT filename, content_type, data FROM service_documents WHERE record_id = ?", (record_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {"filename": row["filename"], "content_type": row["content_type"], "data": bytes(row["data"])}


# ---------------------------------------------------------------------------
# Search (previous and upcoming service bookings)
# ---------------------------------------------------------------------------
def _alnum(text: str | None) -> str:
    """Lowercase letters and digits only: 'TN 09 AB-1234' -> 'tn09ab1234'."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def _name_matches(query: str, name: str) -> bool:
    """Partial or fuzzy match, tolerant of spoken/transcribed spellings
    ('Karthick' finds 'Karthik Raja')."""
    if _alnum(query) in _alnum(name):
        return True
    return any(
        SequenceMatcher(None, _alnum(q), _alnum(n)).ratio() >= 0.8
        for q in query.split()
        for n in name.split()
        if _alnum(q) and _alnum(n)
    )


def same_registration(a: str | None, b: str | None) -> bool:
    return bool(_alnum(a)) and _alnum(a) == _alnum(b)


def same_mobile(a: str | None, b: str | None) -> bool:
    a, b = _digits(a)[-10:], _digits(b)[-10:]
    return len(a) == 10 and a == b


def appointment_state(row: dict) -> str:
    """upcoming / completed / cancelled / missed (date passed, never serviced)."""
    if row["status"] == "COMPLETED":
        return "completed"
    if row["status"] == "CANCELLED":
        return "cancelled"
    return "upcoming" if row["appointment_date"] >= today().isoformat() else "missed"


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
    limit: int | None = None,
) -> list:
    """Search service bookings (past and upcoming) across all customers.

    Every filter is optional and they combine with AND:
    - customer_name: partial / fuzzy ("karthick" finds "Karthik Raja")
    - mobile_number: any part of either of the customer's numbers ("3210" finds "9876543210")
    - registration_number: spacing/case ignored, partial ("1234", "TN09AB1234")
    - model: partial, punctuation ignored ("fzs" finds "Yamaha FZ-S Fi")
    - service_type: partial ("free" finds every free service)
    - state: upcoming, completed, cancelled or missed
    - date_from / date_to: appointment date range (YYYY-MM-DD, inclusive)
    - appointment_id: exact, case-insensitive

    Results are newest first, each with customer and bike details plus `state`.
    """
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT a.*, c.name AS customer_name, c.mobile_number, c.alternate_mobile, v.registration_number, v.model
    FROM service_appointments a
    JOIN customers c ON c.id = a.customer_id
    JOIN vehicles v ON v.id = a.vehicle_id
    ORDER BY a.appointment_date DESC, a.created_at DESC
    """)
    rows = [_plain(r) for r in cursor.fetchall()]
    conn.close()

    start, end = normalize_date(date_from), normalize_date(date_to)
    results = []
    for row in rows:
        row["state"] = appointment_state(row)
        if appointment_id and _alnum(appointment_id) != _alnum(row["id"]):
            continue
        if customer_name and not _name_matches(customer_name, row["customer_name"]):
            continue
        # A filter with nothing usable in it (e.g. a mobile search without
        # digits) matches nothing, not everything.
        if mobile_number and (
            not _digits(mobile_number)
            or not any(_digits(mobile_number) in _digits(n) for n in (row["mobile_number"], row["alternate_mobile"]))
        ):
            continue
        if registration_number and (
            not _alnum(registration_number) or _alnum(registration_number) not in _alnum(row["registration_number"])
        ):
            continue
        if model and _alnum(model) not in _alnum(row["model"]):
            continue
        if service_type and _alnum(service_type) not in _alnum(row["service_type"]):
            continue
        if state and state.strip().lower() != row["state"]:
            continue
        if start and row["appointment_date"] < start:
            continue
        if end and row["appointment_date"] > end:
            continue
        results.append(row)
    return results[:limit] if limit else results


# ---------------------------------------------------------------------------
# Call logs
# ---------------------------------------------------------------------------
def log_call(
    customer_id: int,
    outcome: str,
    vehicle_id: int | None = None,
    callback_date: str | None = None,
    notes: str | None = None,
    actor: str = "agent",
    appointment_id: str | None = None,
) -> tuple[bool, str]:
    """Record a call's outcome. `appointment_id`: the booking this call made
    (date_confirmed); cancelling that booking deletes this entry too."""
    outcome = (outcome or "").strip().lower()
    if outcome not in CALL_OUTCOMES:
        return (False, f"Unknown outcome '{outcome}'. Use one of: {', '.join(sorted(CALL_OUTCOMES))}.")
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO call_logs (customer_id, vehicle_id, outcome, callback_date, notes, appointment_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (customer_id, vehicle_id, outcome, normalize_date(callback_date) or callback_date, notes, appointment_id),
    )
    conn.commit()
    conn.close()
    _record_change(["calls", "customers", "stats"], customer_id)
    if actor == "agent" and outcome in OUTCOME_NOTIFICATIONS:
        level, title = OUTCOME_NOTIFICATIONS[outcome]
        detail = f"Call back on {_human_day(normalize_date(callback_date) or callback_date)}" if callback_date else (notes or "")
        notify(
            f"call_{outcome}", level, f"{title} · {_customer_name(customer_id)}", detail, customer_id,
            ref=f"call-booking:{appointment_id}" if appointment_id else None,
        )
    return (True, f"Call outcome '{outcome}' saved.")


def get_call_logs(
    customer_id: int | None = None, limit: int | None = None, since_utc: str | None = None
) -> list:
    """Call log, newest first. `since_utc` ('YYYY-MM-DD HH:MM:SS') and `limit`
    keep the big table's reads small."""
    conn = _connect()
    cursor = conn.cursor()
    clauses, params = [], []
    if customer_id:
        clauses.append("l.customer_id = ?")
        params.append(customer_id)
    if since_utc:
        clauses.append("l.created_at >= ?")
        params.append(since_utc)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cursor.execute(
        f"""
    SELECT l.*, c.name AS customer_name, c.mobile_number, v.registration_number, v.model
    FROM call_logs l
    JOIN customers c ON c.id = l.customer_id
    LEFT JOIN vehicles v ON v.id = l.vehicle_id
    {where}
    ORDER BY l.created_at DESC, l.id DESC
    {"LIMIT " + str(int(limit)) if limit else ""}
    """,
        tuple(params),
    )
    rows = [_plain(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def delete_call_log(call_id: int) -> tuple[bool, str]:
    """Delete one call log entry (admins, from the Call log)."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT customer_id FROM call_logs WHERE id = ?", (call_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return (False, "Call not found.")
    cursor.execute("DELETE FROM call_logs WHERE id = ?", (call_id,))
    conn.commit()
    conn.close()
    _record_change(["calls", "customers", "stats"], row["customer_id"])
    return (True, "Call deleted.")


def delete_call_logs(outcome: str | None = None) -> tuple[bool, str, int]:
    """Delete every call log entry, or only those with `outcome` (admins)."""
    if outcome is not None and outcome not in CALL_OUTCOMES:
        return (False, f"Unknown outcome '{outcome}'.", 0)
    conn = _connect()
    cursor = conn.cursor()
    if outcome:
        cursor.execute("DELETE FROM call_logs WHERE outcome = ?", (outcome,))
    else:
        cursor.execute("DELETE FROM call_logs")
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    _record_change(["calls", "customers", "stats"])
    return (True, f"{deleted} call{'s' if deleted != 1 else ''} deleted.", deleted)


def count_call_logs() -> int:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) AS n FROM call_logs")
    n = cursor.fetchone()["n"]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# CRM: customers, bikes and bookings managed by showroom staff
# ---------------------------------------------------------------------------
def _clean_mobile(mobile_number: str | None) -> str:
    return re.sub(r"[^0-9+]", "", (mobile_number or "").strip())


def list_customers(query: str | None = None) -> list:
    """Every customer with their bikes, next booking and last call (newest first)."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customers ORDER BY created_at DESC, id DESC")
    customers = [_plain(r) for r in cursor.fetchall()]
    cursor.execute("SELECT * FROM vehicles ORDER BY next_service_date")
    vehicles = [_plain(r) for r in cursor.fetchall()]
    cursor.execute(
        "SELECT * FROM service_appointments WHERE status = 'CONFIRMED' AND appointment_date >= ? "
        "ORDER BY appointment_date",
        (today().isoformat(),),
    )
    upcoming = [_plain(r) for r in cursor.fetchall()]
    # Only each customer's latest call (not the whole call history).
    cursor.execute(
        "SELECT * FROM call_logs WHERE id IN (SELECT max(id) FROM call_logs GROUP BY customer_id)"
    )
    last_calls = {r["customer_id"]: r for r in (_plain(row) for row in cursor.fetchall())}
    conn.close()

    bikes_by_customer: dict[int, list] = {}
    for v in vehicles:
        bikes_by_customer.setdefault(v["customer_id"], []).append(v)
    next_booking: dict[int, dict] = {}
    for a in upcoming:
        next_booking.setdefault(a["customer_id"], a)

    results = []
    for customer in customers:
        cid = customer["id"]
        customer["vehicles"] = bikes_by_customer.get(cid, [])
        customer["next_appointment"] = next_booking.get(cid)
        customer["last_call"] = last_calls.get(cid)
        if query:
            haystack = " ".join(
                [customer["name"], customer["mobile_number"], customer.get("alternate_mobile") or ""]
                + [f"{v['registration_number']} {v['model']}" for v in customer["vehicles"]]
            )
            if not (
                _name_matches(query, customer["name"])
                or _alnum(query) in _alnum(haystack)
                or (_digits(query) and _digits(query) in _digits(customer["mobile_number"]))
                or (_digits(query) and _digits(query) in _digits(customer.get("alternate_mobile")))
            ):
                continue
        results.append(customer)
    return results


def get_customer_detail(customer_id: int) -> dict | None:
    """A customer with bikes, every booking (with its state) and call history."""
    customer = get_customer(customer_id)
    if not customer:
        return None
    customer["appointments"] = [
        {**a, "state": appointment_state(a)}
        for a in sorted(
            get_appointments(customer_id, include_cancelled=True),
            key=lambda a: a["appointment_date"],
            reverse=True,
        )
    ]
    customer["calls"] = get_call_logs(customer_id)
    return customer


def _check_alternate(alternate: str | None, main: str) -> tuple[str | None, str | None]:
    """Cleaned alternate number (None = no second number), or an error."""
    alternate = _clean_mobile(alternate)
    if not alternate:
        return None, None
    if len(_digits(alternate)) < 10:
        return None, "Enter a 10-digit alternate number, or leave it empty."
    if same_mobile(alternate, main):
        return None, "The alternate number is the same as the main number."
    return alternate, None


def create_customer(
    name: str, mobile_number: str, alternate_mobile: str | None = None
) -> tuple[bool, str, int | None]:
    name, mobile_number = (name or "").strip(), _clean_mobile(mobile_number)
    if not name or len(_digits(mobile_number)) < 10:
        return (False, "A name and a 10-digit mobile number are required.", None)
    alternate_mobile, error = _check_alternate(alternate_mobile, mobile_number)
    if error:
        return (False, error, None)
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM customers WHERE mobile_number = ?", (mobile_number,))
    if cursor.fetchone():
        conn.close()
        return (False, f"A customer with mobile {mobile_number} already exists.", None)
    cursor.execute(
        "INSERT INTO customers (name, mobile_number, alternate_mobile) VALUES (?, ?, ?)",
        (name, mobile_number, alternate_mobile),
    )
    customer_id = cursor.lastrowid
    conn.commit()
    conn.close()
    _record_change(["customers", "stats"], customer_id)
    return (True, f"Customer {name} added.", customer_id)


def update_customer(
    customer_id: int,
    name: str | None = None,
    mobile_number: str | None = None,
    alternate_mobile: str | None = None,
) -> tuple[bool, str]:
    """Edit a customer. alternate_mobile="" removes the second number; sending
    both numbers swapped makes the alternate the main (called) number."""
    current = get_customer(customer_id)
    if not current:
        return (False, "Customer not found.")
    set_alternate = alternate_mobile is not None  # "" clears it
    if set_alternate:
        alternate_mobile, error = _check_alternate(alternate_mobile, mobile_number or current["mobile_number"])
        if error:
            return (False, error)
    elif mobile_number is not None and same_mobile(mobile_number, current.get("alternate_mobile")):
        return (False, "That number is already the alternate. Swap the two numbers instead.")
    conn = _connect()
    cursor = conn.cursor()
    if name is not None:
        if not name.strip():
            conn.close()
            return (False, "Name can't be empty.")
        cursor.execute("UPDATE customers SET name = ? WHERE id = ?", (name.strip(), customer_id))
    if mobile_number is not None:
        mobile_number = _clean_mobile(mobile_number)
        if len(_digits(mobile_number)) < 10:
            conn.close()
            return (False, "Enter a 10-digit mobile number.")
        cursor.execute(
            "SELECT id FROM customers WHERE mobile_number = ? AND id != ?", (mobile_number, customer_id)
        )
        if cursor.fetchone():
            conn.close()
            return (False, f"Another customer already uses {mobile_number}.")
        cursor.execute("UPDATE customers SET mobile_number = ? WHERE id = ?", (mobile_number, customer_id))
    if set_alternate:
        cursor.execute("UPDATE customers SET alternate_mobile = ? WHERE id = ?", (alternate_mobile, customer_id))
    conn.commit()
    conn.close()
    _record_change(CUSTOMER_ENTITIES, customer_id)
    return (True, "Customer updated.")


def delete_customer(customer_id: int) -> tuple[bool, str]:
    """Delete a customer and everything recorded for them (bikes, bookings, calls)."""
    if not get_customer(customer_id):
        return (False, "Customer not found.")
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM service_documents WHERE record_id IN (SELECT id FROM service_records WHERE customer_id = ?)",
        (customer_id,),
    )
    for table in ("service_records", "call_logs", "service_appointments", "vehicles"):
        cursor.execute(f"DELETE FROM {table} WHERE customer_id = ?", (customer_id,))
    cursor.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
    conn.commit()
    conn.close()
    _record_change(CUSTOMER_ENTITIES, customer_id)
    return (True, "Customer deleted.")


def list_vehicles() -> list:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT v.*, c.name AS customer_name, c.mobile_number
    FROM vehicles v JOIN customers c ON c.id = v.customer_id
    ORDER BY v.next_service_date
    """)
    rows = [_plain(r) for r in cursor.fetchall()]
    conn.close()
    return rows


VEHICLE_FIELDS = (
    "registration_number", "model", "purchase_date", "last_service_date",
    "odometer_km", "next_service_date", "next_service_type", "frame_number", "engine_number",
    "bought_elsewhere", "dealer_name",
)
# Sent as "" to clear them (e.g. purchase date not known).
CLEARABLE_VEHICLE_FIELDS = ("purchase_date", "dealer_name")


def update_vehicle(vehicle_id: int, **changes) -> tuple[bool, str]:
    changes = {k: v for k, v in changes.items() if k in VEHICLE_FIELDS and v is not None}
    for key in CLEARABLE_VEHICLE_FIELDS:
        if key in changes and not str(changes[key]).strip():
            changes[key] = None
    if "bought_elsewhere" in changes:
        changes["bought_elsewhere"] = 1 if changes["bought_elsewhere"] else 0
        if not changes["bought_elsewhere"]:
            changes["dealer_name"] = None
    if "dealer_name" in changes and changes["dealer_name"]:
        changes["dealer_name"] = changes["dealer_name"].strip()
    if not changes:
        return (False, "Nothing to change.")
    for key in ("purchase_date", "last_service_date", "next_service_date"):
        if key in changes and changes[key] is not None:
            day = normalize_date(str(changes[key]))
            if not day:
                return (False, f"Invalid date for {key.replace('_', ' ')}.")
            changes[key] = day
    if "registration_number" in changes:
        changes["registration_number"] = changes["registration_number"].strip().upper()
    if "model" in changes and not _tidy_model(changes["model"]):
        return (False, "Choose the bike model.")
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id, next_service_date FROM vehicles WHERE id = ?", (vehicle_id,))
    current = cursor.fetchone()
    if not current:
        conn.close()
        return (False, "Bike not found.")
    if "registration_number" in changes:
        cursor.execute(
            "SELECT id FROM vehicles WHERE registration_number = ? AND id != ?",
            (changes["registration_number"], vehicle_id),
        )
        if cursor.fetchone():
            conn.close()
            return (False, f"Bike {changes['registration_number']} already exists.")
    if "model" in changes:
        changes["model"] = _register_model(cursor, changes["model"])
    if changes.get("next_service_date") not in (None, current["next_service_date"]):
        changes["next_due_pending"] = 0  # a new date typed in by hand is a fixed date
    assignments = ", ".join(f"{key} = ?" for key in changes)
    cursor.execute(f"UPDATE vehicles SET {assignments} WHERE id = ?", (*changes.values(), vehicle_id))
    conn.commit()
    conn.close()
    _record_change(["vehicles", "customers", "due", "stats", "bike_models"])
    return (True, "Bike updated.")


def delete_vehicle(vehicle_id: int) -> tuple[bool, str]:
    """Delete a bike with its bookings and service records (and their PDFs); its
    call logs are kept without the bike link."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM vehicles WHERE id = ?", (vehicle_id,))
    if not cursor.fetchone():
        conn.close()
        return (False, "Bike not found.")
    cursor.execute(
        "DELETE FROM service_documents WHERE record_id IN (SELECT id FROM service_records WHERE vehicle_id = ?)",
        (vehicle_id,),
    )
    cursor.execute("DELETE FROM service_records WHERE vehicle_id = ?", (vehicle_id,))
    cursor.execute("DELETE FROM service_appointments WHERE vehicle_id = ?", (vehicle_id,))
    cursor.execute("UPDATE call_logs SET vehicle_id = NULL WHERE vehicle_id = ?", (vehicle_id,))
    cursor.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
    conn.commit()
    conn.close()
    _record_change(CUSTOMER_ENTITIES)
    return (True, "Bike deleted.")


def get_appointment(appointment_id: str) -> dict | None:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        """
    SELECT a.*, c.name AS customer_name, c.mobile_number, v.registration_number, v.model
    FROM service_appointments a
    JOIN customers c ON c.id = a.customer_id
    JOIN vehicles v ON v.id = a.vehicle_id
    WHERE UPPER(a.id) = UPPER(?)
    """,
        ((appointment_id or "").strip(),),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    row = _plain(row)
    row["state"] = appointment_state(row)
    return row


def _ist_day(timestamp: str) -> str:
    """'YYYY-MM-DD HH:MM:SS' stored in UTC -> the showroom (IST) date."""
    try:
        moment = datetime.strptime(timestamp[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    except (TypeError, ValueError):
        return (timestamp or "")[:10]
    return moment.astimezone(SHOWROOM_TZ).date().isoformat()


def _ist_day_start_utc(day: date) -> str:
    """Midnight IST of `day`, as the UTC timestamp format stored in created_at."""
    start = datetime.combine(day, datetime.min.time(), SHOWROOM_TZ).astimezone(ZoneInfo("UTC"))
    return start.strftime("%Y-%m-%d %H:%M:%S")


def get_dashboard_stats() -> dict:
    """Numbers and series for the CRM dashboard (dates in showroom time).

    Reads only what it shows: counts are SQL aggregates, calls are limited to
    the last 30 days, bookings to upcoming ones.
    """
    current = today()
    day = current.isoformat()
    month_start = current.replace(day=1).isoformat()
    week_ago = (current - timedelta(days=6)).isoformat()
    month_ago_day = current - timedelta(days=29)

    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) AS n FROM customers")
    customers = cursor.fetchone()["n"]
    cursor.execute("SELECT count(*) AS n FROM vehicles")
    vehicles = cursor.fetchone()["n"]
    cursor.execute(
        "SELECT count(*) AS n FROM service_appointments WHERE status = 'COMPLETED' AND appointment_date >= ?",
        (month_start,),
    )
    completed_this_month = cursor.fetchone()["n"]
    # Walk-in services (recorded without a booking) count too.
    cursor.execute(
        "SELECT count(*) AS n FROM service_records WHERE appointment_id IS NULL AND service_date >= ?",
        (month_start,),
    )
    completed_this_month += cursor.fetchone()["n"]
    cursor.execute(
        """
    SELECT a.*, c.name AS customer_name, c.mobile_number, v.registration_number, v.model
    FROM service_appointments a
    JOIN customers c ON c.id = a.customer_id
    JOIN vehicles v ON v.id = a.vehicle_id
    WHERE a.status = 'CONFIRMED' AND a.appointment_date >= ?
    ORDER BY a.appointment_date
    """,
        (day,),
    )
    upcoming = [_plain(r) for r in cursor.fetchall()]
    # Latest services done: job card records, plus completed bookings without
    # one (older services, e.g. from the client's import).
    cursor.execute(
        """
    SELECT r.id, r.appointment_id, r.customer_id, r.vehicle_id, r.service_date, r.service_type, r.job_type,
           r.job_card_no, r.amount, r.odometer_km, c.name AS customer_name, v.model, v.registration_number,
           'record' AS kind
    FROM service_records r
    JOIN customers c ON c.id = r.customer_id
    JOIN vehicles v ON v.id = r.vehicle_id
    ORDER BY r.service_date DESC, r.id DESC LIMIT 8
    """
    )
    done = [_plain(r) for r in cursor.fetchall()]
    cursor.execute(
        """
    SELECT a.id AS appointment_id, a.customer_id, a.vehicle_id, a.appointment_date AS service_date, a.service_type,
           c.name AS customer_name, v.model, v.registration_number, 'booking' AS kind
    FROM service_appointments a
    JOIN customers c ON c.id = a.customer_id
    JOIN vehicles v ON v.id = a.vehicle_id
    WHERE a.status = 'COMPLETED' AND NOT EXISTS (SELECT 1 FROM service_records r WHERE r.appointment_id = a.id)
    ORDER BY a.appointment_date DESC LIMIT 8
    """
    )
    done += [_plain(r) for r in cursor.fetchall()]
    done.sort(key=lambda r: (r["service_date"], r.get("id") or 0), reverse=True)
    conn.close()

    calls_30 = get_call_logs(since_utc=_ist_day_start_utc(month_ago_day))
    for call in calls_30:
        call["day"] = _ist_day(call["created_at"])
    due = get_due_services(within_days=7)
    outcomes = {outcome: 0 for outcome in sorted(CALL_OUTCOMES)}
    for call in calls_30:
        outcomes[call["outcome"]] = outcomes.get(call["outcome"], 0) + 1
    confirmed = outcomes.get("date_confirmed", 0)
    upcoming_by_day: dict[str, int] = {}
    for a in upcoming:
        upcoming_by_day[a["appointment_date"]] = upcoming_by_day.get(a["appointment_date"], 0) + 1
    calls_by_day: dict[str, list[int]] = {}
    for c in calls_30:
        totals = calls_by_day.setdefault(c["day"], [0, 0])
        totals[0] += 1
        totals[1] += c["outcome"] == "date_confirmed"

    return {
        "today": day,
        "totals": {
            "customers": customers,
            "vehicles": vehicles,
            "due_this_week": len(due),
            "overdue": sum(1 for d in due if d["is_overdue"]),  # past each type's overdue days
            "upcoming_appointments": len(upcoming),
            "appointments_today": upcoming_by_day.get(day, 0),
            "completed_this_month": completed_this_month,
            "calls_this_week": sum(1 for c in calls_30 if c["day"] >= week_ago),
            "confirmation_rate": round(100 * confirmed / len(calls_30)) if calls_30 else 0,
        },
        "call_outcomes_30d": [{"outcome": k, "count": v} for k, v in outcomes.items()],
        "appointments_next_14_days": [
            {"date": d, "count": upcoming_by_day.get(d, 0)}
            for d in ((current + timedelta(days=i)).isoformat() for i in range(14))
        ],
        "calls_last_14_days": [
            {"date": d, "total": calls_by_day.get(d, [0, 0])[0], "confirmed": calls_by_day.get(d, [0, 0])[1]}
            for d in ((current - timedelta(days=13 - i)).isoformat() for i in range(14))
        ],
        "todays_appointments": [a for a in upcoming if a["appointment_date"] == day],
        "due_services": due[:8],
        "upcoming_list": [a for a in upcoming if a["appointment_date"] > day][:10],  # after today
        "recent_services": done[:8],
        "recent_calls": calls_30[:6] if len(calls_30) >= 6 else get_call_logs(limit=6),
    }


# ---------------------------------------------------------------------------
# Charts with a date range (dashboard, Bikes page)
# ---------------------------------------------------------------------------
MAX_CHART_DAYS = 366


def _chart_range(start: str | None, end: str | None) -> tuple[date, date]:
    """Inclusive showroom-date range from 'YYYY-MM-DD' strings; at most a year."""
    first, last = normalize_date(start), normalize_date(end)
    if not first or not last:
        raise ValueError("Give the range as start and end dates (YYYY-MM-DD).")
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    if b < a:
        raise ValueError("The range ends before it starts.")
    if (b - a).days + 1 > MAX_CHART_DAYS:
        raise ValueError(f"Pick a range of at most {MAX_CHART_DAYS} days.")
    return a, b


def _days(a: date, b: date) -> list[str]:
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def chart_bookings(start: str, end: str) -> list:
    """Bookings per day (upcoming and serviced) between start and end."""
    a, b = _chart_range(start, end)
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT appointment_date AS day, count(*) AS n FROM service_appointments "
        "WHERE status IN ('CONFIRMED', 'COMPLETED') AND appointment_date BETWEEN ? AND ? GROUP BY appointment_date",
        (a.isoformat(), b.isoformat()),
    )
    counts = {r["day"]: r["n"] for r in cursor.fetchall()}
    conn.close()
    return [{"date": d, "count": counts.get(d, 0)} for d in _days(a, b)]


def chart_calls(start: str, end: str) -> dict:
    """Calls between start and end (showroom dates): per day (all / date
    confirmed), by outcome, and the total."""
    a, b = _chart_range(start, end)
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT created_at, outcome FROM call_logs WHERE created_at >= ? AND created_at < ?",
        (_ist_day_start_utc(a), _ist_day_start_utc(b + timedelta(days=1))),
    )
    calls = [(_ist_day(r["created_at"]), r["outcome"]) for r in cursor.fetchall()]
    conn.close()
    per_day: dict[str, list[int]] = {}
    outcomes = {outcome: 0 for outcome in sorted(CALL_OUTCOMES)}
    for day, outcome in calls:
        totals = per_day.setdefault(day, [0, 0])
        totals[0] += 1
        totals[1] += outcome == "date_confirmed"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {
        "per_day": [{"date": d, "total": per_day.get(d, [0, 0])[0], "confirmed": per_day.get(d, [0, 0])[1]} for d in _days(a, b)],
        "outcomes": [{"outcome": k, "count": v} for k, v in outcomes.items()],
        "total": len(calls),
    }


def chart_bike_models(start: str | None = None, end: str | None = None) -> list:
    """Per model: bikes, customers and bought-elsewhere among bikes added to the
    CRM between start and end (no range: every bike). Most customers first."""
    where, params = "", ()
    if start or end:
        a, b = _chart_range(start, end)
        where = "WHERE v.created_at >= ? AND v.created_at < ?"
        params = (_ist_day_start_utc(a), _ist_day_start_utc(b + timedelta(days=1)))
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        f"""
    SELECT COALESCE(m.name, v.model) AS name, count(*) AS bikes, count(DISTINCT v.customer_id) AS customers,
           sum(CASE WHEN COALESCE(v.bought_elsewhere, 0) = 1 THEN 1 ELSE 0 END) AS bought_elsewhere
    FROM vehicles v LEFT JOIN bike_models m ON LOWER(m.name) = LOWER(v.model)
    {where}
    GROUP BY COALESCE(m.name, v.model)
    """,
        params,
    )
    rows = [{**_plain(r), "bought_elsewhere": int(r["bought_elsewhere"] or 0)} for r in cursor.fetchall()]
    conn.close()
    return sorted(rows, key=lambda r: (-r["customers"], -r["bikes"], r["name"].lower()))


# ---------------------------------------------------------------------------
# Service types (managed from the CRM)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Bike models (Bikes page): the bike form's model list
# ---------------------------------------------------------------------------
def _tidy_model(name: str | None) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


def _register_model(cursor, name: str) -> str:
    """The model's catalog name (any case/spacing). A model not in the catalog
    yet (the agent, the Excel import, older forms) is added, so the list
    always covers every bike."""
    name = _tidy_model(name)
    cursor.execute("SELECT name FROM bike_models WHERE LOWER(name) = LOWER(?)", (name,))
    row = cursor.fetchone()
    if row:
        return row["name"]
    cursor.execute("INSERT INTO bike_models (name) VALUES (?)", (name,))
    return name  # callers record the change (with "bike_models") after committing


def list_bike_models() -> list:
    """Every model with how many bikes and customers have it, how many are due
    this week, bought elsewhere and serviced (most customers first)."""
    week = (today() + timedelta(days=7)).isoformat()
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bike_models")
    models = [_plain(r) for r in cursor.fetchall()]
    cursor.execute(
        """
    SELECT LOWER(v.model) AS model, count(*) AS bikes, count(DISTINCT v.customer_id) AS customers,
           sum(CASE WHEN v.next_service_date <= ? AND COALESCE(v.next_due_pending, 0) = 0 THEN 1 ELSE 0 END) AS due_soon,
           sum(CASE WHEN COALESCE(v.bought_elsewhere, 0) = 1 THEN 1 ELSE 0 END) AS bought_elsewhere
    FROM vehicles v GROUP BY LOWER(v.model)
    """,
        (week,),
    )
    stats = {r["model"]: _plain(r) for r in cursor.fetchall()}
    cursor.execute(
        "SELECT LOWER(v.model) AS model, count(*) AS n FROM service_records r JOIN vehicles v ON v.id = r.vehicle_id GROUP BY LOWER(v.model)"
    )
    services = {r["model"]: r["n"] for r in cursor.fetchall()}
    conn.close()
    for m in models:
        st = stats.get(m["name"].lower(), {})
        m.update(
            bikes=st.get("bikes", 0), customers=st.get("customers", 0), due_soon=int(st.get("due_soon") or 0),
            bought_elsewhere=int(st.get("bought_elsewhere") or 0), services=services.get(m["name"].lower(), 0),
        )
    return sorted(models, key=lambda m: (-m["customers"], -m["bikes"], m["name"].lower()))


def create_bike_model(name: str) -> tuple[bool, str, int | None]:
    name = _tidy_model(name)
    if not name:
        return (False, "Enter the model's name.", None)
    if len(name) > 80:
        return (False, "Keep the name under 80 characters.", None)
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM bike_models WHERE LOWER(name) = LOWER(?)", (name,))
    row = cursor.fetchone()
    if row:
        conn.close()
        return (False, f"{row['name']} is already on the list.", None)
    cursor.execute("INSERT INTO bike_models (name) VALUES (?)", (name,))
    model_id = cursor.lastrowid
    conn.commit()
    conn.close()
    _record_change(["bike_models"])
    return (True, f"{name} added.", model_id)


def update_bike_model(model_id: int, name: str) -> tuple[bool, str]:
    """Rename a model; its bikes are renamed with it."""
    name = _tidy_model(name)
    if not name:
        return (False, "Enter the model's name.")
    if len(name) > 80:
        return (False, "Keep the name under 80 characters.")
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM bike_models WHERE id = ?", (model_id,))
    current = cursor.fetchone()
    if not current:
        conn.close()
        return (False, "Bike model not found.")
    cursor.execute("SELECT id FROM bike_models WHERE LOWER(name) = LOWER(?) AND id != ?", (name, model_id))
    if cursor.fetchone():
        conn.close()
        return (False, f"{name} is already on the list.")
    cursor.execute("UPDATE bike_models SET name = ? WHERE id = ?", (name, model_id))
    cursor.execute("UPDATE vehicles SET model = ? WHERE LOWER(model) = LOWER(?)", (name, current["name"]))
    conn.commit()
    conn.close()
    _record_change(["bike_models", "vehicles", "customers"])
    return (True, f"Renamed to {name}.")


def delete_bike_model(model_id: int) -> tuple[bool, str]:
    """Delete a model no bike uses."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM bike_models WHERE id = ?", (model_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return (False, "Bike model not found.")
    cursor.execute("SELECT count(*) AS n FROM vehicles WHERE LOWER(model) = LOWER(?)", (row["name"],))
    bikes = cursor.fetchone()["n"]
    if bikes:
        conn.close()
        return (False, f"{bikes} bike(s) are {row['name']}. Change their model first, then delete it.")
    cursor.execute("DELETE FROM bike_models WHERE id = ?", (model_id,))
    conn.commit()
    conn.close()
    _record_change(["bike_models"])
    return (True, f"{row['name']} deleted.")


# ---------------------------------------------------------------------------
# Our staff (the showroom's people, not CRM logins)
# ---------------------------------------------------------------------------
def list_showroom_staff() -> list:
    """Everyone, by category (in the categories' order) then name."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM showroom_staff ORDER BY name")
    rows = [_plain(r) for r in cursor.fetchall()]
    cursor.execute("SELECT name FROM staff_categories ORDER BY sort_order, id")
    order = {r["name"]: i for i, r in enumerate(cursor.fetchall())}
    conn.close()
    for row in rows:
        row["active"] = bool(row["active"])
    return sorted(rows, key=lambda r: (order.get(r["category"], 999), r["name"].lower()))


def _staff_category(cursor, name: str | None) -> str | None:
    """The category's saved name (any case), or None if there's no such category."""
    cursor.execute("SELECT name FROM staff_categories WHERE LOWER(name) = LOWER(?)", ((name or "").strip(),))
    row = cursor.fetchone()
    return row["name"] if row else None


def _clean_showroom_staff(fields: dict) -> tuple[dict, str | None]:
    clean = {k: v for k, v in fields.items() if k in ("name", "category", "mobile_number", "active")}
    if "name" in clean:
        clean["name"] = re.sub(r"\s+", " ", clean["name"] or "").strip()
        if not clean["name"]:
            return clean, "Enter the name."
        if len(clean["name"]) > 80:
            return clean, "Keep the name under 80 characters."
    if "mobile_number" in clean:
        digits = _digits(clean["mobile_number"])[-10:] if clean["mobile_number"] else ""
        if clean["mobile_number"] and len(digits) != 10:
            return clean, "Enter a 10-digit mobile number, or leave it empty."
        clean["mobile_number"] = digits or None
    if "active" in clean:
        clean["active"] = 1 if clean["active"] else 0
    return clean, None


def _staff_name_taken(cursor, name: str, category: str, other_id: int | None = None) -> bool:
    cursor.execute(
        "SELECT id FROM showroom_staff WHERE LOWER(name) = LOWER(?) AND category = ? AND id != ?",
        (name, category, other_id or 0),
    )
    return cursor.fetchone() is not None


def create_showroom_staff(**fields) -> tuple[bool, str, int | None]:
    clean, error = _clean_showroom_staff({"active": True, **fields})
    if error or "name" not in clean or "category" not in clean:
        return (False, error or "Enter the name and category.", None)
    conn = _connect()
    cursor = conn.cursor()
    clean["category"] = _staff_category(cursor, clean["category"])
    if not clean["category"]:
        conn.close()
        return (False, f"No category called {fields.get('category')}. An admin can add it.", None)
    if _staff_name_taken(cursor, clean["name"], clean["category"]):
        conn.close()
        return (False, f"{clean['name']} is already in {clean['category']}.", None)
    cursor.execute(
        f"INSERT INTO showroom_staff ({', '.join(clean)}) VALUES ({', '.join('?' for _ in clean)})",
        tuple(clean.values()),
    )
    staff_id = cursor.lastrowid
    conn.commit()
    conn.close()
    _record_change(["showroom_staff"])
    return (True, f"{clean['name']} added to {clean['category']}.", staff_id)


def update_showroom_staff(staff_id: int, **fields) -> tuple[bool, str]:
    clean, error = _clean_showroom_staff({k: v for k, v in fields.items() if v is not None or k == "mobile_number"})
    if error:
        return (False, error)
    if not clean:
        return (False, "Nothing to change.")
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM showroom_staff WHERE id = ?", (staff_id,))
    current = cursor.fetchone()
    if not current:
        conn.close()
        return (False, "Staff member not found.")
    if "category" in clean:
        clean["category"] = _staff_category(cursor, clean["category"])
        if not clean["category"]:
            conn.close()
            return (False, f"No category called {fields.get('category')}. An admin can add it.")
    name, category = clean.get("name", current["name"]), clean.get("category", current["category"])
    if _staff_name_taken(cursor, name, category, staff_id):
        conn.close()
        return (False, f"{name} is already in {category}.")
    cursor.execute(
        f"UPDATE showroom_staff SET {', '.join(f'{k} = ?' for k in clean)} WHERE id = ?", (*clean.values(), staff_id)
    )
    conn.commit()
    conn.close()
    _record_change(["showroom_staff"])
    return (True, f"{name} updated.")


def delete_showroom_staff(staff_id: int) -> tuple[bool, str]:
    """Remove someone from the list. Job cards keep the names written on them."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM showroom_staff WHERE id = ?", (staff_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return (False, "Staff member not found.")
    cursor.execute("DELETE FROM showroom_staff WHERE id = ?", (staff_id,))
    conn.commit()
    conn.close()
    _record_change(["showroom_staff"])
    return (True, f"{row['name']} removed.")


DEFAULT_STAFF_CATEGORIES = [("Sales", 0), ("Spares", 0), ("Mechanic", 1)]


def list_staff_categories() -> list:
    """Our staff's categories in order, each with how many people are in it."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM staff_categories ORDER BY sort_order, id")
    rows = [_plain(r) for r in cursor.fetchall()]
    cursor.execute("SELECT category, count(*) AS n FROM showroom_staff GROUP BY category")
    counts = {r["category"]: r["n"] for r in cursor.fetchall()}
    conn.close()
    for row in rows:
        row["is_mechanic"] = bool(row["is_mechanic"])
        row["staff_count"] = counts.get(row["name"], 0)
    return rows


def _clean_category_name(name: str | None) -> tuple[str, str | None]:
    name = re.sub(r"\s+", " ", name or "").strip()
    if not name:
        return name, "Enter the category's name."
    if len(name) > 40:
        return name, "Keep the name under 40 characters."
    return name, None


def create_staff_category(name: str, is_mechanic: bool = False) -> tuple[bool, str, int | None]:
    name, error = _clean_category_name(name)
    if error:
        return (False, error, None)
    conn = _connect()
    cursor = conn.cursor()
    if _staff_category(cursor, name):
        conn.close()
        return (False, f"There's already a category called {name}.", None)
    cursor.execute("SELECT max(sort_order) AS n FROM staff_categories")
    order = (cursor.fetchone()["n"] or 0) + 1
    cursor.execute(
        "INSERT INTO staff_categories (name, is_mechanic, sort_order) VALUES (?, ?, ?)",
        (name, 1 if is_mechanic else 0, order),
    )
    category_id = cursor.lastrowid
    conn.commit()
    conn.close()
    _record_change(["staff_categories", "showroom_staff"])
    return (True, f"Category {name} added.", category_id)


def update_staff_category(category_id: int, name: str | None = None, is_mechanic: bool | None = None) -> tuple[bool, str]:
    """Rename (its staff move with it) or switch whether it's the job card's mechanics."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM staff_categories WHERE id = ?", (category_id,))
    current = cursor.fetchone()
    if not current:
        conn.close()
        return (False, "Category not found.")
    changes = {}
    if name is not None:
        name, error = _clean_category_name(name)
        if error:
            conn.close()
            return (False, error)
        cursor.execute("SELECT id FROM staff_categories WHERE LOWER(name) = LOWER(?) AND id != ?", (name, category_id))
        if cursor.fetchone():
            conn.close()
            return (False, f"There's already a category called {name}.")
        changes["name"] = name
    if is_mechanic is not None:
        changes["is_mechanic"] = 1 if is_mechanic else 0
    if not changes:
        conn.close()
        return (False, "Nothing to change.")
    cursor.execute(
        f"UPDATE staff_categories SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?", (*changes.values(), category_id)
    )
    if "name" in changes and changes["name"] != current["name"]:
        cursor.execute("UPDATE showroom_staff SET category = ? WHERE category = ?", (changes["name"], current["name"]))
    conn.commit()
    conn.close()
    _record_change(["staff_categories", "showroom_staff"])
    return (True, f"Category {changes.get('name', current['name'])} updated.")


def delete_staff_category(category_id: int) -> tuple[bool, str]:
    """Delete an empty category (move or remove its people first)."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM staff_categories WHERE id = ?", (category_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return (False, "Category not found.")
    cursor.execute("SELECT count(*) AS n FROM showroom_staff WHERE category = ?", (row["name"],))
    people = cursor.fetchone()["n"]
    if people:
        conn.close()
        return (False, f"{row['name']} still has {people} staff. Move them to another category (or remove them) first.")
    cursor.execute("DELETE FROM staff_categories WHERE id = ?", (category_id,))
    conn.commit()
    conn.close()
    _record_change(["staff_categories", "showroom_staff"])
    return (True, f"Category {row['name']} deleted.")


SERVICE_TYPE_FIELDS = (
    "name", "description", "is_free", "interval_days", "interval_km", "price", "overdue_days", "active",
    "is_repair",
)


def _service_type_row(row) -> dict:
    row = _plain(row)
    row["is_free"] = bool(row["is_free"])
    row["active"] = bool(row["active"])
    row["is_repair"] = bool(row.get("is_repair"))
    return row


def list_service_types(active_only: bool = False) -> list:
    """Service types in schedule order, each with how many bikes have it due
    next and how many bookings use it."""
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM service_types ORDER BY sort_order, id")
    types = [_service_type_row(r) for r in cursor.fetchall()]
    cursor.execute("SELECT next_service_type AS name, count(*) AS n FROM vehicles GROUP BY next_service_type")
    bikes = {r["name"]: r["n"] for r in cursor.fetchall()}
    cursor.execute("SELECT service_type AS name, count(*) AS n FROM service_appointments GROUP BY service_type")
    bookings = {r["name"]: r["n"] for r in cursor.fetchall()}
    conn.close()
    for t in types:
        t["bikes_due"] = bikes.get(t["name"], 0)
        t["bookings"] = bookings.get(t["name"], 0)
    return [t for t in types if t["active"]] if active_only else types


def _clean_service_type(fields: dict) -> tuple[dict, str | None]:
    clean = {k: v for k, v in fields.items() if k in SERVICE_TYPE_FIELDS}
    if "name" in clean:
        clean["name"] = re.sub(r"\s+", " ", (clean["name"] or "")).strip()
        if not clean["name"]:
            return clean, "Enter a name."
        if len(clean["name"]) > 80:
            return clean, "Keep the name under 80 characters."
    for key in ("interval_days", "interval_km", "price", "overdue_days"):
        if key in clean and clean[key] is not None:
            try:
                clean[key] = int(clean[key])
            except (TypeError, ValueError):
                return clean, f"{key.replace('_', ' ').capitalize()} must be a whole number."
            if clean[key] < 0 or (key == "interval_days" and clean[key] == 0):
                return clean, f"{key.replace('_', ' ').capitalize()} must be more than 0."
    for key in ("is_free", "active", "is_repair"):
        if key in clean:
            clean[key] = 1 if clean[key] else 0
    if clean.get("is_repair"):
        # A repair is never on the schedule (so never followed up).
        clean["interval_days"] = clean["interval_km"] = None
    if "description" in clean:
        clean["description"] = (clean["description"] or "").strip() or None
    return clean, None


def create_service_type(**fields) -> tuple[bool, str, int | None]:
    clean, error = _clean_service_type({"is_free": False, "active": True, **fields})
    if error or "name" not in clean:
        return (False, error or "Enter a name.", None)
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM service_types WHERE LOWER(name) = LOWER(?)", (clean["name"],))
    if cursor.fetchone():
        conn.close()
        return (False, f"A service type called {clean['name']} already exists.", None)
    cursor.execute("SELECT max(sort_order) AS n FROM service_types")
    clean["sort_order"] = (cursor.fetchone()["n"] or 0) + 1
    columns = ", ".join(clean)
    cursor.execute(
        f"INSERT INTO service_types ({columns}) VALUES ({', '.join('?' for _ in clean)})", tuple(clean.values())
    )
    type_id = cursor.lastrowid
    conn.commit()
    conn.close()
    _record_change(["service_types"])
    return (True, f"Service type {clean['name']} added.", type_id)


def update_service_type(type_id: int, **fields) -> tuple[bool, str]:
    """Edit a service type. Renaming also renames it on bikes and bookings."""
    clean, error = _clean_service_type(fields)
    if error:
        return (False, error)
    if not clean:
        return (False, "Nothing to change.")
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM service_types WHERE id = ?", (type_id,))
    current = cursor.fetchone()
    if not current:
        conn.close()
        return (False, "Service type not found.")
    old_name = current["name"]
    if "name" in clean and clean["name"] != old_name:
        cursor.execute(
            "SELECT id FROM service_types WHERE LOWER(name) = LOWER(?) AND id != ?", (clean["name"], type_id)
        )
        if cursor.fetchone():
            conn.close()
            return (False, f"A service type called {clean['name']} already exists.")
        cursor.execute("UPDATE vehicles SET next_service_type = ? WHERE next_service_type = ?", (clean["name"], old_name))
        cursor.execute("UPDATE service_appointments SET service_type = ? WHERE service_type = ?", (clean["name"], old_name))
    assignments = ", ".join(f"{key} = ?" for key in clean)
    cursor.execute(f"UPDATE service_types SET {assignments} WHERE id = ?", (*clean.values(), type_id))
    conn.commit()
    conn.close()
    _record_change(["service_types", "vehicles", "appointments", "customers", "due"])
    return (True, "Service type updated.")


def delete_service_type(type_id: int) -> tuple[bool, str]:
    """Delete a service type nothing uses yet; used ones can be deactivated."""
    types = {t["id"]: t for t in list_service_types()}
    service_type = types.get(type_id)
    if not service_type:
        return (False, "Service type not found.")
    if service_type["bikes_due"] or service_type["bookings"]:
        return (
            False,
            f"{service_type['name']} is used by {service_type['bikes_due']} bike(s) and "
            f"{service_type['bookings']} booking(s). Deactivate it instead.",
        )
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM service_types WHERE id = ?", (type_id,))
    conn.commit()
    conn.close()
    _record_change(["service_types"])
    return (True, f"Service type {service_type['name']} deleted.")


def move_service_type(type_id: int, direction: str) -> tuple[bool, str]:
    """Move a type up or down in the schedule order."""
    order = [t["id"] for t in list_service_types()]
    if type_id not in order:
        return (False, "Service type not found.")
    i = order.index(type_id)
    j = i - 1 if direction == "up" else i + 1
    if j < 0 or j >= len(order):
        return (True, "Already at the edge.")
    order[i], order[j] = order[j], order[i]
    conn = _connect()
    cursor = conn.cursor()
    for position, tid in enumerate(order, start=1):
        cursor.execute("UPDATE service_types SET sort_order = ? WHERE id = ?", (position, tid))
    conn.commit()
    conn.close()
    _record_change(["service_types"])
    return (True, "Order updated.")


# ---------------------------------------------------------------------------
# Staff logins (CRM dashboard)
# ---------------------------------------------------------------------------
STAFF_ROLES = {"admin", "staff"}


def _public_user(row) -> dict:
    user = _plain(row)
    user.pop("password_hash", None)
    return user


def create_staff_user(
    username: str, password_hash: str, full_name: str | None = None, role: str = "staff"
) -> tuple[bool, str, int | None]:
    username = (username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        return (False, "Username: 3-40 letters, numbers, dots, dashes or underscores.", None)
    if role not in STAFF_ROLES:
        return (False, f"Role must be one of: {', '.join(sorted(STAFF_ROLES))}.", None)
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM staff_users WHERE username = ?", (username,))
    if cursor.fetchone():
        conn.close()
        return (False, f"User {username} already exists.", None)
    cursor.execute(
        "INSERT INTO staff_users (username, full_name, password_hash, role) VALUES (?, ?, ?, ?)",
        (username, (full_name or "").strip() or None, password_hash, role),
    )
    user_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return (True, f"User {username} created.", user_id)


def get_staff_user(username: str | None = None, user_id: int | None = None, with_hash: bool = False) -> dict | None:
    conn = _connect()
    cursor = conn.cursor()
    if user_id is not None:
        cursor.execute("SELECT * FROM staff_users WHERE id = ?", (user_id,))
    else:
        cursor.execute("SELECT * FROM staff_users WHERE username = ?", ((username or "").strip().lower(),))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return _plain(row) if with_hash else _public_user(row)


def list_staff_users() -> list:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM staff_users ORDER BY created_at, id")
    rows = [_public_user(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def count_staff_users() -> int:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT count(*) AS n FROM staff_users")
    n = cursor.fetchone()["n"]
    conn.close()
    return n


def set_staff_password(user_id: int, password_hash: str) -> None:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("UPDATE staff_users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    conn.commit()
    conn.close()


def delete_staff_user(user_id: int) -> tuple[bool, str]:
    user = get_staff_user(user_id=user_id)
    if not user:
        return (False, "User not found.")
    if user["role"] == "admin":
        admins = [u for u in list_staff_users() if u["role"] == "admin"]
        if len(admins) <= 1:
            return (False, "Can't delete the last admin.")
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM staff_users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return (True, f"User {user['username']} deleted.")


# ---------------------------------------------------------------------------
# Live events (CRM WebSocket feed), notifications and voice-call tracking
# ---------------------------------------------------------------------------
# Entities the CRM refreshes when they change.
BOOKING_ENTITIES = ["appointments", "customers", "due", "stats"]
CUSTOMER_ENTITIES = ["customers", "vehicles", "appointments", "calls", "due", "stats"]

# Agent call outcomes worth telling staff about: outcome -> (level, title).
# date_confirmed is covered by the "Service booked" notification.
OUTCOME_NOTIFICATIONS = {
    "callback_requested": ("warning", "Call back requested"),
    "not_interested": ("error", "Not interested"),
    "already_serviced": ("info", "Already serviced elsewhere"),
    "bike_sold": ("info", "Bike sold"),
    "wrong_number": ("warning", "Wrong number"),
    "no_answer": ("warning", "No answer"),
}


def emit_event(event_type: str, payload: dict, ref: str | None = None) -> int | None:
    """Append an event for the CRM's live feed. Never raises: live updates
    must not break a booking or a call."""
    try:
        conn = _connect()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO events (type, event_ref, payload) VALUES (?, ?, ?)",
            (event_type, ref, json.dumps(payload, default=str)),
        )
        event_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return event_id
    except Exception:  # noqa: BLE001
        logger.exception("Could not record event %s", event_type)
        return None


def _record_change(entities: list[str], customer_id: int | None = None) -> None:
    emit_event("data.changed", {"entities": entities, "customer_id": customer_id})


def backup_bytes() -> bytes:
    """A consistent copy of the SQLite database (safe while the bot and API
    write: SQLite's online backup). For MySQL, use the host's own backups."""
    if using_mysql():
        raise RuntimeError("Database backups from the CRM are for SQLite only.")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "backup.db")
        source, target = sqlite3.connect(DB_FILE), sqlite3.connect(path)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        with open(path, "rb") as f:
            return f.read()


# ---------------------------------------------------------------------------
# Settings (showroom-wide, changed by admins in the CRM)
# ---------------------------------------------------------------------------
SETTING_DEFAULTS = {"voice_agent_enabled": "1"}


def _get_setting(name: str) -> str:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM app_settings WHERE name = ?", (name,))
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row else SETTING_DEFAULTS[name]


def voice_agent_enabled() -> bool:
    """False when an admin has switched the voice agent off: no calls start."""
    return _get_setting("voice_agent_enabled") == "1"


def get_settings() -> dict:
    return {"voice_agent_enabled": voice_agent_enabled()}


def set_voice_agent_enabled(enabled: bool, actor: str | None = None) -> tuple[bool, str]:
    value = "1" if enabled else "0"
    conn = _connect()
    cursor = conn.cursor()
    # Check, then update or insert (MySQL's rowcount is 0 for an unchanged value).
    cursor.execute("SELECT 1 FROM app_settings WHERE name = ?", ("voice_agent_enabled",))
    if cursor.fetchone():
        cursor.execute(
            "UPDATE app_settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE name = ?", (value, "voice_agent_enabled")
        )
    else:
        cursor.execute("INSERT INTO app_settings (name, value) VALUES (?, ?)", ("voice_agent_enabled", value))
    conn.commit()
    conn.close()
    _record_change(["settings"])
    state = "on" if enabled else "off"
    notify("settings", "info" if enabled else "warning", f"Voice agent turned {state}",
           f"By {actor}." if actor else "")
    return (True, f"Voice agent turned {state}.")


def notify(
    kind: str,
    level: str,
    title: str,
    body: str = "",
    customer_id: int | None = None,
    ref: str | None = None,
) -> int | None:
    """A staff notification (level: info, success, warning, error). With `ref`,
    the same notification is only ever created once."""
    if ref and event_ref_exists(ref):
        return None
    return emit_event(
        "notification",
        {"kind": kind, "level": level, "title": title, "body": body, "customer_id": customer_id},
        ref=ref,
    )


def event_ref_exists(ref: str) -> bool:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM events WHERE event_ref = ?", (ref,))
    found = cursor.fetchone() is not None
    conn.close()
    return found


def _event_row(row) -> dict:
    row = _plain(row)
    return {"id": row["id"], "type": row["type"], "data": json.loads(row["payload"]), "at": row["created_at"]}


def latest_event_id() -> int:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT max(id) AS n FROM events")
    n = cursor.fetchone()["n"]
    conn.close()
    return n or 0


def get_events_after(event_id: int, limit: int = 200) -> list:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (event_id, limit))
    rows = [_event_row(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def recent_notifications(limit: int = 30) -> list:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM events WHERE type = 'notification' ORDER BY id DESC LIMIT ?", (limit,))
    rows = [_event_row(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def prune_events(keep_days: int = 2, keep_notification_days: int = 60) -> None:
    """Drop old feed rows (notifications are kept longer)."""
    now_utc = datetime.now(ZoneInfo("UTC"))
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM events WHERE type != 'notification' AND created_at < ?",
        ((now_utc - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    cursor.execute(
        "DELETE FROM events WHERE type = 'notification' AND created_at < ?",
        ((now_utc - timedelta(days=keep_notification_days)).strftime("%Y-%m-%d %H:%M:%S"),),
    )
    conn.commit()
    conn.close()


MISSED_LOOKBACK_DAYS = 7


def flag_missed_appointments() -> int:
    """Notify once for every booking missed in the last MISSED_LOOKBACK_DAYS
    (date passed, not serviced). Older misses aren't notified, so a first run
    on an existing database doesn't flood the bell."""
    created = 0
    window_start = (today() - timedelta(days=MISSED_LOOKBACK_DAYS)).isoformat()
    for appointment in get_all_appointments():
        if (
            appointment["status"] == "CONFIRMED"
            and window_start <= appointment["appointment_date"] < today().isoformat()
        ):
            if notify(
                "booking_missed", "warning",
                f"Missed booking · {appointment['customer_name']}",
                f"{appointment['model']} was booked for {_human_day(appointment['appointment_date'])} and not marked serviced.",
                appointment["customer_id"], ref=f"missed:{appointment['id']}",
            ):
                created += 1
    return created


def _customer_name(customer_id: int) -> str:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM customers WHERE id = ?", (customer_id,))
    row = cursor.fetchone()
    conn.close()
    return row["name"] if row else f"Customer {customer_id}"


def _human_day(day: str | None) -> str:
    """'2026-10-23' -> 'Fri, 23-10-2026' (notifications and notes staff read)."""
    try:
        return date.fromisoformat(str(day)[:10]).strftime(f"%a, {DISPLAY_DATE}")
    except (TypeError, ValueError):
        return str(day or "")


def _utc_now() -> str:
    return datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


def start_voice_call(customer_id: int, customer_name: str, started_by: str | None) -> int:
    """Record that the voice agent is now on a call (shown live in the CRM)."""
    started_at = _utc_now()
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO voice_calls (customer_id, customer_name, started_by, started_at) VALUES (?, ?, ?, ?)",
        (customer_id, customer_name, started_by, started_at),
    )
    call_id = cursor.lastrowid
    conn.commit()
    conn.close()
    emit_event(
        "call.started",
        {"call_id": call_id, "customer_id": customer_id, "customer_name": customer_name,
         "started_by": started_by, "started_at": started_at},
    )
    return call_id


def end_voice_call(
    call_id: int, outcome: str | None, service_date: str | None = None, service_type: str | None = None
) -> None:
    """Close a call. `service_date` / `service_type`: the visit agreed on the
    call, shown in the CRM's end-of-call notice."""
    ended_at = _utc_now()
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM voice_calls WHERE id = ?", (call_id,))
    row = cursor.fetchone()
    if not row or _plain(row)["ended_at"]:
        conn.close()
        return
    row = _plain(row)
    cursor.execute("UPDATE voice_calls SET ended_at = ?, outcome = ? WHERE id = ?", (ended_at, outcome, call_id))
    conn.commit()
    conn.close()
    started = datetime.strptime(row["started_at"][:19], "%Y-%m-%d %H:%M:%S")
    emit_event(
        "call.ended",
        {"call_id": call_id, "customer_id": row["customer_id"], "customer_name": row["customer_name"],
         "started_by": row["started_by"], "started_at": row["started_at"], "ended_at": ended_at,
         "duration": int((datetime.strptime(ended_at, "%Y-%m-%d %H:%M:%S") - started).total_seconds()),
         "outcome": outcome, "service_date": service_date, "service_type": service_type},
    )


def active_voice_calls(max_age_hours: int = 2) -> list:
    """Calls in progress (open rows younger than `max_age_hours`, so a crashed
    bot can't leave a call "live" forever)."""
    since = (datetime.now(ZoneInfo("UTC")) - timedelta(hours=max_age_hours)).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM voice_calls WHERE ended_at IS NULL AND started_at >= ? ORDER BY started_at",
        (since,),
    )
    rows = [_plain(r) for r in cursor.fetchall()]
    conn.close()
    return rows


# [Plivo disabled — pending client decision on telephony]
# # ---------------------------------------------------------------------------
# # Phone calls (Plivo call UUID -> customer)
# # ---------------------------------------------------------------------------
# def register_phone_call(call_uuid: str, customer_id: int) -> None:
#     conn = _connect()
#     conn.execute(
#         "INSERT OR REPLACE INTO phone_calls (call_uuid, customer_id) VALUES (?, ?)",
#         (call_uuid, customer_id),
#     )
#     conn.commit()
#     conn.close()
#
#
# def get_customer_id_for_call(call_uuid: str) -> int | None:
#     conn = _connect()
#     cursor = conn.cursor()
#     cursor.execute("SELECT customer_id FROM phone_calls WHERE call_uuid = ?", (call_uuid,))
#     row = cursor.fetchone()
#     conn.close()
#     return row["customer_id"] if row else None
