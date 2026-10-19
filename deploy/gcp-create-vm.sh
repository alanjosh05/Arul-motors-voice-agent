#!/usr/bin/env bash
# Create the Google Cloud VM for the voice agent. Run this on your Mac (gcloud
# logged in). Safe to re-run: existing resources are kept.
#
#   bash deploy/gcp-create-vm.sh
set -euo pipefail

PROJECT="${PROJECT:-project-2c317546-b2c6-4a7a-8d4}"
REGION="${REGION:-asia-south1}"          # Mumbai: close to customers; India hosting for Plivo later
ZONE="${ZONE:-asia-south1-a}"
VM="${VM:-arul-voice}"
MACHINE="${MACHINE:-e2-small}"       # 2 shared vCPU, 2 GB: enough for ~2 users at a time
SA_NAME="arul-voice-agent"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT"
gcloud services enable compute.googleapis.com aiplatform.googleapis.com

# Service account the VM runs as. It may call Vertex AI and nothing else; the
# bot picks it up automatically (ADC), so no key file is ever created.
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --display-name="Arul Motors voice agent"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}" --role="roles/aiplatform.user" --condition=None >/dev/null

# Static IP: GoDaddy DNS points voice. and api. at it, so it must never change.
gcloud compute addresses describe "${VM}-ip" --region="$REGION" >/dev/null 2>&1 ||
  gcloud compute addresses create "${VM}-ip" --region="$REGION"
IP="$(gcloud compute addresses describe "${VM}-ip" --region="$REGION" --format='value(address)')"

# HTTPS for the web page, plus the UDP range WebRTC audio uses (Linux's
# ephemeral ports, which aiortc picks from).
gcloud compute firewall-rules describe arul-voice-web >/dev/null 2>&1 ||
  gcloud compute firewall-rules create arul-voice-web --allow=tcp:80,tcp:443 --target-tags=arul-voice
gcloud compute firewall-rules describe arul-voice-webrtc >/dev/null 2>&1 ||
  gcloud compute firewall-rules create arul-voice-webrtc --allow=udp:32768-60999 --target-tags=arul-voice

gcloud compute instances describe "$VM" --zone="$ZONE" >/dev/null 2>&1 ||
  gcloud compute instances create "$VM" \
    --zone="$ZONE" \
    --machine-type="$MACHINE" \
    --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
    --boot-disk-size=20GB \
    --address="$IP" \
    --tags=arul-voice \
    --service-account="$SA" --scopes=cloud-platform

# Daily disk snapshots (02:00 IST), kept 14 days: the backup of the SQLite
# database. Restore = create a disk from a snapshot.
gcloud compute resource-policies describe arul-daily-backup --region="$REGION" >/dev/null 2>&1 ||
  gcloud compute resource-policies create snapshot-schedule arul-daily-backup \
    --region="$REGION" --daily-schedule --start-time=20:30 \
    --max-retention-days=14 --storage-location="$REGION"
gcloud compute disks add-resource-policies "$VM" --zone="$ZONE" \
  --resource-policies=arul-daily-backup >/dev/null 2>&1 || true

echo
echo "VM ready: $VM ($IP)"
echo "Next:"
echo "  1. GoDaddy DNS: add A records  voice -> $IP  and  api -> $IP"
echo "  2. Copy the code (see deploy/README.md, step 2)"
echo "  3. On the VM: sudo bash ~/arul-motors/deploy/vm-setup.sh voice.YOURDOMAIN api.YOURDOMAIN https://YOURDOMAIN"
