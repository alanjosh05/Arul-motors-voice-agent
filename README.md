# Arul Motors (Yamaha) — Service Follow-up Voice Agent (Pipecat + Gemini Live)

Arul Motors, in **Nazareth, Thoothukudi district, Tamil Nadu**, is a **Yamaha sub-dealer showroom** that sells Yamaha bikes and
scooters and services them. This **outbound** voice agent follows up with
customers who bought a bike there. It calls to say the bike's service is coming
up and asks **which date** they will bring it in. There are no time slots.

The agent has **no personal name**. It just says it is calling from *Arul
Motors Yamaha showroom, Nazareth* and speaks casual **Tanglish** like a normal person,
with everyday interjections ("ம்ம்", "சரி சரி", "அச்சச்சோ", "super"…).

It is built the same way as the hospital agent in `../backend`: Pipecat plus
Google's **Gemini Live** speech-to-speech model, with the same aggressive
server-side VAD tuning. Unlike the hospital agent it runs on **Vertex AI**
(`gemini-3.8-live` in `us-central1`) and signs in with Application Default
Credentials. There is no API key, because the Google Cloud org policy blocks them.

> **Telephony is switched off for now.** The agent runs in the browser only.
> Plivo phone-call support is written but commented out (look for
> `Plivo disabled — pending client decision on telephony`) until the client
> confirms a telephony provider.

## How it differs from the hospital agent

| | Hospital (EDITH) | Arul Motors |
|---|---|---|
| Direction | Inbound: patient calls in | **Outbound**: the showroom calls the customer |
| Identity | Named assistant "EDITH" | No name: "Nazareth Arul Motors Yamaha showroom-ல இருந்து call பண்றோம்" |
| What it books | Doctor, date **and time** | **Only the date** the customer will bring the bike in |
| Appointments by voice | Book / reschedule / cancel | Full CRUD: create, check, update (date or notes), cancel |
| Today's date | Tool | Always from `get_current_datetime` (showroom time, IST); never baked into the prompt |
| Context | Collects everything from the caller | Already knows the customer, bike, purchase date and due service (injected into the prompt per call) |
| Tool scope | Any appointment, looked up by name and mobile | Bound to the one customer on the call |
| Ending | Patient hangs up | Agent logs the call outcome, then hangs up (`end_call`) |
| Transport | Browser (WebRTC) | Browser (WebRTC) for now; Plivo phone calls commented out |
| Gemini | AI Studio API key, `gemini-3.1-flash-live-preview` | Vertex AI with ADC, `gemini-3.8-live` in `us-central1` |

## Files

| File | Purpose |
|------|---------|
| `bot.py` | The voice agent (entrypoint). Builds a per-customer system prompt and serves WebRTC (`/api/offer`, dev UI at `/client`) on port 7860. |
| `database.py` | Data layer (SQLite; optional MySQL via `MYSQL_HOST`): customers, bikes sold (purchase date, next service due), service appointments (CRUD), call logs, and the showroom clock (IST). |
| `deploy/` | Production setup: the whole backend (voice agent, API, SQLite) on one GCP VM; the frontend stays on GoDaddy. See `deploy/README.md`. |
| `api.py` | FastAPI server for a dashboard: follow-up list, service dates, call logs (port 8001). |
| `dialer.py` | *(Commented out.)* Plivo outbound dialer, kept for when telephony is enabled. |
| `tests/` | pytest suite for the data layer and bot wiring. |

## Call flow

1. "வணக்கம்ங்க! Nazareth Arul Motors Yamaha showroom-ல இருந்து call பண்றோம், Karthik Raja தானே பேசறீங்க?"
   If it's the wrong person, the agent logs `wrong_number` and hangs up.
2. Calls `get_current_datetime` so it knows today's date (IST) and the dates of
   the coming days.
3. Tells them their bike's service is due, e.g. the FZ-S Fi's first free service.
4. Asks **when** they'll bring the bike in, date only. If they say "Saturday" or
   "next Monday", the agent works out the exact date from the datetime tool and
   confirms it back with the day.
5. Saves it with `create_service_appointment` in the **`service_appointments`** table.
6. If the customer is busy, it records `callback_requested` (with a date). Other
   outcomes are `already_serviced`, `bike_sold` and `not_interested`.
7. Calls `log_call_outcome` (`date_confirmed` when a date was given), says bye,
   and calls `end_call`. If the line drops before an outcome is recorded, the
   call is logged as `no_answer`. Repeating the same outcome in one call is stored once.

### Tools

