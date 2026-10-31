#!/usr/bin/env bash
set -euo pipefail

project_id="${PROJECT_ID:?Set PROJECT_ID}"
database="${FIRESTORE_DATABASE:-(default)}"
state_collection_prefix="${STATE_COLLECTION_PREFIX:-calendar_telegram}"
ttl_collection_groups=(
  "${state_collection_prefix}_deliveries"
  "${state_collection_prefix}_events"
)

output_file="$(mktemp)"
trap 'rm -f "${output_file}"' EXIT

for collection_group in "${ttl_collection_groups[@]}"; do
  if gcloud firestore fields ttls update expires_at \
    --collection-group="${collection_group}" \
    --database="${database}" \
    --enable-ttl \
    --async \
    --project="${project_id}" \
    --quiet >"${output_file}" 2>&1; then
    cat "${output_file}"
    continue
  fi

  cat "${output_file}" >&2
  if grep -Eiq "PERMISSION_DENIED|permission denied" "${output_file}"; then
    echo "Firestore TTL setup failed because the deploy identity lacks permission." >&2
    echo "Grant the deploy identity permission to manage Firestore TTL policies." >&2
  fi

  exit 1
done
