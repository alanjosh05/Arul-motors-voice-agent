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


PROMPT = """Scanned Yamaha job card (Arul Motors), partly handwritten. Fill only what is
written, circled or ticked; blank or unreadable = null or []. Never guess.
Dates: DD/MM/YY on the card. Amounts: whole rupees. A total written on
its own is total_amount, not a part. Copy part, frame and registration numbers exactly.
jobs_done: map written jobs to the allowed values (Oil change -> Engine oil change).
customer_requests: the CUSTOMER DEMANDED lines. Ignore signatures and stamps."""

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