| Tool | What it does |
|------|--------------|
| `get_current_datetime` | Today's date, weekday and time at the showroom (Asia/Kolkata), plus the next 14 days |
| `search_service_bookings` | **Search** previous and upcoming bookings by name, mobile, registration, model, service type, state or date range |
| `create_service_appointment` | **Create**: date (required); optional bike, service type, notes |
| `get_service_appointments` | **Read**: the customer's upcoming appointments |
| `update_service_appointment` | **Update**: new date and/or notes |
| `cancel_service_appointment` | **Delete**: soft cancel; the row stays with status `CANCELLED` |
| `log_call_outcome` | Records how the call ended |
| `end_call` | Hangs up after the goodbye |

The customer can use any of these at any point in the call, e.g. "நான் எப்போ
வரேன்னு சொன்னேன்?", "அத Monday-க்கு மாத்துங்க", or "cancel பண்ணிடுங்க".
All of them are restricted to the customer on the call.

### Searching previous bookings

`search_service_bookings` answers questions like "last time எப்போ service
பண்ணேன்?", "போன தடவை என்ன service?" or "என் இன்னொரு bike TN 22 EF 9012-க்கு
booking இருக்கா?". Every filter is optional and they combine:

| Filter | Matching |
|--------|----------|
| `customer_name` | Partial and fuzzy, so spoken spellings work ("Karthick" finds "Karthik Raja") |
| `mobile_number` | Any part of the number ("3210") |
| `registration_number` | Spacing and case ignored, partial ("TN09AB1234", "1234") |
| `model` | Partial, punctuation ignored ("fzs" → "Yamaha FZ-S Fi", "R15") |
| `service_type` | Partial ("first free", "paid") |
| `state` | `upcoming`, `completed` (previous services), `cancelled`, `missed` (date passed, never serviced) |
| `date_from` / `date_to` | Appointment date range |
| `appointment_id` | Exact |

- With no name, mobile, registration or ID, it searches the caller's own
  bookings. Results come newest first; the agent hears up to 3.
- **Privacy:** another customer's bookings are shown only when the search
  includes that bike's full registration number or that customer's full mobile
  number. Otherwise the agent is told matches are hidden and asks for one.
  Mobile numbers are never read back. Search is read-only for other
  customers' bookings.
- **Service history:** each bike's last service is recorded once as a
  `COMPLETED` booking (from `last_service_date`). When the workshop finishes a
  job, `POST /api/appointments/{id}/complete` marks it completed, updates the
  bike's last service date and, optionally, sets its next due service.

### Rules (`database.py`)

- No time slots and no daily limit: any number of customers can pick the same day.
- No past dates, and nothing more than `MAX_DAYS_AHEAD = 30` days ahead.
- `CLOSED_WEEKDAYS` is empty (open every day). Set it to `{6}` if the workshop
  doesn't take bikes on Sunday.
- One upcoming appointment per bike (changes go through `update_service_appointment`).
- "Today" is always showroom time (`SHOWROOM_TZ`, default `Asia/Kolkata`), even
  if the server runs elsewhere.
- Older databases with a `service_visits` table are migrated automatically on startup.

## Setup (conda `deeplearning` env)

```bash
cd Arul-Motors
conda activate deeplearning
pip install -r requirements.txt
gcloud auth application-default login   # once; uses project project-2c317546-b2c6-4a7a-8d4
cp .env.example .env.local              # optional overrides only, no API key
```

### Gemini on Vertex AI

Token use is kept low because Gemini Live re-counts the prompt, tool
definitions and the conversation so far as input on **every turn**:

- Compact system prompt and tool definitions: about 1,220 tokens, down from 2,740.
- Short tool results (dates as `Mon 2026-10-19`, no repeated sentences); up
  to 3 search results.
- `MAX_REPLY_TOKENS = 400`: a hard cap of about 16 s of speech per reply.
- `CONTEXT_TRIGGER_TOKENS = 6000`: past this, Gemini drops the oldest turns.
  A normal 1–2 minute call never reaches it.

| Setting | Default | Env override |
|---|---|---|
| Project | `project-2c317546-b2c6-4a7a-8d4` | `GOOGLE_CLOUD_PROJECT` |
| Region | `us-central1` | `GOOGLE_CLOUD_LOCATION` |
| Model | `gemini-3.8-live` | `GEMINI_MODEL` |
| Voice | `Aoede` | `GEMINI_VOICE` |

- Auth is ADC: `gcloud auth application-default login` on a laptop, Workload
  Identity Federation on a server. Don't pass Pipecat's `credentials_path`,
  which accepts only service-account key files.
- Live voice models aren't offered in Indian regions. `us-central1` measured
  fastest, about 0.7–0.8 s to the first audio of a reply, so call audio is
  processed in the US. The database stays wherever the bot runs.
