import asyncio
import os
from datetime import date, timedelta
# [Plivo disabled — pending client decision on telephony]
# from urllib.parse import parse_qs

from dotenv import load_dotenv
# [Plivo disabled — pending client decision on telephony]
# from fastapi import Request
# from fastapi.responses import Response
from google.genai.types import EndSensitivity, StartSensitivity
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    EndWorkerFrame,
    FunctionCallResultProperties,
    LLMRunFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi import RTVIObserver, RTVIProcessor
# from pipecat.runner.run import app  # Plivo disabled
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.google.gemini_live.llm import (
    ContextWindowCompressionParams,
    GeminiVADParams,
)
from pipecat.services.google.gemini_live.vertex.llm import GeminiLiveVertexLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.base_transport import TransportParams
# from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams  # Plivo disabled

import database

# import dialer  # Plivo disabled

load_dotenv(".env.local")
load_dotenv()

# Initialize the database (creates tables and seeds demo customers on first run).
database.init_db()

# Gemini Live realtime (speech-to-speech) model on Vertex AI. A single
# multimodal model handles STT, reasoning, and TTS, so there is no separate
# STT/TTS service. Auth is Application Default Credentials (no API key: the
# org policy disallows them). Live voice models are not offered in Indian
# regions; us-central1 measured fastest (see CLAUDE.md).
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "project-2c317546-b2c6-4a7a-8d4")
GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-live")

# Seconds to let the goodbye line finish playing before the call is hung up.
HANGUP_DELAY_SECS = 2.0

# Most search results read back to the model at once (newest first).
MAX_SEARCH_RESULTS = 3

# Token budget. Replies are one or two short sentences (~5 s, ~130 audio
# tokens); 400 tokens (~16 s) only cuts off a runaway answer.
MAX_REPLY_TOKENS = 400
# Context size at which Gemini starts dropping the oldest turns.
CONTEXT_TRIGGER_TOKENS = 6000

SYSTEM_INSTRUCTION_TEMPLATE = """
You are on an OUTBOUND call for the service team of Arul Motors Yamaha
showroom, {location} (a Yamaha sub-dealer that sells and services bikes). The
customer bought their bike here. Remind them the service is due and get the
DATE they will bring it in. No time slots: never ask for a time.

RULES:
- No personal name: you are from "Arul Motors Yamaha showroom, Nazareth".
- Not a mechanic: never diagnose or quote prices. Other topics (repairs,
  prices, new bikes): one line saying the showroom team will help, then back
  to the date.
- You don't know today's date. Call get_current_datetime ONCE after the
  customer confirms who they are; reuse its day list. Never guess dates.
- Ask ONE thing per reply. Every reply is one short sentence, two at most.
- Plain speech only: no symbols, lists or emojis. Never read IDs or
  YYYY-MM-DD aloud; say dates naturally ("இந்த Saturday, 24-ஆம் தேதி").

CUSTOMER: {customer_name}, mobile {mobile_number}
{vehicles}
{appointments}
LANGUAGE: casual spoken TANGLISH (Tamil + everyday English words like bike,
service, date, okay, busy, confirm, cancel), warm, respectful "ங்க" ending,
never formal Tamil. Reply in Tanglish whatever the customer speaks. Use at
most one natural interjection per reply and vary it: ம்ம், சரி சரி, ஓ okay,
அப்படியா, அச்சச்சோ, பரவாயில்லங்க, super. Don't say sir or madam.
Example: "ம்ம், எப்போ service-க்கு வண்டிய கொண்டு வரீங்க?"

FLOW:
1. Confirm you're speaking to {customer_name}. Wrong person: apologise,
   log_call_outcome wrong_number, bye, end_call.
2. Say which bike's service is due and roughly when.
3. Ask which date. Turn "Saturday"/"next Monday" into the exact date and
   confirm it with the day. "Next week" alone: ask which day.
4. Agreed: create_service_appointment, then confirm in one line.
5. Busy/unsure: ask when to call back, log callback_requested with
   callback_date. Else log already_serviced, bike_sold or not_interested.
   Don't push more than once.
6. Always log_call_outcome (date_confirmed if a date was set), one-line bye,
   then end_call.

APPOINTMENTS (only this customer's): create, check
(get_service_appointments), change (update_service_appointment) or cancel
(cancel_service_appointment, confirm once first). Before a change or cancel,
call get_service_appointments. One upcoming booking per bike: if one exists,
offer to change its date. If a tool rejects a date, ask for another.

SEARCH: use search_service_bookings for past or other bookings ("last time
எப்போ service?"), with any detail given. Answer briefly, newest first.
Another customer's bookings show only with their full registration or full
mobile number; if hidden, ask for one. Never say anyone's mobile number.
Read-only for other customers.

Tool dates are YYYY-MM-DD. Say registration numbers one character at a time.
"""


