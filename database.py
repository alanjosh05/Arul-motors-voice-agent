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
# Weekdays the workshop does not take bikes (Monday=0 ... Sunday=6). Empty means
# open every day; e.g. {6} to stop customers choosing a Sunday.
CLOSED_WEEKDAYS: set[int] = set()
MAX_DAYS_AHEAD = 30

# Yamaha service schedule, in order (used to label a bike's previous service).
SERVICE_SEQUENCE = [
    "First free service",
    "Second free service",
    "Offer free service",
    "Paid service",
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
    # WAL lets one read while the other writes; the timeout makes a writer
    # wait for the other's write instead of failing with "database is locked".
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
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
            "table": "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
        }
    return {"pk": "INTEGER PRIMARY KEY AUTOINCREMENT", "str": "TEXT", "day": "TEXT", "id": "TEXT", "table": ""}


def _plain(row) -> dict:
    """A result row as a plain dict, with timestamps as 'YYYY-MM-DD HH:MM:SS'
    strings whichever database returned it."""
    return {
        key: value.strftime("%Y-%m-%d %H:%M:%S") if isinstance(value, datetime) else value
        for key, value in dict(row).items()
    }


def init_db():
    """Initialize the database with tables and seed demo customers."""
    conn = _connect()
    cursor = conn.cursor()

    t = _schema()

    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS customers (
        id {t['pk']},
        name {t['str']} NOT NULL,
        mobile_number {t['str']} NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) {t['table']}
    """)

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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (vehicle_id) REFERENCES vehicles(id)
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

    # Seed demo customers if empty. Dates are relative to today so the demo
    # always has upcoming services to call about.
    cursor.execute("SELECT count(*) AS n FROM customers")
    if cursor.fetchone()["n"] == 0 and _seed_demo():
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

    # Service history: record each bike's last service as a COMPLETED
    # appointment (once), so previous services show up in searches.
    cursor.execute("""
    SELECT * FROM vehicles v
    WHERE v.last_service_date IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM service_appointments a WHERE a.vehicle_id = v.id AND a.status = 'COMPLETED'
      )
    """)
    for vehicle in cursor.fetchall():
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
                _previous_service_type(vehicle["next_service_type"]),
                "Recorded from the bike's last service date.",
            ),
        )

    conn.commit()
    conn.close()
    logger.info("Database initialized")


def _next_appointment_id(cursor) -> str:
    cursor.execute("SELECT count(*) AS n FROM service_appointments")
    return f"AM{1001 + cursor.fetchone()['n']}"


def _previous_service_type(next_service_type: str) -> str:
    """The service before `next_service_type` in the Yamaha schedule."""
    if next_service_type in SERVICE_SEQUENCE:
        index = SERVICE_SEQUENCE.index(next_service_type)
        return SERVICE_SEQUENCE[index - 1] if index > 0 else "Service"
    return "Service"


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
) -> tuple[bool, str]:
    """Add a bike sale (creating the customer if the mobile number is new)."""
    name = (name or "").strip()
    mobile_number = (mobile_number or "").strip()
    registration_number = (registration_number or "").strip().upper()
    due = normalize_date(next_service_date)
    if not all([name, mobile_number, registration_number, model, next_service_type]) or not due:
        return (False, "Name, mobile, registration, model, service type and a valid due date are required.")

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
            "INSERT INTO customers (name, mobile_number) VALUES (?, ?)", (name, mobile_number)
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
) -> tuple[bool, str, str | None]:
    """Create a service appointment for one of the customer's bikes.

    `service_type` defaults to the bike's next due service. Returns
    (success, message, appointment_id).
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
    return (True, f"Appointment {appointment['id']} updated: {', '.join(changes)}.")


def cancel_appointment(customer_id: int, appointment_id: str | None = None) -> tuple[bool, str]:
    """Cancel (soft-delete) an appointment; the row is kept with status CANCELLED."""
    conn = _connect()
    cursor = conn.cursor()
    appointment, error = _get_own_appointment(cursor, customer_id, appointment_id)
    if error:
        conn.close()
        return (False, error)
    cursor.execute(
        "UPDATE service_appointments SET status = 'CANCELLED' WHERE id = ?", (appointment["id"],)
    )
    conn.commit()
    conn.close()
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
    return (True, f"Appointment {row['id']} marked as completed.")


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
    - mobile_number: any part of the number ("3210" finds "9876543210")
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
    SELECT a.*, c.name AS customer_name, c.mobile_number, v.registration_number, v.model
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
        if mobile_number and _digits(mobile_number) not in _digits(row["mobile_number"]):
            continue
        if registration_number and _alnum(registration_number) not in _alnum(row["registration_number"]):
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
) -> tuple[bool, str]:
    outcome = (outcome or "").strip().lower()
    if outcome not in CALL_OUTCOMES:
        return (False, f"Unknown outcome '{outcome}'. Use one of: {', '.join(sorted(CALL_OUTCOMES))}.")
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO call_logs (customer_id, vehicle_id, outcome, callback_date, notes) VALUES (?, ?, ?, ?, ?)",
        (customer_id, vehicle_id, outcome, normalize_date(callback_date) or callback_date, notes),
    )
    conn.commit()
    conn.close()
    return (True, f"Call outcome '{outcome}' saved.")


def get_call_logs() -> list:
    conn = _connect()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT l.*, c.name AS customer_name, c.mobile_number
    FROM call_logs l JOIN customers c ON c.id = l.customer_id
    ORDER BY l.created_at DESC
    """)
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
