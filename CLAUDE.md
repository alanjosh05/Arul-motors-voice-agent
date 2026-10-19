# Arul Motors service voice agent: project context for Claude Code

Read this before changing the voice pipeline. Last updated 2026-10-18, when the
agent moved from AI Studio to Vertex AI. It was adapted from the hospital
agent's (MediAgent) handoff notes; hospital-only content was dropped.

## What this is

An outbound follow-up voice agent for **Arul Motors**, a Yamaha sub-dealer
showroom in Nazareth, Thoothukudi district, Tamil Nadu. The showroom sells
Yamaha bikes and services them. The agent calls customers who bought a bike
there, says their next service is due, and asks **which date** they will bring
the bike in (date only, no time slots). That date is stored in SQLite.

- Speaks natural Tanglish with everyday interjections. Has no personal name:
  "Nazareth Arul Motors Yamaha showroom-ல இருந்து call பண்றோம்".
- Call flow: confirm it's the right customer → `get_current_datetime` (IST) →
  service due → collect the date → log the outcome → goodbye and hang up.
- Customers can create, check, update and cancel their appointments, and
  search past and upcoming bookings by voice. See `README.md` for tools and rules.
- Built from the hospital agent in `../backend` (same Pipecat + Gemini Live pattern).

## Architecture

| Component | Role |
|---|---|
| Pipecat 1.12 | Orchestrator: audio ↔ Gemini, turn-taking, barge-in, tool calls (`bot.py`) |
| Gemini Live on Vertex AI | Speech-to-speech agent (`gemini-3.8-live`, `us-central1`). Listens, decides, speaks, calls tools |
| Database (`database.py`) | **SQLite** (WAL mode) on the VM, shared by bot and API. Customers, bikes, service appointments, call logs. Showroom clock is IST. MySQL support is built in but unused (`MYSQL_HOST`) |
| FastAPI (`api.py`) | CRM API on the VM at `backend.arulmotors.com`: staff logins (`auth.py`, scrypt + HMAC tokens, `AUTH_SECRET`), CRUD for customers/bikes/bookings, `/api/stats`, call relay `/api/voice/offer` → bot |
| Service types | `service_types` table (seeded with First/Second/Third free, Paid, General check-up), managed in the CRM's Service types page; bikes/bookings store the type name, renames cascade |
| Performance | Indexes (`database.INDEXES`); event-versioned JSON cache + ETag/304 + gzip in `api.py`; DB endpoints are sync `def` (thread pool). **Run one API process** (hub/cache/throttle are in-process). Frontend lazy-loads Pipecat, recharts, ⌘K and dialogs |
| Live updates | `events` + `voice_calls` tables written by bot and API; `api.py` streams new events over WebSocket `/api/ws?token=` (`LiveHub`, 0.75 s poll); frontend `components/live-provider.tsx` |
| CRM frontend (`../frontend`) | Next.js 16 + TypeScript + shadcn/ui (Nova/Radix), static export → GoDaddy (`crm.arulmotors.com`). Starts agent calls in the browser via the API relay |
| DNS | **Cloudflare** (not GoDaddy): `voice.`, `backend.` → VM static IP 34.93.71.71, DNS only |
| Hosting (`deploy/`) | **Whole backend on one GCP VM** in `asia-south1`: Caddy (HTTPS, basic auth on voice), systemd services `arul-voice` + `arul-api`, daily disk snapshots (14 days). VM service account has `roles/aiplatform.user` (ADC, no keys) |
| Transport | Browser/WebRTC only (`/client` on port 7860). **Plivo is commented out** pending the client's decision |

## Gemini on Vertex AI (done 2026-10-18)

