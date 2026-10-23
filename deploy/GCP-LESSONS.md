# GCP knowledge base (for an AWS user): lessons, reference and the Arul Motors setup

These are your own notes for learning Google Cloud step by step, written
for someone who knows AWS. They end with deploying the Arul Motors backend
(voice agent, API and SQLite) to a VM. Each lesson records what was
**actually done and seen**, so this file is also the record of your GCP
project.

**How to use this file**

- **Lessons** (top) teach one building block at a time:
  1. concept, with its AWS equivalent;
  2. console clicks;
  3. the same thing with `gcloud`;
  4. check;
  5. clean up;
  6. takeaways.
- **Reference** (bottom) is for later lookups: AWS→GCP names, commands,
  DNS/Cloudflare, HTTPS/Caddy, troubleshooting, security, costs.
- **Current state** (just below) shows what exists in the project right now.

---

## Our setup

| | |
|---|---|
| Project | `project-2c317546-b2c6-4a7a-8d4` ("My First Project"), number `666692619275` |
| Organization | `arulmotorsnazareth-org`. Its policies apply, e.g. **no API keys** |
| Login | `arulmotorsnazareth@gmail.com`: Owner on the project (plus Org Admin, Project Mover and Service Usage Admin inherited from the org) |
| Billing account | `012671-BA0D6F-B8CAD7`: free trial, ₹28,781 credit, expires **17 Jan 2027**. **Upgrade before then** |
| Budget | "Arul Motors", ₹2,000/month, alerts at 50/90/100%. ₹3,500 suggested, since the VM alone is about ₹2,000 |
| Region | `asia-south1` (Mumbai) |
| Domain / DNS | `arulmotors.com`, DNS managed in **Cloudflare** (not GoDaddy) |
| `gcloud` | Installed on the Mac (Homebrew), logged in, project set. Cloud Shell works too |

## Current state (verified 2026-10-19)

**Live now:** **https://backend.arulmotors.com**, a sample FastAPI app behind
Caddy with a Let's Encrypt certificate. `voice.arulmotors.com` → `34.93.71.71`
(DNS only) is created for the voice page (Lesson 9).

```
Browser
  │  https://backend.arulmotors.com
  ▼
Cloudflare DNS (record "backend", A, DNS only / grey cloud) ──▶ 34.93.71.71
  │
  ▼
GCP firewall rule "allow-backend-8000": TCP 80, 443 from 0.0.0.0/0 → VMs tagged "arulmotors-web"
  │
  ▼
VM "arulmotors" (asia-south1-c, e2-small, Debian 13)
  ├─ Caddy :80/:443: HTTPS (Let's Encrypt, auto-renew), http→https redirect
  └─ uvicorn 127.0.0.1:8000: FastAPI demo (private; not reachable from outside)
```

| Resource | Name / value | Billing? |
|---|---|---|
| VM | `arulmotors`, `asia-south1-c`, **e2-small**, Debian 13, RUNNING, internal `10.160.0.3`, runs as **`arul-voice-agent`** (scope `cloud-platform`) | ✅ about ₹2.2/hour while running |
| Service account | `arul-voice-agent@project-2c317546-b2c6-4a7a-8d4.iam.gserviceaccount.com`, role **`roles/aiplatform.user`** only, no keys | free |
| Boot disk | `arulmotors`, 20 GB pd-balanced (auto-delete with the VM) | ✅ even when the VM is stopped |
| Static IP | `arulmotors-ip` = **`34.93.71.71`**, IN_USE by `arulmotors` | ✅ a few rupees/day, attached or not |
| Firewall | `allow-backend-8000` → **tcp:80, tcp:443**, source `0.0.0.0/0`, tag `arulmotors-web` (the name is historical; it no longer opens 8000) | free |
| Firewall | `arul-voice-webrtc` → **udp:32768-60999**, source `0.0.0.0/0`, tag `arulmotors-web` (call audio, WebRTC) | free |
| Firewall (defaults) | `default-allow-ssh` (22), `default-allow-rdp` (3389), `default-allow-icmp`, `default-allow-internal` | free |
| Certificate | Let's Encrypt (issuer `YE2`), CN `backend.arulmotors.com`, valid to **17 Jan 2027**; Caddy renews it automatically | free |
| DNS | Cloudflare A `backend` → `34.93.71.71`, **DNS only** | free |
| APIs enabled | `compute`, `aiplatform` (shown as "Agent Platform API"), `billingbudgets` + about 40 defaults | free |

**Watch-out:** uvicorn was started by hand in an SSH window. **When that SSH
session closes, or the VM restarts, the app stops** and Caddy returns 502
(seen in Lesson 4). Lesson 7 turns it
into a systemd service that survives logouts and reboots.

**Practice VM vs real server:** `arulmotors` was created with console
defaults: Debian, zone `-c`. Since Lesson 5 it runs as `arul-voice-agent`. For the real
deployment (Lesson 9) we either reuse it (after changing its service
account) or create the planned `arul-voice` VM. A static IP can be
**moved** between VMs, so `backend.arulmotors.com` keeps working either way.

## Progress

- [x] **Lesson 0:** Orientation and safety (console, Cloud Shell, verification, budget). 2026-10-19
- [x] **Lesson 1:** Projects, APIs, IAM. 2026-10-19
- [x] **Lesson 2:** First VM (console part). 2026-10-19. *Part B (`gcloud`) still to do.*
- [x] **Lesson 3:** Networking, firewall, sample FastAPI, **HTTPS with Caddy**. 2026-10-19
- [x] **Lesson 4:** Static IP + subdomain (Cloudflare), incl. the stop/start survival test. 2026-10-19
- [x] **Lesson 5:** Service accounts, no keys (Gemini called from the VM with no key). 2026-10-19
- [ ] **Lesson 6:** Disks, snapshots, restore
- [ ] **Lesson 7:** Logs, monitoring, **systemd** (keep apps running), troubleshooting
- [ ] **Lesson 8:** Costs
- [ ] **Lesson 9:** Real deployment of Arul Motors
- [x] **Lesson 10:** Instance schedule (VM off at night to save cost). 2026-10-23

---

# Lessons

## Lesson 0: Orientation and safety ✅

**AWS equivalent:** AWS console, account switcher, CloudShell, Billing → Budgets.

### Concept

- **Project = AWS account.** Every resource lives in a project. The **project
  picker** (top left, "My First Project") is GCP's "am I in the right AWS
  account?". Check it before every action.
- **Billing account:** a separate object that pays for one or more projects.
- **Navigation menu (☰):** all products. Pin Compute Engine, VPC network,
  IAM & Admin and Billing.
- **Search bar (`/`):** the fastest way to any page.
- **Cloud Shell (`>_`):** a free browser terminal with `gcloud` logged in,
  like AWS CloudShell.

### Steps

1. Check that the project picker shows **My First Project**.
2. Pin the products you use (☰ → hover → 📌).
3. **Billing → Account management → Verify account** (the red banner). Until
   it's verified, the upgrade won't apply and the account risks suspension.
4. **Billing → Budgets & caps → Create budget:** project scope, amount,
   alerts at 50/90/100%, **untick "include credits"**. A budget only emails
   you; it doesn't stop anything.
5. Open Cloud Shell and run:
   ```bash
   gcloud config list            # account + project   (like: aws configure list)
   gcloud projects list
   gcloud auth list
   gcloud billing projects describe project-2c317546-b2c6-4a7a-8d4
   ```

### What happened

- `gcloud config list` → account `arulmotorsnazareth@gmail.com`, project `project-2c317546-b2c6-4a7a-8d4`.
- `billing projects describe` → `billingEnabled: true`, account `012671-BA0D6F-B8CAD7`.
- Budget created ("Arul Motors", ₹2,000). Verification submitted.
- `gcloud billing budgets list` failed with *"Cloud Billing Budget API has
  not been used in project … or it is disabled"*, which led into Lesson 1.

### Takeaways

- Check the **project picker** before acting.
- **Project = account; billing account = who pays.**
- Set the **budget alert first**.

---

## Lesson 1: Projects, APIs, IAM ✅

**AWS equivalent:** account ID, IAM; plus enabling an API, a step AWS doesn't have.

### Concept

**A project has three identifiers.**

| | Ours | Use |
|---|---|---|
| Name | My First Project | Label only; can be changed |
| **ID** | `project-2c317546-b2c6-4a7a-8d4` | What you type in commands; permanent |
| Number | `666692619275` | Internal; appears in service-account emails and metadata |

