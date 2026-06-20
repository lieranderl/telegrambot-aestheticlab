# Calendar Telegram Notifier

[![CI](https://github.com/lieranderl/telegrambot-aestheticlab/actions/workflows/ci.yml/badge.svg)](https://github.com/lieranderl/telegrambot-aestheticlab/actions/workflows/ci.yml)
[![Deploy Production to Cloud Run](https://github.com/lieranderl/telegrambot-aestheticlab/actions/workflows/deploy.yml/badge.svg)](https://github.com/lieranderl/telegrambot-aestheticlab/actions/workflows/deploy.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/github/license/lieranderl/telegrambot-aestheticlab)](LICENSE)

FastAPI service that watches one or more Google Calendars and forwards event changes to Telegram. The public runtime is webhook-only. Operational actions run through a separate admin app. Mutable runtime state lives in Firestore.

## Architecture

- `src.main:app`
  Public Cloud Run service with `/health` and `/webhook`.
- `src.admin_main:app`
  Admin-only service with `/admin/register`, `/admin/renew`, `/admin/cleanup`, and `/admin/test-telegram`.
- Firestore
  Stores sync tokens, active channel metadata, webhook validation tokens, and delivery de-duplication markers.

## Security Model

- The public app does not mount admin routes.
- Webhook calls are validated against stored Google channel metadata:
  - `X-Goog-Channel-ID`
  - `X-Goog-Channel-Token`
  - `X-Goog-Resource-ID`
- Admin routes are protected by Cloud Run IAM, not an app-level shared secret.
- Cloud Scheduler invokes `/admin/renew` with OIDC using a dedicated least-privilege service account granted `roles/run.invoker` on the admin service.
- Telegram delivery errors are sanitized before logging or returning API responses, so bot tokens embedded in Telegram API URLs are not exposed.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | Yes | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Yes | Telegram chat ID |
| `WEBHOOK_URL` | Yes | Public webhook URL registered with Google Calendar |
| `CALENDAR_IDS` | Yes | Semicolon-separated `calendar_id|label` pairs |
| `GOOGLE_CLOUD_PROJECT` | Recommended | GCP project fallback when ADC does not provide one |
| `GCP_PROJECT` | Optional | Alternate GCP project fallback |
| `STATE_COLLECTION_PREFIX` | Optional | Firestore collection prefix, default `calendar_telegram` |
| `RENEWAL_LEAD_MINUTES` | Optional | Default renewal window in minutes, default `120` |
| `DELIVERY_TTL_DAYS` | Optional | Firestore delivery marker retention, default `30` |

## Local Development

```bash
uv sync
gcloud auth application-default login
export TELEGRAM_TOKEN="123456:telegram-bot-token"
export TELEGRAM_CHAT_ID="-1001234567890"
export WEBHOOK_URL="https://your-public-url/webhook"
export CALENDAR_IDS="primary|Main Calendar"
export GOOGLE_CLOUD_PROJECT="your-gcp-project"
uv run uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
```

Admin app locally:

```bash
uv run uvicorn src.admin_main:app --host 0.0.0.0 --port 8081 --reload
```

## Testing

```bash
uv run python -m unittest discover -s tests
uv run coverage run -m unittest discover -s tests
uv run coverage report -m
```

Coverage is enforced from [pyproject.toml](/Users/evfedoto/Documents/Projects/telegrambot-aestheticlab/pyproject.toml).

## Deployment

- [deploy.yml](/Users/evfedoto/Documents/Projects/telegrambot-aestheticlab/.github/workflows/deploy.yml)
  Runs tests with coverage, builds one immutable production image tag, deploys both public and admin Cloud Run services, configures scheduler/TTL/alerts, and verifies readiness.
- [deploy-admin.yml](/Users/evfedoto/Documents/Projects/telegrambot-aestheticlab/.github/workflows/deploy-admin.yml)
  Manual admin-only fallback. Requires an immutable image SHA input.

There is no dedicated staging environment. CI validates code, tests, shell scripts, the container build, dependency audit, and deployment workflow contracts without deploying. Real deployment happens only through production workflows. The public service remains unauthenticated because Google Calendar push must reach it. The admin service must stay non-public.

### GitHub Environment Inputs

Create a GitHub Environment named `production`. It must provide:

| Name | Type | Required | Notes |
| --- | --- | --- | --- |
| `GCP_PROJECT` | Secret | Yes | GCP project ID, currently `nail-lab-449417` |
| `GCP_WORKLOAD_ID_PROVIDER` | Secret | Yes | GitHub OIDC Workload Identity provider resource |
| `GCP_SERVICE_ACCOUNT_GITHUB` | Secret | Yes | Deploy service account email used by GitHub Actions |
| `GCP_SERVICE_ACCOUNT` | Secret | Yes | Runtime service account email used by both Cloud Run services |
| `DOCKERHUB_USERNAME` | Secret | Yes | Docker registry username |
| `DOCKERHUB_TOKEN` | Secret | Yes | Docker registry token |
| `DOCKER_IMAGE` | Secret | Yes | Docker image repository without a tag |
| `TELEGRAM_CHAT_ID` | Secret | Yes | Production Telegram destination |
| `WEBHOOK_URL` | Secret | Yes | Production public `/webhook` URL |
| `GCP_MONITORING_NOTIFICATION_CHANNELS` | Secret | No | Comma-separated Monitoring notification channel names |

Required Secret Manager secrets:

| Runtime env | Secret Manager secret |
| --- | --- |
| `TELEGRAM_TOKEN` | `TELEGRAM_TOKEN` |
| `CALENDAR_IDS` | `CALENDAR_IDS` |

Deployment preflight validates required inputs, Docker publishing credentials, service naming, immutable image tags, positive numeric settings, service account formats, and Secret Manager secret names before build or deploy starts.
After GitHub OIDC authentication, deployment also verifies the runtime service account and required Secret Manager secrets before building or pushing an image.

### Deployment Validation Without Staging

Use CI and local checks to validate deployment logic without touching production:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
scripts/audit-dependencies.sh
bash -n scripts/*.sh
uv run coverage run -m unittest discover -s tests
uv run coverage report -m
docker build -t calendar-telegram:test .
```

The test suite includes DevOps contract tests for workflow inputs, production-only deployment assumptions, early-fail validation, immutable image tags, and required setup scripts. Do not run `deploy.yml` or `deploy-admin.yml` unless a production deployment is explicitly approved.

### Deployment IAM

The workflow needs DockerHub credentials to publish the image. The GitHub deploy service account needs GCP permissions to:

- deploy Cloud Run services and set IAM bindings;
- create/update the Cloud Scheduler renewal job;
- configure Firestore TTL for `{prefix}_deliveries.expires_at`;
- create/update Cloud Monitoring alert policies;
- attach Secret Manager secrets to Cloud Run revisions.

The runtime Cloud Run service account must be able to read the configured Secret Manager secrets, read Google Calendar, and read/write the Firestore state collections. The scheduler service account is created by deployment and receives only `roles/run.invoker` on the admin service.

## Firestore State

- `{prefix}_calendar_states/{sha1(calendar_id)}`
  Stores `calendar_id`, `label`, `sync_token`, and update metadata.
- `{prefix}_channels/{channel_id}`
  Stores `calendar_id`, `resource_id`, `label`, `token`, and `expiration_ms`.
- `{prefix}_deliveries/{sha1(calendar_id|event_id|event_version)}`
  Stores de-duplication markers with an `expires_at` timestamp for TTL cleanup.

Deployment runs [configure-firestore-ttl.sh](/Users/evfedoto/Documents/Projects/telegrambot-aestheticlab/scripts/configure-firestore-ttl.sh), which enables Firestore TTL on `{prefix}_deliveries.expires_at`.

## Observability

Deployment runs [configure-alerts.sh](/Users/evfedoto/Documents/Projects/telegrambot-aestheticlab/scripts/configure-alerts.sh) to create alert policies for:

- Public webhook 5xx responses.
- Admin renewal failures.
- Calendar watch channels missing expiration or expiring within 24 hours.

Set `GCP_MONITORING_NOTIFICATION_CHANNELS` to a comma-separated list of Monitoring notification channel resource names to attach notifications during deployment.

## Runtime Notes

- The initial webhook seeds a sync token without replaying historical events.
- Invalid Google sync tokens trigger a reseed.
- Duplicate Telegram sends are suppressed with Firestore delivery markers.
- Sync token updates use optimistic concurrency.
