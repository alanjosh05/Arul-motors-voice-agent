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


def _ensure_column(cursor, table: str, column: str, column_type: str) -> None:
    """Add a column to a table created by an older version (SQLite and MySQL)."""
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
        sort_order INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)
    cursor.execute("SELECT count(*) AS n FROM service_types")
    if cursor.fetchone()["n"] == 0:
        for order, (name, description, is_free, days, km) in enumerate(DEFAULT_SERVICE_TYPES, start=1):
            cursor.execute(
                "INSERT INTO service_types (name, description, is_free, interval_days, interval_km, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, description, is_free, days, km, order),
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
        service_date {t['day']} NOT NULL,
        service_type {t['str']} NOT NULL,
        odometer_km INTEGER,
        work_done TEXT NOT NULL,
        parts TEXT,
        amount INTEGER,
        technician {t['str']},
        remarks TEXT,
        next_service_date {t['day']},
        next_service_type {t['str']},
        created_by {t['str']},
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)

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

    conn.commit()
    conn.close()
    logger.info("Database initialized")


def _next_appointment_id(cursor) -> str:
    cursor.execute("SELECT count(*) AS n FROM service_appointments")
    return f"AM{1001 + cursor.fetchone()['n']}"


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


def _spoken(day: str) -> str:
    """'2026-10-21' -> '2026-10-21 (Wednesday)', so the agent can say the day."""
    return f"{day} ({date.fromisoformat(day).strftime('%A')})"


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
      AND NOT EXISTS (
        SELECT 1 FROM service_appointments a WHERE a.vehicle_id = v.id AND a.status = 'CONFIRMED'
      )
    ORDER BY v.next_service_date
    """,
        (cutoff,),
    )
    rows = [_plain(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def add_customer_vehicle(
    name: str,
    mobile_number: str,
    registration_number: str,
    model: str,
    next_service_date: str,
    next_service_type: str,
    purchase_date: str | None = None,
    odometer_km: int | None = None,
    last_service_date: str | None = None,
    alternate_mobile: str | None = None,
) -> tuple[bool, str]:
    """Add a bike sale (creating the customer, with `alternate_mobile`, if the
    mobile number is new)."""
    name = (name or "").strip()
    mobile_number = (mobile_number or "").strip()
    registration_number = (registration_number or "").strip().upper()
    due = normalize_date(next_service_date)
    if not all([name, mobile_number, registration_number, model, next_service_type]) or not due:
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
    INSERT INTO vehicles (customer_id, registration_number, model, purchase_date, last_service_date, odometer_km, next_service_date, next_service_type)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            customer_id,
            registration_number,
            model,
            normalize_date(purchase_date),
            normalize_date(last_service_date),
            odometer_km,
            due,
            next_service_type,
        ),
    )
    conn.commit()
    conn.close()
    _record_change(["customers", "vehicles", "due", "stats"], customer_id)
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
MAX_PDF_BYTES = 10 * 1024 * 1024  # service report upload limit (10 MB)


def _clean_text(value: str | None, limit: int = 2000) -> str | None:
    value = (value or "").strip()
    return value[:limit] or None


def close_service(
    appointment_id: str,
    *,
    service_date: str | None,
    service_type: str | None,
    odometer_km: int | None,
    work_done: str | None,
    pdf: bytes | None,
    pdf_name: str | None,
    parts: str | None = None,
    amount: int | None = None,
    technician: str | None = None,
    remarks: str | None = None,
    next_service_date: str | None = None,
    next_service_type: str | None = None,
    actor: str | None = None,
) -> tuple[bool, str, int | None]:
    """Close a booking with what was done: saves a maintenance record and its
    service report PDF, marks the booking COMPLETED, and moves the bike on
    (last service date, odometer, next due service). All in one transaction.
    Returns (ok, message, record_id)."""
    day = normalize_date(service_date)
    if not day:
        return (False, "Enter the date the service was done.", None)
    if day > today().isoformat():
        return (False, "The service date can't be in the future.", None)
    work_done = _clean_text(work_done)
    if not work_done:
        return (False, "Describe the work done.", None)
    if odometer_km is not None and odometer_km < 0:
        return (False, "Odometer must be 0 or more.", None)
    if amount is not None and amount < 0:
        return (False, "Amount must be 0 or more.", None)
    if not pdf:
        return (False, "Attach the service report (PDF).", None)
    if len(pdf) > MAX_PDF_BYTES:
        return (False, f"The PDF is larger than {MAX_PDF_BYTES // (1024 * 1024)} MB.", None)
    if not pdf.startswith(b"%PDF"):
        return (False, "The service report must be a PDF file.", None)
    next_due = normalize_date(next_service_date) if next_service_date else None
    if next_service_date and not next_due:
        return (False, "Invalid next service date.", None)
    if next_due and next_due <= day:
        return (False, "The next service must be after this service.", None)
    if next_due and not (next_service_type or "").strip():
        return (False, "Choose the next service type.", None)

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
    service_type = (service_type or "").strip() or booking["service_type"]
    filename = re.sub(r"[^\w.\- ]", "_", (pdf_name or "service-report.pdf").strip())[:80] or "service-report.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    try:
        cursor.execute(
            """
        INSERT INTO service_records (appointment_id, vehicle_id, customer_id, service_date, service_type,
            odometer_km, work_done, parts, amount, technician, remarks, next_service_date, next_service_type, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                booking["id"], booking["vehicle_id"], booking["customer_id"], day, service_type,
                odometer_km, work_done, _clean_text(parts), amount, _clean_text(technician, 100),
                _clean_text(remarks), next_due, (next_service_type or "").strip() or None, actor,
            ),
        )
        record_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO service_documents (record_id, filename, content_type, size_bytes, data) VALUES (?, ?, ?, ?, ?)",
            (record_id, filename, "application/pdf", len(pdf), pdf),
        )
        cursor.execute("UPDATE service_appointments SET status = 'COMPLETED' WHERE id = ?", (booking["id"],))
        cursor.execute("UPDATE vehicles SET last_service_date = ? WHERE id = ?", (day, booking["vehicle_id"]))
        if odometer_km is not None:
            cursor.execute("UPDATE vehicles SET odometer_km = ? WHERE id = ?", (odometer_km, booking["vehicle_id"]))
        if next_due:
            cursor.execute(
                "UPDATE vehicles SET next_service_date = ?, next_service_type = ? WHERE id = ?",
                (next_due, next_service_type.strip(), booking["vehicle_id"]),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    _record_change(BOOKING_ENTITIES + ["vehicles", "service_records"], booking["customer_id"])
    return (True, f"Service closed for booking {booking['id']}.", record_id)


def get_service_history(vehicle_id: int) -> list:
    """A bike's service history, newest first: maintenance records (with their
    PDF's name and size) and, for older services without a record (e.g. the
    client's import), the completed booking itself (kind 'booking')."""
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
    records = [{"kind": "record", **_plain(r)} for r in cursor.fetchall()]
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
    rows = [_plain(r) for r in cursor.fetchall()]
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
    "odometer_km", "next_service_date", "next_service_type",
)


