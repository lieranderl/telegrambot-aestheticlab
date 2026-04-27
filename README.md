# Calendar Telegram Notifier

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
  Runs tests with coverage, builds one immutable image, deploys both public and admin Cloud Run services, and configures the renewal scheduler.
- [deploy-admin.yml](/Users/evfedoto/Documents/Projects/telegrambot-aestheticlab/.github/workflows/deploy-admin.yml)
  Manual admin-only fallback using the current `latest` image.

The public service remains unauthenticated because Google Calendar push must reach it. The admin service must stay non-public.

## Firestore State

- `{prefix}_calendar_states/{sha1(calendar_id)}`
  Stores `calendar_id`, `label`, `sync_token`, and update metadata.
- `{prefix}_channels/{channel_id}`
  Stores `calendar_id`, `resource_id`, `label`, `token`, and `expiration_ms`.
- `{prefix}_deliveries/{sha1(calendar_id|event_id|event_version)}`
  Stores de-duplication markers.

Configure a Firestore TTL policy on the deliveries collection if you want automatic cleanup of old markers.

## Runtime Notes

- The initial webhook seeds a sync token without replaying historical events.
- Invalid Google sync tokens trigger a reseed.
- Duplicate Telegram sends are suppressed with Firestore delivery markers.
- Sync token updates use optimistic concurrency.
