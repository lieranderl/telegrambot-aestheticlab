#!/usr/bin/env bash
set -euo pipefail

project_id="${PROJECT_ID:?Set PROJECT_ID}"
database="${FIRESTORE_DATABASE:-(default)}"
state_collection_prefix="${STATE_COLLECTION_PREFIX:-calendar_telegram}"
delivery_collection_group="${state_collection_prefix}_deliveries"

output_file="$(mktemp)"
trap 'rm -f "${output_file}"' EXIT

if gcloud firestore fields ttls update expires_at \
  --collection-group="${delivery_collection_group}" \
  --database="${database}" \
  --enable-ttl \
  --async \
  --project="${project_id}" \
  --quiet >"${output_file}" 2>&1; then
  cat "${output_file}"
  exit 0
fi

cat "${output_file}" >&2
if grep -Eiq "PERMISSION_DENIED|permission denied" "${output_file}"; then
  echo "Firestore TTL setup failed because the deploy identity lacks permission." >&2
  echo "Grant the deploy identity permission to manage Firestore TTL policies." >&2
fi

exit 1
