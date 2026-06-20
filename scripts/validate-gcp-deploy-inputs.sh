#!/usr/bin/env bash
set -euo pipefail

project_id="${PROJECT_ID:?Set PROJECT_ID}"
runtime_service_account="${RUNTIME_SERVICE_ACCOUNT:?Set RUNTIME_SERVICE_ACCOUNT}"
telegram_token_secret_name="${TELEGRAM_TOKEN_SECRET_NAME:?Set TELEGRAM_TOKEN_SECRET_NAME}"
calendar_ids_secret_name="${CALENDAR_IDS_SECRET_NAME:?Set CALENDAR_IDS_SECRET_NAME}"

gcloud iam service-accounts describe "${runtime_service_account}" \
  --project "${project_id}" >/dev/null

for secret_name in "${telegram_token_secret_name}" "${calendar_ids_secret_name}"; do
  gcloud secrets describe "${secret_name}" \
    --project "${project_id}" >/dev/null
  gcloud secrets versions describe latest \
    --secret "${secret_name}" \
    --project "${project_id}" >/dev/null
done

echo "Google Cloud deployment inputs validated."
