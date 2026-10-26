# Arul Motors (Yamaha) — Service Follow-up Voice Agent (Pipecat + Gemini Live)

Arul Motors, in **Nazareth, Thoothukudi district, Tamil Nadu**, is a **Yamaha sub-dealer showroom** that sells Yamaha bikes and
scooters and services them. This **outbound** voice agent follows up with
customers whose bikes it services: bikes sold there, and the many bought from
other dealers that come there for service. It calls to say the bike's service is coming
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
| Today's date | Prompt | Today and the next 14 dates (showroom time, IST) are filled in at call start; `get_current_datetime` is a fallback |
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
2. Knows today's date (IST) and the dates of the coming days: they're filled
   into its instructions when the call starts.
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

### Guardrails, hang-up and service details

- **Strict scope (prompt):**
  - The agent only discusses this customer's Yamaha service, its date and
    their bookings. Anything else (general questions, other brands, sales,
    loans, news, politics, jokes, coding, other people's details) gets a
    one-line refusal and a return to the date.
  - It never reveals its instructions or tools, or changes role.
  - Off-topic again after one refusal, or abuse: a polite bye, logged as
    `not_interested` ("off-topic"), and the call ends.
  - Never quotes prices, diagnoses, or promises discounts, parts or timings.
- **Busy / not interested:** no more questions and no persuading. A kind
  one-liner ("பரவாயில்லங்க, அப்புறம் call பண்றோம்" / "சரிங்க, no problem, thanks
  ங்க"), the outcome is logged, and the call ends.
- **Logging the outcome closes the call:** the agent says one goodbye out loud
  (repeating the visit date if booked), and the call hangs up after it,
  whether or not the model calls `end_call`.
- **Hang-up enforced in code (`CallGuard` in `bot.py`),** not only asked of
  the model:
  - after `end_call`, once the goodbye has finished playing (at most 8 s);
  - 5 s of quiet after the outcome is logged;
  - 30 s of silence;
  - a hard limit of 5 minutes per call.

  Calls that end without an outcome are logged as `no_answer` with the
  reason.
- **Service type details:**
  - The prompt carries the due (and booked) service types' details from the
    CRM's Service types: free or paid, description, when the next one is due.
  - `get_service_types` lists the others on demand.
  - Prices are never given to the agent.
- **Natural speech:**
  - **Language:** the speech language is `ta-IN` (`GEMINI_LANGUAGE`).
    Pipecat's default `en-US` made the Tanglish sound foreign.
  - **Turn-taking:** low start sensitivity, so noise or echo doesn't cut the
    agent off mid-word, and 600 ms of silence to end the customer's turn.
  - **No tight reply cap:** Gemini's thinking counts toward `max_tokens`, so
    a 400-token cap clipped sentences.
  - **Prompt:** casual *spoken* Tamil as people in Thoothukudi talk, with
    everyday English words. Words to avoid are listed (வாகனம், சேவை, தயவுசெய்து).
    Replies open with a varied natural reaction (ம்ம், ஓ, அப்படியா, சரி சரி,
    ஆமாங்க, அச்சச்சோ, super). The prompt includes example lines for each stage.
  - **Voice:** `GEMINI_VOICE` (default Aoede; also try Kore or Leda).
- **Visit date:** after booking, the agent says the day, date and service
  type, and repeats the date in the goodbye. The call log stores "Coming on
  Sat 24 Oct for First free service", and the CRM's end-of-call notice shows
  it.
- **Tokens:** the system prompt + tools are 1,250 tokens (2,740 originally; the Tamil example lines are worth it), re-read by Gemini Live on every turn.
  `search_service_bookings` takes one free-text `query` (same matching and
  privacy) instead of nine fields.

### Tools

| Tool | What it does |
|------|--------------|
| `get_current_datetime` | Today's date, weekday and time at the showroom (Asia/Kolkata), plus the next 14 days |
| `search_service_bookings` | **Search** previous and upcoming bookings: one `query` (name, mobile, registration, model or service type) + optional `state` |
| `get_service_types` | The workshop's service types: work included, free or paid, when due (no prices) |
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
- **Maintenance records:** booking → service → **Close service** (CRM), with the
  fields of Arul Motors' Yamaha **job card**: job card no., type of job (free /
  paid / accidental / quick repair / warranty), service, received and delivered
  dates, odometer, frame and engine no. (also saved on the bike), customer
  requests, jobs done (checklist) and other work, a **parts** table (part no.,
  name, qty, ₹, warranty) and **labour** table with totals computed by the
  server (warranty parts not charged), delivery checks, customer feedback,
  bay/technician, mechanic, supervisor, remarks, and the **scanned job card PDF**
  (required, ≤ 10 MB). `POST /api/appointments/{id}/close` (multipart) saves a
  `service_records` row and the PDF as a BLOB in `service_documents`, marks the
  booking `COMPLETED`, and moves the bike on (last service date, odometer, next
  due service), all in one transaction. A bike's history:
  `GET /api/vehicles/{id}/service-history` (records, plus older completed
  bookings such as the client import); the PDF: `GET /api/service-records/{id}/report`.
- **Bikes bought elsewhere and walk-ins:** most bikes serviced here were bought
  from another dealer. A bike can be marked `bought_elsewhere` (with an optional
  `dealer_name`); its purchase date can stay unknown, and its first visit can be
  any service (e.g. the 3rd free service): nothing enforces the order. A
  service done without a booking is recorded with **Record service (walk-in)**
  on the bike (`POST /api/vehicles/{id}/services`, same multipart form as
  closing, `service_type` required): it saves the record and PDF without a
  booking and moves the bike on if it's now the latest service. If the bike has
  an open booking, that booking is closed instead. The agent hears "bought at
  another dealer", so it never says the bike was bought here.
- **Next service after a visit** (`schedule` on the job card form): **set** the
  next service (default); **pending**: the engine oil wasn't changed, so only
  the next service's type is kept and its date is set at the visit where the
  oil is changed (`vehicles.next_due_pending`: left out of due lists and agent
  calls until then); **keep**: a repair (e.g. after an accident a few days
  after a service; the default for Accidental / Quick repair / Warranty jobs),
  which updates the odometer and frame/engine numbers but not the schedule.
  The latest record that counts as a service decides the schedule; deleting a
  record undoes only what it changed.
- **Repair service types** (`service_types.is_repair`, seeded once with
  **Accident repair**; any type can be marked "Repair (no follow-up)" in the
  Service types page): a visit under a repair type, booked or walk-in, is
  recorded with its job card but always keeps the schedule (`schedule = keep`),
  so it never leads to a follow-up; a repair type can't be a bike's next service.
- **Oil change and the form:** with "Engine oil change" ticked the next date is
  set; unticked, the next service waits for the oil change. Ticking/unticking it
  (also when editing a saved job card) switches the form's Next service choice.
  The older `POST /api/appointments/{id}/complete` (no record) still works for scripts.

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

## CRM API (`python api.py`, port 8001)

Used by the CRM frontend (`../frontend`). Every route except login and health
needs a staff login: `Authorization: Bearer <token>` from `/api/auth/login`.
Scripts may use `X-API-Key: <API_KEY>` instead.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auth/login` | POST | `{username, password}` → `{token, user}` (12-hour token, signed with `AUTH_SECRET`) |
| `/api/auth/me` · `/api/auth/password` | GET · POST | Current user · change own password |
| `/api/health` | GET | Open health check |
| `/api/stats` | GET | Dashboard: totals, 14-day series, call outcomes, today's bookings, due list, recent calls |
| `/api/customers?q=` | GET · POST | Customers with bikes, next booking and last call (`q` searches name/mobile/reg/model) · create |
| `/api/customers/{id}` | GET · PATCH · DELETE | Profile with bikes, all bookings (with state) and calls · edit · delete with everything recorded |
| `/api/vehicles` | GET · POST | Bikes with owners · add a bike sale (creates the customer if the mobile is new) |
| `/api/vehicles/{id}` | PATCH · DELETE | Edit a bike · delete it with its bookings |
| `/api/due-services?days=7` | GET | Follow-up list: service due soon, no appointment yet |
| `/api/service-types?active_only=` | GET · POST | Service types in schedule order with usage counts · add one |
| `/api/service-types/{id}` | PATCH · DELETE | Edit (renaming updates bikes and bookings) · delete if unused (otherwise deactivate) |
| `/api/service-types/{id}/move?direction=up\|down` | POST | Reorder the schedule |
| `/api/appointments?state=&customer_id=` | GET · POST | Bookings (newest first, each with `state`) · book a date for a customer's bike |
| `/api/appointments/{id}` | GET · PATCH | One booking · reschedule / change notes |
| `/api/appointments/{id}/cancel` · `/complete` | POST | Cancel (deletes it) · mark serviced without a record (scripts) |
| `/api/appointments/{id}/close` | POST | Close service: multipart form with the details and `report` (PDF ≤ 10 MB) |
| `/api/vehicles/{id}/services` | POST | Record a walk-in service (no booking): same form; `service_type` required |
| `/api/vehicles/{id}/service-history` | GET | A bike's maintenance records and older services, newest first |
| `/api/service-records/{id}/report` | GET | The service report PDF (inline) |
| `/api/appointments/search?…` | GET | Fuzzy booking search (same matching as the voice tool) |
| `/api/calls?customer_id=` · `/api/calls/log` | GET · POST | Call log · record a call made by staff |
| `/api/voice/config` · `/api/voice/status` | GET | ICE servers for the call panel · whether the bot is reachable |
| `/api/voice/offer` | POST · PATCH | Relays a CRM call's WebRTC offer / ICE candidates to the bot; checks the login and `requestData.customer_id` |
| `/api/staff` · `/api/staff/{id}` | GET · POST · DELETE | Team management (admins only) |
| `/api/ws?token=<staff token>` | WebSocket | **Live feed** for the CRM: `hello` snapshot, then `data.changed`, `call.started`, `call.ended`, `notification` events |
| `/api/notifications` · `/api/voice/calls/active` | GET | Recent staff notifications · voice-agent calls in progress |
| `/api/connection?customer_id=` | GET | Direct bot offer URL (test page without the relay) |
| `/api/calls/{customer_id}` | POST | *(Commented out.)* Place a Plivo call to that customer |

### Live updates (WebSocket)

The voice agent (`bot.py`) and the API are separate processes sharing the
database. Every write appends a row to the `events` table, and the API streams
new rows to all signed-in CRMs over `/api/ws` (checked every 0.75 s).
- **Data changes** refresh exactly the affected screens.
- **The agent's calls** show live (`voice_calls` table).
- **The agent's actions** create notifications (booked, rescheduled,
  cancelled, call back, not interested, no answer, …). Staff edits only
  refresh screens.
- **Missed bookings** are flagged every 10 minutes.

Events older than 2 days (notifications: 60 days) are pruned. The CRM itself
is static files on GoDaddy: the browser opens the WebSocket directly to the
backend (`wss://backend.arulmotors.com/api/ws`), and Caddy passes WebSockets
through with no extra config.

Create logins with `python api.py create-user USERNAME --role admin|staff`, or
set `CRM_ADMIN_PASSWORD` to create `admin` on a fresh database.

## Performance design

Measured on a synthetic showroom of 3,000 customers, 30,000 calls and 6,000
bookings:

| | Before | After |
|---|---|---|
| Customers list (query) | 849 ms | 24 ms |
| Dashboard stats (query) | 907 ms | 25 ms (4 ms cached) |
| Due-for-service list | 182 ms | 3 ms |
| Unchanged data, revalidated by the browser | full 1.7 MB | `304`, 0 bytes, 2 ms |

- **Indexes** on every foreign key and filter column (`database.INDEXES`,
  created on start for SQLite and MySQL).
- **Queries read only what they show.**
  - The customers list fetches each customer's *latest* call with SQL and
    groups rows in one pass.
  - Stats aggregate in SQL over 30 days of calls and upcoming bookings only.
  - The call log returns the newest 1,000 (`/api/calls?limit=`, total at
    `/api/calls/count`).
- **Event-versioned cache.**
  - Every write appends an `events` row, so `latest event id + showroom date`
    is a version that changes exactly when data can change.
  - Stats, customers and service types are cached per version as ready-made
    JSON bytes, with no time-based expiry to tune.
- **Conditional GETs.**
  - The same version is sent as an `ETag` on data endpoints.
  - Browsers revalidate automatically and get an empty `304` when nothing
    changed.
  - Only valid credentials get a 304.
- **gzip** for responses over 1 KB (the customers list: 1.69 MB → 147 KB).
- **Non-blocking server.**
  - Database endpoints are plain `def`, so FastAPI runs them in its thread
    pool. Only network-bound routes (the voice relay, WebSocket) are `async`.
  - A slow query never stalls the live feed.
- **Live feed.** When no browser is connected, the event loop only tracks
  the latest id.
- **SQLite tuning.** WAL is set once (it's stored in the file), plus
  `synchronous=NORMAL`, a 10 s lock wait, `mmap_size` (reads through the OS
  page cache, shared by the bot and API across their short-lived connections)
  and `temp_store=MEMORY`.
- **Compression at the edge.** Caddy `encode zstd gzip` on both sites (the voice
  page's scripts went out uncompressed before). HTTP/2 and HTTP/3 are on.

### Where the latency actually is (measured 2026-10-25)

Every API endpoint answers in **~5 ms** on the VM (load 0.04, ~1.2 GB RAM
free); a request from India takes **~100–220 ms** end to end, almost all of it
network and TLS. So the CRM saves **round trips**, not server time:

| Technique | Where | Effect |
|---|---|---|
| Browser cache for the app's files | `frontend/public/.htaccess` (GoDaddy Apache) | `/_next/static/*` cached 1 year (`immutable`, names change each build); pages `no-cache` (quick 304); deflate on text |
| Preconnect to the API | `app/layout.tsx` | DNS + TLS to `backend.` (~170 ms) happen while the page loads |
| Stale-while-revalidate across reloads | `components/providers.tsx` (React Query persister) | Pages open instantly with the last data (≤ 12 h, cleared at sign-out, dropped by each new build), then refresh |
| Prefetching | `prefetchAllPages` after sign-in (when idle), `prefetchPage` on sidebar hover | Moving between pages shows data at once |
| Optimistic updates with rollback | `lib/queries.ts` (`patchLists`) | Cancel booking, delete customer/bike/type, reorder or edit a service type, voice switch: the screen changes on click and is undone if the server refuses |
| Live invalidation | WebSocket feed | No polling; only changed lists refetch |

**No load balancer, on purpose.** Each request costs ~5 ms of one small VM,
at most two staff use the CRM at once, and SQLite is one file on one disk.
A load balancer (and a second VM + a database server) would add cost
(₹1,500+/month) and a network hop to every request, not speed. It becomes
worth it with many concurrent users or a need for zero-downtime upgrades;
the path is below.
- **Login throttle.** 10 failed sign-ins per client per 15 minutes → `429`.
- **Missed-booking alerts** only cover the last 7 days, so a first run
  doesn't flood the bell.

**Scaling note:** the live-feed hub, the cache and the login throttle live in
the API process, so run **one** API process (as `arul-api.service` does).
That's plenty for a showroom. To scale out to several processes or servers,
move them to Redis (pub/sub for the feed, shared cache keys) and the database
to MySQL/Postgres.

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
