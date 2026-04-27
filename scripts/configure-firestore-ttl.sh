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
if grep -q "PERMISSION_DENIED" "${output_file}"; then
  echo "Warning: Firestore TTL setup skipped because the deploy identity lacks permission." >&2
  echo "Run this script with an identity that can manage Firestore TTL policies." >&2
  exit 0
fi

exit 1