GREETING_KICKOFF = (
    "The customer picked up. In ONE short Tanglish line, greet them, say you're "
    "calling from Arul Motors Yamaha showroom, Nazareth, and check it's {name}. "
    "Don't mention the service yet."
)


def build_system_instruction(customer: dict) -> str:
    """Fill the system prompt with this call's customer, bike and appointment details."""
    vehicles = "\n".join(
        f"- {v['model']} {v['registration_number']}, bought {v['purchase_date']}, "
        f"~{v['odometer_km']} km, last service {v['last_service_date'] or 'none'}; "
        f"DUE {v['next_service_type']} ~{v['next_service_date']}"
        for v in customer["vehicles"]
    )
    appointments = (
        "- Booked: "
        + "; ".join(
            f"{a['appointment_date']} {a['service_type']} ({a['id']})"
            for a in customer["appointments"]
        )
        + "\n"
        if customer["appointments"]
        else "- Not booked yet.\n"
    )
    return SYSTEM_INSTRUCTION_TEMPLATE.format(
        location=database.SHOWROOM_LOCATION,
        customer_name=customer["name"],
        mobile_number=customer["mobile_number"],
        vehicles=vehicles,
        appointments=appointments,
    )


# ---------------------------------------------------------------------------
# Function tool handlers
#
# Every call is about one known customer, so the handlers are bound to a
# ServiceCall that holds that customer's ID. The model never has to (and can't)
# pick another customer's records.
# ---------------------------------------------------------------------------
class ServiceCall:
    def __init__(self, customer: dict):
        self.customer = customer
        self.customer_id = customer["id"]
        self.outcome_logged = False
        self.logged_outcomes: set[str] = set()

    async def get_current_datetime(self, params: FunctionCallParams) -> None:
        now = database.now()
        days = ", ".join(
            f"{(now + timedelta(days=i)).strftime('%a')} {(now + timedelta(days=i)).date().isoformat()}"
            for i in range(1, 15)
        )
        await params.result_callback(
            f"Now (IST): {now.strftime('%a')} {now.date().isoformat()} {now.strftime('%H:%M')}. "
            f"Next: {days}"
        )

    async def create_service_appointment(self, params: FunctionCallParams) -> None:
        args = params.arguments
        logger.info(f"Creating appointment for customer {self.customer_id}: {args}")
        _, message, _ = database.create_appointment(
            self.customer_id,
            args.get("date"),
            registration_number=args.get("registration_number"),
            service_type=args.get("service_type"),
            notes=args.get("notes"),
        )
        await params.result_callback(message)

    async def get_service_appointments(self, params: FunctionCallParams) -> None:
        appointments = database.get_appointments(self.customer_id)
        if not appointments:
            await params.result_callback("No upcoming appointment.")
            return
        lines = [
            f"{a['id']}: {a['model']} {a['registration_number']}, {a['service_type']}, "
            f"{date.fromisoformat(a['appointment_date']).strftime('%a')} {a['appointment_date']}"
            + (f", notes: {a['notes'].rstrip('.')}" if a["notes"] else "")
            for a in appointments
        ]
        await params.result_callback("Upcoming:\n" + "\n".join(lines))

    async def update_service_appointment(self, params: FunctionCallParams) -> None:
        args = params.arguments
        logger.info(f"Updating appointment for customer {self.customer_id}: {args}")
        _, message = database.update_appointment(
            self.customer_id,
            appointment_id=args.get("appointment_id"),
            new_date=args.get("new_date"),
            notes=args.get("notes"),
        )
        await params.result_callback(message)

    async def cancel_service_appointment(self, params: FunctionCallParams) -> None:
        appointment_id = params.arguments.get("appointment_id")
        logger.info(f"Cancelling appointment {appointment_id} for customer {self.customer_id}")
        _, message = database.cancel_appointment(self.customer_id, appointment_id)
        await params.result_callback(message)

    async def search_service_bookings(self, params: FunctionCallParams) -> None:
        args = {k: v for k, v in params.arguments.items() if v not in (None, "")}
        logger.info(f"Searching bookings for customer {self.customer_id}: {args}")
        filters = {
            key: args.get(key)
            for key in (
                "customer_name",
                "mobile_number",
                "registration_number",
                "model",
                "service_type",
                "state",
                "date_from",
                "date_to",
                "appointment_id",
            )
        }
        identifying = ("customer_name", "mobile_number", "registration_number", "appointment_id")
        if not any(filters[key] for key in identifying):
            # No one named: search the caller's own bookings.
            filters["mobile_number"] = self.customer["mobile_number"]

        rows = database.search_appointments(**filters)

        def visible(row: dict) -> bool:
            # Other customers' bookings need the exact registration or mobile.
            return (
                row["customer_id"] == self.customer_id
                or database.same_registration(filters["registration_number"], row["registration_number"])
                or database.same_mobile(filters["mobile_number"], row["mobile_number"])
            )

        shown = [row for row in rows if visible(row)]
        hidden = len(rows) - len(shown)
        if not shown:
            message = "No matching service bookings found."
            if hidden:
                message = (
                    f"{hidden} match(es) of another customer hidden. Ask for the full "
                    "registration or full mobile number."
                )
            await params.result_callback(message)
            return

        lines = []
        for row in shown[:MAX_SEARCH_RESULTS]:
            day = date.fromisoformat(row["appointment_date"]).strftime("%a")
            owner = (
                "this customer"
                if row["customer_id"] == self.customer_id
                else f"customer {row['customer_name']}"
            )
            lines.append(
                f"{row['id']} {row['state']}: {row['model']} {row['registration_number']} "
                f"({owner}), {row['service_type']}, {day} {row['appointment_date']}"
                + (f", notes: {row['notes'].rstrip('.')}" if row["notes"] else "")
            )
        summary = f"Found {len(shown)}, newest first"
        if len(shown) > MAX_SEARCH_RESULTS:
            summary += f", showing {MAX_SEARCH_RESULTS}"
        if hidden:
            summary += f"; {hidden} of another customer hidden"
        await params.result_callback(summary + ":\n" + "\n".join(lines))

    async def log_call_outcome(self, params: FunctionCallParams) -> None:
        args = params.arguments
        logger.info(f"Call outcome for customer {self.customer_id}: {args}")
        outcome = (args.get("outcome") or "").strip().lower()
        if outcome in self.logged_outcomes:
            # The model sometimes records the same outcome twice; keep one row.
            await params.result_callback(f"'{outcome}' already saved.")
            return
        vehicle = self.customer["vehicles"][0]
        ok, message = database.log_call(
            self.customer_id,
            args.get("outcome"),
            vehicle_id=vehicle["id"],
            callback_date=args.get("callback_date"),
            notes=args.get("notes"),
        )
        if ok:
            self.outcome_logged = True
            self.logged_outcomes.add(outcome)
        await params.result_callback(message)

    async def end_call(self, params: FunctionCallParams) -> None:
        logger.info(f"Ending call with customer {self.customer_id}")
        await params.result_callback(
            "Ending.", properties=FunctionCallResultProperties(run_llm=False)
        )
        # Let the goodbye finish playing, then shut the pipeline down. (With
        # telephony enabled, the serializer also hangs up the phone call.)
        await asyncio.sleep(HANGUP_DELAY_SECS)
        await params.llm.push_frame(EndWorkerFrame(), FrameDirection.UPSTREAM)

    def register(self, llm: GeminiLiveVertexLLMService) -> None:
        for schema in TOOLS.standard_tools:
            llm.register_function(schema.name, getattr(self, schema.name))


