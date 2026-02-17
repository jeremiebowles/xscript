#!/usr/bin/env bash
set -Eeuo pipefail

# One-shot Cloud Run deployment for cute-poetry service.
# Idempotent: safe to run multiple times.

PROJECT_ID="${PROJECT_ID:-$(gcloud config get project 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-cute-poetry}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-cute-poetry-sa}"
RUNTIME_SA_EMAIL="${RUNTIME_SA_EMAIL:-}"
REDDIT_UA="${REDDIT_UA:-cute-poetry-bot/1.0 (+jeremie.bowles@gmail.com)}"
POETRY_API_URL_VALUE="${POETRY_API_URL_VALUE:-https://poetrydb.org/random/20}"
REDDIT_CLIENT_ID_VALUE="${REDDIT_CLIENT_ID_VALUE:-}"
REDDIT_CLIENT_SECRET_VALUE="${REDDIT_CLIENT_SECRET_VALUE:-}"
SCHEDULER_JOB_NAME="${SCHEDULER_JOB_NAME:-cute-poetry-every-6h}"
ENABLE_SCHEDULER="${ENABLE_SCHEDULER:-true}"
SKIP_CONNECTIVITY_CHECK="${SKIP_CONNECTIVITY_CHECK:-false}"
SKIP_IAM_BINDINGS="${SKIP_IAM_BINDINGS:-false}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '\n[%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$*"; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

require_cmd gcloud
require_cmd curl

if [[ -z "${PROJECT_ID}" ]]; then
  echo "PROJECT_ID is empty. Set it explicitly, e.g.:" >&2
  echo "  PROJECT_ID=your-project-id bash ./deploy_cute_poetry_cloud_run.sh" >&2
  exit 1
fi

# Ensure gcloud can write its local config in restricted environments.
if [[ -z "${CLOUDSDK_CONFIG:-}" ]]; then
  export CLOUDSDK_CONFIG="/tmp/gcloud_config"
fi
mkdir -p "${CLOUDSDK_CONFIG}"

# Fail early with a clear message if Google APIs are unreachable.
# Do not require 2xx, because some Google API roots return 404 by design.
if [[ "${SKIP_CONNECTIVITY_CHECK}" != "true" ]]; then
  if ! curl -sS --max-time 10 -I https://cloudresourcemanager.googleapis.com >/dev/null 2>&1; then
    echo "Cannot reach Google APIs (DNS/network issue). Check internet/DNS, then retry." >&2
    exit 1
  fi
fi

log "Using project=${PROJECT_ID} region=${REGION} service=${SERVICE_NAME}"

log "Setting gcloud project"
gcloud config set project "${PROJECT_ID}" >/dev/null

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
if [[ -z "${PROJECT_NUMBER}" ]]; then
  echo "Could not resolve project number for ${PROJECT_ID}" >&2
  exit 1
fi

if [[ -z "${RUNTIME_SA_EMAIL}" ]]; then
  RUNTIME_SA_EMAIL="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
fi

COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

log "Enabling required APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com >/dev/null

log "Ensuring runtime service account exists: ${RUNTIME_SA_EMAIL}"
if ! gcloud iam service-accounts describe "${RUNTIME_SA_EMAIL}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${RUNTIME_SA_NAME}" --display-name="Cute Poetry Runtime SA" >/dev/null
fi

log "Creating/updating Secret Manager values"
if printf '%s' "${REDDIT_UA}" | gcloud secrets create reddit-user-agent --replication-policy=automatic --data-file=- >/dev/null 2>&1; then
  :
else
  printf '%s' "${REDDIT_UA}" | gcloud secrets versions add reddit-user-agent --data-file=- >/dev/null
fi

if printf '%s' "${POETRY_API_URL_VALUE}" | gcloud secrets create poetry-api-url --replication-policy=automatic --data-file=- >/dev/null 2>&1; then
  :
else
  printf '%s' "${POETRY_API_URL_VALUE}" | gcloud secrets versions add poetry-api-url --data-file=- >/dev/null
fi

if [[ -n "${REDDIT_CLIENT_ID_VALUE}" && -n "${REDDIT_CLIENT_SECRET_VALUE}" ]]; then
  if printf '%s' "${REDDIT_CLIENT_ID_VALUE}" | gcloud secrets create reddit-client-id --replication-policy=automatic --data-file=- >/dev/null 2>&1; then
    :
  else
    printf '%s' "${REDDIT_CLIENT_ID_VALUE}" | gcloud secrets versions add reddit-client-id --data-file=- >/dev/null
  fi

  if printf '%s' "${REDDIT_CLIENT_SECRET_VALUE}" | gcloud secrets create reddit-client-secret --replication-policy=automatic --data-file=- >/dev/null 2>&1; then
    :
  else
    printf '%s' "${REDDIT_CLIENT_SECRET_VALUE}" | gcloud secrets versions add reddit-client-secret --data-file=- >/dev/null
  fi