**APIs are off until enabled.** Each project switches on the services it uses
(`compute.googleapis.com`, `aiplatform.googleapis.com`, …). A disabled one
gives *"API … has not been used in project … or it is disabled"*. Enabling
is free.

**IAM = principal + role + resource.**
- **Principal (who):** user, **service account** (a robot identity for
  programs), group.
- **Role (what):** a bundle of permissions.
  - *Basic* (Owner/Editor/Viewer): broad, so avoid them for programs.
  - *Predefined*, e.g. `roles/aiplatform.user`.
  - *Custom.*
- **Binding (where):** organization → folder → project → resource. Roles
  are **inherited downward**.
- **Difference from AWS:** the **resource holds the access list**. You manage
  access on the project's IAM page, not by attaching policies to users.
- **Service agents** (`service-666692619275@gcp-sa-….iam.gserviceaccount.com`)
  are Google's own identities in your project, like AWS service-linked
  roles. Leave them alone.

### Steps

1. ☰ → Cloud overview → **Dashboard** → *Project info* (name, ID, number).
2. ☰ → **APIs & Services → Enabled APIs & services**.
3. **Library** → "Cloud Billing Budget API" → **Enable**.
4. ☰ → **IAM & Admin → IAM**. Tick "Include Google-provided role grants".
5. **IAM & Admin → Roles** → "Agent Platform user" (formerly "Vertex AI User").

```bash
gcloud projects describe project-2c317546-b2c6-4a7a-8d4
gcloud services list --enabled
gcloud services enable billingbudgets.googleapis.com
gcloud billing budgets list --billing-account=012671-BA0D6F-B8CAD7
gcloud projects get-iam-policy project-2c317546-b2c6-4a7a-8d4 \
  --flatten="bindings[].members" --format="table(bindings.role,bindings.members)"
gcloud iam roles describe roles/aiplatform.user
```

### What happened

- **Vertex AI is now called "Agent Platform API"** in the console. It's the
  same `aiplatform.googleapis.com` that Gemini Live uses.
  - Its dashboard showed 60 requests / 65% errors. Those are from the
    2026-10-18 setup tests (the rejected thinking setting).
  - Its "latency" of about 47 s is normal: a Gemini Live conversation is one
    long streaming request.
- `billingbudgets` enabled (free). The budget list now works:
  `Arul Motors  2000  INR  0.5,0.9,1.0`.
- **IAM:** Owner directly on the project. **Organisation Administrator**,
  **Project Mover** and **Service Usage Admin** are inherited from the org
  (Inheritance column).
- **Roles:** `roles/aiplatform.user` ("Agent Platform user") is one of 24
  Agent Platform roles. It holds 459 permissions, all inside Vertex AI only
  (incl. `aiplatform.endpoints.predict`). The real VM gets this role: it can
  use Gemini, but nothing else.

### Takeaways

- Type the **project ID**; the name is just a label.
- **"API not enabled" → enable it.** It's the most common first error.
- Give programs a **narrow predefined role**, never Owner/Editor.

---

## Lesson 2: First VM ✅ (console)

**AWS equivalent:** launching EC2, but no key pair needed.

### Concept

| GCP choice | Meaning | AWS equivalent |
|---|---|---|
| **Region / zone** | `asia-south1` = Mumbai; `asia-south1-a/b/c` = data centres. A VM lives in one zone | `ap-south-1` / `ap-south-1a` |
| **Machine series** | E2 = cheapest, shared CPU. N2/N4 = general. C3 = compute heavy | t3 / m6i / c6i |
| **Machine type** | `e2-micro` (0.25 vCPU, 1 GB), `e2-small` (0.5 vCPU, 2 GB), `e2-medium` (1 vCPU, 4 GB) | t3.micro/small/medium |
| **Boot disk** | Image + disk type + size (e.g. Ubuntu 24.04 x86/64, Balanced, 20 GB) | AMI + EBS gp3 |
| **Network** | `default` VPC; **ephemeral** external IP by default | Default VPC + auto public IP |
| **Service account** | Identity the VM's programs run as (Lesson 5) | Instance profile |
| **Metadata server** | `http://metadata.google.internal`: the VM asks it about itself | IMDS `169.254.169.254` |

- The console's **Equivalent code** button shows the `gcloud` command for any form.
- **The create form is pre-filled** (zone, Debian image, machine type).
  Check every field before clicking Create.
- **SSH:** the **SSH button** (browser) or `gcloud compute ssh`. Both create
  and install keys for you.

### Part A: console (done)

Create instance → name, region/zone, series/type, OS and storage, then
Create. Then SSH, run the commands below, and stop/start to watch the
external IP.

```bash
hostname; cat /etc/os-release | head -2; nproc; free -h; df -h /
curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone; echo
```

### Part B: `gcloud` (still to do; uses a separate throwaway VM)

```bash
gcloud compute instances create practice-cli \
  --zone=asia-south1-a --machine-type=e2-micro \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=10GB
gcloud compute instances list
gcloud compute ssh practice-cli --zone=asia-south1-a      # first time: creates ~/.ssh/google_compute_engine
IPFMT='get(networkInterfaces[0].accessConfigs[0].natIP)'
gcloud compute instances describe practice-cli --zone=asia-south1-a --format="$IPFMT"
gcloud compute instances stop  practice-cli --zone=asia-south1-a
gcloud compute instances start practice-cli --zone=asia-south1-a
gcloud compute instances describe practice-cli --zone=asia-south1-a --format="$IPFMT"
gcloud compute instances delete practice-cli --zone=asia-south1-a --quiet
```

### What happened

- Created **`arulmotors`**: zone `asia-south1-c`, **e2-small**, **Debian 13**,
  20 GB balanced, Standard (not Spot). These were the console's defaults
  instead of the planned e2-micro/Ubuntu/zone -a, which taught the
  "check every field" lesson.
- **Service account:** the Compute Engine default
  `666692619275-compute@developer.gserviceaccount.com` with default scopes.
  It has **no project roles**, so it can do very little (safe).
- **SSH output:**
  - Linux user `arulmotorsnazareth` (created from the Google account)
  - Debian 13 (trixie)
  - `nproc` = 2 (shared vCPUs)
  - 1.9 GiB memory (about 0.5 GiB used by the OS, about 1.4 GiB available)
  - **no swap**
  - 20 GB disk, 2.6 GB used
  - The metadata server answered `projects/666692619275/zones/asia-south1-c`.
- **Stop/start:** the external IP changed **34.100.155.125 → 34.93.71.71**;
  the internal IP stayed **10.160.0.3**.

### Takeaways

- VM = **zone + machine type + image/disk + network + service account**.
- External IPs are **ephemeral** and change on stop/start; internal IPs stay.
- **Stopped ≠ free:** disks and static IPs still bill.

---

## Lesson 3: Networking, firewall, sample FastAPI, HTTPS ✅

**AWS equivalent:** VPC + security groups (+ an nginx/ACM-style HTTPS front).

### Concept