- Vertex rejects a custom thinking level for this project ("not allowlisted
  to customize the thinking level"), so the hospital agent's
  `thinking_budget=0` is not set here.

Four demo customers with Yamaha bikes (FZ-S Fi, Ray ZR 125, R15 V4, Fascino
125) are seeded on first run, with services due 2–7 days from today. Delete
`arul_motors.db` to reset.

## Run — browser test call

```bash
python bot.py        # open http://localhost:7860/client and click Connect
```

With no customer specified, the agent calls the customer whose service is due
soonest. To pick a customer, send `requestData: {"customer_id": 2}` with the
WebRTC offer. `GET /api/connection?customer_id=2` on the API returns exactly
that.

> The hospital bot also uses port 7860. To run both, start this one with
> `python bot.py --port 7861`.

## Later: real outbound phone calls (Plivo — currently commented out)

Not active yet; this is pending the client's decision on telephony. To enable
it, uncomment every block marked `Plivo disabled — pending client decision on
telephony` in `bot.py`, `dialer.py`, `api.py`, `database.py` and the tests.

This follows Plivo's [Pipecat integration guide](https://www.plivo.com/docs/voice-agents/audio-streaming/integration-guides/pipecat/overview)
(outbound flow).

1. Expose the bot publicly, e.g. `ngrok http 7860`, and put the domain (no
   `https://`) in `PUBLIC_HOST`.
2. Fill in `PLIVO_AUTH_ID`, `PLIVO_AUTH_TOKEN` and `PLIVO_PHONE_NUMBER` in `.env.local`.
   The auth values also let the bot hang up the call when the agent ends it.
3. Start the bot in Plivo mode and place calls:

```bash
python bot.py -t plivo -x $PUBLIC_HOST
python dialer.py --list           # see who is due for service
python dialer.py --customer 1     # call one customer
python dialer.py --due 7          # call everyone due within 7 days, 2 min apart
```

How a call is wired:

1. `dialer.py` calls Plivo's Call API with
   `answer_url=https://PUBLIC_HOST/plivo/answer?customer_id=N` (GET).
2. When the customer picks up, Plivo hits `/plivo/answer` with the `CallUUID`.
   The bot saves which customer that UUID belongs to (`phone_calls` table) and returns
   `<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">wss://PUBLIC_HOST/ws</Stream>`.
3. Plivo opens the `/ws` media stream. The bot reads the call UUID from
   `runner_args.call_data.call_id`, loads that customer, and starts talking.

Pipecat's Plivo handshake parser keeps only the stream ID and call ID, so the
customer is matched by call UUID instead of being passed on the WebSocket URL.

## API (`python api.py`, port 8001)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/customers` | GET | Customers and their bikes |
| `/api/vehicles` | POST | Add a bike sale (creates the customer if new) |
| `/api/due-services?days=7` | GET | Follow-up list: service due soon, no appointment yet |
| `/api/appointments` | GET | All service appointments (date each customer will come) |
| `/api/appointments/search?customer_name=&mobile_number=&registration_number=&model=&service_type=&state=&date_from=&date_to=` | GET | Search bookings (same matching as the voice tool, no privacy filter) |
| `/api/appointments/{id}/complete` | POST | Workshop: mark serviced; optional `{"next_service_date", "next_service_type"}` |
| `/api/calls` | GET | Call log with outcomes |
| `/api/calls/{customer_id}` | POST | *(Commented out.)* Place a Plivo call to that customer |
| `/api/connection?customer_id=` | GET | WebRTC offer URL plus `requestData` for a browser test call |

## Database

- **SQLite** file `arul_motors.db`, both locally and on the VM. It is shared by
  the voice agent and the API, with WAL mode so they can write at the same
  time. A new database gets 4 demo customers unless `SEED_DEMO=0`.
- **MySQL support** is built in but unused. Set `MYSQL_HOST`, `MYSQL_USER`,
  `MYSQL_PASSWORD` and `MYSQL_DATABASE` (optionally `MYSQL_PORT`,
  `MYSQL_SSL=on`, `MYSQL_SSL_CA`) if the data ever has to be shared between
  several servers.
- **API settings:**
  - When `API_KEY` is set, `api.py` requires it in the `X-API-Key` header.
  - `CORS_ORIGINS` lists the frontend sites allowed to call it from a browser.

## Deploying

The frontend stays on GoDaddy. The backend runs on one Google Cloud VM in
Mumbai: the voice agent at `voice.` and the API at `api.`, with HTTPS via Caddy
and daily disk-snapshot backups. See [`deploy/README.md`](deploy/README.md).

## Tests

```bash
python -m pytest
```

To also run them against MySQL, set `TEST_MYSQL_HOST`, `TEST_MYSQL_PORT`,
`TEST_MYSQL_USER`, `TEST_MYSQL_PASSWORD` and `TEST_MYSQL_DATABASE`.
**Every table in that database is dropped**, so use an empty test database.
