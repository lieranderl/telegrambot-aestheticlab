#!/usr/bin/env bash
set -euo pipefail

project_id="${PROJECT_ID:?Set PROJECT_ID}"
region="${REGION:?Set REGION}"
admin_service="${ADMIN_SERVICE:?Set ADMIN_SERVICE}"
verify_public_service="${VERIFY_PUBLIC_SERVICE:-true}"

describe_value() {
  local service="$1"
  local field="$2"

  gcloud run services describe "${service}" \
    --region "${region}" \
    --project "${project_id}" \
    --format="value(${field})"
}

require_ready() {
  local service="$1"
  local revision
  local url

  revision="$(describe_value "${service}" "status.latestReadyRevisionName")"
  url="$(describe_value "${service}" "status.url")"

  if [[ -z "${revision}" ]]; then
    echo "Cloud Run service ${service} has no ready revision" >&2
    exit 1
  fi
  if [[ -z "${url}" ]]; then
    echo "Cloud Run service ${service} has no service URL" >&2
    exit 1
  fi

  echo "${service} ready at ${revision}"
}

require_ready "${admin_service}"

if [[ "${verify_public_service}" == "true" ]]; then
  public_service="${SERVICE:?Set SERVICE}"
  require_ready "${public_service}"

  public_url="$(describe_value "${public_service}" "status.url")"
  curl --fail --silent --show-error --max-time 15 "${public_url}/health" >/dev/null
  echo "Public health check passed."
fi
