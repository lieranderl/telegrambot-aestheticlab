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

if [[ $# -ge 2 ]]; then
  admin_api_token="$2"
elif [[ -n "${ADMIN_API_TOKEN:-}" ]]; then
  admin_api_token="$ADMIN_API_TOKEN"
else
  echo "Missing admin API token." >&2
  echo "Pass it as the second argument or set ADMIN_API_TOKEN." >&2
  exit 1
fi

curl --fail --silent --show-error \
  -X POST \
  -H "X-Admin-Token: ${admin_api_token}" \
  --data '' \
  "${admin_service_url%/}/admin/register"
