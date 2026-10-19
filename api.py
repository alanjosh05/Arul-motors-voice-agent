import os
import secrets

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import database

# import dialer  # Plivo disabled — pending client decision on telephony

load_dotenv(".env.local")
load_dotenv()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """When API_KEY is set (always, once public), every request must send it
    in the X-API-Key header. The API exposes customer names and mobiles."""
    expected = os.getenv("API_KEY")
    if expected and not secrets.compare_digest(x_api_key or "", expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


app = FastAPI(
    title="Arul Motors Yamaha (Nazareth, Thoothukudi) Service API",
    dependencies=[Depends(require_api_key)],
)

# The dashboard frontend (hosted on GoDaddy) calls this API from the browser.
# CORS_ORIGINS lists the sites allowed to, comma-separated, e.g.
# "https://arulmotors.com,https://www.arulmotors.com". Unset = any (local dev).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Base URL of the Pipecat voice bot (SmallWebRTC runner). A browser client
# connects its WebRTC session directly to this endpoint.
BOT_URL = os.getenv("BOT_URL", "http://localhost:7860")

# Initialize the database (shared with the agent via database.DB_FILE).
database.init_db()


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


@app.get("/api/customers")
async def get_customers():
    """All customers with their vehicles and next service due."""
    return database.get_all_customers()


@app.post("/api/vehicles")
async def add_vehicle(vehicle: VehicleIn):
    """Add a bike sale (and the customer, if the mobile number is new)."""
    success, message = database.add_customer_vehicle(**vehicle.model_dump())
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"message": message}


@app.get("/api/due-services")
async def get_due_services(days: int = 7):
    """The follow-up call list: bikes due within `days` with no appointment yet."""
    return database.get_due_services(days)


@app.get("/api/appointments")
async def get_appointments():
    """All service appointments (the date each customer will bring their bike in)."""
    return database.get_all_appointments()


@app.get("/api/appointments/search")
async def search_appointments(
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


class CompleteIn(BaseModel):
    next_service_date: str | None = None
    next_service_type: str | None = None


@app.post("/api/appointments/{appointment_id}/complete")
async def complete_appointment(appointment_id: str, body: CompleteIn | None = None):
    """Workshop: mark a bike as serviced, optionally setting its next due service."""
    body = body or CompleteIn()
    success, message = database.complete_appointment(
        appointment_id, body.next_service_date, body.next_service_type
    )
    if not success:
        raise HTTPException(status_code=404, detail=message)
    return {"message": message}


@app.get("/api/calls")
async def get_calls():
    """Call log with the outcome of every follow-up call."""
    return database.get_call_logs()


# [Plivo disabled — pending client decision on telephony]
# @app.post("/api/calls/{customer_id}")
# async def start_call(customer_id: int):
#     """Place an outbound reminder call to the customer via Plivo."""
#     try:
#         return await dialer.place_call(customer_id)
#     except ValueError as e:
#         raise HTTPException(status_code=404, detail=str(e)) from e
#     except RuntimeError as e:
#         raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/api/connection")
async def get_connection(customer_id: int | None = None):
    """Return the Pipecat voice bot connection details for a browser test call.

    Send `requestData` to the bot's /api/offer so it knows which customer it is
    "calling".
    """
    return {
        "offerUrl": f"{BOT_URL}/api/offer",
        "requestData": {"customer_id": customer_id} if customer_id else {},
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("API_PORT", "8001")))