- **Project:** `project-2c317546-b2c6-4a7a-8d4` ("My First Project", org `arulmotorsnazareth-org`)
- **Region:** `us-central1`. **Model:** `gemini-3.8-live`. **Voice:** Aoede.
- **Billing account:** `012671-BA0D6F-B8CAD7`. The console still showed **free trial** on 2026-10-19 (₹16 of ₹28,797 credit used, expires 17 Jan 2027), so the upgrade to a paid account has not taken effect yet. Upgrade before the trial ends, or resources stop.
- **Auth:** Application Default Credentials. The org policy **disallows API
  keys**; never add `GOOGLE_API_KEY` back or create a key.
- Defaults live in `bot.py` and can be overridden with `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_CLOUD_LOCATION`, `GEMINI_MODEL` and `GEMINI_VOICE`.

### Pipecat facts (verified against the installed 1.12.0)

- The class is `GeminiLiveVertexLLMService` in
  `pipecat.services.google.gemini_live.vertex.llm`. The old notes'
  `...gemini_live.llm_vertex` path does not exist.
- Pass the model and voice in `settings=GeminiLiveVertexLLMService.Settings(...)`;
  the `model=` and `voice_id=` init args are deprecated.
- Pass no credentials, and Pipecat falls back to ADC. Don't pass the ADC file as
  `credentials_path`: that argument only accepts service-account key files.
- **Thinking config must stay off.** Vertex returns "Current project is not
  allowlisted to customize the thinking level for this model" if
  `thinking=ThinkingConfig(...)` is set. The hospital agent's
  `thinking_budget=0` is therefore not used.
- The VAD settings (high start/end sensitivity, 100 ms prefix, 300 ms silence)
  are accepted on Vertex.
- In my tests the model connected, spoke the greeting through Pipecat, and
  called the bot's tools (`get_current_datetime`, `get_service_appointments`).

### Why this model and region

Live voice models tested on Vertex for this project (from `~/Downloads/test_live_models.py`):

| Region | gemini-3.1-flash-live-preview | gemini-3.8-live | gemini-live-2.5-flash-native-audio |
|---|---|---|---|
| asia-south1 (Mumbai) | not available | not available | not available |
| asia-southeast1 (Singapore) | not available | not available | not available |
| asia-northeast1 (Tokyo) | not available | not available | not available |
| europe-west4 (Netherlands) | not available | not available | OK, 1,033 ms first audio |
| us-central1 (Iowa) | not available | **OK, 648 ms first audio** | OK, 918 ms first audio |

- `gemini-3.1-flash-live-preview` (the old AI Studio model) isn't on Vertex anywhere.
- Re-test of `gemini-3.8-live` with the bot's real prompt, without the thinking
  config: 720–790 ms to first audio over 4 runs, plus about 2 s to open the
  session. All runs were from a Mac in Tamil Nadu.
- Consequence: customers' voices are processed in the US. The database stays
  wherever the bot is hosted. Tell the client.

### Credentials

- **Local Mac:** run `gcloud auth application-default login` (account
  `arulmotorsnazareth@gmail.com`). It writes
  `~/.config/gcloud/application_default_credentials.json`; quota project = the project above.
- **Server (later):** Workload Identity Federation, no key files. If something
  demands a service-account JSON, stop and raise it, because the org policy may
  block key downloads.
- Never commit credentials. `.gitignore` covers `*.json` and `application_default_credentials.json`.

## Local environment

