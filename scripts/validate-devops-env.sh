#!/usr/bin/env bash
set -euo pipefail

errors=()

require_var() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    errors+=("Set ${name}")
  fi
}

require_match() {
  local name="$1"
  local pattern="$2"
  local message="$3"

  if [[ -n "${!name:-}" && ! "${!name}" =~ ${pattern} ]]; then
    errors+=("${message}")
  fi
}

DEPLOY_ENVIRONMENT="${DEPLOY_ENVIRONMENT:-}"

for name in \
  DEPLOY_ENVIRONMENT \
  PROJECT_ID \
  REGION \
  SERVICE \
  ADMIN_SERVICE \
  RUNTIME_SERVICE_ACCOUNT \
  DOCKER_IMAGE \
  IMAGE_TAG \
  GCP_WORKLOAD_ID_PROVIDER \
  GCP_SERVICE_ACCOUNT_GITHUB \
  TELEGRAM_CHAT_ID \
  WEBHOOK_URL \
  STATE_COLLECTION_PREFIX \
  RENEWAL_LEAD_MINUTES \
  DELIVERY_TTL_DAYS \
  TELEGRAM_TOKEN_SECRET_NAME \
  CALENDAR_IDS_SECRET_NAME \
  SCHEDULER_JOB \
  SCHEDULER_SERVICE_ACCOUNT_ID; do
  require_var "${name}"
done

if [[ "${REQUIRE_DOCKER_LOGIN:-false}" == "true" ]]; then
  require_var DOCKERHUB_USERNAME
  require_var DOCKERHUB_TOKEN
fi

require_match DEPLOY_ENVIRONMENT '^production$' \
  "DEPLOY_ENVIRONMENT must be production"
require_match REGION '^[a-z]+-[a-z]+[0-9]$' \
  "REGION must be a valid Google Cloud region name"
require_match PROJECT_ID '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' \
  "PROJECT_ID must be a valid Google Cloud project ID"
require_match SERVICE '^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$' \
  "SERVICE must be a valid Cloud Run service name"
require_match ADMIN_SERVICE '^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$' \
  "ADMIN_SERVICE must be a valid Cloud Run service name"
require_match RUNTIME_SERVICE_ACCOUNT '^[^@[:space:]]+@[^@[:space:]]+\.iam\.gserviceaccount\.com$' \
  "RUNTIME_SERVICE_ACCOUNT must be a service account email"
require_match GCP_SERVICE_ACCOUNT_GITHUB '^[^@[:space:]]+@[^@[:space:]]+\.iam\.gserviceaccount\.com$' \
  "GCP_SERVICE_ACCOUNT_GITHUB must be a service account email"
require_match WEBHOOK_URL '^https://[^[:space:]/]+/.+' \
  "WEBHOOK_URL must be an absolute https URL"
require_match STATE_COLLECTION_PREFIX '^[A-Za-z0-9_]+$' \
  "STATE_COLLECTION_PREFIX can contain only letters, numbers, and underscores"
require_match RENEWAL_LEAD_MINUTES '^[1-9][0-9]*$' \
  "RENEWAL_LEAD_MINUTES must be a positive integer"
require_match DELIVERY_TTL_DAYS '^[1-9][0-9]*$' \
  "DELIVERY_TTL_DAYS must be a positive integer"
require_match TELEGRAM_TOKEN_SECRET_NAME '^[A-Za-z0-9_-]+$' \
  "TELEGRAM_TOKEN_SECRET_NAME must be a Secret Manager secret name"
require_match CALENDAR_IDS_SECRET_NAME '^[A-Za-z0-9_-]+$' \
  "CALENDAR_IDS_SECRET_NAME must be a Secret Manager secret name"
require_match SCHEDULER_JOB '^[A-Za-z][A-Za-z0-9_-]*$' \
  "SCHEDULER_JOB must be a valid Cloud Scheduler job name"
require_match SCHEDULER_SERVICE_ACCOUNT_ID '^[a-z][a-z0-9-]{4,28}[a-z0-9]$' \
  "SCHEDULER_SERVICE_ACCOUNT_ID must be a valid service account ID"
require_match REQUIRE_DOCKER_LOGIN '^(true|false)$' \
  "REQUIRE_DOCKER_LOGIN must be true or false"

if [[ -n "${SERVICE:-}" && -n "${ADMIN_SERVICE:-}" && "${SERVICE}" == "${ADMIN_SERVICE}" ]]; then
  errors+=("SERVICE and ADMIN_SERVICE must be different")
fi

if [[ -n "${IMAGE_TAG:-}" && "${IMAGE_TAG}" == "latest" ]]; then
  errors+=("IMAGE_TAG must be immutable; latest is not allowed")
fi

if [[ -n "${DOCKER_IMAGE:-}" && "${DOCKER_IMAGE}" == *":latest" ]]; then
  errors+=("DOCKER_IMAGE must not include a mutable :latest tag")
fi

if [[ "${DEPLOY_ENVIRONMENT}" == "production" ]]; then
  if [[ -n "${IMAGE_TAG:-}" && ! "${IMAGE_TAG}" =~ ^[0-9a-f]{40}$ ]]; then
    errors+=("Production IMAGE_TAG must be a 40-character git SHA")
  fi
fi

if (( ${#errors[@]} > 0 )); then
  printf 'DevOps input validation failed:\n' >&2
  printf ' - %s\n' "${errors[@]}" >&2
  exit 1
fi

echo "DevOps input validation passed for ${DEPLOY_ENVIRONMENT}."
