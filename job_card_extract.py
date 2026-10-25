"""Read a scanned Arul Motors Yamaha job card (PDF) with Gemini on Vertex AI
and return its fields for the CRM's "Close service" form.

Same credentials as the voice agent (ADC, no API keys). Nothing is saved here:
staff check the filled-in form before closing the service.
"""

import json
import logging
import os
import time
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

import database

logger = logging.getLogger("arul-motors-api")

# Low-cost "lite" model (well under a rupee per card). Compared on a real
# Arul Motors job card (2026-10-25): 3.1 and 3.5 Flash-Lite read every amount,
# the total, odometer, dates, jobs and part rows correctly; 3.1 left the
# hard-to-read frame number blank where 3.5 guessed it wrong, with fewer
# tokens. Set JOB_CARD_MODEL to try another model without changing code.
EXTRACT_MODEL = os.getenv("JOB_CARD_MODEL", "gemini-3.1-flash-lite")
PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "project-2c317546-b2c6-4a7a-8d4")
# The Lite models are served from Vertex's "global" location for this project,
# not us-central1 (where the voice model runs).
LOCATION = os.getenv("JOB_CARD_LOCATION", "global")

# The job card's fixed lists (keep in step with the CRM's lib/job-card.ts).
JOBS_DONE = [
    "Engine oil change", "Water wash", "General check-up", "Transmission oil change",
    "Chain lube", "Injector cleaning", "Carbon cleaning", "Consumables",
]
DELIVERY_CHECKS = [
    "All jobs done as per periodic maintenance check sheet", "Customer demanded jobs done",
    "Final inspection done", "Tool kit & first aid kit", "Fuel level same as at receiving",
    "Returned without physical damage", "Replaced parts handed over",
]


# Kept lean on purpose: field names, descriptions and the prompt are sent with
# every card, and every field comes back (as null when blank), so only fields
# the Close service form uses are asked for. The two scanned pages are most of
# the cost (~2.4k of ~2.7k input tokens).
class Part(BaseModel):
    part_no: str | None = None
    name: str
    qty: int = 1
    amount: int | None = Field(None, description="₹ on the row")
    warranty: bool = Field(False, description="row is under UNDER WARRANTY")


class Labour(BaseModel):
    description: str
    amount: int | None = None


class Feedback(BaseModel):
    rating: Literal[tuple(database.FEEDBACK_RATINGS)] | None = None  # type: ignore[valid-type]
    delivery_on_time: bool | None = None
    washing_quality: bool | None = Field(None, description="OK=true")
    staff_behaviour: bool | None = None
    fairness_of_charges: bool | None = None
    repairing_quality: bool | None = None


class JobCard(BaseModel):
    job_card_no: str | None = None
    job_type: Literal[tuple(database.JOB_TYPES)] | None = None  # type: ignore[valid-type]
    registration_number: str | None = None
    odometer_km: int | None = None
    received_date: str | None = None
    delivered_date: str | None = None
    frame_number: str | None = None
    engine_number: str | None = None
    customer_requests: str | None = None
    jobs_done: list[Literal[tuple(JOBS_DONE)]] = Field(default_factory=list)  # type: ignore[valid-type]
    other_work: str | None = None
    parts: list[Part] = Field(default_factory=list)
    labour: list[Labour] = Field(default_factory=list)
    delivery_checks: list[Literal[tuple(DELIVERY_CHECKS)]] = Field(default_factory=list)  # type: ignore[valid-type]
    feedback: Feedback = Field(default_factory=Feedback)
    technician: str | None = None
    mechanic: str | None = None
    supervisor: str | None = None
    remarks: str | None = None
    total_amount: int | None = Field(None, description="₹ total: TOTAL/G.TOTAL, or a sum written below the parts")


