#!/usr/bin/env bash
# One-time GCP setup for the CI/CD pipeline (.github/workflows/backend.yml).
# Run from a machine where `gcloud` is logged in as a project owner:
#
#   bash deploy/gcp-setup-cicd.sh
#
# Creates (all idempotent, safe to re-run):
#   - a service account, github-deployer, that can only start/reach this one VM
#   - a Workload Identity pool + GitHub provider, so GitHub Actions signs in
#     with a short-lived token: NO service-account key exists anywhere. Only
#     runs of this repository on its main branch are accepted.
#   - a firewall rule so the deploy can reach SSH through Google's IAP tunnel
# Then prints the two values the workflow needs (set as GitHub repo variables).
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project)}"
ZONE="${ZONE:-asia-south1-c}"
VM="${VM:-arulmotors}"
GH_REPO="${GH_REPO:-alanjosh05/Arul-motors-voice-agent}"
SA_NAME=github-deployer
POOL=github-actions
PROVIDER=github
ROLE_ID=arulDeployer
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
VM_SA="$(gcloud compute instances describe "$VM" --zone "$ZONE" --format='value(serviceAccounts[0].email)')"
NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"

echo ">> APIs"
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com iap.googleapis.com compute.googleapis.com

echo ">> Service account $SA"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --display-name "GitHub Actions deployer (keyless)"

echo ">> Custom role $ROLE_ID: just enough to start the VM and SSH to it"
PERMS="compute.instances.get,compute.instances.list,compute.instances.start,compute.instances.setMetadata,\
compute.projects.get,compute.projects.setCommonInstanceMetadata,compute.zones.get,compute.zoneOperations.get,\
compute.globalOperations.get,compute.instances.osLogin,iap.tunnelInstances.accessViaIAP"
if gcloud iam roles describe "$ROLE_ID" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam roles update "$ROLE_ID" --project "$PROJECT" --permissions "$PERMS" --quiet >/dev/null
else
  gcloud iam roles create "$ROLE_ID" --project "$PROJECT" --title "Arul deployer" --permissions "$PERMS" >/dev/null
fi
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role "projects/$PROJECT/roles/$ROLE_ID" --condition=None --quiet >/dev/null
# SSH needs to act as the VM's own service account.
gcloud iam service-accounts add-iam-policy-binding "$VM_SA" --member "serviceAccount:$SA" \
  --role roles/iam.serviceAccountUser --quiet >/dev/null
# IAP tunnel access.
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
  --role roles/iap.tunnelResourceAccessor --condition=None --quiet >/dev/null

echo ">> Firewall: IAP's range may reach SSH on the VM"
gcloud compute firewall-rules describe allow-iap-ssh >/dev/null 2>&1 ||
  gcloud compute firewall-rules create allow-iap-ssh --direction INGRESS --allow tcp:22 \
    --source-ranges 35.235.240.0/20 --target-tags arulmotors-web

echo ">> Workload Identity pool and GitHub provider (only $GH_REPO on main)"
gcloud iam workload-identity-pools describe "$POOL" --location global >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools create "$POOL" --location global --display-name "GitHub Actions"
CONDITION="assertion.repository=='$GH_REPO' && assertion.ref=='refs/heads/main'"
if gcloud iam workload-identity-pools providers describe "$PROVIDER" --location global --workload-identity-pool "$POOL" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --location global --workload-identity-pool "$POOL" \
    --attribute-condition "$CONDITION" --quiet >/dev/null
else
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --location global --workload-identity-pool "$POOL" \
    --issuer-uri "https://token.actions.githubusercontent.com" \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition "$CONDITION"
fi
gcloud iam service-accounts add-iam-policy-binding "$SA" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$GH_REPO" \
  --quiet >/dev/null

PROVIDER_NAME="projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
echo
echo "Done. Set these as GitHub repository variables (Settings > Secrets and variables > Actions > Variables):"
echo "  GCP_WIF_PROVIDER = $PROVIDER_NAME"
echo "  GCP_DEPLOY_SA    = $SA"