- The **`default` VPC** has a subnet per region (Mumbai `10.160.0.0/20`,
  hence the VM's `10.160.0.3`).
- **Firewall rules belong to the VPC.** A rule allows protocol/ports, from
  sources, **to VMs carrying a network tag**. Tagging a VM attaches the rule,
  just as attaching a security group does in AWS.
- **Incoming traffic is denied by default.** The default VPC only allows SSH
  22, RDP 3389, ICMP and internal traffic.
- An app is reachable from outside only if **both**:
  1. it listens on the right interface (`0.0.0.0`, or behind a proxy), and
  2. a firewall rule allows the port.
- **Least privilege:**
  - **Never** use "Allow all" protocols with `0.0.0.0/0`, because that opens
    every port to the internet.
  - Open only the ports needed. Restrict sources when you can.
- **HTTPS:** a reverse proxy (**Caddy**) terminates TLS on 443 with a free
  Let's Encrypt certificate (auto-renewed) and forwards to the app on
  `127.0.0.1`. The app itself is never exposed.

### Part A: sample FastAPI on the VM (SSH)

```bash
sudo apt-get update && sudo apt-get install -y python3-venv
python3 -m venv ~/demo && source ~/demo/bin/activate
pip install fastapi uvicorn

cat > ~/main.py <<'PY'
import socket
from fastapi import FastAPI

app = FastAPI(title="Arul Motors demo")

@app.get("/")
def hello():
    return {"message": "Vanakkam from GCP!", "host": socket.gethostname()}
PY

cd ~ && uvicorn main:app --host 0.0.0.0 --port 8000     # step 1: public on 8000 (test)
```

`curl localhost:8000` works on the VM. From the Mac it **times out** until a
firewall rule allows it.

### Part B: firewall rule for 8000 (console)

1. **VPC network → Firewall → Create firewall rule:**
   - Ingress, Allow
   - Target tag (e.g. `arulmotors-web`)
   - Source `0.0.0.0/0` (whole internet) or `<your-ip>/32`
   - **Specified protocols and ports → TCP 8000**, not "Allow all"
2. **VM → Edit → Network tags:** add the same tag → Save. No restart needed.
3. Open `http://backend.arulmotors.com:8000` and `/docs`.

```bash
gcloud compute firewall-rules create allow-demo-8000 --network=default \
  --direction=INGRESS --action=ALLOW --rules=tcp:8000 \
  --source-ranges=0.0.0.0/0 --target-tags=arulmotors-web
gcloud compute instances add-tags arulmotors --zone=asia-south1-c --tags=arulmotors-web
```

### Part C: HTTPS with Caddy (final setup)

**1. Firewall:** allow only **TCP 80, 443**, not 8000.
- 443 serves HTTPS.
- 80 is needed for Let's Encrypt to issue the certificate and for the
  http→https redirect.

```bash
gcloud compute firewall-rules update allow-backend-8000 --rules=tcp:80,tcp:443
# (or create a new rule "allow-web" with tcp:80,tcp:443 and delete the old one)
```

**2. Install Caddy** (official repository; Debian/Ubuntu):

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update && sudo apt-get install -y caddy
```

**3. Configure the domain:**

```bash
sudo tee /etc/caddy/Caddyfile <<'EOF'
backend.arulmotors.com {
	reverse_proxy 127.0.0.1:8000
}
EOF
sudo systemctl reload caddy
```

**4. Make the app private:**

```bash
cd ~ && source ~/demo/bin/activate && uvicorn main:app --host 127.0.0.1 --port 8000
```

**5. Open** `https://backend.arulmotors.com` 🔒. The first request takes
10–30 s while the certificate is issued.

### What happened

- First rule `allow-backend-8000` was created with **Protocols: Allow all**
  (`0.0.0.0/0` → all ports) ⚠️. It was fixed to **tcp:80, tcp:443** once
  Caddy was in place.
- Tag `arulmotors-web` added to the VM.
- Before the rule: timeout. After it: `{"message":"Vanakkam from GCP!","host":"arulmotors"}`.
- **Final check (verified from the Mac):**
  - `https://backend.arulmotors.com/` → the JSON above ✓
  - `https://backend.arulmotors.com/docs` → 200 ✓
  - `http://backend.arulmotors.com/` → **308 redirect to https** ✓
  - `http://34.93.71.71:8000` → no response (8000 is private now) ✓
  - Certificate: Let's Encrypt `YE2`, CN `backend.arulmotors.com`, valid
    19 Oct 2026 → **17 Jan 2027**, auto-renewed by Caddy ✓

### Takeaways

- **Listen address + firewall rule** must both allow traffic.
- **Rule + network tag = security group.**
- **Never "Allow all" with `0.0.0.0/0`.** Open exact ports.
- Put the app on `127.0.0.1` behind **Caddy** for HTTPS. It's free, automatic
  and renews itself.
- An app started in an SSH window **dies with the session**. Use systemd
  (Lesson 7).

---

## Lesson 4: Static IP + subdomain ✅

**AWS equivalent:** Elastic IP + a Route 53 A record.

### Concept

- An **ephemeral** IP is released on stop and a new one assigned on start.
  DNS pointing at it would break.
- A **static IP** (reserved address) belongs to your project until you
  release it. It survives stop/start, and it can be **detached from one VM
  and attached to another**, so DNS never needs to change.
- You can **promote** a VM's current ephemeral IP to static: the address
  stays the same.
- **Cost:** a static IP bills a few rupees a day, attached or not. Release it
  when it's no longer needed.

### Steps

**Console:** VPC network → **IP addresses** → the VM's external IP (Type
*Ephemeral*) → ⋮ → **Promote to static IP address** → name → Reserve.

```bash
gcloud compute addresses create arulmotors-ip --region=asia-south1 --addresses=34.93.71.71   # promote
gcloud compute addresses list
# reserve a brand-new one:     gcloud compute addresses create NAME --region=asia-south1
# release (when done for good): gcloud compute addresses delete arulmotors-ip --region=asia-south1
```

