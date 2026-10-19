# Deploying Arul Motors

**Frontend on GoDaddy, the whole backend on one Google Cloud VM.**

New to GCP? Work through [GCP-LESSONS.md](GCP-LESSONS.md) first. It covers
each building block step by step, with its AWS equivalent.
If you already built the practice VM `arulmotors` there, deploy onto it
with **Lesson 9, Path A** instead of creating a new VM below.

```
                        GoDaddy                         Google Cloud VM (Mumbai)
                ┌──────────────────────┐        ┌──────────────────────────────────┐
Staff browser ─▶│ Frontend (dashboard) │──API──▶│ Caddy ─▶ api.py ─┐               │
                │ arulmotors.com       │  HTTPS │  (HTTPS)         ├─▶ SQLite file  │
                └──────────────────────┘        │       ─▶ bot.py ─┘               │
Voice page ──────────── HTTPS + WebRTC ────────▶│            │                     │
                                                └────────────┼─────────────────────┘
                DNS (GoDaddy):                               ▼
                voice.arulmotors.com ─▶ VM IP        Vertex AI (us-central1)
                api.arulmotors.com   ─▶ VM IP
```

| Piece | Where | Files |
|---|---|---|
| Frontend / dashboard website | GoDaddy hosting | Your frontend; it calls `https://api.YOURDOMAIN` |
| Voice agent (`bot.py`) | VM, `https://voice.YOURDOMAIN` | `arul-voice.service` |
| Dashboard API (`api.py`) | VM, `https://api.YOURDOMAIN` | `arul-api.service` |
| Database | VM: SQLite file `/opt/arul-motors/arul_motors.db` | Created on first start; daily disk snapshots |
| HTTPS | VM: Caddy, automatic certificates | `Caddyfile` |
| VM, IP, firewall, backups | Google Cloud | `gcp-create-vm.sh` |

## 1. Create the VM (on your Mac)

From the `Arul-Motors` folder:

```bash
bash deploy/gcp-create-vm.sh
```

This script:
- creates a service account that may only use Vertex AI, and runs the VM as it,
  so no key file is needed;
- reserves a **static IP** that never changes;
- opens TCP 80/443 and the UDP range for call audio;
- schedules **daily backups** (disk snapshots at 02:00 IST, kept 14 days);
- creates an `e2-small` Ubuntu 24.04 VM in Mumbai (2 shared vCPUs, 2 GB):
  enough for about 2 users at a time. Gemini does the heavy work on Vertex.
  Set `MACHINE=e2-medium` for more headroom; the size can be changed later.

It prints the VM's IP. In **GoDaddy → DNS**, add two **A records** pointing at that IP:

| Type | Name | Value |
|---|---|---|
| A | `voice` | VM IP |
| A | `api` | VM IP |

## 2. Install the backend on the VM

```bash
# Copy the code
gcloud compute ssh arul-voice --zone=asia-south1-a --command="mkdir -p ~/arul-motors"
gcloud compute scp --recurse --zone=asia-south1-a \
  bot.py database.py api.py requirements.txt deploy arul-voice:~/arul-motors/

# Install (on the VM)
gcloud compute ssh arul-voice --zone=asia-south1-a
sudo bash ~/arul-motors/deploy/vm-setup.sh voice.YOURDOMAIN.com api.YOURDOMAIN.com https://YOURDOMAIN.com
```

The three arguments are the voice domain, the API domain and your frontend's
address. The script:
- installs Python, Caddy and the code into `/opt/arul-motors`;
- creates `/opt/arul-motors/.env` with a random `API_KEY`;
- starts both services;
- asks for a username and password for the voice page.

Then:
- **See the API key:** `sudo cat /opt/arul-motors/.env`. Put it in the frontend.
- **First test call with demo customers:** set `SEED_DEMO=1` in `.env`, then
  `sudo rm /opt/arul-motors/arul_motors.db*` and
  `sudo systemctl restart arul-voice arul-api`. Open
  `https://voice.YOURDOMAIN.com/client`, log in, and click Connect.
  - Before real use, set `SEED_DEMO=0`, delete the database again and restart.
  - Real customers are added with `POST /api/vehicles`.
- **Logs:** `journalctl -u arul-voice -f` and `journalctl -u arul-api -f`.
- **Update the code later:** repeat the copy step and run `vm-setup.sh` again.
  The database and `.env` are kept.

## 3. Frontend on GoDaddy

Every request from the frontend goes to `https://api.YOURDOMAIN.com/api/...`
with the header `X-API-Key: <API_KEY>`, for example:

```js
fetch("https://api.YOURDOMAIN.com/api/due-services?days=7", {
  headers: { "X-API-Key": API_KEY },
});
```

The API only accepts browser requests from the addresses in `CORS_ORIGINS`
(set by `vm-setup.sh`). If the site also opens on `www.`, add that address as
well, comma-separated, then run `sudo systemctl restart arul-api`.

For test calls, link to `https://voice.YOURDOMAIN.com/client`. It asks for the
voice page password.

## Backups and restore

- Daily snapshots of the VM disk, which includes the database, are listed with
  `gcloud compute snapshots list`.
- **To restore:** create a disk from a snapshot, attach it to the VM, and copy
  `arul_motors.db` back. Or create a new VM from the snapshot.
- **Manual copy at any time:**
  `sudo sqlite3 /opt/arul-motors/arul_motors.db ".backup /tmp/arul-backup.db"`

## Security notes

- **Voice page:** it is behind Caddy's basic auth. Without it, anyone could
  start calls and run up Gemini charges.
- **API:** it requires `API_KEY` and only allows `CORS_ORIGINS` from browsers.
  - **Watch-out:** a key inside frontend JavaScript is visible to anyone who
    opens the page's source. It keeps casual visitors and bots out, but it is
    not a login. Before real customer data goes live, add staff login (or
    keep the dashboard on a password-protected page).
- **Vertex AI:** the VM's service account has only `roles/aiplatform.user`.
  No keys exist to leak.
- The database never leaves the VM.

## Troubleshooting

| Symptom | Check |
|---|---|
| Page loads, but Connect hangs or there is no audio | The UDP firewall rule `arul-voice-webrtc` exists. Some office or mobile networks block UDP and need a TURN server: add it to `PIPECAT_ICE_SERVERS` in `arul-voice.service`. |
| Frontend: "blocked by CORS policy" | The frontend's exact address (with `https://`, with or without `www.`) is in `CORS_ORIGINS` in `.env`; restart `arul-api` |
| Frontend: 401 | Send the `X-API-Key` header with the value of `API_KEY` |
| `403 ... aiplatform` / permission denied | The VM runs as `arul-voice-agent@...` with scope `cloud-platform` (`gcloud compute instances describe arul-voice`) |
| `gcp-create-vm.sh` fails on an org policy | Your org `arulmotorsnazareth-org` may restrict VMs with public IPs. An org admin can allow it for this project. |
| No certificate / HTTPS error | DNS A records for `voice.` and `api.` point at the VM's IP; `journalctl -u caddy` |
