#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-helloayush-prj-1}"
REGION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
SERVICE_NAME="${CLOUD_RUN_SERVICE:-oas-crud-test-server}"
OAS_FILE="${REPO_ROOT}/sample_oas/cloud_run_crud_api.yaml"

echo "Deploying ${SERVICE_NAME} to Cloud Run (project=${PROJECT_ID}, region=${REGION})..."

gcloud run deploy "${SERVICE_NAME}" \
  --source "${SCRIPT_DIR}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --max-instances 1 \
  --concurrency 80 \
  --memory 512Mi \
  --quiet

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format 'value(status.url)')"

if [[ -z "${SERVICE_URL}" ]]; then
  echo "Error: Could not retrieve Cloud Run service URL." >&2
  exit 1
fi

echo "Deployed Cloud Run URL: ${SERVICE_URL}"

if [[ -f "${OAS_FILE}" ]]; then
  python3 -c "
import re
p = '${OAS_FILE}'
with open(p, 'r') as f:
    text = f.read()
text = re.sub(r'(- url:\s*)(\S+)', r'\g<1>${SERVICE_URL}', text, count=1)
with open(p, 'w') as f:
    f.write(text)
"
  echo "Updated servers[0].url in ${OAS_FILE} -> ${SERVICE_URL}"
fi
