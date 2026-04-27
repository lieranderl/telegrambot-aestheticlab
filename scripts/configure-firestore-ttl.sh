#!/usr/bin/env bash
set -euo pipefail

project_id="${PROJECT_ID:?Set PROJECT_ID}"
database="${FIRESTORE_DATABASE:-(default)}"
state_collection_prefix="${STATE_COLLECTION_PREFIX:-calendar_telegram}"
delivery_collection_group="${state_collection_prefix}_deliveries"

gcloud firestore fields ttls update expires_at \
  --collection-group="${delivery_collection_group}" \
  --database="${database}" \
  --enable-ttl \
  --async \
  --project="${project_id}" \
  --quiet
