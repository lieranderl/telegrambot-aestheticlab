#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ge 1 ]]; then
  admin_service_url="$1"
elif [[ -n "${ADMIN_SERVICE_URL:-}" ]]; then
  admin_service_url="$ADMIN_SERVICE_URL"
else
  echo "Usage: $0 <admin-service-url>" >&2
  echo "Or set ADMIN_SERVICE_URL in the environment." >&2
  exit 1
fi

admin_service_url="${admin_service_url%/}"
curl_headers=()

print_identity_token() {
  if [[ -n "${ADMIN_IMPERSONATE_SERVICE_ACCOUNT:-}" ]]; then
    gcloud auth print-identity-token \
      --audiences="${admin_service_url}" \
      --impersonate-service-account="${ADMIN_IMPERSONATE_SERVICE_ACCOUNT}"
  else
    gcloud auth print-identity-token
  fi
}

if [[ "${ADMIN_SKIP_IAM_AUTH:-}" != "1" ]]; then
  if [[ -n "${ADMIN_ID_TOKEN:-}" ]]; then
    identity_token="$ADMIN_ID_TOKEN"
  else
    if ! command -v gcloud >/dev/null 2>&1; then
      echo "Missing gcloud. Install Google Cloud CLI, set ADMIN_ID_TOKEN, or set ADMIN_SKIP_IAM_AUTH=1 for local calls." >&2
      exit 1
    fi
    identity_token="$(print_identity_token)"
  fi

  curl_headers+=(-H "Authorization: Bearer ${identity_token}")
fi

curl --fail --silent --show-error \
  -X POST \
  "${curl_headers[@]}" \
  --data '' \
  "${admin_service_url}/admin/register"