fi

if [[ "${SKIP_IAM_BINDINGS}" != "true" ]]; then
  log "Granting runtime SA access to secrets"
  gcloud secrets add-iam-policy-binding reddit-user-agent \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null

  gcloud secrets add-iam-policy-binding poetry-api-url \
    --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null

  if [[ -n "${REDDIT_CLIENT_ID_VALUE}" && -n "${REDDIT_CLIENT_SECRET_VALUE}" ]]; then
    gcloud secrets add-iam-policy-binding reddit-client-id \
      --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
      --role="roles/secretmanager.secretAccessor" >/dev/null

    gcloud secrets add-iam-policy-binding reddit-client-secret \
      --member="serviceAccount:${RUNTIME_SA_EMAIL}" \
      --role="roles/secretmanager.secretAccessor" >/dev/null
  fi

  log "Granting build identities required roles"
  for SA in "${COMPUTE_SA}" "${CLOUDBUILD_SA}"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${SA}" \
      --role="roles/storage.objectViewer" >/dev/null

    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${SA}" \
      --role="roles/artifactregistry.writer" >/dev/null

    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${SA}" \
      --role="roles/logging.logWriter" >/dev/null

    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${SA}" \
      --role="roles/run.builder" >/dev/null
  done

  log "Granting bucket-level read on Cloud Run source bucket when detectable"
  SOURCE_BUCKET="$(gcloud storage buckets list --project "${PROJECT_ID}" --format='value(name)' | grep -E 'run-sources|cloud-run-source-deploy|gcf-v2-sources' | head -n1 || true)"
  if [[ -n "${SOURCE_BUCKET}" ]]; then
    for SA in "${COMPUTE_SA}" "${CLOUDBUILD_SA}"; do
      gcloud storage buckets add-iam-policy-binding "gs://${SOURCE_BUCKET}" \
        --member="serviceAccount:${SA}" \
        --role="roles/storage.objectViewer" >/dev/null || true
    done
    log "Source bucket policy set on gs://${SOURCE_BUCKET}"
  else
    log "No source bucket auto-detected yet (this is OK before first successful build)."
  fi
else
  log "Skipping IAM bindings (SKIP_IAM_BINDINGS=true)"
fi

log "Deploying Cloud Run service"
cd "${ROOT_DIR}"
SECRET_FLAGS="USER_AGENT=reddit-user-agent:latest,POETRY_API_URL=poetry-api-url:latest"
if [[ -n "${REDDIT_CLIENT_ID_VALUE}" && -n "${REDDIT_CLIENT_SECRET_VALUE}" ]]; then
  SECRET_FLAGS="${SECRET_FLAGS},REDDIT_CLIENT_ID=reddit-client-id:latest,REDDIT_CLIENT_SECRET=reddit-client-secret:latest"
fi
gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --region "${REGION}" \
  --allow-unauthenticated \
  --service-account "${RUNTIME_SA_EMAIL}" \
  --set-secrets "${SECRET_FLAGS}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --region "${REGION}" --format='value(status.url)')"
if [[ -z "${SERVICE_URL}" ]]; then
  echo "Deploy returned no service URL." >&2
  exit 1
fi

log "Smoke test"
curl -fsS "${SERVICE_URL}/?species=dog" >/dev/null
log "Smoke test passed: ${SERVICE_URL}/?species=dog"

if [[ "${ENABLE_SCHEDULER}" == "true" ]]; then
  log "Creating/updating 6-hour Cloud Scheduler job"
  if gcloud scheduler jobs describe "${SCHEDULER_JOB_NAME}" --location="${REGION}" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "${SCHEDULER_JOB_NAME}" \
      --location="${REGION}" \
      --schedule="0 */6 * * *" \
      --uri="${SERVICE_URL}/" \
      --http-method=GET >/dev/null
  else
    gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" \
      --location="${REGION}" \
      --schedule="0 */6 * * *" \
      --uri="${SERVICE_URL}/" \
      --http-method=GET >/dev/null
  fi
  log "Scheduler ready: ${SCHEDULER_JOB_NAME}"
fi

log "Done"
log "Service URL: ${SERVICE_URL}"