PROMPT = """Scanned Arul Motors Yamaha job card, 2 pages, partly handwritten. Fill only what
is written, circled or ticked; blank or unreadable = null/[]. Never guess. Ignore printed
labels, the pre-service checklist, bike pictures, customer details, and signatures or
dates scrawled diagonally across the tables.

PAGE 1 (top to bottom):
- Top margin above the header: a handwritten 17-character code starting "ME1" = frame_number.
- Header, beside YAMAHA "CARD No.": the printed red number = job_card_no.
- First row of boxes, left to right: Vehicle Model | Vehicle Reg. No. = registration_number |
  Odometer Reading = odometer_km | Vehicle Receiving Date = received_date | Ready for delivery (estimate, ignore).
- "TYPE OF JOB" row: the circled/underlined choice = job_type (FREE SERVICE [1-2-3...] = Free service,
  PAID SERVICE, ACCIDENTAL = Accidental, QUICK (Minor) REPAIR, WARRANTY REPAIR).
- Left column "CUSTOMER DEMANDED REPAIRS/OBSERVATION": the handwritten lines = customer_requests
  (one line each, joined with ", "); also map them to jobs_done.
- "SUPERVISOR OBSERVATION" list (Water Wash ... Consumables): items ticked or with a value = jobs_done.
- Bottom: ALLOTED BAY NO./TECHNICIAN NAME = technician, MECHANIC NAME = mechanic,
  SUPERVISOR'S SIGN (a written name only) = supervisor; bottom-right "Frame No" / "Engine No"
  = frame_number (if not at the top) / engine_number; "Ready for delivery Date" = delivered_date.

PAGE 2:
- Top-left "PARTS REPLACED / REPAIRED" table: each written row = a part (PART No. = part_no,
  PART NAME = name, QTY = qty, Rs. = amount). Top-right "LABOUR" table: rows = labour.
- Middle-left "UNDER WARRANTY" table: rows with a part name = parts with warranty=true.
  A number alone in its Rs. column with no part name is the bill total = total_amount.
- "TOTAL" / "G.TOTAL" boxes = total_amount.
- "STANDARD CHECK AT THE TIME OF DELIVERY" boxes: ticked ones = delivery_checks.
- "REMARKS" line = remarks.
- "CUSTOMER FEEDBACK" row: the ticked/circled rating, then DELIVERY ON TIME YES/NO, WASHING QUALITY,
  STAFF BEHAVIOUR, FAIRNESS OF CHARGES, REPAIRING QUALITY (OK=true, NOT OK=false);
  REPLACED PARTS HANDOVER YES = delivery check "Replaced parts handed over".
- Delivery certificate "Date" at the bottom = delivered_date if page 1 has none.

Dates are DD/MM/YY. Amounts in whole rupees. Copy part, frame and registration numbers exactly
(letters and digits as written, no spaces). jobs_done uses only the allowed values
(Oil change -> Engine oil change, General checkup -> General check-up)."""

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
    return _client


def extract_job_card(pdf: bytes) -> dict:
    """Fields read from a job card PDF (see JobCard). Raises ValueError for a bad
    file and RuntimeError if Gemini fails."""
    if not pdf or not pdf.startswith(b"%PDF"):
        raise ValueError("The job card must be a PDF file.")
    if len(pdf) > database.MAX_PDF_BYTES:
        raise ValueError(f"The PDF is larger than {database.MAX_PDF_BYTES // (1024 * 1024)} MB.")
    started = time.monotonic()
    try:
        response = _get_client().models.generate_content(
            model=EXTRACT_MODEL,
            contents=[types.Part.from_bytes(data=pdf, mime_type="application/pdf"), PROMPT],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=JobCard,
                temperature=0,
            ),
        )
        card = JobCard.model_validate(json.loads(response.text))
    except Exception as e:  # network, quota, safety, malformed JSON
        logger.warning(f"Job card extraction failed: {e}")
        raise RuntimeError("Couldn't read the job card. Fill the form by hand.") from e
    usage = response.usage_metadata
    logger.info(
        f"Job card read in {time.monotonic() - started:.1f}s "
        f"({usage.prompt_token_count if usage else '?'} in / {usage.candidates_token_count if usage else '?'} out tokens)"
    )
    data = card.model_dump()
    # Drop dates the card can't have had (unreadable digits read as nonsense).
    for key in ("received_date", "delivered_date"):
        if data[key] and not database.normalize_date(data[key]):
            data[key] = None
    return data
