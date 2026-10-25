#!/usr/bin/env bash
set -euo pipefail

# Default service name is distinct from oas-spec-test ('oas-testsgen-app')
# so existing oas-spec-test deployments are never impacted.
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:-helloayush-prj-1}"
REGION="${CLOUDRUN_REGION:-us-central1}"
VERTEX_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
SERVICE_NAME="${CLOUDRUN_SERVICE_NAME:-oasverify-app}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.8-flash}"

echo "Deploying ${SERVICE_NAME} to Cloud Run (project=${PROJECT_ID}, region=${REGION}, vertex_location=${VERTEX_LOCATION})..."

gcloud run deploy "${SERVICE_NAME}" \
  --source . \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 10 \
  --cpu 2 \
  --memory 2Gi \
  --timeout 900 \
  --session-affinity \
  --no-cpu-throttling \
  --set-env-vars="SERVE_REACT_SPA=1,GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION},GEMINI_MODEL=${GEMINI_MODEL}"

gcloud run services update-traffic "${SERVICE_NAME}" \
  --to-latest \
  --region "${REGION}" \
  --project "${PROJECT_ID}"

echo "Deployment complete!"
