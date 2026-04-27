#!/usr/bin/env bash
set -euo pipefail

project_id="${PROJECT_ID:?Set PROJECT_ID}"
region="${REGION:-europe-west1}"
admin_service="${ADMIN_SERVICE:-aestheticlab-calendar-telegram-admin}"
scheduler_job="${SCHEDULER_JOB:-aestheticlab-calendar-telegram-renew}"
scheduler_service_account_id="${SCHEDULER_SERVICE_ACCOUNT_ID:-calendar-telegram-renewer}"
scheduler_service_account_email="${scheduler_service_account_id}@${project_id}.iam.gserviceaccount.com"

admin_url="$(gcloud run services describe "${admin_service}" \
  --region "${region}" \
  --project "${project_id}" \
  --format='value(status.url)')"

if ! gcloud iam service-accounts describe "${scheduler_service_account_email}" \
  --project "${project_id}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${scheduler_service_account_id}" \
    --project "${project_id}" \
    --display-name "Calendar Telegram renewal scheduler"
fi

gcloud run services add-iam-policy-binding "${admin_service}" \
  --region "${region}" \
  --project "${project_id}" \
  --member "serviceAccount:${scheduler_service_account_email}" \
  --role roles/run.invoker \
  --quiet

if gcloud scheduler jobs describe "${scheduler_job}" \
  --location "${region}" \
  --project "${project_id}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${scheduler_job}" \
    --location "${region}" \
    --project "${project_id}" \
    --schedule "0 */6 * * *" \
    --time-zone "Etc/UTC" \
    --uri "${admin_url}/admin/renew" \
    --http-method POST \
    --oidc-service-account-email "${scheduler_service_account_email}" \
    --oidc-token-audience "${admin_url}" \
    --quiet
else
  gcloud scheduler jobs create http "${scheduler_job}" \
    --location "${region}" \
    --project "${project_id}" \
    --schedule "0 */6 * * *" \
    --time-zone "Etc/UTC" \
    --uri "${admin_url}/admin/renew" \
    --http-method POST \
    --oidc-service-account-email "${scheduler_service_account_email}" \
    --oidc-token-audience "${admin_url}" \
    --quiet
fi