# ---------------------------------------------------------------------------
# Tool schemas (advertised to the Gemini Live model)
# ---------------------------------------------------------------------------
TOOLS = ToolsSchema(
    standard_tools=[
        FunctionSchema(
            name="get_current_datetime",
            description="Today's date and time (IST) and the next 14 dates.",
            properties={},
            required=[],
        ),
        FunctionSchema(
            name="create_service_appointment",
            description="Book the date the customer will bring the bike in.",
            properties={
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "registration_number": {"type": "string", "description": "If several bikes"},
                "service_type": {"type": "string", "description": "If not the due one"},
                "notes": {"type": "string", "description": "What to check"},
            },
            required=["date"],
        ),
        FunctionSchema(
            name="get_service_appointments",
            description="The customer's upcoming appointments.",
            properties={},
            required=[],
        ),
        FunctionSchema(
            name="update_service_appointment",
            description="Change an appointment's date and/or notes.",
            properties={
                "appointment_id": {"type": "string", "description": "If several"},
                "new_date": {"type": "string", "description": "YYYY-MM-DD"},
                "notes": {"type": "string", "description": "Replaces old notes"},
            },
            required=[],
        ),
        FunctionSchema(
            name="cancel_service_appointment",
            description="Cancel an appointment.",
            properties={
                "appointment_id": {"type": "string", "description": "If several"},
            },
            required=[],
        ),
        FunctionSchema(
            name="search_service_bookings",
            description=(
                "Search past and upcoming bookings. All fields optional and partial; "
                "none identifying = this customer's own."
            ),
            properties={
                "customer_name": {"type": "string"},
                "mobile_number": {"type": "string"},
                "registration_number": {"type": "string", "description": "Full or last digits"},
                "model": {"type": "string"},
                "service_type": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["upcoming", "completed", "cancelled", "missed"],
                },
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "appointment_id": {"type": "string"},
            },
            required=[],
        ),
        FunctionSchema(
            name="log_call_outcome",
            description="Record how the call ended, once, before end_call.",
            properties={
                "outcome": {
                    "type": "string",
                    "enum": sorted(database.CALL_OUTCOMES - {"no_answer"}),
                },
                "callback_date": {"type": "string", "description": "YYYY-MM-DD"},
                "notes": {"type": "string", "description": "Short reason"},
            },
            required=["outcome"],
        ),
        FunctionSchema(
            name="end_call",
            description="Hang up, after the goodbye.",
            properties={},
            required=[],
        ),
    ]
)