- Use the conda env **`deeplearning`**: `conda run -n deeplearning python ...`.
  Pipecat, google-genai and the tests are installed there. (`~/mediagent-venv`
  belongs to the hospital project; don't use it.)
- `gcloud` is installed via Homebrew and logged in, with the project set.
- Run: `python bot.py` (open http://localhost:7860/client); `python api.py`; tests: `python -m pytest`.

## Cost reference (checked 2026-10-18, at $1 = ₹94.97)

- **Gemini Live rates (gemini-3.8-live, per Google's AI Studio pricing page):**
  audio in $0.005/min ($3/1M tokens), audio out $0.018/min ($12/1M tokens),
  text in $0.75 per 1M tokens. Not yet confirmed on Vertex's own pricing page.
- **Measured on Vertex:** the prompt plus tool definitions is about 2,740 text
  tokens. Google's usage counts show the whole context (prompt and earlier
  audio) re-counted on every turn: 2,740 → 2,934 → 3,101 tokens over 3 turns.
  Agent speech is about 25 audio tokens per second.
- **Gemini per 1.5-minute call, about 10 turns:**
  - Counted once: about $0.028 (₹2.6).
  - Re-counted every turn: about $0.073 (₹6.9). Budget at this figure until a
    real bill shows otherwise.
- **Plivo, if chosen:** ₹0.60/min outbound local, billed per full minute
  (1.5 min → 2 min ≈ ₹1.20), plus ₹250/month to rent a number.
- **Total per connected call:** about ₹3.8–8.1 with Plivo. The showroom's
  monthly volume isn't known yet.
- **Verify:** make about 5 test calls, then check Cloud console → Billing →
  Reports, filtered to Vertex AI and grouped by item.
- **Token optimisation (done 2026-10-18):** prompt plus tools went from 2,740
  to about 1,220 tokens. Tool results were shortened, search returns up to 3
  rows, replies are capped at 400 tokens, and old turns are dropped after
  6,000 context tokens. Same scripted 5-turn call on Vertex: text input
  21,372 → 12,117 tokens, Gemini cost ₹2.90 → ₹2.12 (−27%). Audio is now
  most of the cost, so keep replies short. When editing the prompt, keep it
  compact: Tamil script costs several tokens per word.

## Telephony (Plivo, commented out, pending the client)

All Plivo code is marked `Plivo disabled — pending client decision on
telephony` in `bot.py`, `dialer.py`, `api.py`, `database.py` and the tests. If
enabled:

- Choose Plivo's **India data region at signup**; it can't be changed later, and Indian numbers require it.
- Business KYC needs a Certificate of Incorporation (or the proprietorship
  equivalent) and a GST certificate, plus an Indian credit card.
- Outbound caller ID must be a Plivo-rented Indian number.
- Host the Pipecat server in India (e.g. AWS Mumbai). TRAI media-anchoring
  rules keep India call traffic on Indian servers.
- No cold calling. Get the customer's consent to service calls, e.g. at bike
  purchase, and register with DLT/TRAI.

## Open items before calling real customers

1. Client decision on telephony (Plivo or other), then account, KYC, number and DLT registration.
1a. Click **Upgrade** in the Cloud console (account still on free trial).
2. Budget alert on the billing account (alerts only, credits excluded). It now bills real money.
3. Customer consent for calls; DPDP Act 2023 handling of names and mobile numbers.
4. Production hosting (India) with Workload Identity Federation for Vertex.
5. Revoke the old AI Studio API key in AI Studio. It has been removed from `.env` and the backup copy deleted.
6. Deploy the CRM: build `../frontend`, upload `out/` to GoDaddy, set `CORS_ORIGINS`, create the first admin (`api.py create-user`).

## Rules for working on this code

- Keep the agent prompt short (it's re-read every turn: 932 tokens with tools). Measure with Vertex `count_tokens` before and after edits. Guardrails (STRICT SCOPE) and `CallGuard` hang-up rules must stay.

- `database.py` must keep working on both SQLite and MySQL: write SQL with `?`
  placeholders (converted for MySQL), keep dates as `YYYY-MM-DD` strings, put
  new column types in `_schema()`, and return rows through `_plain()`. Run the
  tests against MySQL as well (see README).

- No raw call audio is written to disk; only tool-captured structured outcomes are stored.
- The agent never invents dates, service history or details not in the
  database. It asks for the date only; there are no time slots.
- "Today" always comes from the `get_current_datetime` tool (IST), never from the prompt.
- Tools are bound to the customer on the call. Other customers' bookings are
  visible only with the full registration or mobile number, are read-only, and
  mobile numbers are never read out.
- Keep Plivo commented out until the client decides.
- Use the seeded demo customers until the open items above are closed.