**DNS (Cloudflare, because that's where `arulmotors.com` is managed):**
DNS → Records → **Add record**:
- Type **A**
- Name `backend` (only the subdomain part)
- IPv4 `34.93.71.71`
- **Proxy status: DNS only (grey cloud)**

Check with: `dig +short backend.arulmotors.com` → `34.93.71.71`.

### What happened

- Promoted `34.93.71.71` → **`arulmotors-ip`** (static, IN_USE by `arulmotors`).
- Found that the domain's nameservers are **Cloudflare**
  (`ivy.ns.cloudflare.com`, `rommy.ns.cloudflare.com`). DNS is edited there,
  not in GoDaddy.
- The record was first **Proxied (orange)**, so lookups returned Cloudflare
  IPs (`104.21.10.164`, `172.67.146.3`). It was switched to **DNS only**, and
  now it resolves to `34.93.71.71` ✓.

### Stop/start survival test (done)

- Stopped at **12:29:52 IST**, started at **12:32:27 IST** (2026-10-19).
- External IP after the restart: **`34.93.71.71`, unchanged** ✓. Compare
  Lesson 2, where the ephemeral IP changed. The internal IP stayed
  `10.160.0.3`, and `arulmotors-ip` stayed `IN_USE`.
- DNS still resolves to `34.93.71.71`; no Cloudflare change was needed.
- After boot: **Caddy came back by itself** (it's a systemd service), but
  **uvicorn didn't** (it was started by hand), so the site gave 502 until
  uvicorn was started again in SSH. After that: HTTPS JSON ✓, http → 308 ✓.
  This is the motivation for Lesson 7 (systemd).

### Takeaways

- **Static IP before DNS.** Always.
- A static IP is **movable**: practice VM today, real server tomorrow, same DNS.
- **Cloudflare: DNS only** for this backend (see Reference → DNS & Cloudflare).

---

## Lesson 5: Service accounts, no keys

**AWS equivalent:** an IAM role + instance profile on EC2. Programs on the
instance get temporary credentials automatically; there are no access keys.

### Concept

- A **service account (SA)** is an identity for programs, e.g.
  `arul-voice-agent@project-2c317546-b2c6-4a7a-8d4.iam.gserviceaccount.com`.
- **Attach** it to a VM. Then any program on the VM that uses Google's
  libraries gets **short-lived tokens from the metadata server**. No key
  file exists, so there's nothing to leak or rotate. That's *Application
  Default Credentials (ADC)*.
- What the program may do = the SA's **IAM roles** (here only
  `roles/aiplatform.user`) **and** the VM's **access scopes**.
  - Scopes are an older, coarse per-VM limit.
  - Best practice: set scopes to **`cloud-platform`** ("Allow full access
    to all Cloud APIs") and control access with IAM roles.
- **Right now** `arulmotors` runs as the *Compute Engine default SA* with
  default scopes and **no roles**, so calling Gemini from it fails. This
  lesson shows the failure, then fixes it properly.
- **This is exactly how `bot.py` logs in on the server.** It creates
  `GeminiLiveVertexLLMService` with no credentials, and Pipecat falls back
  to ADC.
- **Changing a VM's SA or scopes requires the VM to be stopped.**

### Part A: create the service account

**Console:**
1. ☰ → **IAM & Admin → Service accounts → Create service account**.
2. Name `arul-voice-agent`, description "Arul Motors voice agent" → **Create and continue**.
3. **Grant this service account access to project:** role **Agent Platform
   user** (`roles/aiplatform.user`) → **Continue** → **Done**. Skip "grant
   users access".
4. Don't create a key. (The org blocks it anyway, and it isn't needed.)

**gcloud:**
```bash
gcloud iam service-accounts create arul-voice-agent --display-name="Arul Motors voice agent"
gcloud projects add-iam-policy-binding project-2c317546-b2c6-4a7a-8d4 \
  --member="serviceAccount:arul-voice-agent@project-2c317546-b2c6-4a7a-8d4.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user" --condition=None
gcloud iam service-accounts list
```

### Part B: see it fail first (on the VM, current default SA)

SSH in, then:

```bash
source ~/demo/bin/activate && pip install -q google-genai

cat > ~/gemini_test.py <<'PY'
from google import genai

client = genai.Client(vertexai=True, project="project-2c317546-b2c6-4a7a-8d4", location="us-central1")
reply = client.models.generate_content(model="gemini-2.5-flash",
                                       contents="Say a one-line welcome for a Yamaha showroom in Tanglish.")
print(reply.text)
PY

python ~/gemini_test.py
```

**Expected:** an error such as *"Request had insufficient authentication
scopes"* or *"Permission denied … aiplatform.endpoints.predict"*.

**Who am I right now?** Ask the metadata server:
```bash
curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email; echo
curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/scopes
```

### Part C: attach the new SA (VM must be stopped)

**Console:**
1. VM instances → `arulmotors` → **Stop**.
2. **Edit** → *Security and access* → **Service account:** `arul-voice-agent`
   → **Access scopes:** **Allow full access to all Cloud APIs** → **Save**.
3. **Start**.

**gcloud:**
```bash
gcloud compute instances stop arulmotors --zone=asia-south1-c
gcloud compute instances set-service-account arulmotors --zone=asia-south1-c \
  --service-account=arul-voice-agent@project-2c317546-b2c6-4a7a-8d4.iam.gserviceaccount.com \
  --scopes=cloud-platform
gcloud compute instances start arulmotors --zone=asia-south1-c
```

After the restart, start uvicorn again (until Lesson 7 makes it a service).

### Part D: it works, with no key

```bash
curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email; echo
#   → arul-voice-agent@project-2c317546-b2c6-4a7a-8d4.iam.gserviceaccount.com
source ~/demo/bin/activate && python ~/gemini_test.py
#   → a one-line Tanglish welcome from Gemini
```

Optionally peek at the token the libraries use. **Don't share it**; it's
valid for about 1 hour:
```bash
curl -s -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token | head -c 80; echo
```

### Check (from the Mac)

```bash
gcloud compute instances describe arulmotors --zone=asia-south1-c \
  --format="value(serviceAccounts[0].email,serviceAccounts[0].scopes)"
gcloud projects get-iam-policy project-2c317546-b2c6-4a7a-8d4 \
  --flatten="bindings[].members" --filter="bindings.members~arul-voice-agent" \
  --format="table(bindings.role,bindings.members)"
```

### Clean up

**Keep** `arul-voice-agent`: it's the identity the real server uses. Delete
`~/gemini_test.py` if you like.

### Cost

One text call to `gemini-2.5-flash` costs a fraction of a paisa. Service
accounts are free.

### Takeaways

- **Attach a service account; never use key files.** Credentials come from
  the metadata server.
- Access = **IAM role** (`aiplatform.user`) **+ scopes** (`cloud-platform`).
- Changing the SA or scopes needs a **stopped VM**.
- `bot.py` on the server logs in to Vertex exactly like this test.

### What happened (fill in)

- Date: 2026-10-19
- Created `arul-voice-agent@project-2c317546-b2c6-4a7a-8d4.iam.gserviceaccount.com`
  (Enabled, **no keys**).
- **Mistake 1:** the role step in the create wizard was skipped, so the
  account had **no role** (creating an account grants nothing). Fixed via
  IAM → **Grant access**.
- **Mistake 2:** the first pick was **"Vertex AI Service Agent"**
  (`roles/aiplatform.serviceAgent`). That role is for Google's own service
  agent and too broad, so it was caught before saving. Corrected to
  **Agent Platform user** (`roles/aiplatform.user`) ✓.
- VM stopped → Edit → service account `arul-voice-agent`, scopes **Allow
  full access** → started at 13:58 IST. Verified from the Mac: `RUNNING`,
  `arul-voice-agent@…`, scope `https://www.googleapis.com/auth/cloud-platform`,
  IP still `34.93.71.71` ✓.
- Error before attaching: (Step 2 skipped)
- **Gemini reply after attaching (no key, no key file):**
  `Nazareth-oda Yamaha Showroom-ukku அன்பான Welcome!` ✅

---

## Lesson 6: Disks, snapshots, restore (the real database backup)

**AWS equivalent:** EBS snapshots + Data Lifecycle Manager + "create volume from snapshot".

### Concept

- A **snapshot** is a point-in-time copy of a disk.
  - **Incremental:** only changed blocks are stored, so daily snapshots are cheap.
  - It can be taken while the VM runs (crash-consistent, which is fine for
    SQLite in WAL mode) or while it's stopped (fully consistent).
- Snapshots **outlive the VM**. If the VM or its disk is deleted, the
  snapshots remain.
- **Snapshot schedule:** an automatic policy (e.g. daily, keep 14 days)
  attached to a disk.
  - A disk can have **one** snapshot schedule.
  - Google may create **`default-schedule-1`** automatically for new disks.
    Check it before making your own.
- **Restore options:**
  1. create a new disk from a snapshot, attach it and copy files back (e.g.
     the database);
  2. create a whole new VM whose boot disk comes from the snapshot.
- **What must be protected on the real server:**
  `/opt/arul-motors/arul_motors.db` (+ `-wal` / `-shm`), which holds every
  customer, booking and call log, and `/opt/arul-motors/.env` (`API_KEY`).

### Part A: check the existing schedule

**Console:** ☰ → **Compute Engine → Snapshots** → tab **Snapshot schedules**
→ click **`default-schedule-1`**. Note:
- **Frequency / start time** (times are shown in UTC; IST = UTC + 5:30)
- **Auto-delete snapshots after** (retention)
- **Region / storage location**
- **In use by:** is the disk `arulmotors` listed?

Also: **Compute Engine → Disks → `arulmotors`** → *Snapshot schedule* field.

```bash
gcloud compute resource-policies describe default-schedule-1 --region=asia-south1
gcloud compute disks describe arulmotors --zone=asia-south1-c --format="value(resourcePolicies)"
```

**Decide:**
- It's attached, runs daily and keeps ≥ 7 days → **keep it** (skip Part D).
- Otherwise → create `arul-daily-backup` (Part D). If `default-schedule-1`
  is attached, detach it first (one schedule per disk).

### Part B: take a manual snapshot (works with the VM stopped or running)

**Console:** Compute Engine → **Disks** → `arulmotors` → **Create snapshot**:
- name `arulmotors-snap-1`
- location **Regional → asia-south1**
- Create

```bash
gcloud compute snapshots create arulmotors-snap-1 \
  --source-disk=arulmotors --source-disk-zone=asia-south1-c --storage-location=asia-south1
gcloud compute snapshots list
```

### Part C: restore test, i.e. prove the database can be recovered

1. **Start the VM** (it must be running to attach and read a disk).
2. Create a disk from the snapshot and attach it as a second disk:
   ```bash
   gcloud compute disks create restore-test --zone=asia-south1-c --source-snapshot=arulmotors-snap-1
   gcloud compute instances attach-disk arulmotors --zone=asia-south1-c --disk=restore-test
   ```
   (Console: Disks → **Create disk** → Source **Snapshot**; then VM → Edit →
   **Attach existing disk**.)
3. On the VM (SSH), mount it read-only and read the database **copy**:
   ```bash
   lsblk                                              # new disk, e.g. sdb with partition sdb1
   sudo mkdir -p /mnt/restore && sudo mount -o ro /dev/sdb1 /mnt/restore
   ls -l /mnt/restore/opt/arul-motors/                # arul_motors.db (+ -wal/-shm), .env
   sudo mkdir -p /tmp/restore && sudo cp /mnt/restore/opt/arul-motors/arul_motors.db* /tmp/restore/
   sudo sqlite3 /tmp/restore/arul_motors.db "SELECT count(*) FROM customers; SELECT id, appointment_date, status FROM service_appointments;"
   ```
   - Copy all `arul_motors.db*` files together: recent writes can still be
     in the `-wal` file.
   - The counts should match the live database:
     `sudo sqlite3 /opt/arul-motors/arul_motors.db "SELECT count(*) FROM customers;"`.
4. **How a real recovery would work** (don't run this now):
   ```bash
   sudo systemctl stop arul-voice arul-api
   sudo cp /tmp/restore/arul_motors.db* /opt/arul-motors/ && sudo chown arul:arul /opt/arul-motors/arul_motors.db*
   sudo systemctl start arul-voice arul-api
   ```
5. Clean up the test disk:
   ```bash
   sudo umount /mnt/restore && sudo rm -rf /tmp/restore          # on the VM
   gcloud compute instances detach-disk arulmotors --zone=asia-south1-c --disk=restore-test
   gcloud compute disks delete restore-test --zone=asia-south1-c --quiet
   ```

### Part D: daily automatic snapshots (only if Part A says so)

**Console:**
1. Snapshots → **Snapshot schedules → Create**:
   - name `arul-daily-backup`, region **asia-south1**
   - **Daily**, start **20:30 UTC** (= 02:00 IST)
   - auto-delete after **14 days**
2. Disks → `arulmotors` → **Edit** → Snapshot schedule `arul-daily-backup` → Save.

```bash
# if default-schedule-1 is attached and you're replacing it:
gcloud compute disks remove-resource-policies arulmotors --zone=asia-south1-c --resource-policies=default-schedule-1
gcloud compute resource-policies create snapshot-schedule arul-daily-backup \
  --region=asia-south1 --daily-schedule --start-time=20:30 \
  --max-retention-days=14 --storage-location=asia-south1
gcloud compute disks add-resource-policies arulmotors --zone=asia-south1-c --resource-policies=arul-daily-backup
```

**Restore a whole VM instead:** Create instance → Boot disk → **Change →
Snapshots** → pick one. Useful if the VM itself is broken.

### Check

- `gcloud compute disks describe arulmotors --zone=asia-south1-c --format="value(resourcePolicies)"` → one schedule attached.
- The next day, `gcloud compute snapshots list` shows an automatic snapshot.
- Restore test: the counts from the restored copy match the live database.

### Clean up

- Delete the test disk (Part C, step 5).
- Delete `arulmotors-snap-1` when no longer needed (the schedule makes its
  own): `gcloud compute snapshots delete arulmotors-snap-1 --quiet`.

### Cost

About ₹2–5 per GB-month of stored snapshot data. With 3–4 GB used and small
daily changes, that's tens of rupees a month. The test disk costs a few
paise while it exists.

### Takeaways

- **Backups only count once a restore has been tested.** You restored the
  real database.
- Snapshots are incremental, can be taken live, and outlive the VM.
- One schedule per disk. Check Google's `default-schedule-1` before adding your own.
- Copy `arul_motors.db` **with** its `-wal`/`-shm` files.

### What happened (fill in)

- Date:
- `default-schedule-1` settings / attached to `arulmotors`:
- Restore test (customers / appointments counted):
- Final schedule:

---

## Lesson 7: systemd, logs, monitoring, troubleshooting

**AWS equivalent:** running services on EC2 + CloudWatch agent, logs and alarms + Route 53 health checks.

### Concept

- **systemd** is Linux's service manager. A *unit file* describes how to run
  a program; systemd starts it **at boot**, **restarts it if it crashes**,
  and keeps it running **after you log out**. That's why Caddy came back
  after the restart in Lesson 4 and uvicorn didn't.
- **journald:** systemd keeps each service's output (`journalctl -u NAME`).
- **On the GCP side:**
  - the **Observability** tab: CPU, network and disk out of the box; memory
    needs the **Ops Agent**;
  - **Cloud Logging**: log search;
  - **Cloud Monitoring**: dashboards, **uptime checks**, email alerts;
  - the **serial console**: boot messages, for when SSH won't work.
- **Swap:** GCP images have none (Lesson 2). On a 2 GB VM, a small swap file
  is a cheap safety net against out-of-memory crashes.

### Part A: turn the demo app into a service

On the VM, first stop the hand-started uvicorn (Ctrl+C in its window), then:

```bash
sudo tee /etc/systemd/system/demo.service <<'EOF'
[Unit]
Description=Arul Motors demo FastAPI
After=network-online.target
Wants=network-online.target

[Service]
User=arulmotorsnazareth
WorkingDirectory=/home/arulmotorsnazareth
ExecStart=/home/arulmotorsnazareth/demo/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload           # read new or changed unit files
sudo systemctl enable --now demo       # start now + at every boot
systemctl status demo                  # "active (running)"
journalctl -u demo -f                  # live logs (Ctrl+C to stop following)
```

**Test it:**
- Close all SSH windows and open `https://backend.arulmotors.com`: still works ✓.
- `sudo reboot`, wait a minute, open it again: still works, with no manual step ✓.
- Crash test: `sudo pkill -f "uvicorn main:app"`, then `systemctl status demo`
  a few seconds later. It restarted itself.

**Everyday commands:**

| Command | Does |
|---|---|
| `sudo systemctl restart demo` | Restart (e.g. after editing `main.py`) |
| `sudo systemctl stop demo` / `start demo` | Stop / start now |
| `sudo systemctl disable demo` | Don't start at boot |
| `systemctl status demo` | State + last log lines |
| `journalctl -u demo -n 100` / `-f` / `--since "10 min ago"` | Logs |

The real server uses two units built the same way: `arul-voice.service`
(voice agent) and `arul-api.service` (API).

### Part B: add swap (recommended on e2-small)

```bash
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab     # keep it after reboot
free -h                                                        # Swap: 1.0Gi
```

### Part C: GCP monitoring

1. **VM metrics:** VM instances → `arulmotors` → **Observability** tab (CPU,
   network, disk).
2. **Memory metrics + logs in the cloud** (optional): install the Ops Agent:
   ```bash
   curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
   sudo bash add-google-cloud-ops-agent-repo.sh --also-install
   ```
   It needs the VM's SA to be allowed to write logs and metrics. Grant
   `arul-voice-agent` **Logs Writer** (`roles/logging.logWriter`) and
   **Monitoring Metric Writer** (`roles/monitoring.metricWriter`). Then
   memory appears in the Observability tab and logs in
   ☰ → **Logging → Logs Explorer** (query `resource.type="gce_instance"`).
3. **Uptime check with email alert:** ☰ → **Monitoring → Uptime checks →
   Create**:
   - protocol HTTPS, host `backend.arulmotors.com`, path `/`, every 5 min;
   - create an alert → notification channel = your email.

   If the site goes down, Google emails you.
4. **Serial console** (when SSH fails): VM details → **Logs → Serial port 1
   (console)** shows boot messages.

### Part D: troubleshooting drills

| Drill | You'll see | Lesson |
|---|---|---|
| `sudo systemctl stop demo` → open the site | **502** from Caddy | The app behind the proxy is down → `systemctl status`, `journalctl` |
| Remove the VM's network tag → open the site | **Timeout** | Firewall blocks it → tags and rules |
| `ss -tlnp` on the VM | `127.0.0.1:8000` (uvicorn), `*:443`/`*:80` (Caddy) | What listens where |
| `sudo journalctl -u caddy -n 50` | Certificate and proxy messages | Caddy issues |

Put everything back afterwards (`start demo`, re-add the tag).

### Clean up

Keep `demo.service` while practising. Before Lesson 9:
`sudo systemctl disable --now demo`.

### Cost

systemd and swap are free. The Ops Agent's logs/metrics stay within free
allowances at this size. Uptime checks: the first ones are free.

### Takeaways

- **Anything that must keep running → a systemd service.**
- `systemctl status` + `journalctl -u` are the first two troubleshooting commands.
- **502 = app down behind Caddy; timeout = firewall/DNS.**
- An uptime check + email tells you before your users do.

### What happened (fill in)

- Date:
- Survived reboot without manual start:
- Uptime check created:

---

## Lesson 8: Costs

**AWS equivalent:** Cost Explorer, cost allocation tags, Budgets.

### Concept

- Every charge is a **SKU** (e.g. "E2 Instance Core running in Mumbai",
  "Balanced PD Capacity", "Static IP charge", "Gemini … audio input").
- **What bills when:**

| State | VM CPU/RAM | Disk | Static IP | Snapshots |
|---|---|---|---|---|
| VM running | ✅ | ✅ | ✅ | ✅ |
| VM **stopped** | ❌ | ✅ | ✅ | ✅ |
| VM deleted, IP still reserved | ❌ | ❌ (auto-deleted) | ✅ | ✅ |
| Everything deleted/released | ❌ | ❌ | ❌ | only if kept |

- **Labels** (`key=value`, like AWS cost allocation tags) let reports split
  costs, e.g. `app=arulmotors`, `env=practice` / `env=prod`.
- **Free trial:** charges are paid from the ₹28,781 credit. Reports show
  the cost **and** the credit that offset it.
- Vertex AI (Gemini) appears as its own service, so voice-call costs are
  easy to see.

### Part A: read the bill

1. ☰ → **Billing → Reports:**
   - Time range: this month. **Group by: Service**, then **SKU**.
   - Tick/untick "credits" to see the real cost versus what you pay.
2. **Billing → Cost table:** a line-by-line breakdown, which can be exported.
3. **Billing → Budgets & caps:** set "Arul Motors" to about **₹3,500**, so
   normal months don't trip the 100% alert.

### Part B: labels

**Console:** VM instances → `arulmotors` → **Edit** → *Labels* → add
`app = arulmotors`, `env = practice`. Do the same on **Disks** → `arulmotors`.

```bash
gcloud compute instances add-labels arulmotors --zone=asia-south1-c --labels=app=arulmotors,env=practice
gcloud compute disks add-labels arulmotors --zone=asia-south1-c --labels=app=arulmotors,env=practice
gcloud compute addresses update arulmotors-ip --region=asia-south1 --update-labels=app=arulmotors
```

Labels appear in billing reports after about a day. Then filter **Labels →
app = arulmotors**.

### Part C: the monthly "what am I paying for?" check

```bash
gcloud compute instances list --format="table(name,zone.basename(),machineType.basename(),status)"
gcloud compute disks list --format="table(name,sizeGb,users.list().basename())"   # disks with no user = waste
gcloud compute addresses list --format="table(name,address,status)"              # RESERVED (not IN_USE) = waste
gcloud compute snapshots list --format="table(name,diskSizeGb,creationTimestamp)"
```

### Expected monthly costs

| Item | Approx. |
|---|---|
| e2-small VM 24/7 + 20 GB disk | ₹1,640 |
| Static IP | ₹300–350 |
| Snapshots (14 daily) | ₹50–100 |
| Gemini Live calls | ₹2–7 per call |

### Takeaways

- **Stopped VMs still cost** (disk + static IP). Delete what you don't use.
- An **unattached** static IP or disk is pure waste. Check monthly.
- **Labels** make the bill readable. **Budgets** warn but never stop anything.

### What happened (fill in)

- Date:
- This month's cost so far (before credits):
- Budget updated to:

---

## Lesson 9: Real deployment of Arul Motors

**Goal:** the real backend on the VM:
- the voice agent at **`https://voice.arulmotors.com`**, password-protected;
- the API at **`https://backend.arulmotors.com`**, with an API key;
- SQLite on the VM, with daily snapshots;
- Gemini via the VM's service account.

The frontend stays on GoDaddy.

### Path A (recommended): turn `arulmotors` into the real server

`arulmotors` is already e2-small in Mumbai, with static IP `34.93.71.71`
and DNS for both names. Debian 13 works with the scripts (apt, Python 3.13,
Caddy).

**Domains:**

| Name | Serves | Program on the VM |
|---|---|---|
| `voice.arulmotors.com` | Voice page `/client` (password) | `bot.py` :7860 (`arul-voice.service`) |
| `backend.arulmotors.com` | Dashboard API `/api/...` (X-API-Key) | `api.py` :8001 (`arul-api.service`) |

**Before you start:**
- **Lesson 5 (service account) is required.** Without it, every Gemini call
  from the VM is refused.
- Lessons 6 (snapshot schedule) and 7 (systemd basics) are strongly
  recommended.

**1. Firewall: add UDP for call audio.** Port 80/443 is already open via
`allow-backend-8000`.

```bash
gcloud compute firewall-rules create arul-voice-webrtc --network=default \
  --direction=INGRESS --action=ALLOW --rules=udp:32768-60999 \
  --source-ranges=0.0.0.0/0 --target-tags=arulmotors-web
```

The browser's audio flows over UDP straight to the VM (WebRTC). Linux picks
these ports from its ephemeral range.

**2. DNS (Cloudflare, DNS only):** done. `voice` and `backend` → A →
`34.93.71.71`, both verified 2026-10-19.

**3. Retire the demo** (on the VM):

```bash
sudo systemctl disable --now demo
sudo mv /etc/caddy/Caddyfile /etc/caddy/Caddyfile.demo
```

This keeps things clean. `vm-setup.sh` writes a new Caddyfile only if the
voice domain isn't in the current one. `voice.arulmotors.com` isn't in the
demo config, so it would be replaced anyway, but moving it keeps a copy.

**4. Copy the code** (from the Mac, in the `Arul-Motors` folder):

```bash
gcloud compute ssh arulmotors --zone=asia-south1-c --command="mkdir -p ~/arul-motors"
gcloud compute scp --recurse --zone=asia-south1-c \
  bot.py database.py api.py requirements.txt deploy arulmotors:~/arul-motors/
```

**5. Install** (on the VM):

```bash
sudo bash ~/arul-motors/deploy/vm-setup.sh voice.arulmotors.com backend.arulmotors.com https://arulmotors.com
```

The script:
- installs Python packages into `/opt/arul-motors/.venv`;
- creates `/opt/arul-motors/.env` with a random `API_KEY`;
- installs `arul-voice.service` + `arul-api.service`;
- writes the Caddyfile and **asks for a username and password for the
  voice page**.

**6. First test with demo customers:**

```bash
sudo nano /opt/arul-motors/.env           # set SEED_DEMO=1
sudo rm -f /opt/arul-motors/arul_motors.db*
sudo systemctl restart arul-voice arul-api
journalctl -u arul-voice -f                # watch for errors (Ctrl+C to exit)
```

- Voice: open **`https://voice.arulmotors.com/client`**, log in, click
  **Connect**, and talk. The agent greets you in Tanglish as Arul Motors
  Yamaha showroom, Nazareth.
- API:
  ```bash
  sudo grep API_KEY /opt/arul-motors/.env
  curl -H "X-API-Key: <key>" "https://backend.arulmotors.com/api/due-services?days=7"
  ```

**7. Go live:**
- [ ] `SEED_DEMO=0`, delete the database, restart (no demo customers in real data).
- [ ] Add real bike sales via `POST /api/vehicles` (or the frontend).
- [ ] Frontend (GoDaddy) calls `https://backend.arulmotors.com` with
  `X-API-Key`. If the site also opens on `www.`, add that address to
  `CORS_ORIGINS` in `.env` and restart `arul-api`.
- [ ] Snapshot schedule attached to the `arulmotors` disk (Lesson 6).
- [ ] Uptime checks on `https://voice.arulmotors.com` and `https://backend.arulmotors.com` (Lesson 7).
- [ ] Billing verified and **upgraded** before 17 Jan 2027. Budget ≈ ₹3,500.
- [ ] Delete `default-allow-rdp`; optionally relabel `env=prod`.
- [ ] Rename for clarity: create rule `allow-web` (tcp:80,443) and delete
  `allow-backend-8000`.

**Updating the code later:** repeat step 4, then run `vm-setup.sh` again
with the same three arguments. The `.env` and database are kept.

### Path B: a fresh VM with the scripts

Use `deploy/gcp-create-vm.sh` (it creates `arul-voice`, Ubuntu, its own
static IP, firewall and schedule), then follow `deploy/README.md`. To keep
`backend.arulmotors.com`, either point DNS at the new IP or move
`arulmotors-ip` to the new VM:
1. remove the old VM's access config;
2. add one with `--address=34.93.71.71`;
3. use the tag `arul-voice`.

This path is cleaner but repeats work you've already done.

### Troubleshooting the real app

| Symptom | Check |
|---|---|
| `journalctl -u arul-voice` shows 403 / permission denied for aiplatform | The VM's SA must be `arul-voice-agent` with scope `cloud-platform` (Lesson 5) |
| `/client` loads, Connect hangs, no audio | UDP rule `arul-voice-webrtc` exists with tag `arulmotors-web`; some office/mobile networks need a TURN server |
| 502 on backend or api | `systemctl status arul-voice arul-api`; `journalctl -u ...` |
| The browser keeps asking for the password | The username/password set during `vm-setup.sh` (stored as a hash in `/etc/caddy/Caddyfile`) |
| Frontend "CORS" error | `CORS_ORIGINS` in `/opt/arul-motors/.env` must match the frontend's exact address |
| Frontend 401 | The `X-API-Key` header doesn't match `API_KEY` in `.env` |

### What happened (fill in)

- Date:
- Path chosen:
- First test call result:

---

## Lesson 10: Instance schedule (VM off at night)

**AWS equivalent:** Instance Scheduler / an EventBridge rule that stops and
starts EC2. In GCP it's built in: an **instance schedule** is a resource policy
(the same kind of object as the snapshot schedule) attached to the VM.

### Our schedule

| | Time (IST) | Why |
|---|---|---|
| Start | **07:00** every day | The client's first calling window is 7–10 AM (the start can run ~15 min late) |
| Stop | **21:30** every day | Last calling window ends 8 PM; workshop closes 7:30 PM |

Off 9.5 h a night. Optional: keep it off all Sunday (weekly holiday) by starting
only Monday–Saturday (`0 7 * * 1-6`).

### What happens at stop / start

- **Stop** is a normal shutdown: systemd stops `arul-api` and `arul-voice`
  cleanly, so SQLite closes properly. Start boots the VM and systemd starts both
  services by itself (they're `enabled`, Lesson 7). Nothing to do by hand.
- The **static IP stays** (Lesson 4), so the domains keep working after start.
- While off: the CRM shows "Can't reach the server", calls can't be made, and
  the voice page doesn't load. The CRM's live connection reconnects after start.
- **Snapshots still run**: the daily snapshot (`default-schedule-1`, 22:00 UTC =
  03:30 IST) works on a stopped disk.
- Schedules can run **up to ~15 minutes late**. Calls at 07:00 sharp may find it still starting; allow until ~07:15.

### Cost

| | Per month |
|---|---|
| VM compute (e2-small) saved, ~270 h off | about −₹530 |
| Static IP while the VM is off (billed at the "unused" rate) | about +₹130 |
| **Net saving** | **about ₹400 (≈ 20% of the VM bill)** |

Disk and snapshots cost the same whether the VM runs or not.

### One-time permission (the step everyone misses)

GCP stops/starts the VM as the **Compute Engine Service Agent**,
`service-666692619275@compute-system.iam.gserviceaccount.com`. It needs
permission to start and stop VMs, or the schedule silently fails
(the VM page shows an error under *Instance schedule*).

Console: **IAM & Admin → IAM → Grant access** → principal
`service-666692619275@compute-system.iam.gserviceaccount.com` → role
**Compute Instance Admin (v1)** → Save. (Tick *Include Google-provided role
grants* to see it in the list afterwards.)

```bash
gcloud projects add-iam-policy-binding project-2c317546-b2c6-4a7a-8d4 \
  --member=serviceAccount:service-666692619275@compute-system.iam.gserviceaccount.com \
  --role=roles/compute.instanceAdmin.v1
```

### Console

1. **Compute Engine → VM instances → Instance schedules** tab → **Create schedule**.
2. Name `arulmotors-hours`, region **asia-south1**, time zone **Asia/Kolkata**.
3. Start: **Repeat daily**, 07:00. Stop: **Repeat daily**, 21:30.
4. Create → open the schedule → **Add instances to schedule** → `arulmotors` → Add.

### CLI (same thing)

```bash
gcloud compute resource-policies create instance-schedule arulmotors-hours \
  --region=asia-south1 --timezone=Asia/Kolkata \
  --vm-start-schedule="0 7 * * *" --vm-stop-schedule="30 21 * * *" \
  --description="Arul Motors VM: on 07:00-21:30 IST"

gcloud compute instances add-resource-policies arulmotors \
  --zone=asia-south1-c --resource-policies=arulmotors-hours
```

(Schedule format is cron: `minute hour day month weekday`.)

### Check

```bash
gcloud compute resource-policies describe arulmotors-hours --region=asia-south1
gcloud compute instances describe arulmotors --zone=asia-south1-c \
  --format="value(resourcePolicies)"          # → .../arulmotors-hours
```

Next morning: Compute Engine → VM → **Observability/Logs**, or
`gcloud compute operations list --filter="targetLink~arulmotors" --limit=5`
shows `start` / `stop` operations by the service agent.

### Change or remove

```bash
# change times in place (Console: open the schedule → Edit)
gcloud compute resource-policies update instance-schedule arulmotors-hours \
  --region=asia-south1 --vm-start-schedule="0 7 * * *"

# remove it
gcloud compute instances remove-resource-policies arulmotors \
  --zone=asia-south1-c --resource-policies=arulmotors-hours
gcloud compute resource-policies delete arulmotors-hours --region=asia-south1
```

Need the VM outside hours (e.g. a late demo)? Just **Start** it in the
console; it stops again at the next 21:30.

### Remember

- Instance schedule = resource policy attached to the VM (snapshot schedule =
  resource policy attached to the **disk**).
- The service agent needs **Compute Instance Admin (v1)**, or nothing happens.
- Times are in the schedule's time zone (Asia/Kolkata); snapshot times are UTC.

### What happened (fill in)

- Date: 2026-10-23 (set up from the CLI).
- Permission granted: `roles/compute.instanceAdmin.v1` to the Compute Engine
  Service Agent ✓ (it already had `roles/compute.serviceAgent`).
- Schedule `arulmotors-hours` created (asia-south1, Asia/Kolkata, start
  `30 6 * * *`, stop `30 21 * * *`), status READY, attached to `arulmotors` ✓.
- VM was stopped at the time.
- Same day: start moved to **07:00** (`resource-policies update`, in place, no
  re-attach needed). First automatic start due 24 Oct 07:00 IST.
- First automatic stop / start seen: (check the next morning)

---

# Reference

## AWS → GCP names

| AWS | GCP |
|---|---|
| Account | Project (name / **ID** / number) |
| AWS Organizations / SCP | Organization / Org policy |
| EC2 instance / instance type | Compute Engine VM / machine type |
| AMI | Image (image family, e.g. `ubuntu-2404-lts-amd64`) |
| EBS volume / snapshot / DLM | Persistent disk / snapshot / snapshot schedule |
| Elastic IP | Static external IP (reserved / promoted address) |
| Security group | VPC firewall rule + **network tag** |
| VPC / subnet | VPC network (`default`) / regional subnet |
| IAM role on an instance | Service account attached to the VM |
| IAM user access keys | Service account keys (blocked by our org, and not needed) |
| IMDS `169.254.169.254` | Metadata server `metadata.google.internal` |
| `aws` CLI / CloudShell | `gcloud` / Cloud Shell |
| Bedrock | Vertex AI ("Agent Platform") |
| CloudWatch | Cloud Monitoring / Cloud Logging |
| Route 53 | Cloud DNS (we use Cloudflare instead) |
| ACM certificate on ALB | Caddy + Let's Encrypt on the VM (our choice) |
| Cost Explorer / Budgets | Billing reports / Budgets & alerts |
| `ap-south-1` (Mumbai) | `asia-south1` (Mumbai) |

## Key terms

| Term | Meaning |
|---|---|
| **Project ID** | Permanent identifier you type in commands |
| **API (service) enablement** | Per-project switch; off until enabled; free |
| **Principal / role / binding** | Who / bundle of permissions / "who has what, on which resource" |
| **Service account** | Identity for programs; attached to a VM, it provides credentials with no key file (ADC) |
| **Access scopes** | Legacy per-VM limit on what the attached service account can call; use `cloud-platform` + IAM roles |
| **Zone / region** | Data centre / group of zones; VMs are zonal, static IPs are regional |
| **Ephemeral vs static IP** | Changes on stop/start vs reserved until released |
| **Network tag** | Label on a VM that firewall rules target |
| **Ingress / egress** | Incoming / outgoing traffic (egress is allowed by default) |
| **Equivalent code** | Console button showing the `gcloud` command for a form |
| **ADC** | Application Default Credentials: Google libraries find credentials automatically (your `gcloud` login on the Mac, the service account on a VM) |
| **Reverse proxy** | Server (Caddy) in front of the app: HTTPS, then forwards to `127.0.0.1` |

## Everyday commands

```bash
# Who / where
gcloud config list
gcloud config set project project-2c317546-b2c6-4a7a-8d4

# VMs
gcloud compute instances list
gcloud compute ssh arulmotors --zone=asia-south1-c
gcloud compute instances stop|start|delete arulmotors --zone=asia-south1-c
gcloud compute instances describe arulmotors --zone=asia-south1-c
gcloud compute instances add-tags arulmotors --zone=asia-south1-c --tags=TAG
gcloud compute instances remove-tags arulmotors --zone=asia-south1-c --tags=TAG

# IPs
gcloud compute addresses list
gcloud compute addresses create NAME --region=asia-south1 [--addresses=EXISTING_IP]
gcloud compute addresses delete NAME --region=asia-south1

# Firewall
gcloud compute firewall-rules list --format="table(name,sourceRanges.list(),allowed[].map().firewall_rule().list(),targetTags.list())"
gcloud compute firewall-rules create NAME --network=default --direction=INGRESS --action=ALLOW --rules=tcp:443 --source-ranges=0.0.0.0/0 --target-tags=TAG
gcloud compute firewall-rules update NAME --rules=tcp:80,tcp:443
gcloud compute firewall-rules delete NAME --quiet

# APIs, IAM, billing
gcloud services list --enabled
gcloud services enable SERVICE.googleapis.com
gcloud iam service-accounts list
gcloud billing budgets list --billing-account=012671-BA0D6F-B8CAD7

# Disks / backups
gcloud compute disks list
gcloud compute snapshots list
```

**On the VM**

```bash
sudo systemctl status|reload|restart caddy     # web server
sudo journalctl -u caddy -n 50                 # Caddy logs (certificate issues show here)
sudo nano /etc/caddy/Caddyfile                 # domain → app mapping
curl -s localhost:8000                         # is the app answering locally?
ss -tlnp                                       # which ports are listening, on which address
curl -s -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/zone
```

**From the Mac**

```bash
dig +short backend.arulmotors.com              # should be 34.93.71.71 (DNS only)
curl -I https://backend.arulmotors.com         # HTTP status + headers
curl -s https://api.ipify.org                  # your current public IP (it changes)
```

## DNS & Cloudflare

- `arulmotors.com` is registered at GoDaddy, but its **nameservers are
  Cloudflare**, so **all DNS records are edited in Cloudflare**. Records
  added in GoDaddy are ignored.
- **DNS only (grey cloud):** Cloudflare just answers "this name → this IP".
  The browser connects **directly** to the VM. ✅ Use this for the backend:
  - Caddy can get its Let's Encrypt certificate (it must be reachable
    directly on 80/443);
  - any port works (e.g. `:8000` tests);
  - WebRTC voice setup and later telephony WebSockets work without extra
    configuration.
- **Proxied (orange cloud):** Cloudflare's servers sit in between. The name
  resolves to Cloudflare IPs (e.g. `104.21.x.x`, `172.67.x.x`), and only
  certain ports are passed (80, 443, 8080, 8443, …, not 8000). It can be
  added later for DDoS protection, but then Cloudflare SSL must be set to
  "Full (strict)" and certificate issuance changes. Not needed now.
- Other records in the zone (`arulmotors.com`, `admin`, `crm` →
  `118.139.182.126`, `mail`, `cpanel`, …) belong to the GoDaddy hosting.
  Leave them alone.
- Changes take effect in about 1–5 minutes (TTL Auto).

## HTTPS with Caddy

- **How the certificate works:** when Caddy starts with a domain in the
  Caddyfile, it asks Let's Encrypt for a certificate. Let's Encrypt checks
  the domain points to this server, over port 80 or 443. Caddy stores the
  certificate and **renews it about 30 days before expiry**. Nothing is
  manual and it's free.
- **Requirements:**
  - DNS points directly at the VM (DNS only);
  - firewall allows TCP 80 + 443;
  - nothing else is using ports 80/443 on the VM.
- **Files:**
  - config: `/etc/caddy/Caddyfile`;
  - certificates: under `/var/lib/caddy/.local/share/caddy`;
  - service: `caddy` (systemd).
- **Adding another site** (e.g. the API later):
  ```
  api.arulmotors.com {
  	reverse_proxy 127.0.0.1:8001
  }
  ```
  then `sudo systemctl reload caddy`.
- **Password-protecting a site** (used for the voice page):
  ```
  backend.arulmotors.com {
  	basic_auth {
  		USERNAME HASH_FROM_caddy_hash-password
  	}
  	reverse_proxy 127.0.0.1:7860
  }
  ```

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| Browser **times out** | No firewall rule for that port, or the VM lacks the rule's tag → check `firewall-rules list` + the VM's tags |
| Works with `curl localhost` on the VM but not from outside | The app listens on `127.0.0.1` and you're hitting the port directly → use Caddy, or `--host 0.0.0.0` for a test |
| **502 Bad Gateway** from Caddy | The app behind it isn't running (e.g. the SSH session closed and uvicorn stopped) → start it / use systemd |
| Certificate error / Caddy can't get a certificate | DNS is **Proxied** or points elsewhere; port 80/443 closed → DNS only, open 80+443, `journalctl -u caddy` |
| `dig` shows `104.21.x` / `172.67.x` | Cloudflare record is Proxied → set to DNS only |
| Site broke after a VM stop/start | The IP was ephemeral and changed → promote to static, update DNS |
| *"API … has not been used in project … or it is disabled"* | Enable that API (APIs & Services → Library, or `gcloud services enable`) |
| *"constraint … violated"* / org policy error | The org `arulmotorsnazareth-org` blocks it → an org admin can allow it for the project |
| A rule restricted to "your IP" stopped working | Home/office public IP changed (it went 110.226.112.201 → 103.143.7.206 on the same day) → check `api.ipify.org`, update the rule |
| Gemini call from a VM: permission denied | The VM's service account lacks `roles/aiplatform.user`, or its scopes aren't `cloud-platform` (Lesson 5) |

## Security checklist

- ✅ Firewall opens only **TCP 80/443** to the internet for the web app.
- ⚠️ **`default-allow-rdp` (3389) is open to the world.** RDP is for
  Windows; a Linux VM doesn't need it. Consider deleting it:
  `gcloud compute firewall-rules delete default-allow-rdp`.
- ⚠️ **`default-allow-ssh` (22) is open to the world.** Keys protect it, but
  it can later be limited to Google's IAP range `35.235.240.0/20`, so only
  the console SSH button / `gcloud compute ssh --tunnel-through-iap` works.
- ✅ No API keys or service-account key files (the org blocks them). Use
  attached service accounts.
- ✅ Programs get **narrow roles** (`roles/aiplatform.user`), never Owner/Editor.
- ✅ Apps listen on `127.0.0.1` behind Caddy, not on public ports.
- For the real app: password on the voice page (Caddy `basic_auth`) and
  `API_KEY` on the API.

## Sizing and costs (2026-10-19)

At most **2 people at a time**. Gemini does the speech work on Vertex, so the
VM only relays audio and runs the API and SQLite.

| Item | Choice | Approx. per month |
|---|---|---|
| VM + boot disk | **e2-small** (2 shared vCPUs, 2 GB), Regular (not Spot), Mumbai, 730 h, balanced 20 GB | **₹1,640** (pricing calculator) |
| Static external IP | 1 | ₹300–350 |
| Snapshots | Daily, 14 days | ₹50–100 |
| **VM total** | | **about ₹2,000** |
| Gemini on Vertex | Per call | ₹2–7 per call |

- **Not e2-micro** (1 GB is too tight). **Not Spot** (it can be stopped mid-call).
- "0.5 vCPU" = a guaranteed half CPU, bursting to 2. If audio stutters
  under load: stop → Edit → e2-medium → start.
- The current practice VM `arulmotors` is already e2-small, so it costs the
  same as the planned server while it runs.
- Consider a 1-year committed use discount after 2–3 stable months.
- Exact prices: [cloud.google.com/products/calculator](https://cloud.google.com/products/calculator).

## Notes and problems hit

| Date | What happened | Fix |
|---|---|---|
| 2026-10-19 | Billing page: "an administrator must verify this account"; still on the free trial | **Verify account**, then **Upgrade** before 17 Jan 2027 |
| 2026-10-19 | Budget list via CLI failed: Budget API disabled | Enabled `billingbudgets.googleapis.com` (Lesson 1) |
| 2026-10-19 | VM created with console defaults (Debian, e2-small, zone -c) instead of the planned ones | Fine for practice; check every field next time |
| 2026-10-19 | Ephemeral IP changed on stop/start (34.100.155.125 → 34.93.71.71) | Promoted to static `arulmotors-ip` |
| 2026-10-19 | `backend.arulmotors.com` resolved to Cloudflare IPs | The record was Proxied → **DNS only**. DNS lives in Cloudflare, not GoDaddy |
| 2026-10-19 | Firewall rule created with **Protocols: Allow all** + `0.0.0.0/0` (every port public) | Changed to **tcp:80,tcp:443** |
| 2026-10-19 | UDP rule `arul-voice-webrtc` created with target tag `arul-voice-webrtc` (the rule's own name), so it applied to no VM | Changed the target tag to `arulmotors-web` (the VM's tag). A rule only applies to VMs carrying its target tag |
| 2026-10-19 | Mac's public IP changed during the day | Prefer `0.0.0.0/0` + app-level auth for web; don't rely on a home IP |
| 2026-10-18 | Org policy disallows API keys | Use ADC / service accounts (Lesson 5); never create a Gemini API key |
| 2026-10-18 | Vertex rejected the "thinking" setting for `gemini-3.8-live` | Removed from `bot.py` |