def update_vehicle(vehicle_id: int, **changes) -> tuple[bool, str]:
    changes = {k: v for k, v in changes.items() if k in VEHICLE_FIELDS and v is not None}
    if not changes:
        return (False, "Nothing to change.")
    for key in ("purchase_date", "last_service_date", "next_service_date"):
        if key in changes:
            day = normalize_date(str(changes[key]))
            if not day:
                return (False, f"Invalid date for {key.replace('_', ' ')}.")
            changes[key] = day
    if "registration_number" in changes:
        changes["registration_number"] = changes["registration_number"].strip().upper()
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM vehicles WHERE id = ?", (vehicle_id,))
    if not cursor.fetchone():
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
    assignments = ", ".join(f"{key} = ?" for key in changes)
    cursor.execute(f"UPDATE vehicles SET {assignments} WHERE id = ?", (*changes.values(), vehicle_id))
    conn.commit()
    conn.close()
    _record_change(["vehicles", "customers", "due", "stats"])
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
            "overdue": sum(1 for d in due if d["next_service_date"] < day),
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
        "recent_calls": calls_30[:6] if len(calls_30) >= 6 else get_call_logs(limit=6),
    }


# ---------------------------------------------------------------------------
# Service types (managed from the CRM)
# ---------------------------------------------------------------------------
SERVICE_TYPE_FIELDS = ("name", "description", "is_free", "interval_days", "interval_km", "price", "active")


def _service_type_row(row) -> dict:
    row = _plain(row)
    row["is_free"] = bool(row["is_free"])
    row["active"] = bool(row["active"])
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
    for key in ("interval_days", "interval_km", "price"):
        if key in clean and clean[key] is not None:
            try:
                clean[key] = int(clean[key])
            except (TypeError, ValueError):
                return clean, f"{key.replace('_', ' ').capitalize()} must be a whole number."
            if clean[key] < 0 or (key == "interval_days" and clean[key] == 0):
                return clean, f"{key.replace('_', ' ').capitalize()} must be more than 0."
    for key in ("is_free", "active"):
        if key in clean:
            clean[key] = 1 if clean[key] else 0
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
    """'2026-10-23' -> 'Fri, 23 Oct'."""
    try:
        return date.fromisoformat(str(day)[:10]).strftime("%a, %d %b").replace(" 0", " ")
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
