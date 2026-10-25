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


class Part(BaseModel):
    part_no: str | None = Field(None, description="Part number exactly as written, e.g. 90793-AD411")
    name: str = Field(description="Part name, e.g. Engine oil")
    qty: int = Field(1, description="Quantity")
    amount: int | None = Field(None, description="Rupees written for this line (whole number)")
    warranty: bool = Field(False, description="True only if listed under UNDER WARRANTY")


class Labour(BaseModel):
    description: str
    amount: int | None = None


class Feedback(BaseModel):
    rating: Literal[tuple(database.FEEDBACK_RATINGS)] | None = None  # type: ignore[valid-type]
    delivery_on_time: bool | None = None
    washing_quality: bool | None = Field(None, description="True = OK, False = NOT OK")
    staff_behaviour: bool | None = None
    fairness_of_charges: bool | None = None
    repairing_quality: bool | None = None


class JobCard(BaseModel):
    job_card_no: str | None = Field(None, description="CARD No. printed/written at the top")
    job_type: Literal[tuple(database.JOB_TYPES)] | None = Field(  # type: ignore[valid-type]
        None, description="TYPE OF JOB that is circled/ticked"
    )
    free_service_number: int | None = Field(None, description="Which free service is circled (1-6), if a free service")
    registration_number: str | None = None
    model: str | None = Field(None, description="Vehicle model as written")
    odometer_km: int | None = Field(None, description="Odometer reading")
    received_date: str | None = Field(None, description="Vehicle receiving date as YYYY-MM-DD")
    delivered_date: str | None = Field(None, description="Delivery / job closing date as YYYY-MM-DD, if written")
    frame_number: str | None = Field(None, description="Frame / chassis number (often 17 characters, starts ME1)")
    engine_number: str | None = None
    customer_name: str | None = None
    mobile_number: str | None = None
    customer_requests: str | None = Field(None, description="CUSTOMER DEMANDED REPAIRS / OBSERVATION lines, comma-separated")
    jobs_done: list[Literal[tuple(JOBS_DONE)]] = Field(  # type: ignore[valid-type]
        default_factory=list, description="Jobs from the fixed list that were done (from supervisor observation ticks and the requests)"
    )
    other_work: str | None = Field(None, description="Other work or observations not in jobs_done")
    parts: list[Part] = Field(default_factory=list, description="PARTS REPLACED / REPAIRED and UNDER WARRANTY rows")
    labour: list[Labour] = Field(default_factory=list, description="LABOUR rows with a description")
    delivery_checks: list[Literal[tuple(DELIVERY_CHECKS)]] = Field(default_factory=list)  # type: ignore[valid-type]
    feedback: Feedback = Field(default_factory=Feedback)
    technician: str | None = Field(None, description="ALLOTED BAY NO. / TECHNICIAN NAME")
    mechanic: str | None = None
    supervisor: str | None = None
    remarks: str | None = None
    total_amount: int | None = Field(None, description="TOTAL / G.TOTAL written on the card")


PROMPT = """This is a scanned job card from Arul Motors (Yamaha), Nazareth, Tamil Nadu,
partly handwritten. Read it and fill the fields.

Rules:
- Only use what is written or ticked on the card. If a field is blank or you can't
  read it, leave it null (or an empty list). Never guess.
- Dates on the card are Indian (DD/MM/YY or DD/MM/YYYY); return them as YYYY-MM-DD
  (e.g. 23/10/26 -> 2026-10-23).
- Amounts are whole rupees. Part rows: part number, part name, quantity and the
  rupee amount on that row. Rows under UNDER WARRANTY have warranty = true.
- A total written on its own (e.g. under the parts) is total_amount, not a part.
- Signatures, initials and stamps are not data.
- jobs_done: pick from the allowed list only what was done or requested and done
  (e.g. "Oil change" written -> "Engine oil change"; "Water wash" -> "Water wash").
- Copy part numbers, frame and registration numbers character by character.
"""

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