def _build_llm(customer: dict) -> GeminiLiveVertexLLMService:
    # No credentials passed: Pipecat falls back to ADC (`gcloud auth
    # application-default login` locally, Workload Identity on a server).
    return GeminiLiveVertexLLMService(
        project_id=GOOGLE_CLOUD_PROJECT,
        location=GOOGLE_CLOUD_LOCATION,
        tools=TOOLS,
        settings=GeminiLiveVertexLLMService.Settings(
            model=GEMINI_MODEL,
            voice=os.getenv("GEMINI_VOICE", "Aoede"),
            system_instruction=build_system_instruction(customer),
            # No thinking config: on Vertex, gemini-3.8-live rejects it for this
            # project ("not allowlisted to customize the thinking level").
            # Cost: cap each reply (~25 audio tokens per second of speech) so a
            # rambling answer can't run up output tokens.
            max_tokens=MAX_REPLY_TOKENS,
            # Cost: the whole context is re-counted as input on every turn.
            # Past this size, Gemini drops the oldest turns (the system prompt
            # stays). A normal 1-2 minute call never reaches it.
            context_window_compression=ContextWindowCompressionParams(
                enabled=True, trigger_tokens=CONTEXT_TRIGGER_TOKENS
            ),
            # Latency: let Gemini's server-side VAD end the customer's turn
            # quickly so the agent starts replying right after they stop talking.
            vad=GeminiVADParams(
                start_sensitivity=StartSensitivity.START_SENSITIVITY_HIGH,
                end_sensitivity=EndSensitivity.END_SENSITIVITY_HIGH,
                prefix_padding_ms=100,
                silence_duration_ms=300,
            ),
        ),
    )


def resolve_customer(runner_args: RunnerArguments) -> dict | None:
    """Work out which customer this call is for.

    - Browser (WebRTC): the client sends `{"customer_id": ...}` as requestData,
      exposed as `runner_args.body`.
    - Otherwise (e.g. the /client dev UI): the most overdue customer in the queue.
    """
    customer_id = None
    # [Plivo disabled — pending client decision on telephony]
    # Phone (Plivo): /plivo/answer recorded which customer the call UUID is for;
    # the runner exposes the UUID as `runner_args.call_data.call_id`.
    # if runner_args.call_data is not None and runner_args.call_data.call_id:
    #     customer_id = database.get_customer_id_for_call(runner_args.call_data.call_id)
    #     if customer_id is None:
    #         logger.warning(f"No customer recorded for call {runner_args.call_data.call_id}")
    #     (and change the `if` below to `elif`)
    if isinstance(runner_args.body, dict):
        customer_id = runner_args.body.get("customer_id")
    if customer_id:
        customer = database.get_customer(int(customer_id))
        if customer and customer["vehicles"]:
            return customer
        logger.warning(f"Unknown customer_id {customer_id}")

    due = database.get_due_services(within_days=database.MAX_DAYS_AHEAD)
    if due:
        return database.get_customer(due[0]["customer_id"])
    customers = database.get_all_customers()
    return database.get_customer(customers[0]["id"]) if customers else None


