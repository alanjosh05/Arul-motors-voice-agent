"""Excel export of the CRM (Settings > Data & backups > Export to Excel).

Four sheets the showroom can filter and work on: Customers & Bikes (one row
per bike), Bookings, Calls and Service Types. Dates are real Excel dates and
mobile numbers stay text.
"""

import io
from datetime import date, datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import database

OUTCOME_LABELS = {
    "date_confirmed": "Date confirmed",
    "callback_requested": "Call back",
    "not_interested": "Not interested",
    "already_serviced": "Already serviced",
    "bike_sold": "Bike sold",
    "wrong_number": "Wrong number",
    "no_answer": "No answer",
}
STATE_LABELS = {"upcoming": "Upcoming", "completed": "Serviced", "cancelled": "Cancelled", "missed": "Missed"}

HEADER = PatternFill("solid", fgColor="1F3A8A")
DAY = "DD-MM-YYYY"
DAY_TIME = "DD-MM-YYYY HH:MM"


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _local_time(value) -> datetime | None:
    """Database timestamps are UTC ('YYYY-MM-DD HH:MM:SS'): show IST, no tzinfo
    (Excel can't store time zones)."""
    if not value:
        return None
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(database.SHOWROOM_TZ).replace(tzinfo=None)


def _sheet(wb: Workbook, title: str, columns: list[tuple[str, int, str | None]], rows: list[list]) -> None:
    """columns: (header, width, number format or None)."""
    ws = wb.create_sheet(title)
    ws.append([c[0] for c in columns])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER
        cell.alignment = Alignment(vertical="center")
    for row in rows:
        ws.append(row)
    for i, (_, width, fmt) in enumerate(columns, start=1):
        letter = get_column_letter(i)
        ws.column_dimensions[letter].width = width
        if fmt:
            for (cell,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                cell.number_format = fmt
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 2)}"


def build_workbook() -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    customers = database.list_customers()
    bike_rows = []
    for c in sorted(customers, key=lambda c: c["name"].lower()):
        booking = c.get("next_appointment")
        call = c.get("last_call")
        shared = [
            c["name"],
            c["mobile_number"],
            c.get("alternate_mobile") or "",
        ]
        tail = [
            _day(booking["appointment_date"]) if booking else None,
            _local_time(call["created_at"]) if call else None,
            OUTCOME_LABELS.get(call["outcome"], call["outcome"]) if call else "",
        ]
        for v in c["vehicles"] or [None]:
            bike = (
                [v["model"], v["registration_number"], _day(v["purchase_date"]), _day(v["last_service_date"]),
                 v["odometer_km"], _day(v["next_service_date"]), v["next_service_type"]]
                if v else ["", "", None, None, None, None, ""]
            )
            bike_rows.append(shared + bike + tail)
    _sheet(wb, "Customers & Bikes", [
        ("Customer", 24, None), ("Mobile", 13, "@"), ("Alternate mobile", 15, "@"),
        ("Bike model", 30, None), ("Registration", 15, None), ("Purchase date", 13, DAY),
        ("Last service", 13, DAY), ("Odometer (km)", 13, "#,##0"), ("Next service due", 15, DAY),
        ("Next service type", 22, None), ("Next booking", 13, DAY), ("Last call", 16, DAY_TIME),
        ("Last call outcome", 17, None),
    ], bike_rows)

    bookings = database.search_appointments()
    _sheet(wb, "Bookings", [
        ("Booking", 10, None), ("Date", 12, DAY), ("Status", 11, None), ("Customer", 24, None),
        ("Mobile", 13, "@"), ("Bike model", 30, None), ("Registration", 15, None),
        ("Service type", 22, None), ("Notes", 40, None), ("Created", 16, DAY_TIME),
    ], [
        [b["id"], _day(b["appointment_date"]), STATE_LABELS.get(b["state"], b["state"]), b["customer_name"],
         b["mobile_number"], b["model"], b["registration_number"], b["service_type"], b["notes"] or "",
         _local_time(b["created_at"])]
        for b in bookings
    ])

    calls = database.get_call_logs()
    _sheet(wb, "Calls", [
        ("When", 16, DAY_TIME), ("Customer", 24, None), ("Mobile", 13, "@"), ("Bike", 30, None),
        ("Registration", 15, None), ("Outcome", 17, None), ("Call back on", 13, DAY), ("Notes", 50, None),
    ], [
        [_local_time(c["created_at"]), c["customer_name"], c["mobile_number"], c["model"] or "",
         c["registration_number"] or "", OUTCOME_LABELS.get(c["outcome"], c["outcome"]),
         _day(c["callback_date"]), c["notes"] or ""]
        for c in calls
    ])

    _sheet(wb, "Service Types", [
        ("Service", 26, None), ("Free?", 7, None), ("Due after (days)", 15, None), ("Due after (km)", 14, "#,##0"),
        ("Price (₹)", 10, "#,##0"), ("Active", 8, None), ("Description", 50, None),
    ], [
        [t["name"], "Yes" if t["is_free"] else "No", t["interval_days"], t["interval_km"], t["price"],
         "Yes" if t["active"] else "No", t["description"] or ""]
        for t in database.list_service_types()
    ])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
