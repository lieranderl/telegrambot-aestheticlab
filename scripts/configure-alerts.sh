#!/usr/bin/env bash
set -euo pipefail

project_id="${PROJECT_ID:?Set PROJECT_ID}"
region="${REGION:-europe-west1}"
public_service="${PUBLIC_SERVICE:-aestheticlab-calendar-telegram}"
admin_service="${ADMIN_SERVICE:-aestheticlab-calendar-telegram-admin}"
notification_channels="${NOTIFICATION_CHANNELS:-}"

create_policy_if_missing() {
  local display_name="$1"
  local policy_file="$2"
  local existing

  existing="$(gcloud monitoring policies list \
    --project="${project_id}" \
    --format="value(name,displayName)" |
    awk -F '	' -v display_name="${display_name}" '$2 == display_name { print $1; exit }')"

  if [[ -n "${existing}" ]]; then
    echo "Alert policy already exists: ${display_name} (${existing})"
    return
  fi

  local cmd=(
    gcloud monitoring policies create
    --project="${project_id}"
    --policy-from-file="${policy_file}"
    --quiet
  )

  if [[ -n "${notification_channels}" ]]; then
    cmd+=(--notification-channels="${notification_channels}")
  fi

  "${cmd[@]}"
}

webhook_policy="$(mktemp)"
renewal_policy="$(mktemp)"
expiring_policy="$(mktemp)"
trap 'rm -f "${webhook_policy}" "${renewal_policy}" "${expiring_policy}"' EXIT

cat >"${webhook_policy}" <<EOF
{
  "displayName": "Calendar Telegram public webhook 5xx",
  "enabled": true,
  "combiner": "OR",
  "documentation": {
    "mimeType": "text/markdown",
    "content": "Public Cloud Run webhook is returning 5xx responses. Check Telegram delivery, Firestore, and Google Calendar delta fetch logs."
  },
  "conditions": [
    {
      "displayName": "Public webhook 5xx rate > 0 for 5 minutes",
      "conditionThreshold": {
        "filter": "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.\"service_name\"=\"${public_service}\" AND resource.label.\"location\"=\"${region}\" AND metric.label.\"response_code_class\"=\"5xx\"",
        "comparison": "COMPARISON_GT",
        "thresholdValue": 0,
        "duration": "300s",
        "trigger": {
          "count": 1
        },
        "aggregations": [
          {
            "alignmentPeriod": "300s",
            "perSeriesAligner": "ALIGN_RATE",
            "crossSeriesReducer": "REDUCE_SUM",
            "groupByFields": [
              "resource.label.\"service_name\""
            ]
          }
        ]
      }
    }
  ]
}
EOF

cat >"${renewal_policy}" <<EOF
{
  "displayName": "Calendar Telegram renewal failures",
  "enabled": true,
  "combiner": "OR",
  "documentation": {
    "mimeType": "text/markdown",
    "content": "Admin renewal failed for one or more Google Calendar watch channels. Check admin Cloud Run logs and Calendar API permissions."
  },
  "alertStrategy": {
    "notificationRateLimit": {
      "period": "3600s"
    },
    "autoClose": "604800s"
  },
  "conditions": [
    {
      "displayName": "Renewal failure log event",
      "conditionMatchedLog": {
        "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${admin_service}\" AND textPayload:\"event=calendar_channel_renewal_failure\""
      }
    }
  ]
}
EOF

cat >"${expiring_policy}" <<EOF
{
  "displayName": "Calendar Telegram channel expiring within 24h",
  "enabled": true,
  "combiner": "OR",
  "documentation": {
    "mimeType": "text/markdown",
    "content": "A Google Calendar watch channel is missing an expiration or expires within 24 hours. Renewal should replace it; investigate if this alert persists."
  },
  "alertStrategy": {
    "notificationRateLimit": {
      "period": "21600s"
    },
    "autoClose": "604800s"
  },
  "conditions": [
    {
      "displayName": "Channel expiring log event",
      "conditionMatchedLog": {
        "filter": "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${admin_service}\" AND textPayload:\"event=calendar_channel_expiring\""
      }
    }
  ]
}
EOF

create_policy_if_missing "Calendar Telegram public webhook 5xx" "${webhook_policy}"
create_policy_if_missing "Calendar Telegram renewal failures" "${renewal_policy}"
create_policy_if_missing "Calendar Telegram channel expiring within 24h" "${expiring_policy}"