# Transport parameters used by the Pipecat runner. For now only WebRTC (the
# browser); Plivo phone calls are commented out below until the client decides
# on a telephony provider.
#
# Latency: no local Silero VAD / Smart-Turn endpointer. Gemini Live already does
# server-side VAD, endpointing, and barge-in.
def _webrtc_params() -> TransportParams:
    return TransportParams(audio_in_enabled=True, audio_out_enabled=True)


# ---------------------------------------------------------------------------
# [Plivo disabled — pending client decision on telephony]
# To enable: uncomment this block, the Plivo imports at the top, the phone
# branch in resolve_customer, the "plivo" transport in bot(), dialer.py, the
# phone_calls helpers in database.py and /api/calls/{id} in api.py. Then run
# `python bot.py -t plivo -x $PUBLIC_HOST`.
# ---------------------------------------------------------------------------
# def _plivo_params() -> FastAPIWebsocketParams:
#     # add_wav_header and the Plivo serializer (which needs PLIVO_AUTH_ID and
#     # PLIVO_AUTH_TOKEN to hang up) are set by create_transport.
#     return FastAPIWebsocketParams(audio_in_enabled=True, audio_out_enabled=True)
#
#
# @app.api_route("/plivo/answer", methods=["GET", "POST"])
# async def plivo_answer(request: Request, customer_id: int) -> Response:
#     """Plivo answer URL for outbound calls placed by dialer.py.
#
#     Plivo sends the call details when the customer picks up: as query params for
#     GET (what dialer.py uses, as in Plivo's Pipecat guide) or form-encoded for
#     POST. Record which customer this call UUID is for, then stream audio to /ws.
#     """
#     call_uuid = request.query_params.get("CallUUID")
#     if not call_uuid and request.method == "POST":
#         form = parse_qs((await request.body()).decode())
#         call_uuid = (form.get("CallUUID") or [None])[0]
#     if call_uuid:
#         database.register_phone_call(call_uuid, customer_id)
#         logger.info(f"Plivo call {call_uuid} answered for customer {customer_id}")
#     else:
#         logger.warning("Plivo answer webhook without a CallUUID")
#     host = os.getenv("PUBLIC_HOST") or request.url.hostname
#     return Response(dialer.build_stream_xml(host), media_type="application/xml")


async def run_bot(transport, customer: dict, is_webrtc: bool) -> None:
    """Wire up the pipeline for a connected transport and run it."""
    call = ServiceCall(customer)
    llm = _build_llm(customer)
    call.register(llm)

    context = LLMContext()
    # realtime_service_mode=True: Gemini Live is a speech-to-speech service that
    # emits its own transcripts, so the aggregators adapt to that flow.
    aggregators = LLMContextAggregatorPair(context, realtime_service_mode=True)

    async def start_conversation():
        context.add_message(
            {"role": "user", "content": GREETING_KICKOFF.format(name=customer["name"])}
        )
        await task.queue_frames([LLMRunFrame()])

    processors = [transport.input()]
    observers = []
    if is_webrtc:
        # RTVI: the browser Pipecat client waits for "bot-ready" before it
        # considers itself connected, and receives live transcripts through it.
        rtvi = RTVIProcessor(transport=transport)
        processors.append(rtvi)
        observers.append(RTVIObserver(rtvi))
    processors += [aggregators.user(), llm, transport.output(), aggregators.assistant()]

    task = PipelineTask(
        Pipeline(processors),
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=observers,
    )

    if is_webrtc:

        @rtvi.event_handler("on_client_ready")
        async def on_client_ready(rtvi):
            logger.info(f"Browser ready — calling {customer['name']}")
            await rtvi.set_bot_ready()
            await start_conversation()

    else:

        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            logger.info(f"Phone answered — calling {customer['name']}")
            await start_conversation()

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Call with {customer['name']} disconnected")
        if not call.outcome_logged:
            database.log_call(
                customer["id"],
                "no_answer",
                vehicle_id=customer["vehicles"][0]["id"],
                notes="Call ended before an outcome was recorded.",
            )
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


async def bot(runner_args: RunnerArguments) -> None:
    """Entry point invoked by the Pipecat runner for each session."""
    transport = await create_transport(
        runner_args,
        {
            "webrtc": _webrtc_params,
            # "plivo": _plivo_params,  # Plivo disabled — pending client decision
        },
    )
    customer = resolve_customer(runner_args)
    if not customer:
        logger.error("No customer to call — the customers table is empty.")
        return
    logger.info(f"Outbound service call to {customer['name']} ({customer['mobile_number']})")
    await run_bot(
        transport,
        customer,
        is_webrtc=isinstance(runner_args, SmallWebRTCRunnerArguments),
    )


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
